import os
import pytest
import redis


REDIS_TEST_URL = os.environ.get("N3MC_REDIS_TEST_URL", "redis://localhost:6379/0")


def _redis_available() -> bool:
    try:
        r = redis.Redis.from_url(REDIS_TEST_URL, decode_responses=False)
        r.ping()
        return True
    except Exception:
        return False


requires_redis = pytest.mark.skipif(
    not _redis_available(), reason="Redis Stack not available"
)


@pytest.fixture
def redis_client():
    r = redis.Redis.from_url(REDIS_TEST_URL, decode_responses=False)
    r.flushdb()
    yield r
    r.flushdb()


@pytest.fixture
def cfg(tmp_path):
    import uuid
    return {
        "owner_id": str(uuid.uuid4()),
        "local_id": str(uuid.uuid4()),
        "_session_id": str(uuid.uuid4()),
        "redis_url": REDIS_TEST_URL,
        "ttl_seconds": 604800,
        "dedup_threshold": 0.95,
        "half_life_days": 3,
        "bm25_min_threshold": 0.1,
        "search_result_limit": 20,
        "context_char_limit": 3000,
        "min_score": 0.0,
        "search_query_max_chars": 2000,
        "chunk_threshold": 400,
        "chunk_overlap": 100,
        "access_count_enabled": True,
        "access_count_weight": 0.02,
        "access_count_max_boost": 0.5,
        "ttl_refresh_on_search": False,
        "ttl_refresh_top_k": 5,
        "lexical_rerank_enabled": False,
        "rerank_weight": 0.3,
        "rerank_phrase_weight": 0.2,
    }


@pytest.fixture
def db(cfg, redis_client):
    from n3mc_mcp.database import Database
    d = Database(cfg)
    d.connect()
    d.ensure_index()
    return d


DUMMY_VEC = bytes([0] * (768 * 4))
