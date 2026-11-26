"""Layer 7: CLI commands and server process management (spec §9 Layer 7). Fake HTTP and processes."""
import json
import os
import subprocess
import sys
import time

import pytest

from n3mc_mcp import cli, paths
from n3mc_mcp.tools import REASON_STALE


def _health(cfg, **kw):
    h = {"status": "ok", "root": paths.data_dir(), "code_version": cli.own_code_version(cfg), "pid": 4242,
         "redis": "127.0.0.1:6390/0"}
    h.update(kw)
    return h


class FakeHttp:
    """A scripted server: health answers come from a list (the last one repeats)."""

    def __init__(self, healths, shutdown="ok"):
        self.healths = list(healths)
        self.shutdown = shutdown
        self.calls = []

    def health(self, port):
        self.calls.append(("health", port))
        if len(self.healths) > 1:
            return self.healths.pop(0)
        return self.healths[0]


@pytest.fixture
def fake(monkeypatch):
    def install(healths, shutdown="ok"):
        f = FakeHttp(healths, shutdown)
        monkeypatch.setattr(cli, "_health", f.health)
        monkeypatch.setattr(cli, "_request_shutdown", lambda port, root: (f.calls.append(("shutdown", port)), f.shutdown)[1])
        monkeypatch.setattr(cli, "_wait_port_free", lambda port, seconds=15: True)
        return f
    return install


class TestServerDecision:
    def test_ok(self, cfg, fake):
        fake([_health(cfg)])
        assert cli.ensure_server(cfg) is True

    def test_stale_stopped_with_shutdown(self, cfg, fake, monkeypatch):
        f = fake([_health(cfg, code_version="0" * 16), None])
        started = []
        monkeypatch.setattr(cli, "_start_server", lambda cfg, max_wait=60: started.append(1) or True)
        assert cli.ensure_server(cfg) is True
        assert ("shutdown", cfg["server_port"]) in f.calls and started

    def test_foreign_not_stopped(self, cfg, fake):
        f = fake([_health(cfg, root="/other/folder")])
        assert cli.ensure_server(cfg) is False
        assert not any(c[0] == "shutdown" for c in f.calls)
        assert cli._FOREIGN_SERVER_ROOT == "/other/folder"
        n = json.load(open(paths.notices_path(), encoding="utf-8"))
        assert "port_conflict" in n

    def test_no_root_unconfirmed(self, cfg, fake, monkeypatch):
        h = _health(cfg)
        del h["root"]
        f = fake([h])
        monkeypatch.setattr(cli, "_is_own_server_process", lambda pid: False)
        assert cli.ensure_server(cfg) is False
        assert "(unknown: the server did not report its folder)" in cli._FOREIGN_SERVER_ROOT

    def test_never_kills_by_pid_file(self, cfg, fake, monkeypatch):
        fake([None])
        paths.ensure_data_dir()
        open(paths.pid_path(), "w").write(str(os.getpid()))
        killed = []
        monkeypatch.setattr(cli, "_terminate", lambda pid: killed.append(pid))
        monkeypatch.setattr(cli, "_is_own_server_process", lambda pid: False)
        monkeypatch.setattr(cli, "_start_server", lambda cfg, max_wait=60: False)
        cli.ensure_server(cfg)
        assert not killed

    def test_dead_pid_file_no_wait(self, cfg, fake, monkeypatch):
        fake([None])
        paths.ensure_data_dir()
        open(paths.pid_path(), "w").write("999999")
        monkeypatch.setattr(cli, "_is_own_server_process", lambda pid: False)
        started = []
        monkeypatch.setattr(cli, "_start_server", lambda cfg, max_wait=60: started.append(time.monotonic()) or True)
        t = time.monotonic()
        assert cli.ensure_server(cfg)
        assert time.monotonic() - t < 2 and not os.path.exists(paths.pid_path())


class FakeProc:
    def __init__(self, pid=5555, code=None):
        self.pid = pid
        self.code = code

    def poll(self):
        return self.code


