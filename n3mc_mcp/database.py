"""
Redis layer: index management, CRUD, TTL enforcement, deduplication, search.

Key spec sections:
  §3.3  Redis connection + pipeline atomicity
  §3.4  Ephemerality enforcement
  §3.5  Data layout (schema)
  §3.6  Ranking formula
  §3.8  Duplicate rejection
  §3.9  Startup sequence
  §3.10 Repair
  §3.11 Parent-document + chunks (verbatim recall)
  §3.12 UUID TAG query constraint → Python-side owner/parent filtering
"""
import hashlib
import re
import sys
import uuid
from datetime import datetime, timezone
from typing import Optional

from urllib.parse import urlparse
import redis as redis_lib

from .config import config_unreadable_detail

from .processor import (
    apply_vector_floor,
    b_local,
    chunk_text,
    content_ngram,
    cosine_sim_from_distance,
    drop_lone_cjk_tokens,
    embed,
    has_visible_text,
    model_unavailable_message,
    filter_score,
    final_score,
    judged_score,
    keyword_relevance,
    lexical_rerank,
    normalize_text,
    order_versions_newest_first,
    prepare_query,
    relative_cut,
    sanitize_surrogates,
    time_decay,
)

INDEX_NAME = "n3mc_idx"

_CONN_ERRORS = (redis_lib.ConnectionError, redis_lib.TimeoutError)


_DEDICATED_CONTAINER = (
    "docker run -d --name redis-stack -p 6379:6379 redis/redis-stack-server:latest"
)


def _where(redis_url: str) -> str:
    """host:port of *redis_url* WITHOUT the user name or password."""
    try:
        u = urlparse(redis_url or "")
        return f"{u.hostname or 'localhost'}:{u.port or 6379}"
    except Exception:
        return "the configured redis_url"


def backend_hint(redis_url: str = "redis://localhost:6379/0") -> str:
    """Recovery hint for an unreachable Redis (spec §3.9, English).

    Shows host:port from *redis_url* WITHOUT the user name or password.
    """
    where = _where(redis_url)
    return (
        f"Redis Stack is not reachable at {where}. If Docker itself is not "
        "running, start Docker (Docker Desktop) first. Then run: "
        "docker start redis-stack (first time: docker run -d --name redis-stack "
        "-p 6379:6379 redis/redis-stack-server:latest). The MCP server "
        "reconnects automatically \u2014 no restart needed; just retry."
    )


# Backward-compatible constant (default URL).
_DOCKER_HINT = backend_hint()

_CODE_FENCE_RE = re.compile(r"```")


# ── helpers ──────────────────────────────────────────────────────────────────

def _sha1(text: str) -> str:
    """Dedup digest — computed on NFKC-normalized text.

    Folding to NFKC before hashing makes exact-dedup catch half/full-width
    variants (`カフェ` ↔ `ｶﾌｪ`, `ABC` ↔ `ＡＢＣ`) that are perceptually
    identical but byte-different. The raw `content` field on the hash is
    still stored unchanged for verbatim recall.
    """
    return hashlib.sha1(normalize_text(text).encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_epoch() -> float:
    return datetime.now(timezone.utc).timestamp()


def _new_id() -> str:
    try:
        from uuid_utils import uuid7
        return str(uuid7())
    except ImportError:
        return str(uuid.uuid4())


def _parse_fields(flist) -> dict[str, str]:
    """Decode FT.SEARCH field-value pairs — handles RESP2 list and RESP3 dict.

    Older Redis Stack (RESP2): flist is a flat list [field, val, field, val, …].
    Newer Redis Stack or redis-py ≥5.x may return a Python dict directly.
    Both are decoded to the same {str: str} output.
    """
    if not flist:
        return {}
    # RESP3 / auto-parsed: already a Python dict
    if isinstance(flist, dict):
        return {
            k.decode("utf-8") if isinstance(k, bytes) else str(k):
            v.decode("utf-8") if isinstance(v, bytes) else str(v)
            for k, v in flist.items()
        }
    # RESP2 flat alternating list
    result: dict[str, str] = {}
    for j in range(0, len(flist) - 1, 2):
        k = flist[j].decode("utf-8") if isinstance(flist[j], bytes) else str(flist[j])
        v = flist[j + 1].decode("utf-8") if isinstance(flist[j + 1], bytes) else str(flist[j + 1])
        result[k] = v
    return result


def _to_float(v, default: float = 1.0) -> float:
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _to_int(v, default: int = 0) -> int:
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return default


def _contains_code_block(text: str) -> bool:
    return bool(_CODE_FENCE_RE.search(text))


class TimeBoundError(ValueError):
    """since / until cannot be used (spec §4.3.1, v1.7.1). str(e) is the
    text after `Error: `."""


# Strict ISO 8601 subset (spec §4.3.1): date, or date + `T`/space + time,
# optional fraction, optional `Z` / ±hh:mm / ±hhmm offset.
_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}"
    r"(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|z|[+-]\d{2}:?\d{2})?)?$"
)
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Test hook: a fixed tzinfo for offset-less values. None = the host's local
# time zone (with its DST rules), via naive datetime.timestamp().
_LOCAL_TZ = None


def _parse_time_bound(ts: str, is_until: bool = False) -> float:
    """Parse an ISO 8601 date or datetime to epoch seconds (spec §4.3.1, v1.7.1).

    A date-only value or a datetime without an offset is read in the server's
    LOCAL time zone (the same clock as `saved=`); a value with an offset is
    used as given. Date-only `until` means 23:59:59.999999. Raises ValueError
    when the value is not a valid ISO 8601 date / datetime.
    """
    s = ts.strip()
    if not _ISO_RE.match(s):
        raise ValueError(s)
    if _DATE_ONLY_RE.match(s):
        dt = datetime.strptime(s, "%Y-%m-%d")
        if is_until:
            dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    else:
        dt = datetime.fromisoformat(s[:-1] + "+00:00" if s[-1] in "Zz" else s)
    if dt.tzinfo is None:
        if _LOCAL_TZ is not None:
            dt = dt.replace(tzinfo=_LOCAL_TZ)
        # else: naive .timestamp() uses the host's local time zone
    return dt.timestamp()


def _epoch_from_iso(ts: str, is_until: bool = False) -> float:
    """Lenient wrapper (0.0 on failure) for internal sorting only."""
    if not ts:
        return 0.0
    try:
        return _parse_time_bound(ts, is_until)
    except Exception:
        try:
            return datetime.fromisoformat(ts.strip()).timestamp()
        except Exception:
            return 0.0


def _time_bound(name: str, value, is_until: bool):
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    if not value.strip():
        return None  # an empty string means "no bound"
    try:
        return _parse_time_bound(value, is_until)
    except Exception:
        raise TimeBoundError(
            f"{name} must be an ISO 8601 date or datetime, e.g. 2026-11-13 or "
            f"2026-11-13T09:00:00+09:00 (got '{value}')"
        )


def _time_filter_clause(since: str = "", until: str = "") -> str:
    """§4.3.1 since/until → RediSearch NUMERIC pre-filter.

    Returns `@timestamp_epoch:[<since> <until>]` with `-inf` / `+inf` for an
    open bound, or "" when neither bound is given. v1.7.1: an unreadable
    value or since > until raises TimeBoundError (the search is not run).
    Used inside the KNN query (`<clause>=>[KNN ...]`) and the BM25 query
    (`(<text>) <clause>`). NUMERIC filters do not hit the UUID TAG problem
    of §3.12.
    """
    lo = _time_bound("since", since, is_until=False)
    hi = _time_bound("until", until, is_until=True)
    if lo is None and hi is None:
        return ""
    if lo is not None and hi is not None and lo > hi:
        raise TimeBoundError("since is later than until")
    lo_s = repr(float(lo)) if lo is not None else "-inf"
    hi_s = repr(float(hi)) if hi is not None else "+inf"
    return f"@timestamp_epoch:[{lo_s} {hi_s}]"


# ── main class ───────────────────────────────────────────────────────────────

