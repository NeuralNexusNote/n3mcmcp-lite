"""Layer 6: resident server endpoints."""

import inspect
import json

from n3mc_mcp import app as app_mod
from n3mc_mcp import config as config_mod
from n3mc_mcp import paths


class TestHealth:
    def test_fields(self, client):
        h = client.get("/health").json()
        assert h["status"] == "ok" and paths.same_dir(h["root"], paths.data_dir())
        assert len(h["code_version"]) == 16 and isinstance(h["pid"], int) and h["redis"].endswith("/0")

    def test_code_version_follows_config(self):
        p = paths.config_path()
        v1 = config_mod.code_version()
        d = json.loads(p.read_text(encoding="utf-8"))
        p.write_text(json.dumps(d, indent=4, sort_keys=True), encoding="utf-8")
        assert config_mod.code_version() == v1
        d["min_results"] = 2
        p.write_text(json.dumps(d), encoding="utf-8")
        assert config_mod.code_version() != v1


class TestHostHeader:
    def test_host_and_origin(self, client, cfg):
        assert client.get("/health", headers={"Host": "evil.example"}).status_code == 403
        assert client.get("/health", headers={"Host": f"localhost:{cfg['server_port']}"}).status_code == 200
        for origin in ("http://evil.example", "null"):
            assert client.post("/tools/list_memories", json={}, headers={"Origin": origin}).status_code == 403


class TestPlainDef:
    def test_endpoints_are_plain_def(self, client):
        for route in client.app.routes:
            if getattr(route, "path", "") in ("/buffer", "/search", "/repair", "/tools/{name}"):
                assert not inspect.iscoroutinefunction(route.endpoint)


class TestBufferAndSearch:
    def test_buffer_role(self, client, rconn):
        r = client.post("/buffer", json={"content": "hi there", "role": "user", "session_id": "cc",
                                         "turn_id": "t1"}).json()
        assert r["saved"] and rconn.hget("mem:" + r["id"], "content") == b"[user] hi there"
        assert rconn.hget("mem:" + r["id"], "agent_name") == b"claude-code"

    def test_search_block_and_bad_since(self, client):
        client.post("/buffer", json={"content": "searchable words", "role": "user", "turn_id": "t"})
        t = client.post("/search", json={"query": "searchable words"}).json()["text"]
        assert t.startswith("# Recalled Memory Context\nSource: N3MC Lite (7-day working memory)\n"
                            "Search query: searchable words\n\n")
        r = client.post("/search", json={"query": "x", "since": "2026/11/13"})
        assert r.status_code == 400 and r.json()["detail"].startswith("Error: since must be")


class TestToolsEndpoint:
    def test_nudge_flag_and_bad_body(self, client):
        a = client.post("/tools/list_memories?nudge=1", json={}).json()["text"]
        b = client.post("/tools/list_memories?nudge=0", content=b"[1,2]").json()["text"]
        assert a.startswith(b) and len(a) > len(b)
        assert client.post("/tools/x?nudge=0", json={}).json()["text"] == "Error: unknown tool 'x'"


class TestShutdown:
    def test_root_check(self, client, monkeypatch):
        called = []
        monkeypatch.setattr(app_mod, "_request_shutdown", lambda: called.append(1))
        assert client.post("/shutdown", json={"root": "/elsewhere"}).status_code == 403
        assert client.post("/shutdown", json={"root": str(paths.data_dir())}).json() == {"status": "ok"}
        assert called == [1]


class TestConcurrency:
    def test_parallel_saves(self, client):
        import concurrent.futures as cf
        with cf.ThreadPoolExecutor(8) as ex:
            res = list(ex.map(lambda i: client.post("/tools/save_memory?nudge=0",
                                                    json={"content": "same text"}).json()["text"], range(8)))
        assert sum('"saved": true' in r for r in res) == 1
