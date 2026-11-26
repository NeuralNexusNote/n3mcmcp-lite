"""Layer 8: the MCP thin client (spec §4.6). No model. The handlers are called as functions with a
fake HTTP sender; TestThinSubprocess talks to a real entry and a real server on the test port."""
import asyncio
import contextlib
import json
import os
import subprocess
import sys
import threading
import time

import pytest

import conftest
from n3mc_mcp import __version__, cli, config, paths
from n3mc_mcp import server as thin
from n3mc_mcp.instructions import INSTRUCTIONS
from n3mc_mcp.tooldefs import TOOLS
from n3mc_mcp.tools import _NUDGE_AFTER_SAVE, _NUDGE_GENERIC

import mcp.types as types


def _spec_instructions():
    with open(os.path.join(conftest.REPO_ROOT, "N3MemoryCore_MCP_Spec_EN.md"), encoding="utf-8") as f:
        text = f.read()
    start = text.index("N3MemoryCore — Lite (Ephemeral Memory)\n")
    end = text.index("\n`````", start)
    return text[start:end] + "\n"


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    monkeypatch.setattr(thin, "_state", {"cfg": None, "client_name": None})


class TestThinNames:
    def test_scripts(self):
        import tomllib
        with open(os.path.join(conftest.REPO_ROOT, "pyproject.toml"), "rb") as f:
            p = tomllib.load(f)
        assert p["project"]["scripts"]["n3mc-workingmemory"] == "n3mc_mcp.__main__:run"
        assert p["project"]["scripts"]["n3mc-mcp-lite"] == "n3mc_mcp.__main__:run"
        assert p["project"]["version"] == "2.0.2" == __version__
        assert "mcp>=1.0.0,<2.0.0" in p["project"]["dependencies"]

    def test_server_info(self):
        s = thin.build_server()
        assert s.name == "n3mc-workingmemory" and s.version == __version__


class TestThinTools:
    def test_list_without_server(self):
        s = thin.build_server()
        res = asyncio.run(s.request_handlers[types.ListToolsRequest](types.ListToolsRequest(method="tools/list")))
        assert [t.model_dump() for t in res.root.tools] == [t.model_dump() for t in TOOLS]

    def test_instructions_verbatim(self):
        assert INSTRUCTIONS == _spec_instructions()
        assert thin.build_server().instructions == INSTRUCTIONS

    def test_validate_input_off(self):
        assert thin.build_server()._n3mc_validate_input is False


def _call(server, name, args):
    req = types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(name=name, arguments=args))
    return asyncio.run(server.request_handlers[types.CallToolRequest](req)).root


class TestForwarding:
    @pytest.fixture
    def sent(self, monkeypatch):
        calls = []
        monkeypatch.setattr(cli, "ensure_server", lambda c, max_wait=60: True)
        monkeypatch.setattr(cli, "_post", lambda c, path, body, timeout=30, port=None:
                            (calls.append((path, body)), (200, {"text": f"exact text ✓ {path}"}))[1])
        return calls

    @pytest.mark.parametrize("tool", [t.name for t in TOOLS])
    def test_forward_verbatim(self, sent, tool):
        res = _call(thin.build_server(), tool, {"x": 1})
        assert sent[-1][0] == f"/tools/{tool}?nudge=1"
        assert res.content[0].text == f"exact text ✓ /tools/{tool}?nudge=1" and res.isError is False

    def test_session_filled(self, sent, monkeypatch):
        thin.forward("search_memory", thin.prepare_args("search_memory", {"query": "q"}))
        thin.forward("save_memory", thin.prepare_args("save_memory", {"content": "c", "session_id": "mine"}))
        assert sent[0][1]["session_id"] == thin.SESSION and sent[1][1]["session_id"] == "mine"
        assert "session_id" not in thin.prepare_args("list_memories", {})

    def test_agent_name(self, sent, monkeypatch):
        assert "agent_name" not in thin.prepare_args("save_memory", {"content": "c"})
        thin._state["client_name"] = "client-x"
        assert thin.prepare_args("save_memory", {"content": "c"})["agent_name"] == "client-x"
        monkeypatch.setenv("N3MC_AGENT_NAME", " env-agent ")
        assert thin.prepare_args("save_memory", {"content": "c"})["agent_name"] == "env-agent"
        assert thin.prepare_args("save_memory", {"content": "c", "agent_name": "arg"})["agent_name"] == "arg"

    def test_blank_agent_name_is_never_spaces(self, sent, monkeypatch):
        # §4.6 6.3: blank and no other source -> empty, never the spaces themselves
        monkeypatch.delenv("N3MC_AGENT_NAME", raising=False)
        assert thin.prepare_args("save_memory", {"content": "c", "agent_name": "  "})["agent_name"] == ""
        thin._state["client_name"] = "   "
        assert thin.prepare_args("save_memory", {"content": "c", "agent_name": " 	"})["agent_name"] == ""
        thin._state["client_name"] = "client-y"
        assert thin.prepare_args("save_memory", {"content": "c", "agent_name": "  "})["agent_name"] == "client-y"

    def test_exception_text(self, monkeypatch):
        # §4.6 6.6: "Error: <message>" + the reminder, as in 1.x (no exception type name)
        def boom(name, args):
            raise RuntimeError("kaput")
        monkeypatch.setattr(thin, "forward", boom)
        res = _call(thin.build_server(), "search_memory", {"query": "q"})
        assert res.content[0].text == "Error: kaput" + _NUDGE_GENERIC and res.isError is False

    def test_nan_reaches_server(self, sent):
        _call(thin.build_server(), "save_memory", {"content": "x", "importance": "nan"})
        assert sent[-1][1]["importance"] == "nan"

    def test_unknown_local(self, sent):
        res = _call(thin.build_server(), "nope", {})
        assert res.content[0].text == "Error: unknown tool 'nope'" + _NUDGE_GENERIC and not sent


