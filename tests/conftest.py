"""Shared fixtures (spec section 9): isolated data dir and HOME, test-only Redis, fake embeddings.

Nothing here ever touches the user's real ~/.n3mc-workingmemory, ~/.claude/settings.json or the live Redis."""

import hashlib
import json
import math
import os
import socket
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Computed BEFORE any fixture replaces HOME: the live redis_url(s) the tests must never touch.
_REAL_HOME = os.path.expanduser("~")
REDIS_TEST_URL = os.environ.get("N3MC_REDIS_TEST_URL", "")
REAL_HF_HOME = os.environ.get("HF_HOME") or os.path.join(_REAL_HOME, ".cache", "huggingface")


def _live_urls():
    urls = set()
    for env in ("N3MC_REDIS_URL",):
        if os.environ.get(env):
            urls.add(os.environ[env].rstrip("/"))
    cfg = os.path.join(_REAL_HOME, ".n3mc-workingmemory", "config.json")
    try:
        with open(cfg, "r", encoding="utf-8-sig") as f:
            u = json.load(f).get("redis_url")
        if u:
            urls.add(u.rstrip("/"))
    except (OSError, ValueError, AttributeError):
        pass
    return urls


def _same(a, b):
    return a.rstrip("/") == b.rstrip("/")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


_SESSION_PORT = free_port()
DIM = 768


def fake_vector(text):
    """Deterministic bag-of-tokens vector (L2 normalized); similar texts get a high cosine."""
    from n3mc_mcp import processor
    vec = [0.0] * DIM
    toks = processor.lex_tokens(text) or {"_empty_"}
    for t in toks:
        h = int(hashlib.md5(t.encode("utf-8")).hexdigest(), 16)
        vec[h % DIM] += 1.0
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """Isolated data folder + HOME; no network to the real servers; no real server start."""
    data = tmp_path / "data"
    home = tmp_path / "home"
    data.mkdir()
    home.mkdir()
    monkeypatch.setenv("N3MC_DATA_DIR", str(data))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("N3MC_REDIS_URL", raising=False)
    monkeypatch.delenv("N3MC_SESSION_ID", raising=False)
    monkeypatch.delenv("N3MC_AGENT_NAME", raising=False)
    cfg = {"redis_url": REDIS_TEST_URL or "redis://127.0.0.1:1/0", "server_port": _SESSION_PORT}
    (data / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    from n3mc_mcp import cli

    def _no_http(*a, **k):
        raise ConnectionRefusedError("tests: no real server")

    monkeypatch.setattr(cli, "_get", _no_http)
    monkeypatch.setattr(cli, "_post", _no_http)
    monkeypatch.setattr(cli, "_request_shutdown", _no_http)
    monkeypatch.setattr(cli, "_start_server", lambda cfg, max_wait=60: False)
    monkeypatch.setattr(cli, "_FOREIGN_SERVER_ROOT", None)
    monkeypatch.setattr(cli, "_LAST_STATE", "starting")
    yield {"data": str(data), "home": str(home)}


@pytest.fixture
def fake_embed(monkeypatch):
    """Replace the embedding model by deterministic vectors (no model download)."""
    from n3mc_mcp import processor

    def _embed(text, cfg, query=False, no_wait=False):
        return fake_vector(text)

    _embed.real = processor.embed
    monkeypatch.setattr(processor, "embed", _embed)
    return _embed


@pytest.fixture
def no_model(monkeypatch):
    from n3mc_mcp import processor
    monkeypatch.setattr(processor, "embed", lambda text, cfg, query=False, no_wait=False: None)


@pytest.fixture
def redis_client():
    """A flushed, marked test DB; skips unless N3MC_REDIS_TEST_URL points at a safe Redis."""
    if not REDIS_TEST_URL:
        pytest.skip("N3MC_REDIS_TEST_URL is not set")
    for u in _live_urls():
        if _same(u, REDIS_TEST_URL):
            pytest.skip("N3MC_REDIS_TEST_URL is the live redis_url: refusing to run")
    import redis
    r = redis.from_url(REDIS_TEST_URL, decode_responses=False, protocol=2, socket_connect_timeout=2,
                       socket_timeout=5)
    try:
        r.ping()
    except redis.RedisError:
        pytest.skip("the test Redis is not reachable")
    if r.dbsize() != 0 and not r.exists("__n3mc_test_db__"):
        pytest.skip("N3MC_REDIS_TEST_URL points at a Redis that holds data and is not marked as a test DB")
    def reset():
        r.flushdb()
        try:
            r.execute_command("FT.DROPINDEX", "n3mc_idx")     # a fresh index: BM25 statistics do not carry over
        except redis.ResponseError:
            pass
        r.set("__n3mc_test_db__", "1")

    reset()
    yield r
    if r.dbsize() == 0 or r.exists("__n3mc_test_db__"):
        reset()
    r.close()


@pytest.fixture
def rconn(redis_client, fake_embed):
    """The test Redis connection with the deterministic embeddings in place (used by test_cross_session.py)."""
    return redis_client


@pytest.fixture
def cfg(isolate, redis_client):
    from n3mc_mcp import config
    return config.load_config()


@pytest.fixture
def svc(cfg, fake_embed):
    from n3mc_mcp.tools import Service
    s = Service(cfg)
    yield s
    s.db.close()


@pytest.fixture
def db(svc):
    svc.db.ensure_connected()
    return svc.db


@pytest.fixture
def client(svc, cfg):
    """FastAPI TestClient for the resident server app (Host: 127.0.0.1:<port>)."""
    from fastapi.testclient import TestClient
    from n3mc_mcp import app as app_mod
    app = app_mod.create_app(svc, port=cfg["server_port"])
    app.state.exit_fn = lambda: None
    return TestClient(app, base_url="http://127.0.0.1:%d" % cfg["server_port"])


@pytest.fixture
def wired(client, monkeypatch):
    """Route the CLI's HTTP helpers into the TestClient (no sockets, no real server)."""
    from n3mc_mcp import cli

    def route(method, path, body=None):
        r = client.request(method, path, json=body)
        if r.status_code >= 400:
            raise cli.HttpStatusError(r.status_code, r.text)
        return r.json()

    monkeypatch.setattr(cli, "_get", lambda port, path, timeout=5: route("GET", path))
    monkeypatch.setattr(cli, "_post", lambda port, path, body, timeout=30: route("POST", path, body))
    return client


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: tests that load the real embedding model")
