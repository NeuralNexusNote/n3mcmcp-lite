"""
Redis-backed storage layer for the N3MemoryCore MCP **Lite** server.

The Lite variant is deliberately ephemeral:
  - Every entry is written with a 7d (configurable) TTL.
  - No SQLite file, no migrations, no integrity checks.
  - Redis Stack (RediSearch + RedisJSON) is required. First install:
    ``docker run -d --name redis-stack -p 6379:6379 redis/redis-stack-server:latest --appendonly yes``.
    Subsequent runs (container already created): ``docker start redis-stack``.

Data layout
-----------
  mem:<uuid>                HASH   the memory record (or chunk)
      id              string      (same uuid)
      content         string      original text (chunk text if chunked)
      timestamp       string      ISO-8601 UTC
      timestamp_epoch number      unix seconds (sortable)
      owner_id        string      user/tenant uuid
      local_id        string      install uuid
      agent_name        string      "claude" / "user" / ""
      session_id      string      per-process uuid
      parent_id       string      doc id if this row is a chunk of a parent
                                  document, else ""
      embedding       bytes       float32 * 768 (little-endian)

  mem:sha:<sha1>            STRING value = mem id   (exact-duplicate guard,
                                                     same TTL)

  doc:<uuid>                HASH   parent document (full verbatim text);
                                   NOT in the RediSearch index
      id, content, timestamp, timestamp_epoch, owner_id, local_id,
      agent_name, session_id, chunk_count

  docsha:<sha1>             STRING value = doc id   (parent-level exact
                                                     duplicate guard)

  n3mc_idx                  RediSearch index over the mem:* prefix

Both keys are deleted automatically by Redis once their TTL expires; no
background cleanup is required.
"""
from __future__ import annotations

import hashlib
import re
import struct
import time
from datetime import datetime, timezone
from typing import Any, Optional

from redis import Redis
from redis.commands.search.field import (
    NumericField,
    TagField,
    TextField,
    VectorField,
)
try:  # redis-py >= 6: snake_case module
    from redis.commands.search.index_definition import IndexDefinition, IndexType
except ImportError:  # redis-py <= 5: legacy camelCase module
    from redis.commands.search.indexDefinition import IndexDefinition, IndexType  # type: ignore
from redis.commands.search.query import Query
from redis.exceptions import ResponseError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
INDEX_NAME = "n3mc_idx"
KEY_PREFIX = "mem:"
HASH_PREFIX = "mem:sha:"
DOC_KEY_PREFIX = "doc:"
DOC_SHA_PREFIX = "docsha:"
VECTOR_DIM = 768

# RediSearch query special characters that must be escaped in user input.
# https://redis.io/docs/interact/search-and-query/advanced-concepts/escaping/
_FTS_SPECIAL_RE = re.compile(r'([,\.<>\{\}\[\]"\':;!@#\$%\^&\*\(\)\-\+=~\|\\/?])')

# A soft punctuation stripper for the user's query text (keeps letters /
# digits / whitespace / CJK). We strip first and escape what remains.
_PUNCT_STRIP_RE = re.compile(r'[,\.<>\{\}\[\]"\':;!@#\$%\^&\*\(\)\-\+=~\|\\/?`]')

# CJK Unified Ideographs + common CJK extension blocks.
_CJK_RUN_RE = re.compile(r'[\u3000-\u9fff\uf900-\ufaff\ufe30-\ufe4f]+')


# ---------------------------------------------------------------------------
# Client / index setup
# ---------------------------------------------------------------------------
def get_redis_client(url: str) -> Redis:
    """Construct a Redis client for the given URL (decode_responses=False)."""
    # decode_responses=False because embeddings are raw bytes.
    return Redis.from_url(url, decode_responses=False)