class TestThinNoServer:
    def test_save_direct(self, rconn):
        text = thin.forward("save_memory", thin.prepare_args("save_memory", {"content": "direct thin save"}))
        res = json.JSONDecoder().raw_decode(text)[0]
        assert res["saved"] and res["warning"] == "embedding_unavailable"
        assert res["detail"].startswith("The N3MC Lite memory server was not available (")
        assert res["detail"].endswith("), so this entry was saved without a vector. It is found by keywords now "
                                      "and gets its vector from repair_memory once the server runs.")
        assert text.endswith(_NUDGE_AFTER_SAVE)
        assert b"embedding" not in rconn.hgetall("mem:" + res["id"])

    @pytest.mark.parametrize("tool", ["search_memory", "list_memories", "recall_thread"])
    def test_errors(self, tool):
        text = thin.forward(tool, {})
        assert text.startswith("Error: memory backend unreachable. The N3MC Lite memory server is not available (")
        assert text.endswith(_NUDGE_GENERIC)

    def test_repair(self):
        out = json.JSONDecoder().raw_decode(thin.forward("repair_memory", {}))[0]
        assert out["status"] == "error"
        assert out["message"].startswith("The N3MC Lite memory server is not available (")
        assert out["message"].endswith("). Retry in a minute.")

    def test_redis_down_too(self, isolate):
        conftest.write_test_config(str(isolate["data"]), redis_url=conftest.DEAD_URL)
        text = thin.forward("save_memory", {"content": "nowhere"})
        res = json.JSONDecoder().raw_decode(text)[0]
        assert res == {"status": "error", "saved": False, "reason": res["reason"]} and "Redis Stack" in res["reason"]
        assert "docker start redis-stack" in res["reason"]
        assert text.endswith(_NUDGE_GENERIC)

    def test_delete_states(self, monkeypatch, capsys):
        out = json.JSONDecoder().raw_decode(thin.forward("delete_memory", {"id": "x"}))[0]
        assert "Nothing was deleted" in out["reason"]
        monkeypatch.setattr(cli, "ensure_server", lambda c, max_wait=60: True)
        monkeypatch.setattr(cli, "_post", lambda *a, **k: (200, {"text": '{"status": "deleted"}'}))
        assert thin.forward("delete_memory", {"id": "x"}) == '{"status": "deleted"}'

        def timeout(*a, **k):
            raise TimeoutError("timed out")
        monkeypatch.setattr(cli, "_post", timeout)
        out = json.JSONDecoder().raw_decode(thin.forward("delete_memories_by_session", {"session_id": "s"}))[0]
        assert out["status"] == "unknown"
        cli.main(["--delete-session", "s"])
        assert json.loads(capsys.readouterr().out)["status"] == "unknown"


