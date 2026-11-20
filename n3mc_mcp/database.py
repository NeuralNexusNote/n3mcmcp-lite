"""Redis layer: index, CRUD, TTL, dedup, search shaping (spec §3.3–§3.14, §3.16)."""

import datetime
import os
import sys
import threading
import time
import urllib.parse
import uuid

import redis
from redis.exceptions import ResponseError

from n3mc_mcp import processor as P

INDEX = "n3mc_idx"
MEM = "mem:"
MEMSHA = "mem:sha:"
DOC = "doc:"
DOCSHA = "docsha:"
BACKEND_ERRORS = (redis.ConnectionError, redis.TimeoutError)
HOOK_PREFIXES = ("[user", "[claude")

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
SET_GUARD_IF_EXISTS = """
if redis.call('EXISTS', KEYS[1]) == 1 then
  redis.call('SET', KEYS[2], ARGV[1], 'EX', ARGV[2])
  return 1
end
return 0
"""

_RETURN = ["id", "content", "timestamp", "timestamp_epoch", "owner_id", "agent_name", "session_id",
           "turn_id", "importance", "access_count", "parent_id"]
_DOC_FIELDS = ["id", "content", "timestamp", "timestamp_epoch", "owner_id", "agent_name", "session_id",
               "turn_id", "chunk_count", "chunk_ids"]

_FIELD_SCHEMA = [
    ("content", ["TEXT", "WEIGHT", "1.0"]),
    ("content_ngram", ["TEXT", "WEIGHT", "0.8"]),
    ("timestamp_epoch", ["NUMERIC", "SORTABLE"]),
    ("owner_id", ["TAG"]),
    ("local_id", ["TAG"]),
    ("agent_name", ["TAG"]),
    ("session_id", ["TAG"]),
    ("turn_id", ["TAG"]),
    ("importance", ["NUMERIC"]),
    ("access_count", ["NUMERIC"]),
    ("parent_id", ["TAG"]),
]


class BackendUnavailable(Exception):
    """Redis cannot be used now; str(e) is the recovery hint."""


def redis_location(url: str):
    u = urllib.parse.urlparse(url or "")
    host = u.hostname or "localhost"
    port = u.port or 6379
    db = 0
    path = (u.path or "").strip("/")
    if path.isdigit():
        db = int(path)
    return host, port, db


def redis_display(url: str) -> str:
    host, port, db = redis_location(url)
    return f"{host}:{port}/{db}"


def backend_hint(url: str) -> str:
    host, port, _ = redis_location(url)
    return (f"Redis Stack is not reachable at {host}:{port}. If Docker itself is not running, start Docker "
            f"(Docker Desktop) first. Then run: docker start redis-stack (first time: docker run -d --name "
            f"redis-stack -p 127.0.0.1:6379:6379 redis/redis-stack-server:latest). The memory server reconnects "
            f"automatically — no restart needed; just retry.")


def index_hint(url: str, reason: str) -> str:
    host, port, _ = redis_location(url)
    return (f"The Redis at {host}:{port} does not provide RediSearch (FT.CREATE failed: {reason}). N3MC Lite "
            f"needs Redis Stack, not plain Redis: docker run -d --name redis-stack -p 127.0.0.1:6379:6379 "
            f"redis/redis-stack-server:latest")


