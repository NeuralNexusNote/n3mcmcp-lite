"""Layer 6: the resident server's endpoints and process management (spec §3.15, §4.5).
app.py's TestClient with the fake embedding and a temporary data folder."""
import inspect
import json
import re
import threading
import time

import pytest
from fastapi.testclient import TestClient

import conftest
from conftest import parse_json
from n3mc_mcp import cli, config, lite_hook, lite_stop_hook, paths, processor, tools
from n3mc_mcp.config import compute_code_version, read_config_raw
from n3mc_mcp.database import Database
from n3mc_mcp.tools import ToolRunner

ENDPOINTS = [("get", "/health"), ("post", "/shutdown"), ("post", "/buffer"), ("post", "/search"),
             ("post", "/repair"), ("post", "/tools/list_memories")]


@pytest.fixture
def offline_app(app_factory, cfg):
    """An app whose Database cannot reach Redis (enough for the header checks)."""
    return app_factory(Database(dict(cfg, redis_url=conftest.UNREACHABLE_URL)))


def H(cfg, host=None, **extra):
    h = {"Host": host or f"127.0.0.1:{cfg['server_port']}"}
    h.update(extra)
    return h


class TestHealth:
    def test_fields(self, offline_app, cfg):
        r = TestClient(offline_app).get("/health", headers=H(cfg)).json()
        assert r["status"] == "ok" and paths.same_folder(r["root"], paths.data_dir())
        assert re.fullmatch(r"[0-9a-f]{16}", r["code_version"]) and isinstance(r["pid"], int)
        assert r["redis"] == "127.0.0.1:1/0"

    def test_code_version_follows_config(self):
        a = compute_code_version(b'{"a": 1, "b": 2}')
        assert a == compute_code_version(b'{\n  "b": 2,\n  "a": 1\n}\n')
        assert a != compute_code_version(b'{"a": 1, "b": 3}')
        assert compute_code_version(b"{bad") != compute_code_version(None)


class TestHostHeader:
    @pytest.mark.parametrize("method,path", ENDPOINTS)
    def test_evil_host(self, offline_app, cfg, method, path):
        c = TestClient(offline_app)
        assert getattr(c, method)(path, headers=H(cfg, "evil.example")).status_code == 403

    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
    def test_local_hosts(self, offline_app, cfg, host):
        r = TestClient(offline_app).get("/health", headers=H(cfg, f"{host}:{cfg['server_port']}"))
        assert r.status_code == 200

    @pytest.mark.parametrize("method,path", ENDPOINTS)
    @pytest.mark.parametrize("origin", ["http://evil.example", "null"])
    def test_origin_refused(self, offline_app, cfg, method, path, origin):
        c = TestClient(offline_app)
        assert getattr(c, method)(path, headers=H(cfg, Origin=origin)).status_code == 403

    def test_without_origin_passes(self, offline_app, cfg):
        assert TestClient(offline_app).get("/health", headers=H(cfg)).status_code == 200

    def test_cli_sends_no_origin(self, monkeypatch):
        sent = []

        class Resp:
            status = 200

            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(cli, "_port_free", lambda p: False)
        monkeypatch.setattr(cli._OPENER, "open", lambda req, timeout=5: (sent.append(req), Resp())[1])
        cli._request("POST", 1234, "/search", {"q": 1})
        headers = {k.lower(): v for k, v in sent[0].header_items()}
        assert "origin" not in headers and headers["host"] == "127.0.0.1:1234"


class TestPlainDef:
    def test_plain_def(self, offline_app):
        eps = {r.path: r.endpoint for r in offline_app.routes if hasattr(r, "endpoint")}
        for path in ("/buffer", "/search", "/repair", "/tools/{name}"):
            assert not inspect.iscoroutinefunction(eps[path]), path


