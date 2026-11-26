"""Shared fixtures (spec §9).

- Every test runs with N3MC_DATA_DIR, HOME and USERPROFILE pointed at temporary folders, so no test
  reads or writes ~/.n3mc-workingmemory or the real ~/.claude/settings.json.
- The temporary config.json holds the test Redis URL and a server_port of this test session (a free
  port taken once; never 18520-18524).
- The HTTP senders of the CLI (_get, _post, _request_shutdown) and the process creation (_popen) are
  replaced by fakes unless a test brings its own server.
- The embedding model is a deterministic stand-in (no download): vectors share a common component, so
  unrelated texts have a cosine of about 0.8 and texts with shared tokens come closer to 1, as with
  multilingual-e5.
- Redis tests need N3MC_REDIS_TEST_URL. There is no default. A URL equal to the live redis_url, or a DB
  that holds data and has no test marker, is refused (the test is skipped with the reason).

monkeypatch.undo() is never called: the real paths would come back in the middle of a test.
"""
import hashlib
import json
import math
import os
import socket
from pathlib import Path
from urllib.parse import urlparse

import pytest

REAL_HOME = Path(os.path.expanduser("~"))
REDIS_TEST_URL = os.environ.get("N3MC_REDIS_TEST_URL", "").strip() or None
LIVE_ENV_REDIS_URL = os.environ.get("N3MC_REDIS_URL", "").strip() or None
TEST_MARKER = "__n3mc_test_db__"
NOT_A_TEST_DB = ("N3MC_REDIS_TEST_URL points at a Redis that holds data and is not marked as a test DB")
UNREACHABLE_URL = "redis://127.0.0.1:1/0"


def norm_redis_url(url):
    try:
        p = urlparse(url)
        host = (p.hostname or "localhost").lower()
        if host in ("localhost", "::1"):
            host = "127.0.0.1"
        path = (p.path or "").strip("/")
        return host, p.port or 6379, int(path) if path else 0
    except Exception:  # noqa: BLE001
        return None


def live_config_redis_url(home: Path = REAL_HOME):
    """redis_url of the user's real config.json (read with utf-8-sig, like the server)."""
    data_dir = os.environ.get("N3MC_DATA_DIR_LIVE") or str(home / ".n3mc-workingmemory")
    p = Path(data_dir) / "config.json"
    try:
        obj = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    url = obj.get("redis_url") if isinstance(obj, dict) else None
    return url if isinstance(url, str) else None


def refusal_reason(test_url, live_urls):
    """Why the test URL must not be used (None when it may)."""
    if not test_url:
        return "N3MC_REDIS_TEST_URL is not set"
    t = norm_redis_url(test_url)
    for live in live_urls:
        if live and norm_redis_url(live) == t:
            return f"N3MC_REDIS_TEST_URL is the live Redis ({live}); refusing to run the Redis tests"
    return None


SAFETY_REASON = refusal_reason(REDIS_TEST_URL, [LIVE_ENV_REDIS_URL, live_config_redis_url()])


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


SESSION_PORT = _free_port()
while SESSION_PORT in (18520, 18521, 18522, 18523, 18524):
    SESSION_PORT = _free_port()


# ── the deterministic embedding ──────────────────────────────────────────────

class FakeModel:
    dim = 768
    COMMON = 2.0

    def __init__(self):
        self.calls = []

    def vector(self, text):
        from n3mc_mcp import processor
        bag = [0.0] * self.dim
        for tok in processor.prepare_query(text).lower().split():
            h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
            bag[1 + h % (self.dim - 1)] += 1.0 if (h >> 40) & 1 else -1.0
        n = math.sqrt(sum(x * x for x in bag)) or 1.0
        v = [b / n for b in bag]
        v[0] = self.COMMON
        m = math.sqrt(sum(x * x for x in v))
        return [x / m for x in v]

    def encode(self, texts, normalize_embeddings=True):
        out = []
        for t in texts:
            self.calls.append(t)
            body = t.split(": ", 1)[1] if t.startswith(("passage: ", "query: ")) else t
            out.append(self.vector(body))
        return out


# ── fake HTTP ────────────────────────────────────────────────────────────────

def no_server(*_a, **_k):
    from n3mc_mcp import cli
    raise cli._NotSent("ConnectionRefusedError: no server in this test")


def no_popen(*_a, **_k):
    raise OSError("process creation is disabled in this test")


