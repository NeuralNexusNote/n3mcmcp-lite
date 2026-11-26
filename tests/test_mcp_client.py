"""Layer 8: the MCP thin client (spec §9 Layer 8). No model; the HTTP sender is faked, plus one subprocess test."""
import asyncio
import json
import os
import subprocess
import sys
import time

import pytest

from conftest import parse_json
from n3mc_mcp import cli, paths, server
from n3mc_mcp.instructions import INSTRUCTIONS
from n3mc_mcp.tooldefs import TOOLS
from n3mc_mcp.tools import _NUDGE_GENERIC

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestThinNames:
    def test_scripts(self):
        try:
            import tomllib
        except ImportError:
            pytest.skip("tomllib needs Python 3.11")
        data = tomllib.load(open(os.path.join(REPO, "pyproject.toml"), "rb"))
        s = data["project"]["scripts"]
        assert s["n3mc-workingmemory"] == "n3mc_mcp.__main__:run" == s["n3mc-mcp-lite"]
        assert data["project"]["version"] == "2.0.2"
        assert "mcp>=1.0.0,<2.0.0" in data["project"]["dependencies"]

    def test_server_name(self):
        assert server.SERVER_NAME == "n3mc-workingmemory"


class TestThinTools:
    def test_list_and_instructions(self):
        app = server.build_server(server.ThinClient())
        from mcp.types import ListToolsRequest
        res = asyncio.run(app.request_handlers[ListToolsRequest](ListToolsRequest(method="tools/list")))
        assert [t.name for t in res.root.tools] == [t.name for t in TOOLS]
        assert app.instructions == INSTRUCTIONS

    def test_validate_input_off(self, monkeypatch):
        from mcp.server.lowlevel import Server
        seen = {}
        real = Server.call_tool

        def spy(self, *a, **kw):
            seen.update(kw)
            return real(self, *a, **kw)

        monkeypatch.setattr(Server, "call_tool", spy)
        server.build_server(server.ThinClient())
        assert seen.get("validate_input") is False


@pytest.fixture
def thin(monkeypatch):
    t = server.ThinClient()
    sent = []
    monkeypatch.setattr(cli, "ensure_server", lambda cfg, max_wait=60: True)

    def fake_post(port, path, body, timeout=30):
        sent.append((path, body))
        return 200, {"text": "SERVER TEXT"}

    monkeypatch.setattr(cli, "_post", fake_post)
    t.sent = sent
    return t


class TestForwarding:
    def test_forwards_verbatim(self, thin):
        for t in TOOLS:
            assert thin.handle(t.name, {"x": 1}) == "SERVER TEXT"
            assert thin.sent[-1][0] == f"/tools/{t.name}?nudge=1"

    def test_session_filled(self, thin, monkeypatch):
        thin.handle("search_memory", {"query": "q"})
        s1 = thin.sent[-1][1]["session_id"]
        thin.handle("save_memory", {"content": "c", "session_id": "  "})
        assert thin.sent[-1][1]["session_id"] == s1 == server.PROCESS_SESSION
        thin.handle("save_memory", {"content": "c", "session_id": "mine"})
        assert thin.sent[-1][1]["session_id"] == "mine"
        thin.handle("list_memories", {})
        assert "session_id" not in thin.sent[-1][1]

    def test_agent_name(self, thin, monkeypatch):
        thin.handle("save_memory", {"content": "c"})
        assert thin.sent[-1][1]["agent_name"] == ""
        thin.client_name = "claude-ai"
        thin.handle("save_memory", {"content": "c", "agent_name": "  "})
        assert thin.sent[-1][1]["agent_name"] == "claude-ai"
        monkeypatch.setenv("N3MC_AGENT_NAME", "env-name")
        thin.handle("save_memory", {"content": "c"})
        assert thin.sent[-1][1]["agent_name"] == "env-name"
        thin.handle("save_memory", {"content": "c", "agent_name": "arg"})
        assert thin.sent[-1][1]["agent_name"] == "arg"

    def test_exception(self, monkeypatch):
        app = server.build_server(server.ThinClient())
        monkeypatch.setattr(server.ThinClient, "handle", lambda self, n, a: (_ for _ in ()).throw(ValueError("bad")))
        from mcp.types import CallToolRequest, CallToolRequestParams
        req = CallToolRequest(method="tools/call", params=CallToolRequestParams(name="list_memories", arguments={}))

        async def go():
            return await app.request_handlers[CallToolRequest](req)

        try:
            res = asyncio.run(go())
        except LookupError:
            pytest.skip("request context not available outside a session")
        assert res.root.content[0].text == "Error: bad" + _NUDGE_GENERIC
        assert not res.root.isError

    def test_unknown_local(self, thin):
        assert thin.handle("nope", {}) == "Error: unknown tool 'nope'" + _NUDGE_GENERIC
        assert not thin.sent

    def test_nan_importance_reaches_server(self, thin):
        thin.handle("save_memory", {"content": "c", "importance": "nan"})
        assert thin.sent[-1][1]["importance"] == "nan"


