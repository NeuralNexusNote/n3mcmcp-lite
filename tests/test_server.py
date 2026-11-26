"""Layer 3: the tool handlers (tools.py, run as /tools/<name> in the resident server) (spec §9 Layer 3)."""
import pytest

from conftest import parse_json
from n3mc_mcp.tooldefs import TOOLS
from n3mc_mcp.tools import _NUDGE_AFTER_SAVE, _NUDGE_AFTER_SEARCH, _NUDGE_GENERIC

SHIRANUI = """浮遊都市・不知火（架空の設定資料）

第一章 地理: 不知火は雲海の上に浮かぶ環状の都市で、直径はおよそ十二キロメートル。中心には「灯台塔」と呼ばれる白い塔が立ち、夜ごと青い光を放って周囲の浮島に航路を知らせる。

第二章 住民: 住民は約四万人。空の漁師「雲引き」、浮力石を磨く「石守」、記録を残す「頁守」の三つの職能集団が議会を構成する。議長は毎年、満月の夜にくじで選ばれる。

第三章 技術: 都市を支えるのは浮力石と呼ばれる鉱物で、月の満ち欠けに合わせて浮力が二割ほど変動する。そのため新月の前夜には全住民が重い荷を下層に移す「沈めの儀」を行う。

第四章 禁忌: 灯台塔の最上階には誰も入ってはならない。入った者は記憶を一つ失うと伝えられている。

第五章 暦: 不知火の一年は十三の月からなり、各月は浮力石の色の名で呼ばれる。祭りは年に三度あり、最も大きいのは雲海が最も低くなる「底凪の祭」である。

第六章 交易: 地上との交易は年に二回、気球便で行われる。主な輸出品は磨いた浮力石と雲引きが獲る空魚の干物、輸入品は鉄と小麦である。"""


class TestToolRegistration:
    def test_seven_tools(self):
        names = [t.name for t in TOOLS]
        assert names == ["search_memory", "save_memory", "list_memories", "delete_memory",
                         "delete_memories_by_session", "repair_memory", "recall_thread"]

    def test_schema_types(self):
        for t in TOOLS:
            assert t.inputSchema["type"] == "object"

    def test_descriptions(self):
        assert all(t.description for t in TOOLS)


class TestSaveAndSearch:
    def test_roundtrip(self, runner):
        r = parse_json(runner.call("save_memory", {"content": "坂本龍馬は薩長同盟を仲介した"}))
        assert r["saved"] is True and r["ttl_seconds"] == 604800
        text = runner.call("search_memory", {"query": "坂本龍馬"})
        assert "坂本龍馬は薩長同盟を仲介した" in text
        assert text.endswith(_NUDGE_AFTER_SEARCH)

    def test_save_reminder(self, runner):
        assert runner.call("save_memory", {"content": "reminder check"}).endswith(_NUDGE_AFTER_SAVE)

    def test_exact_duplicate(self, runner):
        runner.call("save_memory", {"content": "dup"})
        assert parse_json(runner.call("save_memory", {"content": "dup"}))["status"] == "duplicate"

    def test_empty(self, runner):
        r = parse_json(runner.call("save_memory", {"content": "   "}))
        assert r == {"status": "error", "saved": False, "reason": "empty content"}

    def test_invisible(self, runner):
        r = parse_json(runner.call("save_memory", {"content": "​"}))
        assert r["reason"] == "empty content (no visible characters)"

    def test_code_block_purified(self, runner, rconn):
        r = parse_json(runner.call("save_memory", {"content": "before\n```\ncode\n```\nafter"}))
        assert rconn.hget("mem:" + r["id"], "content").decode() == "before\n[code omitted]\nafter"


