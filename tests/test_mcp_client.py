"""Layer 8: the MCP thin client (spec §4.6). No model: the handlers are called as functions (or through
an in-memory MCP session) with the HTTP sender faked; one subprocess test uses the real server."""
import asyncio
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

try:
    import tomllib
except ImportError:  # Python 3.10
    tomllib = None

import conftest
from conftest import parse_json
from n3mc_mcp import __version__, cli, config, paths, server, tools
from n3mc_mcp.config import compute_code_version, load_config, read_config_raw, redis_display
from n3mc_mcp.instructions import INSTRUCTIONS
from n3mc_mcp.tooldefs import TOOLS

ROOT = Path(__file__).resolve().parent.parent
SPEC = (ROOT / "N3MemoryCore_MCP_Spec_EN.md").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    monkeypatch.setattr(server, "STATE", server._State())


def capture_post(monkeypatch, answer=None, exc=None):
    sent = []

    def fake_post(port, path, body, timeout=30):
        sent.append((port, path, body))
        if exc is not None:
            raise exc
        return 200, {"text": answer if answer is not None else f"answer from {path}"}

    monkeypatch.setattr(cli, "_post", fake_post)
    monkeypatch.setattr(cli, "ensure_server", lambda cfg, max_wait=60: True)
    return sent


def run_session(coro_fn, client_name="pytest-client"):
    from mcp.shared.memory import create_connected_server_and_client_session
    from mcp.types import Implementation

    async def main():
        async with create_connected_server_and_client_session(
                server.build_server(), client_info=Implementation(name=client_name, version="1")) as s:
            return await coro_fn(s)

    return asyncio.run(main())


class TestThinNames:
    def test_scripts_and_version(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert 'n3mc-workingmemory = "n3mc_mcp.__main__:run"' in text
        assert 'n3mc-mcp-lite = "n3mc_mcp.__main__:run"' in text
        assert re.search(r'^version = "2\.0\.2"$', text, re.M) and __version__ == "2.0.2"
        assert '"mcp>=1.0.0,<2.0.0"' in text
        if tomllib:
            data = tomllib.loads(text)
            assert data["project"]["scripts"]["n3mc-lite"] == "n3mc_mcp.cli:main"

    def test_server_info(self):
        init = run_session(lambda s: asyncio.sleep(0, result=s))
        srv = server.build_server()
        opts = srv.create_initialization_options()
        assert opts.server_name == "n3mc-workingmemory" and opts.server_version == __version__


class TestThinTools:
    def test_tools_list_matches_spec(self):
        block = re.search(r"`````python\n@app.list_tools\(\)\n(.*?)\n`````", SPEC, re.S).group(1)
        from mcp.types import Tool
        ns = {"Tool": Tool}
        exec("def f():\n" + block.split("async def list_tools() -> list[Tool]:\n", 1)[1], ns)
        expected = [t.model_dump() for t in ns["f"]()]
        listed = run_session(lambda s: s.list_tools())
        assert [t.model_dump(exclude_none=True) for t in listed.tools] == \
               [Tool(**e).model_dump(exclude_none=True) for e in expected]

    def test_instructions_match_spec(self):
        block = re.search(r"`````text\n(N3MemoryCore — Lite \(Ephemeral Memory\)\n.*?)\n`````", SPEC, re.S).group(1)
        assert INSTRUCTIONS == block + "\n"
        opts = server.build_server().create_initialization_options()
        assert opts.instructions == INSTRUCTIONS

    def test_validate_input_false(self, monkeypatch):
        from mcp.server.lowlevel import Server
        seen = []
        real = Server.call_tool
        monkeypatch.setattr(Server, "call_tool", lambda self, **kw: (seen.append(kw), real(self, **kw))[1])
        server.build_server()
        assert seen == [{"validate_input": False}]

    def test_old_sdk_fallback(self, monkeypatch):
        from mcp.server.lowlevel import Server
        real = Server.call_tool
        calls = []

        def old(self, **kw):
            calls.append(kw)
            if kw:
                raise TypeError("unexpected keyword")
            return real(self)

        monkeypatch.setattr(Server, "call_tool", old)
        server.build_server()
        assert calls == [{"validate_input": False}, {}]


class TestForwarding:
    @pytest.mark.parametrize("name", [t.name for t in TOOLS])
    def test_each_tool_forwarded(self, monkeypatch, name):
        sent = capture_post(monkeypatch, answer="exact text ✓\n---")
        args = {"query": "q", "content": "c", "id": "i", "session_id": "s", "turn_id": "t"}
        assert server.handle_call(name, args) == "exact text ✓\n---"
        port, path, body = sent[0]
        assert port == conftest.SESSION_PORT and path == f"/tools/{name}?nudge=1"

    def test_session_filled(self, monkeypatch):
        sent = capture_post(monkeypatch)
        server.handle_call("search_memory", {"query": "x"})
        server.handle_call("save_memory", {"content": "x", "session_id": "  "})
        server.handle_call("save_memory", {"content": "x", "session_id": "mine"})
        server.handle_call("list_memories", {})
        sids = [b.get("session_id") for _p, _path, b in sent]
        assert sids[0] == sids[1] == server.SESSION_ID and sids[2] == "mine" and sids[3] is None

    def test_session_from_env(self, monkeypatch):
        monkeypatch.setenv("N3MC_SESSION_ID", "env-session")
        code = ("import os; from n3mc_mcp import server; print(server.SESSION_ID)")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=dict(os.environ))
        assert out.stdout.strip() == "env-session"

    def test_agent_name(self, monkeypatch):
        sent = capture_post(monkeypatch)
        server.handle_call("save_memory", {"content": "x"}, client_name=" claude-ai ")
        monkeypatch.setenv("N3MC_AGENT_NAME", "desktop")
        server.handle_call("save_memory", {"content": "x"}, client_name="claude-ai")
        server.handle_call("save_memory", {"content": "x", "agent_name": "given"}, client_name="claude-ai")
        monkeypatch.delenv("N3MC_AGENT_NAME")
        server.handle_call("save_memory", {"content": "x"})
        names = [b.get("agent_name") for _p, _path, b in sent]
        assert names == ["claude-ai", "desktop", "given", None]

    def test_through_mcp_no_is_error_and_no_validation(self, monkeypatch):
        sent = capture_post(monkeypatch, answer='{"saved": false, "status": "error", "reason": "importance must be numeric (got nan)"}')

        async def go(s):
            return await s.call_tool("save_memory", {"content": "x", "importance": "nan"})

        res = run_session(go)
        assert res.isError is False and res.content[0].text.startswith('{"saved": false')
        assert sent[0][2]["importance"] == "nan" and sent[0][2]["agent_name"] == "pytest-client"

    def test_unknown_tool_local(self, monkeypatch):
        sent = capture_post(monkeypatch)
        assert server.handle_call("nope", {}) == "Error: unknown tool 'nope'" + tools._NUDGE_GENERIC
        assert sent == []

    def test_exception_becomes_error_text(self, monkeypatch):
        monkeypatch.setattr(cli, "ensure_server", lambda cfg, max_wait=60: 1 / 0)
        out = server.handle_call("list_memories", {})
        assert out.startswith("Error: ZeroDivisionError") and out.endswith(tools._NUDGE_GENERIC)


