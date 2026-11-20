"""Layer 8: MCP thin client (no model; HTTP sender replaced)."""

import json
import pathlib
import subprocess
import sys

from n3mc_mcp import cli
from n3mc_mcp import server as thin
from n3mc_mcp._verbatim import _NUDGE_AFTER_SAVE, _NUDGE_GENERIC


class TestThinNames:
    def test_pyproject(self):
        t = (pathlib.Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8")
        assert 'version = "2.0.0"' in t and '"mcp>=1.0.0,<2.0.0"' in t
        assert 'n3mc-workingmemory = "n3mc_mcp.__main__:run"' in t
        assert 'n3mc-mcp-lite = "n3mc_mcp.__main__:run"' in t
        assert thin.SERVER_NAME == "n3mc-workingmemory"


class TestForwarding:
    def test_forward_verbatim_and_fill(self, cfg, monkeypatch):
        sent = []
        monkeypatch.setattr(cli, "ensure_server", lambda cfg, max_wait=60: True)

        def post(path, body, port, timeout=30):
            sent.append((path, body))
            return 200, {"text": "EXACT TEXT"}
        monkeypatch.setattr(cli, "_post", post)
        monkeypatch.setenv("N3MC_AGENT_NAME", "test-client")
        assert thin.call_tool_sync(cfg, "save_memory", {"content": "c", "importance": "nan"}, "cli") == \
            "EXACT TEXT"
        thin.call_tool_sync(cfg, "search_memory", {"query": "q"})
        thin.call_tool_sync(cfg, "list_memories", None)
        assert sent[0][0] == "/tools/save_memory?nudge=1"
        assert sent[0][1]["agent_name"] == "test-client" and sent[0][1]["importance"] == "nan"
        assert sent[0][1]["session_id"] == sent[1][1]["session_id"] == thin._SESSION
        assert sent[2][1] == {}

    def test_client_name_fallback(self, monkeypatch):
        assert thin.fill_args("save_memory", {}, "claude-ai")["agent_name"] == "claude-ai"
        assert "agent_name" not in thin.fill_args("save_memory", {}, None)


class TestThinNoServer:
    def test_direct_save_and_texts(self, cfg, monkeypatch, rconn):
        monkeypatch.setattr(cli, "ensure_server", lambda cfg, max_wait=60: (cli._set_reason("X"), False)[1])
        out = thin.call_tool_sync(cfg, "save_memory", {"content": "saved without server"})
        res = json.JSONDecoder().raw_decode(out)[0]
        assert res["saved"] and res["warning"] == "embedding_unavailable" and "(X)" in res["detail"]
        assert out.endswith(_NUDGE_AFTER_SAVE)
        assert not rconn.hexists("mem:" + res["id"], "embedding") and rconn.ttl("mem:" + res["id"]) > 0
        assert thin.call_tool_sync(cfg, "search_memory", {"query": "q"}).startswith(
            "Error: memory backend unreachable. The N3MC Lite memory server is not available (X).")
        d = thin.call_tool_sync(cfg, "delete_memory", {"id": "x"})
        assert "Nothing was deleted" in d and d.endswith(_NUDGE_GENERIC)


class TestThinNoModel:
    def test_no_torch_import(self, isolated):
        code = ("import sys, n3mc_mcp.server, n3mc_mcp.cli, n3mc_mcp.database; "
                "print('sentence_transformers' in sys.modules, 'torch' in sys.modules)")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        assert r.stdout.strip() == "False False"
