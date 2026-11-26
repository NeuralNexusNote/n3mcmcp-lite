"""The Redis layer (spec §3.3–§3.14, §3.16): connection and per-call check, enforce_ephemeral, the
RediSearch index, saving with the guard script, hybrid search and result shaping, list, recall,
delete, repair.
"""
import hashlib
import math
import os
import struct
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

import redis

from . import processor
from .config import _hostport, backend_hint, index_hint, config_unreadable_detail, redis_db_index
from .config import redis_display  # noqa: F401  (re-exported)

INDEX = "n3mc_idx"
MEM = "mem:"
SHA = "mem:sha:"
DOC = "doc:"
DOCSHA = "docsha:"
TEST_MARKER = "__n3mc_test_db__"

CONN_ERRORS = (redis.ConnectionError, redis.TimeoutError)

ROW_FIELDS = ["content", "timestamp", "timestamp_epoch", "owner_id", "local_id", "agent_name",
              "session_id", "turn_id", "importance", "access_count", "parent_id"]

SAVE_WITH_GUARD = """
if not redis.call('SET', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2]) then
  return 0
end
local pos = 3
for i = 2, #KEYS do
  local n = tonumber(ARGV[pos])
  pos = pos + 1
  local args = {}
  for j = 1, n do
    args[#args + 1] = ARGV[pos]
    args[#args + 1] = ARGV[pos + 1]
    pos = pos + 2
  end
  redis.call('HSET', KEYS[i], unpack(args))
  redis.call('EXPIRE', KEYS[i], ARGV[2])
end
return 1
"""

INCR_IF_EXISTS = """
if redis.call('EXISTS', KEYS[1]) == 1 then
  return redis.call('HINCRBY', KEYS[1], ARGV[1], ARGV[2])
end
return false
"""

HSET_IF_EXISTS = """
if redis.call('EXISTS', KEYS[1]) == 1 then
  redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
  return 1
end
return 0
"""

# KEYS[1] = guard, KEYS[2] = the record (mem:<id> or doc:<id>), KEYS[3..] = chunks; ARGV[1] = id.
# The guard goes only when it still points at this record.
DELETE_WITH_GUARD = """
local chunks = 0
for i = 3, #KEYS do
  chunks = chunks + redis.call('DEL', KEYS[i])
end
local rec = redis.call('DEL', KEYS[2])
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('DEL', KEYS[1])
end
return {rec, chunks}
"""

# Release a guard after a failed save: only when it holds this save's id and the record is absent.
RELEASE_GUARD = """
if redis.call('GET', KEYS[1]) == ARGV[1] and redis.call('EXISTS', KEYS[2]) == 0 then
  redis.call('DEL', KEYS[1])
  return 1
end
return 0
"""


class BackendError(Exception):
    """Redis (or RediSearch) is not usable; the message is the recovery hint."""


def sha1_hex(text: str) -> str:
    return hashlib.sha1(processor.normalize_text(text).encode("utf-8")).hexdigest()


def vec_to_bytes(vec) -> bytes:
    """FLOAT32, little endian (spec §3.5)."""
    vec = list(vec)
    return struct.pack(f"<{len(vec)}f", *vec)


def bytes_to_vec(b):
    if not b:
        return None
    return list(struct.unpack(f"<{len(b) // 4}f", b))


def _s(v, default=""):
    if v is None:
        return default
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return str(v)


def _f(v, default=0.0):
    try:
        return float(_s(v))
    except (TypeError, ValueError):
        return default


def _doc_bare_id(key) -> str:
    k = _s(key)
    return k[len(MEM):] if k.startswith(MEM) else k


def is_hook_content(content: str) -> bool:
    return isinstance(content, str) and (content.startswith("[user") or content.startswith("[claude"))


def guard_key(content: str, turn_id, doc: bool) -> str:
    """The exact-duplicate guard of a stored content (spec §3.8, §3.16)."""
    prefix = DOCSHA if doc else SHA
    if turn_id and is_hook_content(content):
        return f"{prefix}{turn_id}:{sha1_hex(content)}"
    return f"{prefix}{sha1_hex(content)}"


def _utc_now():
    now = datetime.now(timezone.utc)
    return now.isoformat(), now.timestamp()


def _new_id() -> str:
    try:
        import uuid_utils
        return str(uuid_utils.uuid7())
    except Exception:  # noqa: BLE001
        return str(uuid.uuid4())


def _parse_search(res):
    """[(key, score or None, {field: value})] from a RESP2 (or RESP3 dict) FT.SEARCH reply."""
    out = []
    if isinstance(res, dict):
        for r in res.get(b"results", res.get("results", [])) or []:
            attrs = r.get(b"extra_attributes", r.get("extra_attributes", {})) or {}
            fields = {_s(k): v for k, v in attrs.items()}
            out.append((_s(r.get(b"id", r.get("id"))), r.get(b"score", r.get("score")), fields))
        return out
    return out


def _parse_search_list(res, withscores):
    out = []
    if isinstance(res, dict):
        return _parse_search(res)
    if not res:
        return out
    i = 1
    while i < len(res):
        key = res[i]
        i += 1
        score = None
        if withscores:
            score = res[i]
            i += 1
        fields = {}
        if i < len(res) and isinstance(res[i], list):
            arr = res[i]
            for j in range(0, len(arr) - 1, 2):
                fields[_s(arr[j])] = arr[j + 1]
            i += 1
        out.append((_s(key), score, fields))
    return out


