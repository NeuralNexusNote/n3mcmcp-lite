"""Layer 3: the MCP tool handlers end to end (tools.py, isolated config.json + flushed DB 0)."""
import json

import pytest

from n3mc_mcp.tooldefs import TOOLS


def _j(text):
    return json.JSONDecoder().raw_decode(text)[0]


SHIRANUI = (
    "【浮遊都市・不知火（架空の設定資料）】\n"
    "第一節：地理。不知火は雲海の上、高度三千メートルに浮かぶ環状の都市で、直径は十二キロメートル。"
    "外周には霧を集める風車塔が百八基並び、その水が都市のすべての水源になる。\n\n"
    "第二節：政治。都市は七つの浮島区に分かれ、各区の長老が月に一度『雲上評議会』を開く。"
    "評議会の議長は抽選で選ばれ、任期は満月から次の満月まで。\n\n"
    "第三節：経済。主な産業は雲綿（うんめん）の採集と、風車塔で作る蒸留水の輸出である。"
    "通貨は『霧貨』で、一霧貨は蒸留水一リットルと交換できる。\n\n"
    "第四節：禁忌。夜明け前に外周の鐘を三度鳴らしてはならない。鳴らせば雲海の龍が目を覚ますと伝えられる。\n\n"
    "第五節：暦。不知火の一年は十三の月からなり、各月は二十八日。年の終わりの『霧明け祭』では、"
    "住民が灯籠を雲海へ流し、翌年の風を占う。祭の三日間だけは評議会も開かれない。\n\n"
    "第六節：人物。初代議長の汐見カグラは、風車塔の設計者でもあった。彼女の手記『雲上覚書』は、"
    "今も中央図書塔の最上階に収められ、年に一度だけ公開される。"
)
assert len(SHIRANUI) > 400


class TestToolRegistration:
    def test_seven_tools(self):
        names = [t.name for t in TOOLS]
        assert len(names) == 7 and "recall_thread" in names

    def test_schema_types(self):
        assert all(t.inputSchema["type"] == "object" for t in TOOLS)

    def test_descriptions(self):
        assert all(t.description for t in TOOLS)


class TestSaveAndSearch:
    def test_roundtrip(self, runner):
        assert _j(runner.call("save_memory", {"content": "The capital of Atlantis is Poseidonia."}, nudge=False))["saved"]
        out = runner.call("search_memory", {"query": "capital of Atlantis"}, nudge=False)
        assert "Poseidonia" in out

    def test_duplicate(self, runner):
        runner.call("save_memory", {"content": "dup line"}, nudge=False)
        assert _j(runner.call("save_memory", {"content": "dup line"}, nudge=False))["status"] == "duplicate"

    def test_empty(self, runner):
        assert _j(runner.call("save_memory", {"content": "   "}, nudge=False))["reason"] == "empty content"

    def test_code_block_purified(self, runner):
        runner.call("save_memory", {"content": "how to sort:\n```python\nsorted(x)\n```\ndone"}, nudge=False)
        out = runner.call("search_memory", {"query": "how to sort"}, nudge=False)
        assert "[code omitted]" in out and "sorted(x)" not in out


class TestListAndDelete:
    def test_list_newest_three(self, runner):
        for i in range(3):
            runner.call("save_memory", {"content": f"list entry number {i}"}, nudge=False)
        out = runner.call("list_memories", {"limit": 3}, nudge=False)
        assert out.index("number 2") < out.index("number 1") < out.index("number 0")

    def test_delete_missing(self, runner):
        out = runner.call("delete_memory", {"id": "no-such-id"}, nudge=False)
        assert _j(out)["status"] == "not_found"

    def test_list_empty(self, runner):
        assert runner.call("list_memories", {}, nudge=False) == "_No memories stored._"


class TestRepair:
    def test_repair_ok(self, runner):
        res = _j(runner.call("repair_memory", {}, nudge=False))
        assert res["status"] == "ok" and res["message"] == "index ensured"


class TestUnknownTool:
    def test_unknown(self, runner):
        assert runner.call("nope", {}, nudge=False) == "Error: unknown tool 'nope'"


class TestParentDocRecall:
    def test_verbatim(self, runner):
        res = _j(runner.call("save_memory", {"content": SHIRANUI}, nudge=False))
        assert res["chunks"] >= 2
        out = runner.call("search_memory", {"query": "不知火 雲上評議会"}, nudge=False)
        assert SHIRANUI in out

    def test_one_hit_per_parent(self, runner):
        res = _j(runner.call("save_memory", {"content": SHIRANUI}, nudge=False))
        out = runner.call("search_memory", {"query": "不知火 風車塔 霧貨"}, nudge=False)
        assert out.count(f"[{res['parent_id']}]") == 1

    def test_doc_tag(self, runner):
        res = _j(runner.call("save_memory", {"content": SHIRANUI}, nudge=False))
        out = runner.call("search_memory", {"query": "不知火"}, nudge=False)
        assert f"[doc×{res['chunks']}]" in out

    def test_cascade(self, runner, rconn):
        res = _j(runner.call("save_memory", {"content": SHIRANUI}, nudge=False))
        assert _j(runner.call("delete_memory", {"id": res["parent_id"]}, nudge=False))["status"] == "deleted"
        assert not any(rconn.exists("mem:" + c) for c in res["ids"])


class TestEncodingSafetyE2E:
    def test_lone_surrogate_save(self, runner):
        res = _j(runner.call("save_memory", {"content": "記憶\ud800テスト"}, nudge=False))
        assert res["saved"]

    def test_all_surrogates(self, runner):
        res = _j(runner.call("save_memory", {"content": "\udfff" + "\ud800"}, nudge=False))
        assert res["reason"] == "empty content"

    def test_query_surrogate(self, runner):
        out = runner.call("search_memory", {"query": "記憶\udc00"}, nudge=False)
        assert not out.startswith("Error")
