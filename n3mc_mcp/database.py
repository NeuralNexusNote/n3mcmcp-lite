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

from .processor import (
    apply_vector_floor,
    b_local,
    chunk_text,
    content_ngram,
    cosine_sim_from_distance,
    drop_lone_cjk_tokens,
    embed,
    final_score,
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


def backend_hint(redis_url: str = "redis://localhost:6379/0") -> str:
    """Recovery hint for an unreachable Redis (spec §3.9, English).

    Shows host:port from *redis_url* WITHOUT the user name or password.
    """
    try:
        u = urlparse(redis_url or "")
        where = f"{u.hostname or 'localhost'}:{u.port or 6379}"
    except Exception:
        where = "the configured redis_url"
    return (
        f"Redis Stack is not reachable at {where}. Start it with: "
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


def _epoch_from_iso(ts: str, is_until: bool = False) -> float:
    """Parse ISO 8601 date or datetime string to Unix epoch (UTC).

    Date-only inputs:  since → 00:00:00 UTC,  until → 23:59:59 UTC.
    Returns 0.0 on parse failure so callers can treat it as "no filter".
    """
    if not ts:
        return 0.0
    ts = ts.strip()
    try:
        if "T" in ts or len(ts) > 10:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = datetime.strptime(ts[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if is_until:
                dt = dt.replace(hour=23, minute=59, second=59)
        return dt.timestamp()
    except Exception:
        return 0.0


def _time_filter_clause(since: str = "", until: str = "") -> str:
    """§4.3.1 since/until → RediSearch NUMERIC pre-filter.

    Returns `@timestamp_epoch:[<since> <until>]` with `-inf` / `+inf` for an
    open bound, or "" when neither bound is given (or neither parses).
    Used inside the KNN query (`<clause>=>[KNN ...]`) and the BM25 query
    (`(<text>) <clause>`). NUMERIC filters do not hit the UUID TAG problem
    of §3.12.
    """
    lo = _epoch_from_iso(since, is_until=False) if since else 0.0
    hi = _epoch_from_iso(until, is_until=True) if until else 0.0
    if not lo and not hi:
        return ""
    lo_s = repr(float(lo)) if lo else "-inf"
    hi_s = repr(float(hi)) if hi else "+inf"
    return f"@timestamp_epoch:[{lo_s} {hi_s}]"


# ── main class ───────────────────────────────────────────────────────────────

class Database:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self._client: Optional[redis_lib.Redis] = None
        self._ok = False

    # ── §3.9 connection ──────────────────────────────────────────────────────

    def connect(self, quiet: bool = False) -> bool:
        """Spec §3.9 step 2: connect + PING.  On failure stay up with _ok=False."""
        try:
            self._client = redis_lib.Redis.from_url(
                self.cfg["redis_url"], decode_responses=False,
                socket_connect_timeout=2,
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

    def hint(self) -> str:
        return backend_hint(self.cfg.get("redis_url", ""))

    def mark_disconnected(self) -> None:
        if self._ok:
            print(f"[n3mc] Redis connection lost. {self.hint()}", file=sys.stderr)
        self._ok = False

    def ensure_connected(self) -> bool:
        """Spec §3.9 "Redis availability on every tool call".

        Connected → PING. Not connected or PING failed → rebuild the client
        and PING again. On reconnection re-run enforce_ephemeral() and
        ensure_index(). stderr gets one line per state change only.
        """
        if self._ok and self._client is not None:
            try:
                self._client.ping()
                return True
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
            return True
        return False

    # ── §3.4 ephemerality ────────────────────────────────────────────────────

    def enforce_ephemeral(self) -> None:
        """Disable RDB/AOF so Redis never persists to disk (spec §3.4).

        Called at every startup so any persistence re-enabled between runs
        is immediately reverted.
        """
        if not self._ok:
            return
        try:
            self._client.config_set("appendonly", "no")
            self._client.config_set("save", "")
        except Exception as e:
            print(f"[n3mc] enforce_ephemeral: {e}", file=sys.stderr)

    # ── §3.9 / §3.10 index ──────────────────────────────────────────────────

    def ensure_index(self) -> None:
        """Idempotent FT.CREATE (spec §3.5 schema, §3.9 step 3, §3.10 repair)."""
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
        except Exception as e:
            if "already exists" not in str(e).lower():
                raise

    # ── §3.10 repair ────────────────────────────────────────────────────────

    def repair_memory(self) -> dict:
        """Idempotent ensure_index() call (spec §3.10).

        Lite version: thin operation — just re-ensure the RediSearch index.
        No migration markers, no FTS rebuild, no re-embedding loop.
        Existing Redis records are already indexed; expired ones are simply gone.
        """
        if not self._ok:
            return {"status": "error", "message": self.hint()}
        try:
            self.ensure_index()
            return {"status": "ok", "message": "index ensured"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

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
            return self._save_single(content, agent_name, owner_id, importance, effective_session, turn_id)
        return self._save_parent_chunks(content, agent_name, owner_id, importance, effective_session, turn_id)

    def _save_single(
        self,
        content: str,
        agent_name: str,
        owner_id: str,
        importance: float,
        session_id: str,
        turn_id: str = "",
    ) -> dict:
        """§3.8 (A): exact SHA dedup → HSET+EXPIRE+sha-guard pipeline.

        v1.7.0: no near-duplicate (similarity) rejection — an updated fact
        with a new value is saved as a new entry; search_memory puts the
        newest version first (§3.14).
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
        ngram = content_ngram(content)
        now_iso = _now_iso()
        now_epoch = _now_epoch()
        ttl = self.cfg["ttl_seconds"]

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
        pipe = self._client.pipeline()
        pipe.hset(mem_key, mapping=fields)
        pipe.expire(mem_key, ttl)
        pipe.set(sha_key, mem_id.encode(), ex=ttl)
        pipe.execute()

        result: dict = {"status": "saved", "saved": True, "id": mem_id, "ttl_seconds": ttl}
        if not vec_bytes:
            # Embedding model was unavailable at save time.  The record is stored
            # and BM25-searchable but will NOT appear in vector (KNN) search results.
            result["warning"] = "embedding_unavailable"
        return result

    def _save_parent_chunks(
        self,
        content: str,
        agent_name: str,
        owner_id: str,
        importance: float,
        session_id: str,
        turn_id: str = "",
    ) -> dict:
        """§3.8 (B) + §3.11: parent-level exact dedup → doc: parent → chunk pipeline.

        v1.7.0: no parent-level near-duplicate rejection (a re-made document
        with a few changes is saved as a new version).
        """
        sha = _sha1(content)
        docsha_key = f"docsha:{sha}"

        if self._client.exists(docsha_key):
            existing = self._client.get(docsha_key)
            parent_id = existing.decode() if isinstance(existing, bytes) else str(existing)
            return {"status": "duplicate", "saved": False, "parent_id": parent_id}

        threshold = self.cfg.get("chunk_threshold", 400)
        overlap   = self.cfg.get("chunk_overlap", 100)
        chunks    = chunk_text(content, threshold, overlap)

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

        # Write parent document + docsha guard in one pipeline.
        doc_key = f"doc:{parent_id}"
        pipe = self._client.pipeline()
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
        pipe.set(docsha_key, parent_id.encode(), ex=ttl)
        pipe.execute()

        # Write all chunks in a single pipeline (spec §3.11).
        # No per-chunk SHA guards (spec §3.8): sliding-window chunks overlap
        # by design and would otherwise reject each other.
        chunk_pipe = self._client.pipeline()
        for chunk, chunk_id in zip(chunks, chunk_ids):
            ngram     = content_ngram(chunk)
            vec_bytes = embed(normalize_text(chunk), is_query=False)
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
            chunk_pipe.hset(f"mem:{chunk_id}", mapping=fields)
            chunk_pipe.expire(f"mem:{chunk_id}", ttl)
        chunk_pipe.execute()

        return {
            "status":      "saved",
            "saved":       True,
            "parent_id":   parent_id,
            "chunks":      len(chunks),
            "saved_count": len(chunk_ids),
            "ids":         chunk_ids,
            "ttl_seconds": ttl,
        }

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

        # §4.3.1 / §3.14 step 1: since/until are PRE-filters inside both queries.
        time_filter = _time_filter_clause(since, until)

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

            # min_score is NOT applied here: the lexical reranker can lift a
            # fused score that is just below the threshold (spec §3.6).
            candidates.append({
                "key":             key,
                "id":              mem_id,
                "content":         content,
                "timestamp":       timestamp,
                "timestamp_epoch": timestamp_epoch,
                "score":           score,
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

        # min_score floor AFTER rerank (spec §3.6 / §6).
        if min_score_val > 0:
            candidates = [c for c in candidates if c["score"] >= min_score_val]

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

        # §3.14 step 6 / §3.11: TTL refresh on top-K hits, one pipeline so the
        # chunk, ALL sibling chunks and the parent doc are refreshed together.
        if self.cfg.get("ttl_refresh_on_search", True):
            ttl   = self.cfg["ttl_seconds"]
            top_k = self.cfg.get("ttl_refresh_top_k", 5)
            refresh_pipe = self._client.pipeline()
            for c in candidates[:top_k]:
                # access_count is incremented only on the chunk that actually
                # matched (the hit), not on its silently-refreshed siblings.
                refresh_pipe.hincrby(c["key"], "access_count", 1)
                parent_key = c.get("_parent_key")
                if parent_key:
                    refresh_pipe.expire(parent_key, ttl)
                    sib_keys = c.get("_sibling_keys") or [c["key"]]
                    for sk in sib_keys:
                        refresh_pipe.expire(sk, ttl)
                else:
                    refresh_pipe.expire(c["key"], ttl)
            try:
                refresh_pipe.execute()
            except _CONN_ERRORS:
                raise  # §3.9: a lost connection is a backend error, not "no results"
            except Exception:
                pass

        final: list[dict] = []
        for c in candidates[:limit]:
            c.pop("_parent_key", None)
            c.pop("_sibling_keys", None)
            c.pop("_rep_key", None)
            c.pop("key", None)
            final.append(c)
        return final

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
        fts_query  = f"(@content:({fts_q}) | @content_ngram:({fts_q}))"
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

    def list_memories(self, limit: int = 20) -> list[dict]:
        """Return standalone memories + parent docs newest-first (spec §3.11).

        FT.SEARCH '*' fetches all indexed mem: entries; Python filters to
        those owned by this owner AND with empty parent_id (standalone only).
        Parent docs are fetched separately via SCAN doc:* (outside the index).
        """
        if not self._ok:
            return []
        owner_id = self.cfg["owner_id"]
        results: list[dict] = []

        # Standalone memories from the RediSearch index.
        try:
            fetch_limit = max(limit * 5, 200)
            res = self._client.execute_command(
                "FT.SEARCH", INDEX_NAME,
                "*",
                "RETURN", "5", "owner_id", "parent_id", "content", "timestamp", "id",
                "SORTBY", "timestamp_epoch", "DESC",
                "LIMIT", "0", str(fetch_limit),
                "DIALECT", "2",
            )
            res = self._unwrap_search_response(res)
            if res and res[0] > 0:
                i = 1
                while i + 1 < len(res):
                    fdict = _parse_fields(res[i + 1])
                    if (fdict.get("owner_id") != owner_id
                            or fdict.get("parent_id", "")):
                        i += 2
                        continue
                    results.append({
                        "id":        fdict.get("id", ""),
                        "content":   fdict.get("content", ""),
                        "timestamp": fdict.get("timestamp", ""),
                        "tag":       "",
                    })
                    i += 2
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

        doc_key = f"doc:{mem_id}"
        if self._client.exists(doc_key):
            d = self._client.hgetall(doc_key)
            sha_key = None
            try:
                content = d[b"content"].decode("utf-8")
                sha_key = f"docsha:{_sha1(content)}"
            except Exception:
                pass

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

            # Chunks have no individual sha guards (spec §3.8 "チャンク側は個別の
            # sha ガードを付けず") — just delete parent, docsha:, and all chunks.
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
        if not self._client.exists(mem_key):
            return {"status": "not_found", "id": mem_id}

        sha_key = None
        try:
            raw_content = self._client.hget(mem_key, b"content")
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
