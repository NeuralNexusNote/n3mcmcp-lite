"""The Redis layer (spec §3.3-§3.14, §3.16): connection and per-call check, index, ephemeral
settings, save with the guard script, search with shaping, list, recall, delete, repair."""
import datetime
import math
import os
import re
import sys
import threading
import time
import uuid

import redis

from . import config as _config
from . import processor as P

INDEX = "n3mc_idx"
MEM = "mem:"
SHA = "mem:sha:"
DOC = "doc:"
DOCSHA = "docsha:"

_RETURN = ["id", "content", "parent_id", "owner_id", "session_id", "turn_id", "timestamp",
           "timestamp_epoch", "importance", "access_count", "agent_name"]

# ── Lua scripts (§3.3, §3.5, §3.8) ────────────────────────────────────────────

SAVE_WITH_GUARD = """
local ok = redis.call('SET', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2])
if not ok then return 0 end
local pos = 3
for i = 2, #KEYS do
  local n = tonumber(ARGV[pos])
  pos = pos + 1
  local args = {}
  for j = 1, n * 2 do args[j] = ARGV[pos + j - 1] end
  pos = pos + n * 2
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

# Release a guard left by a failed save: only when it still holds this save's id and the record
# it guards does not exist.
RELEASE_GUARD = """
if redis.call('GET', KEYS[1]) == ARGV[1] and redis.call('EXISTS', KEYS[2]) == 0 then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

# Delete records (KEYS[2..]) and the guard KEYS[1] only when the guard holds ARGV[1].
DELETE_WITH_GUARD = """
local n = 0
for i = 2, #KEYS do n = n + redis.call('DEL', KEYS[i]) end
if redis.call('GET', KEYS[1]) == ARGV[1] then redis.call('DEL', KEYS[1]) end
return n
"""


