"""Layer 7: CLI commands and server management (spec §3.15, §12.4, §13)."""
import http.server
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import conftest
from conftest import parse_json
from n3mc_mcp import cli, config, context, paths, processor
from n3mc_mcp.config import compute_code_version, read_config_raw

ROOT = Path(__file__).resolve().parent.parent


def own_cv():
    return compute_code_version(read_config_raw())


def health(root=None, cv=None, pid=111, redis_="127.0.0.1:1/0"):
    h = {"status": "ok", "code_version": cv or own_cv(), "pid": pid, "redis": redis_}
    if root is not False:
        h["root"] = str(root or paths.data_dir())
    return h


class FakeProc:
    def __init__(self, rc=None, pid=4321):
        self.rc, self.pid = rc, pid

    def poll(self):
        return self.rc


class TestServerDecision:
    def test_decisions(self):
        assert cli._health_decision(None) == "none"
        assert cli._health_decision(health()) == "ok"
        assert cli._health_decision(health(cv="0" * 16)) == "stale"
        assert cli._health_decision(health(root="/other/folder")) == "foreign"
        assert cli._health_decision(health(root=False)) == "legacy"

    def test_stale_server_gets_shutdown_first(self, monkeypatch, cfg):
        calls = []
        states = iter([health(cv="0" * 16), None])
        monkeypatch.setattr(cli, "_health", lambda p: next(states, None))
        monkeypatch.setattr(cli, "_request_shutdown", lambda p, r: (calls.append("shutdown"), (200, {}))[1])
        monkeypatch.setattr(cli, "_wait_port_free", lambda p, t=15: True)
        monkeypatch.setattr(cli, "_terminate", lambda pid: calls.append("terminate"))
        monkeypatch.setattr(cli, "_start_server", lambda c, w=60: (calls.append("start"), False)[1])
        cli.ensure_server(cfg, max_wait=0)
        assert calls == ["shutdown", "start"]

    def test_unconfirmed_legacy_is_foreign(self, monkeypatch, cfg):
        monkeypatch.setattr(cli, "_health", lambda p: health(root=False))
        monkeypatch.setattr(cli, "_is_own_server_process", lambda pid: False)
        assert cli.ensure_server(cfg) is False
        assert cli._FOREIGN_SERVER_ROOT == "(unknown: the server did not report its folder)"

    def test_pid_number_is_never_killed(self, monkeypatch, cfg):
        paths.pid_path().write_text(str(os.getpid()), encoding="utf-8")
        killed = []
        monkeypatch.setattr(cli, "_terminate", lambda pid: killed.append(pid))
        monkeypatch.setattr(cli, "_health", lambda p: health(cv="0" * 16, pid=999999))
        monkeypatch.setattr(cli, "_request_shutdown", lambda p, r: (404, {}))
        assert cli.ensure_server(cfg, max_wait=0) is False
        assert killed == [] and paths.pid_path().exists()

    def test_dead_pid_file_does_not_wait(self, cfg):
        paths.pid_path().write_text("999999", encoding="utf-8")
        t0 = time.monotonic()
        assert cli.ensure_server(cfg, max_wait=5) is False
        assert time.monotonic() - t0 < 2 and not paths.pid_path().exists()


