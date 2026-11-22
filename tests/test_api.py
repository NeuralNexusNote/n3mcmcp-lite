"""Layer 6: resident server endpoints and process management (TestClient, temp data folder)."""

import inspect
import json
import re
import threading
import time

import pytest

from n3mc_mcp import app as app_mod
from n3mc_mcp import config, paths
from n3mc_mcp.tools import _NUDGE_AFTER_SAVE, _NUDGE_GENERIC


def port_of(cfg):
    return cfg["server_port"]


class TestHealth:
    def test_fields(self, client, cfg):
        h = client.get("/health").json()
        assert h["status"] == "ok" and h["root"] == paths.data_dir()
        assert re.fullmatch(r"[0-9a-f]{16}", h["code_version"]) and isinstance(h["pid"], int)
        assert h["redis"].startswith(("127.0.0.1:", "localhost:")) and "@" not in h["redis"]   # CI uses localhost:6390

    def test_code_version_follows_config_content_not_formatting(self, client):
        path = paths.config_path()
        v0 = config.code_version()
        data = json.load(open(path, encoding="utf-8"))
        open(path, "w", encoding="utf-8").write(json.dumps(dict(reversed(list(data.items()))), indent=4))
        assert config.code_version() == v0
        data["min_results"] = 7
        open(path, "w", encoding="utf-8").write(json.dumps(data))
        assert config.code_version() != v0


class TestServerVersionIsFixedAtStart:
    def test_health_keeps_the_start_version_after_a_config_edit(self, client):
        started = client.get("/health").json()["code_version"]
        path = paths.config_path()
        data = json.load(open(path, encoding="utf-8"))
        data["min_results"] = 9
        open(path, "w", encoding="utf-8").write(json.dumps(data))
        assert config.code_version() != started                      # a new call would see a different version
        assert client.get("/health").json()["code_version"] == started   # the running server keeps its own

    def test_pid_file_removed_only_when_own(self, isolate):
        import os
        open(paths.pid_path(), "w").write(str(os.getpid()))
        assert paths.remove_pid_file_if_own() is True and not os.path.exists(paths.pid_path())
        open(paths.pid_path(), "w").write("1")
        assert paths.remove_pid_file_if_own() is False and os.path.exists(paths.pid_path())


class TestHostHeader:
    @pytest.mark.parametrize("path,method", [("/health", "GET"), ("/search", "POST"), ("/buffer", "POST"),
                                             ("/repair", "POST"), ("/shutdown", "POST"), ("/tools/list_memories", "POST")])
    def test_evil_host_is_403(self, client, path, method):
        r = client.request(method, path, json={}, headers={"Host": "evil.example"})
        assert r.status_code == 403

    def test_good_hosts(self, client, cfg):
        p = port_of(cfg)
        assert client.get("/health", headers={"Host": "127.0.0.1:%d" % p}).status_code == 200
        assert client.get("/health", headers={"Host": "localhost:%d" % p}).status_code == 200

    @pytest.mark.parametrize("origin", ["http://evil.example", "null"])
    def test_origin_is_403(self, client, cfg, origin):
        h = {"Host": "127.0.0.1:%d" % port_of(cfg), "Origin": origin}
        assert client.get("/health", headers=h).status_code == 403
        assert client.post("/tools/list_memories", json={}, headers=h).status_code == 403
        assert client.get("/health", headers={"Host": "127.0.0.1:%d" % port_of(cfg)}).status_code == 200


class TestPlainDef:
    def test_endpoints_are_sync(self, client):
        for route in client.app.routes:
            if getattr(route, "path", "") in ("/buffer", "/search", "/repair", "/tools/{name}"):
                assert not inspect.iscoroutinefunction(route.endpoint), route.path