def _s(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return str(v)


def _f(v, default=0.0) -> float:
    try:
        x = float(_s(v))
        return default if math.isnan(x) else x
    except (TypeError, ValueError):
        return default


def _bare_id(key) -> str:
    k = _s(key)
    return k[len(MEM):] if k.startswith(MEM) else k


def _uuid7() -> str:
    try:
        import uuid_utils
        return str(uuid_utils.uuid7())
    except Exception:  # noqa: BLE001
        return str(uuid.uuid4())


def _now():
    dt = datetime.datetime.now(datetime.timezone.utc)
    return dt.isoformat(), dt.timestamp()


def _db_index(url: str) -> int:
    try:
        from urllib.parse import urlsplit
        p = (urlsplit(url).path or "/0").lstrip("/")
        return int(p or 0)
    except Exception:  # noqa: BLE001
        return 0


class _FieldError(ValueError):
    pass


def _field_str(name, v) -> str:
    if v is None:
        return ""
    if not isinstance(v, str):
        raise _FieldError(f"{name} must be a string (got {type(v).__name__})")
    return P.sanitize_surrogates(v)


def guard_key_for(content: str, turn_id: str, doc: bool) -> str:
    """The guard of a stored record, recomputed from it (§3.16 "deleting this guard")."""
    h = P.content_hash(content)
    prefix = DOCSHA if doc else SHA
    if turn_id and content.startswith(("[user", "[claude")):
        return f"{prefix}{turn_id}:{h}"
    return f"{prefix}{h}"


class BackendDown(Exception):
    """Redis is unreachable (or lost during a call)."""


class Database:
    def __init__(self, cfg: dict, default_session: str = None):
        self.cfg = cfg
        self.url = cfg.get("redis_url") or _config.DEFAULTS["redis_url"]
        self.default_session = default_session or os.environ.get("N3MC_SESSION_ID") or str(uuid.uuid4())
        self.client = None
        self.index_ok = False
        self.index_error = None
        self._lock = threading.Lock()
        self._was_down = False
        self._scorer = None
        self._scripts = {}

    # ── connection (§3.9) ─────────────────────────────────────────────────────

    def _new_client(self):
        return redis.Redis.from_url(self.url, decode_responses=False, protocol=2,
                                    socket_connect_timeout=2, socket_timeout=5)

    def hint(self) -> str:
        return _config.backend_hint(self.url)

    def index_hint(self) -> str:
        return (f"The Redis at {_config._hostport(self.url)} does not provide RediSearch (FT.CREATE "
                f"failed: {self.index_error}). N3MC Lite needs Redis Stack, not plain Redis: docker run -d "
                "--name redis-stack -p 127.0.0.1:6379:6379 redis/redis-stack-server:latest")

    def mark_down(self) -> None:
        if not self._was_down:
            print(f"n3mc-lite: lost the connection to Redis at {_config._hostport(self.url)}", file=sys.stderr)
        self._was_down = True
        old, self.client = self.client, None
        self.index_ok = False
        if old is not None:
            try:
                old.close()
            except Exception:  # noqa: BLE001
                pass

    def ensure_connected(self, ephemeral: bool = True) -> str:
        """"ok", or the recovery hint (Redis unreachable, or no RediSearch)."""
        with self._lock:
            if self.client is not None:
                try:
                    self.client.ping()
                    if not self.index_ok:
                        self._try_index()
                    return "ok" if self.index_ok else (self.hint() if self.client is None else self.index_hint())
                except (redis.RedisError, OSError):
                    self.mark_down()
            old = self.client
            if old is not None:
                try:
                    old.close()
                except Exception:  # noqa: BLE001
                    pass
            client = None
            try:
                client = self._new_client()
                client.ping()
            except (redis.RedisError, OSError, ValueError):
                if client is not None:
                    try:
                        client.close()
                    except Exception:  # noqa: BLE001
                        pass
                self.client = None
                if not self._was_down:
                    print(f"n3mc-lite: {self.hint()}", file=sys.stderr)
                self._was_down = True
                return self.hint()
            self.client = client
            self._scripts = {}
            if self._was_down:
                print(f"n3mc-lite: connected to Redis at {_config._hostport(self.url)}", file=sys.stderr)
            self._was_down = False
            if ephemeral:
                try:
                    self.enforce_ephemeral()
                except (redis.ConnectionError, redis.TimeoutError):
                    self.mark_down()
                    return self.hint()
                except Exception as e:  # noqa: BLE001
                    print(f"n3mc-lite: enforce_ephemeral failed ({type(e).__name__}: {e})", file=sys.stderr)
            self._try_index()
            if self.client is None:
                return self.hint()
            return "ok" if self.index_ok else self.index_hint()

    def _try_index(self) -> None:
        try:
            self.ensure_index()
        except (redis.ConnectionError, redis.TimeoutError):
            self.mark_down()

    def _script(self, name: str):
        s = self._scripts.get(name)
        if s is None:
            s = self.client.register_script(globals()[name])
            self._scripts[name] = s
        return s

    # ── persistence settings (§3.4) ───────────────────────────────────────────

    def enforce_ephemeral(self) -> None:
        hostport = _config._hostport(self.url)
        shared = False
        try:
            info = self.client.info("keyspace")
            mine = f"db{_db_index(self.url)}"
            for k, v in (info or {}).items():
                name = _s(k)
                if name.startswith("db") and name != mine:
                    keys = v.get("keys", 0) if isinstance(v, dict) else 0
                    if keys:
                        shared = True
                        break
        except redis.ResponseError:
            shared = True
        if not shared:
            for key in self.client.scan_iter(count=1000):
                k = key if isinstance(key, bytes) else str(key).encode()
                if not (k.startswith(b"mem:") or k.startswith(b"doc:") or k.startswith(b"docsha:")):
                    shared = True
                    break
        if shared:
            _config.add_notice(self.cfg, (
                f"shared_redis: the Redis at {hostport} also holds keys that are not N3MC's, so its "
                "persistence settings were left unchanged. N3MC memories in it may survive a Redis "
                "restart. Use a dedicated container (docker run -d --name redis-stack -p "
                "127.0.0.1:6379:6379 redis/redis-stack-server:latest) for Lite."))
            return
        try:
            self.client.config_set("appendonly", "no")
            self.client.config_set("save", "")
        except redis.ResponseError as e:
            print(f"n3mc-lite: CONFIG SET refused by {hostport} ({e})", file=sys.stderr)
            _config.add_notice(self.cfg, (
                f"config_refused: the Redis at {hostport} refused CONFIG SET ({e}), so its persistence "
                "settings could not be turned off. N3MC memories in it may survive a Redis restart. Use "
                "a dedicated container (docker run -d --name redis-stack -p 127.0.0.1:6379:6379 "
                "redis/redis-stack-server:latest) for Lite."))

    # ── index (§3.5, §3.7, §3.9 step 3) ───────────────────────────────────────

    def _create_index(self) -> None:
        dim = int(self.cfg.get("embedding_dim", 768))
        self.client.execute_command(
            "FT.CREATE", INDEX, "ON", "HASH", "PREFIX", 1, MEM, "SCHEMA",
            "content_fts", "TEXT", "WEIGHT", 1.0,
            "content_ngram", "TEXT", "WEIGHT", 0.8,
            "timestamp_epoch", "NUMERIC", "SORTABLE",
            "owner_id", "TAG", "local_id", "TAG", "agent_name", "TAG", "session_id", "TAG",
            "turn_id", "TAG", "importance", "NUMERIC", "access_count", "NUMERIC", "parent_id", "TAG",
            "embedding", "VECTOR", "FLAT", 6, "TYPE", "FLOAT32", "DIM", dim, "DISTANCE_METRIC", "COSINE")

    def index_info(self):
        """(attribute names, vector dim or None) of the existing index."""
        raw = self.client.execute_command("FT.INFO", INDEX)
        if isinstance(raw, dict):
            items = list(raw.items())
        else:
            items = [(raw[i], raw[i + 1]) for i in range(0, len(raw) - 1, 2)]
        names, dim = set(), None
        for k, v in items:
            if _s(k).lower() != "attributes":
                continue
            for attr in v or []:
                toks = list(attr.items()) if isinstance(attr, dict) else list(attr)
                flat = []
                for t in toks:
                    flat.extend(t if isinstance(t, tuple) else [t])
                for i, t in enumerate(flat[:-1]):
                    lt = _s(t).lower()
                    if lt == "identifier":
                        names.add(_s(flat[i + 1]))
                    elif lt == "dim":
                        try:
                            dim = int(_s(flat[i + 1]))
                        except ValueError:
                            pass
        return names, dim

    def ensure_index(self) -> None:
        try:
            self._create_index()
            self.index_ok, self.index_error = True, None
            return
        except redis.ResponseError as e:
            if "already exists" not in str(e).lower():
                self.index_ok, self.index_error = False, str(e)
                return
        self.index_ok, self.index_error = True, None
        try:
            names, dim = self.index_info()
        except redis.ResponseError:
            return
        want = int(self.cfg.get("embedding_dim", 768))
        if dim is not None and dim != want:
            _config.add_notice(self.cfg, (
                f"index_dim_mismatch: the existing index {INDEX} was built for {dim} dimensions but "
                f"embedding_dim is {want}; new vectors are not indexed. Drop the index (FT.DROPINDEX "
                f"{INDEX}) or flush the test DB, then restart."))
            return
        if "content_fts" not in names:
            try:
                self.client.execute_command("FT.DROPINDEX", INDEX)
            except redis.ResponseError as e:
                if "unknown index" not in str(e).lower():
                    raise
            try:
                self._create_index()
            except redis.ResponseError as e:
                if "already exists" not in str(e).lower():
                    self.index_ok, self.index_error = False, str(e)

    # ── FT.SEARCH (raw, RESP2; a RESP3 dict is handled too) ───────────────────

    def _ft_search(self, query, return_fields, *, params=None, sortby=None, offset=0, num=10,
                   withscores=False, scorer=None):
        args = ["FT.SEARCH", INDEX, query]
        if withscores:
            args.append("WITHSCORES")
        if scorer:
            args += ["SCORER", scorer]
        args += ["RETURN", len(return_fields), *return_fields]
        if sortby:
            args += ["SORTBY", sortby[0], sortby[1]]
        args += ["LIMIT", offset, num]
        if params:
            args += ["PARAMS", len(params) * 2]
            for k, v in params.items():
                args += [k, v]
        args += ["DIALECT", 2]
        try:
            res = self.client.execute_command(*args)
        except redis.ResponseError as e:
            # The index vanished while connected (FLUSHDB, FT.DROPINDEX): create it again, retry once.
            msg = str(e).lower()
            if "unknown index" not in msg and "no such index" not in msg:
                raise
            self.ensure_index()
            res = self.client.execute_command(*args)
        return self._parse_search(res, withscores)

    @staticmethod
    def _parse_search(res, withscores):
        rows = []
        total = 0
        if isinstance(res, dict):
            total = int(res.get(b"total_results", res.get("total_results", 0)) or 0)
            for r in res.get(b"results", res.get("results", [])) or []:
                key = r.get(b"id", r.get("id"))
                attrs = r.get(b"extra_attributes", r.get("extra_attributes", {})) or {}
                d = {_s(k): v for k, v in attrs.items()}
                d["__key"] = _s(key)
                if withscores:
                    d["__score"] = _f(r.get(b"score", r.get("score")))
                rows.append(d)
            return total, rows
        if not res:
            return 0, rows
        total = int(res[0])
        i = 1
        while i < len(res):
            key = res[i]
            i += 1
            score = None
            if withscores and i < len(res):
                score = _f(res[i])
                i += 1
            fields = res[i] if i < len(res) else []
            i += 1
            d = {}
            if isinstance(fields, (list, tuple)):
                for j in range(0, len(fields) - 1, 2):
                    d[_s(fields[j])] = fields[j + 1]
            d["__key"] = _s(key)
            if withscores:
                d["__score"] = score
            rows.append(d)
        return total, rows

    @staticmethod
    def _time_filter(since, until) -> str:
        if since is None and until is None:
            return ""
        lo = "-inf" if since is None else repr(float(since))
        hi = "+inf" if until is None else repr(float(until))
        return f"@timestamp_epoch:[{lo} {hi}]"

    def _vector_search(self, qvec, k, since, until):
        tf = self._time_filter(since, until)
        q = f"{tf or '*'}=>[KNN {k} @embedding $vec AS __dist]"
        try:
            _, rows = self._ft_search(q, _RETURN + ["__dist"], params={"vec": P.pack_vector(qvec)},
                                      sortby=("__dist", "ASC"), num=k)
        except (redis.ConnectionError, redis.TimeoutError):
            raise
        except redis.ResponseError as e:
            print(f"n3mc-lite: vector search failed ({e})", file=sys.stderr)
            return []
        return rows

    def _bm25_search(self, query, k, since, until):
        fq = P.bm25_query(query)
        if fq is None:
            return []
        tf = self._time_filter(since, until)
        q = f"({fq}) {tf}" if tf else fq
        scorers = [self._scorer] if self._scorer else ["BM25", "BM25STD", None]
        for sc in scorers:
            try:
                _, rows = self._ft_search(q, _RETURN, withscores=True, scorer=sc, num=k)
                self._scorer = sc
                return rows
            except (redis.ConnectionError, redis.TimeoutError):
                raise
            except redis.ResponseError as e:
                if "scorer" in str(e).lower() and sc is not None:
                    continue
                print(f"n3mc-lite: keyword search failed ({e})", file=sys.stderr)
                return []
        return []

    # ── save (§3.8, §3.11, §3.13, §3.16) ──────────────────────────────────────

    def save_memory(self, content, agent_name="", owner_id=None, importance=1.0, session_id=None,
                    turn_id="", role=None, embed=True, no_model_wait=False, embed_detail=None) -> dict:
        cfg = self.cfg
        if owner_id not in (None, ""):
            if not isinstance(owner_id, str) or owner_id != cfg.get("owner_id"):
                return {"status": "error", "saved": False, "reason": "owner_id mismatch"}
        if importance is None:
            imp = 1.0
        else:
            try:
                if isinstance(importance, bool):
                    raise ValueError
                imp = float(importance)
                if not math.isfinite(imp):
                    raise ValueError
            except (TypeError, ValueError):
                return {"saved": False, "status": "error",
                        "reason": f"importance must be numeric (got {importance})"}
        imp = max(0.5, min(2.0, imp))
        if not isinstance(content, str):
            return {"status": "error", "saved": False, "reason": "content must be a string"}
        body = P.sanitize_surrogates(content).strip()
        if not body:
            return {"status": "error", "saved": False, "reason": "empty content"}
        if not P.has_visible_text(body):
            return {"status": "error", "saved": False, "reason": "empty content (no visible characters)"}
        if cfg.get("skip_code_blocks") and P.has_fence(body):
            return {"status": "skipped_code", "saved": False}
        body = P.purify(body)
        try:
            agent = _field_str("agent_name", agent_name)
            sess = _field_str("session_id", session_id)
            turn = _field_str("turn_id", turn_id)
        except _FieldError as e:
            return {"status": "error", "saved": False, "reason": str(e)}
        if not sess.strip():
            sess = self.default_session
        ttl = int(cfg.get("ttl_seconds", 604800))
        threshold = int(cfg.get("chunk_threshold", 400))
        overlap = int(cfg.get("chunk_overlap", 100))
        wait = not no_model_wait
        ts, epoch = _now()
        common = {"timestamp": ts, "timestamp_epoch": repr(epoch), "owner_id": cfg.get("owner_id", ""),
                  "local_id": cfg.get("local_id", ""), "agent_name": agent, "session_id": sess,
                  "turn_id": turn}
        missing_vector = False

        if len(body) <= threshold:
            stored = f"[{role}] {body}" if role else body
            guard = f"{SHA}{turn}:{P.content_hash(stored)}" if role else f"{SHA}{P.content_hash(body)}"
            if self.client.exists(guard):
                return {"status": "duplicate", "saved": False}
            vec = P.embed(stored, "passage", cfg, wait=wait) if embed else None
            missing_vector = vec is None
            rid = _uuid7()
            fields = dict(common)
            fields.update({"id": rid, "content": stored, "content_fts": P.fts_text(body),
                           "content_ngram": P.ngram_text(body), "importance": repr(imp),
                           "access_count": "0", "parent_id": ""})
            if vec is not None:
                fields["embedding"] = P.pack_vector(vec)
            keys = [guard, MEM + rid]
            ok = self._run_save(keys, rid, ttl, [fields], MEM + rid)
            if not ok:
                return {"status": "duplicate", "saved": False}
            result = {"status": "saved", "saved": True, "id": rid, "ttl_seconds": ttl}
        else:
            chunks = P.chunk_text(body, threshold, overlap)
            n = len(chunks)
            doc_content = f"[{role}] {body}" if role else body
            dguard = (f"{DOCSHA}{turn}:{P.content_hash(doc_content)}" if role
                      else f"{DOCSHA}{P.content_hash(body)}")
            existing = self.client.get(dguard)
            if existing is not None:
                return {"status": "duplicate", "saved": False, "parent_id": _s(existing)}
            if role:
                contents = [f"[{role} {i + 1}/{n}] {c}" if n > 1 else f"[{role}] {c}"
                            for i, c in enumerate(chunks)]
            else:
                contents = list(chunks)
            vecs = P.embed_many(contents, "passage", cfg, wait=wait) if embed else None
            missing_vector = vecs is None
            pid = _uuid7()
            cids = [_uuid7() for _ in range(n)]
            doc = dict(common)
            doc.update({"id": pid, "content": doc_content, "chunk_count": str(n),
                        "chunk_ids": " ".join(cids)})
            records = [doc]
            for i, (cid, c) in enumerate(zip(cids, chunks)):
                f = dict(common)
                f.update({"id": cid, "content": contents[i], "content_fts": P.fts_text(c),
                          "content_ngram": P.ngram_text(c), "importance": repr(imp),
                          "access_count": "0", "parent_id": pid})
                if vecs is not None:
                    f["embedding"] = P.pack_vector(vecs[i])
                records.append(f)
            keys = [dguard, DOC + pid] + [MEM + c for c in cids]
            ok = self._run_save(keys, pid, ttl, records, DOC + pid)
            if not ok:
                cur = self.client.get(dguard)
                return {"status": "duplicate", "saved": False, "parent_id": _s(cur)}
            result = {"status": "saved", "saved": True, "parent_id": pid, "chunks": n, "saved_count": n,
                      "ids": cids, "ttl_seconds": ttl}
        warnings, details = [], []
        if missing_vector:
            warnings.append("embedding_unavailable")
            details.append(embed_detail or P.unavailable_message(cfg))
        if cfg.get("_unreadable"):
            warnings.append("config_unreadable")
            details.append(_config.config_unreadable_detail(cfg))
        if warnings:
            result["warning"] = "; ".join(warnings)
            result["detail"] = " ".join(details)
        return result

    def _run_save(self, keys, rid, ttl, records, main_key) -> bool:
        args = [rid, ttl]
        for rec in records:
            args.append(len(rec))
            for k, v in rec.items():
                args += [k, v]
        try:
            return bool(self._script("SAVE_WITH_GUARD")(keys=keys, args=args))
        except BaseException:
            try:
                self._script("RELEASE_GUARD")(keys=[keys[0], main_key], args=[rid])
            except Exception:  # noqa: BLE001
                pass
            raise

    # ── search (§3.6, §3.11, §3.14) ───────────────────────────────────────────

    def search(self, query, limit=None, session_id=None, since=None, until=None, wait_model=True):
        """(normal rows, weak rows, model message). Raises processor.TimeBoundError."""
        cfg = self.cfg
        q = P.sanitize_surrogates(query) if isinstance(query, str) else ""
        q = q[: int(cfg.get("search_query_max_chars", 2000))]
        try:
            lim = int(limit) if limit not in (None, "") and not isinstance(limit, bool) else int(cfg.get("search_result_limit", 20))
        except (TypeError, ValueError):
            lim = int(cfg.get("search_result_limit", 20))
        lim = max(1, lim)
        s_ep, u_ep = P.parse_time_bounds(since, until)
        sess = session_id.strip() if isinstance(session_id, str) and session_id.strip() else self.default_session
        k = min(lim * 5, 100)
        msg = ""
        qvec = P.embed(q, "query", cfg, wait=wait_model)
        if qvec is None:
            msg = P.unavailable_message(cfg)
        vec_rows = self._vector_search(qvec, k, s_ep, u_ep) if qvec is not None else []
        bm_rows = self._bm25_search(q, k, s_ep, u_ep)
        owner = cfg.get("owner_id")

        cands = {}
        for r in vec_rows:
            if _s(r.get("owner_id")) != owner:
                continue
            rid = _bare_id(r["__key"])
            c = cands.setdefault(rid, self._row_from(r, rid))
            c["cos"] = P.cos_sim_from_distance(_s(r.get("__dist")))
            c["in_vec"] = True
        bm_max = 0.0
        for r in bm_rows:
            if _s(r.get("owner_id")) != owner:
                continue
            bm_max = max(bm_max, abs(r.get("__score") or 0.0))
        for r in bm_rows:
            if _s(r.get("owner_id")) != owner:
                continue
            rid = _bare_id(r["__key"])
            c = cands.setdefault(rid, self._row_from(r, rid))
            c["in_bm"] = True
            c["kw"] = P.keyword_relevance(r.get("__score") or 0.0, bm_max, float(cfg.get("bm25_min_threshold", 0.1)))
        if not cands:
            return [], [], msg

        floor = float(cfg.get("vector_min_cos", 0.85))
        hl = float(cfg.get("half_life_days", 3))
        bmatch, bmis = float(cfg.get("b_session_match", 1.0)), float(cfg.get("b_session_mismatch", 0.6))
        now = time.time()
        for c in cands.values():
            cos = c.get("cos", 0.0)
            kw = c.get("kw", 0.0)
            passes = c.get("in_vec") and cos >= floor
            c["eligible"] = bool(passes or c.get("in_bm"))
            cos_n = cos if passes else 0.0
            bl = P.b_local(c["importance"], c["access_count"], cfg)
            decay = P.time_decay(c["timestamp_epoch"], hl, now)
            bs = bmatch if (sess and c["session_id"] == sess) else bmis
            c["n_filter"] = P.base_score(cos_n, kw) * bl
            c["n_score"] = c["n_filter"] * decay * bs
            c["w_filter"] = P.base_score(cos, kw) * bl
            c["w_score"] = c["w_filter"] * decay * bs

        parents = self._fetch_parents({c["parent_id"] for c in cands.values() if c["parent_id"]})

        # Normal rows: parent resolution (keep the best hit of each parent), rerank, min_score.
        ordered = sorted((c for c in cands.values() if c["eligible"]), key=lambda c: c["n_score"], reverse=True)
        normal, seen, dropped = [], set(), []
        for c in ordered:
            row = self._resolve(c, parents, c["n_score"], c["n_filter"])
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            bonus = P.lexical_bonus(q, row["content"], cfg)
            row["score"] += bonus
            row["filter_score"] += bonus
            if row["filter_score"] >= float(cfg.get("min_score", 0.2)):
                normal.append(row)
            else:
                dropped.append(row["id"])
        normal.sort(key=lambda r: (r["score"], r["timestamp_epoch"], r["id"]), reverse=True)
        if cfg.get("version_order_enabled", True) and len(normal) > 1:
            self._version_order(normal)
        ratio = float(cfg.get("relative_min_score_ratio", 0.6))
        if ratio > 0 and normal:
            top = max(r["filter_score"] for r in normal)
            normal = [r for r in normal if r.get("_version_group") or r["filter_score"] >= top * ratio]
        normal = normal[:lim]

        weak = []
        want = min(int(cfg.get("min_results", 3)), lim)
        if want > 0 and q.strip() and len(normal) < want:
            have = {r["id"] for r in normal}
            pool = []
            for c in cands.values():
                row = self._resolve(c, parents, c["w_score"], c["w_filter"])
                if row["id"] in have:
                    continue
                bonus = P.lexical_bonus(q, row["content"], cfg)
                row["score"] += bonus
                row["filter_score"] += bonus
                if row["filter_score"] < 1e-6:
                    continue
                pool.append(row)
            pool.sort(key=lambda r: (r["filter_score"], r["timestamp_epoch"], r["id"]), reverse=True)
            for row in pool:
                if len(normal) + len(weak) >= want:
                    break
                if row["id"] in have:
                    continue
                have.add(row["id"])
                row["_weak"] = True
                weak.append(row)

        top_k = int(cfg.get("ttl_refresh_top_k", 5))
        if top_k > 0 and normal:
            pipe = self.client.pipeline(transaction=False)
            script = self._script("INCR_IF_EXISTS")
            for r in normal[:top_k]:
                script(keys=[MEM + r["_hit_id"]], args=["access_count", 1], client=pipe)
            pipe.execute()
        return normal, weak, msg

    @staticmethod
    def _row_from(r, rid):
        return {"id": rid, "_hit_id": rid, "content": _s(r.get("content")), "parent_id": _s(r.get("parent_id")),
                "session_id": _s(r.get("session_id")), "turn_id": _s(r.get("turn_id")),
                "timestamp": _s(r.get("timestamp")), "timestamp_epoch": _f(r.get("timestamp_epoch")),
                "importance": _f(r.get("importance"), 1.0), "access_count": _f(r.get("access_count")),
                "agent_name": _s(r.get("agent_name"))}

    def _fetch_parents(self, pids) -> dict:
        pids = [p for p in pids if p]
        if not pids:
            return {}
        fields = ["content", "chunk_count", "chunk_ids", "turn_id", "timestamp", "timestamp_epoch",
                  "owner_id", "session_id"]
        pipe = self.client.pipeline(transaction=False)
        for p in pids:
            pipe.hmget(DOC + p, fields)
        out = {}
        for p, vals in zip(pids, pipe.execute()):
            if vals is None or vals[0] is None:
                continue
            d = dict(zip(fields, vals))
            if _s(d["owner_id"]) != self.cfg.get("owner_id"):
                continue
            out[p] = {"content": _s(d["content"]), "chunk_count": int(_f(d["chunk_count"])),
                      "chunk_ids": _s(d["chunk_ids"]).split(), "turn_id": _s(d["turn_id"]),
                      "timestamp": _s(d["timestamp"]), "timestamp_epoch": _f(d["timestamp_epoch"]),
                      "session_id": _s(d["session_id"])}
        return out

    @staticmethod
    def _resolve(c, parents, score, filt) -> dict:
        row = {k: v for k, v in c.items() if not k.startswith(("n_", "w_"))}
        row["score"], row["filter_score"] = score, filt
        row["_doc"] = False
        p = parents.get(c["parent_id"]) if c["parent_id"] else None
        if p is not None:
            row.update({"id": c["parent_id"], "content": p["content"], "turn_id": p["turn_id"],
                        "timestamp": p["timestamp"], "timestamp_epoch": p["timestamp_epoch"],
                        "chunk_count": p["chunk_count"], "chunk_ids": p["chunk_ids"], "_doc": True})
        return row

    def _version_order(self, rows) -> None:
        min_chars = int(self.cfg.get("version_min_chars", 100))
        thr = float(self.cfg.get("version_sim_threshold", 0.98))
        reps = {}
        keys = []
        for i, r in enumerate(rows):
            if r.get("_doc"):
                ids = r.get("chunk_ids") or []
                if not ids:
                    continue
                keys.append((i, MEM + ids[0]))
            else:
                keys.append((i, MEM + r["_hit_id"]))
        if not keys:
            return
        pipe = self.client.pipeline(transaction=False)
        for _, k in keys:
            pipe.hmget(k, ["content", "embedding"])
        for (i, _), vals in zip(keys, pipe.execute()):
            text, emb = _s(vals[0]), vals[1]
            if len(text) < min_chars or not emb:
                continue
            vec = P.unpack_vector(emb)
            if vec is not None:
                reps[i] = vec
        idx = sorted(reps)
        parent = {i: i for i in idx}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for a in range(len(idx)):
            for b in range(a + 1, len(idx)):
                if P.cosine(reps[idx[a]], reps[idx[b]]) >= thr:
                    ra, rb = find(idx[a]), find(idx[b])
                    if ra != rb:
                        parent[rb] = ra
        groups = {}
        for i in idx:
            groups.setdefault(find(i), []).append(i)
        gid = 0
        for members in groups.values():
            if len(members) < 2:
                continue
            gid += 1
            positions = sorted(members)
            vals = [(rows[p]["score"], rows[p]["filter_score"]) for p in positions]
            moved = sorted((rows[p] for p in positions), key=lambda r: (r["timestamp_epoch"], r["id"]), reverse=True)
            for p, r, (s, f) in zip(positions, moved, vals):
                r["score"], r["filter_score"] = s, f
                r["_version_group"] = gid
                rows[p] = r

    # ── scans ─────────────────────────────────────────────────────────────────

    def _scan_hashes(self, prefix: str, fields):
        """[(bare id, dict)] for every hash under `prefix` (mem:sha: is skipped)."""
        out = []
        batch = []
        for key in self.client.scan_iter(match=prefix + "*", count=1000):
            k = _s(key)
            if k.startswith(SHA) or (prefix == DOC and k.startswith(DOCSHA)):
                continue
            batch.append(k)
        for i in range(0, len(batch), 500):
            part = batch[i:i + 500]
            pipe = self.client.pipeline(transaction=False)
            for k in part:
                pipe.hmget(k, fields)
            try:
                results = pipe.execute(raise_on_error=False)
            except redis.ResponseError:
                results = [None] * len(part)
            for k, vals in zip(part, results):
                if not isinstance(vals, (list, tuple)) or all(v is None for v in vals):
                    continue
                out.append((k[len(prefix):], dict(zip(fields, vals))))
        return out

    # ── list (§4.3, §5 (4)) ───────────────────────────────────────────────────

    def list_memories(self, limit=20) -> str:
        try:
            lim = int(limit) if limit not in (None, "") and not isinstance(limit, bool) else 20
        except (TypeError, ValueError):
            lim = 20
        if lim == 0:
            lim = 20
        lim = max(1, lim)
        owner = self.cfg.get("owner_id")
        singles = []
        offset = 0
        while len(singles) < lim:
            total, rows = self._ft_search("*", ["id", "content", "owner_id", "parent_id", "timestamp"],
                                          sortby=("timestamp_epoch", "DESC"), offset=offset, num=200)
            if not rows:
                break
            for r in rows:
                if _s(r.get("owner_id")) == owner and not _s(r.get("parent_id")):
                    singles.append({"id": _bare_id(r["__key"]), "content": _s(r.get("content")),
                                    "timestamp": _s(r.get("timestamp")), "tag": ""})
            offset += 200
            if offset >= total:
                break
        singles = singles[:lim]
        docs = []
        for pid, d in self._scan_hashes(DOC, ["owner_id", "content", "timestamp", "chunk_count"]):
            if _s(d["owner_id"]) != owner:
                continue
            body = _s(d["content"])
            short = body[:200] + ("…" if len(body) > 200 else "")
            docs.append({"id": pid, "content": short, "timestamp": _s(d["timestamp"]),
                         "tag": f"[doc×{int(_f(d['chunk_count']))}]"})
        items = sorted(singles + docs, key=lambda x: x["timestamp"], reverse=True)[:lim]
        if not items:
            return "_No memories stored._"
        blocks = []
        for it in items:
            tag = it["tag"] + " " if it["tag"] else ""
            blocks.append(f"**{tag}[{it['id']}]** {it['timestamp'][:19]}\n{P.purify(it['content'])}")
        return "\n\n---\n\n".join(blocks)

    # ── recall_thread (§4.3.1) ────────────────────────────────────────────────

    def recall_thread(self, turn_id, before=2, after=2):
        """markdown text, or a dict (error / not_found)."""
        if not isinstance(turn_id, str) or not turn_id.strip():
            return {"status": "error", "reason": "turn_id required"}
        turn_id = P.sanitize_surrogates(turn_id)

        def _n(v):
            try:
                return max(0, int(v)) if v is not None and not isinstance(v, bool) else 2
            except (TypeError, ValueError):
                return 2
        before, after = _n(before), _n(after)
        owner = self.cfg.get("owner_id")
        docs = {}
        for pid, d in self._scan_hashes(DOC, ["owner_id", "content", "timestamp", "timestamp_epoch",
                                              "turn_id", "chunk_count"]):
            docs[pid] = d
        rows = []
        for pid, d in docs.items():
            if _s(d["owner_id"]) != owner:
                continue
            rows.append({"id": pid, "content": _s(d["content"]), "timestamp": _s(d["timestamp"]),
                         "epoch": _f(d["timestamp_epoch"]), "turn_id": _s(d["turn_id"]),
                         "tag": f"[doc×{int(_f(d['chunk_count']))}]"})
        for rid, d in self._scan_hashes(MEM, ["owner_id", "content", "timestamp", "timestamp_epoch",
                                              "turn_id", "parent_id"]):
            if _s(d["owner_id"]) != owner:
                continue
            pid = _s(d["parent_id"])
            if pid and pid in docs:
                continue
            rows.append({"id": rid, "content": _s(d["content"]), "timestamp": _s(d["timestamp"]),
                         "epoch": _f(d["timestamp_epoch"]), "turn_id": _s(d["turn_id"]), "tag": ""})
        rows.sort(key=lambda r: (r["epoch"], r["id"]))
        hits = [i for i, r in enumerate(rows) if r["turn_id"] == turn_id]
        if not hits:
            return {"status": "not_found", "turn_id": turn_id}
        lo = max(0, hits[0] - before)
        hi = min(len(rows), hits[-1] + after + 1)
        blocks = []
        for r in rows[lo:hi]:
            mark = "▶ " if r["turn_id"] == turn_id else "  "
            tag = r["tag"] + " " if r["tag"] else ""
            blocks.append(f"{mark}**{tag}[{r['id']}]** {r['timestamp'][:19]}\n{P.purify(r['content'])}")
        return "\n\n---\n\n".join(blocks)

    # ── delete (§3.5, §3.11, §4.3) ────────────────────────────────────────────

    def _delete_doc(self, pid, d) -> int:
        """Delete a parent, its chunks and its guard. The number of chunks deleted."""
        chunk_ids = _s(d.get("chunk_ids")).split()
        if not chunk_ids:
            chunk_ids = self._chunks_of(pid)
        guard = guard_key_for(_s(d.get("content")), _s(d.get("turn_id")), doc=True)
        keys = [guard, DOC + pid] + [MEM + c for c in chunk_ids]
        n = self._script("DELETE_WITH_GUARD")(keys=keys, args=[pid])
        return max(0, int(n) - 1)

    def _chunks_of(self, pid):
        """Chunks of a parent saved before chunk_ids existed: FT.SEARCH, then SCAN (§3.12)."""
        try:
            esc = re.sub(r"([^A-Za-z0-9_])", r"\\\1", pid)
            _, rows = self._ft_search(f"@parent_id:{{{esc}}}", ["parent_id"], num=10000)
            ids = [_bare_id(r["__key"]) for r in rows if _s(r.get("parent_id")) == pid]
            if ids:
                return ids
        except redis.ResponseError:
            pass
        return [rid for rid, d in self._scan_hashes(MEM, ["parent_id"]) if _s(d["parent_id"]) == pid]

    def delete_memory(self, mem_id) -> dict:
        if not isinstance(mem_id, str) or not mem_id.strip():
            return {"status": "error", "reason": "id required"}
        rid = P.sanitize_surrogates(mem_id).strip()
        if rid.startswith(DOC):
            rid = rid[len(DOC):]
        elif rid.startswith(MEM):
            rid = rid[len(MEM):]
        owner = self.cfg.get("owner_id")
        fields = ["owner_id", "content", "turn_id", "chunk_ids"]
        vals = self.client.hmget(DOC + rid, fields)
        if vals and vals[0] is not None:
            d = dict(zip(fields, vals))
            if _s(d["owner_id"]) != owner:
                return {"status": "not_found", "id": rid}
            n = self._delete_doc(rid, d)
            return {"status": "deleted", "id": rid, "chunks_deleted": n}
        fields = ["owner_id", "content", "turn_id", "parent_id"]
        vals = self.client.hmget(MEM + rid, fields)
        if not vals or vals[0] is None:
            return {"status": "not_found", "id": rid}
        d = dict(zip(fields, vals))
        if _s(d["owner_id"]) != owner:
            return {"status": "not_found", "id": rid}
        if _s(d["parent_id"]):
            self.client.delete(MEM + rid)
        else:
            guard = guard_key_for(_s(d["content"]), _s(d["turn_id"]), doc=False)
            self._script("DELETE_WITH_GUARD")(keys=[guard, MEM + rid], args=[rid])
        return {"status": "deleted", "id": rid}

    def delete_by_session(self, session_id) -> dict:
        if not isinstance(session_id, str) or not session_id.strip():
            return {"status": "error", "reason": "session_id required"}
        sid = P.sanitize_surrogates(session_id)
        owner = self.cfg.get("owner_id")
        docs_deleted = chunks_deleted = singles_deleted = 0
        for pid, d in self._scan_hashes(DOC, ["owner_id", "session_id", "content", "turn_id", "chunk_ids"]):
            if _s(d["owner_id"]) != owner or _s(d["session_id"]) != sid:
                continue
            chunks_deleted += self._delete_doc(pid, d)
            docs_deleted += 1
        for rid, d in self._scan_hashes(MEM, ["owner_id", "session_id", "content", "turn_id", "parent_id"]):
            if _s(d["owner_id"]) != owner or _s(d["session_id"]) != sid:
                continue
            if _s(d["parent_id"]):
                chunks_deleted += int(self.client.delete(MEM + rid))
            else:
                guard = guard_key_for(_s(d["content"]), _s(d["turn_id"]), doc=False)
                singles_deleted += int(self._script("DELETE_WITH_GUARD")(keys=[guard, MEM + rid], args=[rid]))
        total = docs_deleted + chunks_deleted + singles_deleted
        if total == 0:
            return {"status": "not_found", "session_id": sid, "deleted": 0}
        return {"status": "deleted", "session_id": sid, "documents_deleted": docs_deleted,
                "chunks_deleted": chunks_deleted, "singles_deleted": singles_deleted, "deleted": total}

    # ── repair (§3.10) ────────────────────────────────────────────────────────

    def repair(self) -> dict:
        self.ensure_index()
        if not self.index_ok:
            return {"status": "error", "message": self.index_hint()}
        owner = self.cfg.get("owner_id")
        missing = []
        for rid, d in self._scan_hashes(MEM, ["owner_id", "content", "embedding"]):
            if _s(d["owner_id"]) != owner or d["embedding"]:
                continue
            missing.append((rid, _s(d["content"])))
        available = P.get_model(self.cfg, wait=False) is not None
        if not available:
            return {"status": "ok", "message": "index ensured", "reembedded": 0, "remaining": len(missing),
                    "warning": "embedding_unavailable", "detail": P.unavailable_message(self.cfg)}
        todo = missing[:200]
        reembedded = 0
        if todo:
            vecs = P.embed_many([c for _, c in todo], "passage", self.cfg, wait=False)
            if vecs is None:
                return {"status": "ok", "message": "index ensured", "reembedded": 0, "remaining": len(missing),
                        "warning": "embedding_unavailable", "detail": P.unavailable_message(self.cfg)}
            pipe = self.client.pipeline(transaction=False)
            script = self._script("HSET_IF_EXISTS")
            for (rid, _), v in zip(todo, vecs):
                script(keys=[MEM + rid], args=["embedding", P.pack_vector(v)], client=pipe)
            reembedded = sum(1 for r in pipe.execute() if r == 1)
        remaining = len(missing) - len(todo)
        return {"status": "ok", "message": "index ensured", "reembedded": reembedded, "remaining": remaining}