class TestThinNoServer:
    @pytest.fixture
    def down(self, monkeypatch):
        monkeypatch.setattr(cli, "ensure_server", lambda cfg, max_wait=60: False)
        return server.ThinClient()

    def test_save_direct(self, down, rconn):
        out = down.handle("save_memory", {"content": "direct from thin"})
        r = parse_json(out)
        assert r["saved"] and r["warning"] == "embedding_unavailable"
        assert r["detail"].startswith("The N3MC Lite memory server was not available (")
        assert not rconn.hexists("mem:" + r["id"], "embedding")

    def test_other_tools(self, down):
        out = down.handle("search_memory", {"query": "x"})
        assert out.startswith("Error: memory backend unreachable. The N3MC Lite memory server is not available (")
        assert out.endswith(_NUDGE_GENERIC)
        d = parse_json(down.handle("delete_memory", {"id": "x"}))
        assert d["status"] == "error" and "Nothing was deleted" in d["reason"]
        r = parse_json(down.handle("repair_memory", {}))
        assert r["status"] == "error"

    def test_redis_down_too(self, down, isolate):
        p = isolate["data"] / "config.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        d["redis_url"] = "redis://127.0.0.1:1/0"
        p.write_text(json.dumps(d), encoding="utf-8")
        r = parse_json(down.handle("save_memory", {"content": "x"}))
        assert r["status"] == "error" and "docker start redis-stack" in r["reason"]

    def test_delete_answer_lost(self, monkeypatch):
        monkeypatch.setattr(cli, "ensure_server", lambda cfg, max_wait=60: True)
        monkeypatch.setattr(cli, "_post", lambda *a, **k: (_ for _ in ()).throw(cli.NoAnswer("timeout")))
        t = server.ThinClient()
        for name in ("delete_memory", "delete_memories_by_session"):
            assert parse_json(t.handle(name, {"id": "x", "session_id": "s"}))["status"] == "unknown"

    def test_cli_delete_not_sent(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "ensure_server", lambda cfg, max_wait=60: True)
        monkeypatch.setattr(cli, "_post", lambda *a, **k: (_ for _ in ()).throw(cli.NotSent("refused")))
        cli.main(["--delete-session", "s"])
        assert "Nothing was deleted" in capsys.readouterr().out
        monkeypatch.setattr(cli, "_post", lambda *a, **k: (_ for _ in ()).throw(cli.NoAnswer("timeout")))
        cli.main(["--delete", "x"])
        assert '"status": "unknown"' in capsys.readouterr().out


class TestRedisDisplayAndCodeVersion:
    def test_display(self):
        from n3mc_mcp.config import UNPARSEABLE, redis_display
        assert redis_display("redis://u:secret@host:6390/2") == "host:6390/2"
        assert redis_display("redis://u:secret@host:abc/0") == UNPARSEABLE

    def test_unparseable_not_leaked(self, cfg, monkeypatch):
        cfg["redis_url"] = "redis://u:secret@host:abc/0"
        cli._on_server_ok(cfg, {"redis": "127.0.0.1:6379/0"})
        for p in (paths.notices_path(), paths.audit_path()):
            assert "secret" not in open(p, encoding="utf-8").read()

    def test_code_version_match_and_stale(self, isolate):
        from n3mc_mcp.config import load_config
        c = load_config()
        assert cli.own_code_version(c) == cli.own_code_version(load_config())
        h = {"status": "ok", "root": paths.data_dir(), "code_version": cli.own_code_version(c)}
        p = isolate["data"] / "config.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        d["min_score"] = 0.25
        p.write_text(json.dumps(d), encoding="utf-8")
        c2 = load_config()
        assert cli._health_decision(h, code_version=cli.own_code_version(c2)) == "stale"


class TestThinStart:
    def test_ensure_in_background_and_per_call(self, monkeypatch):
        calls = []
        monkeypatch.setattr(cli, "ensure_server", lambda cfg, max_wait=60: (calls.append(1), True)[1])
        monkeypatch.setattr(cli, "_post", lambda *a, **k: (200, {"text": "x"}))
        t = server.ThinClient()
        t.handle("list_memories", {})
        t.handle("list_memories", {})
        assert len(calls) == 2
        src = open(server.__file__, encoding="utf-8").read()
        assert "/shutdown" not in src
        assert "threading.Thread(target=_safe_ensure" in src