def _flatten(x):
    if isinstance(x, (list, tuple)):
        for y in x:
            yield from _flatten(y)
    elif isinstance(x, dict):
        for k, v in x.items():
            yield k
            yield from _flatten(v)
    else:
        yield x


class Database:
    def __init__(self, cfg, default_session=None, enforce_on_connect=True):
        self.cfg = cfg
        self.enforce_on_connect = enforce_on_connect
        self.url = cfg["redis_url"]
        self.client = None
        self.index_ok = False
        self.index_error = None
        self._lock = threading.RLock()
        self._notice_lock = threading.Lock()
        self._state = None
        env_session = os.environ.get("N3MC_SESSION_ID", "").strip()
        self.default_session = default_session or env_session or str(uuid.uuid4())
        self._scripts = {}

    # ── notices ──────────────────────────────────────────────────────────────

    def add_notice(self, message: str) -> None:
        with self._notice_lock:
            lst = self.cfg.setdefault("_notices", [])
            if message not in lst:
                lst.append(message)
        try:
            sys.stderr.write(f"[n3mc-lite] {message}\n")
        except Exception:  # noqa: BLE001
            pass

    # ── connection (spec §3.9) ───────────────────────────────────────────────

    def _new_client(self):
        return redis.Redis.from_url(self.url, decode_responses=False, socket_connect_timeout=2,
                                    socket_timeout=5, protocol=2)

    def _register_scripts(self):
        c = self.client
        self._scripts = {
            "save": c.register_script(SAVE_WITH_GUARD),
            "incr": c.register_script(INCR_IF_EXISTS),
            "hset": c.register_script(HSET_IF_EXISTS),
            "delete": c.register_script(DELETE_WITH_GUARD),
            "release": c.register_script(RELEASE_GUARD),
        }

    def _set_state(self, state, detail=None):
        if state != self._state:
            if state == "down":
                sys.stderr.write(f"[n3mc-lite] Redis is not reachable ({detail}). {backend_hint(self.url)}\n")
            elif state == "up" and self._state == "down":
                sys.stderr.write(f"[n3mc-lite] Redis at {_hostport(self.url)} is reachable again.\n")
            self._state = state

    def mark_disconnected(self):
        with self._lock:
            if self.client is not None:
                try:
                    self.client.close()
                except Exception:  # noqa: BLE001
                    pass
            self.client = None
            self.index_ok = False
            self._set_state("down", "connection lost")

    def close(self):
        with self._lock:
            if self.client is not None:
                try:
                    self.client.close()
                except Exception:  # noqa: BLE001
                    pass
            self.client = None

    def ensure_connected(self, enforce: bool = True) -> str:
        """'ok', or raises BackendError with the recovery hint (spec §3.9)."""
        with self._lock:
            if self.client is not None:
                try:
                    self.client.ping()
                except Exception:  # noqa: BLE001
                    try:
                        self.client.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self.client = None
                    self.index_ok = False
                else:
                    if not self.index_ok:
                        try:
                            self._try_index()
                        except CONN_ERRORS as e:
                            self.mark_disconnected()
                            self._set_state("down", f"{type(e).__name__}: {e}")
                            raise BackendError(backend_hint(self.url))
                    if not self.index_ok:
                        raise BackendError(index_hint(self.url, self.index_error))
                    return "ok"
            c = None
            try:
                c = self._new_client()
                c.ping()
            except Exception as e:  # noqa: BLE001
                if c is not None:
                    try:
                        c.close()
                    except Exception:  # noqa: BLE001
                        pass
                self._set_state("down", f"{type(e).__name__}: {e}")
                raise BackendError(backend_hint(self.url))
            self.client = c
            self._register_scripts()
            try:
                if enforce:
                    self.enforce_ephemeral()
                self._try_index()
            except CONN_ERRORS as e:
                self.mark_disconnected()
                self._set_state("down", f"{type(e).__name__}: {e}")
                raise BackendError(backend_hint(self.url))
            self._set_state("up")
            if not self.index_ok:
                raise BackendError(index_hint(self.url, self.index_error))
            return "ok"

    def _require(self):
        if self.client is None:
            self.ensure_connected(enforce=self.enforce_on_connect)

    def _try_index(self):
        try:
            self.ensure_index()
            self.index_ok = True
            self.index_error = None
        except CONN_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001
            self.index_ok = False
            self.index_error = f"{type(e).__name__}: {e}" if not isinstance(e, redis.ResponseError) else str(e)

    # ── persistence (spec §3.4) ──────────────────────────────────────────────

    def _shared_notice(self):
        return (f"shared_redis: the Redis at {_hostport(self.url)} also holds keys that are not N3MC's, so its "
                "persistence settings were left unchanged. N3MC memories in it may survive a Redis restart. "
                "Use a dedicated container (docker run -d --name redis-stack -p 127.0.0.1:6379:6379 "
                "redis/redis-stack-server:latest) for Lite.")

    def enforce_ephemeral(self):
        c = self.client
        db = redis_db_index(self.url)
        try:
            ks = c.info("keyspace")
        except redis.ResponseError:
            self.add_notice(self._shared_notice())
            return
        for k, v in (ks or {}).items():
            name = _s(k)
            keys = v.get("keys", 0) if isinstance(v, dict) else 0
            if name != f"db{db}" and name.startswith("db") and keys:
                self.add_notice(self._shared_notice())
                return
        cursor = 0
        while True:
            cursor, keys = c.scan(cursor=cursor, count=1000)
            for key in keys:
                k = _s(key)
                if not (k.startswith(MEM) or k.startswith(DOC) or k.startswith(DOCSHA)):
                    self.add_notice(self._shared_notice())
                    return
            if int(cursor) == 0:
                break
        try:
            c.config_set("appendonly", "no")
            c.config_set("save", "")
        except redis.ResponseError as e:
            sys.stderr.write(f"[n3mc-lite] CONFIG SET was refused: {e}\n")
            self.add_notice(
                f"config_refused: the Redis at {_hostport(self.url)} refused CONFIG SET ({e}), so its "
                "persistence could not be turned off and N3MC memories in it may survive a Redis restart. "
                "Use a dedicated container (docker run -d --name redis-stack -p 127.0.0.1:6379:6379 "
                "redis/redis-stack-server:latest) for Lite.")

    # ── index (spec §3.5, §3.7, §3.9 step 3) ─────────────────────────────────

    def _ft_info(self):
        """(attribute names, vector DIM) of the index, or None when it does not exist."""
        try:
            info = self.client.execute_command("FT.INFO", INDEX)
        except redis.ResponseError as e:
            msg = str(e).lower()
            if "unknown index" in msg or "no such index" in msg or "not found" in msg:
                return None
            raise
        d = {}
        if isinstance(info, dict):
            d = {_s(k): v for k, v in info.items()}
        else:
            for i in range(0, len(info) - 1, 2):
                d[_s(info[i])] = info[i + 1]
        names = set()
        dim = None
        for attr in d.get("attributes", []) or []:
            flat = list(_flatten(attr))
            for j in range(len(flat) - 1):
                key = _s(flat[j]).lower()
                if key in ("identifier", "attribute"):
                    names.add(_s(flat[j + 1]))
                if key == "dim":
                    try:
                        dim = int(_s(flat[j + 1]))
                    except ValueError:
                        pass
        return names, dim

    def _ft_create(self):
        dim = int(self.cfg["embedding_dim"])
        self.client.execute_command(
            "FT.CREATE", INDEX, "ON", "HASH", "PREFIX", 1, MEM, "SCHEMA",
            "content_fts", "TEXT", "WEIGHT", 1.0,
            "content_ngram", "TEXT", "WEIGHT", 0.8,
            "timestamp_epoch", "NUMERIC", "SORTABLE",
            "owner_id", "TAG", "local_id", "TAG", "agent_name", "TAG", "session_id", "TAG", "turn_id", "TAG",
            "importance", "NUMERIC", "access_count", "NUMERIC", "parent_id", "TAG",
            "embedding", "VECTOR", "FLAT", 6, "TYPE", "FLOAT32", "DIM", dim, "DISTANCE_METRIC", "COSINE")

    def ensure_index(self):
        if self.client is None:
            raise redis.ConnectionError("not connected")
        info = self._ft_info()
        if info is None:
            try:
                self._ft_create()
                return
            except redis.ResponseError as e:
                if "already exists" not in str(e).lower():
                    raise
                info = self._ft_info()
                if info is None:
                    return
        names, dim = info
        want = int(self.cfg["embedding_dim"])
        if dim is not None and dim != want:
            self.add_notice(
                f"index_dim_mismatch: the existing index {INDEX} was built for {dim} dimensions but embedding_dim "
                f"is {want}; new vectors are not indexed. Drop the index (FT.DROPINDEX {INDEX}) or flush the test "
                "DB, then restart.")
            return
        if "content_fts" not in names:
            try:
                self.client.execute_command("FT.DROPINDEX", INDEX)
            except redis.ResponseError as e:
                if "unknown index" not in str(e).lower():
                    raise
            try:
                self._ft_create()
            except redis.ResponseError as e:
                if "already exists" not in str(e).lower():
                    raise

    # ── save (spec §3.8, §3.11, §3.13, §3.16) ────────────────────────────────

    def _check_str(self, name, value):
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string (got {type(value).__name__})")
        return processor.sanitize_surrogates(value)

    def save_memory(self, content, agent_name=None, owner_id=None, importance=None, session_id=None,
                    turn_id=None, role=None, embed=True, no_model_wait=False) -> dict:
        cfg = self.cfg
        if owner_id is not None and owner_id != "" and owner_id != cfg["owner_id"]:
            return {"status": "error", "saved": False, "reason": "owner_id mismatch"}
        if importance is None:
            imp = 1.0
        else:
            ok = isinstance(importance, (int, float)) and not isinstance(importance, bool)
            if not ok or (isinstance(importance, float) and math.isnan(importance)):
                return {"saved": False, "status": "error", "reason": f"importance must be numeric (got {importance})"}
            imp = max(0.5, min(2.0, float(importance)))
        if content is None:
            content = ""
        if not isinstance(content, str):
            return {"status": "error", "saved": False, "reason": "content must be a string"}
        try:
            agent_name = self._check_str("agent_name", agent_name)
            session_id = self._check_str("session_id", session_id)
            turn_id = self._check_str("turn_id", turn_id)
        except ValueError as e:
            return {"status": "error", "saved": False, "reason": str(e)}
        if role is not None and role not in ("user", "claude"):
            return {"status": "error", "saved": False, "reason": "role must be \"user\" or \"claude\""}
        body = processor.sanitize_surrogates(content).strip()
        if not body:
            return {"status": "error", "saved": False, "reason": "empty content"}
        if not processor.has_visible_text(body):
            return {"status": "error", "saved": False, "reason": "empty content (no visible characters)"}
        if cfg.get("skip_code_blocks") and "```" in body:
            return {"status": "skipped_code", "saved": False}
        body = processor.purify(body)
        self._require()
        session = session_id.strip() or self.default_session
        threshold = int(cfg["chunk_threshold"])
        if len(body) <= threshold:
            return self._save_single(body, agent_name, session, turn_id, role, imp, embed, no_model_wait)
        return self._save_long(body, agent_name, session, turn_id, role, imp, embed, no_model_wait)

    def _base_fields(self, agent_name, session, turn_id):
        ts, epoch = _utc_now()
        return {
            "timestamp": ts, "timestamp_epoch": repr(epoch),
            "owner_id": self.cfg["owner_id"], "local_id": self.cfg["local_id"],
            "agent_name": agent_name, "session_id": session, "turn_id": turn_id,
        }

    def _warnings(self, result, emb_missing):
        warnings, details = [], []
        if emb_missing:
            warnings.append("embedding_unavailable")
            details.append(processor.unavailable_message())
        if self.cfg.get("_unreadable"):
            warnings.append("config_unreadable")
            details.append(config_unreadable_detail(self.cfg))
        if warnings:
            result["warning"] = "; ".join(warnings)
            result["detail"] = " ".join(details)
        return result

    @staticmethod
    def _argv(records):
        out = []
        for fields in records:
            out.append(len(fields))
            for k, v in fields.items():
                out.append(k)
                out.append(v)
        return out

    def _run_save(self, guard, rid, record_key, keys, argv):
        """Run SAVE_WITH_GUARD. On a failure that is not a lost connection, release the guard when it
        still holds this id and the record is absent, and return the error dict."""
        try:
            return self._scripts["save"](keys=[guard] + keys, args=argv), None
        except CONN_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001
            try:
                self._scripts["release"](keys=[guard, record_key], args=[rid])
            except CONN_ERRORS:
                raise
            except Exception:  # noqa: BLE001
                pass
            return None, {"status": "error", "saved": False, "reason": f"{type(e).__name__}: {e}"}

    def _save_single(self, body, agent_name, session, turn_id, role, imp, embed, no_model_wait):
        cfg = self.cfg
        stored = f"[{role}] {body}" if role else body
        guard = guard_key(stored, turn_id if role else None, doc=False) if role else f"{SHA}{sha1_hex(stored)}"
        if self.client.exists(guard):
            return {"status": "duplicate", "saved": False}
        vec = None
        emb_missing = False
        if embed:
            vec = processor.embed(stored, "passage", no_model_wait=no_model_wait)
            emb_missing = vec is None
        rid = _new_id()
        try:
            fts, ngram = processor.index_texts(body)
            fields = {"id": rid, "content": stored, "content_ngram": ngram, "content_fts": fts}
            fields.update(self._base_fields(agent_name, session, turn_id))
            fields.update({"importance": repr(float(imp)), "access_count": "0", "parent_id": ""})
            if vec is not None:
                fields["embedding"] = vec_to_bytes(vec)
            ttl = int(cfg["ttl_seconds"])
            argv = [rid, ttl] + self._argv([fields])
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "saved": False, "reason": f"{type(e).__name__}: {e}"}
        res, err = self._run_save(guard, rid, MEM + rid, [MEM + rid], argv)
        if err:
            return err
        if not res:
            return {"status": "duplicate", "saved": False}
        return self._warnings({"status": "saved", "saved": True, "id": rid, "ttl_seconds": ttl}, emb_missing)

    def _save_long(self, body, agent_name, session, turn_id, role, imp, embed, no_model_wait):
        cfg = self.cfg
        stored_parent = f"[{role}] {body}" if role else body
        guard = (guard_key(stored_parent, turn_id, doc=True) if role else f"{DOCSHA}{sha1_hex(stored_parent)}")
        if self.client.exists(guard):
            existing = _s(self.client.get(guard)) or None
            out = {"status": "duplicate", "saved": False}
            if existing:
                out["parent_id"] = existing
            return out
        chunks = processor.chunk_text(body, int(cfg["chunk_threshold"]), int(cfg["chunk_overlap"]))
        n = len(chunks)
        if role:
            stored_chunks = ([f"[{role}] {c}" for c in chunks] if n == 1
                             else [f"[{role} {i + 1}/{n}] {c}" for i, c in enumerate(chunks)])
        else:
            stored_chunks = list(chunks)
        vecs = [None] * n
        emb_missing = False
        if embed:
            for i, sc in enumerate(stored_chunks):
                v = processor.embed(sc, "passage", no_model_wait=no_model_wait)
                if v is None:
                    emb_missing = True
                    vecs = [None] * n
                    break
                vecs[i] = v
        pid = _new_id()
        ids = [_new_id() for _ in range(n)]
        try:
            base = self._base_fields(agent_name, session, turn_id)
            parent = {"id": pid, "content": stored_parent}
            parent.update(base)
            parent.update({"chunk_count": str(n), "chunk_ids": " ".join(ids)})
            records = [parent]
            for i in range(n):
                fts, ngram = processor.index_texts(chunks[i])
                f = {"id": ids[i], "content": stored_chunks[i], "content_ngram": ngram, "content_fts": fts}
                f.update(base)
                f.update({"importance": repr(float(imp)), "access_count": "0", "parent_id": pid})
                if vecs[i] is not None:
                    f["embedding"] = vec_to_bytes(vecs[i])
                records.append(f)
            ttl = int(cfg["ttl_seconds"])
            keys = [DOC + pid] + [MEM + i for i in ids]
            argv = [pid, ttl] + self._argv(records)
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "saved": False, "reason": f"{type(e).__name__}: {e}"}
        res, err = self._run_save(guard, pid, DOC + pid, keys, argv)
        if err:
            return err
        if not res:
            out = {"status": "duplicate", "saved": False}
            existing = _s(self.client.get(guard)) or None
            if existing:
                out["parent_id"] = existing
            return out
        result = {"status": "saved", "saved": True, "parent_id": pid, "chunks": n, "saved_count": n,
                  "ids": ids, "ttl_seconds": ttl}
        return self._warnings(result, emb_missing)

    # ── FT.SEARCH helpers ────────────────────────────────────────────────────

    def _ft_search(self, query, return_fields, limit, offset=0, params=None, sortby=None,
                   withscores=False, scorer=None):
        args = ["FT.SEARCH", INDEX, query]
        if withscores:
            args.append("WITHSCORES")
        if scorer:
            args += ["SCORER", scorer]
        if params:
            args += ["PARAMS", 2 * len(params)]
            for k, v in params.items():
                args += [k, v]
        args += ["RETURN", len(return_fields)] + list(return_fields)
        if sortby:
            args += ["SORTBY", sortby[0], sortby[1]]
        args += ["LIMIT", offset, limit, "DIALECT", 2]
        res = self.client.execute_command(*args)
        return _parse_search_list(res, withscores)

    @staticmethod
    def _time_filter(since, until):
        if since is None and until is None:
            return ""
        lo = "-inf" if since is None else repr(float(since))
        hi = "+inf" if until is None else repr(float(until))
        return f"@timestamp_epoch:[{lo} {hi}]"

    def _row(self, key, fields):
        row = {k: _s(fields.get(k)) for k in ROW_FIELDS}
        row["id"] = _doc_bare_id(key)
        row["timestamp_epoch"] = _f(fields.get("timestamp_epoch"))
        row["importance"] = _f(fields.get("importance"), 1.0)
        row["access_count"] = _f(fields.get("access_count"), 0.0)
        return row

    def _vector_search(self, qvec, k, since=None, until=None):
        tf = self._time_filter(since, until)
        prefix = f"({tf})" if tf else "*"
        q = f"{prefix}=>[KNN {k} @embedding $vec AS __dist]"
        try:
            hits = self._ft_search(q, ROW_FIELDS + ["__dist"], k, params={"vec": vec_to_bytes(qvec)},
                                   sortby=("__dist", "ASC"))
        except CONN_ERRORS:
            raise
        except redis.ResponseError as e:
            sys.stderr.write(f"[n3mc-lite] vector search skipped: {e}\n")
            return []
        out = []
        for key, _score, fields in hits:
            if _s(fields.get("owner_id")) != self.cfg["owner_id"]:
                continue
            row = self._row(key, fields)
            row["cos"] = processor.cosine_from_distance(_s(fields.get("__dist"), "1"))
            out.append(row)
        return out

    def _bm25_search(self, query, k, since=None, until=None):
        terms = processor.bm25_terms(query)
        if not terms:
            return []
        q = "|".join(terms)
        text = f"(@content_fts:({q}) | @content_ngram:({q}))"
        tf = self._time_filter(since, until)
        if tf:
            text = f"({text}) {tf}"
        hits = None
        for scorer in ("BM25", "BM25STD", None):
            try:
                hits = self._ft_search(text, ROW_FIELDS, k, withscores=True, scorer=scorer)
                break
            except CONN_ERRORS:
                raise
            except redis.ResponseError as e:
                if scorer is not None and "scor" in str(e).lower():
                    continue
                sys.stderr.write(f"[n3mc-lite] keyword search skipped: {e}\n")
                return []
        out = []
        for key, score, fields in hits or []:
            if _s(fields.get("owner_id")) != self.cfg["owner_id"]:
                continue
            row = self._row(key, fields)
            row["bm25"] = _f(score)
            out.append(row)
        return out

    # ── search (spec §3.6, §3.11, §3.14) ─────────────────────────────────────

    def _resolve_parents(self, cands):
        pids = sorted({c["parent_id"] for c in cands if c.get("parent_id")})
        docs = {}
        if pids:
            pipe = self.client.pipeline(transaction=False)
            for pid in pids:
                pipe.hmget(DOC + pid, "content", "chunk_count", "chunk_ids", "turn_id", "timestamp",
                           "timestamp_epoch", "session_id", "owner_id")
            for pid, vals in zip(pids, pipe.execute()):
                if vals and vals[0] is not None:
                    docs[pid] = vals
        for c in cands:
            c["_hit_key"] = c["id"]
            pid = c.get("parent_id")
            if pid and pid in docs:
                v = docs[pid]
                c["id"] = pid
                c["content"] = _s(v[0])
                c["_doc"] = True
                c["chunk_count"] = int(_f(v[1], 0))
                c["chunk_ids"] = _s(v[2]).split()
                c["turn_id"] = _s(v[3])
                c["timestamp"] = _s(v[4]) or c.get("timestamp", "")
                c["timestamp_epoch"] = _f(v[5], c.get("timestamp_epoch", 0.0))
                c["session_id"] = _s(v[6]) or c.get("session_id", "")
            else:
                c["_doc"] = False
                c["chunk_count"] = 0
                c["chunk_ids"] = []

    def _representatives(self, rows):
        """{row id: (vector, text)} for the version grouping (spec §3.14 step 4)."""
        keys = []
        for r in rows:
            if r.get("_doc"):
                if r.get("chunk_ids"):
                    keys.append((r["id"], MEM + r["chunk_ids"][0]))
            else:
                keys.append((r["id"], MEM + r["id"]))
        out = {}
        if not keys:
            return out
        pipe = self.client.pipeline(transaction=False)
        for _rid, k in keys:
            pipe.hmget(k, "embedding", "content")
        for (rid, _k), vals in zip(keys, pipe.execute()):
            if vals and vals[0]:
                out[rid] = (bytes_to_vec(vals[0]), _s(vals[1]))
        return out

    def search(self, query, limit=None, session_id=None, since=None, until=None, no_model_wait=False):
        """(normal rows, weak rows, model message or None) — spec §3.14."""
        self._require()
        cfg = self.cfg
        query = processor.sanitize_surrogates(query if isinstance(query, str) else "")
        query = query[: int(cfg["search_query_max_chars"])]
        if limit is None:
            limit = int(cfg["search_result_limit"])
        limit = max(1, int(limit))
        session = (session_id or "").strip() or self.default_session
        k = min(limit * 5, 100)
        qvec = processor.embed(query, "query", no_model_wait=no_model_wait)
        message = None if qvec is not None else processor.unavailable_message()
        merged = {}
        if qvec is not None:
            for r in self._vector_search(qvec, k, since, until):
                merged[r["id"]] = r
        for r in self._bm25_search(query, k, since, until):
            if r["id"] in merged:
                merged[r["id"]]["bm25"] = r["bm25"]
            else:
                r["cos"] = None
                merged[r["id"]] = r
        cands = list(merged.values())
        self._resolve_parents(cands)
        reps_cb = self._representatives
        normal, weak = shape_results(cands, query, cfg, session, limit, reps_cb)
        top_k = int(cfg["ttl_refresh_top_k"])
        hits = [r["_hit_key"] for r in normal[:top_k]]
        if hits:
            pipe = self.client.pipeline(transaction=False)
            for h in hits:
                self._scripts["incr"](keys=[MEM + h], args=["access_count", 1], client=pipe)
            pipe.execute()
        return normal, weak, message

    # ── list / recall (spec §4.3, §4.3.1) ────────────────────────────────────

    def _scan(self, pattern):
        for key in self.client.scan_iter(match=pattern, count=1000):
            yield _s(key)

    def list_entries(self, limit):
        self._require()
        owner = self.cfg["owner_id"]
        singles = []
        offset = 0
        page = 200
        while len(singles) < limit:
            hits = self._ft_search("*", ["content", "timestamp", "owner_id", "parent_id"], page, offset=offset,
                                   sortby=("timestamp_epoch", "DESC"))
            for key, _sc, fields in hits:
                if _s(fields.get("owner_id")) != owner or _s(fields.get("parent_id")):
                    continue
                singles.append({"id": _doc_bare_id(key), "content": _s(fields.get("content")),
                                "timestamp": _s(fields.get("timestamp")), "_doc": False})
                if len(singles) >= limit:
                    break
            if len(hits) < page:
                break
            offset += page
        docs = []
        keys = [k for k in self._scan(DOC + "*") if not k.startswith(DOCSHA)]
        if keys:
            pipe = self.client.pipeline(transaction=False)
            for k in keys:
                pipe.hmget(k, "content", "timestamp", "owner_id", "chunk_count")
            for k, vals in zip(keys, pipe.execute()):
                if not vals or vals[0] is None or _s(vals[2]) != owner:
                    continue
                docs.append({"id": k[len(DOC):], "content": _s(vals[0]), "timestamp": _s(vals[1]),
                             "_doc": True, "chunk_count": int(_f(vals[3], 0))})
        rows = singles[:limit] + docs
        rows.sort(key=lambda r: r["timestamp"], reverse=True)
        return rows[:limit]

    def all_entries(self):
        """Every record of this owner for recall_thread: parent documents, single records, and chunks
        whose parent is gone (spec §4.3.1)."""
        self._require()
        owner = self.cfg["owner_id"]
        docs = {}
        keys = [k for k in self._scan(DOC + "*") if not k.startswith(DOCSHA)]
        if keys:
            pipe = self.client.pipeline(transaction=False)
            for k in keys:
                pipe.hmget(k, "content", "timestamp", "timestamp_epoch", "owner_id", "turn_id", "chunk_count")
            for k, vals in zip(keys, pipe.execute()):
                if vals and vals[0] is not None:
                    docs[k[len(DOC):]] = vals
        out = []
        for did, vals in docs.items():
            if _s(vals[3]) != owner:
                continue
            out.append({"id": did, "content": _s(vals[0]), "timestamp": _s(vals[1]),
                        "timestamp_epoch": _f(vals[2]), "turn_id": _s(vals[4]), "_doc": True,
                        "chunk_count": int(_f(vals[5], 0))})
        mkeys = [k for k in self._scan(MEM + "*") if not k.startswith(SHA)]
        if mkeys:
            pipe = self.client.pipeline(transaction=False)
            for k in mkeys:
                pipe.hmget(k, "content", "timestamp", "timestamp_epoch", "owner_id", "turn_id", "parent_id")
            for k, vals in zip(mkeys, pipe.execute()):
                if not vals or vals[0] is None or _s(vals[3]) != owner:
                    continue
                pid = _s(vals[5])
                if pid and pid in docs:
                    continue
                out.append({"id": k[len(MEM):], "content": _s(vals[0]), "timestamp": _s(vals[1]),
                            "timestamp_epoch": _f(vals[2]), "turn_id": _s(vals[4]), "_doc": False})
        out.sort(key=lambda r: (r["timestamp_epoch"], r["id"]))
        return out

    # ── delete (spec §3.5, §3.11, §4.3) ──────────────────────────────────────

    def _chunk_ids_for(self, pid, data):
        ids = _s(data.get(b"chunk_ids")).split()
        if ids:
            return ids
        found = []
        try:
            hits = self._ft_search(f"@parent_id:{{{pid}}}", ["parent_id"], 10000)
            found = [_doc_bare_id(k) for k, _s2, f in hits if _s(f.get("parent_id")) == pid]
        except CONN_ERRORS:
            raise
        except redis.ResponseError:
            found = []
        if not found:
            for k in self._scan(MEM + "*"):
                if k.startswith(SHA):
                    continue
                try:
                    if _s(self.client.hget(k, "parent_id")) == pid:
                        found.append(k[len(MEM):])
                except redis.ResponseError:
                    continue
        return found

    def _delete_doc(self, pid, data):
        content = _s(data.get(b"content"))
        guard = guard_key(content, _s(data.get(b"turn_id")), doc=True)
        chunks = self._chunk_ids_for(pid, data)
        res = self._scripts["delete"](keys=[guard, DOC + pid] + [MEM + c for c in chunks], args=[pid])
        return int(res[1]) if res else 0

    def _delete_single(self, rid, data):
        content = _s(data.get(b"content"))
        if _s(data.get(b"parent_id")):
            return int(self.client.delete(MEM + rid))
        guard = guard_key(content, _s(data.get(b"turn_id")), doc=False)
        res = self._scripts["delete"](keys=[guard, MEM + rid], args=[rid])
        return int(res[0]) if res else 0

    def delete_memory(self, mem_id) -> dict:
        self._require()
        raw = mem_id.strip() if isinstance(mem_id, str) else ""
        if raw.startswith(DOC) or raw.startswith(MEM):
            raw = raw[4:]
        if not raw or raw.startswith("sha:") or ":" in raw:
            return {"status": "not_found", "id": raw}
        owner = self.cfg["owner_id"]
        data = self.client.hgetall(DOC + raw)
        if data:
            if _s(data.get(b"owner_id")) != owner:
                return {"status": "not_found", "id": raw}
            n = self._delete_doc(raw, data)
            return {"status": "deleted", "id": raw, "chunks_deleted": n}
        try:
            data = self.client.hgetall(MEM + raw)
        except redis.ResponseError:
            data = {}
        if data:
            if _s(data.get(b"owner_id")) != owner:
                return {"status": "not_found", "id": raw}
            self._delete_single(raw, data)
            return {"status": "deleted", "id": raw}
        return {"status": "not_found", "id": raw}

    def delete_by_session(self, session_id) -> dict:
        self._require()
        owner = self.cfg["owner_id"]
        sid = session_id if isinstance(session_id, str) else ""
        docs_deleted = chunks_deleted = singles_deleted = 0
        for k in [k for k in self._scan(DOC + "*") if not k.startswith(DOCSHA)]:
            data = self.client.hgetall(k)
            if not data or _s(data.get(b"owner_id")) != owner or _s(data.get(b"session_id")) != sid:
                continue
            chunks_deleted += self._delete_doc(k[len(DOC):], data)
            docs_deleted += 1
        for k in [k for k in self._scan(MEM + "*") if not k.startswith(SHA)]:
            try:
                data = self.client.hgetall(k)
            except redis.ResponseError:
                continue
            if not data or _s(data.get(b"owner_id")) != owner or _s(data.get(b"session_id")) != sid:
                continue
            rid = k[len(MEM):]
            if _s(data.get(b"parent_id")):
                chunks_deleted += int(self.client.delete(k))
            else:
                singles_deleted += self._delete_single(rid, data)
        total = docs_deleted + chunks_deleted + singles_deleted
        if total == 0:
            return {"status": "not_found", "session_id": sid, "deleted": 0}
        return {"status": "deleted", "session_id": sid, "documents_deleted": docs_deleted,
                "chunks_deleted": chunks_deleted, "singles_deleted": singles_deleted, "deleted": total}

    # ── repair (spec §3.10) ──────────────────────────────────────────────────

    def repair(self, no_model_wait=False, max_reembed=200) -> dict:
        self._require()
        self._try_index()
        if not self.index_ok:
            return {"status": "error", "message": index_hint(self.url, self.index_error)}
        owner = self.cfg["owner_id"]
        model = processor.get_model(no_model_wait=no_model_wait)
        reembedded = remaining = 0
        keys = [k for k in self._scan(MEM + "*") if not k.startswith(SHA)]
        for i in range(0, len(keys), 200):
            batch = keys[i:i + 200]
            pipe = self.client.pipeline(transaction=False)
            for k in batch:
                pipe.hmget(k, "owner_id", "content")
                pipe.hexists(k, "embedding")
            res = pipe.execute()
            for j, k in enumerate(batch):
                vals, has = res[2 * j], res[2 * j + 1]
                if not vals or vals[0] is None or _s(vals[0]) != owner or has:
                    continue
                if model is None or reembedded >= max_reembed:
                    remaining += 1
                    continue
                vec = processor.embed(_s(vals[1]), "passage", no_model_wait=no_model_wait)
                if vec is None:
                    remaining += 1
                    continue
                if self._scripts["hset"](keys=[k], args=["embedding", vec_to_bytes(vec)]):
                    reembedded += 1
        out = {"status": "ok", "message": "index ensured", "reembedded": reembedded, "remaining": remaining}
        if model is None:
            out["warning"] = "embedding_unavailable"
            out["detail"] = processor.unavailable_message()
        return out