class TestDeleteBySession:
    def test_counts_and_cleanup(self, runner, rconn):
        long = parse_json(runner.call("save_memory", {"content": "session doc sentence. " * 40, "session_id": "S1"}))
        runner.call("save_memory", {"content": "single one", "session_id": "S1"})
        runner.call("save_memory", {"content": "single two", "session_id": "S1"})
        keep = parse_json(runner.call("save_memory", {"content": "other session", "session_id": "S2"}))
        r = parse_json(runner.call("delete_memories_by_session", {"session_id": "S1"}))
        assert r == {"status": "deleted", "session_id": "S1", "documents_deleted": 1,
                     "chunks_deleted": long["chunks"], "singles_deleted": 2, "deleted": 3 + long["chunks"]}
        assert rconn.exists("mem:" + keep["id"])
        assert not list(rconn.scan_iter("doc:*")) and not list(rconn.scan_iter("docsha:*"))
        assert parse_json(runner.call("save_memory", {"content": "single one", "session_id": "S1"}))["saved"]
        again = parse_json(runner.call("delete_memories_by_session", {"session_id": "S1"}))
        assert again["status"] == "deleted"
        third = parse_json(runner.call("delete_memories_by_session", {"session_id": "S1"}))
        assert third == {"status": "not_found", "session_id": "S1", "deleted": 0}

    def test_blank(self, runner):
        r = parse_json(runner.call("delete_memories_by_session", {"session_id": "  "}))
        assert r == {"status": "error", "reason": "session_id required"}

    def test_guard_of_other_record_kept(self, runner, db, rconn):
        a = parse_json(runner.call("save_memory", {"content": "shared text", "session_id": "KEEP"}))
        guard = [k for k in rconn.scan_iter("mem:sha:*")][0]
        rconn.hset("mem:fake-record", mapping={"owner_id": db.cfg["owner_id"], "content": "shared text",
                                              "session_id": "GONE", "parent_id": "", "turn_id": ""})
        runner.call("delete_memories_by_session", {"session_id": "GONE"})
        assert rconn.get(guard).decode() == a["id"]


class TestOwnerMismatch:
    def test_mismatch(self, runner):
        r = parse_json(runner.call("save_memory", {"content": "x", "owner_id": "not-me"}))
        assert r == {"status": "error", "saved": False, "reason": "owner_id mismatch"}


class TestListAndDelete:
    def test_list_newest_first(self, runner):
        for i in range(3):
            runner.call("save_memory", {"content": f"entry {i}"})
        text = runner.call("list_memories", {}, nudge=False)
        assert text.index("entry 2") < text.index("entry 1") < text.index("entry 0")

    def test_delete_missing(self, runner):
        r = parse_json(runner.call("delete_memory", {"id": "nope"}))
        assert r["status"] == "not_found"

    def test_empty_list(self, runner):
        assert runner.call("list_memories", {}, nudge=False) == "_No memories stored._"


class TestRepair:
    def test_empty_db(self, runner):
        r = parse_json(runner.call("repair_memory", {}))
        assert r == {"status": "ok", "message": "index ensured", "reembedded": 0, "remaining": 0}


class TestUnknownTool:
    def test_unknown(self, runner):
        assert runner.call("nope", {}) == "Error: unknown tool 'nope'" + _NUDGE_GENERIC


class TestParentDocRecall:
    def test_verbatim(self, runner, rconn):
        r = parse_json(runner.call("save_memory", {"content": SHIRANUI}))
        assert r["chunks"] >= 2
        text = runner.call("search_memory", {"query": "浮力石 沈めの儀"}, nudge=False)
        assert SHIRANUI in text
        assert f"[doc×{r['chunks']}]" in text
        assert text.count(f"[{r['parent_id']}]") == 1

    def test_cascade(self, runner, rconn):
        r = parse_json(runner.call("save_memory", {"content": SHIRANUI}))
        runner.call("delete_memory", {"id": r["parent_id"]})
        assert not any(rconn.exists("mem:" + c) for c in r["ids"])


class TestEncodingSafetyE2E:
    def test_save_with_surrogate(self, runner):
        r = parse_json(runner.call("save_memory", {"content": "abc" + chr(0xD800) + "def"}))
        assert r["saved"] is True

    def test_all_surrogates(self, runner):
        r = parse_json(runner.call("save_memory", {"content": chr(0xD800) + chr(0xDFFF)}))
        assert r["reason"] == "empty content"

    def test_search_with_surrogate(self, runner):
        text = runner.call("search_memory", {"query": "x" + chr(0xDC80)}, nudge=False)
        assert not text.startswith("Error")
