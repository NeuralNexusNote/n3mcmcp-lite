"""Layer 6: the resident server's endpoints and process management (spec §3.15, §4.5)."""
import inspect
import json
import os
import threading
import time

import pytest
from fastapi.testclient import TestClient

import conftest
from n3mc_mcp import app as app_mod
from n3mc_mcp import cli, config, paths
from n3mc_mcp import processor as P
from n3mc_mcp.database import MEM, Database
from n3mc_mcp.tools import _NUDGE_AFTER_SAVE, _NUDGE_AFTER_SEARCH, _NUDGE_GENERIC, ToolRunner

PORT = conftest.TEST_PORT
H = {"Host": f"127.0.0.1:{PORT}"}


class _DownDB:
    url = "redis://127.0.0.1:1/0"

    def ensure_connected(self, *a, **k):
        return "Redis Stack is not reachable at 127.0.0.1:1. hint"

    def hint(self):
        return self.ensure_connected()

    def mark_down(self):
        pass


def _app(cfg, db=None):
    return app_mod.create_app(cfg, db or _DownDB(), code_version=config.compute_code_version(cfg["_raw"]),
                              load_model=False)


class TestHealth:
    def test_fields(self, cfg):
        r = TestClient(_app(cfg)).get("/health", headers=H).json()
        assert r["status"] == "ok" and r["pid"] == os.getpid()
        assert os.path.normcase(r["root"]) == os.path.normcase(paths.data_dir())
        assert len(r["code_version"]) == 16 and int(r["code_version"], 16) >= 0
        assert r["redis"] == "127.0.0.1:1/0"

    def test_code_version_tracks_config(self):
        a = config.compute_code_version(b'{"a": 1, "b": 2}')
        assert a == config.compute_code_version(b'{ "b":2,\n "a":1 }')
        assert a != config.compute_code_version(b'{"a": 1, "b": 3}')


class TestHostHeader:
    ENDPOINTS = [("get", "/health"), ("post", "/buffer"), ("post", "/search"), ("post", "/repair"),
                 ("post", "/tools/list_memories"), ("post", "/shutdown")]

    @pytest.mark.parametrize("method,path", ENDPOINTS)
    def test_evil_host(self, cfg, method, path):
        c = TestClient(_app(cfg))
        assert getattr(c, method)(path, headers={"Host": "evil.example"}).status_code == 403

    @pytest.mark.parametrize("host", [f"127.0.0.1:{PORT}", f"localhost:{PORT}"])
    @pytest.mark.parametrize("method,path", ENDPOINTS[:5])
    def test_local_host(self, cfg, host, method, path):
        c = TestClient(_app(cfg))
        kw = {} if method == "get" else {"json": {}}
        assert getattr(c, method)(path, headers={"Host": host}, **kw).status_code != 403

    @pytest.mark.parametrize("origin", ["http://evil.example", "null"])
    @pytest.mark.parametrize("method,path", ENDPOINTS)
    def test_origin(self, cfg, origin, method, path):
        c = TestClient(_app(cfg))
        assert getattr(c, method)(path, headers=dict(H, Origin=origin)).status_code == 403

    def test_cli_sends_no_origin(self, monkeypatch, cfg):
        seen = {}

        class R:
            status = 200

            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        monkeypatch.setattr(cli._OPENER, "open", lambda req, timeout=0: (seen.update(dict(req.header_items())), R())[1])
        cli._http(cfg, "GET", "/health")
        assert "Origin" not in seen and seen["Host"] == f"127.0.0.1:{PORT}"


class TestPlainDef:
    def test_plain(self, cfg):
        app = _app(cfg)
        eps = {r.path: r.endpoint for r in app.routes if hasattr(r, "endpoint")}
        for p in ("/buffer", "/search", "/repair", "/tools/{name}"):
            assert not inspect.iscoroutinefunction(eps[p])