class TestThinNoServer:
    def test_save_direct(self, rconn):
        cli._record_start_failure("x")
        r = parse_json(server.handle_call("save_memory", {"content": "direct from mcp"}))
        assert r["saved"] and r["warning"] == "embedding_unavailable"
        assert r["detail"].startswith("The N3MC Lite memory server was not available (the memory server could not start")
        assert not rconn.hexists("mem:" + r["id"], "embedding")
        assert server.handle_call("save_memory", {"content": "again"}).endswith(tools._NUDGE_AFTER_SAVE)

    @pytest.mark.parametrize("name", ["search_memory", "list_memories", "recall_thread"])
    def test_read_tools(self, name, monkeypatch):
        monkeypatch.setattr(cli, "ensure_server", lambda cfg, max_wait=60: cli._foreign(cfg, "/x"))
        out = server.handle_call(name, {"query": "q", "turn_id": "t"})
        assert out.startswith("Error: memory backend unreachable. The N3MC Lite memory server is not available "
                              f"(another N3MC folder's server is using port {conftest.SESSION_PORT}).")
        assert out.endswith(tools._NUDGE_GENERIC)

    def test_repair(self):
        out = server.handle_call("repair_memory", {})
        assert parse_json(out)["status"] == "error" and "Retry in a minute." in out

    def test_redis_down_too(self, isolated):
        p = isolated["data"] / "config.json"
        obj = json.loads(p.read_text(encoding="utf-8"))
        obj["redis_url"] = conftest.UNREACHABLE_URL
        p.write_text(json.dumps(obj), encoding="utf-8")
        r = parse_json(server.handle_call("save_memory", {"content": "nowhere"}))
        assert r["status"] == "error" and "docker start redis-stack" in r["reason"]

    @pytest.mark.parametrize("name,args", [("delete_memory", {"id": "x"}),
                                           ("delete_memories_by_session", {"session_id": "s"})])
    def test_delete_states(self, monkeypatch, capsys, name, args):
        out = server.handle_call(name, args)
        assert "Nothing was deleted" in out
        capture_post(monkeypatch, exc=cli._NoAnswer("TimeoutError: timed out"))
        out = server.handle_call(name, args)
        assert parse_json(out)["status"] == "unknown" and "may have completed" in out
        capture_post(monkeypatch, answer='{"status": "deleted"}')
        assert server.handle_call(name, args) == '{"status": "deleted"}'
        flag = "--delete" if name == "delete_memory" else "--delete-session"
        capture_post(monkeypatch, exc=cli._NoAnswer("TimeoutError: timed out"))
        cli.main([flag, list(args.values())[0]])
        assert '"status": "unknown"' in capsys.readouterr().out


