"""
Redis-backed storage layer for the N3MemoryCore MCP **Trial** server.

The Trial variant is deliberately ephemeral:
  - Every entry is written with a 24h (configurable) TTL.
  - No SQLite file, no migrations, no integrity checks.
  - Redis Stack (RediSearch + RedisJSON) is required — the user runs
    ``docker run -p 6379:6379 redis/redis-stack-server:latest``.

Data layout
-----------
  mem:<uuid>                HASH   the memory record
      id              string      (same uuid)
      content         string      original text
      timestamp       string      ISO-8601 UTC
      timestamp_epoch number      unix seconds (sortable)
      owner_id        string      user/tenant uuid
      local_id        string      install uuid
      agent_id        string      "claude" / "user" / ""
      session_id      string      per-process uuid
      embedding       bytes       float32 * 768 (little-endian)

  mem:sha:<sha1>            STRING value = mem id   (exact-duplicate guard,
                                                     same TTL)

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
VECTOR_DIM = 768

# RediSearch query special characters that must be escaped in user input.
# https://redis.io/docs/interact/search-and-query/advanced-concepts/escaping/
_FTS_SPECIAL_RE = re.compile(r'([,\.<>\{\}\[\]"\':;!@#\$%\^&\*\(\)\-\+=~\|\\/?])')

# A soft punctuation stripper for the user's query text (keeps letters /
# digits / whitespace / CJK). We strip first and escape what remains.
_PUNCT_STRIP_RE = re.compile(r'[,\.<>\{\}\[\]"\':;!@#\$%\^&\*\(\)\-\+=~\|\\/?`]')


# ---------------------------------------------------------------------------
# Client / index setup
# ---------------------------------------------------------------------------
def get_redis_client(url: str) -> Redis:
    """Construct a Redis client for the given URL (decode_responses=False)."""
    # decode_responses=False because embeddings are raw bytes.
    return Redis.from_url(url, decode_responses=False)


def ensure_index(client: Redis) -> None:
    """Create the RediSearch index if it does not already exist."""
    schema = (
        TextField("content", weight=1.0),
        NumericField("timestamp_epoch", sortable=True),
        TagField("owner_id"),
        TagField("local_id"),
        TagField("agent_id"),
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
    )
    definition = IndexDefinition(prefix=[KEY_PREFIX], index_type=IndexType.HASH)
    try:
        client.ft(INDEX_NAME).create_index(schema, definition=definition)
    except ResponseError as e:
        if "already exists" in str(e).lower():
            return
        raise


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
    agent_id: Optional[str] = None,
    session_id: Optional[str] = None,
    ttl_seconds: int = 86400,
) -> None:
    """Insert a memory record with TTL. Embedding is required for vector search."""
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
        "timestamp": timestamp,
        "timestamp_epoch": ts_epoch,
        "owner_id": owner_id or "",
        "local_id": local_id or "",
        "agent_id": agent_id or "",
        "session_id": session_id or "",
    }
    if embedding is not None:
        mapping["embedding"] = vector_to_bytes(embedding)

    key = f"{KEY_PREFIX}{record_id}"
    pipe = client.pipeline()
    pipe.hset(key, mapping=mapping)
    pipe.expire(key, ttl_seconds)

    sha_key = f"{HASH_PREFIX}{sha1_of(content)}"
    pipe.set(sha_key, record_id, ex=ttl_seconds)
    pipe.execute()


def delete_memory(client: Redis, record_id: str) -> bool:
    """Delete a memory record and its sha1 guard. Returns True if it existed."""
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


def check_exact_duplicate(client: Redis, content: str) -> bool:
    """O(1) exact-content duplicate check."""
    return bool(client.exists(f"{HASH_PREFIX}{sha1_of(content)}"))


def count_memories(client: Redis, owner_id: Optional[str] = None) -> int:
    """Total memory count (optionally filtered by owner)."""
    q_str = "*"
    if owner_id:
        q_str = f"@owner_id:{{{_escape_tag(owner_id)}}}"
    q = Query(q_str).no_content().paging(0, 0).dialect(2)
    res = client.ft(INDEX_NAME).search(q)
    return int(res.total)


def get_all_memories(
    client: Redis,
    limit: int = 20,
    owner_id: Optional[str] = None,
) -> list[dict]:
    """Return the most recent memories, newest first."""
    q_str = "*"
    if owner_id:
        q_str = f"@owner_id:{{{_escape_tag(owner_id)}}}"
    q = (
        Query(q_str)
        .sort_by("timestamp_epoch", asc=False)
        .return_fields("id", "content", "timestamp", "agent_id")
        .paging(0, limit)
        .dialect(2)
    )
    res = client.ft(INDEX_NAME).search(q)
    rows: list[dict] = []
    for doc in res.docs:
        rows.append({
            "id": _strip_key_prefix(_as_str(getattr(doc, "id", ""))),
            "content": _as_str(getattr(doc, "content", "")),
            "timestamp": _as_str(getattr(doc, "timestamp", "")),
            "agent_id": _as_str(getattr(doc, "agent_id", "")),
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
    BM25 text search over the ``content`` field.

    Returns list of (record_id, bm25_score). Higher score = more relevant
    (RediSearch BM25 scores are already non-negative).
    """
    cleaned = strip_fts_punctuation(query).strip()
    if not cleaned:
        return []
    escaped = _escape_redis_query(cleaned)

    if owner_id:
        q_str = f"(@content:({escaped}) @owner_id:{{{_escape_tag(owner_id)}}})"
    else:
        q_str = f"@content:({escaped})"

    q = (
        Query(q_str)
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
