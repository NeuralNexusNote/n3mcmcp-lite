"""Layer 6: the resident server endpoints and process management (spec §9 Layer 6)."""
import inspect
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from conftest import host_headers, parse_json
from n3mc_mcp import app as A
from n3mc_mcp import cli
from n3mc_mcp import config as CF
from n3mc_mcp import paths
from n3mc_mcp.tools import _NUDGE_AFTER_SAVE, _NUDGE_GENERIC


class _NoDb:
    url = "redis://127.0.0.1:1/0"

    def ensure_connected(self, ephemeral=True):
        return CF.backend_hint(self.url)

    def mark_disconnected(self):
        pass


@pytest.fixture
def bare(cfg):
    app = A.create_app(cfg, _NoDb(), code_version=cli.own_code_version(cfg), port=cfg["server_port"])
    app.state.exit = lambda: None
    return TestClient(app), cfg["server_port"]


class TestHealth:
    def test_fields(self, bare, cfg):
        c, port = bare
        r = c.get("/health", headers=host_headers(port)).json()
        assert r["status"] == "ok" and r["pid"] > 0
        assert r["root"] == paths.data_dir()
        assert len(r["code_version"]) == 16 and int(r["code_version"], 16) >= 0

    def test_code_version_follows_config(self, isolate):
        p = isolate["data"] / "config.json"
        a = CF.compute_code_version(p.read_bytes())
        d = json.loads(p.read_text(encoding="utf-8"))
        same = json.dumps(dict(reversed(list(d.items()))), indent=4).encode()
        assert CF.compute_code_version(same) == a
        d["min_score"] = 0.3
        assert CF.compute_code_version(json.dumps(d).encode()) != a


class TestHostHeader:
    PATHS = [("GET", "/health"), ("POST", "/shutdown"), ("POST", "/buffer"), ("POST", "/search"),
             ("POST", "/repair"), ("POST", "/tools/list_memories")]

    @pytest.mark.parametrize("method,path", PATHS)
    def test_evil_host(self, bare, method, path):
        c, _port = bare
        assert c.request(method, path, headers={"Host": "evil.example"}, json={}).status_code == 403

    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
    def test_local_hosts(self, bare, host):
        c, port = bare
        assert c.get("/health", headers={"Host": f"{host}:{port}"}).status_code == 200

    @pytest.mark.parametrize("origin", ["http://evil.example", "null"])
    @pytest.mark.parametrize("method,path", PATHS)
    def test_origin(self, bare, method, path, origin):
        c, port = bare
        h = host_headers(port)
        h["Origin"] = origin
        assert c.request(method, path, headers=h, json={}).status_code == 403

    def test_without_origin_passes(self, bare):
        c, port = bare
        assert c.get("/health", headers=host_headers(port)).status_code == 200

    def test_cli_sends_no_origin(self, monkeypatch):
        seen = {}

        class Resp:
            status = 200

            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_open(req, timeout=None):
            seen.update({k.lower(): v for k, v in req.header_items()})
            return Resp()

        monkeypatch.setattr(cli._OPENER, "open", fake_open)
        import conftest
        conftest.REAL_REQUEST(1234, "GET", "/health")
        assert "origin" not in seen and seen["host"] == "127.0.0.1:1234"


class TestPlainDef:
    def test_plain(self, bare):
        c, _ = bare
        for route in c.app.routes:
            if getattr(route, "path", "") in ("/buffer", "/search", "/repair", "/tools/{name}"):
                assert not inspect.iscoroutinefunction(route.endpoint), route.path


class TestBufferEndpoint:
    def test_roles(self, server, rconn):
        port = server.app_obj.state.cfg["server_port"]
        r = server.post("/buffer", json={"content": "hello", "role": "user", "turn_id": "t1", "session_id": "s"},
                        headers=host_headers(port)).json()
        assert r["saved"] and rconn.hget("mem:" + r["id"], "content") == b"[user] hello"
        r = server.post("/buffer", json={"content": "answer", "role": "claude", "turn_id": "t1"},
                        headers=host_headers(port)).json()
        assert rconn.hget("mem:" + r["id"], "content") == b"[claude] answer"

    def test_redis_down(self, bare):
        c, port = bare
        r = c.post("/buffer", json={"content": "x", "role": "user"}, headers=host_headers(port))
        assert r.status_code == 503 and "docker start redis-stack" in r.json()["reason"]


class TestSearchEndpoint:
    def test_text(self, server, db):
        port = server.app_obj.state.cfg["server_port"]
        db.save_memory("search endpoint words")
        r = server.post("/search", json={"query": "search endpoint words"}, headers=host_headers(port)).json()
        assert r["text"].startswith("# Recalled Memory Context\nSource: N3MC Lite (7-day working memory)\n"
                                    "Search query: search endpoint words\n\n")

    def test_limit(self, server, db):
        port = server.app_obj.state.cfg["server_port"]
        for i in range(30):
            db.save_memory(f"limit topic words number {i} " + "filler " * 60)
        big = server.post("/search", json={"query": "limit topic words"}, headers=host_headers(port)).json()["text"]
        small = server.post("/search", json={"query": "limit topic words", "max_chars": 3000},
                            headers=host_headers(port)).json()["text"]
        from n3mc_mcp.context import context_size
        assert context_size(small) + 2 <= 3000 < context_size(big)

    def test_bad_since(self, server):
        port = server.app_obj.state.cfg["server_port"]
        r = server.post("/search", json={"query": "x", "since": "yesterday"}, headers=host_headers(port))
        assert r.status_code == 400 and r.json()["detail"].startswith("Error: since must be")

    def test_redis_down(self, bare):
        c, port = bare
        r = c.post("/search", json={"query": "x"}, headers=host_headers(port))
        assert r.status_code == 503


