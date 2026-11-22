"""Layer 7: CLI commands and server management (no real server is ever started)."""

import io
import json
import os
import socket
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from n3mc_mcp import cli, config, paths

ORIG_START_SERVER = cli._start_server      # conftest replaces cli._start_server for every test


def own_health(**kw):
    h = {"status": "ok", "root": paths.data_dir(), "code_version": config.code_version(), "pid": 4242,
         "redis": "127.0.0.1:6390/0"}
    h.update(kw)
    return h


def pathlib_posix(p):
    import pathlib
    return pathlib.Path(p).as_posix()


def set_stdin(monkeypatch, text):
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(text.encode("utf-8")), isatty=lambda: False))


@pytest.fixture
def conf(isolate):
    return config.load_config()


class TestServerDecision:
    def test_decisions(self, conf):
        assert cli._health_decision(own_health()) == "ok"
        assert cli._health_decision(own_health(code_version="0" * 16)) == "stale"
        assert cli._health_decision(own_health(root="C:/other")) == "foreign"
        h = own_health()
        del h["root"]
        assert cli._health_decision(h) == "legacy"
        assert cli._health_decision(None) == "none"

    def test_stale_server_is_asked_to_shutdown_first(self, conf, monkeypatch):
        calls = []
        monkeypatch.setattr(cli, "_get", lambda port, path, timeout=5: own_health(code_version="1" * 16) if not calls else (_ for _ in ()).throw(OSError()))
        monkeypatch.setattr(cli, "_request_shutdown", lambda port: calls.append("shutdown") or {"status": "ok"})
        monkeypatch.setattr(cli, "_wait_port_free", lambda port, s=15: True)
        monkeypatch.setattr(cli, "_kill_process", lambda pid: pytest.fail("must not kill by pid"))
        assert cli.ensure_server(conf, max_wait=0) is False      # stopped, new one "starting" (patched start)
        assert calls == ["shutdown"]

    def test_foreign_server_is_neither_used_nor_stopped(self, conf, monkeypatch):
        monkeypatch.setattr(cli, "_get", lambda port, path, timeout=5: own_health(root="C:/other"))
        monkeypatch.setattr(cli, "_request_shutdown", lambda port: pytest.fail("must not stop foreign"))
        assert cli.ensure_server(conf) is False
        assert cli._FOREIGN_SERVER_ROOT == "C:/other"
        assert "port_conflict" in cli._read_notices()

    def test_no_root_and_unverifiable_is_foreign(self, conf, monkeypatch):
        h = own_health()
        del h["root"]
        monkeypatch.setattr(cli, "_get", lambda port, path, timeout=5: h)
        monkeypatch.setattr(cli, "_is_own_server_process", lambda pid: False)
        assert cli.ensure_server(conf) is False
        assert cli._FOREIGN_SERVER_ROOT.startswith("(unknown")

    def test_pid_file_number_never_kills(self, conf, monkeypatch):
        open(paths.pid_path(), "w").write("4242")
        monkeypatch.setattr(cli, "_get", lambda *a, **k: (_ for _ in ()).throw(OSError()))
        monkeypatch.setattr(cli, "_kill_process", lambda pid: pytest.fail("killed by pid file number"))
        monkeypatch.setattr(cli, "_is_own_server_process", lambda pid: False)
        cli.ensure_server(conf, max_wait=0)

    def test_dead_pid_file_does_not_wait(self, conf, monkeypatch):
        open(paths.pid_path(), "w").write("999999")
        monkeypatch.setattr(cli, "_get", lambda *a, **k: (_ for _ in ()).throw(OSError()))
        monkeypatch.setattr(cli, "_is_own_server_process", lambda pid: False)
        monkeypatch.setattr(cli, "_poll_health", lambda *a: pytest.fail("waited on a dead pid"))
        t0 = time.time()
        cli.ensure_server(conf)
        assert time.time() - t0 < 5 and not os.path.exists(paths.pid_path())


