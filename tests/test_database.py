"""Layer 1: Redis / RediSearch (CRUD, schema, TTL, dedup, BM25, KNN) (spec §9 Layer 1). Needs the test Redis."""
import time

import pytest

from n3mc_mcp import database as D
from n3mc_mcp.processor import cjk_bigram_expand


def _keys(r, pattern="*"):
    return {k.decode() for k in r.scan_iter(match=pattern) if k != b"__n3mc_test_db__"}


class TestIndexSetup:
    def test_index_created(self, db, rconn):
        info = rconn.execute_command("FT.INFO", D.INDEX_NAME)
        assert info

    def test_idempotent(self, db):
        assert db.ensure_index() is True
        assert db.ensure_index() is True

    def test_has_content_fts(self, db):
        assert "content_fts" in db._index_info()


class TestInsertAndRetrieve:
    def test_insert_and_count(self, db, rconn):
        db.save_memory("first entry for the count")
        db.save_memory("second entry for the count")
        assert len(_keys(rconn, "mem:????????-*")) == 2

    def test_hash_fields(self, db, rconn):
        r = db.save_memory("hash field check", agent_name="a1", session_id="s1", turn_id="t1", importance=1.5)
        h = rconn.hgetall("mem:" + r["id"])
        assert h[b"content"] == "hash field check".encode()
        assert h[b"agent_name"] == b"a1" and h[b"session_id"] == b"s1" and h[b"turn_id"] == b"t1"
        assert float(h[b"importance"]) == 1.5
        assert h[b"parent_id"] == b""
        assert len(h[b"embedding"]) == 768 * 4

    def test_insert_without_embedding(self, db, rconn):
        r = db.save_memory("no vector here", embed=False)
        assert b"embedding" not in rconn.hgetall("mem:" + r["id"])

    def test_parent_id_stored(self, db, rconn):
        r = db.save_memory("long body sentence. " * 40)
        for cid in r["ids"]:
            assert rconn.hget("mem:" + cid, "parent_id").decode() == r["parent_id"]


class TestDedup:
    def test_exact_duplicate(self, db):
        assert db.save_memory("same text")["status"] == "saved"
        assert db.save_memory("same text") == {"status": "duplicate", "saved": False}

    def test_nfkc_duplicate(self, db):
        db.save_memory("ＡＩ 2026")
        assert db.save_memory("AI 2026")["status"] == "duplicate"

    def test_parent_guard(self, db):
        body = "a long parent body sentence. " * 30
        r1 = db.save_memory(body)
        r2 = db.save_memory(body)
        assert r2["status"] == "duplicate" and r2["parent_id"] == r1["parent_id"]

    def test_similar_is_not_rejected(self, db):
        assert db.save_memory("The budget is 100 dollars.")["saved"]
        assert db.save_memory("The budget is 120 dollars.")["saved"]


class TestDelete:
    def test_single(self, db, rconn):
        r = db.save_memory("to delete")
        assert db.delete_memory(r["id"])["status"] == "deleted"
        assert not rconn.exists("mem:" + r["id"])
        assert db.save_memory("to delete")["status"] == "saved"

    def test_parent_cascade(self, db, rconn):
        r = db.save_memory("cascade body sentence. " * 40)
        out = db.delete_memory(r["parent_id"])
        assert out["status"] == "deleted"
        assert not rconn.exists("doc:" + r["parent_id"])
        for cid in r["ids"]:
            assert not rconn.exists("mem:" + cid)
        assert not _keys(rconn, "docsha:*")

    def test_chunk_delete_keeps_independent_guard(self, db, rconn):
        body = ("Chunk sentence number one. " * 30)
        r = db.save_memory(body)
        first_chunk = rconn.hget("mem:" + r["ids"][0], "content").decode()
        single = db.save_memory(first_chunk)
        assert single["status"] == "saved"
        db.delete_memory(r["ids"][0])
        assert db.save_memory(first_chunk)["status"] == "duplicate"
        db.delete_memory(single["id"])
        assert db.save_memory(first_chunk)["status"] == "saved"

    def test_not_found(self, db):
        assert db.delete_memory("00000000-0000-0000-0000-000000000000")["status"] == "not_found"


class TestTTL:
    def test_ttl_set(self, db, rconn):
        r = db.save_memory("ttl check")
        assert 604700 < rconn.ttl("mem:" + r["id"]) <= 604800
        assert all(rconn.ttl(k) > 0 for k in rconn.scan_iter("mem:sha:*"))

    def test_search_does_not_change_ttl(self, db, rconn):
        r = db.save_memory("ttl search keyword alpha")
        rconn.expire("mem:" + r["id"], 1000)
        db.search("ttl search keyword alpha")
        assert rconn.ttl("mem:" + r["id"]) <= 1000

    def test_expires(self, db, rconn, cfg):
        cfg["ttl_seconds"] = 1
        r = db.save_memory("short lived")
        time.sleep(2.1)
        assert not rconn.exists("mem:" + r["id"])


class TestFTS:
    def test_punctuation_removed(self, db):
        db.save_memory("Hello, world! punctuation test.")
        rows = db._bm25_search("world", 10)
        assert rows and rows[0]["_bm25"] > 0

    def test_bm25_scores_differ(self, db):
        db.save_memory("apple orange banana")
        db.save_memory("apple apple apple apple apple")
        rows = db._bm25_search("apple", 10)
        scores = sorted(r["_bm25"] for r in rows)
        assert len(scores) == 2 and scores[0] < scores[1]

    def test_short_query(self, db):
        db.save_memory("ok")
        assert db._bm25_search("ok", 10)

    def test_cjk(self, db):
        db.save_memory("記憶装置の設計について話した")
        assert db._bm25_search("記憶装置", 10)


