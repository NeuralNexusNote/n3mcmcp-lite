"""Layer 7: the CLI and the server management of cli.py (spec §3.15, §12.4, §13)."""
import http.server
import json
import os
import subprocess
import sys
import threading
import time

import pytest

import conftest
from n3mc_mcp import cli, config, paths


def _health(cfg, **over):
    h = {"status": "ok", "root": paths.data_dir(), "code_version": cli.own_code_version(cfg), "pid": 4242,
         "redis": config.redis_display(cfg["redis_url"])}
    h.update(over)
    return h


class FakePopen:
    instances = []

    def __init__(self, args, **kw):
        self.args, self.kw = args, kw
        self.pid = 987654
        self.code = kw.pop("_code", None)
        FakePopen.instances.append(self)

    def poll(self):
        return FakePopen.exit_code


@pytest.fixture
def real_start(monkeypatch):
    monkeypatch.setattr(cli, "_start_server", conftest.ORIG_START_SERVER)
    FakePopen.instances = []
    FakePopen.exit_code = None
    monkeypatch.setattr(cli.subprocess, "Popen", FakePopen)
    return FakePopen


class TestServerDecision:
    def test_decisions(self, cfg):
        assert cli._health_decision(_health(cfg), cfg) == "ok"
        assert cli._health_decision(_health(cfg, code_version="0" * 16), cfg) == "stale"
        assert cli._health_decision(_health(cfg, root="/elsewhere"), cfg) == "foreign"
        assert cli._health_decision({"status": "ok"}, cfg) == "legacy"
        assert cli._health_decision(None, cfg) == "none"

    def test_legacy_unverifiable_is_foreign(self, cfg, monkeypatch):
        monkeypatch.setattr(cli, "_health", lambda c, port=None: {"status": "ok", "pid": 1})
        monkeypatch.setattr(cli, "_is_own_server_process", lambda pid: False)
        assert cli.ensure_server(cfg) is False
        assert cli._FOREIGN_SERVER_ROOT.startswith("(unknown")

    def test_stale_shutdown_first(self, cfg, monkeypatch):
        seq = []
        hs = [_health(cfg, code_version="0" * 16), _health(cfg)]
        monkeypatch.setattr(cli, "_health", lambda c, port=None: hs.pop(0) if len(hs) > 1 else hs[0])
        monkeypatch.setattr(cli, "_request_shutdown", lambda c, root, port=None: seq.append("shutdown") or True)
        monkeypatch.setattr(cli, "_wait_port_free", lambda p, total=15: True)
        monkeypatch.setattr(cli, "_terminate", lambda pid: seq.append("terminate"))
        monkeypatch.setattr(cli, "_start_server", lambda c, max_wait=60: seq.append("start") or True)
        assert cli.ensure_server(cfg)
        assert seq == ["shutdown", "start"]

    def test_never_kills_by_pid_file(self, cfg, monkeypatch):
        killed = []
        monkeypatch.setattr(cli, "_terminate", lambda pid: killed.append(pid))
        with open(paths.pid_path(), "w") as f:
            f.write(str(os.getpid()))
        cli.ensure_server(cfg, max_wait=0)
        assert not killed

    def test_dead_pid_file_no_wait(self, cfg, monkeypatch):
        with open(paths.pid_path(), "w") as f:
            f.write("999999")
        t0 = time.time()
        cli.ensure_server(cfg, max_wait=60)
        assert time.time() - t0 < 5 and not os.path.exists(paths.pid_path())


