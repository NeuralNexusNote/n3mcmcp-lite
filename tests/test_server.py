import json
import time
import pytest
from tests.conftest import requires_redis


def _parse_json_response(text: str) -> dict:
    """Strip the trailing auto-save reminder (spec §11 / §4.3 — every tool
    response ends with a short nudge) and return the JSON payload. Plain
    `json.loads` would fail on the trailing markdown."""
    return json.JSONDecoder().raw_decode(text)[0]


# ── TestToolRegistration ──────────────────────────────────────────────────────

class TestToolRegistration:
    def test_six_tools_registered(self):
        import asyncio
        from n3mc_mcp.server import list_tools
        tools = asyncio.get_event_loop().run_until_complete(list_tools())
        names = {t.name for t in tools}
        # §4.3 で documented な 6 ツール（`delete_memories_by_session`
        # は Lite 専用、§10 Test 6 と §3.1+ session_id ガイダンス参照）。
        assert names == {
            "search_memory",
            "save_memory",
            "list_memories",
            "delete_memory",
            "delete_memories_by_session",
            "repair_memory",
        }

    def test_tools_have_descriptions(self):
        import asyncio
        from n3mc_mcp.server import list_tools
        tools = asyncio.get_event_loop().run_until_complete(list_tools())
        for t in tools:
            assert t.description and len(t.description) > 0

    def test_search_memory_requires_query(self):
        import asyncio
        from n3mc_mcp.server import list_tools
        tools = asyncio.get_event_loop().run_until_complete(list_tools())
        sm = next(t for t in tools if t.name == "search_memory")
        assert "query" in sm.inputSchema.get("required", [])

    def test_save_memory_requires_content(self):
        import asyncio
        from n3mc_mcp.server import list_tools
        tools = asyncio.get_event_loop().run_until_complete(list_tools())
        sm = next(t for t in tools if t.name == "save_memory")
        assert "content" in sm.inputSchema.get("required", [])

    def test_delete_memory_requires_id(self):
        import asyncio
        from n3mc_mcp.server import list_tools
        tools = asyncio.get_event_loop().run_until_complete(list_tools())
        dm = next(t for t in tools if t.name == "delete_memory")
        assert "id" in dm.inputSchema.get("required", [])


# ── TestSaveAndSearch ─────────────────────────────────────────────────────────

@requires_redis
class TestSaveAndSearch:
    def _setup_db(self, db):
        import n3mc_mcp.server as srv
        srv._db = db

    def test_save_returns_json(self, db):
        import asyncio
        from n3mc_mcp.server import call_tool
        self._setup_db(db)
        result = asyncio.get_event_loop().run_until_complete(
            call_tool("save_memory", {"content": "server layer test content"})
        )
        assert len(result) == 1
        data = _parse_json_response(result[0].text)
        assert data["saved"] is True

    def test_search_finds_saved(self, db):
        import asyncio
        from n3mc_mcp.server import call_tool
        self._setup_db(db)
        asyncio.get_event_loop().run_until_complete(
            call_tool("save_memory", {"content": "unique searchable astronomy content"})
        )
        time.sleep(0.5)
        result = asyncio.get_event_loop().run_until_complete(
            call_tool("search_memory", {"query": "astronomy"})
        )
        assert "astronomy" in result[0].text

    def test_exact_duplicate_rejected(self, db):
        import asyncio
        from n3mc_mcp.server import call_tool
        self._setup_db(db)
        content = "duplicate rejection test server layer"
        asyncio.get_event_loop().run_until_complete(
            call_tool("save_memory", {"content": content})
        )
        result = asyncio.get_event_loop().run_until_complete(
            call_tool("save_memory", {"content": content})
        )
        data = _parse_json_response(result[0].text)
        assert data["saved"] is False

    def test_empty_content_error(self, db):
        import asyncio
        from n3mc_mcp.server import call_tool
        self._setup_db(db)
        result = asyncio.get_event_loop().run_until_complete(
            call_tool("save_memory", {"content": ""})
        )
        data = _parse_json_response(result[0].text)
        assert data["saved"] is False

    def test_search_empty_db_no_error(self, db):
        import asyncio
        from n3mc_mcp.server import call_tool
        self._setup_db(db)
        result = asyncio.get_event_loop().run_until_complete(
            call_tool("search_memory", {"query": "nothing here"})
        )
        # The body starts with "_No memories found._" and is followed by
        # the §11 / §4.3 auto-save nudge; substring check is enough.
        assert result[0].text.startswith("_No memories found._")