class TestVectorSearch:
    def test_knn(self, db):
        from n3mc_mcp import processor
        db.save_memory("vector search target text")
        rows = db._vector_search(processor.embed("vector search target text", "query"), 10)
        assert rows and rows[0]["_cos"] > 0.99

    def test_empty_db(self, db):
        from n3mc_mcp import processor
        assert db._vector_search(processor.embed("anything", "query"), 10) == []

    def test_owner_filter(self, db, cfg):
        from n3mc_mcp import processor
        other = dict(cfg)
        other["owner_id"] = "someone-else"
        D.Database(other).ensure_connected()
        d2 = D.Database(other)
        d2.ensure_connected()
        d2.save_memory("owner filter text")
        assert db._vector_search(processor.embed("owner filter text", "query"), 10) == []

    def test_list_only_independent(self, db):
        db.save_memory("independent one")
        db.save_memory("parent body sentence. " * 30)
        rows = db.list_memories(20)
        assert len(rows) == 2
        assert sum(1 for r in rows if r.get("_doc")) == 1


class TestCjkBigramExpand:
    def test_mixed(self):
        assert cjk_bigram_expand("記憶装置") == "記憶 憶装 装置"

    def test_kana(self):
        assert cjk_bigram_expand("カタカナ") == "カタ タカ カナ"

    def test_boundary(self):
        assert cjk_bigram_expand("AI記憶") == "AI 記憶"

    def test_single(self):
        assert cjk_bigram_expand("記") == "記"


class TestSearchWritesNothing:
    """A search writes nothing to a record: no hit counter, so RediSearch never indexes a hit again and
    the BM25 of a record that is found often does not fall."""

    def _hashes(self, rconn):
        out = {}
        for k in rconn.scan_iter("*"):
            if rconn.type(k) == b"hash":
                out[k] = rconn.hgetall(k)
        return out

    def test_records_unchanged(self, db, rconn, cfg):
        # settings of earlier versions, left in an existing config.json, are ignored
        cfg.update({"access_count_enabled": True, "access_count_weight": 0.5, "ttl_refresh_top_k": 5})
        db.save_memory("search writes nothing target words")
        db.save_memory("search writes nothing long sentence. " * 40)
        before = self._hashes(rconn)
        for _ in range(5):
            db.search("search writes nothing target words")
            db.search("search writes nothing long sentence")
        assert self._hashes(rconn) == before

    def test_no_reindex_and_bm25_stable(self, db, rconn):
        for i in range(3):
            db.save_memory(f"the nightly backup job number {i} runs at night")
        for i in range(30):
            db.save_memory(f"unrelated note {i} about fonts and colours")

        def info():
            raw = rconn.execute_command("FT.INFO", "n3mc_idx")
            return dict(zip(raw[::2], raw[1::2])).get(b"max_doc_id")

        scores = sorted(round(r["_bm25"], 6) for r in db._bm25_search("backup", 100))
        doc_id = info()
        for _ in range(30):
            db.search("nightly backup")
        assert info() == doc_id
        assert sorted(round(r["_bm25"], 6) for r in db._bm25_search("backup", 100)) == scores


class TestSerialization:
    def test_roundtrip(self):
        v = [0.5, -1.25, 3.0]
        assert D.unpack_vector(D.pack_vector(v)) == v

    def test_little_endian(self):
        assert D.pack_vector([1.0]) == b"\x00\x00\x80?"


class TestSha1:
    def test_hex(self):
        assert D.sha1_hex("abc") == "a9993e364706816aba3e25717850c26c9cd0d89d"

    def test_empty(self):
        assert D.sha1_hex("") == "da39a3ee5e6b4b0d3255bfef95601890afd80709"

    def test_non_ascii(self):
        assert len(D.sha1_hex("記憶")) == 40

    def test_long(self):
        assert len(D.sha1_hex("x" * 100000)) == 40


class TestFinalScoreProduct:
    def test_factors(self, db, cfg, monkeypatch):
        cfg["lexical_rerank_enabled"] = False
        cfg["relative_min_score_ratio"] = 0.0
        cfg["min_score"] = 0.0
        cfg["half_life_days"] = 3
        monkeypatch.setattr(D.processor, "keyword_relevance", lambda *a, **k: 0.5)
        r = db.save_memory("product factor text", session_id="S")
        normal, _w, _m = db.search("product factor text", session_id="S")
        base = normal[0]["score"]
        assert base == pytest.approx((1.0 * 0.7 + 0.5 * 0.3), abs=0.01)
        normal2, _w, _m = db.search("product factor text", session_id="other")
        assert normal2[0]["score"] == pytest.approx(base * 0.6, rel=1e-3)
        db.client.hset("mem:" + r["id"], "importance", "2.0")
        normal3, _w, _m = db.search("product factor text", session_id="S")
        assert normal3[0]["score"] == pytest.approx(base * 2.0, rel=1e-3)
        old = time.time() - 3 * 86400
        db.client.hset("mem:" + r["id"], mapping={"importance": "1.0", "timestamp_epoch": repr(old)})
        normal4, _w, _m = db.search("product factor text", session_id="S")
        assert normal4[0]["score"] == pytest.approx(base * 0.5, rel=1e-3)
