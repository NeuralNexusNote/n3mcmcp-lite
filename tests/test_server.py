"""
MCP Lite server / tool integration tests.

These tests exercise the tool dispatch path end-to-end against a Redis
Stack test DB (see conftest.py). We do NOT spin up stdio transport —
we call the tool functions directly.
"""
import asyncio
import json
import os
import time

import pytest


@pytest.fixture
def server_mod(isolated_data_dir, monkeypatch):
    """Import the server module with a clean data dir + test Redis URL, then run startup."""
    import importlib

    test_url = os.environ.get("N3MC_REDIS_TEST_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("N3MC_REDIS_URL", test_url)

    from n3mc_mcp import config as cfg_mod
    importlib.reload(cfg_mod)
    from n3mc_mcp import server as s
    importlib.reload(s)
    s._startup()

    # Flush the test DB so each test starts clean, then re-ensure the index.
    s._CLIENT.flushdb()
    from n3mc_mcp.database import ensure_index
    ensure_index(s._CLIENT)

    yield s

    s._CLIENT.flushdb()


def _wait_indexed(client, expected_total, timeout=2.0):
    from n3mc_mcp.database import count_memories
    deadline = time.time() + timeout
    while time.time() < deadline:
        if count_memories(client) >= expected_total:
            return
        time.sleep(0.05)


class TestToolRegistration:
    def test_list_tools_returns_five_tools(self, server_mod):
        tools = asyncio.run(server_mod.list_tools())
        names = {t.name for t in tools}
        assert names == {
            "search_memory", "save_memory", "list_memories",
            "delete_memory", "repair_memory",
        }

    def test_each_tool_has_schema(self, server_mod):
        tools = asyncio.run(server_mod.list_tools())
        for t in tools:
            assert t.description
            assert t.inputSchema.get("type") == "object"


class TestSaveAndSearch:
    def test_save_then_search_roundtrip(self, server_mod):
        save_result = asyncio.run(server_mod.call_tool(
            "save_memory", {"content": "Abraham Lincoln was the 16th US president"}
        ))
        payload = json.loads(save_result[0].text)
        assert payload["saved"] is True
        assert "id" in payload
        assert payload["ttl_seconds"] > 0

        _wait_indexed(server_mod._CLIENT, 1)

        search_result = asyncio.run(server_mod.call_tool(
            "search_memory", {"query": "Lincoln president"}
        ))
        text = search_result[0].text
        assert "Lincoln" in text

    def test_exact_duplicate_rejected(self, server_mod):
        asyncio.run(server_mod.call_tool("save_memory", {"content": "duplicate target"}))
        result = asyncio.run(server_mod.call_tool("save_memory", {"content": "duplicate target"}))
        payload = json.loads(result[0].text)
        assert payload["saved"] is False
        assert payload["status"] == "duplicate"

    def test_empty_content_rejected(self, server_mod):
        result = asyncio.run(server_mod.call_tool("save_memory", {"content": "   "}))
        assert "empty content" in result[0].text


class TestListAndDelete:
    def test_list_memories_shows_recent(self, server_mod):
        for i in range(3):
            asyncio.run(server_mod.call_tool(
                "save_memory", {"content": f"entry number {i} with some detail"}
            ))
        _wait_indexed(server_mod._CLIENT, 3)
        result = asyncio.run(server_mod.call_tool("list_memories", {"limit": 10}))
        assert "entry number" in result[0].text

    def test_delete_nonexistent(self, server_mod):
        result = asyncio.run(server_mod.call_tool(
            "delete_memory", {"id": "00000000-0000-0000-0000-000000000000"}
        ))
        payload = json.loads(result[0].text)
        assert payload["status"] == "not_found"


class TestRepair:
    def test_repair_on_empty_db(self, server_mod):
        result = asyncio.run(server_mod.call_tool("repair_memory", {}))
        payload = json.loads(result[0].text)
        assert payload["status"] == "ok"


class TestUnknownTool:
    def test_unknown_tool_returns_error(self, server_mod):
        result = asyncio.run(server_mod.call_tool("nonexistent_tool", {}))
        assert "Unknown tool" in result[0].text