class TestRedisDisplayAndCodeVersion:
    def test_display(self):
        assert redis_display("redis://user:pw@host:6390/2") == "host:6390/2"
        assert redis_display("redis://u:secret@host:abc/0") == "<unparseable redis_url>"

    def test_no_secret_anywhere(self, monkeypatch, cfg, app_factory):
        from fastapi.testclient import TestClient
        from n3mc_mcp.database import Database
        bad = dict(cfg, redis_url="redis://u:pw-s3cr3t@host:abc/0")
        client = TestClient(app_factory(Database(bad)))
        h = client.get("/health", headers={"Host": f"127.0.0.1:{cfg['server_port']}"}).json()
        assert "s3cr3t" not in json.dumps(h)
        conftest.route_http_to(monkeypatch, client, cfg["server_port"])
        cli.ensure_server(cfg)
        for p in (paths.notices_path(), paths.audit_log_path()):
            assert "s3cr3t" not in p.read_text(encoding="utf-8")

    def test_code_version_matches_after_fill(self, isolated):
        c = load_config(quiet=True)
        assert compute_code_version(c["_config_raw"]) == compute_code_version(read_config_raw())

    def test_change_between_load_and_start(self, isolated, monkeypatch):
        c = load_config(quiet=True)
        server_cv = compute_code_version(c["_config_raw"])
        p = isolated["data"] / "config.json"
        obj = json.loads(p.read_text(encoding="utf-8"))
        obj["min_results"] = 2
        p.write_text(json.dumps(obj), encoding="utf-8")
        h = {"status": "ok", "root": str(paths.data_dir()), "code_version": server_cv}
        assert cli._health_decision(h) == "stale"
        h["code_version"] = compute_code_version(load_config(quiet=True)["_config_raw"])
        assert cli._health_decision(h) == "ok"

    def test_stop_server(self, monkeypatch, capsys, cfg):
        root = str(paths.data_dir())
        monkeypatch.setattr(cli, "_health", lambda p: {"status": "ok", "root": root, "pid": 77})
        monkeypatch.setattr(cli, "_request_shutdown", lambda p, r: (200, {}))
        monkeypatch.setattr(cli, "_wait_port_free", lambda p, t=15: True)
        assert cli.main(["--stop-server"]) == 0
        assert capsys.readouterr().out == "Stopped the N3MC Lite server (pid 77).\n"
        monkeypatch.setattr(cli, "_wait_port_free", lambda p, t=15: False)
        assert cli.main(["--stop-server"]) == 1
        assert "Could not stop the N3MC Lite server (pid 77)" in capsys.readouterr().err
        monkeypatch.setattr(cli, "_health", lambda p: {"status": "ok", "root": "/other", "pid": 1})
        assert cli.main(["--stop-server"]) == 0
        assert capsys.readouterr().out == "No N3MC Lite server of this data folder is running.\n"
        monkeypatch.setattr(cli, "_health", lambda p: None)
        assert cli.main(["--stop-server"]) == 0


class TestThinStart:
    def test_background_ensure_and_per_call(self, monkeypatch):
        gate = threading.Event()
        calls = []

        def slow(cfg, max_wait=60):
            calls.append(threading.current_thread().name)
            gate.wait(10)
            return False

        monkeypatch.setattr(cli, "ensure_server", slow)
        server.current_config()
        t = server.start_background_ensure()
        t0 = time.monotonic()

        async def go(s):
            return await s.list_tools()

        listed = run_session(go)
        assert len(listed.tools) == 7 and time.monotonic() - t0 < 5
        gate.set()
        t.join(5)
        monkeypatch.setattr(cli, "ensure_server", lambda cfg, max_wait=60: calls.append("call") or False)
        server.handle_call("list_memories", {})
        assert calls[0] == "n3mc-ensure-server" and calls[-1] == "call"

    def test_never_shutdown(self, monkeypatch):
        shut = []
        monkeypatch.setattr(cli, "_request_shutdown", lambda *a: shut.append(a))
        capture_post(monkeypatch)
        for t in TOOLS:
            server.handle_call(t.name, {})
        assert shut == []