class TestServerStart:
    def test_flags_and_command(self, cfg, monkeypatch):
        seen = {}

        def fake_popen(cmd, **kw):
            seen["cmd"] = cmd
            seen.update(kw)
            return FakeProc()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        _real_popen()(cfg)
        assert seen["cmd"][-3:] == ["-m", "n3mc_mcp.cli", "--run-server"]
        if os.name == "nt":
            assert seen["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
            assert seen["creationflags"] & subprocess.CREATE_NO_WINDOW
        else:
            assert seen["start_new_session"] is True
        assert seen["stdout"] == subprocess.DEVNULL and seen["stdin"] == subprocess.DEVNULL
        assert os.path.basename(seen["stderr"].name) == "server.log"
        assert seen["env"]["N3MC_DATA_DIR"] == paths.data_dir()

    def test_early_exit(self, cfg, monkeypatch):
        monkeypatch.setattr(cli, "_popen_server", lambda cfg: FakeProc(code=3))
        monkeypatch.setattr(cli, "_health", lambda port: None)
        assert cli._start_server(cfg, 5) is False
        assert os.path.exists(paths.start_failed_path())
        n = json.load(open(paths.notices_path(), encoding="utf-8"))
        assert "exit code 3" in n["server_start_failed"]["message"]
        assert not os.path.exists(paths.pid_path())
        started = []
        monkeypatch.setattr(cli, "_start_server", lambda cfg, max_wait=60: started.append(1))
        assert cli.ensure_server(cfg) is False and not started

    def test_success_clears(self, cfg, monkeypatch):
        cli._notify("server_start_failed", "x")
        cli._mark_start_failed()
        monkeypatch.setattr(cli, "_health", lambda port: _health(cfg))
        assert cli.ensure_server(cfg) is True
        assert not os.path.exists(paths.start_failed_path())
        assert "server_start_failed" not in json.load(open(paths.notices_path(), encoding="utf-8"))

    def test_still_starting_not_duplicated(self, cfg, monkeypatch):
        paths.ensure_data_dir()
        open(paths.pid_path(), "w").write("4321")
        monkeypatch.setattr(cli, "_health", lambda port: None)
        monkeypatch.setattr(cli, "_is_own_server_process", lambda pid: True)
        monkeypatch.setattr(cli, "_handle_other_port", lambda cfg, pid: False)
        popen = []
        monkeypatch.setattr(cli, "_popen_server", lambda cfg: popen.append(1))
        assert cli.ensure_server(cfg, max_wait=0) is False
        assert not popen and os.path.exists(paths.pid_path())


def _real_popen():
    import conftest
    return conftest.REAL_POPEN


class TestServerCwd:
    def test_cwd_is_data_dir(self, cfg, monkeypatch, tmp_path):
        seen = {}
        monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: (seen.update(kw), FakeProc())[1])
        monkeypatch.chdir(tmp_path)
        _real_popen()(cfg)
        assert seen["cwd"] == paths.data_dir()

    def test_cwd_in_checkout(self, cfg, monkeypatch):
        seen = {}
        monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: (seen.update(kw), FakeProc())[1])
        repo = os.path.dirname(os.path.dirname(os.path.abspath(cli.__file__)))
        monkeypatch.chdir(repo)
        _real_popen()(cfg)
        assert os.path.normcase(seen["cwd"]) == os.path.normcase(repo)


class TestStaleServerUnstoppable:
    def _setup(self, cfg, fake, after):
        stale = _health(cfg, code_version="f" * 16)
        return fake([stale, after], shutdown="failed")

    def test_other_process_replaced_it(self, cfg, fake):
        self._setup(cfg, fake, _health(cfg))
        assert cli.ensure_server(cfg) is True

    def test_gone_then_start(self, cfg, fake, monkeypatch):
        self._setup(cfg, fake, None)
        monkeypatch.setattr(cli, "_start_server", lambda cfg, max_wait=60: True)
        assert cli.ensure_server(cfg) is True

    def test_still_stale_not_used(self, cfg, fake, monkeypatch, rconn):
        self._setup(cfg, fake, _health(cfg, code_version="f" * 16))
        assert cli.ensure_server(cfg) is False
        assert cli.last_unavailable_reason() == REASON_STALE
        out = cli._search_request(cfg, False, {"query": "x"})
        assert "an old N3MC Lite memory server could not be stopped" in out