class TestToolsEndpoint:
    def test_same_text(self, server, db, cfg):
        from n3mc_mcp.tools import ToolRunner
        port = cfg["server_port"]
        r1 = server.post("/tools/save_memory?nudge=1", json={"content": "tools endpoint"}, headers=host_headers(port))
        assert r1.json()["text"].endswith(_NUDGE_AFTER_SAVE)
        r0 = server.post("/tools/list_memories?nudge=0", json={}, headers=host_headers(port)).json()["text"]
        assert r0 == ToolRunner(cfg, db).call("list_memories", {}, nudge=False)
        r1 = server.post("/tools/list_memories", json={}, headers=host_headers(port)).json()["text"]
        assert r1 == r0 + _NUDGE_GENERIC

    def test_unknown(self, server, cfg):
        r = server.post("/tools/nope?nudge=0", json={}, headers=host_headers(cfg["server_port"])).json()
        assert r["text"] == "Error: unknown tool 'nope'"

    def test_non_object_body(self, server, cfg):
        r = server.post("/tools/list_memories?nudge=0", json=[1, 2], headers=host_headers(cfg["server_port"]))
        assert r.status_code == 200


class TestShutdown:
    def test_other_root(self, bare):
        c, port = bare
        assert c.post("/shutdown", json={"root": "/elsewhere"}, headers=host_headers(port)).status_code == 403

    def test_own_root(self, bare):
        c, port = bare
        exits = []
        c.app.state.exit = lambda: exits.append(1)
        r = c.post("/shutdown", json={"root": paths.data_dir()}, headers=host_headers(port))
        assert r.json() == {"status": "ok"}
        time.sleep(0.6)
        assert exits == [1]


class TestConcurrency:
    def test_threads(self, server, cfg):
        port = cfg["server_port"]
        errors = []

        def work(i):
            try:
                r = server.post("/tools/save_memory?nudge=0", json={"content": f"concurrent {i}"},
                                headers=host_headers(port)).json()
                assert parse_json(r["text"])["saved"]
                server.post("/search", json={"query": f"concurrent {i}"}, headers=host_headers(port)).json()
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        ts = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert not errors

    def test_same_text_once(self, server, cfg, rconn):
        port = cfg["server_port"]
        out = []

        def work():
            out.append(parse_json(server.post("/tools/save_memory?nudge=0", json={"content": "same twice"},
                                               headers=host_headers(port)).json()["text"])["status"])

        ts = [threading.Thread(target=work) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert sorted(out) == ["duplicate", "saved"]


class TestServerProcessEdgeCases:
    def test_no_model_wait(self, server, cfg, monkeypatch, rconn):
        from n3mc_mcp import processor as P
        monkeypatch.setattr(P, "embed_many", lambda t, kind="passage", no_wait=False: None if no_wait else
                            (_ for _ in ()).throw(AssertionError("waited")))
        monkeypatch.setattr(P, "model_unavailable_message", P.loading_message)
        port = cfg["server_port"]
        t = time.monotonic()
        r = server.post("/buffer", json={"content": "no wait", "role": "user", "turn_id": "x", "no_model_wait": True},
                        headers=host_headers(port)).json()
        assert time.monotonic() - t < 1.5
        assert r["saved"] and not rconn.hget("mem:" + r["id"], "embedding")
        s = server.post("/search", json={"query": "no wait", "no_model_wait": True}, headers=host_headers(port)).json()
        assert "still loading" in s["text"]

    def test_repair_already_running(self, server, cfg, db, monkeypatch):
        gate = threading.Event()
        started = threading.Event()

        def slow(no_model_wait=False):
            started.set()
            gate.wait(5)
            return {"status": "ok", "message": "index ensured", "reembedded": 0, "remaining": 0}

        monkeypatch.setattr(db, "repair", slow)
        port = cfg["server_port"]
        out = []
        th = threading.Thread(target=lambda: out.append(server.post("/repair", json={}, headers=host_headers(port)).json()))
        th.start()
        started.wait(5)
        second = server.post("/repair", json={}, headers=host_headers(port)).json()
        gate.set()
        th.join()
        assert second["message"] == "repair already running"

    def test_run_server_records_failure(self, monkeypatch):
        monkeypatch.setattr(cli, "load_config", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        with pytest.raises(RuntimeError):
            cli.run_server()
        assert __import__("os").path.exists(paths.start_failed_path())
        notices = json.load(open(paths.notices_path(), encoding="utf-8"))
        assert "server_start_failed" in notices and "RuntimeError: boom" in notices["server_start_failed"]["message"]

    def test_run_server_exit_code(self, monkeypatch):
        monkeypatch.setattr(cli, "load_config", lambda: (_ for _ in ()).throw(SystemExit(1)))
        with pytest.raises(SystemExit):
            cli.run_server()
        assert __import__("os").path.exists(paths.start_failed_path())

    def test_run_server_clean_exit(self, monkeypatch):
        monkeypatch.setattr(cli, "load_config", lambda: (_ for _ in ()).throw(SystemExit(0)))
        with pytest.raises(SystemExit):
            cli.run_server()
        assert not __import__("os").path.exists(paths.start_failed_path())

    def test_hook_request_fields(self, cfg, monkeypatch):
        bodies = []
        monkeypatch.setattr(cli, "_post", lambda port, path, body, timeout=30: (bodies.append((path, body)), (200, {"status": "saved", "saved": True, "text": ""}))[1])
        cli._hook_save(cfg, True, "x", "user", "s", "t")
        cli._hook_search(cfg, True, "q", "s")
        assert all(b.get("no_model_wait") is True for _p, b in bodies)