class TestThinNoModel:
    def _run(self, tmp_path, stdin):
        env = dict(os.environ)
        env["N3MC_DATA_DIR"] = str(tmp_path / "d")
        os.makedirs(env["N3MC_DATA_DIR"], exist_ok=True)
        with open(os.path.join(env["N3MC_DATA_DIR"], "config.json"), "w") as f:
            json.dump({"redis_url": "redis://127.0.0.1:1/0", "server_port": 1}, f)
        code = ("import sys, n3mc_mcp.server as s\n"
                "orig = s._safe_ensure\n"
                "s._safe_ensure = lambda cli, cfg: None\n"
                "s.run()\n"
                "print('MODS', 'sentence_transformers' in sys.modules, 'torch' in sys.modules, file=sys.stderr)\n")
        return subprocess.run([sys.executable, "-c", code], input=stdin, capture_output=True, env=env, timeout=120)

    def test_empty_stdin(self, tmp_path):
        cp = self._run(tmp_path, b"")
        assert cp.stdout == b""
        assert b"MODS False False" in cp.stderr

    def test_initialize_and_list(self, tmp_path):
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
        # Like a real client: send a request, read its answer, then send the next one. (Writing everything
        # and closing stdin at once lets the entry see end-of-input before it answers tools/list.)
        env = dict(os.environ)
        env["N3MC_DATA_DIR"] = str(tmp_path / "d")
        os.makedirs(env["N3MC_DATA_DIR"], exist_ok=True)
        with open(os.path.join(env["N3MC_DATA_DIR"], "config.json"), "w") as f:
            json.dump({"redis_url": "redis://127.0.0.1:1/0", "server_port": 1}, f)
        code = ("import n3mc_mcp.server as s\n"
                "s._safe_ensure = lambda cli, cfg: None\n"
                "s.run()\n")
        p = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, env=env)
        try:
            def send(m):
                p.stdin.write((json.dumps(m) + "\n").encode())
                p.stdin.flush()

            send(msgs[0])
            first = p.stdout.readline()
            send(msgs[1])
            send(msgs[2])
            second = p.stdout.readline()
            p.stdin.close()
            rest = p.stdout.read()
            p.wait(timeout=60)
        finally:
            if p.poll() is None:
                p.kill()
        lines = [json.loads(x) for x in (first + second + rest).decode().splitlines() if x.strip()]
        assert len(lines) == 2
        init = lines[0]["result"]
        assert init["serverInfo"]["name"] == "n3mc-workingmemory" and init["instructions"] == INSTRUCTIONS
        assert init["protocolVersion"] == "2025-06-18"
        assert len(lines[1]["result"]["tools"]) == 7


class TestThinSubprocess:
    @pytest.mark.real_server
    @pytest.mark.real_model
    def test_roundtrip(self, tmp_path, rconn, isolate):
        hub = os.path.join(os.environ.get("HF_HOME", ""), "hub", "models--intfloat--multilingual-e5-base")
        if not os.path.isdir(hub):
            pytest.skip("the model is not in the Hugging Face cache")
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        env = dict(os.environ)
        exe = os.path.join(os.path.dirname(sys.executable), "Scripts" if os.name == "nt" else "bin", "n3mc-workingmemory")
        if not (os.path.exists(exe) or os.path.exists(exe + ".exe")):
            pytest.skip("n3mc-workingmemory is not installed next to this Python")
        params = StdioServerParameters(command=exe, args=[], env=env)
        port = json.loads((isolate["data"] / "config.json").read_text())["server_port"]

        async def go():
            async with stdio_client(params) as (r, w):
                async with ClientSession(r, w) as s:
                    t = time.monotonic()
                    await s.initialize()
                    elapsed = time.monotonic() - t
                    tools = await s.list_tools()
                    saved = await s.call_tool("save_memory", {"content": "subprocess roundtrip words"})
                    found = await s.call_tool("search_memory", {"query": "subprocess roundtrip words"})
                    return elapsed, tools, saved, found

        try:
            elapsed, tools, saved, found = asyncio.run(go())
            assert elapsed < 2.0
            assert len(tools.tools) == 7
            assert parse_json(saved.content[0].text)["saved"]
            assert "subprocess roundtrip words" in found.content[0].text
        finally:
            import conftest
            try:
                conftest.REAL_REQUEST(port, "POST", "/shutdown", {"root": paths.data_dir()}, timeout=5)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1)


class TestCliBufferNoAnswer:
    """§13 / §4.6: --buffer whose request times out saves directly (it never prints 'unknown tool')."""

    def test_timeout_saves_directly(self, monkeypatch, capsys, rconn):
        monkeypatch.setattr(cli, "ensure_server", lambda cfg, max_wait=60: True)
        monkeypatch.setattr(cli, "_post", lambda *a, **k: (_ for _ in ()).throw(cli.NoAnswer("TimeoutError: timed out")))
        cli.main(["--buffer", "saved after a timeout"])
        r = json.loads(capsys.readouterr().out)
        assert r["saved"] and r["warning"] == "embedding_unavailable"
        assert "TimeoutError: timed out" in r["detail"]
