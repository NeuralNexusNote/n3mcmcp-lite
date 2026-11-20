"""Layers 1 and 4: Redis / RediSearch behavior (needs N3MC_REDIS_TEST_URL)."""

import time

from n3mc_mcp import processor as P
from n3mc_mcp.database import INDEX, Database


def _all_have_ttl(r):
    return all(r.ttl(k) > 0 for k in r.scan_iter("*") if k != b"__n3mc_test_db__")


class TestIndexSetup:
    def test_idempotent(self, db, rconn):
        db.ensure_index()
        db.ensure_index()
        assert db.index_ok
        assert rconn.execute_command("FT.INFO", INDEX)


class TestSaveSingle:
    def test_saved_with_ttl_and_guard(self, db, rconn):
        res = db.save_memory("hello world")
        assert res["saved"] and res["ttl_seconds"] == 604800
        assert 0 < rconn.ttl("mem:" + res["id"]) <= 604800
        assert rconn.get("mem:sha:" + P.sha1_hex("hello world")).decode() == res["id"]

    def test_exact_duplicate_only(self, db):
        assert db.save_memory("The project budget is 100 dollars.")["saved"]
        assert db.save_memory("The project budget is 120 dollars.")["saved"]
        assert db.save_memory("  The project budget is 100 dollars.\n")["status"] == "duplicate"

    def test_empty_and_invisible(self, db):
        assert db.save_memory("   ")["reason"] == "empty content"
        assert db.save_memory("\x00​")["reason"] == "empty content (no visible characters)"
        assert db.save_memory(chr(0xD800) + chr(0xDC00))["reason"] == "empty content"

    def test_importance_nan_rejected(self, db):
        r = db.save_memory("abc", importance="nan")
        assert r["status"] == "error" and "importance must be numeric" in r["reason"]

    def test_code_omitted_on_save(self, db, rconn):
        r = db.save_memory("see ```py\nx=1\n``` here")
        assert rconn.hget("mem:" + r["id"], "content").decode() == "see [code omitted] here"

    def test_guard_released_on_field_failure(self, db, rconn):
        r = db.save_memory("turn int", turn_id=12)
        assert r["status"] == "error"
        assert not list(rconn.scan_iter("mem:sha:*"))
        assert db.save_memory("turn int")["saved"]


class TestLongDocument:
    TEXT = "\n\n".join(f"第{i}区画「アルファ{i}」は雲海の上に浮かぶ。住民は約{1000 + i}人。" * 3 for i in range(12))

    def test_parent_chunks_verbatim_and_cascade(self, db, rconn, runner):
        r = db.save_memory(self.TEXT, session_id="s1")
        assert r["chunks"] > 1 and len(r["ids"]) == r["chunks"]
        assert rconn.hget("doc:" + r["parent_id"], "content").decode() == self.TEXT.strip()
        out = runner.call("search_memory", {"query": "アルファ5 住民", "session_id": "s1"}, nudge=False)
        assert f"[doc×{r['chunks']}] [{r['parent_id']}]" in out
        assert self.TEXT.strip() in out
        d = db.delete_memory("doc:" + r["parent_id"])
        assert d["status"] == "deleted" and d["chunks_deleted"] == r["chunks"]
        assert not list(rconn.scan_iter("mem:*")) and not list(rconn.scan_iter("docsha:*"))

    def test_duplicate_parent(self, db):
        a = db.save_memory(self.TEXT)
        b = db.save_memory(self.TEXT)
        assert b["status"] == "duplicate" and b["parent_id"] == a["parent_id"]


class TestSearch:
    def test_cjk_punctuation_query(self, db, runner):
        db.save_memory("架空の惑星「アルファ9」の気温設定")
        for q in ("アルファ9 気温", "「アルファ9」の気温は？"):
            out = runner.call("search_memory", {"query": q}, nudge=False)
            assert "アルファ9" in out.split("#### Possibly related")[0] or "アルファ9" in out

    def test_or_bm25(self, db):
        db.save_memory("the quick fox jumps")
        terms = P.drop_lone_cjk_tokens(P.prepare_query("what did the quick fox do elephant")).split()
        assert db._bm25(terms, 10, None)

    def test_time_window_prefilter(self, db, rconn):
        r = db.save_memory("window record alpha")
        rconn.hset("mem:" + r["id"], "timestamp_epoch", str(time.time() - 3 * 86400))
        rows, weak, _ = db.search("window record alpha", since=time.time() - 86400)
        assert not rows and not weak
        rows, weak, _ = db.search("window record alpha", until=time.time() - 2 * 86400)
        assert [x["id"] for x in rows + weak] == [r["id"]]

    def test_weak_matches_do_not_refresh_ttl(self, db, rconn, cfg):
        cfg["vector_min_cos"] = 1.0
        cfg["min_score"] = 1.0
        r = db.save_memory("zebra crossing notes")
        rconn.expire("mem:" + r["id"], 1000)
        rows, weak, _ = db.search("zebra crossing notes")
        assert not rows and weak and weak[0]["_weak"]
        assert rconn.ttl("mem:" + r["id"]) <= 1000
        assert rconn.hget("mem:" + r["id"], "access_count") == b"0"

    def test_hit_refreshes_ttl_and_guard(self, db, rconn):
        r = db.save_memory("refresh me please")
        rconn.expire("mem:" + r["id"], 1000)
        rconn.expire("mem:sha:" + P.sha1_hex("refresh me please"), 1000)
        rows, _, _ = db.search("refresh me please")
        assert rows[0]["id"] == r["id"]
        assert rconn.ttl("mem:" + r["id"]) > 1000
        assert rconn.ttl("mem:sha:" + P.sha1_hex("refresh me please")) > 1000
        assert _all_have_ttl(rconn)

    def test_empty_query_adds_no_weak(self, db):
        db.save_memory("something")
        rows, weak, _ = db.search("   ")
        assert weak == []