# ── result shaping (spec §3.14) — pure, testable without Redis ──────────────

def _score_row(c, cos, query, cfg, session, now):
    kw = c.get("_kw", 0.0)
    bl = processor.b_local(c.get("importance", 1.0), c.get("access_count", 0), cfg)
    decay = processor.time_decay(c.get("timestamp_epoch"), cfg["half_life_days"], now)
    bs = cfg["b_session_match"] if (session and c.get("session_id") == session) else cfg["b_session_mismatch"]
    f = processor.fused(cos, kw) * bl
    s = f * decay * bs
    bonus = 0.0
    if cfg.get("lexical_rerank_enabled", True):
        bonus = processor.rerank_bonus(query, c.get("content", ""), cfg)
    return s + bonus, f + bonus, s


def _newer_key(r):
    return (r.get("timestamp_epoch", 0.0), r.get("id", ""))


def shape_results(cands, query, cfg, session, limit, reps_cb=None, now=None):
    """(normal rows, weak rows) from merged candidates that already went through the parent resolution.

    Each candidate: id (resolved), content, timestamp_epoch, session_id, importance, access_count,
    cos (actual cosine or None when only BM25 found it), bm25 (or None), _doc, chunk_ids, _hit_key."""
    now = time.time() if now is None else now
    vmin = float(cfg["vector_min_cos"])
    max_bm25 = max([abs(c["bm25"]) for c in cands if c.get("bm25") is not None] or [0.0])
    for c in cands:
        c["_kw"] = processor.keyword_relevance(c.get("bm25"), max_bm25, float(cfg["bm25_min_threshold"]))
    main = []
    for c in cands:
        cos = c.get("cos")
        vec_ok = cos is not None and cos >= vmin
        if not (vec_ok or c.get("bm25") is not None):
            continue
        r = dict(c)
        r["_base"] = processor.fused(cos if vec_ok else 0.0, c["_kw"])
        main.append(r)
    # one hit per parent document: keep the best one (before the rerank, spec §3.11)
    best = {}
    for r in main:
        cos_eff = r["cos"] if (r.get("cos") is not None and r["cos"] >= vmin) else 0.0
        r["score"], r["filter_score"], r["_pre"] = _score_row(r, cos_eff, query, cfg, session, now)
        prev = best.get(r["id"])
        if prev is None or r["_pre"] > prev["_pre"]:
            best[r["id"]] = r
    rows = [r for r in best.values() if r["filter_score"] >= float(cfg["min_score"])]
    rows.sort(key=lambda r: (r["score"], _newer_key(r)), reverse=True)
    if cfg.get("version_order_enabled", True) and len(rows) > 1 and reps_cb is not None:
        _version_order(rows, cfg, reps_cb)
    ratio = float(cfg["relative_min_score_ratio"])
    if rows and ratio > 0:
        top = max(r["filter_score"] for r in rows)
        rows = [r for r in rows if r.get("_version_group") or r["filter_score"] >= top * ratio]
    normal = rows
    weak = []
    want = min(int(cfg["min_results"]), int(limit))
    if want > 0 and query.strip() and len(normal) < want:
        taken = {r["id"] for r in normal}
        pool = {}
        for c in cands:
            if c["id"] in taken:
                continue
            w = dict(c)
            w["score"], w["filter_score"], _pre = _score_row(w, c.get("cos") or 0.0, query, cfg, session, now)
            if w["filter_score"] < 1e-6:
                continue
            prev = pool.get(w["id"])
            if prev is None or w["filter_score"] > prev["filter_score"]:
                pool[w["id"]] = w
        ordered = sorted(pool.values(), key=lambda r: (r["filter_score"], _newer_key(r)), reverse=True)
        for w in ordered[: want - len(normal)]:
            w["_weak"] = True
            weak.append(w)
    return normal[:limit], weak


