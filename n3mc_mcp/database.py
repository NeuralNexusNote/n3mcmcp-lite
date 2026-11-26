"""The Redis layer: connection, ephemeral settings, index, save (guard + records in one Lua script),
hybrid search and result shaping, list, recall, delete, repair (spec §3.3–§3.14, §3.16)."""
import hashlib
import math
import os
import socket
import struct
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse

import redis
from redis.commands.search.query import Query

from . import processor
from .config import backend_hint, hostport, no_search_hint
from .processor import (
    CODE_OMITTED, chunk_text, has_visible_text, normalize_text, purify, sanitize_surrogates,
)

MEM_PREFIX = "mem:"
SHA_PREFIX = "mem:sha:"
DOC_PREFIX = "doc:"
DOCSHA_PREFIX = "docsha:"
INDEX_NAME = "n3mc_idx"

ROLE_PREFIXES = ("[user", "[claude")

_SAVE_WITH_GUARD = """
local ok = redis.call('SET', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2])
if not ok then return 0 end
local idx = 3
for i = 2, #KEYS do
  local n = tonumber(ARGV[idx]); idx = idx + 1
  local args = {}
  for j = 1, n do
    args[#args + 1] = ARGV[idx]; args[#args + 1] = ARGV[idx + 1]; idx = idx + 2
  end
  redis.call('HSET', KEYS[i], unpack(args))
  redis.call('EXPIRE', KEYS[i], ARGV[2])
end
return 1
"""

_RELEASE_GUARD = """
if redis.call('GET', KEYS[1]) == ARGV[1] and redis.call('EXISTS', KEYS[2]) == 0 then
  redis.call('DEL', KEYS[1])
  return 1
end
return 0
"""

_HSET_IF_EXISTS = """
if redis.call('EXISTS', KEYS[1]) == 1 then
  redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
  return 1
end
return 0
"""

_DEL_WITH_GUARD = """
local n = redis.call('DEL', KEYS[1])
if redis.call('GET', KEYS[2]) == ARGV[1] then redis.call('DEL', KEYS[2]) end
return n
"""

_DEL_GUARD_IF_OWNED = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

_RETURN_FIELDS = ["content", "timestamp", "timestamp_epoch", "owner_id", "local_id", "agent_name",
                  "session_id", "turn_id", "importance", "parent_id"]


# ── Small helpers ────────────────────────────────────────────────────────────

