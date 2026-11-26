"""Shared fixtures (spec §9): an isolated data folder and home folder for every test, the test
Redis (N3MC_REDIS_TEST_URL, no default) with the two safety checks, and a deterministic stand-in
for the embedding model.

Nothing here reads or writes the real ~/.n3mc-workingmemory, ~/.claude or ~/.claude.json: the
autouse fixture points N3MC_DATA_DIR, HOME and USERPROFILE at temporary folders before each test.
monkeypatch.undo() is never called."""
import hashlib
import json
import math
import os
import socket
import uuid

import pytest

REDIS_TEST_URL = os.environ.get("N3MC_REDIS_TEST_URL") or None
TEST_MARK = "__n3mc_test_db__"

# Captured at import, before any fixture changes the environment.
_REAL_HOME = os.path.expanduser("~")
_REAL_DATA_DIR = os.environ.get("N3MC_DATA_DIR") or os.path.join(_REAL_HOME, ".n3mc-workingmemory")
REAL_HF_HOME = os.environ.get("HF_HOME") or os.path.join(_REAL_HOME, ".cache", "huggingface")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_live_redis_url(path):
    """redis_url of a config.json read like the server reads it (utf-8-sig), or None."""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        url = data.get("redis_url") if isinstance(data, dict) else None
        return url if isinstance(url, str) else None
    except (OSError, ValueError):
        return None


def _norm_url(url):
    return (url or "").strip().rstrip("/").lower()


def refusal_reason(test_url, live_urls):
    """Why the suite refuses to use test_url, or None."""
    if not test_url:
        return "N3MC_REDIS_TEST_URL is not set"
    for live in live_urls:
        if live and _norm_url(live) == _norm_url(test_url):
            return f"N3MC_REDIS_TEST_URL is the same as the live redis_url ({live}); refusing to flush it"
    return None


def _live_urls():
    urls = [os.environ.get("N3MC_REDIS_URL")]
    urls.append(read_live_redis_url(os.path.join(_REAL_DATA_DIR, "config.json")))
    return [u for u in urls if u]


LIVE_URLS = _live_urls()
REFUSAL = refusal_reason(REDIS_TEST_URL, LIVE_URLS)


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


TEST_PORT = _free_port()
DEAD_URL = f"redis://127.0.0.1:{_free_port()}/0"   # nothing listens there
assert TEST_PORT not in (18520, 18521, 18522, 18523, 18524)


def fake_embed(text):
    """Deterministic 768-d vector: a shared base direction plus a bag of hashed tokens. Unrelated
    texts land near cos 0.8 (like e5), identical texts at 1.0, overlapping texts in between."""
    body = text.split(": ", 1)[-1]
    from n3mc_mcp import processor as P
    v = [0.0] * 768
    for t in P.prepare_query(body).lower().split():
        h = int(hashlib.md5(t.encode("utf-8")).hexdigest(), 16)
        v[1 + h % 767] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    v = [x / n for x in v]
    v[0] = 2.0
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def write_test_config(data_dir, **extra):
    cfg = {"owner_id": str(uuid.uuid4()), "local_id": str(uuid.uuid4()),
           "redis_url": REDIS_TEST_URL or DEAD_URL, "server_port": TEST_PORT}
    cfg.update(extra)
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return cfg


def _no_server(*_a, **_k):
    raise ConnectionRefusedError("no N3MC Lite server in tests")


from n3mc_mcp import cli as _cli  # noqa: E402

ORIG_START_SERVER = _cli._start_server
START_CALLS = []


def _fake_start_server(cfg, max_wait=60):
    """Tests never start a real server unless they ask for it (they restore ORIG_START_SERVER)."""
    START_CALLS.append(max_wait)
    _cli._LAST_REASON = _cli.reason_starting()
    return False


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    data = tmp_path / "data"
    home = tmp_path / "home"
    data.mkdir()
    home.mkdir()
    monkeypatch.setenv("N3MC_DATA_DIR", str(data))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HF_HOME", REAL_HF_HOME)
    for var in ("N3MC_REDIS_URL", "N3MC_SESSION_ID", "N3MC_AGENT_NAME", "HTTP_PROXY", "HTTPS_PROXY",
                "http_proxy", "https_proxy", "CLAUDE_PROJECT_DIR"):
        monkeypatch.delenv(var, raising=False)
    write_test_config(str(data))
    from n3mc_mcp import cli
    from n3mc_mcp import processor as P
    monkeypatch.setattr(P, "_FAKE_EMBED", fake_embed)
    monkeypatch.setattr(cli, "_get", _no_server)
    monkeypatch.setattr(cli, "_post", _no_server)
    monkeypatch.setattr(cli, "_request_shutdown", lambda *a, **k: False)
    monkeypatch.setattr(cli, "_start_server", _fake_start_server)
    monkeypatch.setattr(cli, "_LAST_REASON", None)
    monkeypatch.setattr(cli, "_FOREIGN_SERVER_ROOT", None)
    yield {"data": data, "home": home, "tmp": tmp_path}


@pytest.fixture
def cfg():
    from n3mc_mcp import config
    return config.load_config()


def _redis_client():
    import redis
    return redis.Redis.from_url(REDIS_TEST_URL, decode_responses=False, protocol=2,
                                socket_connect_timeout=2, socket_timeout=5)


def safe_flush(client):
    """Flush only an empty DB or one marked as a test DB, then mark it (§9, second defence)."""
    if client.dbsize() != 0 and not client.exists(TEST_MARK):
        pytest.skip("N3MC_REDIS_TEST_URL points at a Redis that holds data and is not marked as a test DB")
    client.flushdb()
    client.set(TEST_MARK, 1)


@pytest.fixture
def rconn():
    if REFUSAL:
        pytest.skip(REFUSAL)
    client = _redis_client()
    try:
        client.ping()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"the test Redis is not reachable: {e}")
    safe_flush(client)
    yield client
    try:
        safe_flush(client)
    finally:
        client.close()


@pytest.fixture
def db(cfg, rconn):
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


class AppRouter:
    """Routes the CLI's HTTP senders to an in-process FastAPI TestClient."""

    def __init__(self, app, port=TEST_PORT):
        from fastapi.testclient import TestClient
        self.client = TestClient(app)
        self.port = port
        self.calls = []

    def _headers(self, port):
        return {"Host": f"127.0.0.1:{port or self.port}"}

    def get(self, cfg, path, timeout=5.0, port=None):
        if port not in (None, self.port):
            raise ConnectionRefusedError("other port")
        self.calls.append(("GET", path, None, timeout))
        r = self.client.get(path, headers=self._headers(port))
        return r.status_code, (r.json() if r.content else {})

    def post(self, cfg, path, body, timeout=30.0, port=None):
        if port not in (None, self.port):
            raise ConnectionRefusedError("other port")
        self.calls.append(("POST", path, body, timeout))
        r = self.client.post(path, json=body, headers=self._headers(port))
        return r.status_code, (r.json() if r.content else {})


@pytest.fixture
def app_router(cfg, db, monkeypatch):
    """A real app (fake embeddings, no model load) behind the CLI's _get / _post."""
    from n3mc_mcp import app as app_mod
    from n3mc_mcp import cli, config
    application = app_mod.create_app(cfg, db, code_version=config.compute_code_version(cfg["_raw"]),
                                     load_model=False)
    router = AppRouter(application)
    monkeypatch.setattr(cli, "_get", router.get)
    monkeypatch.setattr(cli, "_post", router.post)
    monkeypatch.setattr(app_mod, "_exit_process", lambda: None)
    router.app = application
    return router
