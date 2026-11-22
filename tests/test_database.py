"""Layer 1: Redis / RediSearch unit tests (needs N3MC_REDIS_TEST_URL)."""

import time

import pytest

from n3mc_mcp import database, processor
from n3mc_mcp.database import DOC, DOC_SHA, MEM, MEM_SHA, guard_key_for


def ttl_ok(r, key):
    t = r.ttl(key)
    return 0 < t <= 604800


def hget(r, key, field):
    v = r.hget(key, field)
    return v.decode("utf-8") if isinstance(v, bytes) else v


class TestIndexSetup:
    def test_index_created_and_idempotent(self, db):
        db.ensure_index()
        db.ensure_index()
        assert db.index_error is None
        info = db.r.execute_command("FT.INFO", database.INDEX)
        assert info

    def test_dim_mismatch_notice(self, db):
        db.cfg["embedding_dim"] = 384
        db.cfg["_notices"].clear()
        db.ensure_index()
        assert any(n.startswith("index_dim_mismatch") for n in db.cfg["_notices"])


class TestInsertAndRetrieve:
    def test_single_record_fields_and_ttl(self, db):
        res = db.save_memory("hello world note", agent_name="a", session_id="s", turn_id="t")
        key = MEM + res["id"]
        assert hget(db.r, key, "content") == "hello world note"
        assert hget(db.r, key, "owner_id") == db.cfg["owner_id"]
        assert hget(db.r, key, "parent_id") == ""
        assert db.r.hget(key, "embedding") is not None and len(db.r.hget(key, "embedding")) == 768 * 4
        assert ttl_ok(db.r, key)

    def test_without_embedding(self, db):
        res = db.save_memory("no vector here", embed=False)
        assert db.r.hget(MEM + res["id"], "embedding") is None
        assert res["warning"] == "embedding_unavailable"

    def test_parent_and_chunks(self, db):
        text = "段落です。" * 200
        res = db.save_memory(text)
        assert res["chunks"] >= 2
        pid = res["parent_id"]
        assert hget(db.r, DOC + pid, "content") == text
        assert hget(db.r, DOC + pid, "chunk_ids").split() == res["ids"]
        for cid in res["ids"]:
            assert hget(db.r, MEM + cid, "parent_id") == pid
            assert ttl_ok(db.r, MEM + cid)
        assert ttl_ok(db.r, DOC + pid)


class TestDedup:
    def test_exact_duplicate(self, db):
        assert db.save_memory("same text")["saved"]
        assert db.save_memory("same text") == {"status": "duplicate", "saved": False}

    def test_nfkc_duplicate(self, db):
        assert db.save_memory("ＡＩ memory")["saved"]
        assert db.save_memory("AI memory")["status"] == "duplicate"

    def test_parent_guard(self, db):
        text = "long long text. " * 60
        first = db.save_memory(text)
        second = db.save_memory(text)
        assert second["status"] == "duplicate" and second["parent_id"] == first["parent_id"]
        assert db.r.exists(DOC_SHA + processor.sha1_hex(text.strip()))

    def test_guard_has_ttl(self, db):
        db.save_memory("guarded")
        assert ttl_ok(db.r, MEM_SHA + processor.sha1_hex("guarded"))


class TestDelete:
    def test_single(self, db):
        res = db.save_memory("to delete")
        out = db.delete_memory(res["id"])
        assert out["status"] == "deleted"
        assert not db.r.exists(MEM + res["id"])
        assert not db.r.exists(MEM_SHA + processor.sha1_hex("to delete"))

    def test_parent_cascade(self, db):
        res = db.save_memory("cascade text. " * 80)
        out = db.delete_memory(res["parent_id"])
        assert out["status"] == "deleted"
        assert not db.r.exists(DOC + res["parent_id"])
        assert all(not db.r.exists(MEM + c) for c in res["ids"])
        assert not db.r.exists(DOC_SHA + processor.sha1_hex(("cascade text. " * 80).strip()))

    def test_not_found(self, db):
        assert db.delete_memory("nope")["status"] == "not_found"

    def test_prefixed_ids(self, db):
        res = db.save_memory("prefix delete")
        assert db.delete_memory("mem:" + res["id"])["id"] == res["id"]


class TestTTL:
    def test_refresh_on_search(self, db):
        res = db.save_memory("ttl refresh target text", session_id="s")
        db.r.expire(MEM + res["id"], 100)
        rows, _, _ = db.search("ttl refresh target text", "s", 5)
        assert rows
        assert db.r.ttl(MEM + res["id"]) > 1000

    def test_every_key_has_ttl(self, db):
        db.save_memory("alpha beta gamma")
        db.save_memory("long text. " * 100)
        db.search("alpha", "s", 5)
        for key in db.r.scan_iter(count=100):
            if key == b"__n3mc_test_db__":
                continue
            assert db.r.ttl(key) > 0, key


class TestFTS:
    def test_bm25_hit(self, db):
        db.save_memory("kubernetes deployment rollout notes")
        rows = db._bm25_search("kubernetes", 10, "")
        assert rows and rows[0]["bm25"] > 0

    def test_cjk_bigram_hit(self, db):
        db.save_memory("記憶装置の設計メモ")
        assert db._bm25_search("記憶装置", 10, "")

    def test_punctuation_query_does_not_raise(self, db):
        db.save_memory("plain text")
        for q in ["!!!", "\x00\x01", "「」？", "()[]{}", "_"]:
            db._bm25_search(q, 10, "")

    def test_or_semantics(self, db):
        db.save_memory("the quick fox runs")
        assert db._bm25_search("quick fox elephant", 10, "")


class TestVectorSearch:
    def test_knn(self, db, fake_embed):
        db.save_memory("vector search sample sentence")
        rows = db._vector_search(fake_embed("vector search sample sentence", db.cfg), 10, "")
        assert rows and rows[0]["cos"] > 0.99

    def test_empty_db(self, db, fake_embed):
        assert db._vector_search(fake_embed("x", db.cfg), 10, "") == []

    def test_owner_filter_in_python(self, db, fake_embed):
        db.save_memory("owner filter")
        other = dict(db.cfg, owner_id="someone-else")
        d2 = database.Database(other)
        d2.ensure_connected()
        assert d2._vector_search(fake_embed("owner filter", other), 10, "") == []


class TestAccessCount:
    def test_incremented_for_top_hits(self, db):
        res = db.save_memory("access counted memory entry")
        db.search("access counted memory entry", "s", 5)
        assert int(hget(db.r, MEM + res["id"], "access_count")) == 1

    def test_not_created_for_missing_key(self, db):
        db._scripts["incr"](keys=[MEM + "ghost"], args=["access_count", 1])
        assert not db.r.exists(MEM + "ghost")


class TestSerialization:
    def test_vector_round_trip(self):
        v = [0.5, -1.25, 3.0]
        assert processor.unpack_vector(processor.pack_vector(v)) == v
        assert len(processor.pack_vector([0.0] * 768)) == 768 * 4


class TestSha1:
    def test_guard_keys(self):
        assert guard_key_for(MEM, "abc", "") == MEM_SHA + processor.sha1_hex("abc")
        assert guard_key_for(MEM, "[user] abc", "t1") == MEM_SHA + "t1:" + processor.sha1_hex("[user] abc")
        assert guard_key_for(DOC, "[claude] abc", "t1") == DOC_SHA + "t1:" + processor.sha1_hex("[claude] abc")
        assert guard_key_for(MEM, "plain", "t1") == MEM_SHA + processor.sha1_hex("plain")
