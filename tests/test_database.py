"""Layer 1: the Redis / RediSearch layer (spec §3.3-§3.12). Needs N3MC_REDIS_TEST_URL."""
import struct
import time

import pytest

from n3mc_mcp import processor as P
from n3mc_mcp.database import DOC, DOCSHA, INDEX, MEM, SHA, Database


def _normal(db, q, **kw):
    return db.search(q, **kw)[0]


class TestIndexSetup:
    def test_index_created(self, db, rconn):
        names, dim = db.index_info()
        assert {"content_fts", "content_ngram", "embedding", "owner_id", "parent_id"} <= names
        assert dim == 768

    def test_ensure_index_idempotent(self, db):
        db.ensure_index()
        db.ensure_index()
        assert db.index_ok

    def test_index_recreated_after_flush(self, db, rconn):
        from conftest import TEST_MARK
        rconn.flushdb()
        rconn.set(TEST_MARK, 1)
        assert db.save_memory("after flush the index comes back")["saved"]
        assert _normal(db, "after flush index")


class TestInsertAndRetrieve:
    def test_insert_count(self, db, rconn):
        db.save_memory("first fact about apples")
        db.save_memory("second fact about pears")
        keys = [k for k in rconn.keys("mem:*") if not k.startswith(b"mem:sha:")]
        assert len(keys) == 2

    def test_hash_fields(self, db, rconn, cfg):
        r = db.save_memory("hash integrity check", agent_name="a1", session_id="s1", turn_id="t1")
        h = rconn.hgetall(MEM + r["id"])
        assert h[b"id"].decode() == r["id"]
        assert h[b"content"] == "hash integrity check".encode()
        assert h[b"owner_id"].decode() == cfg["owner_id"]
        assert h[b"agent_name"] == b"a1" and h[b"session_id"] == b"s1" and h[b"turn_id"] == b"t1"
        assert h[b"parent_id"] == b""
        assert len(h[b"embedding"]) == 768 * 4

    def test_insert_without_embedding(self, db, rconn):
        r = db.save_memory("no vector here", embed=False)
        assert r["warning"] == "embedding_unavailable"
        assert b"embedding" not in rconn.hgetall(MEM + r["id"])
        assert any("no vector" in x["content"] for x in _normal(db, "no vector here"))

    def test_parent_id_stored(self, db, rconn):
        r = db.save_memory("long " * 200)
        for cid in r["ids"]:
            assert rconn.hget(MEM + cid, "parent_id").decode() == r["parent_id"]


class TestDedup:
    def test_exact_duplicate(self, db):
        assert db.save_memory("same text")["saved"]
        assert db.save_memory("same text")["status"] == "duplicate"

    def test_nfkc_duplicate(self, db):
        assert db.save_memory("ＡＩ ２０２６")["saved"]
        assert db.save_memory("AI 2026")["status"] == "duplicate"

    def test_parent_sha_guard(self, db, rconn):
        body = "parent document text. " * 40
        r = db.save_memory(body)
        dup = db.save_memory(body)
        assert dup["status"] == "duplicate" and dup["parent_id"] == r["parent_id"]
        assert rconn.exists(DOCSHA + P.content_hash(body.strip()))

    def test_similar_is_not_rejected(self, db):
        assert db.save_memory("The project budget is 100 dollars.")["saved"]
        assert db.save_memory("The project budget is 120 dollars.")["saved"]


class TestDelete:
    def test_single_delete(self, db, rconn):
        r = db.save_memory("to be deleted")
        assert db.delete_memory(r["id"])["status"] == "deleted"
        assert not rconn.exists(MEM + r["id"])
        assert not rconn.exists(SHA + P.content_hash("to be deleted"))

    def test_cascade(self, db, rconn):
        r = db.save_memory("cascade " * 120)
        res = db.delete_memory(r["parent_id"])
        assert res["status"] == "deleted" and res["chunks_deleted"] == r["chunks"]
        assert not rconn.exists(DOC + r["parent_id"])
        assert not any(rconn.exists(MEM + c) for c in r["ids"])
        assert not rconn.exists(DOCSHA + P.content_hash(("cascade " * 120).strip()))

    def test_chunk_delete_keeps_single_guard(self, db, rconn):
        body = ("alpha beta gamma delta. " * 30).strip()
        r = db.save_memory(body)
        first = rconn.hget(MEM + r["ids"][0], "content").decode().strip()
        single = db.save_memory(first)
        assert single["saved"]
        assert db.delete_memory(r["ids"][0])["status"] == "deleted"
        assert db.save_memory(first)["status"] == "duplicate"
        assert db.delete_memory(single["id"])["status"] == "deleted"
        assert db.save_memory(first)["saved"]

    def test_not_found(self, db):
        assert db.delete_memory("00000000-0000-0000-0000-000000000000")["status"] == "not_found"


class TestTTL:
    def test_ttl_set(self, db, rconn, cfg):
        r = db.save_memory("ttl check")
        assert 0 < rconn.ttl(MEM + r["id"]) <= cfg["ttl_seconds"]
        assert 0 < rconn.ttl(SHA + P.content_hash("ttl check")) <= cfg["ttl_seconds"]

    def test_search_does_not_change_ttl(self, db, rconn):
        r = db.save_memory("ttl search check")
        rconn.expire(MEM + r["id"], 1000)
        _normal(db, "ttl search check")
        assert rconn.ttl(MEM + r["id"]) <= 1000

    def test_expiry(self, cfg, rconn):
        cfg["ttl_seconds"] = 1
        d = Database(cfg)
        assert d.ensure_connected() == "ok"
        r = d.save_memory("short lived")
        time.sleep(2.2)
        assert not rconn.exists(MEM + r["id"])
        d.client.close()