class FakeProc:
    def __init__(self, exit_code=None, pid=31337):
        self.pid, self.code, self.returncode = pid, exit_code, exit_code

    def poll(self):
        return self.code


class TestServerStart:
    def patch(self, monkeypatch, proc, healthy_after=None):
        seen = {}

        def popen(cmd, **kw):
            seen["cmd"], seen["kw"] = cmd, kw
            return proc

        monkeypatch.setattr(cli.subprocess, "Popen", popen)
        n = {"i": 0}

        def get(port, path, timeout=5):
            n["i"] += 1
            if healthy_after is not None and n["i"] > healthy_after:
                return own_health()
            raise OSError("down")

        monkeypatch.setattr(cli, "_get", get)
        monkeypatch.setattr(cli.time, "sleep", lambda s: None)
        return seen

    def test_command_flags_and_log(self, conf, monkeypatch):
        seen = self.patch(monkeypatch, FakeProc(), healthy_after=1)
        assert ORIG_START_SERVER(conf, 5) is True
        assert seen["cmd"][-3:] == ["-m", "n3mc_mcp.cli", "--run-server"] and seen["cmd"][0] == sys.executable
        kw = seen["kw"]
        assert kw["stdin"] is cli.subprocess.DEVNULL and kw["stdout"] is cli.subprocess.DEVNULL
        if os.name == "nt":
            assert kw["creationflags"] == cli.subprocess.CREATE_NEW_PROCESS_GROUP | cli.subprocess.CREATE_NO_WINDOW
        else:
            assert kw["start_new_session"] is True
        assert os.path.exists(paths.server_log_path())
        assert open(paths.pid_path()).read() == "31337"

    def test_early_exit_stops_waiting_and_marks(self, conf, monkeypatch):
        self.patch(monkeypatch, FakeProc(exit_code=3))
        t0 = time.time()
        assert ORIG_START_SERVER(conf, 60) is False
        assert time.time() - t0 < 5
        assert "server_start_failed" in cli._read_notices() and os.path.exists(paths.start_failed_path())
        assert not os.path.exists(paths.pid_path())

    def test_no_new_start_within_five_minutes(self, conf, monkeypatch):
        self.patch(monkeypatch, FakeProc(exit_code=3))
        ORIG_START_SERVER(conf, 60)
        called = []
        monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: called.append(1))
        assert ORIG_START_SERVER(conf, 60) is False and called == []
        old = time.time() - 600
        os.utime(paths.start_failed_path(), (old, old))
        self.patch(monkeypatch, FakeProc(), healthy_after=1)
        assert ORIG_START_SERVER(conf, 5) is True
        assert not os.path.exists(paths.start_failed_path()) and "server_start_failed" not in cli._read_notices()

    def test_still_starting_process_is_left_alone(self, conf, monkeypatch):
        open(paths.pid_path(), "w").write("4242")
        monkeypatch.setattr(cli, "_get", lambda *a, **k: (_ for _ in ()).throw(OSError()))
        monkeypatch.setattr(cli, "_is_own_server_process", lambda pid: True)
        monkeypatch.setattr(cli, "_listening_ports", lambda pid: [])
        monkeypatch.setattr(cli, "_poll_health", lambda cfg, s: None)
        monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: pytest.fail("second server started"))
        assert cli.ensure_server(conf) is False
        assert open(paths.pid_path()).read() == "4242"
        assert "still starting" in cli.unavailable_reason(conf)

    def test_server_on_another_port_is_shut_down(self, conf, monkeypatch):
        open(paths.pid_path(), "w").write("4242")
        stopped = []
        monkeypatch.setattr(cli, "_get", lambda port, path, timeout=5: own_health() if port == 18999 else (_ for _ in ()).throw(OSError()))
        monkeypatch.setattr(cli, "_is_own_server_process", lambda pid: True)
        monkeypatch.setattr(cli, "_listening_ports", lambda pid: [18999])
        monkeypatch.setattr(cli, "_request_shutdown", lambda port: stopped.append(port))
        monkeypatch.setattr(cli, "_wait_port_free", lambda port, s=15: True)
        cli.ensure_server(conf, max_wait=0)
        assert stopped == [18999]

    def test_empty_pid_file_counts_as_starting_for_a_minute(self, conf, monkeypatch):
        open(paths.pid_path(), "w").write("")
        monkeypatch.setattr(cli, "_get", lambda *a, **k: (_ for _ in ()).throw(OSError()))
        monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: pytest.fail("second server started"))
        assert cli.ensure_server(conf, max_wait=0) is False
        assert os.path.exists(paths.pid_path())