# ── TestListAndDelete ─────────────────────────────────────────────────────────

@requires_redis
class TestListAndDelete:
    def _setup_db(self, db):
        import n3mc_mcp.server as srv
        srv._db = db

    def test_list_shows_saved_items(self, db):
        import asyncio
        from n3mc_mcp.server import call_tool
        self._setup_db(db)
        for i in range(3):
            asyncio.get_event_loop().run_until_complete(
                call_tool("save_memory", {"content": f"list test item number {i} unique"})
            )
        result = asyncio.get_event_loop().run_until_complete(
            call_tool("list_memories", {})
        )
        assert "list test item" in result[0].text

    def test_delete_nonexistent_not_found(self, db):
        import asyncio
        from n3mc_mcp.server import call_tool
        self._setup_db(db)
        result = asyncio.get_event_loop().run_until_complete(
            call_tool("delete_memory", {"id": "nonexistent-id-xyz"})
        )
        data = _parse_json_response(result[0].text)
        assert data["status"] == "not_found"


# ── TestRepair ────────────────────────────────────────────────────────────────

@requires_redis
class TestRepair:
    def test_repair_returns_ok(self, db):
        import asyncio
        from n3mc_mcp.server import call_tool
        import n3mc_mcp.server as srv
        srv._db = db
        result = asyncio.get_event_loop().run_until_complete(
            call_tool("repair_memory", {})
        )
        data = _parse_json_response(result[0].text)
        assert data["status"] == "ok"


# ── TestUnknownTool ───────────────────────────────────────────────────────────

@requires_redis
class TestUnknownTool:
    def test_unknown_tool_returns_error(self, db):
        import asyncio
        from n3mc_mcp.server import call_tool
        import n3mc_mcp.server as srv
        srv._db = db
        result = asyncio.get_event_loop().run_until_complete(
            call_tool("unknown_tool_xyz", {})
        )
        assert "Error" in result[0].text


# ── TestParentDocRecall ───────────────────────────────────────────────────────

@requires_redis
class TestParentDocRecall:
    def test_verbatim_recall_via_search(self, db):
        import asyncio
        from n3mc_mcp.server import call_tool
        import n3mc_mcp.server as srv
        srv._db = db

        long_text = (
            "架空都市「不知火」は南太平洋に浮かぶ人工島都市である。"
            "住民は空中庭園と水中回廊に暮らし、エネルギー源は反重力結晶とされる。"
            "行政は五名の評議員による合議制で、通貨は光子トークンが用いられる。"
            "都市の直径は約三十キロメートルで、恒久的な霧に包まれているとされる。"
        ) * 8

        save_result = asyncio.get_event_loop().run_until_complete(
            call_tool("save_memory", {"content": long_text})
        )
        data = _parse_json_response(save_result[0].text)
        assert data["saved"] is True
        parent_id = data["parent_id"]

        time.sleep(1.0)

        search_result = asyncio.get_event_loop().run_until_complete(
            call_tool("search_memory", {"query": "不知火 浮遊都市 反重力結晶"})
        )
        assert "不知火" in search_result[0].text
        assert "[doc×" in search_result[0].text

        del_result = asyncio.get_event_loop().run_until_complete(
            call_tool("delete_memory", {"id": parent_id})
        )
        del_data = _parse_json_response(del_result[0].text)
        assert del_data["status"] == "deleted"
        assert del_data.get("chunks_deleted", 0) > 0