def ensure_index(client: Redis) -> None:
    """Create the RediSearch index if it does not already exist.

    When the index already exists, attempt to add newer optional fields
    (content_ngram, importance) via FT.ALTER so existing deployments get
    the upgrade without a full drop/recreate.
    """
    _NEW_FIELDS = [
        TextField("content_ngram", weight=0.8),
        NumericField("importance"),
        NumericField("access_count"),
        TagField("parent_id"),
    ]
    schema = (
        TextField("content", weight=1.0),
        NumericField("timestamp_epoch", sortable=True),
        TagField("owner_id"),
        TagField("local_id"),
        TagField("agent_name"),
        TagField("session_id"),
        VectorField(
            "embedding",
            "FLAT",
            {
                "TYPE": "FLOAT32",
                "DIM": VECTOR_DIM,
                "DISTANCE_METRIC": "COSINE",
            },
        ),
        *_NEW_FIELDS,
    )
    definition = IndexDefinition(prefix=[KEY_PREFIX], index_type=IndexType.HASH)
    try:
        client.ft(INDEX_NAME).create_index(schema, definition=definition)
    except ResponseError as e:
        if "already exists" not in str(e).lower():
            raise
        # Index already exists — try to add the new fields (idempotent).
        try:
            client.ft(INDEX_NAME).alter_schema_add(_NEW_FIELDS)
        except (ResponseError, Exception):
            pass  # fields already present or FT.ALTER unsupported


def ping(client: Redis) -> bool:
    try:
        return bool(client.ping())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Vector serialization
# ---------------------------------------------------------------------------
def vector_to_bytes(vec: list[float]) -> bytes:
    """Pack a Python float list as little-endian FLOAT32 bytes."""
    return struct.pack(f"<{len(vec)}f", *vec)


def bytes_to_vector(b: bytes) -> list[float]:
    n = len(b) // 4
    return list(struct.unpack(f"<{n}f", b))


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
def strip_fts_punctuation(text: str) -> str:
    """Collapse punctuation for BM25-style tokenization."""
    return _PUNCT_STRIP_RE.sub(" ", text)


def _escape_redis_query(text: str) -> str:
    """Backslash-escape RediSearch special chars in a user query string."""
    return _FTS_SPECIAL_RE.sub(r"\\\1", text)


def _escape_tag(value: str) -> str:
    """Escape a TAG value for use inside ``@field:{value}`` expressions."""
    return re.sub(
        r'([ ,\.<>\{\}\[\]"\':;!@#\$%\^&\*\(\)\-\+=~\|\\/?])', r"\\\1", value
    )


def cjk_bigram_expand(text: str) -> str:
    """Expand contiguous CJK runs into overlapping bigrams separated by spaces.

    Example: "記憶装置" → "記憶 憶装 装置"
    Non-CJK portions pass through unchanged. Single CJK chars are kept as-is.
    This lets RediSearch's whitespace tokenizer index each bigram individually,
    dramatically improving BM25 recall for Japanese / Chinese queries.
    """
    def _expand(m: re.Match) -> str:
        run = m.group(0)
        if len(run) < 2:
            return run
        return " ".join(run[i : i + 2] for i in range(len(run) - 1))

    return _CJK_RUN_RE.sub(_expand, text)