class TestNoProxy:
    def test_proxy_environment_is_ignored(self, conf, monkeypatch):
        seen = {"proxy": 0}

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"status": "ok"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        target = HTTPServer(("127.0.0.1", 0), H)
        proxy = socket.socket()
        proxy.bind(("127.0.0.1", 0))
        proxy.listen(5)
        proxy.settimeout(0.3)
        stop = threading.Event()

        def watch():
            while not stop.is_set():
                try:
                    proxy.accept()[0].close()
                    seen["proxy"] += 1
                except OSError:
                    pass

        threading.Thread(target=target.serve_forever, daemon=True).start()
        threading.Thread(target=watch, daemon=True).start()
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:%d" % proxy.getsockname()[1])
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:%d" % proxy.getsockname()[1])
        try:
            assert cli._request("GET", target.server_address[1], "/health", None, 5) == {"status": "ok"}
        finally:
            stop.set()
            target.shutdown()
            proxy.close()
        assert seen["proxy"] == 0


class Recorder:
    def __init__(self, monkeypatch):
        self.calls = []
        real = cli._post

        def post(port, path, body, timeout=30):
            self.calls.append((path, body))
            return real(port, path, body, timeout)

        monkeypatch.setattr(cli, "_post", post)


class TestCommands:
    def run(self, capsys, *argv):
        capsys.readouterr()
        rc = cli.main(list(argv))
        return rc, capsys.readouterr()

    def test_search_has_no_max_chars(self, wired, db, monkeypatch, capsys):
        rec = Recorder(monkeypatch)
        rc, out = self.run(capsys, "--search", "pineapple", "--since", "2026-01-01", "--until", "2026-12-31")
        assert rc == 0 and out.out.startswith("# Recalled Memory Context")
        path, body = [c for c in rec.calls if c[0] == "/search"][0]
        assert "max_chars" not in body and body["since"] == "2026-01-01" and body["until"] == "2026-12-31"

    def test_unreadable_since_exits_2(self, wired, db, capsys):
        rc, out = self.run(capsys, "--search", "x", "--since", "yesterday")
        assert rc == 2 and out.err.startswith("Error: since must be an ISO 8601")

    @pytest.mark.skipif(os.name != "nt", reason="these dates convert on other platforms")
    @pytest.mark.parametrize("flag,val", [("--until", "9999-12-31"), ("--since", "1969-12-31")])
    def test_out_of_range_date_exits_2(self, wired, db, capsys, flag, val):
        """L3 (2026-11-22): OSError from the conversion is an unreadable value, not a traceback."""
        rc, out = self.run(capsys, "--search", "x", flag, val)
        name = flag.lstrip("-")
        assert rc == 2 and out.err.startswith("Error: %s must be an ISO 8601" % name)
        assert len(out.err.strip().splitlines()) == 1

    def test_buffer_list_recall_delete_repair(self, wired, db, monkeypatch, capsys, svc):
        rec = Recorder(monkeypatch)
        rc, out = self.run(capsys, "--buffer", "remember this fact", "--turn-id", "T1")
        saved = json.loads(out.out)
        assert saved["status"] == "saved" and "Reminder" not in out.out
        assert rec.calls[-1][0] == "/tools/save_memory?nudge=0" and rec.calls[-1][1]["agent_name"] == "claude-code"
        rc, out = self.run(capsys, "--list")
        assert "remember this fact" in out.out and rec.calls[-1][0] == "/tools/list_memories?nudge=0"
        rc, out = self.run(capsys, "--recall-thread", "T1", "--before", "1", "--after", "3")
        assert rec.calls[-1][1] == {"turn_id": "T1", "before": 1, "after": 3} and "remember this fact" in out.out
        rc, out = self.run(capsys, "--delete", saved["id"])
        assert json.loads(out.out)["status"] == "deleted"
        rc, out = self.run(capsys, "--delete-session", "nope")
        assert json.loads(out.out)["status"] == "not_found"
        rc, out = self.run(capsys, "--repair")
        assert json.loads(out.out)["message"] == "index ensured"

    def test_buffer_from_stdin(self, wired, db, monkeypatch, capsys):
        set_stdin(monkeypatch, "text from stdin")
        rc, out = self.run(capsys, "--buffer", "-")
        assert json.loads(out.out)["saved"] is True

    def test_exactly_one_command(self, capsys):
        with pytest.raises(SystemExit) as e:
            cli.main(["--list", "--repair"])
        assert e.value.code == 2