class TestRedisDisplayAndCodeVersion:
    def test_display(self):
        assert config.redis_display("redis://user:pw@h:6390/2") == "h:6390/2"
        assert config.redis_display("redis://u:secret@host:abc/0") == "<unparseable redis_url>"

    def test_no_secret_leak(self, isolate, capsys):
        bad = "redis://u:pw9Zq7@host:abc/0"
        conftest.write_test_config(str(isolate["data"]), redis_url=bad)
        cfg = config.load_config()
        from n3mc_mcp import app as app_mod
        from fastapi.testclient import TestClient
        from n3mc_mcp.database import Database
        a = app_mod.create_app(cfg, Database(cfg), code_version="x", load_model=False)
        h = TestClient(a).get("/health", headers={"Host": f"127.0.0.1:{conftest.TEST_PORT}"}).json()
        assert "pw9Zq7" not in json.dumps(h)
        cli._server_ok(dict(cfg, redis_url="redis://other:1/0"), h)
        for p in (paths.notices_path(), paths.audit_log_path()):
            assert "pw9Zq7" not in open(p, encoding="utf-8").read()
        assert "pw9Zq7" not in capsys.readouterr().err

    def test_code_version_matches_after_fill(self, isolate):
        (isolate["data"] / "config.json").write_text(json.dumps({"owner_id": "o", "local_id": "l"}), encoding="utf-8")
        server_cfg = config.load_config()
        client_cfg = config.load_config()
        assert cli.own_code_version(server_cfg) == cli.own_code_version(client_cfg)

    def test_changed_between_load_and_start(self, isolate):
        server_cfg = config.load_config()
        conftest.write_test_config(str(isolate["data"]), min_score=0.25)
        h = {"root": paths.data_dir(), "code_version": cli.own_code_version(server_cfg)}
        assert cli._health_decision(h, config.load_config()) == "stale"
        h["code_version"] = cli.own_code_version(config.load_config())
        assert cli._health_decision(h, config.load_config()) == "ok"

    def test_stop_server(self, monkeypatch, capsys, cfg):
        monkeypatch.setattr(cli, "_health", lambda c, port=None: None)
        assert cli.main(["--stop-server"]) == 0 and "No N3MC Lite server" in capsys.readouterr().out
        monkeypatch.setattr(cli, "_health", lambda c, port=None: {"root": "/other", "pid": 1})
        assert cli.main(["--stop-server"]) == 0 and "No N3MC Lite server" in capsys.readouterr().out
        monkeypatch.setattr(cli, "_health", lambda c, port=None: {"root": paths.data_dir(), "pid": 77})
        monkeypatch.setattr(cli, "_request_shutdown", lambda *a, **k: True)
        monkeypatch.setattr(cli, "_wait_port_free", lambda p, total=15: True)
        assert cli.main(["--stop-server"]) == 0 and "Stopped the N3MC Lite server (pid 77)." in capsys.readouterr().out
        monkeypatch.setattr(cli, "_wait_port_free", lambda p, total=15: False)
        err = (cli.main(["--stop-server"]), capsys.readouterr().err)
        assert err[0] == 1 and "Could not stop the N3MC Lite server (pid 77): the port was still in use" in err[1]
        # §13: the request failed but the server went down anyway -> a stop
        monkeypatch.setattr(cli, "_request_shutdown", lambda *a, **k: False)
        monkeypatch.setattr(cli, "_wait_port_free", lambda p, total=15: True)
        assert cli.main(["--stop-server"]) == 0 and "Stopped the N3MC Lite server (pid 77)." in capsys.readouterr().out
        # the request failed and the port is still taken -> not a stop
        monkeypatch.setattr(cli, "_wait_port_free", lambda p, total=15: False)
        err = (cli.main(["--stop-server"]), capsys.readouterr().err)
        assert err[0] == 1 and "Could not stop the N3MC Lite server (pid 77): the stop request failed." in err[1]


class TestThinStart:
    def test_background_and_per_call(self, monkeypatch):
        started = threading.Event()
        release = threading.Event()
        calls = []

        def slow(cfg, max_wait=60):
            calls.append(1)
            started.set()
            release.wait(5)
            return False
        monkeypatch.setattr(cli, "ensure_server", slow)

        @contextlib.asynccontextmanager
        async def fake_stdio():
            yield (None, None)

        async def fake_run(self, r, w, opts):
            assert started.wait(2)

        monkeypatch.setattr(thin, "stdio_server", fake_stdio)
        monkeypatch.setattr(thin.Server, "run", fake_run)
        t0 = time.time()
        asyncio.run(thin._main())
        assert time.time() - t0 < 2 and calls == [1]
        release.set()
        thin.forward("list_memories", {})
        assert len(calls) == 2

    def test_no_shutdown(self):
        src = open(thin.__file__, encoding="utf-8").read()
        assert "/shutdown" not in src and "_request_shutdown" not in src