class TestServerStart:
    def _capture(self, monkeypatch, proc):
        seen = {}

        def popen(cmd, **kw):
            seen.update(kw, cmd=cmd)
            return proc

        monkeypatch.setattr(cli, "_popen", popen)
        return seen

    def test_flags_and_log(self, monkeypatch, cfg):
        seen = self._capture(monkeypatch, FakeProc())
        assert cli._start_server(cfg, max_wait=0) is False
        assert seen["cmd"][-3:] == ["-m", "n3mc_mcp.cli", "--run-server"]
        assert Path(seen["stderr"].name) == paths.server_log_path()
        if os.name == "nt":
            assert seen["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
            assert seen["creationflags"] & subprocess.CREATE_NO_WINDOW
        else:
            assert seen["start_new_session"] is True
        assert paths.pid_path().read_text(encoding="utf-8") == "4321"
        assert cli.last_unavailable_reason() == cli.STILL_STARTING

    def test_early_exit(self, monkeypatch, cfg):
        self._capture(monkeypatch, FakeProc(rc=3))
        assert cli._start_server(cfg, max_wait=1) is False
        assert paths.start_failed_path().exists() and not paths.pid_path().exists()
        n = json.loads(paths.notices_path().read_text(encoding="utf-8"))
        assert "exit code 3" in n["server_start_failed"]["message"]
        starts = []
        monkeypatch.setattr(cli, "_popen", lambda *a, **k: starts.append(1))
        assert cli.ensure_server(cfg, max_wait=0) is False and starts == []

    def test_success_clears_marker(self, monkeypatch, cfg):
        cli._record_start_failure("earlier")
        monkeypatch.setattr(cli, "_health", lambda p: health(redis_=config.redis_display(cfg["redis_url"])))
        assert cli.ensure_server(cfg) is True
        assert not paths.start_failed_path().exists()
        assert "server_start_failed" not in json.loads(paths.notices_path().read_text(encoding="utf-8"))

    def test_still_starting_pid_not_replaced(self, monkeypatch, cfg):
        paths.pid_path().write_text("", encoding="utf-8")
        starts = []
        monkeypatch.setattr(cli, "_popen", lambda *a, **k: starts.append(1))
        assert cli.ensure_server(cfg, max_wait=0.6) is False
        assert starts == [] and paths.pid_path().exists()


class TestServerCwd:
    def test_cwd_is_data_folder(self, monkeypatch, cfg, tmp_path):
        monkeypatch.chdir(tmp_path)
        seen = TestServerStart()._capture(monkeypatch, FakeProc())
        cli._start_server(cfg, max_wait=0)
        assert paths.same_folder(seen["cwd"], paths.data_dir())
        assert paths.same_folder(seen["env"]["N3MC_DATA_DIR"], paths.data_dir())

    def test_source_checkout_cwd(self, monkeypatch, cfg):
        monkeypatch.chdir(ROOT)
        seen = TestServerStart()._capture(monkeypatch, FakeProc())
        cli._start_server(cfg, max_wait=0)
        assert paths.same_folder(seen["cwd"], ROOT) and "-P" not in seen["cmd"]


class TestStaleServerUnstoppable:
    def _run(self, monkeypatch, cfg, second):
        seq = iter([health(cv="0" * 16), second])
        monkeypatch.setattr(cli, "_health", lambda p: next(seq, second))
        monkeypatch.setattr(cli, "_stop_stale_server", lambda p, r, pid=None: False)
        starts = []
        monkeypatch.setattr(cli, "_start_server", lambda c, w=60: (starts.append(1), False)[1])
        return cli.ensure_server(cfg, max_wait=0), starts

    def test_other_process_replaced_it(self, monkeypatch, cfg):
        ok, starts = self._run(monkeypatch, cfg, health(redis_=config.redis_display(cfg["redis_url"])))
        assert ok is True and starts == []

    def test_gone_then_started(self, monkeypatch, cfg):
        ok, starts = self._run(monkeypatch, cfg, None)
        assert ok is False and starts == [1]

    def test_still_old(self, monkeypatch, cfg, capsys, rconn):
        ok, starts = self._run(monkeypatch, cfg, health(cv="0" * 16))
        assert ok is False and starts == [] and cli.last_unavailable_reason() == cli.OLD_SERVER
        monkeypatch.setattr(cli, "ensure_server",
                            lambda c, max_wait=60: (setattr(cli, "_UNAVAILABLE_REASON", cli.OLD_SERVER), False)[1])
        monkeypatch.setattr(cli, "_read_stdin", lambda: json.dumps({"prompt": "old server", "session_id": "s"}).encode())
        cli.main(["--hook-submit"])
        out = capsys.readouterr().out
        assert "an old N3MC Lite memory server could not be stopped" in out
        assert any(rconn.hget(k, "content") == b"[user] old server" for k in rconn.keys("mem:*")
                   if not k.startswith(b"mem:sha:"))


def _serve(handler_body, hits):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            body = json.dumps(handler_body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class TestNoProxy:
    def test_proxy_bypassed(self, monkeypatch):
        server_hits, proxy_hits = [], []
        srv = _serve({"status": "ok", "who": "lite"}, server_hits)
        proxy = _serve({"status": "ok", "who": "proxy"}, proxy_hits)
        try:
            monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy.server_address[1]}")
            monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy.server_address[1]}")
            monkeypatch.delenv("NO_PROXY", raising=False)
            monkeypatch.delenv("no_proxy", raising=False)
            status, data = cli._request("GET", srv.server_address[1], "/health")
            assert data["who"] == "lite" and server_hits and not proxy_hits
        finally:
            srv.shutdown()
            proxy.shutdown()