class TestServerProcessEdgeCases:
    def test_server_on_other_port_is_stopped(self, monkeypatch, cfg):
        other = cfg["server_port"] + 7
        root = str(paths.data_dir())
        stops, starts = [], []
        monkeypatch.setattr(cli, "_health", lambda p: {"status": "ok", "root": root} if p == other else None)
        monkeypatch.setattr(cli, "_pid_state", lambda: ("own", 4242))
        monkeypatch.setattr(cli, "_listening_ports", lambda pid: {other})
        monkeypatch.setattr(cli, "_request_shutdown", lambda p, r: (stops.append(p), (200, {}))[1])
        monkeypatch.setattr(cli, "_wait_port_free", lambda p, t=15: True)
        monkeypatch.setattr(cli, "_start_server", lambda c, w=60: (starts.append(1), False)[1])
        cli.ensure_server(cfg, max_wait=0)
        assert stops == [other] and starts == [1]

    def test_foreign_server_on_other_port_untouched(self, monkeypatch, cfg):
        other = cfg["server_port"] + 7
        stops = []
        monkeypatch.setattr(cli, "_health", lambda p: {"status": "ok", "root": "/elsewhere"} if p == other else None)
        monkeypatch.setattr(cli, "_listening_ports", lambda pid: {other})
        monkeypatch.setattr(cli, "_request_shutdown", lambda p, r: stops.append(p))
        assert cli._handle_other_port(4242, cfg) is False and stops == []

    @pytest.mark.parametrize("exc", [SystemExit(1), RuntimeError("bind failed")])
    def test_run_server_failure_recorded(self, monkeypatch, cfg, exc):
        def boom(*a, **k):
            raise exc

        monkeypatch.setattr(processor, "import_backend", lambda: None)
        monkeypatch.setattr(Database, "ensure_connected", lambda self, enforce=True: "ok")
        import uvicorn
        monkeypatch.setattr(uvicorn, "run", boom)
        with pytest.raises(type(exc)):
            cli.run_server()
        assert paths.start_failed_path().exists()
        assert "server_start_failed" in json.loads(paths.notices_path().read_text(encoding="utf-8"))
        starts = []
        monkeypatch.setattr(cli, "_popen", lambda *a, **k: starts.append(1))
        assert cli.ensure_server(cfg, max_wait=0) is False and starts == []

    def test_run_server_normal_end(self, monkeypatch):
        monkeypatch.setattr(processor, "import_backend", lambda: None)
        monkeypatch.setattr(Database, "ensure_connected", lambda self, enforce=True: "ok")
        import uvicorn
        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
        assert cli.run_server() == 0
        assert not paths.start_failed_path().exists() and not paths.pid_path().exists()

    def test_model_lock(self, monkeypatch):
        monkeypatch.setattr(processor, "_model", None)
        monkeypatch.setattr(processor, "MODEL_WAIT_SECONDS", 0.5)
        monkeypatch.setattr(processor, "start_background_load", lambda: None)
        processor._model_lock.acquire()
        try:
            t0 = time.monotonic()
            assert processor.get_model(no_model_wait=True) is None
            assert time.monotonic() - t0 < 0.1
            t0 = time.monotonic()
            assert processor.get_model() is None
            assert time.monotonic() - t0 >= 0.4
        finally:
            processor._model_lock.release()

    def test_no_model_wait_endpoints(self, monkeypatch, server_client, cfg, rconn):
        monkeypatch.setattr(processor, "_model", None)
        monkeypatch.setattr(processor, "start_background_load", lambda: None)
        processor._model_lock.acquire()
        try:
            t0 = time.monotonic()
            r = server_client.post("/buffer", json={"content": "fast save", "role": "user", "turn_id": "t",
                                                    "no_model_wait": True}, headers=H(cfg)).json()
            s = server_client.post("/search", json={"query": "fast save", "no_model_wait": True},
                                   headers=H(cfg)).json()
            assert time.monotonic() - t0 < 1.5
            assert r["saved"] and not rconn.hexists("mem:" + r["id"], "embedding")
            assert "still loading" in s["text"]
        finally:
            processor._model_lock.release()

    def test_save_clears_save_failed(self, monkeypatch, server_client, cfg, rconn):
        paths.notify("save_failed", "A record could not be saved (X: y). Current memories may be lost.")
        cli._hook_save(cfg, True, "a later save", "user", "s", "t")
        assert "save_failed" not in json.loads(paths.notices_path().read_text(encoding="utf-8"))

    def test_repair_already_running(self, monkeypatch, server_client, cfg, db):
        gate, entered = threading.Event(), threading.Event()
        real = db.repair

        def slow(**kw):
            entered.set()
            gate.wait(5)
            return real(**kw)

        monkeypatch.setattr(db, "repair", slow)
        th = threading.Thread(target=lambda: server_client.post("/repair", json={}, headers=H(cfg)))
        th.start()
        entered.wait(5)
        second = server_client.post("/repair", json={}, headers=H(cfg)).json()
        gate.set()
        th.join()
        assert second["message"] == "repair already running"

    def test_search_argument_sanitised(self, monkeypatch):
        sent = []
        monkeypatch.setattr(cli, "ensure_server", lambda cfg, max_wait=60: True)
        monkeypatch.setattr(cli, "_post", lambda p, path, body, timeout=30: (sent.append(body), (200, {"text": "x"}))[1])
        cli.main(["--search", "abc" + chr(0xD800)])
        assert sent[0]["query"] == "abc"

    def test_stop_entry_shares_child_timeout(self, monkeypatch):
        deadlines = []
        monkeypatch.setattr(lite_hook, "read_stdin", lambda: b"{}")
        monkeypatch.setattr(lite_hook, "run_step", lambda args, d, dl: deadlines.append(dl))
        t0 = time.monotonic()
        lite_stop_hook.main()
        assert len(deadlines) == 2 and deadlines[0] == deadlines[1]
        assert deadlines[0] - t0 <= lite_hook.CHILD_TIMEOUT + 0.1


