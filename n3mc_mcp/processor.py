"""
Processing layer: embedding generation, ranking, text purification.

In the Lite (Redis) build, vector distance comes back as **cosine
distance** directly from RediSearch (not L2), so the converter is a trivial
``1 - distance`` — we keep the old name ``cosine_sim_from_l2`` available as
a compatibility shim for anyone importing it, but the preferred name is
``cosine_sim_from_distance``.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from math import pow

from .database import (
    get_memory_by_id,
    search_fts,
    search_vector,
)

# ---------------------------------------------------------------------------
# Embedding model (lazy-loaded singleton)
# ---------------------------------------------------------------------------
_model = None
_MODEL_NAME = "intfloat/e5-base-v2"
_VECTOR_DIM = 768


def get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(_MODEL_NAME)
    return _model


def embed_passage(text: str) -> list:
    """Embed text for storage (document). Adds 'passage: ' prefix."""
    model = get_model()
    vec = model.encode("passage: " + text, normalize_embeddings=True)
    return vec.tolist()


def embed_query(text: str) -> list:
    """Embed text for search (query). Adds 'query: ' prefix."""
    model = get_model()
    vec = model.encode("query: " + text, normalize_embeddings=True)
    return vec.tolist()


# ---------------------------------------------------------------------------
# Text purification
# ---------------------------------------------------------------------------
_CODE_BLOCK_RE = re.compile(r'```[\s\S]*?```', re.MULTILINE)


def purify(text: str) -> str:
    """Replace multi-line code blocks with '[code omitted]'. Keep inline code as-is."""
    return _CODE_BLOCK_RE.sub('[code omitted]', text)


# ---------------------------------------------------------------------------
# Ranking formula components
# ---------------------------------------------------------------------------
def cosine_sim_from_distance(cosine_distance: float) -> float:
    """
    Convert RediSearch cosine distance to cosine similarity.

    RediSearch returns cosine_distance = 1 - cos_sim (range [0, 2]).
    Clamp to [0, 1] since we treat negative similarities as irrelevant.
    """
    return max(0.0, min(1.0, 1.0 - float(cosine_distance)))


# Backwards-compatible alias (some callers still import the old name).
cosine_sim_from_l2 = cosine_sim_from_distance


def time_decay(timestamp_str: str, half_life_days: float) -> float:
    """
    time_decay = 2^(-days_elapsed / half_life_days)
    Returns 1.0 on invalid timestamp.
    """
    try:
        ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        now = datetime.now(tz=timezone.utc)
        days_elapsed = (now - ts).total_seconds() / 86400.0
        return pow(2.0, -days_elapsed / half_life_days)
    except Exception:
        return 1.0


def keyword_relevance(bm25_score: float, max_abs_score: float, bm25_min_threshold: float) -> float:
    """Normalize BM25 score to [0.0, 1.0]."""
    abs_score = abs(bm25_score)
    if abs_score < bm25_min_threshold:
        return 0.0
    return abs_score / max(1.0, max_abs_score)


def final_score(
    cos_sim: float,
    kw_relevance: float,
    decay: float,
    b_local: float = 1.0,
) -> float:
    """
    Final Score = (cos_sim * 0.7 + keyword_relevance * 0.3) * time_decay * b_local
    """
    return (cos_sim * 0.7 + kw_relevance * 0.3) * decay * b_local


# ---------------------------------------------------------------------------
# Hybrid search (vector + BM25)
# ---------------------------------------------------------------------------
def hybrid_search(
    client,
    query: str,
    config: dict,
    k: int = 50,
) -> list:
    """
    Perform hybrid vector + BM25 search against Redis and return ranked results.
    Returns list of dicts: {id, content, score, timestamp}
    """
    half_life_days = config.get("half_life_days", 3)
    bm25_min_threshold = config.get("bm25_min_threshold", 0.1)
    search_result_limit = config.get("search_result_limit", 20)
    min_score = config.get("min_score", 0.2)
    owner_id = config.get("owner_id") or None

    query_vec = embed_query(query)
    vec_results = search_vector(client, query_vec, k=k, owner_id=owner_id)
    vec_map = {rid: dist for rid, dist in vec_results}

    fts_results = search_fts(client, query, limit=k, owner_id=owner_id)
    fts_map = {rid: score for rid, score in fts_results}

    if fts_map:
        max_abs_bm25 = max(abs(s) for s in fts_map.values())
    else:
        max_abs_bm25 = 1.0

    all_ids = set(vec_map.keys()) | set(fts_map.keys())

    results = []
    for rid in all_ids:
        row = get_memory_by_id(client, rid)
        if row is None:
            continue

        cos = cosine_sim_from_distance(vec_map[rid]) if rid in vec_map else 0.0
        kw = (
            keyword_relevance(fts_map[rid], max_abs_bm25, bm25_min_threshold)
            if rid in fts_map
            else 0.0
        )

        ts = row.get("timestamp", "") or ""
        decay = time_decay(ts, half_life_days)

        score = final_score(cos, kw, decay, 1.0)

        if score < min_score:
            continue

        results.append({
            "id": row.get("id", rid),
            "content": row.get("content", ""),
            "score": round(score, 4),
            "timestamp": ts,
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:search_result_limit]