def _version_order(rows, cfg, reps_cb):
    reps = reps_cb(rows) or {}
    min_chars = int(cfg["version_min_chars"])
    thr = float(cfg["version_sim_threshold"])
    elig = [i for i, r in enumerate(rows)
            if r["id"] in reps and reps[r["id"]][0] and len(reps[r["id"]][1] or "") >= min_chars]
    parent = {i: i for i in elig}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a_i, a in enumerate(elig):
        for b in elig[a_i + 1:]:
            if processor.cosine_similarity(reps[rows[a]["id"]][0], reps[rows[b]["id"]][0]) >= thr:
                parent[find(a)] = find(b)
    groups = {}
    for i in elig:
        groups.setdefault(find(i), []).append(i)
    gno = 0
    snapshot = list(rows)
    for members in sorted(groups.values(), key=lambda m: min(m)):
        if len(members) < 2:
            continue
        gno += 1
        positions = sorted(members)
        scores = [(snapshot[p]["score"], snapshot[p]["filter_score"]) for p in positions]
        newest_first = sorted((snapshot[p] for p in positions), key=_newer_key, reverse=True)
        for p, r, (s, fs) in zip(positions, newest_first, scores):
            r["score"], r["filter_score"] = s, fs
            r["_version_group"] = gno
            rows[p] = r
