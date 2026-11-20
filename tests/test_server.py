"""Layer 3: tool registration and tool dispatch through the handlers (the text the MCP client gets)."""

import json

from n3mc_mcp._verbatim import _NUDGE_AFTER_SAVE, _NUDGE_AFTER_SEARCH, _NUDGE_GENERIC
from n3mc_mcp.instructions import INSTRUCTIONS
from n3mc_mcp.tooldefs import TOOLS


class TestToolRegistration:
    def test_seven_tools(self):
        assert [t.name for t in TOOLS] == ["search_memory", "save_memory", "list_memories", "delete_memory",
                                           "delete_memories_by_session", "repair_memory", "recall_thread"]
        assert all(t.description and t.inputSchema["type"] == "object" for t in TOOLS)

    def test_instructions_verbatim_shape(self):
        assert INSTRUCTIONS.startswith("N3MemoryCore — Lite (Ephemeral Memory)\n")
        assert INSTRUCTIONS.endswith("memories were found).\n") and not INSTRUCTIONS.endswith("\n\n")
        assert "HOOK CLIENTS:" in INSTRUCTIONS


class TestSaveAndSearch:
    def test_round_trip_and_nudges(self, runner):
        out = runner.call("save_memory", {"content": "坂本龍馬は土佐藩の出身"})
        assert out.endswith(_NUDGE_AFTER_SAVE)
        assert json.JSONDecoder().raw_decode(out)[0]["saved"] is True
        out = runner.call("search_memory", {"query": "坂本龍馬"})
        assert "坂本龍馬は土佐藩の出身" in out and out.endswith(_NUDGE_AFTER_SEARCH)

    def test_errors_get_generic_nudge(self, runner):
        out = runner.call("save_memory", {"content": "x", "importance": "nan"})
        assert out.startswith('{"saved": false') and out.endswith(_NUDGE_GENERIC)
        out = runner.call("search_memory", {"query": "x", "since": "yesterday"})
        assert out.startswith("Error: since must be an ISO 8601") and out.endswith(_NUDGE_GENERIC)


class TestListAndDelete:
    def test_list_and_missing_delete(self, runner):
        assert runner.call("list_memories", {}, nudge=False) == "_No memories stored._"
        for i in range(3):
            runner.call("save_memory", {"content": f"item {i}"})
        out = runner.call("list_memories", {"limit": 3}, nudge=False)
        assert out.count("\n\n---\n\n") == 2
        assert json.loads(runner.call("delete_memory", {"id": "nope"}, nudge=False))["status"] == "not_found"


class TestRepair:
    def test_ok(self, runner):
        assert json.loads(runner.call("repair_memory", {}, nudge=False))["status"] == "ok"


class TestUnknownTool:
    def test_error_text(self, runner):
        assert runner.call("nope", {}) == "Error: unknown tool 'nope'" + _NUDGE_GENERIC


class TestBackendDown:
    def test_redis_down_hint(self, cfg):
        from n3mc_mcp.database import Database
        from n3mc_mcp.tools import ToolRunner
        cfg = dict(cfg, redis_url="redis://127.0.0.1:1/0")
        r = ToolRunner(cfg, Database(cfg))
        assert r.call("search_memory", {"query": "x"}, nudge=False).startswith(
            "Error: memory backend unreachable. Redis Stack is not reachable at 127.0.0.1:1.")
        assert json.loads(r.call("save_memory", {"content": "x"}, nudge=False))["status"] == "error"