class TestNoProxy:
    def test_proxy_ignored(self, monkeypatch):
        import conftest
        import http.server
        import threading

        hits = {"proxy": 0}

        class Proxy(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits["proxy"] += 1
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status": "proxy"}')

            def log_message(self, *a):
                pass

        class Real(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status": "ok", "root": "real"}')

            def log_message(self, *a):
                pass

        ps = http.server.HTTPServer(("127.0.0.1", 0), Proxy)
        rs = http.server.HTTPServer(("127.0.0.1", 0), Real)
        for s in (ps, rs):
            threading.Thread(target=s.serve_forever, daemon=True).start()
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{ps.server_port}")
        monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{ps.server_port}")
        try:
            status, body = conftest.REAL_REQUEST(rs.server_port, "GET", "/health")
            assert body["root"] == "real" and hits["proxy"] == 0
        finally:
            ps.shutdown()
            rs.shutdown()


class TestCommands:
    @pytest.fixture
    def posts(self, cfg, monkeypatch):
        calls = []
        monkeypatch.setattr(cli, "ensure_server", lambda cfg, max_wait=60: True)

        def fake_post(port, path, body, timeout=30):
            calls.append((path, body))
            return 200, {"text": f"TEXT for {path}"}

        monkeypatch.setattr(cli, "_post", fake_post)
        return calls

    def test_search(self, posts, capsys):
        assert cli.main(["--search", "kw", "--since", "2026-11-13", "--until", "2026-11-14"]) == 0
        path, body = posts[-1]
        assert path == "/search" and body["since"] == "2026-11-13" and "max_chars" not in body
        assert capsys.readouterr().out == "TEXT for /search\n"

    @pytest.mark.parametrize("args,path", [
        (["--buffer", "remember"], "/tools/save_memory?nudge=0"),
        (["--list"], "/tools/list_memories?nudge=0"),
        (["--recall-thread", "t1", "--before", "1", "--after", "3"], "/tools/recall_thread?nudge=0"),
        (["--delete", "abc"], "/tools/delete_memory?nudge=0"),
        (["--delete-session", "s"], "/tools/delete_memories_by_session?nudge=0"),
        (["--repair"], "/tools/repair_memory?nudge=0"),
    ])
    def test_tool_commands(self, posts, capsys, args, path):
        assert cli.main(args) == 0
        assert posts[-1][0] == path
        assert capsys.readouterr().out == f"TEXT for {path}\n"

    def test_recall_args(self, posts):
        cli.main(["--recall-thread", "t1", "--before", "1", "--after", "3"])
        assert posts[-1][1] == {"turn_id": "t1", "before": 1, "after": 3}

    @pytest.mark.parametrize("args", [["--since", "yesterday"], ["--until", "9999-12-31"], ["--since", "2026-13-01"]])
    def test_bad_bounds(self, posts, capsys, args):
        if args == ["--until", "9999-12-31"] and os.name != "nt":
            pytest.skip("9999-12-31 converts on this platform")
        assert cli.main(["--search", "x", *args]) == 2
        err = capsys.readouterr().err.strip().splitlines()
        assert len(err) == 1 and err[0].startswith(f"Error: {args[0][2:]} must be an ISO 8601")

    @pytest.mark.skipif(os.name != "nt", reason="1969-12-31 converts outside Windows")
    def test_1969_windows(self, posts, capsys):
        assert cli.main(["--search", "x", "--since", "1969-12-31"]) == 2


class TestServerPidAndIdentity:
    def test_write_own_pid(self):
        paths.ensure_data_dir()
        open(paths.pid_path(), "w").write("1")
        cli._write_own_pid()
        assert open(paths.pid_path()).read() == str(os.getpid())
        os.remove(paths.pid_path())
        cli._write_own_pid()
        assert open(paths.pid_path()).read() == str(os.getpid())

    def test_redis_mismatch_cleared(self, cfg):
        cli._on_server_ok(cfg, {"redis": "elsewhere:1/0"})
        assert "redis_url_mismatch" in json.load(open(paths.notices_path(), encoding="utf-8"))
        from n3mc_mcp.config import redis_display
        cli._on_server_ok(cfg, {"redis": redis_display(cfg["redis_url"])})
        assert "redis_url_mismatch" not in json.load(open(paths.notices_path(), encoding="utf-8"))

    def test_code_version_before_redis(self, monkeypatch):
        order = []
        import n3mc_mcp.config as CF
        import n3mc_mcp.database as D
        real_cv = cli.compute_code_version
        monkeypatch.setattr(cli, "compute_code_version", lambda b: (order.append("cv"), real_cv(b))[1])
        monkeypatch.setattr(D.Database, "ensure_connected", lambda self, ephemeral=True: (order.append("redis"), "x")[1])
        seen = {}

        def fake_create(cfg, db, code_version, port=None):
            seen["cv"] = code_version
            raise SystemExit(0)

        import n3mc_mcp.app as A
        monkeypatch.setattr(A, "create_app", fake_create)
        from n3mc_mcp import processor
        monkeypatch.setattr(processor, "import_backend", lambda: order.append("import"))
        with pytest.raises(SystemExit):
            cli.run_server()
        assert order.index("cv") < order.index("redis") < order.index("import")
        assert len(seen["cv"]) == 16


class TestForeignPort:
    def test_foreign(self, cfg, monkeypatch, rconn, capsys):
        monkeypatch.setattr(cli, "_health", lambda port: _health(cfg, root="/elsewhere"))
        assert cli.main(["--search", "x"]) == 0
        assert f"another N3MC folder's server is using port {cfg['server_port']}" in capsys.readouterr().out
        cli.main(["--list"])
        assert capsys.readouterr().out.startswith("Error: memory backend unreachable.")
        cli.main(["--delete", "x"])
        assert '"status": "error"' in capsys.readouterr().out
        cli.main(["--buffer", "foreign direct"])
        out = json.loads(capsys.readouterr().out)
        assert out["saved"] and out["warning"] == "embedding_unavailable"


class TestStop:
    def test_cleanup(self, cfg, tmp_path, monkeypatch, capsys):
        proj = tmp_path / "proj"
        proj.mkdir()
        paths.ensure_data_dir()
        old = paths.turn_id_path("old")
        open(old, "w").write("t")
        os.utime(old, (time.time() - 31 * 86400,) * 2)
        new = paths.turn_id_path("new")
        open(new, "w").write("t")
        open(paths.audit_path(), "w").write("x" * 100)
        monkeypatch.setattr(cli, "AUDIT_ROTATE_BYTES", 10)
        cli.stop_cleanup(json.dumps({"cwd": str(proj)}))
        assert (proj / ".claude" / "rules" / "n3mc-lite-behavior.md").exists()
        assert not os.path.exists(old) and os.path.exists(new)
        assert os.path.exists(paths.audit_rotated_path()) and not os.path.exists(paths.audit_path())
        assert capsys.readouterr().out == ""

    def test_home_skipped(self, isolate):
        cli.stop_cleanup(json.dumps({"cwd": str(isolate["home"])}))
        assert not (isolate["home"] / ".claude" / "rules").exists()

    def test_project_dir(self, tmp_path, monkeypatch):
        proj, sub = tmp_path / "p", tmp_path / "p" / "sub"
        sub.mkdir(parents=True)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
        cli.stop_cleanup(json.dumps({"cwd": str(sub)}))
        assert (proj / ".claude" / "rules" / "n3mc-lite-behavior.md").exists()
        assert not (sub / ".claude").exists()

    def test_project_dir_home(self, isolate, tmp_path, monkeypatch):
        sub = tmp_path / "x"
        sub.mkdir()
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(isolate["home"]))
        cli.stop_cleanup(json.dumps({"cwd": str(sub)}))
        assert not (isolate["home"] / ".claude").exists() and not (sub / ".claude").exists()


class TestStopServer:
    def test_none(self, cfg, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_health", lambda port: None)
        assert cli.main(["--stop-server"]) == 0
        assert "No N3MC Lite server of this data folder is running." in capsys.readouterr().out

    def test_stopped(self, cfg, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_health", lambda port: _health(cfg))
        monkeypatch.setattr(cli, "_request_shutdown", lambda port, root: "ok")
        monkeypatch.setattr(cli, "_wait_port_free", lambda port, seconds=15: True)
        assert cli.main(["--stop-server"]) == 0
        assert "Stopped the N3MC Lite server (pid 4242)." in capsys.readouterr().out

    def test_could_not(self, cfg, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_health", lambda port: _health(cfg))
        monkeypatch.setattr(cli, "_request_shutdown", lambda port, root: "ok")
        monkeypatch.setattr(cli, "_wait_port_free", lambda port, seconds=15: False)
        assert cli.main(["--stop-server"]) == 1
        assert "Could not stop the N3MC Lite server (pid 4242)" in capsys.readouterr().err

    def test_failed_request_but_free(self, cfg, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_health", lambda port: _health(cfg))
        monkeypatch.setattr(cli, "_request_shutdown", lambda port, root: "failed")
        monkeypatch.setattr(cli, "_wait_port_free", lambda port, seconds=15: True)
        assert cli.main(["--stop-server"]) == 0

    def test_foreign(self, cfg, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_health", lambda port: _health(cfg, root="/other"))
        assert cli.main(["--stop-server"]) == 0
        assert "No N3MC Lite server" in capsys.readouterr().out