def route_http_to(monkeypatch, client, port):
    """Route the CLI's HTTP senders to a FastAPI TestClient for `port`."""
    from n3mc_mcp import cli
    headers = {"Host": f"127.0.0.1:{port}"}

    def _get(p, path, timeout=5):
        if int(p) != int(port):
            raise cli._NotSent("ConnectionRefusedError: nothing on that port")
        r = client.get(path, headers=headers)
        return r.status_code, (r.json() if r.content else {})

    def _post(p, path, body, timeout=30):
        if int(p) != int(port):
            raise cli._NotSent("ConnectionRefusedError: nothing on that port")
        r = client.post(path, content=json.dumps(body), headers=headers)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {}

    def _shutdown(p, root):
        return _post(p, "/shutdown", {"root": str(root)})

    monkeypatch.setattr(cli, "_get", _get)
    monkeypatch.setattr(cli, "_post", _post)
    monkeypatch.setattr(cli, "_request_shutdown", _shutdown)


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def stop_leftover_test_server():
    """Safety net: a server a test started on the session port (data folder under the pytest temp
    folder) is asked to stop when the session ends."""
    yield
    from n3mc_mcp import cli
    try:
        if cli._port_free(SESSION_PORT):
            return
        _s, h = cli._request("GET", SESSION_PORT, "/health")
        root = h.get("root", "")
        if "pytest-of-" in root:
            cli._request("POST", SESSION_PORT, "/shutdown", {"root": root})
            cli._wait_port_free(SESSION_PORT, 15)
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    data = tmp_path / "data"
    home = tmp_path / "home"
    data.mkdir()
    home.mkdir()
    monkeypatch.setenv("N3MC_DATA_DIR", str(data))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for var in ("N3MC_REDIS_URL", "N3MC_SESSION_ID", "N3MC_AGENT_NAME", "CLAUDE_PROJECT_DIR"):
        monkeypatch.delenv(var, raising=False)
    url = REDIS_TEST_URL if (REDIS_TEST_URL and not SAFETY_REASON) else UNREACHABLE_URL
    (data / "config.json").write_text(json.dumps({"redis_url": url, "server_port": SESSION_PORT}),
                                      encoding="utf-8")

    from n3mc_mcp import cli, processor
    fake = FakeModel()
    monkeypatch.setattr(processor, "_model", fake)
    monkeypatch.setattr(processor, "_build_model", lambda name: FakeModel())
    monkeypatch.setattr(processor, "_last_error", None)
    monkeypatch.setattr(cli, "_get", no_server)
    monkeypatch.setattr(cli, "_post", no_server)
    monkeypatch.setattr(cli, "_request_shutdown", no_server)
    monkeypatch.setattr(cli, "_popen", no_popen)
    monkeypatch.setattr(cli, "_FOREIGN_SERVER_ROOT", None)
    monkeypatch.setattr(cli, "_UNAVAILABLE_REASON", None)
    yield {"data": data, "home": home, "model": fake, "url": url}


@pytest.fixture
def fake_model(isolated):
    return isolated["model"]


@pytest.fixture
def cfg(isolated):
    from n3mc_mcp.config import load_config
    return load_config(quiet=True)


def _connect(url):
    import redis
    return redis.Redis.from_url(url, decode_responses=False, socket_connect_timeout=2, socket_timeout=5,
                                protocol=2)


def flush_test_db(client):
    """FLUSHDB only an empty DB or one with the test marker; then set the marker (spec §9)."""
    if client.dbsize() != 0 and not client.exists(TEST_MARKER):
        pytest.skip(NOT_A_TEST_DB)
    client.flushdb()
    client.set(TEST_MARKER, 1)


@pytest.fixture
def rconn(cfg):
    """The flushed test Redis (the deterministic embedding is already in place)."""
    if not REDIS_TEST_URL:
        pytest.skip("N3MC_REDIS_TEST_URL is not set")
    if SAFETY_REASON:
        pytest.skip(SAFETY_REASON)
    client = _connect(REDIS_TEST_URL)
    try:
        client.ping()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"the test Redis is not reachable: {e}")
    flush_test_db(client)
    yield client
    try:
        if client.dbsize() == 0 or client.exists(TEST_MARKER):
            client.flushdb()
            client.set(TEST_MARKER, 1)
    finally:
        client.close()


@pytest.fixture
def db(rconn, cfg):
    from n3mc_mcp.database import Database
    d = Database(cfg)
    assert d.ensure_connected() == "ok"
    yield d
    d.close()


@pytest.fixture
def runner(db, cfg):
    from n3mc_mcp.tools import ToolRunner
    return ToolRunner(cfg, db)


@pytest.fixture
def app_factory(cfg, monkeypatch):
    """Build the FastAPI app with a given Database; the process exit of /shutdown is recorded only."""
    from n3mc_mcp import app as app_mod
    from n3mc_mcp.config import compute_code_version, read_config_raw
    exits = []
    monkeypatch.setattr(app_mod, "_exit_server", lambda: exits.append(1))

    def make(database, **kw):
        cv = kw.pop("code_version", None) or compute_code_version(read_config_raw())
        return app_mod.create_app(cfg, database, code_version=cv, **kw)

    make.exits = exits
    return make


@pytest.fixture
def server_client(app_factory, db, cfg, monkeypatch):
    """A TestClient of the resident server, with the CLI's HTTP senders routed to it."""
    from fastapi.testclient import TestClient
    client = TestClient(app_factory(db))
    route_http_to(monkeypatch, client, cfg["server_port"])
    return client


def parse_json(text):
    return json.JSONDecoder().raw_decode(text)[0]