class TestBufferEndpoint:
    @pytest.mark.parametrize("role", ["user", "claude"])
    def test_role_prefix(self, server_client, cfg, rconn, role):
        r = server_client.post("/buffer", json={"content": "hello", "role": role, "turn_id": "t"},
                               headers=H(cfg)).json()
        assert r["status"] == "saved"
        assert rconn.hget("mem:" + r["id"], "content").decode() == f"[{role}] hello"

    def test_redis_down(self, offline_app, cfg):
        r = TestClient(offline_app).post("/buffer", json={"content": "x", "role": "user"}, headers=H(cfg))
        assert r.status_code == 503 and "docker start redis-stack" in r.json()["reason"]


class TestSearchEndpoint:
    def test_block_and_limits(self, server_client, cfg, db):
        for i in range(30):
            db.save_memory(f"limit test record {i} " + "padding text " * 30)
        full = server_client.post("/search", json={"query": "limit test record"}, headers=H(cfg)).json()["text"]
        small = server_client.post("/search", json={"query": "limit test record", "max_chars": 3000},
                                   headers=H(cfg)).json()["text"]
        assert full.startswith("# Recalled Memory Context\nSource: N3MC Lite (7-day working memory)\n")
        assert len(full) > len(small) and "omitted to keep this block" in small and "omitted" not in full

    def test_bad_since(self, server_client, cfg):
        r = server_client.post("/search", json={"query": "x", "since": "yesterday"}, headers=H(cfg))
        assert r.status_code == 400 and r.json()["detail"].startswith("Error: since must be")

    def test_redis_down(self, offline_app, cfg):
        r = TestClient(offline_app).post("/search", json={"query": "x"}, headers=H(cfg))
        assert r.status_code == 503


def _noscore(text):
    return re.sub(r" score=[0-9.]+", "", text)