def _s(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return str(v)


def _f(v, default=0.0) -> float:
    try:
        return float(_s(v))
    except (TypeError, ValueError):
        return default


def new_id() -> str:
    try:
        import uuid_utils
        return str(uuid_utils.uuid7())
    except Exception:  # noqa: BLE001
        return str(uuid.uuid4())


def guard_key(content: str, turn_id, is_doc: bool) -> str:
    """The exact-duplicate guard of a stored record (§3.8, §3.16 L6)."""
    prefix = DOCSHA if is_doc else MEMSHA
    sha = P.sha1_hex(content)
    if turn_id and content.startswith(HOOK_PREFIXES):
        return f"{prefix}{turn_id}:{sha}"
    return f"{prefix}{sha}"


def fmt_saved(epoch) -> str:
    try:
        dt = datetime.datetime.fromtimestamp(float(epoch)).astimezone()
    except (TypeError, ValueError, OverflowError, OSError):
        return "unknown"
    return dt.strftime("%Y-%m-%d %H:%M") + f" (UTC{dt.strftime('%z')})"


def _require_str(v, name):
    if v is None:
        return ""
    if not isinstance(v, str):
        raise TypeError(f"{name} must be a string (got {type(v).__name__})")
    return v


def _parse_importance(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
    elif isinstance(v, str):
        try:
            f = float(v.strip())
        except ValueError:
            return None
    else:
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


_REPAIR_LOCK = threading.Lock()  # one /repair at a time per server process (L5)


class Database:
    def __init__(self, cfg: dict, default_session: str = None):
        self.cfg = cfg
        self.url = cfg["redis_url"]
        self.owner = cfg["owner_id"]
        self.local = cfg["local_id"]
        self.default_session = default_session or os.environ.get("N3MC_SESSION_ID") or str(uuid.uuid4())
        self.client = None
        self.index_ok = False
        self.index_error = None
        self._lock = threading.Lock()
        self._state = None
        self._scripts = {}
        self._notice_lock = threading.Lock()
        cfg.setdefault("_notices", [])

    # ------------------------------------------------------------ notices
    def add_notice(self, message: str) -> None:
        key = message.split(":", 1)[0]
        with self._notice_lock:
            notices = self.cfg.setdefault("_notices", [])
            if not any(n.split(":", 1)[0] == key for n in notices):
                notices.append(message)
                print("N3MC Lite: " + message, file=sys.stderr, flush=True)

    # ------------------------------------------------------------ connection
    def _log_state(self, state, detail=""):
        if state != self._state:
            self._state = state
            msg = {"down": f"Redis is not reachable ({detail})", "ok": "connected to Redis"}.get(state, state)
            print(f"N3MC Lite: {msg} at {redis_display(self.url)}", file=sys.stderr, flush=True)

    def _drop(self):
        c, self.client = self.client, None
        self.index_ok = False
        if c is not None:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass

    def mark_down(self):
        with self._lock:
            self._drop()
            self._log_state("down", "connection lost")

    def _make_client(self):
        return redis.Redis.from_url(self.url, decode_responses=False, socket_connect_timeout=2,
                                    socket_timeout=5, protocol=2)

    def ensure_connected(self, ephemeral: bool = True) -> str:
        """'ok', 'down' or 'noindex'. PING / reconnect / re-run enforce_ephemeral + ensure_index (§3.9)."""
        with self._lock:
            if self.client is not None:
                try:
                    self.client.ping()
                except BACKEND_ERRORS:
                    self._drop()
            if self.client is None:
                c = None
                try:
                    c = self._make_client()
                    c.ping()
                except BACKEND_ERRORS as e:
                    if c is not None:
                        try:
                            c.close()
                        except Exception:  # noqa: BLE001
                            pass
                    self._log_state("down", type(e).__name__)
                    return "down"
                self.client = c
                self._scripts = {
                    "incr": c.register_script(INCR_IF_EXISTS),
                    "hset": c.register_script(HSET_IF_EXISTS),
                    "guard": c.register_script(SET_GUARD_IF_EXISTS),
                }
                self.index_ok = False
                try:
                    if ephemeral:
                        self.enforce_ephemeral()
                except BACKEND_ERRORS:
                    self._drop()
                    self._log_state("down", "connection lost")
                    return "down"
                self._log_state("ok")
            if not self.index_ok:
                try:
                    self.ensure_index()
                except BACKEND_ERRORS:
                    self._drop()
                    self._log_state("down", "connection lost")
                    return "down"
            return "ok" if self.index_ok else "noindex"

    def unavailable_hint(self, status: str) -> str:
        if status == "noindex":
            return index_hint(self.url, self.index_error or "unknown error")
        return backend_hint(self.url)

    def enforce_ephemeral(self) -> None:
        c = self.client
        host, port, db = redis_location(self.url)
        shared = False
        try:
            info = c.info("keyspace")
            for k, v in info.items():
                if str(k).startswith("db") and str(k)[2:].isdigit() and int(str(k)[2:]) != db:
                    if isinstance(v, dict) and int(v.get("keys", 0)) > 0:
                        shared = True
        except ResponseError:
            shared = True
        if not shared:
            cursor = 0
            while True:
                cursor, keys = c.scan(cursor=cursor, count=1000)
                if any(not k.startswith((b"mem:", b"doc:", b"docsha:")) for k in keys):
                    shared = True
                    break
                if int(cursor) == 0:
                    break
        if shared:
            self.add_notice(
                f"shared_redis: the Redis at {host}:{port} also holds keys that are not N3MC's, so its "
                f"persistence settings were left unchanged. N3MC memories in it may survive a Redis restart. "
                f"Use a dedicated container (docker run -d --name redis-stack -p 127.0.0.1:6379:6379 "
                f"redis/redis-stack-server:latest) for Lite.")
            return
        try:
            c.config_set("appendonly", "no")
            c.config_set("save", "")
        except ResponseError as e:
            print(f"N3MC Lite: CONFIG SET was refused: {e}", file=sys.stderr, flush=True)
            self.add_notice(
                f"config_refused: the Redis at {host}:{port} refused CONFIG SET ({e}), so its persistence could "
                f"not be turned off and N3MC memories in it may survive a Redis restart. Use a dedicated "
                f"container (docker run -d --name redis-stack -p 127.0.0.1:6379:6379 "
                f"redis/redis-stack-server:latest) for Lite.")

    def ensure_index(self) -> None:
        c = self.client
        dim = int(self.cfg["embedding_dim"])
        args = ["FT.CREATE", INDEX, "ON", "HASH", "PREFIX", "1", MEM, "SCHEMA"]
        for name, spec in _FIELD_SCHEMA:
            args += [name] + spec
        args += ["embedding", "VECTOR", "FLAT", "6", "TYPE", "FLOAT32", "DIM", str(dim),
                 "DISTANCE_METRIC", "COSINE"]
        try:
            c.execute_command(*args)
            self.index_ok, self.index_error = True, None
        except ResponseError as e:
            msg = str(e)
            if "already exists" in msg.lower():
                self.index_ok, self.index_error = True, None
                self._check_existing_index(dim)
            else:
                self.index_ok, self.index_error = False, msg
                print(f"N3MC Lite: FT.CREATE failed: {msg}", file=sys.stderr, flush=True)

    def _check_existing_index(self, dim: int) -> None:
        try:
            info = self.client.execute_command("FT.INFO", INDEX)
        except ResponseError:
            return
        attrs = []
        if isinstance(info, (list, tuple)):
            for i in range(0, len(info) - 1, 2):
                if _s(info[i]) == "attributes":
                    attrs = info[i + 1]
        names = {}
        for a in attrs or []:
            if not isinstance(a, (list, tuple)):
                continue
            d = {}
            for j in range(0, len(a) - 1, 2):
                d[_s(a[j]).lower()] = a[j + 1]
            names[_s(d.get("identifier"))] = d
        for name, spec in _FIELD_SCHEMA:
            if names and name not in names:
                try:
                    self.client.execute_command("FT.ALTER", INDEX, "SCHEMA", "ADD", name, *spec)
                except ResponseError:
                    pass
        emb = names.get("embedding")
        if emb and "dim" in emb:
            old = int(_f(emb["dim"]))
            if old and old != dim:
                self.add_notice(
                    f"index_dim_mismatch: the existing index {INDEX} was built for {old} dimensions but "
                    f"embedding_dim is {dim}; new vectors are not indexed. Drop the index "
                    f"(FT.DROPINDEX {INDEX}) or flush the test DB, then restart.")

    # ------------------------------------------------------------ FT.SEARCH helpers
    def _ft(self, query, return_fields, limit, offset=0, params=None, withscores=False, scorer=None,
            sortby=None):
        args = ["FT.SEARCH", INDEX, query]
        if withscores:
            args.append("WITHSCORES")
        if scorer:
            args += ["SCORER", scorer]
        args += ["RETURN", str(len(return_fields))] + list(return_fields)
        if sortby:
            args += ["SORTBY"] + list(sortby)
        args += ["LIMIT", str(offset), str(limit)]
        if params:
            args += ["PARAMS", str(len(params) * 2)]
            for k, v in params.items():
                args += [k, v]
        args += ["DIALECT", "2"]
        res = self.client.execute_command(*args)
        out = []
        if not isinstance(res, (list, tuple)) or not res:
            return out
        i = 1
        step = 3 if withscores else 2
        while i < len(res):
            key = _s(res[i])
            score = _f(res[i + 1]) if withscores else None
            fields = res[i + (2 if withscores else 1)] if i + (2 if withscores else 1) < len(res) else []
            d = {}
            if isinstance(fields, (list, tuple)):
                for j in range(0, len(fields) - 1, 2):
                    d[_s(fields[j])] = fields[j + 1]
            row = {k: (_s(v) if k != "embedding" else v) for k, v in d.items()}
            row["id"] = key[len(MEM):] if key.startswith(MEM) else key
            if withscores:
                row["_bm25"] = score
            out.append(row)
            i += step
        return out

    @staticmethod
    def _time_filter(since, until):
        if since is None and until is None:
            return None
        a = "-inf" if since is None else repr(float(since))
        b = "+inf" if until is None else repr(float(until))
        return f"@timestamp_epoch:[{a} {b}]"

    def _knn(self, qvec, k, tf):
        base = f"({tf})" if tf else "*"
        q = f"{base}=>[KNN {k} @embedding $vec AS __dist]"
        try:
            rows = self._ft(q, _RETURN + ["__dist"], k, params={"vec": P.to_bytes(qvec)},
                            sortby=("__dist", "ASC"))
        except ResponseError as e:   # §3.9: skip only this path; connection errors still propagate
            print(f"N3MC Lite: vector search skipped: {e}", file=sys.stderr, flush=True)
            return []
        return [r for r in rows if r.get("owner_id") == self.owner]

    def _bm25(self, terms, k, tf):
        q = "|".join(terms)
        fts = f"(@content:({q}) | @content_ngram:({q}))"
        if tf:
            fts = f"({fts}) {tf}"
        last_err = None
        for scorer in ("BM25", "BM25STD", None):
            try:
                rows = self._ft(fts, _RETURN, k, withscores=True, scorer=scorer)
                return [r for r in rows if r.get("owner_id") == self.owner]
            except ResponseError as e:
                last_err = e
                if "scorer" in str(e).lower():
                    continue
                break
        print(f"N3MC Lite: BM25 search skipped: {last_err}", file=sys.stderr, flush=True)
        return []

    # ------------------------------------------------------------ save
    def save_memory(self, content, agent_name=None, owner_id=None, importance=None, session_id=None,
                    turn_id=None, role=None, embed=True) -> dict:
        cfg = self.cfg
        c = self.client
        content = P.sanitize_surrogates(content)
        agent_name = P.sanitize_surrogates(agent_name)
        session_id = P.sanitize_surrogates(session_id)
        turn_id = P.sanitize_surrogates(turn_id)

        if importance is None:
            imp = 1.0
        else:
            imp = _parse_importance(importance)
            if imp is None:
                return {"saved": False, "status": "error",
                        "reason": f"importance must be numeric (got {importance})"}
            imp = max(0.5, min(2.0, imp))
        if owner_id not in (None, "") and owner_id != self.owner:
            return {"status": "error", "saved": False, "reason": "owner_id mismatch"}
        if content is None:
            content = ""
        if not isinstance(content, str):
            content = str(content)
        content = content.strip()
        if not content:
            return {"status": "error", "saved": False, "reason": "empty content"}
        if not P.has_visible_text(content):
            return {"status": "error", "saved": False, "reason": "empty content (no visible characters)"}
        if cfg.get("skip_code_blocks") and "```" in content:
            return {"status": "skipped_code", "saved": False}
        body = P.purify(content)
        if session_id is None or (isinstance(session_id, str) and not session_id.strip()):
            session_id = self.default_session
        ttl = int(cfg["ttl_seconds"])
        ct = int(cfg["chunk_threshold"])
        ov = int(cfg["chunk_overlap"])
        model_msg = None

        if len(body) <= ct:
            stored = f"[{role}] {body}" if role else body
            gk = guard_key(stored, turn_id if role else None, False) if role else f"{MEMSHA}{P.sha1_hex(stored)}"
            if c.exists(gk):
                return {"status": "duplicate", "saved": False}
            vecs = None
            if embed:
                try:
                    vecs, model_msg = P.embed_passages([stored], cfg)
                except Exception as e:  # noqa: BLE001
                    return {"status": "error", "saved": False, "reason": f"{type(e).__name__}: {e}"}
            rid = new_id()
            if not c.set(gk, rid, nx=True, ex=ttl):
                return {"status": "duplicate", "saved": False}
            try:
                fields = self._fields(rid, stored, agent_name, session_id, turn_id)
                fields["importance"] = repr(imp)
                fields["access_count"] = "0"
                fields["parent_id"] = ""
                fields["content_ngram"] = P.build_ngram(stored)
                if vecs:
                    fields["embedding"] = P.to_bytes(vecs[0])
                pipe = c.pipeline(transaction=True)
                pipe.hset(MEM + rid, mapping=fields)
                pipe.expire(MEM + rid, ttl)
                pipe.execute()
            except BACKEND_ERRORS:
                self._release(gk)
                raise
            except Exception as e:  # noqa: BLE001
                self._release(gk)
                return {"status": "error", "saved": False, "reason": f"{type(e).__name__}: {e}"}
            res = {"status": "saved", "saved": True, "id": rid, "ttl_seconds": ttl}
        else:
            chunks = P.chunk_text(body, ct, ov)
            n = len(chunks)
            if role:
                parent_content = f"[{role}] {body}"
                chunk_contents = ([f"[{role} {i + 1}/{n}] {ch}" for i, ch in enumerate(chunks)] if n > 1
                                  else [f"[{role}] {chunks[0]}"])
                gk = guard_key(parent_content, turn_id, True)
            else:
                parent_content = body
                chunk_contents = chunks
                gk = f"{DOCSHA}{P.sha1_hex(parent_content)}"
            existing = c.get(gk)
            if existing:
                return {"status": "duplicate", "saved": False, "parent_id": _s(existing)}
            vecs = None
            if embed:
                try:
                    vecs, model_msg = P.embed_passages(chunk_contents, cfg)
                except Exception as e:  # noqa: BLE001
                    return {"status": "error", "saved": False, "reason": f"{type(e).__name__}: {e}"}
            pid = new_id()
            if not c.set(gk, pid, nx=True, ex=ttl):
                return {"status": "duplicate", "saved": False, "parent_id": _s(c.get(gk))}
            try:
                cids = [new_id() for _ in chunk_contents]
                doc = self._fields(pid, parent_content, agent_name, session_id, turn_id)
                doc["chunk_count"] = str(n)
                doc["chunk_ids"] = " ".join(cids)
                pipe = c.pipeline(transaction=True)
                pipe.hset(DOC + pid, mapping=doc)
                pipe.expire(DOC + pid, ttl)
                for i, (cid, cc) in enumerate(zip(cids, chunk_contents)):
                    f = self._fields(cid, cc, agent_name, session_id, turn_id)
                    f["importance"] = repr(imp)
                    f["access_count"] = "0"
                    f["parent_id"] = pid
                    f["content_ngram"] = P.build_ngram(cc)
                    if vecs:
                        f["embedding"] = P.to_bytes(vecs[i])
                    pipe.hset(MEM + cid, mapping=f)
                    pipe.expire(MEM + cid, ttl)
                pipe.execute()
            except BACKEND_ERRORS:
                self._release(gk)
                raise
            except Exception as e:  # noqa: BLE001
                self._release(gk)
                return {"status": "error", "saved": False, "reason": f"{type(e).__name__}: {e}"}
            res = {"status": "saved", "saved": True, "parent_id": pid, "chunks": n, "saved_count": n,
                   "ids": cids, "ttl_seconds": ttl}

        warnings, details = [], []
        if embed and not vecs:
            warnings.append("embedding_unavailable")
            details.append(model_msg or P.model_status(cfg["embedding_model"]) or "")
        if cfg.get("_unreadable"):
            warnings.append("config_unreadable")
            details.append(
                f"config.json could not be read, so this entry was saved under a temporary owner_id and will "
                f"not be visible after the server restarts. Fix {cfg.get('_path')} and restart the MCP client.")
        if warnings:
            res["warning"] = "; ".join(warnings)
            res["detail"] = " ".join(d for d in details if d)
        return res

    def _fields(self, rid, content, agent_name, session_id, turn_id) -> dict:
        now = time.time()
        return {
            "id": rid,
            "content": content,
            "timestamp": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat(),
            "timestamp_epoch": repr(now),
            "owner_id": self.owner,
            "local_id": self.local,
            "agent_name": _require_str(agent_name, "agent_name"),
            "session_id": _require_str(session_id, "session_id"),
            "turn_id": _require_str(turn_id, "turn_id"),
        }

    def _release(self, gk):
        try:
            self.client.delete(gk)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ search (§3.14)
    def search(self, query, limit=None, session_id=None, since=None, until=None):
        """Returns (normal_rows, weak_rows, model_message_or_None)."""
        cfg = self.cfg
        query = P.sanitize_surrogates(query if query is not None else "")
        if not isinstance(query, str):
            query = str(query)
        q = query[: int(cfg["search_query_max_chars"])]
        try:
            limit = int(limit) if limit not in (None, "") else int(cfg["search_result_limit"])
        except (TypeError, ValueError):
            limit = int(cfg["search_result_limit"])
        limit = max(1, limit)
        if isinstance(session_id, str) and session_id.strip():
            eff = session_id
        else:
            eff = self.default_session
        k = min(limit * 5, 100)
        tf = self._time_filter(since, until)

        qvec, model_msg = P.embed_query(q, cfg)
        cands = {}
        if qvec is not None:
            for r in self._knn(qvec, k, tf):
                r["_real_cos"] = P.cos_from_distance(r.get("__dist"))
                cands[r["id"]] = r
        terms = P.drop_lone_cjk_tokens(P.prepare_query(q)).split()
        if terms:
            for r in self._bm25(terms, k, tf):
                if r["id"] in cands:
                    cands[r["id"]]["_bm25"] = r["_bm25"]
                else:
                    r["_real_cos"] = None
                    cands[r["id"]] = r
        thr = float(cfg["bm25_min_threshold"])
        max_bm = max((abs(r.get("_bm25") or 0.0) for r in cands.values()), default=0.0)
        for r in cands.values():
            r["kw"] = P.keyword_relevance(r.get("_bm25") or 0.0, max_bm, thr) if r.get("_bm25") is not None else 0.0

        vmin = float(cfg["vector_min_cos"])
        main = []
        for r in cands.values():
            rc = r["_real_cos"]
            if rc is not None and rc >= vmin:
                main.append(dict(r, cos=rc))
            elif r.get("_bm25") is not None:
                main.append(dict(r, cos=0.0))
        for r in main:
            self._score(r, eff)
        main = self._resolve_parents(sorted(main, key=lambda r: r["score"], reverse=True))
        self._rerank(main, q)
        min_score = float(cfg["min_score"])
        kept = [r for r in main if r["filter_score"] >= min_score]
        kept.sort(key=lambda r: (r["score"], _f(r.get("timestamp_epoch")), r["id"]), reverse=True)
        if cfg.get("version_order_enabled"):
            self._version_order(kept)
        ratio = float(cfg["relative_min_score_ratio"])
        if ratio > 0 and kept:
            top = max(r["filter_score"] for r in kept)
            kept = [r for r in kept if r.get("_version_group") or r["filter_score"] >= top * ratio]
        normal = kept[:limit]

        weak = []
        need = min(int(cfg["min_results"]), limit) - len(normal)
        if need > 0 and int(cfg["min_results"]) > 0 and query.strip():
            weak = self._weak_fill(cands, normal, need, q, eff)

        if cfg.get("ttl_refresh_on_search"):
            top_k = int(cfg["ttl_refresh_top_k"])
            if top_k > 0 and normal:
                self._refresh(normal[:top_k])
        return normal, weak, model_msg

    def _score(self, r, eff):
        base = P.base_score(r["cos"], r.get("kw", 0.0))
        bl = P.b_local(r.get("importance", 1.0), r.get("access_count", 0), self.cfg)
        r["filter_score"] = base * bl
        r["score"] = (base * P.time_decay(r.get("timestamp_epoch"), self.cfg["half_life_days"]) * bl
                      * P.b_session(r.get("session_id"), eff, self.cfg))

    def _resolve_parents(self, rows):
        pids = list({r["parent_id"] for r in rows if r.get("parent_id")})
        docs = {}
        if pids:
            pipe = self.client.pipeline(transaction=False)
            for pid in pids:
                pipe.hmget(DOC + pid, _DOC_FIELDS)
            for pid, vals in zip(pids, pipe.execute()):
                if vals and vals[1] is not None:
                    docs[pid] = dict(zip(_DOC_FIELDS, vals))
        out, seen = [], set()
        for r in rows:
            pid = r.get("parent_id")
            if pid and pid in docs:
                if pid in seen:
                    continue
                seen.add(pid)
                d = docs[pid]
                if _s(d.get("owner_id")) != self.owner:
                    continue
                r = dict(r)
                r["_chunk_id"] = r["id"]
                r["id"] = pid
                r["_doc"] = True
                r["content"] = _s(d["content"])
                r["chunk_count"] = int(_f(d.get("chunk_count")))
                r["chunk_ids"] = _s(d.get("chunk_ids")).split()
                r["turn_id"] = _s(d.get("turn_id"))
                r["timestamp"] = _s(d.get("timestamp")) or r.get("timestamp")
                r["timestamp_epoch"] = _s(d.get("timestamp_epoch")) or r.get("timestamp_epoch")
            out.append(r)
        return out

    def _rerank(self, rows, q):
        if not self.cfg.get("lexical_rerank_enabled"):
            return
        for r in rows:
            b = P.rerank_bonus(q, r.get("content", ""), self.cfg)
            r["score"] += b
            r["filter_score"] += b

    def _version_order(self, kept):
        reps = []
        for i, r in enumerate(kept):
            if r.get("_doc"):
                ids = r.get("chunk_ids") or []
                if not ids:
                    continue
                reps.append((i, MEM + ids[0]))
            else:
                reps.append((i, MEM + r["id"]))
        if len(reps) < 2:
            return
        pipe = self.client.pipeline(transaction=False)
        for _, key in reps:
            pipe.hmget(key, ["embedding", "content"])
        vecs = {}
        for (i, _), vals in zip(reps, pipe.execute()):
            emb, content = (vals or [None, None])
            if emb and content is not None and len(_s(content)) >= int(self.cfg["version_min_chars"]):
                vecs[i] = P.from_bytes(emb)
        idx = sorted(vecs)
        parent = {i: i for i in idx}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        thr = float(self.cfg["version_sim_threshold"])
        for a in range(len(idx)):
            for b in range(a + 1, len(idx)):
                if P.cosine_sim(vecs[idx[a]], vecs[idx[b]]) >= thr:
                    parent[find(idx[a])] = find(idx[b])
        groups = {}
        for i in idx:
            groups.setdefault(find(i), []).append(i)
        gno = 0
        for members in sorted(groups.values(), key=min):
            if len(members) < 2:
                continue
            gno += 1
            positions = sorted(members)
            scores = [(kept[p]["score"], kept[p]["filter_score"]) for p in positions]
            rows = sorted((kept[p] for p in positions),
                          key=lambda r: (_f(r.get("timestamp_epoch")), r["id"]), reverse=True)
            for p, r, (s, fs) in zip(positions, rows, scores):
                r = dict(r, score=s, filter_score=fs, _version_group=gno)
                kept[p] = r

    def _weak_fill(self, cands, normal, need, q, eff):
        taken = {r["id"] for r in normal}
        pool = []
        for r in cands.values():
            w = dict(r)
            w["cos"] = r["_real_cos"] if r["_real_cos"] is not None else 0.0
            self._score(w, eff)
            pool.append(w)
        pool.sort(key=lambda r: (r["filter_score"], _f(r.get("timestamp_epoch")), r["id"]), reverse=True)
        pool = self._resolve_parents(pool)
        self._rerank(pool, q)
        pool.sort(key=lambda r: (r["filter_score"], _f(r.get("timestamp_epoch")), r["id"]), reverse=True)
        weak = []
        for r in pool:
            if r["id"] in taken or r["filter_score"] < 1e-6:
                continue
            taken.add(r["id"])
            r["_weak"] = True
            weak.append(r)
            if len(weak) >= need:
                break
        return weak

    def _refresh(self, rows):
        ttl = int(self.cfg["ttl_seconds"])
        plans = []
        pipe = self.client.pipeline(transaction=False)
        for r in rows:
            if r.get("_doc"):
                hit = r["_chunk_id"]
                keys = [DOC + r["id"]] + [MEM + c for c in r.get("chunk_ids") or []]
                if MEM + hit not in keys:
                    keys.append(MEM + hit)
                record = DOC + r["id"]
                gk = guard_key(r.get("content", ""), r.get("turn_id"), True)
            else:
                hit = r["id"]
                keys = [MEM + hit]
                record = MEM + hit
                gk = None if r.get("parent_id") else guard_key(r.get("content", ""), r.get("turn_id"), False)
            for k in keys:
                pipe.expire(k, ttl)
            plans.append((keys, MEM + hit, record, gk, r["id"]))
        results = pipe.execute()
        pos = 0
        pipe2 = self.client.pipeline(transaction=False)
        n = 0
        for keys, hitkey, record, gk, rid in plans:
            got = dict(zip(keys, results[pos:pos + len(keys)]))
            pos += len(keys)
            if got.get(hitkey):
                self._scripts["incr"](keys=[hitkey], args=["access_count", 1], client=pipe2)
                n += 1
            if gk and got.get(record):
                self._scripts["guard"](keys=[record, gk], args=[rid, ttl], client=pipe2)
                n += 1
        if n:
            pipe2.execute()

    # ------------------------------------------------------------ scans
    def _scan_hashes(self, match, skip_prefix, fields):
        out = []
        batch = []
        for key in self.client.scan_iter(match=match, count=1000):
            if key.startswith(skip_prefix):
                continue
            batch.append(key)
            if len(batch) >= 500:
                out.extend(self._hmget_batch(batch, fields))
                batch = []
        if batch:
            out.extend(self._hmget_batch(batch, fields))
        return out

    def _hmget_batch(self, keys, fields):
        pipe = self.client.pipeline(transaction=False)
        for k in keys:
            pipe.hmget(k, fields)
        res = []
        for k, vals in zip(keys, pipe.execute(raise_on_error=False)):
            if isinstance(vals, Exception) or not vals:
                continue
            d = {f: v for f, v in zip(fields, vals)}
            d["_key"] = _s(k)
            res.append(d)
        return res

    def _all_records(self):
        mems = self._scan_hashes("mem:*", b"mem:sha:", _RETURN)
        docs = self._scan_hashes("doc:*", b"docsha:", _DOC_FIELDS)
        mems = [{k: (_s(v) if k != "_key" else v) for k, v in m.items()} for m in mems]
        docs = [{k: (_s(v) if k != "_key" else v) for k, v in d.items()} for d in docs]
        mems = [m for m in mems if m.get("owner_id") == self.owner and m.get("content") is not None]
        docs = [d for d in docs if d.get("owner_id") == self.owner and d.get("content") is not None]
        for m in mems:
            m["id"] = m["_key"][len(MEM):]
        for d in docs:
            d["id"] = d["_key"][len(DOC):]
        return mems, docs

    # ------------------------------------------------------------ list / recall
    def list_memories(self, limit):
        singles = []
        offset = 0
        while len(singles) < limit:
            rows = self._ft("*", ["id", "content", "timestamp", "owner_id", "parent_id"], 200, offset=offset,
                            sortby=("timestamp_epoch", "DESC"))
            for r in rows:
                if r.get("owner_id") == self.owner and not r.get("parent_id"):
                    singles.append(r)
            offset += 200
            if len(rows) < 200:
                break
        singles = singles[:limit]
        docs = self._scan_hashes("doc:*", b"docsha:", _DOC_FIELDS)
        parents = []
        for d in docs:
            d = {k: (_s(v) if k != "_key" else v) for k, v in d.items()}
            if d.get("owner_id") == self.owner and d.get("content") is not None:
                d["id"] = d["_key"][len(DOC):]
                d["_doc"] = True
                parents.append(d)
        items = singles + parents
        items.sort(key=lambda r: r.get("timestamp") or "", reverse=True)
        return items[:limit]

    def recall_rows(self):
        mems, docs = self._all_records()
        doc_ids = {d["id"] for d in docs}
        rows = []
        for d in docs:
            d["_doc"] = True
            rows.append(d)
        for m in mems:
            if m.get("parent_id") and m["parent_id"] in doc_ids:
                continue
            rows.append(m)
        rows.sort(key=lambda r: (_f(r.get("timestamp_epoch")), r["id"]))
        return rows

    # ------------------------------------------------------------ delete
    def delete_memory(self, rid):
        c = self.client
        rid = P.sanitize_surrogates(rid if isinstance(rid, str) else _s(rid)).strip()
        for pre in (DOC, MEM):
            if rid.startswith(pre):
                rid = rid[len(pre):]
                break
        if not rid:
            return {"status": "not_found", "id": rid}
        doc = c.hmget(DOC + rid, ["owner_id", "content", "turn_id", "chunk_ids"])
        if doc and doc[1] is not None:
            if _s(doc[0]) != self.owner:
                return {"status": "not_found", "id": rid}
            chunk_ids = _s(doc[3]).split()
            if not chunk_ids:
                chunk_ids = self._chunks_of(rid)
            gk = guard_key(_s(doc[1]), _s(doc[2]), True)
            pipe = c.pipeline(transaction=True)
            pipe.delete(DOC + rid)
            for cid in chunk_ids:
                pipe.delete(MEM + cid)
            if _s(c.get(gk)) in ("", rid):
                pipe.delete(gk)
            res = pipe.execute()
            return {"status": "deleted", "id": rid, "chunks_deleted": int(sum(res[1:1 + len(chunk_ids)]))}
        mem = c.hmget(MEM + rid, ["owner_id", "content", "turn_id", "parent_id"])
        if mem and mem[1] is not None:
            if _s(mem[0]) != self.owner:
                return {"status": "not_found", "id": rid}
            pipe = c.pipeline(transaction=True)
            pipe.delete(MEM + rid)
            if not _s(mem[3]):
                gk = guard_key(_s(mem[1]), _s(mem[2]), False)
                if _s(c.get(gk)) in ("", rid):
                    pipe.delete(gk)
            pipe.execute()
            return {"status": "deleted", "id": rid}
        return {"status": "not_found", "id": rid}

    def _chunks_of(self, pid):
        try:
            rows = self._ft(f"@parent_id:{{{pid}}}", ["parent_id"], 10000)
            ids = [r["id"] for r in rows if r.get("parent_id") == pid]
            if ids:
                return ids
        except ResponseError:
            pass
        rows = self._scan_hashes("mem:*", b"mem:sha:", ["parent_id"])
        return [r["_key"][len(MEM):] for r in rows if _s(r.get("parent_id")) == pid]

    def delete_by_session(self, session_id):
        c = self.client
        session_id = P.sanitize_surrogates(session_id)
        if not isinstance(session_id, str) or not session_id.strip():
            return {"status": "error", "reason": "session_id required"}
        mems, docs = self._all_records()
        docs = [d for d in docs if d.get("session_id") == session_id]
        doc_ids = {d["id"] for d in docs}
        all_doc_ids = set()
        for k in c.scan_iter(match="doc:*", count=1000):
            if not k.startswith(b"docsha:"):
                all_doc_ids.add(_s(k)[len(DOC):])
        d_del = c_del = s_del = 0
        for d in docs:
            chunk_ids = set(d.get("chunk_ids", "").split())
            chunk_ids |= {m["id"] for m in mems if m.get("parent_id") == d["id"]}
            gk = guard_key(d.get("content", ""), d.get("turn_id"), True)
            pipe = c.pipeline(transaction=True)
            pipe.delete(DOC + d["id"])
            for cid in chunk_ids:
                pipe.delete(MEM + cid)
            if _s(c.get(gk)) in ("", d["id"]):
                pipe.delete(gk)
            res = pipe.execute()
            d_del += int(res[0])
            c_del += int(sum(res[1:1 + len(chunk_ids)]))
        for m in mems:
            if m.get("session_id") != session_id:
                continue
            pid = m.get("parent_id")
            if pid:
                if pid in doc_ids or pid in all_doc_ids:
                    continue
                c_del += int(c.delete(MEM + m["id"]))
            else:
                gk = guard_key(m.get("content", ""), m.get("turn_id"), False)
                pipe = c.pipeline(transaction=True)
                pipe.delete(MEM + m["id"])
                if _s(c.get(gk)) in ("", m["id"]):
                    pipe.delete(gk)
                s_del += int(pipe.execute()[0])
        total = d_del + c_del + s_del
        if total == 0:
            return {"status": "not_found", "session_id": session_id, "deleted": 0}
        return {"status": "deleted", "session_id": session_id, "documents_deleted": d_del,
                "chunks_deleted": c_del, "singles_deleted": s_del, "deleted": total}

    # ------------------------------------------------------------ repair (§3.10)
    def repair(self):
        # One repair at a time (L5): the hook calls /repair on every prompt with a 10-second timeout,
        # and embedding 200 records on a CPU can take longer, so the next call returns at once.
        if not _REPAIR_LOCK.acquire(blocking=False):
            return {"status": "ok", "message": "repair already running", "reembedded": 0, "remaining": 0}
        try:
            return self._repair()
        finally:
            _REPAIR_LOCK.release()

    def _repair(self):
        self.ensure_index()
        model, msg = P.get_model(self.cfg["embedding_model"], wait=0)
        todo, remaining = [], 0
        keys = [k for k in self.client.scan_iter(match="mem:*", count=1000) if not k.startswith(b"mem:sha:")]
        for i in range(0, len(keys), 500):
            part = keys[i:i + 500]
            pipe = self.client.pipeline(transaction=False)
            for k in part:
                pipe.hmget(k, ["owner_id", "content"])
                pipe.hexists(k, "embedding")
            res = pipe.execute(raise_on_error=False)
            for j, k in enumerate(part):
                vals, has = res[2 * j], res[2 * j + 1]
                if isinstance(vals, Exception) or isinstance(has, Exception) or not vals:
                    continue
                if _s(vals[0]) != self.owner or vals[1] is None or has:
                    continue
                if model is not None and len(todo) < 200:
                    todo.append((k, _s(vals[1])))
                else:
                    remaining += 1
        if model is None:
            return {"status": "ok", "message": "index ensured", "reembedded": 0, "remaining": remaining,
                    "warning": "embedding_unavailable", "detail": msg or ""}
        written = 0
        if todo:
            vecs, msg = P.embed_passages([t for _, t in todo], self.cfg)
            if vecs is None:
                return {"status": "ok", "message": "index ensured", "reembedded": 0,
                        "remaining": remaining + len(todo), "warning": "embedding_unavailable",
                        "detail": msg or ""}
            pipe = self.client.pipeline(transaction=False)
            for (k, _), v in zip(todo, vecs):
                self._scripts["hset"](keys=[k], args=["embedding", P.to_bytes(v)], client=pipe)
            written = sum(1 for x in pipe.execute() if x == 1)
        return {"status": "ok", "message": "index ensured", "reembedded": written, "remaining": remaining}