class TestHookRecords:
    def test_role_prefix_and_turn_scoped_guard(self, db, rconn):
        a = db.save_memory("ok", role="user", turn_id="t1", session_id="cc", agent_name="claude-code")
        b = db.save_memory("ok", role="user", turn_id="t2", session_id="cc", agent_name="claude-code")
        c = db.save_memory("ok")
        d = db.save_memory("ok", role="user", turn_id="t1", session_id="cc")
        assert a["saved"] and b["saved"] and c["saved"] and d["status"] == "duplicate"
        assert rconn.hget("mem:" + a["id"], "content") == b"[user] ok"
        assert rconn.exists("mem:sha:t1:" + P.sha1_hex("[user] ok"))

    def test_long_reply_prefixes(self, db, rconn):
        body = "設定。" * 400
        r = db.save_memory(body, role="claude", turn_id="t9")
        assert rconn.hget("doc:" + r["parent_id"], "content").decode() == "[claude] " + body
        n = r["chunks"]
        first = rconn.hget("mem:" + r["ids"][0], "content").decode()
        assert first.startswith(f"[claude 1/{n}] ")


class TestDeleteOwnerAndPrefix:
    def test_other_owner_not_found(self, db, cfg):
        other = Database(dict(cfg, owner_id="someone-else"))
        other.ensure_connected()
        r = other.save_memory("not mine")
        assert db.delete_memory(r["id"])["status"] == "not_found"
        assert db.delete_memory("mem:" + r["id"])["status"] == "not_found"

    def test_prefix_forms(self, db):
        r = db.save_memory("delete me")
        assert db.delete_memory("mem:" + r["id"]) == {"status": "deleted", "id": r["id"]}
        assert db.delete_memory("doc:doc:x")["status"] == "not_found"


class TestDeleteBySession:
    def test_counts_and_idempotent(self, db):
        db.save_memory("はい", session_id="eval-x")
        db.save_memory("a medium fact about the cleanup test", session_id="eval-x")
        long = db.save_memory("長文。" * 300, session_id="eval-x")
        db.save_memory("other session", session_id="eval-y")
        res = db.delete_by_session("eval-x")
        assert res["documents_deleted"] == 1 and res["singles_deleted"] == 2
        assert res["chunks_deleted"] == long["chunks"]
        assert db.delete_by_session("eval-x") == {"status": "not_found", "session_id": "eval-x", "deleted": 0}
        assert [r["content"] for r in db.recall_rows()] == ["other session"]


class TestRepairAndRecall:
    def test_repair_reembeds(self, db, rconn):
        r = db.save_memory("no vector yet", embed=False)
        assert not rconn.hexists("mem:" + r["id"], "embedding")
        out = db.repair()
        assert out["reembedded"] == 1 and out["remaining"] == 0
        assert rconn.hexists("mem:" + r["id"], "embedding")

    def test_recall_orphan_chunks(self, db, rconn, runner):
        r = db.save_memory("孤立。" * 300, turn_id="T1")
        out = runner.call("recall_thread", {"turn_id": "T1"}, nudge=False)
        assert out.count("▶ ") == 1 and "[doc×" in out
        rconn.delete("doc:" + r["parent_id"])
        out = runner.call("recall_thread", {"turn_id": "T1"}, nudge=False)
        assert out.count("▶ ") == r["chunks"]

    def test_list_paging_after_long_doc(self, db, runner, cfg):
        db.save_memory("single before")
        db.save_memory("長い。" * 3000)
        out = runner.call("list_memories", {"limit": 20}, nudge=False)
        assert "single before" in out and "[doc×" in out
