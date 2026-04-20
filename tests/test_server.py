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
        # Unknown tool must surface as a proper MCP tool error so the
        # LLM cannot treat it as a successful "no results" return.
        assert result.isError is True
        assert "Unknown tool" in result.content[0].text


class TestRedisUnreachableSurfacesError:
    """When Redis is down, every tool call MUST return isError=True.

    A plain text-content string containing "Error: ..." looks like
    success to the MCP client — the LLM may paste it back verbatim or
    silently swallow it, and every subsequent save_memory call also
    fails silently. The Shiranui case lost all content this way.
    """

    def _force_redis_down(self, server_mod):
        """Point the module's client at an unreachable URL without
        touching the real redis-stack the rest of the suite depends on."""
        from n3mc_mcp.database import get_redis_client
        # Redis on a definitely-closed port; ping will fail fast.
        server_mod._CLIENT = get_redis_client("redis://127.0.0.1:1/0")

    def test_search_returns_mcp_error_when_redis_down(self, server_mod):
        original = server_mod._CLIENT
        try:
            self._force_redis_down(server_mod)
            result = asyncio.run(server_mod.call_tool(
                "search_memory", {"query": "anything"}
            ))
            assert result.isError is True, (
                "search_memory must set isError=True when Redis is unreachable "
                "— otherwise the LLM treats the failure as a successful empty "
                "search and proceeds to generate content that will never be "
                "saved."
            )
            text = result.content[0].text
            assert "Redis is not reachable" in text
            assert "docker" in text  # recovery hint present
        finally:
            server_mod._CLIENT = original

    def test_save_returns_mcp_error_when_redis_down(self, server_mod):
        original = server_mod._CLIENT
        try:
            self._force_redis_down(server_mod)
            result = asyncio.run(server_mod.call_tool(
                "save_memory", {"content": "this must not silently succeed"}
            ))
            assert result.isError is True, (
                "save_memory must set isError=True when Redis is unreachable "
                "— a silent success here is how the Lite build loses user "
                "content without the LLM ever noticing."
            )
            text = result.content[0].text
            assert "Redis is not reachable" in text
            # The error must warn the user that nothing is being persisted
            # this turn, so long-form generation can be aborted.
            assert "NOT persist" in text or "not persist" in text
        finally:
            server_mod._CLIENT = original

    def test_list_delete_repair_also_error_when_redis_down(self, server_mod):
        original = server_mod._CLIENT
        try:
            self._force_redis_down(server_mod)
            for name, args in [
                ("list_memories", {}),
                ("delete_memory", {"id": "00000000-0000-0000-0000-000000000000"}),
                ("repair_memory", {}),
            ]:
                result = asyncio.run(server_mod.call_tool(name, args))
                assert result.isError is True, (
                    f"{name} must set isError=True when Redis is unreachable"
                )
        finally:
            server_mod._CLIENT = original


