"""Layer 1: Redis / RediSearch (spec §3.3–§3.12). Needs N3MC_REDIS_TEST_URL."""
import struct
import time

import pytest

from n3mc_mcp import database, processor
from n3mc_mcp.database import DOC, DOCSHA, INDEX, MEM, SHA, Database, sha1_hex


def _keys(rconn, pattern):
    return [k.decode() for k in rconn.keys(pattern)]


def _mem_keys(rconn):
    return [k for k in _keys(rconn, "mem:*") if not k.startswith(SHA)]


LONG = "\n".join(f"第{i}項: 設定値 alpha{i} は {i * 7} で、運用時に必ず確認すること。" for i in range(1, 40))


class TestIndexSetup:
    def test_index_is_created(self, db, rconn):
        info = rconn.execute_command("FT.INFO", INDEX)
        assert info

    def test_ensure_index_is_idempotent(self, db):
        db.ensure_index()
        db.ensure_index()
        names, dim = db._ft_info()
        assert "content_fts" in names and dim == 768

    def test_old_index_without_content_fts_is_rebuilt(self, rconn, cfg):
        rconn.execute_command("FT.CREATE", INDEX, "ON", "HASH", "PREFIX", 1, MEM, "SCHEMA",
                              "content", "TEXT", "embedding", "VECTOR", "FLAT", 6, "TYPE", "FLOAT32",
                              "DIM", 768, "DISTANCE_METRIC", "COSINE")
        d = Database(cfg)
        assert d.ensure_connected() == "ok"
        names, _dim = d._ft_info()
        assert "content_fts" in names
        d.close()


class TestInsertAndRetrieve:
    def test_insert_then_count(self, db, rconn):
        for i in range(3):
            assert db.save_memory(f"insert test entry number {i}")["saved"]
        assert len(_mem_keys(rconn)) == 3

    def test_hash_fields(self, db, rconn, cfg):
        r = db.save_memory("hash field check", agent_name="agent-x", session_id="s-1", turn_id="t-1")
        h = rconn.hgetall(MEM + r["id"])
        assert h[b"id"].decode() == r["id"]
        assert h[b"content"] == "hash field check".encode()
        assert h[b"owner_id"].decode() == cfg["owner_id"]
        assert h[b"agent_name"] == b"agent-x" and h[b"session_id"] == b"s-1" and h[b"turn_id"] == b"t-1"
        assert h[b"parent_id"] == b"" and float(h[b"importance"]) == 1.0
        assert len(h[b"embedding"]) == 768 * 4

    def test_insert_without_embedding(self, db, rconn):
        r = db.save_memory("no vector here", embed=False)
        assert r["saved"]
        assert not rconn.hexists(MEM + r["id"], "embedding")

    def test_chunks_store_parent_id(self, db, rconn):
        r = db.save_memory(LONG)
        assert r["chunks"] >= 2
        for cid in r["ids"]:
            assert rconn.hget(MEM + cid, "parent_id").decode() == r["parent_id"]


class TestDedup:
    def test_exact_duplicate(self, db):
        assert db.save_memory("same text")["saved"]
        assert db.save_memory("same text") == {"status": "duplicate", "saved": False}

    def test_parent_duplicate_reports_parent(self, db):
        first = db.save_memory(LONG)
        again = db.save_memory(LONG)
        assert again["status"] == "duplicate" and again["parent_id"] == first["parent_id"]

    def test_similar_text_is_not_rejected(self, db):
        assert db.save_memory("The project budget is 100 dollars.")["saved"]
        assert db.save_memory("The project budget is 120 dollars.")["saved"]


class TestDelete:
    def test_delete_single_and_guard(self, db, rconn):
        r = db.save_memory("to be deleted")
        assert db.delete_memory(r["id"])["status"] == "deleted"
        assert not rconn.exists(MEM + r["id"])
        assert not rconn.exists(SHA + sha1_hex("to be deleted"))

    def test_cascade_parent_to_chunks(self, db, rconn):
        r = db.save_memory(LONG)
        out = db.delete_memory(r["parent_id"])
        assert out["status"] == "deleted" and out["chunks_deleted"] == r["chunks"]
        assert not _mem_keys(rconn) and not rconn.exists(DOC + r["parent_id"])

    def test_docsha_guard_is_removed(self, db, rconn):
        r = db.save_memory(LONG)
        db.delete_memory(r["parent_id"])
        assert not _keys(rconn, DOCSHA + "*")
        assert db.save_memory(LONG)["saved"]

    def test_chunk_delete_keeps_independent_guard(self, db, rconn, cfg):
        r = db.save_memory(LONG)
        first_chunk = processor.chunk_text(LONG, cfg["chunk_threshold"], cfg["chunk_overlap"])[0].strip()
        single = db.save_memory(first_chunk)
        assert single["saved"]
        db.delete_memory(r["ids"][0])
        assert db.save_memory(first_chunk)["status"] == "duplicate"
        db.delete_memory(single["id"])
        assert db.save_memory(first_chunk)["saved"]


