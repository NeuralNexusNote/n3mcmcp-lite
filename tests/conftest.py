"""Shared fixtures (spec §9).

SAFETY (v1.7.0 / v1.7.1): the suite FLUSHDBs DB 0 of the Redis it uses. There is no
default test URL — Redis tests run only against the dedicated instance named
by N3MC_REDIS_TEST_URL, and never against the live `redis_url` of the user's
config.json / N3MC_REDIS_URL. v1.7.1: before EVERY flush the DB must be
empty or carry the marker key `__n3mc_test_db__` (set right after each
flush); an unmarked DB with data is never flushed. v1.6.x defaulted to redis://localhost:6379/0,
the same Redis the MCP server uses, so a plain `pytest` wiped working memory.
"""
import json
import os
from urllib.parse import urlparse

import pytest
import redis

REDIS_TEST_URL = os.environ.get("N3MC_REDIS_TEST_URL", "").strip()


def _norm_url(url: str) -> tuple:
    u = urlparse(url)
    host = (u.hostname or "localhost").lower()
    if host in ("localhost", "::1", "0.0.0.0"):
        host = "127.0.0.1"
    db = (u.path or "/0").lstrip("/") or "0"
    return (host, u.port or 6379, db)


def _live_redis_urls() -> set:
    """redis_url values the real MCP server may be using."""
    urls = {"redis://localhost:6379/0"}  # the built-in default
    env = os.environ.get("N3MC_REDIS_URL")
    if env:
        urls.add(env)
    try:
        from n3mc_mcp.paths import get_config_path
        path = get_config_path()
        if path.exists():
            # utf-8-sig like the server (v1.7.1): a BOM must not hide redis_url.
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict) and isinstance(data.get("redis_url"), str):
                urls.add(data["redis_url"])
    except Exception:
        pass
    return {_norm_url(u) for u in urls}


# v1.7.1 (Lite review LL6): second guard — the DB itself. Flush only an empty
# DB or one carrying this marker; set the marker right after every flush.
TEST_DB_MARKER = "__n3mc_test_db__"
UNMARKED_REASON = ("N3MC_REDIS_TEST_URL points at a Redis that holds data and "
                   "is not marked as a test DB")


def is_flushable(r) -> bool:
    return r.dbsize() == 0 or bool(r.exists(TEST_DB_MARKER))


def safe_flushdb(r) -> bool:
    """FLUSHDB only if the DB is empty or marked; then set the marker.
    Returns False (and flushes nothing) for an unmarked DB with data."""
    if not is_flushable(r):
        return False
    r.flushdb()
    r.set(TEST_DB_MARKER, b"1")
    return True


def _skip_reason() -> str:
    if not REDIS_TEST_URL:
        return ("N3MC_REDIS_TEST_URL is not set — Redis tests need a DEDICATED "
                "Redis Stack (they FLUSHDB it), see spec §9")
    if _norm_url(REDIS_TEST_URL) in _live_redis_urls():
        return ("refusing to run: N3MC_REDIS_TEST_URL points at the live "
                "redis_url used by the MCP server (tests would FLUSHDB it)")
    try:
        r = redis.Redis.from_url(REDIS_TEST_URL, decode_responses=False)
        r.ping()
        if not is_flushable(r):
            return UNMARKED_REASON
    except Exception as e:
        return f"Redis Stack not reachable at N3MC_REDIS_TEST_URL ({e})"
    return ""


REDIS_SKIP_REASON = _skip_reason()

requires_redis = pytest.mark.skipif(
    bool(REDIS_SKIP_REASON), reason=REDIS_SKIP_REASON or "redis available"
)


@pytest.fixture
def redis_client():
    if REDIS_SKIP_REASON:
        pytest.skip(REDIS_SKIP_REASON)
    r = redis.Redis.from_url(REDIS_TEST_URL, decode_responses=False)
    if not safe_flushdb(r):
        pytest.skip(UNMARKED_REASON)
    yield r
    safe_flushdb(r)


@pytest.fixture
def cfg(tmp_path):
    import uuid
    return {
        "owner_id": str(uuid.uuid4()),
        "local_id": str(uuid.uuid4()),
        "_session_id": str(uuid.uuid4()),
        "redis_url": REDIS_TEST_URL,
        "ttl_seconds": 604800,
        "half_life_days": 3,
        "bm25_min_threshold": 0.1,
        "search_result_limit": 20,
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
        # v1.7.0 shaping off by default in unit fixtures (dummy vectors);
        # tests/test_regressions.py turns each step on explicitly.
        "vector_min_cos": 0.0,
        "relative_min_score_ratio": 0.0,
        "version_order_enabled": False,
    }


@pytest.fixture
def db(cfg, redis_client):
    from n3mc_mcp.database import Database
    d = Database(cfg)
    d.connect()
    d.ensure_index()
    return d


DUMMY_VEC = bytes([0] * (768 * 4))
