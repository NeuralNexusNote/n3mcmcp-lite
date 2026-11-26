"""Shared fixtures: an isolated data folder and home folder for every test, the test Redis (only
N3MC_REDIS_TEST_URL, no default), deterministic stand-in embeddings, and a fake HTTP layer (spec §9)."""
import hashlib
import json
import math
import os
import socket
import sys

import pytest

REDIS_TEST_URL = os.environ.get("N3MC_REDIS_TEST_URL", "").strip()
MARKER = "__n3mc_test_db__"


def _live_redis_urls():
    """The live redis_url of the user's real config.json (read with utf-8-sig) and N3MC_REDIS_URL,
    read before any test changes HOME."""
    urls = set()
    env = os.environ.get("N3MC_REDIS_URL", "").strip()
    if env:
        urls.add(env)
    data_dir = os.environ.get("N3MC_DATA_DIR", "").strip() or os.path.join(os.path.expanduser("~"),
                                                                           ".n3mc-workingmemory")
    path = os.path.join(data_dir, "config.json")
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        if isinstance(data, dict):
            urls.add(data.get("redis_url") or "redis://localhost:6379/0")
    except (OSError, ValueError):
        urls.add("redis://localhost:6379/0")
    return urls


LIVE_URLS = _live_redis_urls()
REAL_HF_HOME = os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface")


def _norm_url(u):
    return (u or "").strip().rstrip("/")


def test_url_problem(url=REDIS_TEST_URL, live=None):
    """None when url may be used, else the reason to skip."""
    live = LIVE_URLS if live is None else live
    if not url:
        return "N3MC_REDIS_TEST_URL is not set"
    if any(_norm_url(url) == _norm_url(x) for x in live):
        return "N3MC_REDIS_TEST_URL is the live redis_url; refusing to flush it"
    return None


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


TEST_PORT = _free_port()

from n3mc_mcp import cli as _cli  # noqa: E402

REAL_REQUEST = _cli._request
REAL_POPEN = _cli._popen_server


# ── Deterministic stand-in embeddings ────────────────────────────────────────

DIM = 768
BIAS = 2.0   # cos of unrelated texts = BIAS² / (BIAS² + 1) = 0.8 (below vector_min_cos 0.85)


def fake_vector(text):
    from n3mc_mcp.processor import prepare_query
    for p in ("passage: ", "query: "):
        if text.startswith(p):
            text = text[len(p):]
    counts = {}
    for t in prepare_query(text).lower().split():
        h = int(hashlib.md5(t.encode("utf-8")).hexdigest(), 16) % (DIM - 1) + 1
        counts[h] = counts.get(h, 0) + 1
    v = [0.0] * DIM
    norm = math.sqrt(sum(c * c for c in counts.values())) or 1.0
    for h, c in counts.items():
        v[h] = c / norm
    v[0] = BIAS
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def fake_embed_many(texts, kind="passage", no_wait=False):
    return [fake_vector(t) for t in texts]


# ── Isolation of every test ──────────────────────────────────────────────────