class TestServerStart:
    def test_flags_and_log(self, cfg, real_start):
        FakePopen.exit_code = 1
        assert cli._start_server(cfg, max_wait=1) is False
        p = real_start.instances[0]
        assert p.args[-3:] == ["-m", "n3mc_mcp.cli", "--run-server"]
        if os.name == "nt":
            flags = p.kw["creationflags"]
            assert flags & subprocess.CREATE_NEW_PROCESS_GROUP and flags & subprocess.CREATE_NO_WINDOW
        else:
            assert p.kw["start_new_session"]
        assert p.kw["stdout"] == subprocess.DEVNULL and p.kw["stderr"].name.endswith("server.log")

    def test_early_exit(self, cfg, real_start):
        FakePopen.exit_code = 3
        assert cli.ensure_server(cfg, max_wait=5) is False
        assert paths.has_notice("server_start_failed") and os.path.exists(paths.start_failed_path())
        assert "exit code 3" in json.load(open(paths.notices_path(), encoding="utf-8"))["server_start_failed"]["message"]
        n = len(real_start.instances)
        cli.ensure_server(cfg, max_wait=5)
        assert len(real_start.instances) == n

    def test_success_clears(self, cfg, real_start, monkeypatch):
        cli._record_start_failure("exit code 1")
        monkeypatch.setattr(cli, "_health", lambda c, port=None: _health(cfg))
        assert cli.ensure_server(cfg)
        assert not paths.has_notice("server_start_failed") and not os.path.exists(paths.start_failed_path())

    def test_still_starting_not_replaced(self, cfg, real_start, monkeypatch):
        monkeypatch.setattr(cli, "_read_pid_file", lambda: (4242, time.time()))
        monkeypatch.setattr(cli, "_pid_is_starting", lambda pid, m: True)
        monkeypatch.setattr(cli, "_handle_other_port", lambda c, pid: False)
        assert cli.ensure_server(cfg, max_wait=0) is False
        assert not real_start.instances and cli._LAST_REASON == cli.reason_starting()