class Database:
    # Spec §3.9 (v1.7.1, LH2): read/write timeout. A Redis that accepted the
    # connection but stopped answering fails within this many seconds.
    SOCKET_TIMEOUT = 5
    SOCKET_CONNECT_TIMEOUT = 2

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self._client: Optional[redis_lib.Redis] = None
        self._ok = False
        # Spec §3.9 step 3 (v1.7.1, LM3): "index missing" state.
        self._index_ok = False
        self._index_error = ""

    # ── §3.9 connection ──────────────────────────────────────────────────────

    def _close_client(self) -> None:
        """Release the old client's connection pool (v1.7.1, LL11)."""
        old, self._client = self._client, None
        if old is None:
            return
        try:
            old.close()
        except Exception:
            pass

    def connect(self, quiet: bool = False) -> bool:
        """Spec §3.9 step 2: connect + PING.  On failure stay up with _ok=False.

        v1.7.1: `socket_timeout=5` next to `socket_connect_timeout=2`, and the
        previous client is closed before it is replaced.
        """
        self._close_client()
        try:
            self._client = redis_lib.Redis.from_url(
                self.cfg["redis_url"], decode_responses=False,
                socket_connect_timeout=self.SOCKET_CONNECT_TIMEOUT,
                socket_timeout=self.SOCKET_TIMEOUT,
            )
            self._client.ping()
            self._ok = True
            return True
        except Exception as e:
            if not quiet:
                print(f"[n3mc] Redis connect failed: {e}", file=sys.stderr)
                print(f"[n3mc] Hint: {self.hint()}", file=sys.stderr)
            self._ok = False
            return False

    def index_hint(self) -> str:
        """Spec §3.9 step 3 (v1.7.1): hint while the index is missing."""
        return (
            f"The Redis at {_where(self.cfg.get('redis_url', ''))} does not provide "
            f"RediSearch (FT.CREATE failed: {self._index_error}). N3MC Lite needs "
            f"Redis Stack, not plain Redis: {_DEDICATED_CONTAINER}"
        )

    def hint(self) -> str:
        if self._ok and not self._index_ok and self._index_error:
            return self.index_hint()
        return backend_hint(self.cfg.get("redis_url", ""))

    def mark_disconnected(self) -> None:
        if self._ok:
            print(f"[n3mc] Redis connection lost. "
                  f"{backend_hint(self.cfg.get('redis_url', ''))}", file=sys.stderr)
        self._ok = False

    def ensure_connected(self) -> bool:
        """Spec §3.9 "Redis availability on every tool call".

        Connected → PING. Not connected or PING failed → rebuild the client
        and PING again. On reconnection re-run enforce_ephemeral() and
        ensure_index(). stderr gets one line per state change only.
        v1.7.1: a read timeout is a lost connection; while the index is
        missing, ensure_index() is retried before every call and the call
        gets the "Redis Stack required" hint (returns False).
        """
        if self._ok and self._client is not None:
            try:
                self._client.ping()
                return self._ensure_index_for_call()
            except Exception:
                self.mark_disconnected()
        if self.connect(quiet=True):
            print("[n3mc] Redis connection restored.", file=sys.stderr)
            try:
                self.enforce_ephemeral()
                self.ensure_index()
            except _CONN_ERRORS:
                self.mark_disconnected()
                return False
            except Exception as e:
                print(f"[n3mc] reconnect setup: {e}", file=sys.stderr)
            return self._ok and self._index_ok
        return False

    def _ensure_index_for_call(self) -> bool:
        if self._index_ok:
            return True
        try:
            self.ensure_index()
        except _CONN_ERRORS:
            self.mark_disconnected()
            return False
        return self._ok and self._index_ok

    def _add_notice(self, message: str) -> None:
        """Add a connection-layer notice to cfg["_notices"] (spec §6), once."""
        notices = self.cfg.setdefault("_notices", [])
        key = message.split(":", 1)[0]
        if any(str(n).split(":", 1)[0] == key for n in notices):
            return
        notices.append(message)
        print(f"[n3mc] {message}", file=sys.stderr)

    # ── §3.4 ephemerality ────────────────────────────────────────────────────

    _N3MC_KEY_PREFIXES = (b"mem:", b"doc:", b"docsha:")   # mem:sha: ⊂ mem:

    def _only_n3mc_keys(self) -> bool:
        """SCAN (COUNT 1000) and stop at the first non-N3MC key (spec §3.4)."""
        cursor = 0
        while True:
            cursor, keys = self._client.scan(cursor, count=1000)
            for k in keys:
                kb = k if isinstance(k, bytes) else str(k).encode()
                if not kb.startswith(self._N3MC_KEY_PREFIXES):
                    return False
            if cursor == 0:
                return True

    def enforce_ephemeral(self) -> None:
        """Turn off RDB/AOF persistence (spec §3.4, v1.7.1).

        Only when the DB is empty or holds nothing but N3MC keys; otherwise
        the configuration is left alone and the notice `shared_redis` is
        added. A refused CONFIG adds `config_refused`. Never fails the
        connection (connection errors propagate to the caller).
        """
        if not self._ok:
            return
        where = _where(self.cfg.get("redis_url", ""))
        if not self._only_n3mc_keys():
            self._add_notice(
                f"shared_redis: the Redis at {where} also holds keys that are "
                "not N3MC's, so its persistence settings were left unchanged. "
                "N3MC memories in it may survive a Redis restart. Use a "
                f"dedicated container ({_DEDICATED_CONTAINER}) for Lite."
            )
            return
        try:
            self._client.config_set("appendonly", "no")
            self._client.config_set("save", "")
        except _CONN_ERRORS:
            raise
        except Exception as e:
            print(f"[n3mc] enforce_ephemeral: CONFIG SET refused: {e}", file=sys.stderr)
            self._add_notice(
                f"config_refused: the Redis at {where} refused CONFIG SET ({e}), "
                "so its persistence (RDB/AOF) may still be on and N3MC memories "
                "may survive a Redis restart. Use a dedicated container "
                f"({_DEDICATED_CONTAINER}) for Lite."
            )

    # ── §3.9 / §3.10 index ──────────────────────────────────────────────────

    def _index_dim(self) -> Optional[int]:
        """DIM of the `embedding` attribute of the existing index (FT.INFO)."""
        info = self._client.execute_command("FT.INFO", INDEX_NAME)

        def dec(x):
            return x.decode("utf-8", "replace") if isinstance(x, bytes) else x

        def as_map(x) -> dict:
            if isinstance(x, dict):
                return {str(dec(k)).lower(): v for k, v in x.items()}
            if isinstance(x, (list, tuple)):
                return {str(dec(x[i])).lower(): x[i + 1]
                        for i in range(0, len(x) - 1, 2)
                        if isinstance(dec(x[i]), str)}
            return {}

        top = as_map(info)
        attrs = top.get("attributes") or top.get("fields") or []
        for attr in attrs:
            m = as_map(attr)
            name = dec(m.get("identifier", m.get("attribute", b"")))
            if name != "embedding":
                continue
            if "dim" in m:
                return int(dec(m["dim"]))
            # Some versions nest the vector parameters.
            for v in m.values():
                sub = as_map(v) if isinstance(v, (list, tuple, dict)) else {}
                if "dim" in sub:
                    return int(dec(sub["dim"]))
        return None

    def ensure_index(self) -> None:
        """Idempotent FT.CREATE (spec §3.5 schema, §3.9 step 3, §3.10 repair).

        v1.7.1: "already exists" is success, followed by a DIM check
        (`index_dim_mismatch` notice). Any other failure does NOT raise: the
        server stays up in the "index missing" state (`_index_ok = False`)
        and ensure_connected() retries before every call. Connection errors
        propagate (a lost connection).
        """
        if not self._ok:
            return
        try:
            self._client.execute_command(
                "FT.CREATE", INDEX_NAME,
                "ON", "HASH", "PREFIX", "1", "mem:",
                "SCHEMA",
                "content",         "TEXT",    "WEIGHT", "1.0",
                "content_ngram",   "TEXT",    "WEIGHT", "0.8",
                "timestamp_epoch", "NUMERIC", "SORTABLE",
                "owner_id",        "TAG",
                "local_id",        "TAG",
                "agent_name",      "TAG",
                "session_id",      "TAG",
                "turn_id",         "TAG",
                "importance",      "NUMERIC",
                "access_count",    "NUMERIC",
                "parent_id",       "TAG",
                "embedding",       "VECTOR", "FLAT", "6",
                                   "TYPE", "FLOAT32",
                                   "DIM", str(self.cfg.get("embedding_dim", 768)),
                                   "DISTANCE_METRIC", "COSINE",
            )
        except _CONN_ERRORS:
            raise
        except Exception as e:
            if "already exists" not in str(e).lower():
                was_ok = self._index_ok or not self._index_error
                self._index_ok = False
                self._index_error = str(e)
                if was_ok:
                    print(f"[n3mc] {self.index_hint()}", file=sys.stderr)
                return
            self._check_index_dim()
        self._index_ok = True
        self._index_error = ""

    def _check_index_dim(self) -> None:
        """LL8: an existing index built for another DIM → notice."""
        want = self.cfg.get("embedding_dim", 768)
        try:
            have = self._index_dim()
        except _CONN_ERRORS:
            raise
        except Exception as e:
            print(f"[n3mc] FT.INFO {INDEX_NAME}: {e}", file=sys.stderr)
            return
        if have is not None and int(have) != int(want):
            self._add_notice(
                f"index_dim_mismatch: the existing index {INDEX_NAME} was built "
                f"for {have} dimensions but embedding_dim is {want}; new vectors "
                f"are not indexed. Drop the index (FT.DROPINDEX {INDEX_NAME}) or "
                "flush the test DB, then restart."
            )

    # ── §3.10 repair ────────────────────────────────────────────────────────

    REEMBED_PER_CALL = 200

    def repair_memory(self) -> dict:
        """ensure_index() + add missing vectors (spec §3.10, v1.7.1).

        Entries saved while the model was unavailable have no `embedding`.
        SCAN mem:* (skipping mem:sha:*), and for up to 200 hashes of this
        owner without `embedding` compute embed(content) (passage prefix) and
        HSET the field — HSET does not change the TTL. `remaining` = entries
        still without a vector after this call.
        """
        if not self._ok:
            return {"status": "error", "message": self.hint()}
        try:
            self.ensure_index()
        except _CONN_ERRORS:
            raise
        except Exception as e:
            return {"status": "error", "message": str(e)}
        if not self._index_ok:
            return {"status": "error", "message": self.index_hint()}

        owner_id = self.cfg["owner_id"]
        reembedded = 0
        remaining = 0
        model_down = False
        cursor = 0
        while True:
            cursor, keys = self._client.scan(cursor, match="mem:*", count=500)
            todo = [k for k in keys
                    if not (k if isinstance(k, bytes) else str(k).encode()).startswith(b"mem:sha:")]
            if todo:
                pipe = self._client.pipeline(transaction=True)
                for k in todo:
                    pipe.hmget(k, b"owner_id", b"content")
                    pipe.hexists(k, b"embedding")
                replies = pipe.execute()
                for j, k in enumerate(todo):
                    (oid, content), has_vec = replies[2 * j], replies[2 * j + 1]
                    if has_vec or not content:
                        continue
                    if (oid.decode() if oid else "") != owner_id:
                        continue
                    if model_down or reembedded >= self.REEMBED_PER_CALL:
                        remaining += 1
                        continue
                    text = content.decode("utf-8", errors="replace")
                    vec = embed(normalize_text(text), is_query=False)
                    if not vec:
                        model_down = True
                        remaining += 1
                        continue
                    # Only if the key still exists (it may have expired).
                    if self._client.exists(k):
                        self._client.hset(k, b"embedding", vec)
                        reembedded += 1
            if cursor == 0:
                break

        result = {"status": "ok", "message": "index ensured",
                  "reembedded": reembedded, "remaining": remaining}
        if model_down:
            result["warning"] = "embedding_unavailable"
            result["detail"] = (
                "The embedding model is not available, so no vectors were added. "
                + (model_unavailable_message() or "")
            ).strip()
        return result

    # ── §3.8 / §3.11 save ───────────────────────────────────────────────────

    def save_memory(
        self,
        content: str,
        agent_name: str = "",
        owner_id: str = "",
        importance: float = 1.0,
        session_id: str = "",
        turn_id: str = "",
    ) -> dict:
        if not self._ok:
            return {"status": "error", "saved": False, "reason": self.hint()}

        # Encoding safety — strip lone UTF-16 surrogates BEFORE strip()
        # so a payload that is all-surrogates (degenerate Windows stdin
        # case) collapses to empty and is rejected with "empty content"
        # rather than blowing up at .encode("utf-8") downstream.
        content = sanitize_surrogates(content).strip()
        if not content:
            return {"status": "error", "saved": False, "reason": "empty content"}
        # §3.13 invisible-only contract: strip() keeps NUL / U+200B / U+FEFF,
        # so a payload made only of those would be saved as a blank memory.
        if not has_visible_text(content):
            return {"status": "error", "saved": False,
                    "reason": "empty content (no visible characters)"}

        # owner_id validation (spec §4.3 save_memory)
        cfg_owner = self.cfg["owner_id"]
        if owner_id and owner_id != cfg_owner:
            return {"status": "error", "saved": False, "reason": "owner_id mismatch"}
        owner_id = cfg_owner

        effective_session = session_id.strip() or self.cfg.get("_session_id", "")
        importance = max(0.5, min(2.0, importance))

        # skip_code_blocks policy (spec §6 / §5 rule 8)
        if self.cfg.get("skip_code_blocks", False) and _contains_code_block(content):
            return {
                "status": "skipped_code",
                "saved": False,
                "reason": "content contains a code fence (```); skip_code_blocks=true",
            }

        threshold = self.cfg.get("chunk_threshold", 400)
        if len(content) <= threshold:
            result = self._save_single(content, agent_name, owner_id, importance, effective_session, turn_id)
        else:
            result = self._save_parent_chunks(content, agent_name, owner_id, importance, effective_session, turn_id)
        return self._add_config_warning(result)

    def _add_config_warning(self, result: dict) -> dict:
        """Spec §6 (v1.7.1, LL9): while config.json is unreadable, every
        save_memory response carries `warning: config_unreadable` + detail.
        If `warning` is already set (embedding_unavailable), both are kept,
        joined with "; " (the spec does not define the combination)."""
        path = self.cfg.get("_config_unreadable")
        if not path or not result.get("saved"):
            return result
        detail = config_unreadable_detail(path)
        if result.get("warning"):
            result["warning"] = f"{result['warning']}; config_unreadable"
            result["detail"] = (
                f"{result['detail']} {detail}" if result.get("detail") else detail
            )
        else:
            result["warning"] = "config_unreadable"
            result["detail"] = detail
        return result

    def _save_single(
        self,
        content: str,
        agent_name: str,
        owner_id: str,
        importance: float,
        session_id: str,
        turn_id: str = "",
    ) -> dict:
        """§3.8 (A): exact SHA dedup → embed → SET NX guard → HSET+EXPIRE.

        v1.7.0: no near-duplicate (similarity) rejection — an updated fact
        with a new value is saved as a new entry; search_memory puts the
        newest version first (§3.14).
        v1.7.1 (LL10): the guard is taken atomically with SET NX EX; the
        EXISTS check only avoids embedding an obvious duplicate. If the
        record write fails, the guard is released (best effort).
        """
        sha = _sha1(content)
        sha_key = f"mem:sha:{sha}"

        if self._client.exists(sha_key):
            return {"status": "duplicate", "saved": False}

        # Embed the NFKC-normalized form so vector recall is invariant to
        # half/full-width and other compat differences. The `content` field
        # below still stores the raw input verbatim for §3.11 recall.
        vec_bytes = embed(normalize_text(content), is_query=False)

        mem_id = _new_id()
        ttl = self.cfg["ttl_seconds"]
        if not self._client.set(sha_key, mem_id.encode(), nx=True, ex=ttl):
            return {"status": "duplicate", "saved": False}
        ngram = content_ngram(content)
        now_iso = _now_iso()
        now_epoch = _now_epoch()

        fields: dict = {
            b"id":              mem_id.encode(),
            b"content":         content.encode("utf-8"),
            b"content_ngram":   ngram.encode("utf-8"),
            b"timestamp":       now_iso.encode(),
            b"timestamp_epoch": str(now_epoch).encode(),
            b"owner_id":        owner_id.encode(),
            b"local_id":        self.cfg.get("local_id", "").encode(),
            b"agent_name":      (agent_name or "").encode(),
            b"session_id":      session_id.encode(),
            b"turn_id":         turn_id.encode(),
            b"importance":      str(importance).encode(),
            b"access_count":    b"0",
            b"parent_id":       b"",
        }
        if vec_bytes:
            fields[b"embedding"] = vec_bytes

        mem_key = f"mem:{mem_id}"
        try:
            pipe = self._client.pipeline(transaction=True)
            pipe.hset(mem_key, mapping=fields)
            pipe.expire(mem_key, ttl)
            pipe.execute()
        except Exception:
            self._release_guard(sha_key)
            raise

        result: dict = {"status": "saved", "saved": True, "id": mem_id, "ttl_seconds": ttl}
        if not vec_bytes:
            self._embedding_warning(result)
        return result

    @staticmethod
    def _embedding_warning(result: dict) -> None:
        """Embedding model unavailable at save time: the record is stored and
        BM25-searchable but not in KNN results until repair_memory (§3.10)."""
        result["warning"] = "embedding_unavailable"
        detail = model_unavailable_message()
        if detail:
            result["detail"] = detail

    def _release_guard(self, key: str) -> None:
        """Best-effort DEL of a guard taken by this save (v1.7.1)."""
        try:
            self._client.delete(key)
        except Exception as e:
            print(f"[n3mc] could not release {key}: {e}", file=sys.stderr)

    def _save_parent_chunks(
        self,
        content: str,
        agent_name: str,
        owner_id: str,
        importance: float,
        session_id: str,
        turn_id: str = "",
    ) -> dict:
        """§3.8 (B) + §3.11: parent-level exact dedup → embed every chunk →
        SET NX docsha guard → ONE pipeline with the parent and all chunks.

        v1.7.0: no parent-level near-duplicate rejection (a re-made document
        with a few changes is saved as a new version).
        v1.7.1 (LM1): nothing is written before every chunk embedding has been
        computed; if the write fails the guard is released, so nothing is
        left half-written and the same body can be saved again.
        """
        sha = _sha1(content)
        docsha_key = f"docsha:{sha}"

        def duplicate() -> Optional[dict]:
            existing = self._client.get(docsha_key)
            if existing is None:
                return None
            parent_id = existing.decode() if isinstance(existing, bytes) else str(existing)
            return {"status": "duplicate", "saved": False, "parent_id": parent_id}

        dup = duplicate()
        if dup:
            return dup

        threshold = self.cfg.get("chunk_threshold", 400)
        overlap   = self.cfg.get("chunk_overlap", 100)
        chunks    = chunk_text(content, threshold, overlap)

        # Step 1: every chunk embedding first — no Redis write yet.
        # Once the model is unavailable for this call (e.g. the 20 s model
        # wait ran out, §3.9 step 4), the remaining chunks are not embedded
        # either, so one call waits at most once.
        vectors: list = []
        for chunk in chunks:
            vec = embed(normalize_text(chunk), is_query=False) if (not vectors or vectors[-1]) else None
            vectors.append(vec)

        parent_id  = _new_id()
        ttl        = self.cfg["ttl_seconds"]
        now_iso    = _now_iso()
        now_epoch  = _now_epoch()
        local_id   = self.cfg.get("local_id", "")

        # Pre-generate chunk ids so they can be recorded on the parent doc
        # (spec §3.5 `chunk_ids` field). This lets search_memory's TTL refresh
        # extend EVERY sibling chunk in O(1) — no SCAN — when the parent is hit,
        # keeping the whole document's chunks alive together (spec §3.11).
        chunk_ids: list[str] = [_new_id() for _ in chunks]

        # Step 2: take the guard atomically.
        if not self._client.set(docsha_key, parent_id.encode(), nx=True, ex=ttl):
            return duplicate() or {"status": "duplicate", "saved": False}

        # Step 3: parent + all chunks in ONE pipeline (MULTI/EXEC).
        doc_key = f"doc:{parent_id}"
        pipe = self._client.pipeline(transaction=True)
        pipe.hset(
            doc_key,
            mapping={
                b"id":              parent_id.encode(),
                b"content":         content.encode("utf-8"),
                b"timestamp":       now_iso.encode(),
                b"timestamp_epoch": str(now_epoch).encode(),
                b"owner_id":        owner_id.encode(),
                b"local_id":        local_id.encode(),
                b"agent_name":      (agent_name or "").encode(),
                b"session_id":      session_id.encode(),
                b"turn_id":         turn_id.encode(),
                b"chunk_count":     str(len(chunks)).encode(),
                b"chunk_ids":       " ".join(chunk_ids).encode(),
            },
        )
        pipe.expire(doc_key, ttl)

        # No per-chunk SHA guards (spec §3.8): sliding-window chunks overlap
        # by design and would otherwise reject each other.
        for chunk, chunk_id, vec_bytes in zip(chunks, chunk_ids, vectors):
            ngram     = content_ngram(chunk)
            fields: dict = {
                b"id":              chunk_id.encode(),
                b"content":         chunk.encode("utf-8"),
                b"content_ngram":   ngram.encode("utf-8"),
                b"timestamp":       now_iso.encode(),
                b"timestamp_epoch": str(now_epoch).encode(),
                b"owner_id":        owner_id.encode(),
                b"local_id":        local_id.encode(),
                b"agent_name":      (agent_name or "").encode(),
                b"session_id":      session_id.encode(),
                b"turn_id":         turn_id.encode(),
                b"importance":      str(importance).encode(),
                b"access_count":    b"0",
                b"parent_id":       parent_id.encode(),
            }
            if vec_bytes:
                fields[b"embedding"] = vec_bytes
            pipe.hset(f"mem:{chunk_id}", mapping=fields)
            pipe.expire(f"mem:{chunk_id}", ttl)
        try:
            pipe.execute()
        except Exception:
            # Step 4: release the guard so the same body can be saved again.
            self._release_guard(docsha_key)
            raise

        result = {
            "status":      "saved",
            "saved":       True,
            "parent_id":   parent_id,
            "chunks":      len(chunks),
            "saved_count": len(chunk_ids),
            "ids":         chunk_ids,
            "ttl_seconds": ttl,
        }
        if not all(vectors):
            self._embedding_warning(result)
        return result

    # ── §3.6 / §3.11 / §3.14 search ─────────────────────────────────────────

    def search_memory(
        self,
        query: str,
        limit: Optional[int] = None,
        session_id: str = "",
        since: str = "",
        until: str = "",
    ) -> list[dict]:
        """Hybrid search + v1.7.0 result shaping (spec §3.6, §3.11, §3.14).

        Order: time-window pre-filter (inside both queries) → vector floor →
        fusion → parent resolution → lexical rerank → min_score →
        newest version first → relative cut → TTL refresh / access_count
        (top-K) → truncate to `limit`.
        """
        # §4.3.1 (v1.7.1): an unreadable since/until (or since > until) is an
        # error and the search is not run (raises TimeBoundError).
        time_filter = _time_filter_clause(since, until)

        if not self._ok:
            return []

        if limit is None:
            limit = self.cfg.get("search_result_limit", 20)

        # Encoding safety: strip lone surrogates before any FT.SEARCH /
        # embed call that would .encode("utf-8") on the query.
        query    = sanitize_surrogates(query)
        query    = query[: self.cfg.get("search_query_max_chars", 2000)]
        owner_id = self.cfg["owner_id"]
        half_life     = self.cfg.get("half_life_days", 3)
        min_score_val = self.cfg.get("min_score", 0.2)
        bm25_thr      = self.cfg.get("bm25_min_threshold", 0.1)

        # b_session resolution (spec §3.6): per-call arg → env var (in cfg) → process UUID
        effective_session = (session_id or "").strip() or self.cfg.get("_session_id", "")
        b_sess_match    = self.cfg.get("b_session_match", 1.0)
        b_sess_mismatch = self.cfg.get("b_session_mismatch", 0.6)

        # §4.3.1 / §3.14 step 1: since/until are PRE-filters inside both
        # queries (time_filter, parsed above).
        vec_results = self._vector_search(query, owner_id, limit, time_filter)
        # §3.14 step 2: vector floor. A dropped row can still enter via BM25
        # (with cos_sim = 0.0).
        vec_results = apply_vector_floor(
            vec_results, self.cfg.get("vector_min_cos", 0.85)
        )
        # §3.7: a BM25 failure never fails search_memory.
        try:
            bm25_results = self._bm25_search(query, owner_id, limit, time_filter)
        except _CONN_ERRORS:
            raise  # §3.9: a lost connection is a backend error, not "no results"
        except Exception as e:
            print(f"[n3mc] BM25 search skipped: {e}", file=sys.stderr)
            bm25_results = {}

        all_keys = set(vec_results) | set(bm25_results)
        max_bm25 = max(
            (v["bm25_score"] for v in bm25_results.values()), default=1.0
        )

        candidates: list[dict] = []
        for key in all_keys:
            vr = vec_results.get(key, {})
            br = bm25_results.get(key, {})

            content         = vr.get("content") or br.get("content", "")
            timestamp       = vr.get("timestamp") or br.get("timestamp", "")
            timestamp_epoch = vr.get("timestamp_epoch") or br.get("timestamp_epoch", 0.0)
            imp             = _to_float(vr.get("importance") or br.get("importance", 1.0))
            acc             = _to_int(vr.get("access_count") or br.get("access_count", 0))
            parent_id       = vr.get("parent_id") or br.get("parent_id", "")
            mem_id          = vr.get("id") or br.get("id", "")
            row_sess        = vr.get("session_id") or br.get("session_id", "")
            turn_id         = vr.get("turn_id") or br.get("turn_id", "")

            cos  = vr.get("cos_sim", 0.0)
            bm25 = br.get("bm25_score", 0.0)
            kw   = keyword_relevance(bm25, max_bm25, bm25_thr)
            dec  = time_decay(timestamp, half_life)
            b    = b_local(imp, acc, self.cfg)

            # b_session: match when effective_session is set and matches stored tag.
            # When effective_session is empty every row gets b_sess_mismatch
            # symmetrically (spec §3.6 — no row gains an advantage).
            b_sess = (
                b_sess_match
                if (effective_session and row_sess == effective_session)
                else b_sess_mismatch
            )
            score = final_score(cos, kw, dec, b, b_sess)
            # v1.7.1 (LD1): the floors judge filter_score — no time_decay,
            # no b_session (they decide the order, not whether a row shows).
            fscore = filter_score(cos, kw, b)

            # min_score is NOT applied here: the lexical reranker can lift a
            # fused score that is just below the threshold (spec §3.6).
            candidates.append({
                "key":             key,
                "id":              mem_id,
                "content":         content,
                "timestamp":       timestamp,
                "timestamp_epoch": timestamp_epoch,
                "score":           score,
                "filter_score":    fscore,
                "parent_id":       parent_id,
                "session_id":      row_sess,
                "turn_id":         turn_id,
                "importance":      imp,
                "access_count":    acc,
            })

        # Sort descending so the highest-scoring chunk wins when multiple
        # chunks share the same parent (spec §3.11: keep the best hit).
        candidates.sort(key=lambda x: x["score"], reverse=True)

        # §3.11 parent-document resolution — BEFORE lexical rerank so the
        # reranker sees the full verbatim body rather than just the chunk text.
        seen_parents: dict[str, bool] = {}
        resolved: list[dict] = []
        for c in candidates:
            pid = c.get("parent_id", "")
            if pid:
                if pid in seen_parents:
                    continue  # lower-scoring duplicate; already emitted
                try:
                    doc_data = self._client.hgetall(f"doc:{pid}")
                except _CONN_ERRORS:
                    raise  # §3.9: a lost connection is a backend error, not "no results"
                except Exception:
                    doc_data = None
                if doc_data:
                    seen_parents[pid] = True
                    chunk_count = doc_data.get(b"chunk_count", b"?").decode()
                    raw = doc_data.get(b"content", b"")
                    c["content"]      = raw.decode("utf-8") if raw else ""
                    c["id"]           = pid
                    c["_tag"]         = f"[doc×{chunk_count}]"
                    c["_parent_key"]  = f"doc:{pid}"
                    doc_turn = doc_data.get(b"turn_id", b"")
                    if doc_turn:
                        c["turn_id"] = doc_turn.decode()
                    doc_ts = doc_data.get(b"timestamp", b"")
                    if doc_ts:
                        c["timestamp"] = doc_ts.decode()
                    doc_ep = doc_data.get(b"timestamp_epoch", b"")
                    if doc_ep:
                        c["timestamp_epoch"] = _to_float(doc_ep.decode(), c["timestamp_epoch"])
                    # Every sibling chunk key, so the TTL refresh below extends
                    # the whole document's chunks together (spec §3.11). Older
                    # docs without chunk_ids fall back to single-chunk refresh.
                    cids_raw = doc_data.get(b"chunk_ids", b"")
                    cids = cids_raw.decode().split() if cids_raw else []
                    c["_sibling_keys"] = [f"mem:{cid}" for cid in cids if cid]
                    # §3.14: a parent's representative embedding is its FIRST
                    # chunk's (chunk_ids[0]). No chunk_ids → no representative.
                    c["_rep_key"] = f"mem:{cids[0]}" if cids else ""
                # else: parent expired — orphan chunk surfaces as its own hit
                # (graceful degrade, spec §3.11). Do NOT mark seen_parents so
                # sibling orphan chunks each get their own listing.
            resolved.append(c)
        candidates = resolved

        # Lexical rerank (spec §3.6) — optional, default enabled.
        if self.cfg.get("lexical_rerank_enabled", True):
            candidates = lexical_rerank(
                candidates,
                query,
                self.cfg.get("rerank_weight", 0.3),
                self.cfg.get("rerank_phrase_weight", 0.2),
            )
        else:
            candidates.sort(key=lambda x: x["score"], reverse=True)

        # min_score floor AFTER rerank, on filter_score (spec §3.6 / §6, v1.7.1).
        if min_score_val > 0:
            candidates = [c for c in candidates if judged_score(c) >= min_score_val]

        # §3.14 step 4: newest version first.
        if self.cfg.get("version_order_enabled", True) and len(candidates) > 1:
            rep_vectors, rep_texts = self._fetch_representatives(candidates)
            candidates = order_versions_newest_first(
                candidates, rep_vectors, self.cfg, rep_texts
            )

        # §3.14 step 5: relative cut (spares `_version_group` rows).
        candidates = relative_cut(
            candidates, self.cfg.get("relative_min_score_ratio", 0.6)
        )

        # §3.14 step 6 / §3.11: TTL refresh on top-K hits.
        if self.cfg.get("ttl_refresh_on_search", True):
            try:
                self._refresh_ttl(candidates[: self.cfg.get("ttl_refresh_top_k", 5)])
            except _CONN_ERRORS:
                raise  # §3.9: a lost connection is a backend error, not "no results"
            except Exception as e:
                print(f"[n3mc] TTL refresh skipped: {e}", file=sys.stderr)

        final: list[dict] = []
        for c in candidates[:limit]:
            c.pop("_parent_key", None)
            c.pop("_sibling_keys", None)
            c.pop("_rep_key", None)
            c.pop("key", None)
            final.append(c)
        return final

    def _refresh_ttl(self, rows: list[dict]) -> None:
        """TTL refresh for the top-K hits (spec §3.3, §3.11, §3.14 step 6).

        Pipeline 1: EXPIRE the hit, its parent doc and ALL sibling chunks.
        Pipeline 2 (v1.7.1): only where the EXPIRE of the hit / parent
        returned 1 (the key still exists):
          - HINCRBY access_count on the hit (LL4 — never recreate an expired
            key as a hash without TTL),
          - reset the dedup guard with the record (LM7): `mem:sha:<sha1>` for
            a standalone record, `docsha:<sha1>` for a parent document. The
            sha1 is recomputed from the stored content (same `_sha1` as
            save_memory). A chunk has no guard of its own.
        """
        if not rows:
            return
        ttl = self.cfg["ttl_seconds"]
        pipe = self._client.pipeline(transaction=True)
        slots: list[tuple[dict, int, Optional[int]]] = []   # (row, hit_idx, parent_idx)
        n = 0
        for c in rows:
            parent_key = c.get("_parent_key")
            parent_idx = None
            if parent_key:
                pipe.expire(parent_key, ttl)
                parent_idx = n
                n += 1
                sib_keys = list(c.get("_sibling_keys") or [])
                if c["key"] not in sib_keys:
                    sib_keys.append(c["key"])
                hit_idx = None
                for sk in sib_keys:
                    pipe.expire(sk, ttl)
                    if sk == c["key"]:
                        hit_idx = n
                    n += 1
            else:
                pipe.expire(c["key"], ttl)
                hit_idx = n
                n += 1
            slots.append((c, hit_idx, parent_idx))
        replies = pipe.execute()

        pipe2 = self._client.pipeline(transaction=True)
        queued = 0
        for c, hit_idx, parent_idx in slots:
            if hit_idx is not None and replies[hit_idx]:
                # access_count only on the chunk that actually matched.
                pipe2.hincrby(c["key"], "access_count", 1)
                queued += 1
            content = c.get("content") or ""
            if parent_idx is not None:
                if replies[parent_idx] and content:
                    pid = c.get("id", "")
                    pipe2.set(f"docsha:{_sha1(content)}", pid.encode(), ex=ttl)
                    queued += 1
            elif not c.get("parent_id") and hit_idx is not None and replies[hit_idx] and content:
                mid = c.get("id", "")
                pipe2.set(f"mem:sha:{_sha1(content)}", mid.encode(), ex=ttl)
                queued += 1
        if queued:
            pipe2.execute()

    def _fetch_representatives(self, rows: list[dict]) -> tuple[list, list]:
        """Fetch each row's representative embedding + text (spec §3.14 step 4).

        Standalone row (or orphan chunk) → its own `mem:` hash (`row["key"]`).
        Parent document → its first chunk `mem:<chunk_ids[0]>` (`row["_rep_key"]`).
        Returns (vectors, texts), aligned with *rows*; vectors are raw FLOAT32
        bytes or None, texts are str ("" when unavailable). One pipeline.
        """
        keys: list[str] = []
        for r in rows:
            if r.get("_parent_key"):
                keys.append(r.get("_rep_key", ""))
            else:
                keys.append(r.get("key", ""))
        vectors: list = [None] * len(rows)
        texts:   list = [""] * len(rows)
        try:
            pipe = self._client.pipeline()
            fetch_idx: list[int] = []
            for i, k in enumerate(keys):
                if k:
                    pipe.hmget(k, b"embedding", b"content")
                    fetch_idx.append(i)
            if not fetch_idx:
                return vectors, texts
            replies = pipe.execute()
        except _CONN_ERRORS:
            raise  # §3.9: a lost connection is a backend error, not "no results"
        except Exception as e:
            print(f"[n3mc] version ordering skipped: {e}", file=sys.stderr)
            return vectors, texts
        for i, reply in zip(fetch_idx, replies):
            try:
                emb, content = reply
            except Exception:
                continue
            vectors[i] = emb or None
            if content:
                texts[i] = (
                    content.decode("utf-8", errors="replace")
                    if isinstance(content, bytes) else str(content)
                )
        return vectors, texts

    @staticmethod
    def _unwrap_search_response(res) -> list:
        """Normalize FT.SEARCH response across redis-py versions.

        redis-py <8: returns a raw RESP2 list [count, key, fields, ...]
        redis-py ≥8: may return a dict or SearchResult via registered callbacks.
        We extract the raw list form in either case so downstream parsing is
        version-independent.
        """
        if isinstance(res, list):
            return res
        # dict (redis-py 8 callback format): try common key names
        if isinstance(res, dict):
            # {total_results: N, results: [...]}, or similar
            count = res.get("total_results", res.get("total", 0))
            results = res.get("results", res.get("docs", []))
            # Rebuild the RESP2-style flat list for downstream compatibility
            flat: list = [count]
            for doc in results:
                if isinstance(doc, dict):
                    key  = doc.get("id", "")
                    fmap = doc.get("fields", doc.get("extra_attributes", doc))
                    # Remove meta-keys that aren't field-value pairs
                    fmap = {k: v for k, v in fmap.items() if k not in ("id", "score")}
                    flat.append(key.encode() if isinstance(key, str) else key)
                    flat.append({k.encode() if isinstance(k, str) else k:
                                  v.encode() if isinstance(v, str) else v
                                  for k, v in fmap.items()})
                else:
                    # Unknown inner format — skip
                    pass
            return flat
        # SearchResult or other object — try iterating as a sequence
        try:
            return list(res)
        except TypeError:
            return [0]

    def _vector_search(
        self, query: str, owner_id: str, limit: int, time_filter: str = ""
    ) -> dict[str, dict]:
        """KNN search — no owner_id TAG filter (UUID hyphen issue §3.12).

        *time_filter* (`@timestamp_epoch:[a b]`, §4.3.1) is a PRE-filter:
        `@timestamp_epoch:[a b]=>[KNN N @embedding $vec AS __dist]`.
        """
        vec_bytes = embed(normalize_text(query), is_query=True)
        if not vec_bytes:
            return {}
        knn_limit = min(limit * 5, 100)
        base = time_filter or "*"
        try:
            res = self._client.execute_command(
                "FT.SEARCH", INDEX_NAME,
                f"{base}=>[KNN {knn_limit} @embedding $vec AS __dist]",
                "PARAMS", "2", "vec", vec_bytes,
                "RETURN", "11",
                "__dist", "owner_id", "content", "timestamp", "timestamp_epoch",
                "importance", "access_count", "parent_id", "id", "session_id", "turn_id",
                "SORTBY", "__dist",
                "LIMIT", "0", str(knn_limit),
                "DIALECT", "2",
            )
            res = self._unwrap_search_response(res)
        except _CONN_ERRORS:
            raise  # §3.9: a lost connection is a backend error, not "no results"
        except Exception as e:
            print(f"[n3mc] vector search: {e}", file=sys.stderr)
            return {}

        if not res or res[0] == 0:
            return {}

        out: dict[str, dict] = {}
        i = 1
        while i + 1 < len(res):
            key   = res[i].decode() if isinstance(res[i], bytes) else str(res[i])
            fdict = _parse_fields(res[i + 1])
            if fdict.get("owner_id") != owner_id:
                i += 2
                continue
            dist = _to_float(fdict.get("__dist", "1.0"))
            out[key] = {
                "cos_sim":         cosine_sim_from_distance(dist),
                "content":         fdict.get("content", ""),
                "timestamp":       fdict.get("timestamp", ""),
                "timestamp_epoch": _to_float(fdict.get("timestamp_epoch", "0"), 0.0),
                "importance":      _to_float(fdict.get("importance", "1.0")),
                "access_count":    _to_int(fdict.get("access_count", "0")),
                "parent_id":       fdict.get("parent_id", ""),
                "id":              fdict.get("id", ""),
                "session_id":      fdict.get("session_id", ""),
                "turn_id":         fdict.get("turn_id", ""),
            }
            i += 2
        return out

    def _bm25_search(
        self, query: str, owner_id: str, limit: int, time_filter: str = ""
    ) -> dict[str, dict]:
        """BM25 FTS search — no owner_id TAG filter (spec §3.12).

        The query is cleaned with `prepare_query` (§3.7); an empty cleaned
        query skips BM25 (vector only). *time_filter* is appended as a
        PRE-filter: `(<text query>) @timestamp_epoch:[a b]` (§4.3.1).
        Scorer fallback BM25 → BM25STD → default (§3.6). Any failure returns
        {} so search_memory continues with vector results.
        """
        try:
            fts_q = drop_lone_cjk_tokens(prepare_query(query))
        except Exception as e:
            print(f"[n3mc] BM25 query cleaning: {e}", file=sys.stderr)
            return {}
        if not fts_q:
            return {}
        bm25_limit = min(limit * 5, 100)
        # v1.7.1 (LD3 / LM8): terms joined with OR — a record matches when it
        # contains ANY term; BM25 + the rerank coverage do the ranking.
        q = "|".join(fts_q.split())
        fts_query  = f"(@content:({q}) | @content_ngram:({q}))"
        if time_filter:
            fts_query = f"{fts_query} {time_filter}"

        res = None
        for scorer in ("BM25", "BM25STD", None):
            scorer_args = ["SCORER", scorer] if scorer else []
            try:
                res = self._client.execute_command(
                    "FT.SEARCH", INDEX_NAME,
                    fts_query,
                    *scorer_args,
                    "WITHSCORES",
                    "RETURN", "10",
                    "owner_id", "content", "timestamp", "timestamp_epoch",
                    "importance", "access_count", "parent_id", "id", "session_id", "turn_id",
                    "LIMIT", "0", str(bm25_limit),
                    "DIALECT", "2",
                )
                res = self._unwrap_search_response(res)
                break
            except _CONN_ERRORS:
                raise  # §3.9: a lost connection is a backend error, not "no results"
            except Exception as e:
                if scorer and "scor" in str(e).lower():
                    continue  # unknown scorer on this Redis version → next
                print(f"[n3mc] BM25 search: {e}", file=sys.stderr)
                return {}

        if not res or res[0] == 0:
            return {}

        out: dict[str, dict] = {}
        i = 1
        while i + 2 < len(res):
            key       = res[i].decode() if isinstance(res[i], bytes) else str(res[i])
            score_raw = res[i + 1]
            score     = _to_float(
                score_raw.decode() if isinstance(score_raw, bytes) else score_raw
            )
            fdict = _parse_fields(res[i + 2])
            if fdict.get("owner_id") != owner_id:
                i += 3
                continue
            out[key] = {
                "bm25_score":      score,
                "content":         fdict.get("content", ""),
                "timestamp":       fdict.get("timestamp", ""),
                "timestamp_epoch": _to_float(fdict.get("timestamp_epoch", "0"), 0.0),
                "importance":      _to_float(fdict.get("importance", "1.0")),
                "access_count":    _to_int(fdict.get("access_count", "0")),
                "parent_id":       fdict.get("parent_id", ""),
                "id":              fdict.get("id", ""),
                "session_id":      fdict.get("session_id", ""),
                "turn_id":         fdict.get("turn_id", ""),
            }
            i += 3
        return out

    # ── list ─────────────────────────────────────────────────────────────────

    LIST_PAGE = 200

    def list_memories(self, limit: int = 20) -> list[dict]:
        """Return standalone memories + parent docs newest-first (spec §3.11).

        FT.SEARCH '*' fetches indexed mem: entries newest first; Python filters
        to those owned by this owner AND with empty parent_id (standalone
        only). v1.7.1 (LM6): pages of 200 (`LIMIT <offset> 200`) until `limit`
        standalone memories are collected or the index is exhausted.
        Parent docs are fetched separately via SCAN doc:* (outside the index).
        """
        if not self._ok:
            return []
        owner_id = self.cfg["owner_id"]
        results: list[dict] = []

        # Standalone memories from the RediSearch index.
        try:
            offset = 0
            while len(results) < limit:
                res = self._client.execute_command(
                    "FT.SEARCH", INDEX_NAME,
                    "*",
                    "RETURN", "5", "owner_id", "parent_id", "content", "timestamp", "id",
                    "SORTBY", "timestamp_epoch", "DESC",
                    "LIMIT", str(offset), str(self.LIST_PAGE),
                    "DIALECT", "2",
                )
                res = self._unwrap_search_response(res)
                if not res or res[0] == 0:
                    break
                rows = 0
                i = 1
                while i + 1 < len(res):
                    rows += 1
                    fdict = _parse_fields(res[i + 1])
                    i += 2
                    if (fdict.get("owner_id") != owner_id
                            or fdict.get("parent_id", "")):
                        continue
                    results.append({
                        "id":        fdict.get("id", ""),
                        "content":   fdict.get("content", ""),
                        "timestamp": fdict.get("timestamp", ""),
                        "tag":       "",
                    })
                offset += self.LIST_PAGE
                if rows < self.LIST_PAGE or offset >= int(res[0]):
                    break
        except _CONN_ERRORS:
            raise  # §3.9: a lost connection is a backend error, not "no results"
        except Exception as e:
            print(f"[n3mc] list_memories FTS: {e}", file=sys.stderr)

        # Parent documents (outside prefix mem:, not in the index).
        try:
            cursor = 0
            parents: list[dict] = []
            while True:
                cursor, keys = self._client.scan(cursor, match="doc:*", count=100)
                for key in keys:
                    try:
                        d = self._client.hgetall(key)
                        if d.get(b"owner_id", b"").decode() != owner_id:
                            continue
                        raw_content = d.get(b"content", b"")
                        preview     = raw_content.decode("utf-8") if raw_content else ""
                        chunk_count = d.get(b"chunk_count", b"?").decode()
                        parents.append({
                            "id":        d.get(b"id", b"").decode(),
                            "content":   preview[:200] + ("…" if len(preview) > 200 else ""),
                            "timestamp": d.get(b"timestamp", b"").decode(),
                            "tag":       f"[doc×{chunk_count}]",
                        })
                    except Exception:
                        pass
                if cursor == 0:
                    break
            results.extend(parents)
        except _CONN_ERRORS:
            raise  # §3.9: a lost connection is a backend error, not "no results"
        except Exception as e:
            print(f"[n3mc] list_memories parent scan: {e}", file=sys.stderr)

        results.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        return results[:limit]

    # ── delete ───────────────────────────────────────────────────────────────

    def delete_memory(self, mem_id: str) -> dict:
        """Delete a single memory or a parent + all its chunks (spec §3.11).

        Branch on whether doc:<mem_id> exists:
          yes → cascade delete parent, docsha:, and every chunk
          no  → delete mem:<mem_id> and its mem:sha: guard
        """
        if not self._ok:
            return {"status": "error", "reason": self.hint()}

        owner_id = self.cfg["owner_id"]
        doc_key = f"doc:{mem_id}"
        d = self._client.hgetall(doc_key)
        if d:
            # v1.7.1 (LL3): only records of the configured owner_id.
            if d.get(b"owner_id", b"").decode() != owner_id:
                return {"status": "not_found", "id": mem_id}
            sha_key = None
            try:
                content = d[b"content"].decode("utf-8")
                sha_key = f"docsha:{_sha1(content)}"
            except Exception:
                pass

            # v1.7.1: the chunks are the ids in `chunk_ids`.
            cids_raw = d.get(b"chunk_ids", b"")
            chunk_keys: list[str] = [
                f"mem:{cid}" for cid in (cids_raw.decode().split() if cids_raw else []) if cid
            ]
            if not chunk_keys:
                chunk_keys = self._legacy_chunk_keys(mem_id)

            # Chunks have no individual sha guards (spec §3.8) — just delete
            # parent, docsha:, and all chunks, in one pipeline.
            pipe = self._client.pipeline()
            pipe.delete(doc_key)
            if sha_key:
                pipe.delete(sha_key)
            for ck in chunk_keys:
                pipe.delete(ck)
            pipe.execute()
            return {
                "status":         "deleted",
                "id":             mem_id,
                "chunks_deleted": len(chunk_keys),
            }

        mem_key = f"mem:{mem_id}"
        fields = self._client.hmget(mem_key, b"owner_id", b"content")
        if not self._client.exists(mem_key):
            return {"status": "not_found", "id": mem_id}
        if (fields[0].decode() if fields[0] else "") != owner_id:
            return {"status": "not_found", "id": mem_id}

        sha_key = None
        try:
            raw_content = fields[1]
            if raw_content:
                content = (
                    raw_content.decode("utf-8")
                    if isinstance(raw_content, bytes)
                    else raw_content
                )
                sha_key = f"mem:sha:{_sha1(content)}"
        except Exception:
            pass

        pipe = self._client.pipeline()
        pipe.delete(mem_key)
        if sha_key:
            pipe.delete(sha_key)
        pipe.execute()
        return {"status": "deleted", "id": mem_id}

    def _legacy_chunk_keys(self, mem_id: str) -> list[str]:
        """Chunks of a parent saved before `chunk_ids` existed (spec §3.11)."""
        # Attempt TAG query for chunks; falls back to SCAN on UUID parse error.
        # With RETURN 0 the FT.SEARCH reply is a FLAT key list
        # [count, key1, key2, ...] — no interleaved field arrays — so iterate
        # res[1:] one element at a time (NOT i += 2, which would drop every
        # other chunk and leak orphans). Hyphenated UUIDs normally make this
        # TAG query match 0 rows (§3.12), so the SCAN fallback below is what
        # runs in practice; this path is correct for the day the TAG query works.
        chunk_keys: list[str] = []
        try:
            res = self._client.execute_command(
                "FT.SEARCH", INDEX_NAME,
                f"@parent_id:{{{mem_id}}}",
                "RETURN", "0",
                "LIMIT", "0", "1000",
                "DIALECT", "2",
            )
            res = self._unwrap_search_response(res)
            if res and res[0] > 0:
                for raw in res[1:]:
                    chunk_keys.append(
                        raw.decode() if isinstance(raw, bytes) else str(raw)
                    )
        except Exception:
            pass

        if not chunk_keys:
            cursor = 0
            while True:
                cursor, keys = self._client.scan(cursor, match="mem:*", count=200)
                for key in keys:
                    raw_key = key.decode() if isinstance(key, bytes) else str(key)
                    if raw_key.startswith("mem:sha:"):
                        continue
                    try:
                        pid_val = self._client.hget(key, b"parent_id")
                        if pid_val and pid_val.decode() == mem_id:
                            chunk_keys.append(raw_key)
                    except Exception:
                        pass
                if cursor == 0:
                    break
        return chunk_keys

    # ── bulk delete by session (Lite-only, spec §4.3) ────────────────────────

    def delete_by_session(self, session_id: str) -> dict:
        """Delete every memory, parent doc, chunk, and sha guard for a session.

        Two-phase SCAN: mem:* (singles + chunks) then doc:* (parents).
        Scoped to the configured owner_id.
        """
        if not self._ok:
            return {"status": "error", "reason": self.hint()}
        session_id = session_id.strip()
        if not session_id:
            return {"status": "error", "reason": "session_id required"}

        owner_id         = self.cfg["owner_id"]
        keys_to_delete:  list[str] = []
        sha_keys:        list[str] = []
        deleted_singles  = 0
        deleted_chunks   = 0
        deleted_docs     = 0

        # Phase 1: scan mem:* (skip sha guards).
        # Only singles carry sha guards (spec §3.8: chunks do not have sha guards).
        cursor = 0
        while True:
            cursor, keys = self._client.scan(cursor, match="mem:*", count=200)
            for key in keys:
                raw_key = key.decode() if isinstance(key, bytes) else str(key)
                if raw_key.startswith("mem:sha:"):
                    continue
                try:
                    fields = self._client.hmget(
                        raw_key,
                        b"session_id", b"owner_id", b"parent_id", b"content",
                    )
                    sid       = fields[0].decode() if fields[0] else ""
                    oid       = fields[1].decode() if fields[1] else ""
                    pid       = fields[2].decode() if fields[2] else ""
                    content_b = fields[3]
                except Exception:
                    continue
                if sid != session_id or oid != owner_id:
                    continue
                keys_to_delete.append(raw_key)
                if pid:
                    # Chunk: no sha guard to collect (spec §3.8).
                    deleted_chunks += 1
                else:
                    # Standalone: collect its sha guard.
                    if content_b:
                        try:
                            sha_keys.append(
                                f"mem:sha:{_sha1(content_b.decode('utf-8'))}"
                            )
                        except Exception:
                            pass
                    deleted_singles += 1
            if cursor == 0:
                break

        # Phase 2: scan doc:*
        cursor = 0
        while True:
            cursor, keys = self._client.scan(cursor, match="doc:*", count=100)
            for key in keys:
                raw_key = key.decode() if isinstance(key, bytes) else str(key)
                try:
                    fields = self._client.hmget(
                        raw_key,
                        b"session_id", b"owner_id", b"content",
                    )
                    sid       = fields[0].decode() if fields[0] else ""
                    oid       = fields[1].decode() if fields[1] else ""
                    content_b = fields[2]
                except Exception:
                    continue
                if sid != session_id or oid != owner_id:
                    continue
                keys_to_delete.append(raw_key)
                deleted_docs += 1
                if content_b:
                    try:
                        sha_keys.append(
                            f"docsha:{_sha1(content_b.decode('utf-8'))}"
                        )
                    except Exception:
                        pass
            if cursor == 0:
                break

        if not keys_to_delete and not sha_keys:
            return {
                "status":     "not_found",
                "session_id": session_id,
                "deleted":    0,
            }

        pipe = self._client.pipeline()
        for k in keys_to_delete:
            pipe.delete(k)
        for sk in sha_keys:
            pipe.delete(sk)
        pipe.execute()

        total = deleted_singles + deleted_chunks + deleted_docs
        return {
            "status":            "deleted",
            "session_id":        session_id,
            "documents_deleted": deleted_docs,
            "chunks_deleted":    deleted_chunks,
            "singles_deleted":   deleted_singles,
            "deleted":           total,
        }

    # ── §4.3.1 thread retrieval ──────────────────────────────────────────────

    def recall_thread(
        self,
        turn_id: str,
        before: int = 2,
        after: int = 2,
    ) -> list[dict]:
        """Return entries sharing turn_id plus `before` earlier and `after` later entries.

        SCAN mem:* (standalones + chunks) + SCAN doc:*, sort by timestamp_epoch,
        locate target indices, slice neighbourhood.
        Spec §4.3.1: "同一 turn_id を持つ親ドキュメント／単独メモリ／チャンク".
        Returns [] when turn_id is not found.
        """
        if not self._ok:
            return []

        owner_id = self.cfg["owner_id"]
        all_entries: list[dict] = []

        # mem: entries — standalone memories only (parent_id == "").
        # Chunks (parent_id != "") are skipped: their parent doc is already
        # collected in the doc:* scan below, which returns the full verbatim body.
        # This mirrors search_memory's chunk→parent resolution so recall_thread
        # never surfaces fragmented chunk text.
        cursor = 0
        while True:
            cursor, keys = self._client.scan(cursor, match="mem:*", count=200)
            for key in keys:
                raw_key = key.decode() if isinstance(key, bytes) else str(key)
                if raw_key.startswith("mem:sha:"):
                    continue
                try:
                    d = self._client.hmget(
                        raw_key,
                        b"owner_id", b"parent_id", b"id",
                        b"content", b"timestamp", b"timestamp_epoch", b"turn_id",
                    )
                    oid = d[0].decode() if d[0] else ""
                    pid = d[1].decode() if d[1] else ""
                except Exception:
                    continue
                if oid != owner_id:
                    continue
                if pid:
                    # Skip chunks — parent doc carries the full body.
                    continue
                all_entries.append({
                    "id":              (d[2].decode() if d[2] else ""),
                    "content":         (d[3].decode("utf-8") if d[3] else ""),
                    "timestamp":       (d[4].decode() if d[4] else ""),
                    "timestamp_epoch": _to_float(d[5].decode() if d[5] else "0", 0.0),
                    "turn_id":         (d[6].decode() if d[6] else ""),
                    "tag":             "",
                })
            if cursor == 0:
                break

        # Parent doc: entries
        cursor = 0
        while True:
            cursor, keys = self._client.scan(cursor, match="doc:*", count=100)
            for key in keys:
                raw_key = key.decode() if isinstance(key, bytes) else str(key)
                try:
                    d = self._client.hmget(
                        raw_key,
                        b"owner_id", b"id",
                        b"content", b"timestamp", b"timestamp_epoch",
                        b"turn_id", b"chunk_count",
                    )
                    oid = d[0].decode() if d[0] else ""
                except Exception:
                    continue
                if oid != owner_id:
                    continue
                chunk_count = d[6].decode() if d[6] else "?"
                raw_content = d[2] or b""
                all_entries.append({
                    "id":              (d[1].decode() if d[1] else ""),
                    "content":         raw_content.decode("utf-8") if raw_content else "",
                    "timestamp":       (d[3].decode() if d[3] else ""),
                    "timestamp_epoch": _to_float(d[4].decode() if d[4] else "0", 0.0),
                    "turn_id":         (d[5].decode() if d[5] else ""),
                    "tag":             f"[doc×{chunk_count}]",
                })
            if cursor == 0:
                break

        if not all_entries:
            return []

        # Sort chronologically
        all_entries.sort(key=lambda e: e.get("timestamp_epoch") or _epoch_from_iso(e.get("timestamp", "")))

        # Find indices of target turn entries
        target_indices = [i for i, e in enumerate(all_entries) if e.get("turn_id") == turn_id]
        if not target_indices:
            return []

        first = target_indices[0]
        last  = target_indices[-1]
        start = max(0, first - before)
        end   = min(len(all_entries), last + 1 + after)

        return all_entries[start:end]
