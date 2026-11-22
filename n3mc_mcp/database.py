"""Redis layer: connection, index, CRUD, TTL, dedup, hybrid search (spec sections 3.3 - 3.5, 3.8 - 3.16)."""

import re
import sys
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import redis

from . import processor

MEM = "mem:"
DOC = "doc:"
MEM_SHA = "mem:sha:"
DOC_SHA = "docsha:"
INDEX = "n3mc_idx"
TEST_MARKER = "__n3mc_test_db__"

SAVE_WITH_GUARD = """
-- KEYS[1] = guard key, KEYS[2..] = record keys
-- ARGV[1] = guard value, ARGV[2] = ttl_seconds, then per record: <pair count>, <field>, <value>, ...
local ok = redis.call('SET', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2])
if not ok then
  return 0
end
local i = 3
for k = 2, #KEYS do
  local n = tonumber(ARGV[i])
  i = i + 1
  local args = {}
  for j = 1, n do
    args[#args + 1] = ARGV[i]
    args[#args + 1] = ARGV[i + 1]
    i = i + 2
  end
  redis.call('HSET', KEYS[k], unpack(args))
  redis.call('EXPIRE', KEYS[k], ARGV[2])
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

_SEARCH_FIELDS = ["content", "timestamp", "timestamp_epoch", "owner_id", "agent_name", "session_id",
                  "turn_id", "importance", "access_count", "parent_id"]


class BackendUnreachable(Exception):
    """Redis (or RediSearch) cannot be used; str(exc) is the recovery hint."""


def _hostport(redis_url: str) -> str:
    try:
        u = urlparse(redis_url)
        host = u.hostname or "localhost"
        port = u.port or 6379
        return "%s:%s" % (host, port)
    except Exception:  # noqa: BLE001
        return "localhost:6379"


def backend_hint(redis_url: str) -> str:
    return ("Redis Stack is not reachable at %s. If Docker itself is not running, start Docker "
            "(Docker Desktop) first. Then run: docker start redis-stack (first time: docker run -d "
            "--name redis-stack -p 127.0.0.1:6379:6379 redis/redis-stack-server:latest). The memory "
            "server reconnects automatically — no restart needed; just retry." % _hostport(redis_url))


def redis_display(redis_url: str) -> str:
    """host:port/db without user name or password."""
    try:
        u = urlparse(redis_url)
        db = (u.path or "/0").lstrip("/") or "0"
        return "%s:%s/%s" % (u.hostname or "localhost", u.port or 6379, db)
    except Exception:  # noqa: BLE001
        return redis_url


def _text(v):
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v if v is not None else ""


def _bare(key) -> str:
    key = _text(key)
    return key[len(MEM):] if key.startswith(MEM) else key


def _new_id() -> str:
    import uuid_utils
    return str(uuid_utils.uuid7())


def _iso_epoch():
    now = datetime.now(timezone.utc)
    return now.isoformat(), now.timestamp()


def guard_key_for(record_key_prefix: str, content: str, turn_id: str):
    """Guard key for a stored record: hook records (turn_id and a role prefix) are guarded per turn."""
    sha = processor.sha1_hex(content)
    base = MEM_SHA if record_key_prefix == MEM else DOC_SHA
    if turn_id and (content.startswith("[user") or content.startswith("[claude")):
        return "%s%s:%s" % (base, turn_id, sha)
    return base + sha


class Database:
    def __init__(self, cfg, default_session=None):
        self.cfg = cfg
        self.default_session = default_session
        self.r = None
        self.index_error = None
        self._lock = threading.RLock()
        self._nlock = threading.Lock()
        self._down_logged = False
        self._scripts = {}

    # ------------------------------------------------------------------ connection
    def add_notice(self, message: str):
        with self._nlock:
            if message not in self.cfg["_notices"]:
                self.cfg["_notices"].append(message)
                try:
                    print(message, file=sys.stderr)
                except Exception:  # noqa: BLE001
                    pass

    def _build_client(self):
        old = self.r
        if old is not None:
            try:
                old.close()
            except Exception:  # noqa: BLE001
                pass
        self.r = redis.from_url(self.cfg["redis_url"], decode_responses=False, protocol=2,
                                socket_connect_timeout=2, socket_timeout=5)
        self._scripts = {
            "save": self.r.register_script(SAVE_WITH_GUARD),
            "incr": self.r.register_script(INCR_IF_EXISTS),
            "hset": self.r.register_script(HSET_IF_EXISTS),
        }
        return self.r

    @property
    def client(self):
        return self.r

    def hint(self) -> str:
        return backend_hint(self.cfg["redis_url"])

    def mark_down(self):
        with self._lock:
            if not self._down_logged:
                self._down_logged = True
                try:
                    print("N3MC Lite: lost the connection to Redis", file=sys.stderr)
                except Exception:  # noqa: BLE001
                    pass
            if self.r is not None:
                try:
                    self.r.close()
                except Exception:  # noqa: BLE001
                    pass
            self.r = None

    def ensure_connected(self, enforce=True):
        """PING; (re)connect when needed; then enforce_ephemeral + ensure_index. Raises BackendUnreachable."""
        with self._lock:
            if self.r is not None:
                try:
                    self.r.ping()
                    if self.index_error is None:
                        return "ok"
                except (redis.ConnectionError, redis.TimeoutError):
                    self.mark_down()
                except redis.RedisError:
                    self.mark_down()
            if self.r is None:
                try:
                    self._build_client()
                    self.r.ping()
                except (redis.RedisError, OSError):
                    try:
                        self.r.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self.r = None
                    if not self._down_logged:
                        self._down_logged = True
                        print("N3MC Lite: Redis is not reachable at %s" % _hostport(self.cfg["redis_url"]),
                              file=sys.stderr)
                    raise BackendUnreachable(self.hint())
                if self._down_logged:
                    self._down_logged = False
                    print("N3MC Lite: reconnected to Redis", file=sys.stderr)
                if enforce:
                    self.enforce_ephemeral()
            try:
                self.ensure_index()
            except (redis.ConnectionError, redis.TimeoutError):
                self.mark_down()
                raise BackendUnreachable(self.hint())
            if self.index_error:
                raise BackendUnreachable(
                    "The Redis at %s does not provide RediSearch (FT.CREATE failed: %s). N3MC Lite needs "
                    "Redis Stack, not plain Redis: docker run -d --name redis-stack -p 127.0.0.1:6379:6379 "
                    "redis/redis-stack-server:latest" % (_hostport(self.cfg["redis_url"]), self.index_error))
        return "ok"

    # ------------------------------------------------------------------ ephemeral + index
    def enforce_ephemeral(self):
        r = self.r
        url = _hostport(self.cfg["redis_url"])
        shared = ("shared_redis: the Redis at %s also holds keys that are not N3MC's, so its persistence "
                  "settings were left unchanged. N3MC memories in it may survive a Redis restart. Use a "
                  "dedicated container (docker run -d --name redis-stack -p 127.0.0.1:6379:6379 "
                  "redis/redis-stack-server:latest) for Lite." % url)
        try:
            try:
                db = int((urlparse(self.cfg["redis_url"]).path or "/0").lstrip("/") or 0)
            except ValueError:
                db = 0
            other = False
            try:
                info = r.info("keyspace")
                for name, val in info.items():
                    m = re.match(r"db(\d+)$", str(name))
                    if m and int(m.group(1)) != db:
                        keys = val.get("keys", 0) if isinstance(val, dict) else 0
                        if int(keys) > 0:
                            other = True
            except redis.ResponseError:
                other = True
            if not other:
                for key in r.scan_iter(count=1000):
                    k = _text(key)
                    if not (k.startswith(MEM) or k.startswith(DOC) or k.startswith(DOC_SHA)):
                        other = True
                        break
            if other:
                self.add_notice(shared)
                return
            try:
                r.config_set("appendonly", "no")
                r.config_set("save", "")
            except redis.ResponseError as exc:
                print("N3MC Lite: CONFIG SET was refused (%s)" % exc, file=sys.stderr)
                self.add_notice("config_refused: the Redis at %s refused CONFIG SET, so its persistence "
                                "settings could not be turned off. N3MC memories in it may survive a Redis "
                                "restart. Use a dedicated container (docker run -d --name redis-stack "
                                "-p 127.0.0.1:6379:6379 redis/redis-stack-server:latest) for Lite." % url)
        except (redis.ConnectionError, redis.TimeoutError):
            raise

    def ensure_index(self):
        r = self.r
        dim = self.cfg["embedding_dim"]
        try:
            r.execute_command(
                "FT.CREATE", INDEX, "ON", "HASH", "PREFIX", "1", MEM, "SCHEMA",
                "content", "TEXT", "WEIGHT", "1.0",
                "content_ngram", "TEXT", "WEIGHT", "0.8",
                "timestamp_epoch", "NUMERIC", "SORTABLE",
                "owner_id", "TAG", "local_id", "TAG", "agent_name", "TAG", "session_id", "TAG",
                "turn_id", "TAG", "importance", "NUMERIC", "access_count", "NUMERIC", "parent_id", "TAG",
                "embedding", "VECTOR", "FLAT", "6", "TYPE", "FLOAT32", "DIM", str(dim),
                "DISTANCE_METRIC", "COSINE")
        except redis.ResponseError as exc:
            if "already exists" in str(exc).lower():
                self.index_error = None
                self._check_dim()
                return
            self.index_error = str(exc)
            return
        self.index_error = None

    def _check_dim(self):
        try:
            info = self.r.execute_command("FT.INFO", INDEX)
        except redis.ResponseError:
            return
        flat = []

        def walk(x):
            if isinstance(x, (list, tuple)):
                for y in x:
                    walk(y)
            else:
                flat.append(_text(x) if isinstance(x, bytes) else x)

        walk(info)
        old = None
        for i, v in enumerate(flat[:-1]):
            if isinstance(v, str) and v.lower() == "dim":
                old = flat[i + 1]
                break
        try:
            if old is not None and int(old) != int(self.cfg["embedding_dim"]):
                self.add_notice(
                    "index_dim_mismatch: the existing index n3mc_idx was built for %s dimensions but "
                    "embedding_dim is %s; new vectors are not indexed. Drop the index (FT.DROPINDEX "
                    "n3mc_idx) or flush the test DB, then restart." % (old, self.cfg["embedding_dim"]))
        except (TypeError, ValueError):
            pass

    # ------------------------------------------------------------------ save
    def _fields(self, rec_id, content, agent_name, session_id, turn_id, importance, parent_id, vec,
                ts_iso, ts_epoch):
        f = {
            "id": rec_id, "content": content, "content_ngram": processor.ngram_text(content),
            "timestamp": ts_iso, "timestamp_epoch": repr(ts_epoch),
            "owner_id": self.cfg["owner_id"], "local_id": self.cfg["local_id"],
            "agent_name": agent_name, "session_id": session_id, "turn_id": turn_id,
            "importance": repr(float(importance)), "access_count": "0", "parent_id": parent_id,
        }
        if vec is not None:
            f["embedding"] = processor.pack_vector(vec)
        return f

    @staticmethod
    def _flat(fields: dict):
        out = []
        for k, v in fields.items():
            out.append(k)
            out.append(v if isinstance(v, bytes) else str(v).encode("utf-8"))
        return out

    def _run_save(self, guard_key, guard_value, ttl, records):
        """records: list of (key, fields dict). One Lua script: guard NX + all HSET/EXPIRE."""
        keys = [guard_key] + [k for k, _ in records]
        args = [guard_value, str(ttl)]
        for _, fields in records:
            flat = self._flat(fields)
            args.append(str(len(flat) // 2))
            args.extend(flat)
        try:
            return int(self._scripts["save"](keys=keys, args=args))
        except (redis.ConnectionError, redis.TimeoutError):
            raise
        except Exception:
            try:
                if _text(self.r.get(guard_key)) == guard_value and not self.r.exists(records[0][0]):
                    self.r.delete(guard_key)
            except Exception:  # noqa: BLE001
                pass
            raise

    def save_memory(self, content, agent_name="", session_id="", turn_id="", importance=1.0,
                    role=None, embed=True, no_model_wait=False, owner_id=None):
        """Save one memory (spec 3.8, 3.11, 3.13, 3.16). Returns the result dict of section 4.3."""
        cfg = self.cfg
        for name, val in (("agent_name", agent_name), ("session_id", session_id), ("turn_id", turn_id)):
            if not isinstance(val, str):
                return {"status": "error", "saved": False, "reason": "%s must be a string" % name}
        if not session_id and self.default_session:
            session_id = self.default_session
        content = processor.sanitize_surrogates(content)
        agent_name = processor.sanitize_surrogates(agent_name)
        session_id = processor.sanitize_surrogates(session_id)
        turn_id = processor.sanitize_surrogates(turn_id)
        if not isinstance(content, str):
            return {"status": "error", "saved": False, "reason": "content must be a string"}
        content = content.strip()
        if not content:
            return {"status": "error", "saved": False, "reason": "empty content"}
        if not processor.has_visible_text(content):
            return {"status": "error", "saved": False, "reason": "empty content (no visible characters)"}
        if cfg["skip_code_blocks"] and "```" in content:
            return {"status": "skipped_code", "saved": False}
        body = processor.purify(content)
        ttl = cfg["ttl_seconds"]
        ts_iso, ts_epoch = _iso_epoch()
        prefix = "[%s] " % role if role else ""
        warn_novec = False

        def vec_of(text):
            nonlocal warn_novec
            if not embed:
                warn_novec = True
                return None
            v = processor.embed(text, cfg, query=False, no_wait=no_model_wait)
            if v is None:
                warn_novec = True
            return v

        if len(body) <= cfg["chunk_threshold"]:
            stored = prefix + body
            guard = guard_key_for(MEM, stored, turn_id) if role else MEM_SHA + processor.sha1_hex(stored)
            if self.r.exists(guard):
                return {"status": "duplicate", "saved": False}
            vec = vec_of(stored)
            rec_id = _new_id()
            fields = self._fields(rec_id, stored, agent_name, session_id, turn_id, importance, "", vec,
                                  ts_iso, ts_epoch)
            ok = self._run_save(guard, rec_id, ttl, [(MEM + rec_id, fields)])
            if not ok:
                return {"status": "duplicate", "saved": False}
            res = {"status": "saved", "saved": True, "id": rec_id, "ttl_seconds": ttl}
        else:
            parent_content = prefix + body
            guard = (guard_key_for(DOC, parent_content, turn_id) if role
                     else DOC_SHA + processor.sha1_hex(parent_content))
            existing = self.r.get(guard)
            if existing is not None:
                return {"status": "duplicate", "saved": False, "parent_id": _text(existing)}
            chunks = processor.chunk_text(body, cfg["chunk_threshold"], cfg["chunk_overlap"])
            n = len(chunks)
            texts = []
            for i, ch in enumerate(chunks, 1):
                if role:
                    texts.append(("[%s] %s" % (role, ch)) if n == 1 else ("[%s %d/%d] %s" % (role, i, n, ch)))
                else:
                    texts.append(ch)
            vecs = [vec_of(t) for t in texts]
            parent_id = _new_id()
            chunk_ids = [_new_id() for _ in texts]
            records = []
            pf = {
                "id": parent_id, "content": parent_content, "timestamp": ts_iso,
                "timestamp_epoch": repr(ts_epoch), "owner_id": cfg["owner_id"], "local_id": cfg["local_id"],
                "agent_name": agent_name, "session_id": session_id, "turn_id": turn_id,
                "chunk_count": str(n), "chunk_ids": " ".join(chunk_ids),
            }
            records.append((DOC + parent_id, pf))
            for cid, t, v in zip(chunk_ids, texts, vecs):
                records.append((MEM + cid, self._fields(cid, t, agent_name, session_id, turn_id,
                                                        importance, parent_id, v, ts_iso, ts_epoch)))
            ok = self._run_save(guard, parent_id, ttl, records)
            if not ok:
                ex = self.r.get(guard)
                d = {"status": "duplicate", "saved": False}
                if ex is not None:
                    d["parent_id"] = _text(ex)
                return d
            res = {"status": "saved", "saved": True, "parent_id": parent_id, "chunks": n,
                   "saved_count": n, "ids": chunk_ids, "ttl_seconds": ttl}
        if warn_novec:
            res["warning"] = "embedding_unavailable"
        return res

    # ------------------------------------------------------------------ search
    @staticmethod
    def _epoch_bounds(since, until):
        if since is None and until is None:
            return ""
        lo = "-inf" if since is None else repr(float(since))
        hi = "+inf" if until is None else repr(float(until))
        return "@timestamp_epoch:[%s %s]" % (lo, hi)

    @staticmethod
    def _parse_reply(reply, withscores):
        out = []
        if isinstance(reply, dict):
            for item in reply.get(b"results", reply.get("results", [])):
                ex = item.get(b"extra_attributes", item.get("extra_attributes", {}))
                fields = {_text(k): v for k, v in ex.items()}
                score = item.get(b"score", item.get("score"))
                out.append((_text(item.get(b"id", item.get("id"))), score, fields))
            return out
        if not reply:
            return out
        i = 1
        while i < len(reply):
            key = _text(reply[i])
            i += 1
            score = None
            if withscores:
                score = reply[i]
                i += 1
            fl = reply[i] if i < len(reply) else []
            i += 1
            fields = {}
            if isinstance(fl, (list, tuple)):
                for j in range(0, len(fl) - 1, 2):
                    fields[_text(fl[j])] = fl[j + 1]
            out.append((key, score, fields))
        return out

    def _row(self, key, fields):
        g = lambda n: _text(fields.get(n, b""))  # noqa: E731
        d = {
            "id": _bare(key), "content": g("content"), "timestamp": g("timestamp"),
            "timestamp_epoch": g("timestamp_epoch"), "owner_id": g("owner_id"),
            "agent_name": g("agent_name"), "session_id": g("session_id"), "turn_id": g("turn_id"),
            "importance": g("importance") or "1.0", "access_count": g("access_count") or "0",
            "parent_id": g("parent_id"),
        }
        return d

    def _vector_search(self, vec, n, bounds):
        pre = bounds if bounds else "*"
        q = "%s=>[KNN %d @embedding $vec AS __dist]" % (pre, n)
        ret = _SEARCH_FIELDS + ["__dist"]
        try:
            reply = self.r.execute_command(
                "FT.SEARCH", INDEX, q, "RETURN", str(len(ret)), *ret, "LIMIT", "0", str(n),
                "DIALECT", "2", "PARAMS", "2", "vec", processor.pack_vector(vec))
        except (redis.ConnectionError, redis.TimeoutError):
            raise
        except redis.ResponseError as exc:
            print("N3MC Lite: vector search failed (%s)" % exc, file=sys.stderr)
            return []
        rows = []
        for key, _, fields in self._parse_reply(reply, False):
            row = self._row(key, fields)
            if row["owner_id"] != self.cfg["owner_id"]:
                continue
            row["cos"] = processor.cos_sim_from_distance(_text(fields.get("__dist", b"1")))
            rows.append(row)
        return rows

    def _bm25_search(self, query, n, bounds):
        terms = processor.drop_lone_cjk_tokens(processor.prepare_query(query)).split()
        if not terms:
            return []
        q = "|".join(terms)
        fts = "(@content:(%s) | @content_ngram:(%s))" % (q, q)
        if bounds:
            fts = "(%s) %s" % (fts, bounds)
        ret = _SEARCH_FIELDS
        reply = None
        for scorer in ("BM25", "BM25STD", None):
            args = ["FT.SEARCH", INDEX, fts, "WITHSCORES"]
            if scorer:
                args += ["SCORER", scorer]
            args += ["RETURN", str(len(ret)), *ret, "LIMIT", "0", str(n), "DIALECT", "2"]
            try:
                reply = self.r.execute_command(*args)
                break
            except (redis.ConnectionError, redis.TimeoutError):
                raise
            except redis.ResponseError as exc:
                if scorer is not None and "scorer" in str(exc).lower():
                    continue
                print("N3MC Lite: keyword search failed (%s)" % exc, file=sys.stderr)
                return []
        rows = []
        for key, score, fields in self._parse_reply(reply, True):
            row = self._row(key, fields)
            if row["owner_id"] != self.cfg["owner_id"]:
                continue
            try:
                row["bm25"] = float(_text(score))
            except (TypeError, ValueError):
                row["bm25"] = 0.0
            rows.append(row)
        return rows

    def _parents(self, pids):
        if not pids:
            return {}
        pipe = self.r.pipeline(transaction=False)
        for pid in pids:
            pipe.hmget(DOC + pid, "content", "chunk_count", "chunk_ids", "turn_id")
        out = {}
        for pid, vals in zip(pids, pipe.execute()):
            if vals and vals[0] is not None:
                try:
                    cc = int(_text(vals[1]) or 0)
                except ValueError:
                    cc = 0
                out[pid] = {"content": _text(vals[0]), "chunk_count": cc,
                            "chunk_ids": _text(vals[2]).split(), "turn_id": _text(vals[3])}
        return out

    def _repr(self, row):
        rid = row["id"]
        if row.get("_is_parent"):
            ids = row.get("chunk_ids") or []
            if not ids:
                return None
            rid = ids[0]
        v, t = self.r.hmget(MEM + rid, "embedding", "content")
        if v is None or t is None:
            return None
        return processor.unpack_vector(v), _text(t)

    def search(self, query, effective_session=None, limit=None, since=None, until=None, no_model_wait=False):
        """Hybrid search (3.14). Returns (rows, weak_rows, message); message is the model-unavailable text or ""."""
        cfg = self.cfg
        if effective_session is None:
            effective_session = self.default_session or ""
        if limit is None:
            limit = cfg["search_result_limit"]
        bounds = self._epoch_bounds(since, until)
        n = min(limit * 5, 100)
        model_missing = False
        vec = processor.embed(query, cfg, query=True, no_wait=no_model_wait)
        if vec is None:
            model_missing = True
        merged = {}
        if vec is not None:
            for r in self._vector_search(vec, n, bounds):
                merged[r["id"]] = r
        for r in self._bm25_search(query, n, bounds):
            if r["id"] in merged:
                merged[r["id"]]["bm25"] = r["bm25"]
            else:
                r["cos"] = None
                merged[r["id"]] = r
        for r in merged.values():
            r.setdefault("bm25", None)
        rows = list(merged.values())
        main, weak = processor.build_results(rows, query, cfg, effective_session, limit,
                                             self._parents, self._repr)
        self._count_access(main[:max(0, cfg["ttl_refresh_top_k"])])
        return main[:limit], weak, (processor.model_unavailable_message(cfg) if model_missing else "")

    def _count_access(self, rows):
        """access_count +1 on the matched chunk of ordinary rows (3.14 step 7). Never called for weak rows.

        A search changes no TTL (decided 2026-11-22, 3.3): every record is deleted 7 days after it was saved."""
        if not rows:
            return
        pipe = self.r.pipeline(transaction=False)
        for row in rows:
            self._scripts["incr"](keys=[MEM + row["_chunk_id"]], args=["access_count", 1], client=pipe)
        pipe.execute()

    # ------------------------------------------------------------------ list / thread
    def _scan(self, pattern):
        for key in self.r.scan_iter(match=pattern, count=1000):
            k = _text(key)
            if pattern == "mem:*" and k.startswith(MEM_SHA):
                continue
            if pattern == "doc:*" and k.startswith(DOC_SHA):
                continue
            yield k

    def list_entries(self, limit):
        """Newest first: singles (paged FT.SEARCH `*`) merged with parent documents (SCAN doc:*)."""
        owner = self.cfg["owner_id"]
        entries = []
        singles = 0
        offset = 0
        while singles < limit:
            reply = self.r.execute_command(
                "FT.SEARCH", INDEX, "*", "RETURN", "5", "content", "timestamp", "owner_id", "parent_id",
                "turn_id", "SORTBY", "timestamp_epoch", "DESC", "LIMIT", str(offset), "200", "DIALECT", "2")
            got = self._parse_reply(reply, False)
            if not got:
                break
            for key, _, f in got:
                if _text(f.get("owner_id", b"")) != owner or _text(f.get("parent_id", b"")):
                    continue
                entries.append({"tag": "", "id": _bare(key), "timestamp": _text(f.get("timestamp", b"")),
                                "content": _text(f.get("content", b""))})
                singles += 1
                if singles >= limit:
                    break
            offset += 200
            if len(got) < 200:
                break
        for k in self._scan("doc:*"):
            f = self.r.hmget(k, "id", "content", "timestamp", "owner_id", "chunk_count")
            if f[0] is None or _text(f[3]) != owner:
                continue
            entries.append({"tag": "[doc×%s]" % (_text(f[4]) or "0"), "id": _text(f[0]),
                            "timestamp": _text(f[2]), "content": _text(f[1]), "parent": True})
        entries.sort(key=lambda e: e["timestamp"], reverse=True)
        return entries[:limit]

    def thread_entries(self):
        """All display entries of this owner for recall_thread (chronological)."""
        owner = self.cfg["owner_id"]
        out = []
        keys_mem = list(self._scan("mem:*"))
        keys_doc = list(self._scan("doc:*"))
        pipe = self.r.pipeline(transaction=False)
        for k in keys_doc:
            pipe.hmget(k, "id", "content", "timestamp", "timestamp_epoch", "owner_id", "turn_id",
                       "chunk_count")
        docs = pipe.execute()
        doc_ids = set()
        for k, f in zip(keys_doc, docs):
            if f[0] is None or _text(f[4]) != owner:
                continue
            doc_ids.add(_text(f[0]))
            out.append({"tag": "[doc×%s]" % (_text(f[6]) or "0"), "id": _text(f[0]),
                        "timestamp": _text(f[2]), "epoch": _text(f[3]), "turn_id": _text(f[5]),
                        "content": _text(f[1])})
        pipe = self.r.pipeline(transaction=False)
        for k in keys_mem:
            pipe.hmget(k, "id", "content", "timestamp", "timestamp_epoch", "owner_id", "turn_id",
                       "parent_id")
        for f in pipe.execute():
            if f[0] is None or _text(f[4]) != owner:
                continue
            pid = _text(f[6])
            if pid and pid in doc_ids:
                continue
            if pid and self.r.exists(DOC + pid):
                continue
            out.append({"tag": "", "id": _text(f[0]), "timestamp": _text(f[2]), "epoch": _text(f[3]),
                        "turn_id": _text(f[5]), "content": _text(f[1])})

        def key(e):
            try:
                ep = float(e["epoch"])
            except ValueError:
                ep = 0.0
            return (ep, e["id"])

        out.sort(key=key)
        return out

    # ------------------------------------------------------------------ delete
    def _parent_chunk_ids(self, pid, chunk_ids_field):
        ids = chunk_ids_field.split() if chunk_ids_field else []
        if ids:
            return ids
        found = []
        try:
            reply = self.r.execute_command("FT.SEARCH", INDEX, "@parent_id:{%s}" % pid.replace("-", "\\-"),
                                           "RETURN", "1", "parent_id", "LIMIT", "0", "1000", "DIALECT", "2")
            for key, _, f in self._parse_reply(reply, False):
                if _text(f.get("parent_id", b"")) == pid:
                    found.append(_bare(key))
        except redis.ResponseError:
            found = []
        if not found:
            for k in self._scan("mem:*"):
                if _text(self.r.hget(k, "parent_id")) == pid:
                    found.append(_bare(k))
        return found

    def delete_memory(self, ident: str):
        ident = ident.strip()
        for p in (DOC, MEM):
            if ident.startswith(p):
                ident = ident[len(p):]
                break
        owner = self.cfg["owner_id"]
        r = self.r
        if r.exists(DOC + ident):
            f = r.hmget(DOC + ident, "owner_id", "content", "chunk_ids", "turn_id")
            if _text(f[0]) != owner:
                return {"status": "not_found", "id": ident}
            ids = self._parent_chunk_ids(ident, _text(f[2]))
            pipe = r.pipeline(transaction=True)
            pipe.delete(DOC + ident)
            pipe.delete(guard_key_for(DOC, _text(f[1]), _text(f[3])))
            for c in ids:
                pipe.delete(MEM + c)
            pipe.execute()
            return {"status": "deleted", "id": ident, "chunks_deleted": len(ids)}
        if r.exists(MEM + ident):
            f = r.hmget(MEM + ident, "owner_id", "content", "turn_id")
            if _text(f[0]) != owner:
                return {"status": "not_found", "id": ident}
            pipe = r.pipeline(transaction=True)
            pipe.delete(MEM + ident)
            pipe.delete(guard_key_for(MEM, _text(f[1]), _text(f[2])))
            pipe.execute()
            return {"status": "deleted", "id": ident}
        return {"status": "not_found", "id": ident}

    def delete_by_session(self, session_id: str):
        owner = self.cfg["owner_id"]
        r = self.r
        docs, chunks, singles = 0, 0, 0
        pipe = r.pipeline(transaction=True)
        deleted_chunk_ids = set()
        doc_ids = {}
        for k in self._scan("doc:*"):
            f = r.hmget(k, "id", "owner_id", "session_id", "content", "chunk_ids", "turn_id")
            if f[0] is None or _text(f[1]) != owner:
                continue
            doc_ids[_text(f[0])] = _text(f[2]) == session_id
            if _text(f[2]) != session_id:
                continue
            pid = _text(f[0])
            ids = self._parent_chunk_ids(pid, _text(f[4]))
            pipe.delete(k)
            pipe.delete(guard_key_for(DOC, _text(f[3]), _text(f[5])))
            for c in ids:
                pipe.delete(MEM + c)
                deleted_chunk_ids.add(c)
            docs += 1
            chunks += len(ids)
        for k in self._scan("mem:*"):
            bare = _bare(k)
            if bare in deleted_chunk_ids:
                continue
            f = r.hmget(k, "owner_id", "session_id", "content", "turn_id", "parent_id")
            if f[0] is None or _text(f[0]) != owner or _text(f[1]) != session_id:
                continue
            pipe.delete(k)
            if _text(f[4]):
                chunks += 1          # an orphan chunk (its parent is gone)
            else:
                pipe.delete(guard_key_for(MEM, _text(f[2]), _text(f[3])))
                singles += 1
        pipe.execute()
        total = docs + chunks + singles
        if total == 0:
            return {"status": "not_found", "session_id": session_id, "deleted": 0}
        return {"status": "deleted", "session_id": session_id, "documents_deleted": docs,
                "chunks_deleted": chunks, "singles_deleted": singles, "deleted": total}

    # ------------------------------------------------------------------ repair
    def repair(self, max_n=200, no_model_wait=False):
        self.ensure_index()
        if self.index_error:
            return {"status": "error", "message": "FT.CREATE failed: %s" % self.index_error}
        res = {"status": "ok", "message": "index ensured", "reembedded": 0, "remaining": 0}
        owner = self.cfg["owner_id"]
        todo = []
        for k in self._scan("mem:*"):
            f = self.r.hmget(k, "owner_id", "content", "embedding")
            if f[0] is None or _text(f[0]) != owner or f[2] is not None:
                continue
            todo.append((k, _text(f[1])))
        unavailable = False
        done = 0
        for k, content in todo:
            if done >= max_n:
                break
            vec = processor.embed(content, self.cfg, query=False, no_wait=no_model_wait)
            if vec is None:
                unavailable = True
                break
            ok = self._scripts["hset"](keys=[k], args=["embedding", processor.pack_vector(vec)])
            if int(ok):
                done += 1
        res["reembedded"] = done
        res["remaining"] = len(todo) - done
        if unavailable:
            res["warning"] = "embedding_unavailable"
            res["detail"] = processor.model_unavailable_message(self.cfg)
        return res

    def close(self):
        if self.r is not None:
            try:
                self.r.close()
            except Exception:  # noqa: BLE001
                pass
            self.r = None