class TestParentDocRecall:
    """Option A: long content saved verbatim survives chunked search."""

    LONG_DOC = (
        "架空の浮遊都市「不知火(しらぬい)」設定資料\n\n"
        "【概要】不知火は反重力結晶エーテライトを動力源とする全長12kmの"
        "浮遊都市で、標高4000m前後を自律航行する。建都は1873年、建設者は"
        "エドガー・ヴァンクロス博士。首都機能と学術機能を兼ね備えた"
        "特殊な都市として、周辺諸国との外交関係を維持している。\n\n"
        "【構造】都市は上層のアカデミー区、中層の市街区、下層の工業区に"
        "分かれ、各層は反重力エレベータで接続される。中央塔は"
        "「天穹の柱」と呼ばれ、エーテライト核を格納している。周辺部には"
        "気象観測所、天文台、浮遊船の係留塔が配置される。\n\n"
        "【住民】公称人口は32,500人で、学者、技術者、商人、航海士が"
        "主要な職業となっている。公用語は古不知火語と共通語の二言語で、"
        "初等教育から両言語の習得が義務付けられている。\n\n"
        "【歴史】第二次大嵐戦役(1921)で南半分が崩落したが、"
        "10年かけて再建された。現在は中立都市として四大陸と交易を行う。"
        "戦役記念碑は中央広場に建てられ、毎年慰霊祭が開催される。\n\n"
        "【経済】主要産業はエーテライト精製、航空機製造、精密機械、"
        "学術出版。対外通貨は不知火クローネ、域内では銀貨も流通する。\n\n"
        "【文化】古不知火暦では一年を14ヶ月に分割し、各月に神話の"
        "星座名を与える。祭事は風月祭、光穹祭、静寂祭の三大祭が有名。"
    )

    def test_long_content_saves_as_parent_with_chunks(self, server_mod):
        result = asyncio.run(server_mod.call_tool(
            "save_memory", {"content": self.LONG_DOC}
        ))
        payload = json.loads(result[0].text)
        assert payload["saved"] is True
        assert payload.get("parent_id")
        assert payload["chunks"] > 1
        assert payload["saved_count"] >= 1

    def test_search_returns_full_parent_content(self, server_mod):
        save = asyncio.run(server_mod.call_tool(
            "save_memory", {"content": self.LONG_DOC}
        ))
        parent_id = json.loads(save[0].text)["parent_id"]
        _wait_indexed(server_mod._CLIENT, 1)

        result = asyncio.run(server_mod.call_tool(
            "search_memory", {"query": "不知火 設定"}
        ))
        text = result[0].text
        # Full verbatim passages from all sections should survive in output.
        assert "エーテライト" in text
        assert "エドガー・ヴァンクロス" in text
        assert "天穹の柱" in text
        assert "第二次大嵐戦役" in text
        assert parent_id in text

    def test_search_dedupes_chunks_to_single_parent(self, server_mod):
        save = asyncio.run(server_mod.call_tool(
            "save_memory", {"content": self.LONG_DOC}
        ))
        parent_id = json.loads(save[0].text)["parent_id"]
        _wait_indexed(server_mod._CLIENT, 1)

        result = asyncio.run(server_mod.call_tool(
            "search_memory", {"query": "不知火"}
        ))
        text = result[0].text
        # Parent id appears once even though multiple chunks matched.
        assert text.count(parent_id) == 1

    def test_parent_exact_duplicate_rejected(self, server_mod):
        first = asyncio.run(server_mod.call_tool(
            "save_memory", {"content": self.LONG_DOC}
        ))
        first_id = json.loads(first[0].text)["parent_id"]

        second = asyncio.run(server_mod.call_tool(
            "save_memory", {"content": self.LONG_DOC}
        ))
        payload = json.loads(second[0].text)
        assert payload["saved"] is False
        assert payload["status"] == "duplicate"
        assert payload.get("parent_id") == first_id

    def test_list_shows_parent_and_hides_chunks(self, server_mod):
        save = asyncio.run(server_mod.call_tool(
            "save_memory", {"content": self.LONG_DOC}
        ))
        parent_id = json.loads(save[0].text)["parent_id"]
        _wait_indexed(server_mod._CLIENT, 1)

        result = asyncio.run(server_mod.call_tool("list_memories", {"limit": 50}))
        text = result[0].text
        assert parent_id in text
        assert "[doc" in text  # parent marker rendered

    def test_delete_parent_cascades_to_chunks(self, server_mod):
        from n3mc_mcp.database import _find_chunk_ids_for_parent, get_parent_doc

        save = asyncio.run(server_mod.call_tool(
            "save_memory", {"content": self.LONG_DOC}
        ))
        parent_id = json.loads(save[0].text)["parent_id"]

        assert get_parent_doc(server_mod._CLIENT, parent_id) is not None
        assert _find_chunk_ids_for_parent(server_mod._CLIENT, parent_id)

        result = asyncio.run(server_mod.call_tool(
            "delete_memory", {"id": parent_id}
        ))
        payload = json.loads(result[0].text)
        assert payload["status"] == "ok"

        assert get_parent_doc(server_mod._CLIENT, parent_id) is None
        assert _find_chunk_ids_for_parent(server_mod._CLIENT, parent_id) == []