class TestFTS:
    def test_punctuation_removed(self, db):
        db.save_memory("note, with punctuation!")
        assert _normal(db, "note punctuation")

    def test_bm25_scores(self, db):
        rows = db._bm25_search("apple", 10, None, None)
        assert rows == []
        db.save_memory("apple")
        db.save_memory("apple apple apple apple apple tree")
        rows = db._bm25_search("apple", 10, None, None)
        assert len(rows) == 2 and all(r["__score"] is not None for r in rows)
        assert len({round(r["__score"], 4) for r in rows}) == 2

    def test_short_query(self, db):
        db.save_memory("go is a language")
        assert db._bm25_search("go", 10, None, None)

    def test_cjk_bigram_hit(self, db):
        db.save_memory("記憶装置の設計について話した")
        assert db._bm25_search("記憶装置", 10, None, None)


class TestVectorSearch:
    def test_knn(self, db):
        db.save_memory("vector search target sentence")
        rows = db._vector_search(P.embed("vector search target sentence", "query"), 5, None, None)
        assert rows and float(rows[0]["__dist"]) < 0.05

    def test_empty_db(self, db):
        assert db._vector_search(P.embed("anything", "query"), 5, None, None) == []

    def test_owner_filter(self, db, cfg, rconn):
        other = dict(cfg)
        other["owner_id"] = "11111111-1111-1111-1111-111111111111"
        d2 = Database(other)
        d2.ensure_connected()
        d2.save_memory("belongs to someone else entirely")
        assert not any("someone else" in r["content"] for r in _normal(db, "belongs to someone else entirely"))
        d2.client.close()

    def test_only_standalone_in_list(self, db):
        db.save_memory("standalone row")
        db.save_memory("chunked " * 150)
        text = db.list_memories(20)
        assert "standalone row" in text and "[doc×" in text


class TestCjkBigramExpand:
    def test_mixed(self):
        assert P.cjk_bigram_expand("ひらがなカタカナ漢字").split()[:3] == ["ひら", "らが", "がな"]

    def test_single_char(self):
        assert P.cjk_bigram_expand("A 字 B").split() == ["A", "字", "B"]

    def test_boundary(self):
        assert P.cjk_bigram_expand("abc記憶def").split() == ["abc", "記憶", "def"]


class TestAccessCount:
    def test_increment(self, db, rconn):
        r = db.save_memory("access counted row")
        _normal(db, "access counted row")
        assert int(rconn.hget(MEM + r["id"], "access_count")) == 1

    def test_top_n_only(self, db, rconn, cfg):
        cfg["ttl_refresh_top_k"] = 1
        ids = [db.save_memory(f"topn sample number {i}")["id"] for i in range(3)]
        _normal(db, "topn sample number")
        counts = [int(rconn.hget(MEM + i, "access_count")) for i in ids]
        assert sum(counts) == 1

    def test_cap(self):
        cfg = {"access_count_enabled": True, "access_count_weight": 0.02, "access_count_max_boost": 0.5}
        assert P.b_local(1.0, 1000, cfg) == 1.5


class TestSerialization:
    def test_roundtrip(self):
        v = [0.5, -1.25, 3.0]
        assert P.unpack_vector(P.pack_vector(v)) == v

    def test_f32_le(self):
        assert P.pack_vector([1.0]) == struct.pack("<f", 1.0)


class TestSha1:
    def test_hex(self):
        assert P.sha1_hex("abc") == "a9993e364706816aba3e25717850c26c9cd0d89d"

    def test_empty(self):
        assert P.sha1_hex("") == "da39a3ee5e6b4b0d3255bfef95601890afd80709"

    def test_non_ascii(self):
        assert len(P.sha1_hex("記憶")) == 40

    def test_long(self):
        assert len(P.sha1_hex("x" * 100000)) == 40



class TestFinalScoreProduct:
    """§3.6: score = base × time_decay × b_local × b_session (the lexical bonus is added afterwards)."""

    def test_factors_multiply(self, db, monkeypatch):
        monkeypatch.setattr(P, "lexical_bonus", lambda *a, **k: 0.0)
        # Each search bumps access_count, which rewrites the hash; in a one-record test index the BM25
        # score then falls towards 0. The keyword part is fixed so only the product is measured.
        monkeypatch.setattr(P, "keyword_relevance", lambda *a, **k: 1.0)
        db.save_memory("orbital harbor lantern schedule", session_id="S1")

        def score(sid):
            rows = _normal(db, "orbital harbor lantern schedule", session_id=sid)
            assert rows, sid
            return rows[0]["score"]

        monkeypatch.setattr(P, "time_decay", lambda *a, **k: 1.0)
        base = score("S1")                                   # importance 1.0, same session: the base score
        assert 0 < base <= 1.0
        assert score("S2") == pytest.approx(base * 0.6)      # b_session mismatch
        monkeypatch.setattr(P, "time_decay", lambda *a, **k: 0.5)
        assert score("S1") == pytest.approx(base * 0.5)      # time_decay
        monkeypatch.setattr(P, "b_local", lambda *a, **k: 1.5)
        assert score("S1") == pytest.approx(base * 0.5 * 1.5)
        assert score("S2") == pytest.approx(base * 0.5 * 1.5 * 0.6)