class TestTTL:
    def test_ttl_is_set(self, db, rconn, cfg):
        r = db.save_memory("ttl check")
        assert 0 < rconn.ttl(MEM + r["id"]) <= cfg["ttl_seconds"]
        assert 0 < rconn.ttl(SHA + sha1_hex("ttl check")) <= cfg["ttl_seconds"]

    def test_search_does_not_change_ttl(self, db, rconn):
        r = db.save_memory("ttl stays after search")
        rconn.expire(MEM + r["id"], 1000)
        db.search("ttl stays after search")
        assert rconn.ttl(MEM + r["id"]) <= 1000

    def test_expiry(self, rconn, cfg):
        cfg["ttl_seconds"] = 1
        d = Database(cfg)
        d.ensure_connected()
        r = d.save_memory("short lived")
        time.sleep(2.2)
        assert not rconn.exists(MEM + r["id"])
        d.close()


class TestFTS:
    def test_punctuation_query(self, db):
        r = db.save_memory("Redis Stack hybrid search, with BM25!")
        hits = db._bm25_search("hybrid; (search)?", 10)
        assert r["id"] in [h["id"] for h in hits]

    def test_bm25_score_present(self, db):
        db.save_memory("apple apple apple apple banana")
        db.save_memory("apple cherry")
        hits = db._bm25_search("apple", 10)
        assert hits and all(h["bm25"] > 0 for h in hits)

    def test_short_query(self, db):
        r = db.save_memory("ok then we go")
        assert r["id"] in [h["id"] for h in db._bm25_search("ok", 10)]

    def test_cjk_bigram_hit(self, db):
        r = db.save_memory("記憶装置の設計について話した")
        assert r["id"] in [h["id"] for h in db._bm25_search("記憶装置", 10)]


class TestVectorSearch:
    def test_knn_returns_nearest(self, db, fake_model):
        target = db.save_memory("quantum computing optimization problems")
        db.save_memory("a recipe for miso soup")
        hits = db._vector_search(fake_model.vector("quantum computing optimization problems"), 5)
        assert hits[0]["id"] == target["id"] and hits[0]["cos"] > 0.99

    def test_empty_db(self, db, fake_model):
        assert db._vector_search(fake_model.vector("anything"), 5) == []

    def test_owner_filter(self, db, cfg, fake_model):
        other = dict(cfg, owner_id="00000000-0000-4000-8000-000000000000")
        d2 = Database(other)
        d2.ensure_connected()
        d2.save_memory("belongs to someone else")
        assert db._vector_search(fake_model.vector("belongs to someone else"), 5) == []
        d2.close()

    def test_list_shows_independent_records_only(self, db):
        r = db.save_memory(LONG)
        s = db.save_memory("independent record")
        ids = [x["id"] for x in db.list_entries(20)]
        assert s["id"] in ids and r["parent_id"] in ids
        assert not set(r["ids"]) & set(ids)


class TestCjkBigramExpand:
    def test_mixed_scripts(self):
        assert processor.cjk_bigram_expand("記憶装置") == "記憶 憶装 装置"
        assert processor.cjk_bigram_expand("アルファ9") == "アル ルフ ファ 9"

    def test_single_char_run_kept(self):
        assert processor.cjk_bigram_expand("a 猫 b") == "a 猫 b"

    def test_ascii_untouched_and_folded(self):
        assert processor.cjk_bigram_expand("hello world") == "hello world"
        assert processor.cjk_bigram_expand("café") == "café cafe"


class TestAccessCount:
    def test_hits_are_counted(self, db, rconn):
        r = db.save_memory("count my access please")
        db.search("count my access please")
        assert int(rconn.hget(MEM + r["id"], "access_count")) == 1

    def test_only_top_n(self, db, rconn, cfg):
        cfg["ttl_refresh_top_k"] = 1
        a = db.save_memory("alpha beta gamma delta")
        b = db.save_memory("alpha beta gamma epsilon")
        normal, _w, _m = db.search("alpha beta gamma delta")
        counts = {x["id"]: int(rconn.hget(MEM + x["id"], "access_count")) for x in (a, b)}
        assert sum(counts.values()) == 1 and counts[normal[0]["id"]] == 1

    def test_boost_is_capped(self, cfg):
        cfg = dict(cfg, access_count_enabled=True)
        assert processor.b_local(1.0, 10_000, cfg) == 1.5
        assert processor.b_local(2.0, 10_000, cfg) == 2.0


class TestSerialization:
    def test_roundtrip(self):
        v = [0.5, -1.25, 3.0]
        assert database.bytes_to_vec(database.vec_to_bytes(v)) == v

    def test_little_endian_f32(self):
        assert database.vec_to_bytes([1.0]) == struct.pack("<f", 1.0)


class TestSha1:
    def test_hex(self):
        assert len(sha1_hex("abc")) == 40 and all(c in "0123456789abcdef" for c in sha1_hex("abc"))

    def test_empty(self):
        assert sha1_hex("") == "da39a3ee5e6b4b0d3255bfef95601890afd80709"

    def test_non_ascii_is_utf8_and_nfkc(self):
        assert sha1_hex("ＡＩ") == sha1_hex("AI")
        assert sha1_hex("日本語") != sha1_hex("日本")

    def test_long(self):
        assert len(sha1_hex("x" * 100_000)) == 40