class TestReviewFixes:
    def test_l5_repair_already_running(self, client, svc, db):
        assert svc.repair_lock.acquire(blocking=False)
        try:
            r = client.post("/repair", json={})
        finally:
            svc.repair_lock.release()
        assert r.json() == {"status": "ok", "message": "repair already running", "reembedded": 0, "remaining": 0}

    def test_m3_no_model_wait_returns_fast_and_saves_without_vector(self, cfg, redis_client, monkeypatch, client, fake_embed):
        from n3mc_mcp import processor
        monkeypatch.setattr(processor, "embed", fake_embed.real)
        monkeypatch.setattr(processor, "_model", None)
        monkeypatch.setattr(processor, "_last_error", None)
        monkeypatch.setattr(processor, "MODEL_WAIT_SECONDS", 30)
        monkeypatch.setattr(processor, "_bg_thread", None)
        monkeypatch.setattr(processor, "_load_locked", lambda name: None)   # no real model load
        started, release = threading.Event(), threading.Event()

        def hold():
            with processor._model_lock:
                started.set()
                release.wait(20)

        t = threading.Thread(target=hold)
        t.start()
        started.wait(5)
        try:
            t0 = time.time()
            r = client.post("/buffer", json={"content": "hook message", "role": "user", "turn_id": "t",
                                             "no_model_wait": True})
            s = client.post("/search", json={"query": "hook message", "no_model_wait": True})
            assert time.time() - t0 < 5
            assert r.json()["saved"] is True and r.json()["warning"] == "embedding_unavailable"
            assert "still loading" in s.json()["text"]
        finally:
            release.set()
            t.join()

    def test_l4_later_save_clears_save_failed(self, svc, db, wired, cfg):
        from n3mc_mcp import cli
        cli._notify("save_failed", "A record could not be saved (x). Current memories may be lost.")
        assert "save_failed" in cli._read_notices()
        cli._hook_save(cfg, True, "a good message", "user", "sid", "turn-1")
        assert "save_failed" not in cli._read_notices()

    def test_m2_run_server_failure_marks(self, cfg, monkeypatch):
        from n3mc_mcp import cli
        monkeypatch.setattr(cli, "_run_server", lambda c, v=None: (_ for _ in ()).throw(SystemExit(1)))
        with pytest.raises(SystemExit):
            cli.run_server()
        assert (open(paths.start_failed_path()).read())
        assert "server_start_failed" in cli._read_notices()

    def test_m2_clean_exit_writes_nothing(self, cfg, monkeypatch):
        from n3mc_mcp import cli
        monkeypatch.setattr(cli, "_run_server", lambda c, v=None: None)
        cli.run_server()
        import os
        assert not os.path.exists(paths.start_failed_path())


class TestBufferEndpoint:
    def test_roles_and_prefix(self, client, cfg, redis_client):
        r = client.post("/buffer", json={"content": "hello there", "role": "user", "turn_id": "T", "session_id": "S",
                                         "agent_name": "claude-code"})
        assert r.status_code == 200 and r.json()["status"] == "saved"
        key = b"mem:" + r.json()["id"].encode()
        assert redis_client.hget(key, "content") == b"[user] hello there"
        assert redis_client.hget(key, "agent_name") == b"claude-code"
        assert redis_client.hget(key, "session_id") == b"S"

    def test_redis_down_is_503(self, svc, client, monkeypatch):
        from n3mc_mcp.database import BackendUnreachable
        monkeypatch.setattr(svc.db, "ensure_connected", lambda enforce=True: (_ for _ in ()).throw(BackendUnreachable("hint here")))
        r = client.post("/buffer", json={"content": "x", "role": "user", "turn_id": "T"})
        assert r.status_code == 503
        assert r.json() == {"status": "error", "saved": False, "reason": "hint here"}