class TestCommands:
    @pytest.fixture
    def sent(self, monkeypatch):
        sent = []

        def fake_post(port, path, body, timeout=30):
            sent.append((path, body))
            return 200, {"text": f"TEXT for {path}"}

        monkeypatch.setattr(cli, "_post", fake_post)
        monkeypatch.setattr(cli, "ensure_server", lambda cfg, max_wait=60: True)
        return sent

    def test_search(self, sent, capsys):
        assert cli.main(["--search", "kw", "--since", "2026-11-01", "--until", "2026-11-02"]) == 0
        path, body = sent[0]
        assert path == "/search" and body["since"] == "2026-11-01" and body["until"] == "2026-11-02"
        assert "max_chars" not in body and capsys.readouterr().out == "TEXT for /search\n"

    @pytest.mark.parametrize("args", [["--since", "yesterday"], ["--until", "9999-12-31"], ["--since", "1969-12-31"]])
    def test_bad_bounds(self, sent, capsys, args):
        assert cli.main(["--search", "kw"] + args) == 2
        assert capsys.readouterr().err.startswith("Error: ") and sent == []

    @pytest.mark.parametrize("argv,path,body", [
        (["--buffer", "remember", "--session-id", "s", "--turn-id", "t", "--importance", "1.5"],
         "/tools/save_memory?nudge=0",
         {"content": "remember", "agent_name": "claude-code", "session_id": "s", "turn_id": "t", "importance": 1.5}),
        (["--list", "--limit", "5"], "/tools/list_memories?nudge=0", {"limit": 5}),
        (["--recall-thread", "t1", "--before", "1", "--after", "3"], "/tools/recall_thread?nudge=0",
         {"turn_id": "t1", "before": 1, "after": 3}),
        (["--delete", "abc"], "/tools/delete_memory?nudge=0", {"id": "abc"}),
        (["--delete-session", "s9"], "/tools/delete_memories_by_session?nudge=0", {"session_id": "s9"}),
        (["--repair"], "/tools/repair_memory?nudge=0", {}),
    ])
    def test_tool_commands(self, sent, capsys, argv, path, body):
        assert cli.main(argv) == 0
        assert sent == [(path, body)] and capsys.readouterr().out == f"TEXT for {path}\n"

    def test_buffer_stdin(self, sent, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_read_stdin", lambda: "from stdin".encode())
        cli.main(["--buffer", "-"])
        assert sent[0][1]["content"] == "from stdin"

    def test_extreme_bounds_from_server(self, server_client, cfg, runner):
        h = {"Host": f"127.0.0.1:{cfg['server_port']}"}
        r = server_client.post("/search", json={"query": "x", "until": "9999-12-31"}, headers=h)
        assert r.status_code == 400 and r.json()["detail"].startswith("Error: until must be")
        assert runner.call("search_memory", {"query": "x", "since": "1969-12-31"}).startswith("Error: since must be")


class TestServerPidAndIdentity:
    def test_write_own_pid(self):
        paths.pid_path().write_text("12345", encoding="utf-8")
        cli._write_own_pid()
        assert paths.pid_path().read_text(encoding="utf-8") == str(os.getpid())
        paths.pid_path().unlink()
        cli._write_own_pid()
        assert paths.pid_path().read_text(encoding="utf-8") == str(os.getpid())

    def test_run_server_order(self, monkeypatch):
        events = []
        from n3mc_mcp import app as app_mod, database
        import uvicorn
        monkeypatch.setattr(cli, "_write_own_pid", lambda: events.append("pid"))
        monkeypatch.setattr(cli, "compute_code_version", lambda raw: (events.append("cv"), "c0ffee0123456789")[1])
        monkeypatch.setattr(processor, "import_backend", lambda: events.append("model-libs"))
        monkeypatch.setattr(database.Database, "ensure_connected", lambda self, enforce=True: events.append("redis"))
        real_create = app_mod.create_app
        monkeypatch.setattr(app_mod, "create_app",
                            lambda *a, **k: (events.append(("app", k["code_version"])), real_create(*a, **k))[1])
        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: events.append("run"))
        cli.run_server()
        assert events[0] == "pid" and events.index("cv") < events.index("model-libs") < events.index("redis")
        assert ("app", "c0ffee0123456789") in events
        assert events[-2:] == ["pid", "run"]

    def test_redis_url_mismatch_then_cleared(self, monkeypatch, cfg):
        monkeypatch.setattr(cli, "_health", lambda p: health(redis_="otherhost:6379/0"))
        cli.ensure_server(cfg)
        assert "redis_url_mismatch" in json.loads(paths.notices_path().read_text(encoding="utf-8"))
        monkeypatch.setattr(cli, "_health", lambda p: health(redis_=config.redis_display(cfg["redis_url"])))
        cli.ensure_server(cfg)
        assert "redis_url_mismatch" not in json.loads(paths.notices_path().read_text(encoding="utf-8"))