class TestServerProcessEdgeCases:
    def test_other_port_stopped(self, cfg, monkeypatch):
        other = PORT + 7
        sent, started = [], []
        monkeypatch.setattr(cli, "_health", lambda c, port=None: ({"root": paths.data_dir(), "code_version": "x", "pid": 4242}
                                                                 if port == other else None))
        monkeypatch.setattr(cli, "_read_pid_file", lambda: (4242, time.time()))
        monkeypatch.setattr(cli, "_pid_is_starting", lambda pid, m: True)
        monkeypatch.setattr(cli, "_listening_ports", lambda pid: [other])
        monkeypatch.setattr(cli, "_request_shutdown", lambda c, root, port=None: sent.append(port) or True)
        monkeypatch.setattr(cli, "_wait_port_free", lambda p, total=15: True)
        monkeypatch.setattr(cli, "_start_server", lambda c, max_wait=60: started.append(1) or False)
        cli.ensure_server(cfg, max_wait=0)
        assert sent == [other] and started == [1]

    def test_foreign_on_other_port_untouched(self, cfg, monkeypatch):
        other = PORT + 7
        sent = []
        monkeypatch.setattr(cli, "_health", lambda c, port=None: ({"root": "/elsewhere", "pid": 1} if port == other else None))
        monkeypatch.setattr(cli, "_listening_ports", lambda pid: [other])
        monkeypatch.setattr(cli, "_request_shutdown", lambda *a, **k: sent.append(1) or True)
        assert cli._handle_other_port(cfg, 4242) is False and not sent

    @pytest.mark.parametrize("exc", [SystemExit(1), RuntimeError("boom")])
    def test_run_server_failure_recorded(self, monkeypatch, exc):
        import uvicorn
        monkeypatch.setattr(app_mod, "create_app", lambda *a, **k: object())
        monkeypatch.setattr(Database, "ensure_connected", lambda self, *a, **k: "ok")
        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: (_ for _ in ()).throw(exc))
        with pytest.raises(type(exc)):
            cli.run_server()
        assert os.path.exists(paths.start_failed_path()) and paths.has_notice("server_start_failed")
        before = len(conftest.START_CALLS)
        assert cli.ensure_server(config.load_config(), max_wait=0) is False
        assert len(conftest.START_CALLS) == before

    def test_run_server_clean_exit(self, monkeypatch):
        import uvicorn
        monkeypatch.setattr(app_mod, "create_app", lambda *a, **k: object())
        monkeypatch.setattr(Database, "ensure_connected", lambda self, *a, **k: "ok")
        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
        cli.run_server()
        assert not os.path.exists(paths.start_failed_path())

    def test_no_model_wait(self, cfg, db, monkeypatch):
        monkeypatch.setattr(P, "_FAKE_EMBED", None)
        monkeypatch.setattr(P, "_model", None)
        monkeypatch.setattr(P, "MODEL_WAIT_SECONDS", 2)
        monkeypatch.setattr(P, "start_background_load", lambda cfg=None: None)
        P._lock.acquire()
        try:
            c = TestClient(_app(cfg, db))
            t0 = time.time()
            r = c.post("/buffer", json={"content": "fast save", "role": "user", "turn_id": "t", "no_model_wait": True}, headers=H)
            s = c.post("/search", json={"query": "fast", "no_model_wait": True}, headers=H)
            assert time.time() - t0 < 1.5 and r.json()["saved"] and s.status_code == 200
            assert b"embedding" not in db.client.hgetall(MEM + r.json()["id"])
            t0 = time.time()
            assert P.get_model(cfg, wait=True) is None and time.time() - t0 >= 1.9
        finally:
            P._lock.release()

    def test_hook_requests_carry_field(self, cfg, monkeypatch):
        bodies = []
        monkeypatch.setattr(cli, "ensure_server", lambda c, max_wait=60: True)
        monkeypatch.setattr(cli, "_post", lambda c, path, body, timeout=30, port=None:
                            (bodies.append((path, body)), (200, {"text": "t", "status": "saved"}))[1])
        cli.hook_submit(json.dumps({"session_id": "s", "prompt": "hello"}))
        assert all(b.get("no_model_wait") is True for p, b in bodies if p in ("/search", "/buffer"))
        bodies.clear()
        cli.main(["--search", "a\ud800b"])
        assert bodies[0][0] == "/search" and "no_model_wait" not in bodies[0][1] and bodies[0][1]["query"] == "ab"

    def test_save_clears_save_failed(self, app_router, rconn):
        paths.notify("save_failed", "A record could not be saved (X). Current memories may be lost.")
        cli.hook_submit(json.dumps({"session_id": "s", "prompt": "clears the notice"}))
        assert not paths.has_notice("save_failed")

    def test_repair_already_running(self, cfg, db):
        app = _app(cfg, db)
        runner = app.state.runner
        runner.repair_lock.acquire()
        try:
            r = TestClient(app).post("/repair", json={}, headers=H).json()
            assert r == {"status": "ok", "message": "repair already running", "reembedded": 0, "remaining": 0}
        finally:
            runner.repair_lock.release()


class TestBufferEndpoint:
    def test_roles(self, cfg, db, rconn):
        c = TestClient(_app(cfg, db))
        for role in ("user", "claude"):
            r = c.post("/buffer", json={"content": f"{role} text", "role": role, "turn_id": "t"}, headers=H)
            assert r.status_code == 200 and r.json()["status"] == "saved"
            assert rconn.hget(MEM + r.json()["id"], "content") == f"[{role}] {role} text".encode()

    def test_redis_down(self, cfg):
        r = TestClient(_app(cfg)).post("/buffer", json={"content": "x", "role": "user"}, headers=H)
        assert r.status_code == 503 and r.json()["saved"] is False and "Redis Stack" in r.json()["reason"]


