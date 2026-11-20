"""Shared fixtures (spec §9): isolated data folder, HOME, own port, test-only Redis, fake embeddings."""

import hashlib
import json
import os
import pathlib
import socket

import pytest

REDIS_TEST_URL = os.environ.get("N3MC_REDIS_TEST_URL")
TEST_MARKER = "__n3mc_test_db__"
DIM = 768


def _real_redis_urls():
    urls = set()
    if os.environ.get("N3MC_REDIS_URL"):
        urls.add(os.environ["N3MC_REDIS_URL"])
    home = pathlib.Path(os.path.expanduser("~"))
    dd = os.environ.get("N3MC_DATA_DIR")
    p = pathlib.Path(dd) / "config.json" if dd else home / ".n3mc-workingmemory" / "config.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8-sig"))
        if isinstance(d, dict) and isinstance(d.get("redis_url"), str):
            urls.add(d["redis_url"])
    except Exception:  # noqa: BLE001
        pass
    if not urls:
        urls.add("redis://localhost:6379/0")
    return urls


_REAL_URLS = _real_redis_urls()


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


TEST_PORT = _free_port()


def fake_vector(text: str):
    """Deterministic unit vector: words hashed into dimensions (similar texts -> similar vectors)."""
    import numpy as np
    v = np.zeros(DIM, dtype="float32")
    words = text.lower().replace("passage: ", "").replace("query: ", "").split() or [text]
    for w in words:
        h = int(hashlib.md5(w.encode("utf-8")).hexdigest(), 16)
        v[h % DIM] += 1.0
        v[(h >> 16) % DIM] += 0.5
    n = float(np.linalg.norm(v)) or 1.0
    return list(map(float, v / n))


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("N3MC_DATA_DIR", str(data))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("N3MC_REDIS_URL", raising=False)
    monkeypatch.delenv("N3MC_SESSION_ID", raising=False)
    monkeypatch.delenv("N3MC_AGENT_NAME", raising=False)
    cfg = {"redis_url": REDIS_TEST_URL or "redis://127.0.0.1:1/0", "server_port": TEST_PORT}
    (data / "config.json").write_text(json.dumps(cfg), encoding="utf-8")

    from n3mc_mcp import processor as P

    def fake_encode(texts, model_name):
        return [fake_vector(t) for t in texts], None

    monkeypatch.setattr(P, "_encode", fake_encode)
    monkeypatch.setattr(P, "get_model", lambda name, wait=None: (object(), None))
    from n3mc_mcp import cli
    monkeypatch.setattr(cli, "_start_server", lambda cfg, max_wait=60: False)
    monkeypatch.setattr(cli, "_request_shutdown", lambda port, root: (None, None))
    yield tmp_path


def _redis_skip_reason():
    if not REDIS_TEST_URL:
        return "N3MC_REDIS_TEST_URL is not set"
    if REDIS_TEST_URL in _REAL_URLS:
        return "N3MC_REDIS_TEST_URL is the live redis_url; refusing to flush it"
    return None


@pytest.fixture
def rconn():
    reason = _redis_skip_reason()
    if reason:
        pytest.skip(reason)
    import redis
    try:
        r = redis.Redis.from_url(REDIS_TEST_URL, protocol=2)
        r.ping()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"test Redis unreachable: {e}")
    if r.dbsize() and not r.exists(TEST_MARKER):
        pytest.skip("N3MC_REDIS_TEST_URL points at a Redis that holds data and is not marked as a test DB")
    r.flushdb()
    r.set(TEST_MARKER, 1)
    yield r
    if r.exists(TEST_MARKER) or not r.dbsize():
        r.flushdb()
        r.set(TEST_MARKER, 1)
    r.close()


@pytest.fixture
def cfg(isolated):
    from n3mc_mcp import config
    return config.load_config()


@pytest.fixture
def db(rconn, cfg):
    from n3mc_mcp.database import Database
    d = Database(cfg, default_session="sess-test")
    assert d.ensure_connected() == "ok"
    yield d
    if d.client is not None:
        d.client.close()


@pytest.fixture
def runner(db, cfg):
    from n3mc_mcp.tools import ToolRunner
    return ToolRunner(cfg, db)


@pytest.fixture
def client(db, cfg, runner):
    from fastapi.testclient import TestClient
    from n3mc_mcp.app import create_app
    app = create_app(cfg, db, runner)
    with TestClient(app) as c:
        c.headers.update({"Host": f"127.0.0.1:{cfg['server_port']}"})
        yield c
