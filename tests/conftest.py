"""
N3MemoryCore MCP **Trial** — test fixtures.

These tests require a running Redis Stack instance (RediSearch module).
If Redis is not reachable at ``N3MC_REDIS_TEST_URL`` (or the default
``redis://localhost:6379/0``), the whole test session is skipped.

IMPORTANT: RediSearch requires db 0 (``Cannot create index on db != 0``).
The test suite FLUSHDBs db 0 before and after every test — do **not**
point it at a Redis instance that holds data you care about. Run a
dedicated container for testing.
"""
from __future__ import annotations

import math
import os
import uuid

import pytest


_TEST_REDIS_URL = os.environ.get(
    "N3MC_REDIS_TEST_URL", "redis://localhost:6379/0"
)


def _redis_available(url: str) -> bool:
    try:
        from redis import Redis
        c = Redis.from_url(url, decode_responses=False, socket_connect_timeout=1)
        return bool(c.ping())
    except Exception:
        return False


if not _redis_available(_TEST_REDIS_URL):
    pytest.skip(
        f"Redis Stack not reachable at {_TEST_REDIS_URL}; "
        "start it with `docker run -p 6379:6379 redis/redis-stack-server:latest` "
        "to run the Trial tests.",
        allow_module_level=True,
    )


@pytest.fixture
def redis_client():
    """Yield a Redis client pointed at a flushed test DB with the index created."""
    from n3mc_mcp.database import ensure_index, get_redis_client

    client = get_redis_client(_TEST_REDIS_URL)
    client.flushdb()
    ensure_index(client)
    yield client
    client.flushdb()


@pytest.fixture
def dummy_vec():
    """Deterministic 768-dim unit vector."""
    return [1.0 / math.sqrt(768)] * 768


@pytest.fixture
def base_config():
    """Minimal config for testing."""
    return {
        "owner_id": f"test-owner-{uuid.uuid4()}",
        "local_id": f"test-local-{uuid.uuid4()}",
        "redis_url": _TEST_REDIS_URL,
        "ttl_seconds": 86400,
        "dedup_threshold": 0.95,
        "half_life_days": 90,
        "bm25_min_threshold": 0.1,
        "search_result_limit": 20,
        "context_char_limit": 3000,
        "min_score": 0.0,
        "search_query_max_chars": 2000,
    }


@pytest.fixture(scope="session")
def embedding_model():
    """Load the embedding model once per session (~400MB first-run download)."""
    from n3mc_mcp.processor import get_model
    return get_model()


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    """Point the package at a throw-away config directory."""
    monkeypatch.setenv("N3MC_DATA_DIR", str(tmp_path))
    yield tmp_path
