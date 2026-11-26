"""Layer 3: the tool handlers end to end (tools.py, as run by /tools/<name>), isolated config.json and
a flushed test DB 0."""
import pytest

from conftest import parse_json
from n3mc_mcp.tooldefs import TOOLS

SHIRANUI = """浮遊都市・不知火（架空の設定資料）

第一章　地理：不知火は雲海の上、高度三千二百メートルに浮かぶ環状の都市である。直径は十四キロ、外周には「霧の港」と呼ばれる七つの浮遊桟橋が並ぶ。
第二章　動力：都市を支えるのは中央塔に収められた「蒼炎炉」。燃料は月に一度だけ採れる結晶「燐光石」で、炉守と呼ばれる職人が三交代で管理する。
第三章　社会：住民はおよそ八万人。評議会は九名で構成され、議長は「灯主」と呼ばれる。通貨は「雫」で、百雫が一燐に当たる。
第四章　禁忌：夜明け前に鐘を三度鳴らすことは禁じられている。破った者は外周の桟橋で一年間の見張り役を務める。
第五章　年表：建都三百年祭は来年の秋分に予定されており、記念に新しい桟橋「八番港」が開かれる。
第六章　気候：雲海の上にあるため雨は降らず、水は朝霧を集める「霧網」から得る。霧網は外周に二百四十枚張られ、乾季には半数が畳まれる。
第七章　交通：環状の街路を「風車馬車」が巡り、中央塔へは三本の吊り橋で渡る。吊り橋の名は東から順に暁橋、凪橋、宵橋である。
第八章　祭礼：毎年冬至の夜、住民は燐光石の欠片を灯籠に入れて雲海へ流す。灯籠が見えなくなるまで誰も口をきかないのが習わしである。"""

assert len(SHIRANUI) > 400


def _hits(text):
    return [line for line in text.splitlines() if line.startswith("### ")]


class TestToolRegistration:
    def test_seven_tools(self):
        names = [t.name for t in TOOLS]
        assert len(names) == 7 and "recall_thread" in names

    def test_schemas_are_objects(self):
        assert all(t.inputSchema["type"] == "object" for t in TOOLS)

    def test_descriptions_not_empty(self):
        assert all(t.description.strip() for t in TOOLS)


class TestSaveAndSearch:
    def test_roundtrip(self, runner):
        r = parse_json(runner.call("save_memory", {"content": "坂本龍馬は薩長同盟を仲介した。"}))
        assert r["saved"] is True
        out = runner.call("search_memory", {"query": "坂本龍馬"})
        assert "坂本龍馬は薩長同盟を仲介した。" in out and r["id"] in out

    def test_exact_duplicate(self, runner):
        runner.call("save_memory", {"content": "dup me"})
        assert parse_json(runner.call("save_memory", {"content": "dup me"}))["status"] == "duplicate"

    def test_empty_content(self, runner):
        r = parse_json(runner.call("save_memory", {"content": "   "}))
        assert r == {"status": "error", "saved": False, "reason": "empty content"}

    def test_code_block_purified(self, runner, rconn):
        r = parse_json(runner.call("save_memory", {"content": "before\n```py\nsecret()\n```\nafter"}))
        assert rconn.hget("mem:" + r["id"], "content") == "before\n[code omitted]\nafter".encode()


class TestListAndDelete:
    def test_list_newest_three(self, runner):
        for i in range(3):
            runner.call("save_memory", {"content": f"list entry {i}"})
        out = runner.call("list_memories", {"limit": 3}, nudge=False)
        assert out.count("**[") == 3 and out.index("list entry 2") < out.index("list entry 0")

    def test_delete_unknown_id(self, runner):
        r = parse_json(runner.call("delete_memory", {"id": "00000000-0000-7000-8000-000000000000"}))
        assert r["status"] == "not_found"

    def test_delete_existing(self, runner):
        r = parse_json(runner.call("save_memory", {"content": "delete me now"}))
        assert parse_json(runner.call("delete_memory", {"id": r["id"]}))["status"] == "deleted"
        assert "delete me now" not in runner.call("list_memories", {}, nudge=False)


class TestRepair:
    def test_repair_empty_db(self, runner):
        r = parse_json(runner.call("repair_memory", {}))
        assert r["status"] == "ok" and r["message"] == "index ensured"


class TestUnknownTool:
    def test_unknown(self, runner):
        assert runner.call("nope", {}).startswith("Error: unknown tool 'nope'")


class TestParentDocRecall:
    def test_verbatim_restore(self, runner):
        parse_json(runner.call("save_memory", {"content": SHIRANUI}))
        out = runner.call("search_memory", {"query": "不知火 蒼炎炉 燐光石"})
        assert SHIRANUI in out

    def test_chunks_fold_into_one_parent(self, runner):
        r = parse_json(runner.call("save_memory", {"content": SHIRANUI}))
        out = runner.call("search_memory", {"query": "不知火 桟橋 評議会 雫"})
        assert sum(1 for h in _hits(out) if r["parent_id"] in h) == 1

    def test_doc_tag(self, runner):
        r = parse_json(runner.call("save_memory", {"content": SHIRANUI}))
        out = runner.call("search_memory", {"query": "不知火"})
        assert f"[doc×{r['chunks']}] [{r['parent_id']}]" in out
        assert f"[doc×{r['chunks']}] [{r['parent_id']}]" in runner.call("list_memories", {})

    def test_cascade_delete(self, runner, rconn):
        r = parse_json(runner.call("save_memory", {"content": SHIRANUI}))
        parse_json(runner.call("delete_memory", {"id": r["parent_id"]}))
        assert not [k for k in rconn.keys("mem:*") if not k.startswith(b"mem:sha:")]


class TestEncodingSafetyE2E:
    def test_lone_surrogate_save(self, runner):
        r = parse_json(runner.call("save_memory", {"content": "壊れた" + chr(0xD800) + "文字"}))
        assert r["saved"] is True

    def test_all_surrogate_save(self, runner):
        r = parse_json(runner.call("save_memory", {"content": chr(0xD800) + chr(0xDFFF)}))
        assert r["reason"] == "empty content"

    def test_lone_surrogate_query(self, runner):
        runner.call("save_memory", {"content": "サロゲートの検索"})
        out = runner.call("search_memory", {"query": "サロゲート" + chr(0xDC80)})
        assert not out.startswith("Error") and "サロゲートの検索" in out