def sha1_hex(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def guard_hash(text: str) -> str:
    return sha1_hex(normalize_text(text))


def pack_vector(vec) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack_vector(data):
    if not data:
        return None
    n = len(data) // 4
    return list(struct.unpack(f"<{n}f", data[:n * 4]))


def _s(v):
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    return str(v)


def _f(v, default=0.0):
    try:
        x = float(_s(v))
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _doc_bare_id(doc_id) -> str:
    s = _s(doc_id)
    return s[len(MEM_PREFIX):] if s.startswith(MEM_PREFIX) else s


def _uuid7() -> str:
    try:
        import uuid_utils
        return str(uuid_utils.uuid7())
    except Exception:  # noqa: BLE001
        return str(uuid.uuid4())


def is_hook_record(content: str, turn_id: str) -> bool:
    return bool(turn_id) and isinstance(content, str) and content.startswith(ROLE_PREFIXES)


def single_guard_key(content: str, turn_id: str = "") -> str:
    """The guard of a stored independent record (§3.5, §3.16)."""
    if is_hook_record(content, turn_id):
        return f"{SHA_PREFIX}{turn_id}:{guard_hash(content)}"
    return f"{SHA_PREFIX}{guard_hash(content)}"


def doc_guard_key(content: str, turn_id: str = "") -> str:
    if is_hook_record(content, turn_id):
        return f"{DOCSHA_PREFIX}{turn_id}:{guard_hash(content)}"
    return f"{DOCSHA_PREFIX}{guard_hash(content)}"


def _check_field(name, value):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string (got {type(value).__name__})")
    return value


class BackendError(Exception):
    """Redis is not reachable (or has no RediSearch); the message is the recovery hint."""


class Database:
    def __init__(self, cfg: dict, default_session: str = None):
        self.cfg = cfg
        self.url = cfg.get("redis_url") or "redis://localhost:6379/0"
        self.client = None
        self.connected = False
        self.index_ready = False
        self.index_error = None
        self._lock = threading.RLock()
        self._state = None          # last state printed to stderr
        self._scripts = {}
        env_session = os.environ.get("N3MC_SESSION_ID", "").strip()
        self.default_session = default_session or env_session or str(uuid.uuid4())
        processor.configure(cfg.get("embedding_model"))

    # ── connection (§3.9) ────────────────────────────────────────────────

    def _connect_url(self):
        """The URL to connect with: for host "localhost", 127.0.0.1 when a TCP connection there succeeds
        within 0.3 s (§3.3). Windows tries ::1 first; a Redis that listens on 127.0.0.1 only (docker -p
        127.0.0.1:6379:6379) then costs the whole socket_connect_timeout (2 s) on every new connection."""
        try:
            p = urlparse(self.url)
            if (p.hostname or "").lower() != "localhost":
                return self.url
            port = p.port or 6379
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                pass
            netloc = p.netloc
            at = netloc.rfind("@")
            userinfo, hostport_part = (netloc[:at + 1], netloc[at + 1:]) if at >= 0 else ("", netloc)
            new_hostport = "127.0.0.1" + hostport_part[len("localhost"):] \
                if hostport_part.lower().startswith("localhost") else hostport_part
            return urlunparse(p._replace(netloc=userinfo + new_hostport))
        except (OSError, ValueError):
            return self.url

    def _make_client(self):
        url = self._connect_url()
        try:
            return redis.from_url(url, decode_responses=False, protocol=2,
                                  socket_connect_timeout=2, socket_timeout=5)
        except TypeError:
            return redis.from_url(url, decode_responses=False,
                                  socket_connect_timeout=2, socket_timeout=5)

    def _close_client(self):
        c = self.client
        self.client = None
        self._scripts = {}
        if c is not None:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass

    def _log_state(self, state, extra=""):
        if state != self._state:
            self._state = state
            print(f"[n3mc-lite] Redis {state} ({hostport(self.url)}){extra}", file=sys.stderr)

    def mark_disconnected(self):
        with self._lock:
            self.connected = False
            self._close_client()
            self._log_state("disconnected")

    def ensure_connected(self, ephemeral: bool = True) -> str:
        """"ok", or the recovery hint (Redis unreachable, or no RediSearch)."""
        with self._lock:
            if self.client is not None and self.connected:
                try:
                    self.client.ping()
                    if not self.index_ready:
                        self.ensure_index()
                    if not self.index_ready:
                        return no_search_hint(self.url, self.index_error)
                    return "ok"
                except (redis.ConnectionError, redis.TimeoutError, OSError):
                    self.connected = False
            self._close_client()
            try:
                self.client = self._make_client()
                self.client.ping()
            except (redis.ConnectionError, redis.TimeoutError, OSError, ValueError) as e:
                self._close_client()
                self.connected = False
                self._log_state("unreachable", f": {type(e).__name__}")
                return backend_hint(self.url)
            self.connected = True
            self._log_state("connected")
            try:
                if ephemeral:
                    self.enforce_ephemeral()
                self.ensure_index()
            except (redis.ConnectionError, redis.TimeoutError, OSError):
                self.connected = False
                self._close_client()
                return backend_hint(self.url)
            if not self.index_ready:
                return no_search_hint(self.url, self.index_error)
            return "ok"

    def _require(self):
        if self.client is None or not self.connected:
            raise redis.ConnectionError("not connected")
        return self.client

    def _script(self, name, body):
        s = self._scripts.get(name)
        if s is None:
            s = self._require().register_script(body)
            self._scripts[name] = s
        return s

    def _add_notice(self, message):
        notices = self.cfg.setdefault("_notices", [])
        if message not in notices:
            notices.append(message)
            print(f"[n3mc-lite] {message}", file=sys.stderr)

    # ── ephemeral (§3.4) ─────────────────────────────────────────────────

    def enforce_ephemeral(self):
        c = self._require()
        own_db = str(c.connection_pool.connection_kwargs.get("db", 0) or 0)
        shared = False
        try:
            ks = c.info("keyspace")
            for name, stats in (ks or {}).items():
                n = _s(name)
                if n.startswith("db") and n[2:] != own_db:
                    keys = stats.get("keys", 0) if isinstance(stats, dict) else 1
                    if keys:
                        shared = True
                        break
        except redis.ResponseError:
            shared = True
        if not shared:
            for key in c.scan_iter(count=1000):
                k = _s(key)
                if not (k.startswith(MEM_PREFIX) or k.startswith(DOC_PREFIX) or k.startswith(DOCSHA_PREFIX)):
                    shared = True
                    break
        if shared:
            self._add_notice(
                f"shared_redis: the Redis at {hostport(self.url)} also holds keys that are not N3MC's, so its "
                "persistence settings were left unchanged. N3MC memories in it may survive a Redis restart. "
                "Use a dedicated container (docker run -d --name redis-stack -p 127.0.0.1:6379:6379 "
                "redis/redis-stack-server:latest) for Lite.")
            return False
        try:
            c.config_set("appendonly", "no")
            c.config_set("save", "")
            return True
        except redis.ResponseError as e:
            print(f"[n3mc-lite] CONFIG SET was refused: {e}", file=sys.stderr)
            self._add_notice(
                f"config_refused: the Redis at {hostport(self.url)} refused CONFIG SET ({e}), so its "
                "persistence may still be on and N3MC memories may survive a Redis restart. Use a dedicated "
                "container (docker run -d --name redis-stack -p 127.0.0.1:6379:6379 "
                "redis/redis-stack-server:latest) for Lite.")
            return False

    # ── index (§3.5, §3.7, §3.9) ─────────────────────────────────────────

    def _index_info(self):
        info = self._require().execute_command("FT.INFO", INDEX_NAME)
        d = {}
        for k, v in zip(info[::2], info[1::2]):
            d[_s(k)] = v
        attrs = {}
        for a in d.get("attributes") or []:
            items = {}
            for k, v in zip(a[::2], a[1::2]):
                items[_s(k).lower()] = v
            name = _s(items.get("attribute") or items.get("identifier"))
            attrs[name] = items
        return attrs

    def _create_index(self):
        dim = int(self.cfg.get("embedding_dim", 768))
        self._require().execute_command(
            "FT.CREATE", INDEX_NAME, "ON", "HASH", "PREFIX", "1", MEM_PREFIX, "SCHEMA",
            "content_fts", "TEXT", "WEIGHT", "1.0",
            "content_ngram", "TEXT", "WEIGHT", "0.8",
            "timestamp_epoch", "NUMERIC", "SORTABLE",
            "owner_id", "TAG",
            "local_id", "TAG",
            "agent_name", "TAG",
            "session_id", "TAG",
            "turn_id", "TAG",
            "importance", "NUMERIC",
            "access_count", "NUMERIC",
            "parent_id", "TAG",
            "embedding", "VECTOR", "FLAT", "6", "TYPE", "FLOAT32", "DIM", str(dim),
            "DISTANCE_METRIC", "COSINE",
        )

    def ensure_index(self):
        dim = int(self.cfg.get("embedding_dim", 768))
        try:
            self._create_index()
            self.index_ready = True
            self.index_error = None
            return True
        except redis.ResponseError as e:
            msg = str(e)
            if "already exists" not in msg.lower():
                self.index_ready = False
                self.index_error = msg
                print(f"[n3mc-lite] FT.CREATE failed: {msg}", file=sys.stderr)
                return False
        self.index_ready = True
        self.index_error = None
        try:
            attrs = self._index_info()
        except redis.ResponseError:
            return True
        old_dim = None
        emb = attrs.get("embedding")
        if emb is not None:
            try:
                old_dim = int(_s(emb.get("dim")))
            except (TypeError, ValueError):
                old_dim = None
        if old_dim is not None and old_dim != dim:
            self._add_notice(
                f"index_dim_mismatch: the existing index {INDEX_NAME} was built for {old_dim} dimensions but "
                f"embedding_dim is {dim}; new vectors are not indexed. Drop the index (FT.DROPINDEX "
                f"{INDEX_NAME}) or flush the test DB, then restart.")
            return True
        if "content_fts" not in attrs:
            try:
                self._require().execute_command("FT.DROPINDEX", INDEX_NAME)
            except redis.ResponseError as e:
                if "unknown index" not in str(e).lower():
                    raise
            try:
                self._create_index()
            except redis.ResponseError as e:
                if "already exists" not in str(e).lower():
                    self.index_ready = False
                    self.index_error = str(e)
                    return False
            print(f"[n3mc-lite] rebuilt the index {INDEX_NAME} for the content_fts field", file=sys.stderr)
        return True

    # ── save (§3.8, §3.11, §3.13, §3.16) ─────────────────────────────────

    def save_memory(self, content, agent_name=None, owner_id=None, importance=None, session_id=None,
                    turn_id=None, role=None, embed=True, no_model_wait=False):
        cfg = self.cfg
        content = sanitize_surrogates(content)
        agent_name = sanitize_surrogates(agent_name)
        session_id = sanitize_surrogates(session_id)
        turn_id = sanitize_surrogates(turn_id)
        if owner_id is not None and owner_id != "" and owner_id != cfg.get("owner_id"):
            return {"status": "error", "saved": False, "reason": "owner_id mismatch"}
        if importance is None or importance == "":
            imp = 1.0
        else:
            if isinstance(importance, bool) or not isinstance(importance, (int, float)) \
                    or (isinstance(importance, float) and math.isnan(importance)):
                return {"saved": False, "status": "error",
                        "reason": f"importance must be numeric (got {importance})"}
            imp = max(0.5, min(2.0, float(importance)))
        if not isinstance(content, str):
            return {"status": "error", "saved": False, "reason": "empty content"}
        content = content.strip()
        if not content:
            return {"status": "error", "saved": False, "reason": "empty content"}
        if not has_visible_text(content):
            return {"status": "error", "saved": False, "reason": "empty content (no visible characters)"}
        if cfg.get("skip_code_blocks") and "```" in content:
            return {"status": "skipped_code", "saved": False}
        body = purify(content)
        try:
            agent_name = _check_field("agent_name", agent_name)
            session_id = _check_field("session_id", session_id)
            turn_id = _check_field("turn_id", turn_id)
        except TypeError as e:
            return {"status": "error", "saved": False, "reason": str(e)}
        if not session_id.strip():
            session_id = self.default_session
        threshold = int(cfg.get("chunk_threshold", 400))
        overlap = int(cfg.get("chunk_overlap", 100))
        ttl = int(cfg.get("ttl_seconds", 604800))
        prefix = f"[{role}] " if role else ""
        client = self._require()
        now = datetime.now(timezone.utc)
        ts = now.isoformat()
        epoch = now.timestamp()
        base = {
            "timestamp": ts,
            "timestamp_epoch": repr(epoch),
            "owner_id": cfg.get("owner_id", ""),
            "local_id": cfg.get("local_id", ""),
            "agent_name": agent_name,
            "session_id": session_id,
            "turn_id": turn_id,
        }
        warnings = []
        if len(body) <= threshold:
            stored = prefix + body
            gkey = (f"{SHA_PREFIX}{turn_id}:{guard_hash(stored)}" if (role and turn_id)
                    else f"{SHA_PREFIX}{guard_hash(stored)}")
            if client.exists(gkey):
                return {"status": "duplicate", "saved": False}
            vec = None
            if embed:
                vec = processor.embed(stored, "passage", no_wait=no_model_wait)
                if vec is None:
                    warnings.append(("embedding_unavailable", processor.model_unavailable_message()))
            rid = _uuid7()
            fts = processor.fts_text(body)
            fields = dict(base)
            fields.update({
                "id": rid, "content": stored, "content_fts": fts, "content_ngram": processor.build_ngram(fts),
                "importance": repr(imp), "access_count": "0", "parent_id": "",
            })
            if vec is not None:
                fields["embedding"] = pack_vector(vec)
            ok = self._run_save(gkey, rid, ttl, [(MEM_PREFIX + rid, fields)])
            if not ok:
                return {"status": "duplicate", "saved": False}
            result = {"status": "saved", "saved": True, "id": rid, "ttl_seconds": ttl}
        else:
            stored_doc = prefix + body
            gkey = (f"{DOCSHA_PREFIX}{turn_id}:{guard_hash(stored_doc)}" if (role and turn_id)
                    else f"{DOCSHA_PREFIX}{guard_hash(stored_doc)}")
            existing = client.get(gkey)
            if existing is not None:
                return {"status": "duplicate", "saved": False, "parent_id": _s(existing)}
            chunks = chunk_text(body, threshold, overlap)
            n = len(chunks)
            if role:
                chunk_contents = ([f"[{role}] {chunks[0]}"] if n == 1
                                  else [f"[{role} {i + 1}/{n}] {c}" for i, c in enumerate(chunks)])
            else:
                chunk_contents = list(chunks)
            vecs = None
            if embed:
                vecs = processor.embed_many(chunk_contents, "passage", no_wait=no_model_wait)
                if vecs is None:
                    warnings.append(("embedding_unavailable", processor.model_unavailable_message()))
            pid = _uuid7()
            chunk_ids = [_uuid7() for _ in chunks]
            records = []
            doc_fields = dict(base)
            doc_fields.update({"id": pid, "content": stored_doc, "chunk_count": str(n),
                               "chunk_ids": " ".join(chunk_ids)})
            records.append((DOC_PREFIX + pid, doc_fields))
            for i, (cid, cc, raw_chunk) in enumerate(zip(chunk_ids, chunk_contents, chunks)):
                fts = processor.fts_text(raw_chunk)
                f = dict(base)
                f.update({"id": cid, "content": cc, "content_fts": fts,
                          "content_ngram": processor.build_ngram(fts), "importance": repr(imp),
                          "access_count": "0", "parent_id": pid})
                if vecs is not None:
                    f["embedding"] = pack_vector(vecs[i])
                records.append((MEM_PREFIX + cid, f))
            ok = self._run_save(gkey, pid, ttl, records)
            if not ok:
                existing = client.get(gkey)
                return {"status": "duplicate", "saved": False, "parent_id": _s(existing)}
            result = {"status": "saved", "saved": True, "parent_id": pid, "chunks": n, "saved_count": n,
                      "ids": chunk_ids, "ttl_seconds": ttl}
        if self.cfg.get("_config_unreadable"):
            warnings.append(("config_unreadable",
                             "config.json could not be read, so this entry was saved under a temporary "
                             "owner_id and will not be visible after the server restarts. Fix "
                             f"{self.cfg.get('_config_path', 'config.json')} and restart the MCP client."))
        for w, d in warnings:
            add_warning(result, w, d)
        return result

    def _run_save(self, guard_key, owner_value, ttl, records) -> bool:
        keys = [guard_key] + [k for k, _ in records]
        argv = [owner_value, str(ttl)]
        for _key, fields in records:
            argv.append(str(len(fields)))
            for fk, fv in fields.items():
                argv.append(fk)
                argv.append(fv if isinstance(fv, bytes) else str(fv))
        script = self._script("save", _SAVE_WITH_GUARD)
        try:
            res = script(keys=keys, args=argv)
        except redis.ResponseError:
            self._release_guard(guard_key, owner_value, records[0][0])
            raise
        return bool(int(res or 0))

    def _release_guard(self, guard_key, owner_value, record_key):
        try:
            self._script("release", _RELEASE_GUARD)(keys=[guard_key, record_key], args=[owner_value])
        except Exception:  # noqa: BLE001
            pass

    # ── search (§3.6, §3.11, §3.12, §3.14) ───────────────────────────────

    def _time_filter(self, since, until):
        if since is None and until is None:
            return ""
        lo = "-inf" if since is None else repr(float(since))
        hi = "+inf" if until is None else repr(float(until))
        return f"@timestamp_epoch:[{lo} {hi}]"

    def _row_from_doc(self, doc):
        row = {k: _s(getattr(doc, k, "")) for k in _RETURN_FIELDS}
        row["id"] = _doc_bare_id(doc.id)
        return row

    def _vector_search(self, qvec, k, since=None, until=None):
        tf = self._time_filter(since, until)
        base = f"({tf})" if tf else "*"
        q = (Query(f"{base}=>[KNN {k} @embedding $vec AS __dist]")
             .sort_by("__dist").return_fields(*_RETURN_FIELDS, "__dist").paging(0, k).dialect(2))
        try:
            res = self._require().ft(INDEX_NAME).search(q, query_params={"vec": pack_vector(qvec)})
        except (redis.ConnectionError, redis.TimeoutError):
            raise
        except redis.ResponseError as e:
            print(f"[n3mc-lite] vector search failed: {e}", file=sys.stderr)
            return []
        owner = self.cfg.get("owner_id")
        rows = []
        for doc in res.docs:
            row = self._row_from_doc(doc)
            if row["owner_id"] != owner:
                continue
            row["_cos"] = processor.cos_from_distance(getattr(doc, "__dist", None))
            rows.append(row)
        return rows

    def _bm25_search(self, query, k, since=None, until=None):
        terms = processor.drop_lone_cjk_tokens(processor.prepare_query(query)).split()
        if not terms:
            return []
        q = "|".join(terms)
        fts = f"(@content_fts:({q}) | @content_ngram:({q}))"
        tf = self._time_filter(since, until)
        if tf:
            fts = f"({fts}) {tf}"
        res = None
        for scorer in ("BM25", "BM25STD", None):
            qq = Query(fts).with_scores().return_fields(*_RETURN_FIELDS).paging(0, k).dialect(2)
            if scorer:
                qq = qq.scorer(scorer)
            try:
                res = self._require().ft(INDEX_NAME).search(qq)
                break
            except (redis.ConnectionError, redis.TimeoutError):
                raise
            except redis.ResponseError as e:
                if scorer is None or "scorer" not in str(e).lower():
                    print(f"[n3mc-lite] keyword search failed: {e}", file=sys.stderr)
                    return []
        if res is None:
            return []
        owner = self.cfg.get("owner_id")
        rows = []
        for doc in res.docs:
            row = self._row_from_doc(doc)
            if row["owner_id"] != owner:
                continue
            row["_bm25"] = _f(getattr(doc, "score", 0.0))
            rows.append(row)
        return rows

    def _fetch_docs(self, parent_ids):
        c = self._require()
        pipe = c.pipeline(transaction=False)
        for pid in parent_ids:
            pipe.hmget(DOC_PREFIX + pid, ["content", "chunk_count", "chunk_ids", "turn_id", "timestamp",
                                          "timestamp_epoch", "session_id", "owner_id"])
        out = {}
        for pid, vals in zip(parent_ids, pipe.execute()):
            if vals and vals[0] is not None:
                out[pid] = {"content": _s(vals[0]), "chunk_count": int(_f(vals[1], 0)),
                            "chunk_ids": _s(vals[2]).split(), "turn_id": _s(vals[3]),
                            "timestamp": _s(vals[4]), "timestamp_epoch": _s(vals[5]),
                            "session_id": _s(vals[6]), "owner_id": _s(vals[7])}
        return out

    def _resolve_parents(self, rows, docs):
        """Fold chunk hits into their parent documents; keep the best-scored hit per parent (rows must be
        sorted best first). Orphan chunks stay as single rows."""
        out = []
        seen = set()
        for r in rows:
            pid = r.get("parent_id") or ""
            if pid and pid in docs:
                if pid in seen:
                    continue
                seen.add(pid)
                d = docs[pid]
                nr = dict(r)
                nr["_chunk_id"] = r["id"]
                nr["id"] = pid
                nr["content"] = d["content"]
                nr["_doc"] = True
                nr["chunk_count"] = d["chunk_count"]
                nr["chunk_ids"] = d["chunk_ids"]
                nr["turn_id"] = d["turn_id"] or r.get("turn_id", "")
                out.append(nr)
            else:
                key = ("id", r["id"])
                if key in seen:
                    continue
                seen.add(key)
                nr = dict(r)
                nr["_chunk_id"] = r["id"]
                nr["_doc"] = False
                out.append(nr)
        return out

    def _score(self, row, cos, kw, effective_session, now):
        cfg = self.cfg
        decay = processor.time_decay(row.get("timestamp_epoch"), cfg.get("half_life_days", 3), now=now)
        bl = processor.b_local(row.get("importance") or 1.0)
        bs = (cfg.get("b_session_match", 1.0) if (effective_session and row.get("session_id") == effective_session)
              else cfg.get("b_session_mismatch", 0.6))
        base = cos * 0.7 + kw * 0.3
        row["_base"] = base
        row["score"] = base * decay * bl * bs
        row["filter_score"] = base * bl

    def _rerank(self, rows, query):
        cfg = self.cfg
        if not cfg.get("lexical_rerank_enabled", True):
            return
        for r in rows:
            bonus = processor.lexical_bonus(query, r.get("content", ""), cfg.get("rerank_weight", 0.3),
                                            cfg.get("rerank_phrase_weight", 0.2))
            r["score"] += bonus
            r["filter_score"] += bonus

    @staticmethod
    def _newest_key(r):
        return (_f(r.get("timestamp_epoch"), 0.0), r.get("id", ""))

    def _version_order(self, rows):
        cfg = self.cfg
        if not cfg.get("version_order_enabled", True) or len(rows) < 2:
            return rows
        min_chars = int(cfg.get("version_min_chars", 100))
        thr = float(cfg.get("version_sim_threshold", 0.98))
        rep_keys = []
        for r in rows:
            if r.get("_doc"):
                cids = r.get("chunk_ids") or []
                rep_keys.append(MEM_PREFIX + cids[0] if cids else None)
            else:
                rep_keys.append(MEM_PREFIX + r.get("_chunk_id", r["id"]))
        c = self._require()
        pipe = c.pipeline(transaction=False)
        for k in rep_keys:
            if k:
                pipe.hmget(k, ["embedding", "content"])
        fetched = iter(pipe.execute()) if any(rep_keys) else iter(())
        reps = []
        for k in rep_keys:
            if not k:
                reps.append(None)
                continue
            emb, text = next(fetched)
            vec = unpack_vector(emb) if emb else None
            if vec is None or len(_s(text)) < min_chars:
                reps.append(None)
            else:
                reps.append(vec)
        n = len(rows)
        parent = list(range(n))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(n):
            if reps[i] is None:
                continue
            for j in range(i + 1, n):
                if reps[j] is None:
                    continue
                if processor.cosine(reps[i], reps[j]) >= thr:
                    parent[find(i)] = find(j)
        groups = {}
        for i in range(n):
            if reps[i] is not None:
                groups.setdefault(find(i), []).append(i)
        out = list(rows)
        gno = 0
        for members in sorted((m for m in groups.values() if len(m) >= 2), key=lambda m: m[0]):
            gno += 1
            positions = sorted(members)
            pos_scores = [(rows[p]["score"], rows[p]["filter_score"]) for p in positions]
            moved = sorted((dict(rows[p]) for p in positions), key=self._newest_key, reverse=True)
            for p, r, (sc, fs) in zip(positions, moved, pos_scores):
                r["score"] = sc
                r["filter_score"] = fs
                r["_version_group"] = gno
                out[p] = r
        return out

    def search(self, query, limit=None, session_id=None, since=None, until=None, no_model_wait=False):
        """(normal rows, weak rows, model message or None). Rows: content, score, filter_score, id,
        session_id, turn_id, timestamp, _doc, chunk_count, _weak."""
        cfg = self.cfg
        query = sanitize_surrogates(query if isinstance(query, str) else "")
        q = query[: int(cfg.get("search_query_max_chars", 2000))]
        try:
            limit = int(limit) if limit not in (None, "") else int(cfg.get("search_result_limit", 20))
        except (TypeError, ValueError):
            limit = int(cfg.get("search_result_limit", 20))
        limit = max(1, limit)
        session_id = sanitize_surrogates(session_id) if isinstance(session_id, str) else ""
        effective_session = session_id.strip() and session_id or self.default_session
        k = min(limit * 5, 100)
        qvec = processor.embed(q, "query", no_wait=no_model_wait)
        msg = None
        if qvec is None:
            msg = processor.model_unavailable_message()
        vec_rows = self._vector_search(qvec, k, since, until) if qvec is not None else []
        bm_rows = self._bm25_search(q, k, since, until)
        cands = {}
        for r in vec_rows:
            cands[r["id"]] = dict(r)
        for r in bm_rows:
            if r["id"] in cands:
                cands[r["id"]]["_bm25"] = r["_bm25"]
            else:
                cands[r["id"]] = dict(r)
        max_bm25 = max((abs(r.get("_bm25", 0.0)) for r in bm_rows), default=0.0)
        thr = float(cfg.get("bm25_min_threshold", 0.1))
        floor = float(cfg.get("vector_min_cos", 0.83))
        now = time.time()
        normal_c, all_c = [], []
        for cid, r in cands.items():
            cos_actual = r.get("_cos")
            kw = processor.keyword_relevance(r.get("_bm25", 0.0), max_bm25, thr) if "_bm25" in r else 0.0
            in_bm = "_bm25" in r
            vec_ok = cos_actual is not None and cos_actual >= floor
            w = dict(r)
            self._score(w, cos_actual or 0.0, kw, effective_session, now)
            all_c.append(w)
            if vec_ok or in_bm:
                nr = dict(r)
                self._score(nr, cos_actual if vec_ok else 0.0, kw, effective_session, now)
                normal_c.append(nr)
        parent_ids = sorted({r["parent_id"] for r in all_c if r.get("parent_id")})
        docs = self._fetch_docs(parent_ids) if parent_ids else {}
        normal_c.sort(key=lambda r: r["score"], reverse=True)
        normal = self._resolve_parents(normal_c, docs)
        self._rerank(normal, q)
        min_score = float(cfg.get("min_score", 0.2))
        if min_score > 0:
            normal = [r for r in normal if r["filter_score"] >= min_score]
        normal.sort(key=lambda r: (r["score"], self._newest_key(r)), reverse=True)
        normal = self._version_order(normal)
        ratio = float(cfg.get("relative_min_score_ratio", 0.6))
        if ratio > 0 and normal:
            top = max(r["filter_score"] for r in normal)
            normal = [r for r in normal if r.get("_version_group") or r["filter_score"] >= top * ratio]
        weak = []
        min_results = int(cfg.get("min_results", 3))
        need = min(min_results, limit) - len(normal)
        if need > 0 and min_results > 0 and q.strip():
            taken = {r["id"] for r in normal}
            all_c.sort(key=lambda r: r["filter_score"], reverse=True)
            pool = self._resolve_parents(all_c, docs)
            self._rerank(pool, q)
            pool = [r for r in pool if r["filter_score"] >= 1e-6 and r["id"] not in taken]
            pool.sort(key=lambda r: (r["filter_score"], self._newest_key(r)), reverse=True)
            for r in pool:
                if len(weak) >= need:
                    break
                if r["id"] in taken:
                    continue
                taken.add(r["id"])
                r["_weak"] = True
                weak.append(r)
        normal = normal[:limit]
        return normal, weak, msg

    # ── list / recall (§4.3, §5 (4)) ─────────────────────────────────────

    def list_memories(self, limit=20):
        owner = self.cfg.get("owner_id")
        c = self._require()
        singles = []
        offset = 0
        page = 200
        while len(singles) < limit:
            q = (Query("*").sort_by("timestamp_epoch", asc=False)
                 .return_fields("content", "timestamp", "timestamp_epoch", "owner_id", "parent_id", "turn_id")
                 .paging(offset, page).dialect(2))
            res = c.ft(INDEX_NAME).search(q)
            if not res.docs:
                break
            for doc in res.docs:
                if _s(getattr(doc, "owner_id", "")) != owner or _s(getattr(doc, "parent_id", "")):
                    continue
                singles.append({"id": _doc_bare_id(doc.id), "content": _s(doc.content),
                                "timestamp": _s(doc.timestamp), "turn_id": _s(getattr(doc, "turn_id", "")),
                                "tag": ""})
                if len(singles) >= limit:
                    break
            offset += page
            if offset >= int(res.total or 0):
                break
        docs = []
        for key in c.scan_iter(match=DOC_PREFIX + "*", count=1000):
            vals = c.hmget(key, ["owner_id", "content", "timestamp", "chunk_count", "turn_id"])
            if _s(vals[0]) != owner:
                continue
            docs.append({"id": _s(key)[len(DOC_PREFIX):], "content": _s(vals[1]), "timestamp": _s(vals[2]),
                         "turn_id": _s(vals[4]), "tag": f"[doc×{int(_f(vals[3], 0))}]", "_doc": True})
        rows = singles + docs
        rows.sort(key=lambda r: r["timestamp"], reverse=True)
        return rows[:limit]

    def _all_records(self):
        """Every record of the owner: (key, fields) for mem:* (not mem:sha:*) and doc:*."""
        owner = self.cfg.get("owner_id")
        c = self._require()
        out = []
        for pattern in (MEM_PREFIX + "*", DOC_PREFIX + "*"):
            keys = [k for k in c.scan_iter(match=pattern, count=1000) if not _s(k).startswith(SHA_PREFIX)]
            if not keys:
                continue
            pipe = c.pipeline(transaction=False)
            for k in keys:
                pipe.hmget(k, ["owner_id", "content", "timestamp", "timestamp_epoch", "turn_id", "parent_id",
                               "session_id", "chunk_count", "chunk_ids"])
            for k, vals in zip(keys, pipe.execute()):
                if _s(vals[0]) != owner:
                    continue
                key = _s(k)
                out.append((key, {"content": _s(vals[1]), "timestamp": _s(vals[2]),
                                  "timestamp_epoch": _f(vals[3], 0.0), "turn_id": _s(vals[4]),
                                  "parent_id": _s(vals[5]), "session_id": _s(vals[6]),
                                  "chunk_count": int(_f(vals[7], 0)), "chunk_ids": _s(vals[8]).split()}))
        return out

    def recall_thread(self, turn_id, before=2, after=2, session_id=None):
        """Rows of the turn plus `before` / `after` rows of the same session (§4.3.1). A list of rows; [] when
        nothing matches; {"status": "ambiguous", ...} when the turn_id is in several sessions and no
        session_id was given."""
        recs = self._all_records()
        doc_ids = {k[len(DOC_PREFIX):] for k, _ in recs if k.startswith(DOC_PREFIX)}
        entries = []
        for key, f in recs:
            if key.startswith(DOC_PREFIX):
                entries.append({"id": key[len(DOC_PREFIX):], "tag": f"[doc×{f['chunk_count']}]", **f})
            else:
                if f["parent_id"] and f["parent_id"] in doc_ids:
                    continue
                entries.append({"id": key[len(MEM_PREFIX):], "tag": "", **f})
        session_id = session_id.strip() if isinstance(session_id, str) else ""
        targets = [e for e in entries if e["turn_id"] == turn_id and (not session_id or e["session_id"] == session_id)]
        if not targets:
            return []
        sessions = sorted({e["session_id"] for e in targets})
        if len(sessions) > 1:
            return {"status": "ambiguous", "turn_id": turn_id, "session_ids": sessions,
                    "reason": "this turn_id is in several sessions; call recall_thread again with one of "
                              "session_ids as session_id"}
        session = sessions[0]
        entries = [e for e in entries if e["session_id"] == session]
        entries.sort(key=lambda e: (e["timestamp_epoch"], e["id"]))
        idx = [i for i, e in enumerate(entries) if e["turn_id"] == turn_id]
        lo = max(0, idx[0] - max(0, int(before)))
        hi = min(len(entries), idx[-1] + max(0, int(after)) + 1)
        out = []
        for e in entries[lo:hi]:
            e = dict(e)
            e["_target"] = e["turn_id"] == turn_id
            out.append(e)
        return out

    # ── delete (§3.5, §3.11, §4.3) ───────────────────────────────────────

    def _delete_single(self, pipe_or_client, rec_id, content, turn_id):
        gkey = single_guard_key(content, turn_id)
        self._script("delguard", _DEL_WITH_GUARD)(keys=[MEM_PREFIX + rec_id, gkey], args=[rec_id],
                                                 client=pipe_or_client)

    def _doc_chunk_ids(self, pid, fields):
        ids = fields.get("chunk_ids") or []
        if ids:
            return ids
        c = self._require()
        found = []
        try:
            q = Query(f"@parent_id:{{{pid}}}").return_fields("parent_id").paging(0, 10000).dialect(2)
            res = c.ft(INDEX_NAME).search(q)
            found = [_doc_bare_id(d.id) for d in res.docs]
        except redis.ResponseError:
            found = []
        if not found:
            for k in c.scan_iter(match=MEM_PREFIX + "*", count=1000):
                ks = _s(k)
                if ks.startswith(SHA_PREFIX):
                    continue
                try:
                    if _s(c.hget(k, "parent_id")) == pid:
                        found.append(ks[len(MEM_PREFIX):])
                except redis.ResponseError:
                    continue
        return found

    def delete_memory(self, mem_id):
        c = self._require()
        owner = self.cfg.get("owner_id")
        raw = mem_id if isinstance(mem_id, str) else ""
        bare = raw.strip()
        if bare.startswith(DOC_PREFIX):
            bare = bare[len(DOC_PREFIX):]
        elif bare.startswith(MEM_PREFIX):
            bare = bare[len(MEM_PREFIX):]
        if not bare or bare.startswith(DOC_PREFIX) or bare.startswith(MEM_PREFIX):
            return {"status": "not_found", "id": bare}
        vals = c.hmget(DOC_PREFIX + bare, ["owner_id", "content", "turn_id", "chunk_ids"])
        if vals[0] is not None or vals[1] is not None:
            if _s(vals[0]) != owner:
                return {"status": "not_found", "id": bare}
            chunk_ids = self._doc_chunk_ids(bare, {"chunk_ids": _s(vals[3]).split()})
            gkey = doc_guard_key(_s(vals[1]), _s(vals[2]))
            pipe = c.pipeline(transaction=True)
            pipe.delete(DOC_PREFIX + bare)
            for cid in chunk_ids:
                pipe.delete(MEM_PREFIX + cid)
            self._script("delguardonly", _DEL_GUARD_IF_OWNED)(keys=[gkey], args=[bare], client=pipe)
            pipe.execute()
            return {"status": "deleted", "id": bare, "chunks_deleted": len(chunk_ids)}
        vals = c.hmget(MEM_PREFIX + bare, ["owner_id", "content", "turn_id", "parent_id"])
        if vals[0] is None and vals[1] is None:
            return {"status": "not_found", "id": bare}
        if _s(vals[0]) != owner:
            return {"status": "not_found", "id": bare}
        if _s(vals[3]):
            c.delete(MEM_PREFIX + bare)
        else:
            self._delete_single(c, bare, _s(vals[1]), _s(vals[2]))
        return {"status": "deleted", "id": bare}

    def delete_by_session(self, session_id):
        if not isinstance(session_id, str) or not session_id.strip():
            return {"status": "error", "reason": "session_id required"}
        c = self._require()
        recs = self._all_records()
        docs = {k[len(DOC_PREFIX):]: f for k, f in recs if k.startswith(DOC_PREFIX) and f["session_id"] == session_id}
        doc_chunks = set()
        for pid, f in docs.items():
            doc_chunks.update(self._doc_chunk_ids(pid, f))
        singles = []
        orphan_chunks = []
        for k, f in recs:
            if not k.startswith(MEM_PREFIX):
                continue
            rid = k[len(MEM_PREFIX):]
            if rid in doc_chunks:
                continue
            if f["session_id"] != session_id:
                continue
            if f["parent_id"]:
                orphan_chunks.append(rid)
            else:
                singles.append((rid, f))
        if not docs and not singles and not orphan_chunks:
            return {"status": "not_found", "session_id": session_id, "deleted": 0}
        pipe = c.pipeline(transaction=True)
        for pid, f in docs.items():
            pipe.delete(DOC_PREFIX + pid)
            self._script("delguardonly", _DEL_GUARD_IF_OWNED)(keys=[doc_guard_key(f["content"], f["turn_id"])],
                                                             args=[pid], client=pipe)
        for cid in doc_chunks:
            pipe.delete(MEM_PREFIX + cid)
        for cid in orphan_chunks:
            pipe.delete(MEM_PREFIX + cid)
        for rid, f in singles:
            self._delete_single(pipe, rid, f["content"], f["turn_id"])
        pipe.execute()
        d, ch, s = len(docs), len(doc_chunks) + len(orphan_chunks), len(singles)
        return {"status": "deleted", "session_id": session_id, "documents_deleted": d, "chunks_deleted": ch,
                "singles_deleted": s, "deleted": d + ch + s}

    # ── repair (§3.10) ───────────────────────────────────────────────────

    def repair(self, no_model_wait=False):
        self.ensure_index()
        if not self.index_ready:
            return {"status": "error", "message": no_search_hint(self.url, self.index_error)}
        c = self._require()
        owner = self.cfg.get("owner_id")
        model = processor.get_model(no_wait=no_model_wait)
        missing = []
        for k in c.scan_iter(match=MEM_PREFIX + "*", count=1000):
            ks = _s(k)
            if ks.startswith(SHA_PREFIX):
                continue
            try:
                vals = c.hmget(k, ["owner_id", "embedding", "content"])
            except redis.ResponseError:
                continue
            if _s(vals[0]) != owner or vals[1]:
                continue
            missing.append((ks, _s(vals[2])))
        result = {"status": "ok", "message": "index ensured", "reembedded": 0, "remaining": len(missing)}
        if model is None:
            add_warning(result, "embedding_unavailable", processor.model_unavailable_message())
            return result
        batch = missing[:200]
        reembedded = 0
        if batch:
            vecs = processor.embed_many([t for _, t in batch], "passage", no_wait=no_model_wait)
            if vecs is None:
                add_warning(result, "embedding_unavailable", processor.model_unavailable_message())
                return result
            script = self._script("hset", _HSET_IF_EXISTS)
            pipe = c.pipeline(transaction=False)
            for (key, _t), v in zip(batch, vecs):
                script(keys=[key], args=["embedding", pack_vector(v)], client=pipe)
            for r in pipe.execute():
                if int(r or 0) == 1:
                    reembedded += 1
        result["reembedded"] = reembedded
        result["remaining"] = len(missing) - len(batch)
        return result


def add_warning(result: dict, warning: str, detail: str):
    """Add a warning/detail pair; a second one joins the first (§6: "embedding_unavailable; config_unreadable")."""
    if not warning:
        return result
    if result.get("warning"):
        parts = result["warning"].split("; ")
        if warning in parts:
            return result
        order = ["embedding_unavailable", "config_unreadable"]
        items = [(result["warning"], result.get("detail", "")), (warning, detail or "")]
        items.sort(key=lambda it: order.index(it[0]) if it[0] in order else 99)
        result["warning"] = "; ".join(i[0] for i in items)
        result["detail"] = " ".join(i[1] for i in items if i[1])
    else:
        result["warning"] = warning
        if detail:
            result["detail"] = detail
    return result