class _NoServer:
    """The fake HTTP layer: nothing listens (requests are not sent)."""

    def __call__(self, port, method, path, body=None, timeout=30):
        from n3mc_mcp import cli
        raise cli.NotSent("ConnectionRefusedError: no server in tests")


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch, request):
    data = tmp_path / "data"
    home = tmp_path / "home"
    data.mkdir()
    home.mkdir()
    monkeypatch.setenv("N3MC_DATA_DIR", str(data))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HF_HOME", REAL_HF_HOME)
    for k in ("N3MC_REDIS_URL", "N3MC_SESSION_ID", "N3MC_AGENT_NAME", "CLAUDE_PROJECT_DIR", "HTTP_PROXY",
              "http_proxy"):
        monkeypatch.delenv(k, raising=False)
    url = REDIS_TEST_URL if not test_url_problem() else "redis://127.0.0.1:1/0"
    (data / "config.json").write_text(json.dumps({"redis_url": url, "server_port": TEST_PORT}), encoding="utf-8")
    from n3mc_mcp import cli, processor
    if "real_model" not in request.keywords:
        monkeypatch.setattr(processor, "embed_many", fake_embed_many)
        monkeypatch.setattr(processor, "get_model", lambda no_wait=False: object())
        monkeypatch.setattr(processor, "model_unavailable_message", lambda: None)
        monkeypatch.setattr(processor, "start_background_load", lambda: False)
    if "real_server" not in request.keywords:
        monkeypatch.setattr(cli, "_request", _NoServer())
        monkeypatch.setattr(cli, "_request_shutdown", lambda port, root: "failed")
        monkeypatch.setattr(cli, "_popen_server", _refuse_popen)
    monkeypatch.setattr(cli, "_FOREIGN_SERVER_ROOT", None)
    monkeypatch.setattr(cli, "_LAST_REASON", None)
    yield {"data": data, "home": home}


def _refuse_popen(cfg):
    raise OSError("tests do not start a real server unless marked real_server")


def pytest_configure(config):
    config.addinivalue_line("markers", "real_model: use the real embedding model (no stand-in)")
    config.addinivalue_line("markers", "real_server: the test starts a real server on its own port")
    config.addinivalue_line("markers", "slow: loads the real embedding model")


@pytest.fixture
def cfg():
    from n3mc_mcp.config import load_config
    return load_config()


@pytest.fixture
def rconn():
    """The flushed test Redis (DB 0), with the marker key; skipped without N3MC_REDIS_TEST_URL."""
    problem = test_url_problem()
    if problem:
        pytest.skip(problem)
    import redis
    r = redis.from_url(REDIS_TEST_URL, decode_responses=False, protocol=2, socket_connect_timeout=2,
                       socket_timeout=5)
    try:
        r.ping()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"test Redis not reachable: {e}")
    safe_flush(r)
    yield r
    try:
        safe_flush(r)
    finally:
        r.close()


def safe_flush(r):
    """FLUSHDB only an empty DB or one marked as a test DB; set the marker right after."""
    if r.dbsize() != 0 and not r.exists(MARKER):
        pytest.skip("N3MC_REDIS_TEST_URL points at a Redis that holds data and is not marked as a test DB")
    r.flushdb()
    r.set(MARKER, 1)


@pytest.fixture
def db(rconn, cfg):
    from n3mc_mcp.database import Database
    d = Database(cfg)
    assert d.ensure_connected() == "ok"
    yield d
    if d.client is not None:
        d.client.close()


@pytest.fixture
def runner(cfg, db):
    from n3mc_mcp.tools import ToolRunner
    return ToolRunner(cfg, db)


def parse_json(text):
    return json.JSONDecoder().raw_decode(text)[0]


@pytest.fixture
def server(cfg, db, monkeypatch):
    """The app of app.py behind the fake HTTP layer (a FastAPI TestClient on the test port)."""
    from fastapi.testclient import TestClient
    from n3mc_mcp import app as app_mod, cli
    app = app_mod.create_app(cfg, db, code_version=cli.own_code_version(cfg), port=cfg["server_port"])
    app.state.exit = lambda: None
    client = TestClient(app, base_url=f"http://127.0.0.1:{cfg['server_port']}")
    calls = []

    def fake_request(port, method, path, body=None, timeout=30):
        calls.append((method, path, body))
        if int(port) != int(cfg["server_port"]):
            raise cli.NotSent("no server on that port")
        resp = client.request(method, path, json=body,
                              headers={"Host": f"127.0.0.1:{cfg['server_port']}"})
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, resp.text

    monkeypatch.setattr(cli, "_request", fake_request)
    client.calls = calls
    client.app_obj = app
    return client


def host_headers(port):
    return {"Host": f"127.0.0.1:{port}"}


sys.path.insert(0, os.path.dirname(__file__))