class TestToolsEndpoint:
    def _tool(self, client, cfg, name, args, nudge):
        return client.post(f"/tools/{name}?nudge={nudge}", json=args, headers=H(cfg)).json()["text"]

    def test_same_text_as_handlers(self, server_client, cfg, db):
        cfg["_notices"].clear()      # process notices are shown once; keep both runners equal
        r = parse_json(self._tool(server_client, cfg, "save_memory", {"content": "tool text", "turn_id": "tt"}, 1))
        ref = ToolRunner(cfg, db)
        for name, args in [("search_memory", {"query": "tool text"}), ("list_memories", {}),
                           ("recall_thread", {"turn_id": "tt"}), ("repair_memory", {})]:
            with_nudge = self._tool(server_client, cfg, name, args, 1)
            without = self._tool(server_client, cfg, name, args, 0)
            # BM25 scores of a tiny corpus move after any write, so the score= values are left out
            assert _noscore(with_nudge) == _noscore(ref.call(name, args, nudge=True))
            assert _noscore(without) == _noscore(ToolRunner(cfg, db).call(name, args, nudge=False))
        assert self._tool(server_client, cfg, "save_memory", {"content": "tool text"}, 1).endswith(tools._NUDGE_AFTER_SAVE)
        out = self._tool(server_client, cfg, "delete_memory", {"id": r["id"]}, 0)
        assert parse_json(out)["status"] == "deleted" and not out.endswith(tools._NUDGE_GENERIC)
        out = self._tool(server_client, cfg, "delete_memories_by_session", {"session_id": "none"}, 1)
        assert out.endswith(tools._NUDGE_GENERIC)

    def test_unknown_and_non_object(self, server_client, cfg):
        assert self._tool(server_client, cfg, "nope", {}, 0) == "Error: unknown tool 'nope'"
        r = server_client.post("/tools/list_memories?nudge=0", content="[1, 2]", headers=H(cfg)).json()
        assert r["text"] == "_No memories stored._"

    def test_search_context_limit(self, server_client, cfg, db):
        cfg["context_max_chars"] = 2000
        for i in range(10):
            db.save_memory(f"bounded output record {i} " + "word " * 80)
        out = self._tool(server_client, cfg, "search_memory", {"query": "bounded output record"}, 1)
        from n3mc_mcp.context import context_size
        assert context_size(out) + 2 <= 2000 and out.endswith(tools._NUDGE_AFTER_SEARCH)


class TestShutdown:
    def test_other_root(self, offline_app, cfg):
        r = TestClient(offline_app).post("/shutdown", json={"root": "/somewhere/else"}, headers=H(cfg))
        assert r.status_code == 403

    def test_own_root(self, app_factory, cfg):
        app = app_factory(Database(dict(cfg, redis_url=conftest.UNREACHABLE_URL)))
        r = TestClient(app).post("/shutdown", json={"root": str(paths.data_dir())}, headers=H(cfg))
        assert r.json() == {"status": "ok"}
        time.sleep(0.5)
        assert app_factory.exits == [1]


class TestConcurrency:
    def test_parallel(self, server_client, cfg, rconn):
        results = []

        def work(i):
            a = server_client.post("/tools/save_memory?nudge=0", json={"content": f"parallel {i}"}, headers=H(cfg))
            b = server_client.post("/search", json={"query": f"parallel {i}"}, headers=H(cfg))
            c = server_client.post("/tools/save_memory?nudge=0", json={"content": "same for all"}, headers=H(cfg))
            results.append((a.status_code, b.status_code, c.status_code, parse_json(a.json()["text"])["saved"]))

        ts = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert len(results) == 8 and all(r[:3] == (200, 200, 200) and r[3] for r in results)
        same = [k for k in rconn.keys("mem:*") if not k.startswith(b"mem:sha:")
                and rconn.hget(k, "content") == b"same for all"]
        assert len(same) == 1
