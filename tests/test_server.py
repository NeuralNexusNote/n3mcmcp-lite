"""Layer 3: tool handlers end to end (tools.py through the Service; isolated config, flushed DB 0)."""

import json

import pytest

from n3mc_mcp import tools
from n3mc_mcp.tooldefs import list_tools_definitions
from n3mc_mcp.tools import _NUDGE_AFTER_SAVE, _NUDGE_AFTER_SEARCH, _NUDGE_GENERIC


def call(svc, name, args=None, nudge=False):
    svc.cfg["_notices"].clear()
    return svc.call_tool(name, args or {}, nudge=nudge)


def jcall(svc, name, args=None):
    return json.loads(call(svc, name, args))


class TestToolRegistration:
    def test_seven_tools(self):
        tl = list_tools_definitions()
        names = [t.name for t in tl]
        assert names == ["search_memory", "save_memory", "list_memories", "delete_memory",
                         "delete_memories_by_session", "repair_memory", "recall_thread"]
        for t in tl:
            assert t.description
            assert t.inputSchema["type"] == "object"
        sm = {t.name: t for t in tl}["search_memory"].inputSchema
        assert sm["required"] == ["query"] and sm["properties"]["limit"]["maximum"] == 100


class TestSaveAndSearch:
    def test_round_trip(self, svc, db):
        r = jcall(svc, "save_memory", {"content": "The project budget is 100 dollars."})
        assert r["status"] == "saved" and r["ttl_seconds"] == 604800
        out = call(svc, "search_memory", {"query": "project budget"})
        assert "The project budget is 100 dollars." in out and "saved=" in out

    def test_exact_duplicate(self, svc, db):
        jcall(svc, "save_memory", {"content": "dup me"})
        assert jcall(svc, "save_memory", {"content": "dup me"})["status"] == "duplicate"

    def test_empty_content(self, svc, db):
        assert jcall(svc, "save_memory", {"content": "   "})["reason"] == "empty content"
        r = jcall(svc, "save_memory", {"content": "\x00​"})
        assert r["reason"] == "empty content (no visible characters)"

    def test_code_blocks_are_purified(self, svc, db):
        jcall(svc, "save_memory", {"content": "before\n```py\nsecret = 1\n```\nafter"})
        out = call(svc, "list_memories")
        assert "[code omitted]" in out and "secret" not in out

    def test_importance_validation(self, svc, db):
        r = jcall(svc, "save_memory", {"content": "x1", "importance": "nan"})
        assert r["saved"] is False and r["reason"] == "importance must be numeric (got nan)"
        assert jcall(svc, "save_memory", {"content": "x2", "importance": 9})["saved"] is True

    def test_owner_mismatch(self, svc, db):
        r = jcall(svc, "save_memory", {"content": "x", "owner_id": "other"})
        assert r["reason"] == "owner_id mismatch"

    def test_skip_code_blocks_policy(self, svc, db):
        svc.cfg["skip_code_blocks"] = True
        assert jcall(svc, "save_memory", {"content": "a ```b``` c"})["status"] == "skipped_code"

    def test_nudges(self, svc, db):
        assert call(svc, "save_memory", {"content": "nudge me"}, nudge=True).endswith(_NUDGE_AFTER_SAVE)
        assert call(svc, "search_memory", {"query": "nudge"}, nudge=True).endswith(_NUDGE_AFTER_SEARCH)
        assert call(svc, "list_memories", nudge=True).endswith(_NUDGE_GENERIC)


class TestListAndDelete:
    def test_list_newest_first(self, svc, db):
        for i in range(3):
            jcall(svc, "save_memory", {"content": "entry number %d" % i})
        out = call(svc, "list_memories", {"limit": 3})
        assert out.index("entry number 2") < out.index("entry number 0")
        assert out.count("---") == 2

    def test_list_empty(self, svc, db):
        assert call(svc, "list_memories") == "_No memories stored._"

    def test_delete_missing(self, svc, db):
        assert jcall(svc, "delete_memory", {"id": "00000000"})["status"] == "not_found"


class TestRepair:
    def test_repair_empty_db(self, svc, db):
        r = jcall(svc, "repair_memory")
        assert r == {"status": "ok", "message": "index ensured", "reembedded": 0, "remaining": 0}


class TestUnknownTool:
    def test_unknown(self, svc, db):
        assert call(svc, "nope", nudge=True) == "Error: unknown tool 'nope'" + _NUDGE_GENERIC