class TestForeignPort:
    def test_foreign(self, monkeypatch, capsys, cfg, rconn):
        monkeypatch.setattr(cli, "_health", lambda p: health(root="/another/n3mc"))
        cli.main(["--search", "x"])
        out = capsys.readouterr().out
        assert f"another N3MC folder's server is using port {cfg['server_port']}" in out
        cli.main(["--list"])
        assert capsys.readouterr().out.startswith("Error: memory backend unreachable.")
        cli.main(["--delete", "x"])
        assert "Nothing was deleted" in capsys.readouterr().out
        monkeypatch.setattr(cli, "_read_stdin", lambda: json.dumps({"prompt": "foreign", "session_id": "s"}).encode())
        cli.main(["--hook-submit"])
        assert any(rconn.hget(k, "content") == b"[user] foreign" for k in rconn.keys("mem:*")
                   if not k.startswith(b"mem:sha:"))


class TestStop:
    def _stop(self, monkeypatch, capsys, payload):
        monkeypatch.setattr(cli, "_read_stdin", lambda: json.dumps(payload).encode())
        shut = []
        monkeypatch.setattr(cli, "_request_shutdown", lambda *a: shut.append(a))
        assert cli.main(["--stop"]) == 0
        assert capsys.readouterr().out == "" and shut == []

    def test_housekeeping(self, monkeypatch, capsys, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        old = paths.turn_id_path("old")
        old.write_text("x", encoding="utf-8")
        os.utime(old, (time.time() - 31 * 86400,) * 2)
        new = paths.turn_id_path("new")
        new.write_text("y", encoding="utf-8")
        paths.audit_log_path().write_text("a" * 200, encoding="utf-8")
        monkeypatch.setattr(cli, "AUDIT_ROTATE_BYTES", 100)
        self._stop(monkeypatch, capsys, {"cwd": str(proj)})
        rules = proj / ".claude" / "rules" / "n3mc-lite-behavior.md"
        assert rules.read_text(encoding="utf-8").startswith("<!-- n3mc-lite-behavior v1 -->")
        assert not old.exists() and new.exists()
        assert (paths.data_dir() / "audit.log.1").exists() and not paths.audit_log_path().exists()

    def test_home_is_skipped(self, monkeypatch, capsys, isolated):
        self._stop(monkeypatch, capsys, {"cwd": str(isolated["home"])})
        assert not (isolated["home"] / ".claude" / "rules").exists()

    def test_project_dir_wins(self, monkeypatch, capsys, tmp_path):
        proj, sub = tmp_path / "proj", tmp_path / "proj" / "sub"
        sub.mkdir(parents=True)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
        self._stop(monkeypatch, capsys, {"cwd": str(sub)})
        assert (proj / ".claude" / "rules" / "n3mc-lite-behavior.md").exists()
        assert not (sub / ".claude").exists()

    def test_project_dir_home(self, monkeypatch, capsys, isolated, tmp_path):
        sub = tmp_path / "work"
        sub.mkdir()
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(isolated["home"]))
        self._stop(monkeypatch, capsys, {"cwd": str(sub)})
        assert not (isolated["home"] / ".claude").exists() and not (sub / ".claude").exists()