class TestServerCwd:
    def test_data_dir(self, cfg, real_start, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        FakePopen.exit_code = 0
        cli._start_server(cfg, max_wait=1)
        assert os.path.normcase(real_start.instances[0].kw["cwd"]) == os.path.normcase(paths.data_dir())
        assert real_start.instances[0].kw["env"]["N3MC_DATA_DIR"] == paths.data_dir()

    def test_source_checkout(self, cfg, real_start, monkeypatch):
        monkeypatch.chdir(conftest.REPO_ROOT)
        FakePopen.exit_code = 0
        cli._start_server(cfg, max_wait=1)
        assert os.path.normcase(real_start.instances[0].kw["cwd"]) == os.path.normcase(conftest.REPO_ROOT)


class TestStaleServerUnstoppable:
    def _setup(self, cfg, monkeypatch, after):
        hs = [_health(cfg, code_version="0" * 16), after]
        monkeypatch.setattr(cli, "_health", lambda c, port=None: hs.pop(0) if len(hs) > 1 else hs[0])
        monkeypatch.setattr(cli, "_stop_stale_server", lambda c, h: False)

    def test_other_process_replaced_it(self, cfg, monkeypatch):
        self._setup(cfg, monkeypatch, _health(cfg))
        assert cli.ensure_server(cfg)

    def test_gone_then_start(self, cfg, monkeypatch):
        self._setup(cfg, monkeypatch, None)
        before = len(conftest.START_CALLS)
        cli.ensure_server(cfg, max_wait=0)
        assert len(conftest.START_CALLS) == before + 1

    def test_still_old(self, cfg, monkeypatch, rconn, capsys):
        self._setup(cfg, monkeypatch, _health(cfg, code_version="0" * 16))
        assert cli.ensure_server(cfg) is False and cli._LAST_REASON == cli.REASON_STALE
        monkeypatch.setattr(cli, "ensure_server", lambda c, max_wait=60: (setattr(cli, "_LAST_REASON", cli.REASON_STALE), False)[1])
        cli.hook_submit(json.dumps({"session_id": "s", "prompt": "direct while stale"}))
        assert "an old N3MC Lite memory server could not be stopped" in capsys.readouterr().out
        assert any(b"direct while stale" in rconn.hget(k, "content") for k in rconn.keys("mem:0*"))


class TestNoProxy:
    def test_proxy_ignored(self, cfg, monkeypatch):
        hits = []

        class Proxy(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(502)
                self.end_headers()

            def log_message(self, *a):
                pass

        class Lite(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"status": "ok", "root": "lite"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        servers = [http.server.HTTPServer(("127.0.0.1", 0), h) for h in (Proxy, Lite)]
        for s in servers:
            threading.Thread(target=s.serve_forever, daemon=True).start()
        try:
            monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{servers[0].server_port}")
            status, data = cli._http(cfg, "GET", "/health", port=servers[1].server_port)
            assert status == 200 and data["root"] == "lite" and not hits
        finally:
            for s in servers:
                s.shutdown()


class TestCommands:
    @pytest.fixture
    def sent(self, monkeypatch):
        calls = []
        monkeypatch.setattr(cli, "ensure_server", lambda c, max_wait=60: True)
        monkeypatch.setattr(cli, "_post", lambda c, path, body, timeout=30, port=None:
                            (calls.append((path, body)), (200, {"text": f"TEXT {path}"}))[1])
        return calls

    def test_search(self, sent, capsys):
        assert cli.main(["--search", "kw", "--since", "2026-11-13", "--until", "2026-11-14"]) == 0
        assert sent == [("/search", {"query": "kw", "since": "2026-11-13", "until": "2026-11-14"})]
        assert capsys.readouterr().out == "TEXT /search\n"

    @pytest.mark.parametrize("argv", [["--search", "x", "--since", "yesterday"], ["--search", "x", "--until", "9999-12-31"],
                                      ["--search", "x", "--since", "2026-13-01"]])
    def test_bad_time(self, sent, capsys, argv):
        assert cli.main(argv) == 2
        err = capsys.readouterr().err
        key, value = argv[2].lstrip("-"), argv[3]
        assert err == (f"Error: {key} must be an ISO 8601 date or datetime, e.g. 2026-11-13 or "
                       f"2026-11-13T09:00:00+09:00 (got '{value}')\n") and not sent

    @pytest.mark.skipif(sys.platform != "win32", reason="the conversion fails only on Windows (§3.12)")
    def test_before_1970_on_windows(self, sent, capsys):
        # 1969-12-31 in the local time zone cannot be converted on Windows: unreadable, one line, exit 2
        assert cli.main(["--search", "x", "--since", "1969-12-31"]) == 2
        err = capsys.readouterr().err
        assert err.startswith("Error: since must be an ISO 8601") and err.count("\n") == 1 and not sent

    @pytest.mark.parametrize("argv,path,body", [
        (["--buffer", "fact"], "/tools/save_memory?nudge=0", {"content": "fact", "agent_name": "claude-code"}),
        (["--list"], "/tools/list_memories?nudge=0", {}),
        (["--recall-thread", "T", "--before", "1", "--after", "3"], "/tools/recall_thread?nudge=0",
         {"turn_id": "T", "before": 1, "after": 3}),
        (["--delete", "ID"], "/tools/delete_memory?nudge=0", {"id": "ID"}),
        (["--delete-session", "S"], "/tools/delete_memories_by_session?nudge=0", {"session_id": "S"}),
        (["--repair"], "/tools/repair_memory?nudge=0", {}),
    ])
    def test_tools(self, sent, capsys, argv, path, body):
        assert cli.main(argv) == 0
        assert sent == [(path, body)] and capsys.readouterr().out == f"TEXT {path}\n"

    def test_tool_error_texts(self, runner):
        out = runner.call("search_memory", {"query": "x", "until": "9999-12-31"}, nudge=False)
        assert out.startswith("Error: until must be")

    def test_search_endpoint_400(self, app_router, capsys):
        r = app_router.client.post("/search", json={"query": "x", "since": "2026-13-01"},
                                   headers={"Host": f"127.0.0.1:{conftest.TEST_PORT}"})
        assert r.status_code == 400

    def test_hooks_without_init(self):
        with pytest.raises(SystemExit) as e:
            cli.main(["--list", "--hooks"])
        assert e.value.code == 2


class TestServerPidAndIdentity:
    def test_write_own_pid(self):
        with open(paths.pid_path(), "w") as f:
            f.write("123")
        cli._write_own_pid()
        assert open(paths.pid_path()).read() == str(os.getpid())
        os.unlink(paths.pid_path())
        cli._write_own_pid()
        assert open(paths.pid_path()).read() == str(os.getpid())

    def test_run_server_order(self, monkeypatch):
        import uvicorn
        from n3mc_mcp import app as app_mod, processor
        from n3mc_mcp.database import Database
        order = []
        orig_cv = config.compute_code_version
        monkeypatch.setattr(config, "compute_code_version", lambda raw: (order.append("code_version"), orig_cv(raw))[1])
        monkeypatch.setattr(cli, "_write_own_pid", lambda: order.append("pid"))
        monkeypatch.setattr(processor, "import_backend", lambda: order.append("import"))
        monkeypatch.setattr(Database, "ensure_connected", lambda self, *a, **k: order.append("redis") or "ok")
        seen = {}
        monkeypatch.setattr(app_mod, "create_app", lambda cfg, db, code_version=None, **k: seen.update(cv=code_version) or object())
        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: order.append("listen"))
        cli.run_server()
        assert order.index("code_version") < order.index("import") < order.index("redis")
        assert order.count("pid") == 2 and order.index("pid", order.index("redis")) < order.index("listen")
        assert seen["cv"] == orig_cv(config.load_config()["_raw"])

    def test_handle_other_port(self, cfg, monkeypatch):
        sent = []
        monkeypatch.setattr(cli, "_listening_ports", lambda pid: [conftest.TEST_PORT + 3])
        monkeypatch.setattr(cli, "_health", lambda c, port=None: _health(cfg))
        monkeypatch.setattr(cli, "_request_shutdown", lambda c, root, port=None: sent.append(port) or True)
        monkeypatch.setattr(cli, "_wait_port_free", lambda p, total=15: True)
        assert cli._handle_other_port(cfg, 4242) and sent == [conftest.TEST_PORT + 3]

    def test_redis_mismatch_cleared(self, cfg):
        cli._server_ok(cfg, _health(cfg, redis="otherhost:6379/0"))
        assert paths.has_notice("redis_url_mismatch")
        cli._server_ok(cfg, _health(cfg))
        assert not paths.has_notice("redis_url_mismatch")


class TestForeignPort:
    @pytest.fixture(autouse=True)
    def foreign(self, monkeypatch):
        monkeypatch.setattr(cli, "_health", lambda c, port=None: {"status": "ok", "root": "/other/folder", "pid": 1})

    def test_search(self, capsys):
        cli.main(["--search", "x"])
        out = capsys.readouterr().out
        assert f"_(memory search unavailable: another N3MC folder's server is using port {conftest.TEST_PORT})_" in out
        assert paths.has_notice("port_conflict")

    def test_list_and_delete(self, capsys):
        cli.main(["--list"])
        assert capsys.readouterr().out.startswith("Error: memory backend unreachable.")
        cli.main(["--delete", "x"])
        assert "Nothing was deleted" in capsys.readouterr().out

    def test_hook_save_direct(self, rconn, capsys):
        cli.hook_submit(json.dumps({"session_id": "s", "prompt": "foreign direct"}))
        assert any(rconn.hget(k, "content") == b"[user] foreign direct" for k in rconn.keys("mem:0*"))


class TestStop:
    def _stop(self, cwd):
        return cli.stop(json.dumps({"session_id": "s", "cwd": str(cwd)}))

    def test_rules_cleanup_rotate(self, tmp_path, monkeypatch, capsys):
        proj = tmp_path / "proj"
        proj.mkdir()
        old = os.path.join(paths.data_dir(), "turn_id_old.txt")
        open(old, "w").write("x")
        os.utime(old, (time.time() - 31 * 86400,) * 2)
        open(paths.audit_log_path(), "w").write("y" * 100)
        monkeypatch.setattr(paths, "AUDIT_ROTATE_BYTES", 10)
        shut = []
        monkeypatch.setattr(cli, "_request_shutdown", lambda *a, **k: shut.append(1))
        assert self._stop(proj) == 0
        rules = proj / ".claude" / "rules" / "n3mc-lite-behavior.md"
        assert rules.read_text(encoding="utf-8").startswith("<!-- n3mc-lite-behavior v1 -->")
        assert not os.path.exists(old) and os.path.exists(paths.audit_log_path() + ".1")
        assert capsys.readouterr().out == "" and not shut

    def test_home_skipped(self, isolate):
        self._stop(isolate["home"])
        assert not (isolate["home"] / ".claude" / "rules").exists()

    def test_project_dir_env(self, tmp_path, monkeypatch):
        proj, sub = tmp_path / "p", tmp_path / "p" / "sub"
        sub.mkdir(parents=True)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
        self._stop(sub)
        assert (proj / ".claude" / "rules" / "n3mc-lite-behavior.md").exists()
        assert not (sub / ".claude").exists()

    def test_project_dir_home(self, isolate, tmp_path, monkeypatch):
        sub = tmp_path / "w"
        sub.mkdir()
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(isolate["home"]))
        self._stop(sub)
        assert not (isolate["home"] / ".claude").exists() and not (sub / ".claude").exists()