class TestSearchEndpoint:
    def test_block(self, cfg, db):
        db.save_memory("endpoint search fact")
        r = TestClient(_app(cfg, db)).post("/search", json={"query": "endpoint search"}, headers=H)
        assert r.json()["text"].startswith("# Recalled Memory Context\nSource: N3MC Lite (7-day working memory)\nSearch query: endpoint search")

    def test_max_chars(self, cfg, db):
        for i in range(12):
            db.save_memory(f"limit topic sentence number {i} " + "filler words " * 30)
        c = TestClient(_app(cfg, db))
        full = c.post("/search", json={"query": "limit topic sentence"}, headers=H).json()["text"]
        small = c.post("/search", json={"query": "limit topic sentence", "max_chars": 2000}, headers=H).json()["text"]
        from n3mc_mcp.context import context_size
        assert context_size(small) + 2 <= 2000 < context_size(full)

    def test_bad_since(self, cfg, db):
        r = TestClient(_app(cfg, db)).post("/search", json={"query": "x", "since": "yesterday"}, headers=H)
        assert r.status_code == 400 and r.json()["detail"].startswith("Error: since must be")

    def test_redis_down(self, cfg):
        assert TestClient(_app(cfg)).post("/search", json={"query": "x"}, headers=H).status_code == 503


class TestToolsEndpoint:
    CALLS = [("search_memory", {"query": "tools endpoint"}), ("save_memory", {"content": "tools endpoint fact"}),
             ("list_memories", {}), ("recall_thread", {"turn_id": "none"}), ("delete_memory", {"id": "none"}),
             ("delete_memories_by_session", {"session_id": "none"}), ("repair_memory", {})]

    @pytest.mark.parametrize("name,args", CALLS)
    def test_nudge(self, cfg, db, rconn, name, args):
        cfg["_notices"].clear()
        c = TestClient(_app(cfg, db))
        with_n = c.post(f"/tools/{name}?nudge=1", json=args, headers=H).json()["text"]
        if name == "save_memory":
            rconn.flushdb()
            rconn.set(conftest.TEST_MARK, 1)
        without = c.post(f"/tools/{name}?nudge=0", json=args, headers=H).json()["text"]
        tail = {"search_memory": _NUDGE_AFTER_SEARCH, "save_memory": _NUDGE_AFTER_SAVE}.get(name, _NUDGE_GENERIC)
        assert with_n.endswith(tail) and not without.endswith(tail)
        if name != "save_memory":
            assert with_n == without + tail

    def test_unknown(self, cfg, db):
        c = TestClient(_app(cfg, db))
        assert c.post("/tools/nope?nudge=1", json={}, headers=H).json()["text"] == "Error: unknown tool 'nope'" + _NUDGE_GENERIC
        assert c.post("/tools/nope?nudge=0", json={}, headers=H).json()["text"] == "Error: unknown tool 'nope'"

    def test_non_object_body(self, cfg, db):
        r = TestClient(_app(cfg, db)).post("/tools/list_memories?nudge=0", json=[1, 2], headers=H)
        assert r.status_code == 200 and r.json()["text"] == "_No memories stored._"

    def test_search_limited(self, cfg, db):
        cfg["context_max_chars"] = 2000
        for i in range(10):
            db.save_memory(f"bounded result {i} " + "padding text " * 40)
        text = TestClient(_app(cfg, db)).post("/tools/search_memory", json={"query": "bounded result"}, headers=H).json()["text"]
        from n3mc_mcp.context import context_size
        assert context_size(text) + 2 <= 2000 and text.endswith(_NUDGE_AFTER_SEARCH)


class TestShutdown:
    def test_wrong_root(self, cfg):
        assert TestClient(_app(cfg)).post("/shutdown", json={"root": "/elsewhere"}, headers=H).status_code == 403

    def test_own_root(self, cfg, monkeypatch):
        ended = threading.Event()
        monkeypatch.setattr(app_mod, "_exit_process", ended.set)
        r = TestClient(_app(cfg)).post("/shutdown", json={"root": paths.data_dir()}, headers=H)
        assert r.json() == {"status": "ok"} and ended.wait(3)


class TestConcurrency:
    def test_threads(self, cfg, db, rconn):
        c = TestClient(_app(cfg, db))
        errors = []

        def work(i):
            try:
                a = c.post("/buffer", json={"content": f"concurrent {i}", "role": "user", "turn_id": f"t{i}"}, headers=H)
                b = c.post("/search", json={"query": f"concurrent {i}"}, headers=H)
                assert a.status_code == 200 and b.status_code == 200
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        ts = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert not errors

    def test_same_text_once(self, cfg, db, rconn):
        c = TestClient(_app(cfg, db))
        c.post("/tools/save_memory", json={"content": "only once"}, headers=H)
        c.post("/tools/save_memory", json={"content": "only once"}, headers=H)
        assert len([k for k in rconn.keys("mem:*") if not k.startswith(b"mem:sha:")]) == 1