class TestForeignPort:
    def test_search_list_delete_and_direct_buffer(self, cfg, redis_client, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_get", lambda port, path, timeout=5: own_health(root="C:/other"))
        capsys.readouterr()
        cli.main(["--search", "x"])
        assert "_(memory search unavailable: another N3MC folder's server is using port" in capsys.readouterr().out
        cli.main(["--list"])
        assert capsys.readouterr().out.startswith("Error: memory backend unreachable.")
        cli.main(["--delete", "abc"])
        d = json.loads(capsys.readouterr().out)
        assert d["status"] == "error" and "another N3MC folder's server" in d["reason"]
        cli.main(["--buffer", "kept locally"])
        r = json.loads(capsys.readouterr().out)
        assert r["saved"] is True and r["warning"] == "embedding_unavailable" and "gets its vector from repair_memory" in r["detail"]
        assert redis_client.hget(b"mem:" + r["id"].encode(), b"embedding") is None


class TestStop:
    def run_stop(self, monkeypatch, capsys, cwd):
        set_stdin(monkeypatch, json.dumps({"cwd": cwd}))
        capsys.readouterr()
        assert cli.cmd_stop() == 0
        return capsys.readouterr().out

    def test_writes_rules_file_and_prints_nothing(self, isolate, monkeypatch, capsys, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        assert self.run_stop(monkeypatch, capsys, str(proj)) == ""
        text = (proj / ".claude" / "rules" / "n3mc-lite-behavior.md").read_text(encoding="utf-8")
        assert text.startswith("<!-- n3mc-lite-behavior v1 -->\n# N3MemoryCore Lite Behavioral Guidelines")
        assert "-m n3mc_mcp.cli --search" in text and "{cli}" not in text and text.endswith("kept only in `audit.log`.\n")

    def test_project_dir_is_used(self, isolate, monkeypatch, capsys, tmp_path):
        """F3 (2026-11-22): CLAUDE_PROJECT_DIR wins over the hook's cwd (a subfolder after a cd)."""
        proj, sub = tmp_path / "proj", tmp_path / "proj" / "sub"
        sub.mkdir(parents=True)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", proj.as_posix())
        self.run_stop(monkeypatch, capsys, str(sub))
        assert (proj / ".claude" / "rules" / "n3mc-lite-behavior.md").exists()
        assert not (sub / ".claude").exists()

    def test_without_project_dir_hook_cwd_is_used(self, isolate, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "gone"))  # not an existing folder
        proj = tmp_path / "proj"
        proj.mkdir()
        self.run_stop(monkeypatch, capsys, str(proj))
        assert (proj / ".claude" / "rules" / "n3mc-lite-behavior.md").exists()

    def test_project_dir_home_creates_nothing(self, isolate, monkeypatch, capsys, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", pathlib_posix(isolate["home"]))
        self.run_stop(monkeypatch, capsys, str(work))
        assert not os.path.exists(os.path.join(isolate["home"], ".claude", "rules"))
        assert not (work / ".claude").exists()

    def test_home_folder_is_skipped(self, isolate, monkeypatch, capsys):
        self.run_stop(monkeypatch, capsys, isolate["home"])
        assert not os.path.exists(os.path.join(isolate["home"], ".claude", "rules"))

    def test_old_turn_files_deleted_and_audit_rotated(self, isolate, monkeypatch, capsys, tmp_path):
        old = os.path.join(paths.data_dir(), "turn_id_old.txt")
        fresh = os.path.join(paths.data_dir(), "turn_id_new.txt")
        open(old, "w").write("a")
        open(fresh, "w").write("b")
        t = time.time() - 31 * 86400
        os.utime(old, (t, t))
        open(paths.audit_log_path(), "w").write("x" * 100)
        monkeypatch.setattr(cli, "_AUDIT_MAX_BYTES", 50)
        proj = tmp_path / "p2"
        proj.mkdir()
        self.run_stop(monkeypatch, capsys, str(proj))
        assert not os.path.exists(old) and os.path.exists(fresh)
        assert os.path.exists(paths.audit_log_path() + ".1") and not os.path.exists(paths.audit_log_path())

    def test_does_not_stop_the_server(self, isolate, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr(cli, "_request_shutdown", lambda port: pytest.fail("--stop must not stop the server"))
        proj = tmp_path / "p3"
        proj.mkdir()
        self.run_stop(monkeypatch, capsys, str(proj))

    def test_stop_server_command_only_stops_own(self, isolate, monkeypatch, capsys, conf):
        monkeypatch.setattr(cli, "_get", lambda port, path, timeout=5: own_health(root="C:/other"))
        monkeypatch.setattr(cli, "_request_shutdown", lambda port: pytest.fail("foreign server stopped"))
        cli.main(["--stop-server"])
        assert capsys.readouterr().out.strip() == "No N3MC Lite server of this data folder is running."
        stopped = []
        monkeypatch.setattr(cli, "_get", lambda port, path, timeout=5: own_health())
        monkeypatch.setattr(cli, "_request_shutdown", lambda port: stopped.append(1))
        monkeypatch.setattr(cli, "_wait_port_free", lambda port, s=15: True)
        cli.main(["--stop-server"])
        assert capsys.readouterr().out.strip() == "Stopped the N3MC Lite server (pid 4242)." and stopped == [1]


class TestReview20261122:
    def test_l1_server_writes_its_own_pid(self, isolate):
        with open(paths.pid_path(), "w", encoding="utf-8") as f:
            f.write("37620")  # the launcher's number that the starter wrote
        cli._write_own_pid()
        assert open(paths.pid_path(), encoding="utf-8").read() == str(os.getpid())
        os.unlink(paths.pid_path())
        cli._write_own_pid()  # no file: created
        assert open(paths.pid_path(), encoding="utf-8").read() == str(os.getpid())

    def test_l1_l4_run_server_writes_pid_and_passes_early_version(self, isolate, monkeypatch):
        from n3mc_mcp import app as app_mod, processor, tools
        order, seen = [], {}
        monkeypatch.setattr(config, "code_version", lambda *a, **k: order.append("version") or "v-first")

        class FakeDb:
            def ensure_connected(self):
                order.append("redis")

        monkeypatch.setattr(tools, "Service", lambda cfg: types.SimpleNamespace(db=FakeDb(), cfg=cfg))
        monkeypatch.setattr(processor, "import_backend", lambda: order.append("model"))
        monkeypatch.setattr(app_mod, "create_app", lambda *a, **k: seen.update(k) or "app")
        monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace(run=lambda *a, **k: order.append("listen")))
        with open(paths.pid_path(), "w", encoding="utf-8") as f:
            f.write("37620")
        writes = []
        real = cli._write_own_pid
        monkeypatch.setattr(cli, "_write_own_pid", lambda: (writes.append(1), real())[1])
        cli.run_server()
        assert order.index("version") < order.index("redis") < order.index("model") < order.index("listen")
        assert seen["code_version"] == "v-first" and len(writes) == 2

    def test_l1_moved_port_server_gets_shutdown(self, conf, monkeypatch):
        other = conf["server_port"] + 1
        sent = []
        monkeypatch.setattr(cli, "_listening_ports", lambda pid: [conf["server_port"], other])
        monkeypatch.setattr(cli, "_health", lambda port: {"status": "ok", "root": cli._own_root()})
        monkeypatch.setattr(cli, "_request_shutdown", lambda port: sent.append(port))
        monkeypatch.setattr(cli, "_wait_port_free", lambda port, t: True)
        assert cli._handle_other_port(conf, 32424) is True and sent == [other]

    def test_l2_matching_redis_clears_mismatch(self, conf):
        from n3mc_mcp.database import redis_display
        cli._after_ok(conf, {"redis": "otherhost:6379/0"})
        assert "redis_url_mismatch" in cli._read_notices()
        cli._after_ok(conf, {"redis": redis_display(conf["redis_url"])})
        assert "redis_url_mismatch" not in cli._read_notices()

    def test_l3_conversion_error_is_unreadable(self, monkeypatch):
        from n3mc_mcp import tools

        class Boom:
            @staticmethod
            def fromisoformat(v):
                raise OSError(22, "Invalid argument")

        monkeypatch.setattr(tools, "datetime", Boom)
        with pytest.raises(tools.TimeRangeError):
            tools.parse_time("2026-11-13", False, "since")


_LOAD_IDS = ("import json, sys; from n3mc_mcp import config; c = config.load_config(); "
             "print(json.dumps([c['owner_id'], c['local_id']]))")


class TestConfigCreate:
    """2026-11-22, review #2: filling a missing owner_id / local_id of an existing config.json under a lock."""

    def test_processes_on_existing_file_without_ids_end_with_the_ids_on_disk(self, tmp_path):
        import subprocess
        folder = tmp_path / "shared"
        folder.mkdir()
        p = folder / "config.json"
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for _ in range(3):
            p.write_text(json.dumps({"half_life_days": 5, "my_own_key": "kept"}), encoding="utf-8")
            env = dict(os.environ, N3MC_DATA_DIR=str(folder), PYTHONPATH=root)
            env.pop("N3MC_REDIS_URL", None)
            procs = [subprocess.Popen([sys.executable, "-c", _LOAD_IDS], env=env, cwd=root,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(4)]
            res = [pr.communicate(timeout=60) for pr in procs]
            got = [json.loads(o.decode().strip().splitlines()[-1]) for o, _ in res]
            disk = json.load(open(p, encoding="utf-8"))
            assert all(g == [disk["owner_id"], disk["local_id"]] for g in got), (got, disk, [e for _, e in res])
            assert disk["half_life_days"] == 5 and disk["my_own_key"] == "kept"
            assert not os.path.exists(str(p) + ".lock")

    def test_ids_are_read_again_under_the_lock(self, isolate, monkeypatch):
        """Another process filled the IDs between our first read and our lock: we use (and keep) its IDs."""
        path = paths.config_path()
        real = config._acquire_lock

        def other_process_first(lock):
            c = json.load(open(path, encoding="utf-8"))
            c.update({"owner_id": "owner-of-the-winner", "local_id": "local-of-the-winner"})
            open(path, "w", encoding="utf-8").write(json.dumps(c))
            return real(lock)

        monkeypatch.setattr(config, "_acquire_lock", other_process_first)
        cfg = config.load_config()
        assert cfg["owner_id"] == "owner-of-the-winner" and cfg["local_id"] == "local-of-the-winner"
        disk = json.load(open(path, encoding="utf-8"))
        assert disk["owner_id"] == "owner-of-the-winner" and disk["local_id"] == "local-of-the-winner"
        assert not os.path.exists(path + ".lock")

    def test_lock_waits_and_stale_lock_is_removed(self, isolate, monkeypatch):
        path = paths.config_path()
        lock = path + ".lock"
        open(lock, "w").close()
        threading.Timer(0.3, lambda: os.unlink(lock)).start()
        t0 = time.time()
        cfg = config.load_config()
        assert time.time() - t0 >= 0.25 and cfg["owner_id"] == json.load(open(path, encoding="utf-8"))["owner_id"]
        assert not os.path.exists(lock)
        c = json.load(open(path, encoding="utf-8"))
        del c["owner_id"]
        open(path, "w", encoding="utf-8").write(json.dumps(c))
        open(lock, "w").close()
        old = time.time() - 60
        os.utime(lock, (old, old))                  # a lock left by a process that died
        t0 = time.time()
        cfg = config.load_config()
        assert time.time() - t0 < 5 and not os.path.exists(lock)
        assert cfg["owner_id"] == json.load(open(path, encoding="utf-8"))["owner_id"]

    def test_no_lock_when_ids_are_present(self, isolate, monkeypatch):
        config.load_config()                        # fills the IDs
        monkeypatch.setattr(config, "_acquire_lock", lambda lock: pytest.fail("lock taken although IDs exist"))
        config.load_config()

    def test_records_of_both_are_listed_after_restart(self, isolate, redis_client, fake_embed):
        from n3mc_mcp.database import Database
        out, errors = [], []

        def work(i):
            try:
                c = config.load_config()
                d = Database(c)
                d.ensure_connected()
                out.append((c["owner_id"], d.save_memory("record from starter %d" % i, embed=False)["status"]))
                d.close()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        ts = [threading.Thread(target=work, args=(i,)) for i in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert not errors and [s for _, s in out] == ["saved", "saved"]
        assert out[0][0] == out[1][0]
        c = config.load_config()                    # the restart
        d = Database(c)
        d.ensure_connected()
        listed = [e["content"] for e in d.list_entries(20)]
        d.close()
        assert "record from starter 0" in listed and "record from starter 1" in listed


class TestReview20261122b:
    # ---- review #1: redis_display never shows any part of an unparseable URL
    def test_redis_display(self):
        from n3mc_mcp.database import redis_display
        assert redis_display("redis://user:secret@example.org:6390/2") == "example.org:6390/2"
        assert redis_display("redis://u:secret@host:abc/0") == "<unparseable redis_url>"

    def test_health_has_no_password(self, client, svc):
        svc.cfg["redis_url"] = "redis://u:secret@host:abc/0"
        h = client.get("/health").json()
        assert h["redis"] == "<unparseable redis_url>" and "secret" not in json.dumps(h)

    def test_mismatch_notice_has_no_password(self, conf, capsys):
        conf = dict(conf, redis_url="redis://u:secret@host:abc/0")
        cli._after_ok(conf, own_health(redis="127.0.0.1:6390/0"))
        err = capsys.readouterr().err
        assert "<unparseable redis_url>" in err and "secret" not in err
        for p in (paths.audit_log_path(), paths.notices_path()):
            text = open(p, encoding="utf-8").read()
            assert "<unparseable redis_url>" in text and "secret" not in text

    # ---- review #9: the server hashes the config.json content load_config() used
    def test_code_version_matches_client_after_fill_in(self, isolate):
        assert "owner_id" not in json.load(open(paths.config_path(), encoding="utf-8"))
        cfg = config.load_config()                  # fills owner_id / local_id and writes the file
        assert config.code_version(cfg["_config_bytes"]) == config.code_version()
        cfg = config.load_config()                  # nothing changed
        assert config.code_version(cfg["_config_bytes"]) == config.code_version()

    def test_code_version_of_missing_and_unreadable_file(self, isolate):
        os.unlink(paths.config_path())
        assert config.code_version(None) == config.code_version()
        open(paths.config_path(), "w", encoding="utf-8").write("{not json")
        cfg = config.load_config()
        assert cfg["_unreadable"] and config.code_version(cfg["_config_bytes"]) == config.code_version()

    def test_change_between_load_and_start_is_stale_then_ok(self, isolate, monkeypatch):
        from n3mc_mcp import app as app_mod, processor, tools
        seen = {}
        real_load = config.load_config

        def load_then_edit():
            cfg = real_load()
            c = json.load(open(paths.config_path(), encoding="utf-8"))
            c["half_life_days"] = 7                 # the user edits config.json while the server starts
            open(paths.config_path(), "w", encoding="utf-8").write(json.dumps(c))
            return cfg

        class FakeDb:
            def ensure_connected(self):
                pass

        monkeypatch.setattr(config, "load_config", load_then_edit)
        monkeypatch.setattr(tools, "Service", lambda cfg: types.SimpleNamespace(db=FakeDb(), cfg=cfg))
        monkeypatch.setattr(processor, "import_backend", lambda: None)
        monkeypatch.setattr(app_mod, "create_app", lambda *a, **k: seen.update(k) or "app")
        monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace(run=lambda *a, **k: None))
        cli.run_server()
        first = seen["code_version"]
        assert cli._health_decision(own_health(code_version=first)) == "stale"
        monkeypatch.setattr(config, "load_config", real_load)
        cli.run_server()                            # the restart
        assert cli._health_decision(own_health(code_version=seen["code_version"])) == "ok"

    # ---- review #8: --stop-server reports success only when the port became free
    def test_stop_server_stopped(self, conf, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_get", lambda port, path, timeout=5: own_health())
        monkeypatch.setattr(cli, "_request_shutdown", lambda port: {"status": "stopping"})
        monkeypatch.setattr(cli, "_wait_port_free", lambda port, s=15: True)
        assert cli.main(["--stop-server"]) == 0
        assert capsys.readouterr().out.strip() == "Stopped the N3MC Lite server (pid 4242)."

    def test_stop_server_none_running_or_foreign(self, conf, monkeypatch, capsys):
        assert cli.main(["--stop-server"]) == 0     # nothing answers
        assert capsys.readouterr().out.strip() == "No N3MC Lite server of this data folder is running."
        monkeypatch.setattr(cli, "_get", lambda port, path, timeout=5: own_health(root="C:/other"))
        monkeypatch.setattr(cli, "_request_shutdown", lambda port: pytest.fail("foreign server stopped"))
        assert cli.main(["--stop-server"]) == 0
        assert capsys.readouterr().out.strip() == "No N3MC Lite server of this data folder is running."

    @pytest.mark.parametrize("fail_request", [True, False])
    def test_stop_server_failed(self, conf, monkeypatch, capsys, fail_request):
        monkeypatch.setattr(cli, "_get", lambda port, path, timeout=5: own_health())
        if fail_request:
            monkeypatch.setattr(cli, "_request_shutdown",
                                lambda port: (_ for _ in ()).throw(cli.HttpStatusError(403, "forbidden")))
        else:
            monkeypatch.setattr(cli, "_request_shutdown", lambda port: {"status": "stopping"})
        monkeypatch.setattr(cli, "_wait_port_free", lambda port, s=15: False)
        assert cli.main(["--stop-server"]) == 1
        out = capsys.readouterr()
        assert "Stopped" not in out.out
        assert out.err.startswith("Could not stop the N3MC Lite server (pid 4242): ")
        assert out.err.strip().endswith(". End the process whose command line contains n3mc_mcp.cli and --run-server.")
        assert ("shutdown request failed" in out.err) == fail_request

    def test_stop_server_request_failed_but_port_freed(self, conf, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_get", lambda port, path, timeout=5: own_health())
        monkeypatch.setattr(cli, "_request_shutdown", lambda port: (_ for _ in ()).throw(ConnectionResetError("gone")))
        monkeypatch.setattr(cli, "_wait_port_free", lambda port, s=15: True)
        assert cli.main(["--stop-server"]) == 0
        assert capsys.readouterr().out.strip() == "Stopped the N3MC Lite server (pid 4242)."