class TestSearchEndpoint:
    def test_text_and_limits(self, client, svc, db):
        for i in range(12):
            svc.save({"content": "alpha beta topic number %d " % i + "filler " * 80})
        svc.cfg["_notices"].clear()
        full = client.post("/search", json={"query": "alpha beta topic"}).json()["text"]
        assert full.startswith("# Recalled Memory Context\nSource: N3MC Lite (7-day working memory)\nSearch query: alpha beta topic")
        limited = client.post("/search", json={"query": "alpha beta topic", "max_chars": 2500}).json()["text"]
        assert len(limited) < len(full)

    def test_unreadable_since_is_400(self, client, db):
        r = client.post("/search", json={"query": "x", "since": "yesterday"})
        assert r.status_code == 400 and r.json()["detail"].startswith("Error: since must be")

    def test_redis_down_is_503(self, svc, client, monkeypatch):
        from n3mc_mcp.database import BackendUnreachable
        monkeypatch.setattr(svc.db, "ensure_connected", lambda enforce=True: (_ for _ in ()).throw(BackendUnreachable("down")))
        r = client.post("/search", json={"query": "x"})
        assert r.status_code == 503 and r.json() == {"detail": "down"}


class TestToolsEndpoint:
    def test_each_tool_with_and_without_nudge(self, client, svc, db):
        a = client.post("/tools/save_memory?nudge=1", json={"content": "tool endpoint text"}).json()["text"]
        b = client.post("/tools/save_memory?nudge=0", json={"content": "tool endpoint text two"}).json()["text"]
        assert a.endswith(_NUDGE_AFTER_SAVE) and not b.endswith(_NUDGE_AFTER_SAVE)
        assert json.loads(a[:a.index("\n\n---")])["status"] == "saved"
        l = client.post("/tools/list_memories?nudge=0", json={}).json()["text"]
        assert "tool endpoint text" in l
        s0 = client.post("/tools/search_memory?nudge=0", json={"query": "tool endpoint"}).json()["text"]
        s1 = client.post("/tools/search_memory?nudge=1", json={"query": "tool endpoint"}).json()["text"]
        assert "tool endpoint text" in s0 and "tool endpoint text" in s1
        assert "_Reminder:" in s1 and "_Reminder:" not in s0
        for name in ("recall_thread", "delete_memory", "delete_memories_by_session", "repair_memory"):
            body = {"turn_id": "x"} if name == "recall_thread" else {"id": "x"} if name == "delete_memory" else \
                {"session_id": "x"} if name.startswith("delete_memories") else {}
            assert client.post("/tools/%s?nudge=0" % name, json=body).json()["text"]

    def test_unknown_and_non_object(self, client, db):
        assert client.post("/tools/nope?nudge=0", json={}).json()["text"] == "Error: unknown tool 'nope'"
        assert client.post("/tools/nope", json={}).json()["text"].endswith(_NUDGE_GENERIC)
        assert client.post("/tools/list_memories?nudge=0", json=[1, 2]).json()["text"] == "_No memories stored._"

    def test_search_follows_context_max_chars(self, client, svc, db):
        for i in range(12):
            svc.save({"content": "limit topic %d " % i + "padding " * 120})
        svc.cfg["context_max_chars"] = 2500
        svc.cfg["min_score"] = 0.0
        svc.cfg["relative_min_score_ratio"] = 0.0
        t = client.post("/tools/search_memory?nudge=1", json={"query": "limit topic", "limit": 20}).json()["text"]
        from n3mc_mcp import context
        assert context.context_size(t) + 2 <= 2500


class TestShutdown:
    def test_other_root_forbidden(self, client):
        assert client.post("/shutdown", json={"root": "C:/somewhere/else"}).status_code == 403

    def test_own_root_ok(self, client):
        calls = []
        client.app.state.exit_fn = lambda: calls.append(1)
        assert client.post("/shutdown", json={"root": paths.data_dir()}).json() == {"status": "ok"}
        time.sleep(0.6)
        assert calls == [1]


class TestConcurrency:
    def test_parallel_saves_and_searches(self, client, svc, db):
        errors = []

        def work(i):
            try:
                assert client.post("/buffer", json={"content": "parallel message %d" % i, "role": "user", "turn_id": "T%d" % i}).status_code == 200
                assert client.post("/search", json={"query": "parallel message"}).status_code == 200
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        ts = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert errors == []
        for _ in range(2):
            client.post("/buffer", json={"content": "same twice", "role": "user", "turn_id": "TT"})
        keys = [k for k in db.r.keys("mem:*") if not k.startswith(b"mem:sha:")]
        assert len(keys) == 9