SHIRANUI = ("浮遊都市・不知火は、雲海の上に浮かぶ円環状の都市である。人口は十二万人、中央には「天蓋の塔」が立つ。\n\n"
            "第一章 成り立ち。不知火は旧世界の崩壊後、七つの浮遊機関によって空へ押し上げられた。\n\n"
            "第二章 統治。都市は三人の評議長によって運営され、決議は鐘の音で告げられる。\n\n"
            "第三章 産業。雲を濾して得られる「霧鋼」が主要な輸出品であり、職人街で加工される。\n\n"
            "第四章 禁忌。塔の最下層には立ち入ってはならない。そこには初代評議長の遺した扉がある。") * 2


class TestParentDocRecall:
    def test_verbatim_recall_and_doc_tag(self, svc, db):
        r = jcall(svc, "save_memory", {"content": SHIRANUI, "turn_id": "t-doc"})
        assert r["chunks"] >= 2
        out = call(svc, "search_memory", {"query": "不知火 霧鋼 評議長"})
        assert "[doc×%d]" % r["chunks"] in out
        assert SHIRANUI.strip() in out
        assert out.count("### [") == 1       # chunks collapse into one parent hit

    def test_cascade_delete(self, svc, db):
        r = jcall(svc, "save_memory", {"content": SHIRANUI})
        d = jcall(svc, "delete_memory", {"id": r["parent_id"]})
        assert d["status"] == "deleted"
        assert call(svc, "list_memories") == "_No memories stored._"
        assert db.r.keys("mem:*") == [] and db.r.keys("doc:*") == []


class TestEncodingSafetyE2E:
    def test_surrogates_in_content(self, svc, db):
        r = jcall(svc, "save_memory", {"content": "ok\ud800 text\udfff"})
        assert r["saved"] is True
        assert "ok text" in call(svc, "list_memories")

    def test_all_surrogate_content(self, svc, db):
        assert jcall(svc, "save_memory", {"content": "\ud800\ud801"})["reason"] == "empty content"

    def test_surrogate_query(self, svc, db):
        jcall(svc, "save_memory", {"content": "findable text"})
        assert "findable text" in call(svc, "search_memory", {"query": "findable\ud800 text"})


class TestRecallThread:
    def test_missing_turn(self, svc, db):
        assert jcall(svc, "recall_thread", {"turn_id": "nope"}) == {"status": "not_found", "turn_id": "nope"}
        assert jcall(svc, "recall_thread", {"turn_id": ""})["reason"] == "turn_id required"

    def test_thread_with_neighbours(self, svc, db):
        jcall(svc, "save_memory", {"content": "earlier entry one", "turn_id": "a"})
        jcall(svc, "save_memory", {"content": "the target entry", "turn_id": "b"})
        jcall(svc, "save_memory", {"content": "later entry three", "turn_id": "c"})
        out = call(svc, "recall_thread", {"turn_id": "b", "before": 1, "after": 1})
        assert "earlier entry one" in out and "later entry three" in out
        assert "▶ **" in out and "  **" in out


class TestDeleteBySession:
    def test_mixed_forms_then_not_found(self, svc, db):
        sid = "eval-cleanup"
        jcall(svc, "save_memory", {"content": "はい", "session_id": sid})
        jcall(svc, "save_memory", {"content": "a medium sized fact about the system", "session_id": sid})
        jcall(svc, "save_memory", {"content": SHIRANUI, "session_id": sid})
        jcall(svc, "save_memory", {"content": "kept elsewhere", "session_id": "other"})
        r = jcall(svc, "delete_memories_by_session", {"session_id": sid})
        assert r["status"] == "deleted" and r["documents_deleted"] == 1 and r["singles_deleted"] == 2
        assert r["deleted"] == r["documents_deleted"] + r["chunks_deleted"] + r["singles_deleted"]
        assert "kept elsewhere" in call(svc, "list_memories")
        assert jcall(svc, "delete_memories_by_session", {"session_id": sid}) == {
            "status": "not_found", "session_id": sid, "deleted": 0}


class TestTimeParsing:
    def test_since_errors(self, svc, db):
        out = call(svc, "search_memory", {"query": "x", "since": "yesterday"})
        assert out.startswith("Error: since must be an ISO 8601 date or datetime")
        assert call(svc, "search_memory", {"query": "x", "since": "2026-11-02", "until": "2026-11-01"}) \
            == "Error: since is later than until"

    def test_parse_helpers(self):
        assert tools.parse_time("", False, "since") is None
        assert tools.parse_time("2026-11-13T09:00:00+09:00", False, "since") == tools.parse_time("2026-11-13T00:00:00Z", False, "since")
        assert tools.parse_time("2026-11-13", True, "until") > tools.parse_time("2026-11-13", False, "since")