class TestThinNoModel:
    def test_imports(self):
        code = ("import sys, n3mc_mcp.server as s; s.build_server(); "
                "print('sentence_transformers' in sys.modules, 'torch' in sys.modules)")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                             env=dict(os.environ, PYTHONPATH=conftest.REPO_ROOT))
        assert out.stdout.strip() == "False False"

    def test_stdout_silent(self):
        with open(paths.start_failed_path(), "w") as f:   # a real process: keep it from starting a server
            f.write("test")
        p = subprocess.run([sys.executable, "-m", "n3mc_mcp"], input=b"", capture_output=True, timeout=120,
                           cwd=conftest.REPO_ROOT)
        assert p.stdout == b""

    def test_stdout_only_mcp_answers(self):
        # initialize + tools/list: stdout holds exactly the two JSON-RPC answers and nothing else
        with open(paths.start_failed_path(), "w") as f:
            f.write("test")
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
        p = subprocess.Popen([sys.executable, "-m", "n3mc_mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, cwd=conftest.REPO_ROOT)
        try:
            p.stdin.write((json.dumps(init) + "\n").encode())
            p.stdin.flush()
            first = json.loads(p.stdout.readline())
            p.stdin.write((json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode())
            p.stdin.write((json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n").encode())
            p.stdin.flush()
            second = json.loads(p.stdout.readline())
            p.stdin.close()
            rest = p.stdout.read()
            p.wait(timeout=60)
        finally:
            if p.poll() is None:
                p.kill()
        assert first["id"] == 1 and first["result"]["serverInfo"] == {"name": "n3mc-workingmemory", "version": __version__}
        assert first["result"]["instructions"] == INSTRUCTIONS
        assert second["id"] == 2 and len(second["result"]["tools"]) == 7
        assert rest.strip() == b""


class TestThinSubprocess:
    @pytest.mark.slow
    def test_roundtrip(self, rconn, isolate):
        cache = os.path.join(conftest.REAL_HF_HOME, "hub", "models--intfloat--multilingual-e5-base")
        if not os.path.isdir(cache):
            pytest.skip("the embedding model is not in the Hugging Face cache")
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        env = dict(os.environ, N3MC_DATA_DIR=str(isolate["data"]), HF_HOME=conftest.REAL_HF_HOME,
                   N3MC_AGENT_NAME="test-client", PYTHONPATH=conftest.REPO_ROOT)
        params = StdioServerParameters(command=sys.executable, args=["-m", "n3mc_mcp"], env=env)
        cfg = config.load_config()
        timings = {}

        async def go():
            async with stdio_client(params) as (r, w):
                async with ClientSession(r, w) as s:
                    t0 = time.time()
                    await s.initialize()
                    timings["initialize"] = time.time() - t0
                    tools = await s.list_tools()
                    assert len(tools.tools) == 7
                    deadline = time.time() + 180
                    while True:
                        res = await s.call_tool("save_memory", {"content": "subprocess roundtrip fact 42"})
                        data = json.JSONDecoder().raw_decode(res.content[0].text)[0]
                        if data.get("saved") or data.get("status") == "duplicate":
                            break
                        assert time.time() < deadline, data
                        await asyncio.sleep(2)
                    while True:
                        res = await s.call_tool("search_memory", {"query": "subprocess roundtrip fact"})
                        if "subprocess roundtrip fact 42" in res.content[0].text:
                            break
                        assert time.time() < deadline, res.content[0].text
                        await asyncio.sleep(2)
        try:
            asyncio.run(go())
            assert timings["initialize"] < 2.0, timings
            # The client may or may not have ended the server with its job object (§3.15): either passes.
            try:
                status, _ = cli._http(cfg, "GET", "/health")
            except OSError:
                status = None
            if status != 200:
                p = subprocess.run([sys.executable, "-m", "n3mc_mcp.cli", "--list"], env=env, timeout=240,
                                   capture_output=True, text=True, encoding="utf-8")
                assert "subprocess roundtrip fact 42" in p.stdout
        finally:
            _stop_real_server(cfg)


def _stop_real_server(cfg):
    try:
        status, h = cli._http(cfg, "GET", "/health")
        if status == 200:
            cli._http(cfg, "POST", "/shutdown", {"root": paths.data_dir()})
            cli._wait_port_free(cfg["server_port"], 15)
    except Exception:  # noqa: BLE001
        pass
    pid, _ = cli._read_pid_file()
    if pid:
        try:
            import psutil
            p = psutil.Process(pid)
            if "--run-server" in p.cmdline():
                p.kill()
        except Exception:  # noqa: BLE001
            pass