def sha1_of(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Core CRUD
# ---------------------------------------------------------------------------
def insert_memory(
    client: Redis,
    record_id: str,
    content: str,
    timestamp: str,
    owner_id: str,
    embedding: Optional[list[float]],
    local_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    session_id: Optional[str] = None,
    ttl_seconds: int = 604800,
    importance: float = 1.0,
    parent_id: Optional[str] = None,
    skip_sha_guard: bool = False,
) -> None:
    """Insert a memory record with TTL. Embedding is required for vector search.

    When ``parent_id`` is provided, this row is a chunk belonging to a parent
    document (see ``insert_parent_doc``). Chunks typically pass
    ``skip_sha_guard=True`` since the parent's sha guard already dedupes the
    full text and overlapping chunks would collide pointlessly.
    """
    try:
        ts_dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if ts_dt.tzinfo is None:
            ts_dt = ts_dt.replace(tzinfo=timezone.utc)
        ts_epoch = ts_dt.timestamp()
    except Exception:
        ts_epoch = time.time()

    mapping: dict[str, Any] = {
        "id": record_id,
        "content": content,
        "content_ngram": cjk_bigram_expand(content),
        "timestamp": timestamp,
        "timestamp_epoch": ts_epoch,
        "owner_id": owner_id or "",
        "local_id": local_id or "",
        "agent_name": agent_name or "",
        "session_id": session_id or "",
        "importance": max(0.5, min(2.0, float(importance))),
        "access_count": 0,
        "parent_id": parent_id or "",
    }
    if embedding is not None:
        mapping["embedding"] = vector_to_bytes(embedding)

    key = f"{KEY_PREFIX}{record_id}"
    pipe = client.pipeline()
    pipe.hset(key, mapping=mapping)
    pipe.expire(key, ttl_seconds)

    if not skip_sha_guard:
        sha_key = f"{HASH_PREFIX}{sha1_of(content)}"
        pipe.set(sha_key, record_id, ex=ttl_seconds)
    pipe.execute()


def delete_memory(client: Redis, record_id: str) -> bool:
    """Delete a memory record and its sha1 guard. Returns True if it existed.

    If ``record_id`` names a parent document (``doc:<id>``), cascades to all
    chunks whose ``parent_id`` matches. Chunks can still be deleted
    individually by passing a chunk's own id.
    """
    # Parent-document path: cascade to all chunks.
    doc_key = f"{DOC_KEY_PREFIX}{record_id}"
    if client.exists(doc_key):
        return delete_parent_doc(client, record_id)

    key = f"{KEY_PREFIX}{record_id}"
    content = client.hget(key, "content")
    if content is None:
        return False
    pipe = client.pipeline()
    pipe.delete(key)
    if isinstance(content, bytes):
        try:
            content_str = content.decode("utf-8")
        except Exception:
            content_str = ""
    else:
        content_str = content
    if content_str:
        pipe.delete(f"{HASH_PREFIX}{sha1_of(content_str)}")
    results = pipe.execute()
    return bool(results and results[0])


# ---------------------------------------------------------------------------
# Parent-document helpers (Option A: full-text recall)
# ---------------------------------------------------------------------------
def check_parent_exact_duplicate(client: Redis, content: str) -> Optional[str]:
    """Return the parent doc id if an identical full-text doc already exists."""
    val = client.get(f"{DOC_SHA_PREFIX}{sha1_of(content)}")
    if val is None:
        return None
    return val.decode("utf-8") if isinstance(val, bytes) else str(val)


def insert_parent_doc(
    client: Redis,
    doc_id: str,
    content: str,
    timestamp: str,
    owner_id: str,
    local_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    session_id: Optional[str] = None,
    chunk_count: int = 0,
    ttl_seconds: int = 604800,
) -> None:
    """Insert a parent full-text document. Not indexed by RediSearch."""
    try:
        ts_dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if ts_dt.tzinfo is None:
            ts_dt = ts_dt.replace(tzinfo=timezone.utc)
        ts_epoch = ts_dt.timestamp()
    except Exception:
        ts_epoch = time.time()

    mapping: dict[str, Any] = {
        "id": doc_id,
        "content": content,
        "timestamp": timestamp,
        "timestamp_epoch": ts_epoch,
        "owner_id": owner_id or "",
        "local_id": local_id or "",
        "agent_name": agent_name or "",
        "session_id": session_id or "",
        "chunk_count": int(chunk_count),
    }
    key = f"{DOC_KEY_PREFIX}{doc_id}"
    pipe = client.pipeline()
    pipe.hset(key, mapping=mapping)
    pipe.expire(key, ttl_seconds)
    pipe.set(f"{DOC_SHA_PREFIX}{sha1_of(content)}", doc_id, ex=ttl_seconds)
    pipe.execute()


def get_parent_doc(client: Redis, doc_id: str) -> Optional[dict]:
    """Fetch a parent document by id. Returns None if missing."""
    key = f"{DOC_KEY_PREFIX}{doc_id}"
    raw = client.hgetall(key)
    if not raw:
        return None
    out: dict[str, Any] = {}
    for k, v in raw.items():
        k_s = k.decode("utf-8") if isinstance(k, bytes) else k
        if isinstance(v, bytes):
            try:
                out[k_s] = v.decode("utf-8")
            except Exception:
                out[k_s] = ""
        else:
            out[k_s] = v
    return out


def _find_chunk_ids_for_parent(
    client: Redis, parent_id: str, owner_id: Optional[str] = None
) -> list[str]:
    """Return all chunk record ids whose parent_id matches."""
    parent_tag = _escape_tag(parent_id)
    owner_filter = f" @owner_id:{{{_escape_tag(owner_id)}}}" if owner_id else ""
    q_str = f"@parent_id:{{{parent_tag}}}{owner_filter}"
    q = Query(q_str).return_fields("id").paging(0, 1000).dialect(2)
    try:
        res = client.ft(INDEX_NAME).search(q)
    except ResponseError:
        return []
    out: list[str] = []
    for doc in res.docs:
        rid = _strip_key_prefix(_as_str(getattr(doc, "id", "")))
        if rid:
            out.append(rid)
    return out


def delete_parent_doc(client: Redis, doc_id: str) -> bool:
    """Delete a parent doc and cascade to all its chunks. Returns True on hit."""
    doc_key = f"{DOC_KEY_PREFIX}{doc_id}"
    content = client.hget(doc_key, "content")
    if content is None:
        return False
    content_str = content.decode("utf-8") if isinstance(content, bytes) else str(content)

    chunk_ids = _find_chunk_ids_for_parent(client, doc_id)

    pipe = client.pipeline()
    pipe.delete(doc_key)
    if content_str:
        pipe.delete(f"{DOC_SHA_PREFIX}{sha1_of(content_str)}")
    for cid in chunk_ids:
        pipe.delete(f"{KEY_PREFIX}{cid}")
    pipe.execute()
    return True


def list_parent_docs(
    client: Redis, limit: int = 20, owner_id: Optional[str] = None
) -> list[dict]:
    """Return recent parent docs, newest first. Owner-scoped if provided."""
    rows: list[dict] = []
    cursor = 0
    match = f"{DOC_KEY_PREFIX}*"
    scanned = 0
    while True:
        cursor, keys = client.scan(cursor=cursor, match=match, count=200)
        for raw_key in keys:
            scanned += 1
            if scanned > 5000:
                break
            key = raw_key.decode("utf-8") if isinstance(raw_key, bytes) else raw_key
            # Pull only the fields we need to rank/display.
            data = client.hmget(
                key,
                "id",
                "content",
                "timestamp",
                "timestamp_epoch",
                "owner_id",
                "agent_name",
                "chunk_count",
            )
            if not data or data[0] is None:
                continue
            doc_id, content, ts, ts_epoch, owner, agent, chunk_count = (
                _as_str(data[0]),
                _as_str(data[1]),
                _as_str(data[2]),
                _as_str(data[3]),
                _as_str(data[4]),
                _as_str(data[5]),
                _as_str(data[6]),
            )
            if owner_id and owner != owner_id:
                continue
            try:
                ts_sort = float(ts_epoch)
            except Exception:
                ts_sort = 0.0
            rows.append({
                "id": doc_id,
                "content": content,
                "timestamp": ts,
                "timestamp_epoch": ts_sort,
                "agent_name": agent,
                "chunk_count": int(chunk_count) if chunk_count.isdigit() else 0,
                "is_parent": True,
            })
        if cursor == 0 or scanned > 5000:
            break

    rows.sort(key=lambda r: r.get("timestamp_epoch", 0.0), reverse=True)
    return rows[:limit]


def count_parent_docs(client: Redis, owner_id: Optional[str] = None) -> int:
    """Best-effort count of parent docs (owner-scoped if provided)."""
    cursor = 0
    match = f"{DOC_KEY_PREFIX}*"
    total = 0
    scanned = 0
    while True:
        cursor, keys = client.scan(cursor=cursor, match=match, count=500)
        for raw_key in keys:
            scanned += 1
            if scanned > 10000:
                break
            if owner_id is None:
                total += 1
                continue
            owner = client.hget(raw_key, "owner_id")
            owner_s = owner.decode("utf-8") if isinstance(owner, bytes) else (owner or "")
            if owner_s == owner_id:
                total += 1
        if cursor == 0 or scanned > 10000:
            break
    return total


def check_exact_duplicate(client: Redis, content: str) -> bool:
    """O(1) exact-content duplicate check."""
    return bool(client.exists(f"{HASH_PREFIX}{sha1_of(content)}"))


def increment_access_counts(
    client: Redis,
    record_ids: list[str],
    ttl_seconds: Optional[int] = None,
) -> None:
    """Atomically HINCRBY access_count for each id in a single pipeline.

    When ``ttl_seconds`` is provided, also refreshes each key's TTL so the
    access-count update and TTL bump ride the same round-trip.
    Silently ignores missing keys (already expired).
    """
    if not record_ids:
        return
    pipe = client.pipeline()
    for rid in record_ids:
        key = f"{KEY_PREFIX}{rid}"
        pipe.hincrby(key, "access_count", 1)
        if ttl_seconds is not None:
            pipe.expire(key, ttl_seconds)
    try:
        pipe.execute()
    except Exception:
        pass  # non-critical: access count is a best-effort signal


def count_memories(
    client: Redis,
    owner_id: Optional[str] = None,
    exclude_chunks: bool = True,
) -> int:
    """Total memory count (optionally filtered by owner).

    When ``exclude_chunks`` is True (default), rows that belong to a parent
    document are not counted — they are represented by the parent instead.
    """
    parts: list[str] = []
    if owner_id:
        parts.append(f"@owner_id:{{{_escape_tag(owner_id)}}}")
    if exclude_chunks:
        parts.append("-@parent_id:{*}")
    q_str = " ".join(parts) if parts else "*"
    q = Query(q_str).no_content().paging(0, 0).dialect(2)
    try:
        res = client.ft(INDEX_NAME).search(q)
        return int(res.total)
    except ResponseError:
        # Index may not yet have parent_id field — fall back to unfiltered.
        q2_str = f"@owner_id:{{{_escape_tag(owner_id)}}}" if owner_id else "*"
        res = client.ft(INDEX_NAME).search(Query(q2_str).no_content().paging(0, 0).dialect(2))
        return int(res.total)


def get_all_memories(
    client: Redis,
    limit: int = 20,
    owner_id: Optional[str] = None,
    exclude_chunks: bool = True,
) -> list[dict]:
    """Return the most recent memories, newest first. Chunks are excluded by default."""
    parts: list[str] = []
    if owner_id:
        parts.append(f"@owner_id:{{{_escape_tag(owner_id)}}}")
    if exclude_chunks:
        parts.append("-@parent_id:{*}")
    q_str = " ".join(parts) if parts else "*"
    q = (
        Query(q_str)
        .sort_by("timestamp_epoch", asc=False)
        .return_fields("id", "content", "timestamp", "agent_name")
        .paging(0, limit)
        .dialect(2)
    )
    try:
        res = client.ft(INDEX_NAME).search(q)
    except ResponseError:
        # Fallback: old index without parent_id field.
        q2_str = f"@owner_id:{{{_escape_tag(owner_id)}}}" if owner_id else "*"
        q2 = (
            Query(q2_str)
            .sort_by("timestamp_epoch", asc=False)
            .return_fields("id", "content", "timestamp", "agent_name")
            .paging(0, limit)
            .dialect(2)
        )
        res = client.ft(INDEX_NAME).search(q2)
    rows: list[dict] = []
    for doc in res.docs:
        rows.append({
            "id": _strip_key_prefix(_as_str(getattr(doc, "id", ""))),
            "content": _as_str(getattr(doc, "content", "")),
            "timestamp": _as_str(getattr(doc, "timestamp", "")),
            "agent_name": _as_str(getattr(doc, "agent_name", "")),
        })
    return rows


def get_memory_by_id(client: Redis, record_id: str) -> Optional[dict]:
    """Fetch a single memory record by id (HGETALL; embedding stripped)."""
    key = f"{KEY_PREFIX}{record_id}"
    raw = client.hgetall(key)
    if not raw:
        return None
    out: dict[str, Any] = {}
    for k, v in raw.items():
        k_s = k.decode("utf-8") if isinstance(k, bytes) else k
        if k_s == "embedding":
            continue
        if isinstance(v, bytes):
            try:
                out[k_s] = v.decode("utf-8")
            except Exception:
                out[k_s] = ""
        else:
            out[k_s] = v
    return out


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
def search_vector(
    client: Redis,
    query_vec: list[float],
    k: int = 50,
    owner_id: Optional[str] = None,
) -> list[tuple[str, float]]:
    """
    KNN vector search. Returns list of (record_id, cosine_distance).

    Cosine distance is in [0, 2]; cosine similarity = 1.0 - distance.
    """
    prefilter = "*"
    if owner_id:
        prefilter = f"@owner_id:{{{_escape_tag(owner_id)}}}"

    q = (
        Query(f"({prefilter})=>[KNN {k} @embedding $vec AS vector_distance]")
        .sort_by("vector_distance")
        .return_fields("id", "vector_distance")
        .paging(0, k)
        .dialect(2)
    )
    try:
        res = client.ft(INDEX_NAME).search(
            q, query_params={"vec": vector_to_bytes(query_vec)}
        )
    except ResponseError:
        return []

    out: list[tuple[str, float]] = []
    for doc in res.docs:
        rid = _strip_key_prefix(_as_str(getattr(doc, "id", "")))
        dist_raw = getattr(doc, "vector_distance", None)
        try:
            dist = float(_as_str(dist_raw)) if dist_raw is not None else 2.0
        except Exception:
            dist = 2.0
        if rid:
            out.append((rid, dist))
    return out


def search_fts(
    client: Redis,
    query: str,
    limit: int = 50,
    owner_id: Optional[str] = None,
) -> list[tuple[str, float]]:
    """
    BM25 text search over ``content`` and ``content_ngram`` fields.

    Queries are CJK-expanded before matching so that Japanese/Chinese text
    benefits from bigram tokenization. Falls back to content-only if the
    content_ngram field is not yet present in the index.

    Returns list of (record_id, bm25_score). Higher score = more relevant.
    """
    cleaned = strip_fts_punctuation(query).strip()
    if not cleaned:
        return []
    expanded = cjk_bigram_expand(cleaned)
    escaped = _escape_redis_query(expanded)

    owner_filter = f" @owner_id:{{{_escape_tag(owner_id)}}}" if owner_id else ""
    q_str = f"(@content:({escaped}) | @content_ngram:({escaped})){owner_filter}"

    def _run(query_str: str) -> list[tuple[str, float]]:
        q = (
            Query(query_str)
            .scorer("BM25")
            .with_scores()
            .return_fields("id")
            .paging(0, limit)
            .dialect(2)
        )
        try:
            res = client.ft(INDEX_NAME).search(q)
        except ResponseError:
            return []
        out: list[tuple[str, float]] = []
        for doc in res.docs:
            rid = _strip_key_prefix(_as_str(getattr(doc, "id", "")))
            score = float(getattr(doc, "score", 0.0) or 0.0)
            if rid:
                out.append((rid, score))
        return out

    results = _run(q_str)
    if not results:
        # Retry with content-only in case content_ngram field is absent.
        fallback = f"@content:({escaped}){owner_filter}"
        results = _run(fallback)
    return results


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _strip_key_prefix(key: str) -> str:
    """Strip the ``mem:`` prefix so callers see bare record ids (uuids)."""
    if key.startswith(KEY_PREFIX):
        return key[len(KEY_PREFIX):]
    return key


def _as_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8")
        except Exception:
            return ""
    return str(v)