class TestThinNoModel:
    def test_direct_save_modules_loaded_before_stdio(self):
        code = ("import sys, json; import n3mc_mcp.server; "
                "print(json.dumps([m in sys.modules for m in ('n3mc_mcp.database', 'redis', 'psutil')]))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
        assert json.loads(out.stdout.strip().splitlines()[-1]) == [True, True, True], out.stderr

    def test_no_model_imports(self):
        cli._record_start_failure("no real server in this test")
        code = ("import sys, json; from n3mc_mcp import server, __main__; "
                "server.handle_call('list_memories', {}); "
                "print(json.dumps([m for m in ('sentence_transformers', 'torch') if m in sys.modules]))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                             env=dict(os.environ, N3MC_DATA_DIR=str(paths.data_dir())))
        assert json.loads(out.stdout.strip().splitlines()[-1]) == [], out.stderr

    def test_nothing_on_stdout(self, capsys, monkeypatch):
        cli._record_start_failure("x")
        server.handle_call("list_memories", {})
        server.handle_call("nope", {})
        assert capsys.readouterr().out == ""


def _hf_home():
    env = os.environ.get("HF_HOME_REAL") or str(conftest.REAL_HOME / ".cache" / "huggingface")
    return env if (Path(env) / "hub" / "models--intfloat--multilingual-e5-base").is_dir() else None


class TestThinSubprocess:
    def test_real_roundtrip(self, rconn, cfg, monkeypatch):
        hf = _hf_home()
        if not hf:
            pytest.skip("the embedding model is not in the Hugging Face cache; the test does not download it")
        if shutil.which("n3mc-workingmemory") is None and not Path(sys.executable).with_name("Scripts").exists():
            pytest.skip("n3mc-workingmemory is not installed")
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        env = dict(os.environ, HF_HOME=hf, HF_HUB_OFFLINE="1", N3MC_AGENT_NAME="test-client")
        params = StdioServerParameters(command=sys.executable, args=["-m", "n3mc_mcp"], env=env,
                                       cwd=str(paths.data_dir()))
        port = cfg["server_port"]

        async def go():
            async with stdio_client(params) as (r, w):
                async with ClientSession(r, w) as s:
                    t0 = time.monotonic()
                    await s.initialize()
                    init_s = time.monotonic() - t0
                    tools_ = await s.list_tools()
                    saved = None
                    for _ in range(60):
                        res = await s.call_tool("save_memory", {"content": "subprocess roundtrip record"})
                        saved = parse_json(res.content[0].text)
                        if saved.get("saved") and "warning" not in saved:
                            break
                        if saved.get("saved"):
                            break
                        await asyncio.sleep(2)
                    found = await s.call_tool("search_memory", {"query": "subprocess roundtrip record"})
                    return init_s, len(tools_.tools), saved, found.content[0].text

        try:
            init_s, n, saved, text = asyncio.run(go())
            # spec §4.6: about 2 s. Measured from the process start, so Python's start-up and the imports are
            # included; the bound here only catches a client that waits for the server or the model.
            print(f"initialize took {init_s:.2f} s")
            assert init_s < 5 and n == 7 and saved["saved"] is True
            assert "subprocess roundtrip record" in text
            time.sleep(1.0)
            try:
                h = cli._request("GET", port, "/health")[1]
            except (cli._NotSent, cli._NoAnswer):
                h = None        # the client's job object ended the server with the entry (spec §3.15)
                cli._wait_port_free(port, 15)
            if h:
                assert h["status"] == "ok"
            else:
                monkeypatch.setattr(cli, "_get", lambda p, path, timeout=5: cli._request("GET", p, path, None, timeout))
                monkeypatch.setattr(cli, "_post", lambda p, path, body, timeout=30: cli._request("POST", p, path, body, timeout))
                monkeypatch.setattr(cli, "_popen", subprocess.Popen)
                assert cli.ensure_server(load_config(quiet=True), max_wait=120) is True
        finally:
            if not cli._port_free(port):
                try:
                    cli._request("POST", port, "/shutdown", {"root": str(paths.data_dir())})
                except Exception:  # noqa: BLE001
                    pass
                cli._wait_port_free(port, 15)
