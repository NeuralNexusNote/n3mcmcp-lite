import time
import pytest
from tests.conftest import requires_redis


# ── TestIndexSetup ────────────────────────────────────────────────────────────

@requires_redis
class TestIndexSetup:
    def test_index_created(self, db):
        info = db._client.execute_command("FT.INFO", "n3mc_idx")
        assert info is not None

    def test_ensure_index_idempotent(self, db):
        db.ensure_index()
        db.ensure_index()
        info = db._client.execute_command("FT.INFO", "n3mc_idx")
        assert info is not None


# ── TestInsertAndRetrieve ─────────────────────────────────────────────────────

@requires_redis
class TestInsertAndRetrieve:
    def test_save_single_returns_saved(self, db):
        result = db.save_memory("Hello Redis test")
        assert result["saved"] is True
        assert "id" in result
        assert result["ttl_seconds"] == 604800

    def test_key_exists_after_save(self, db, redis_client):
        result = db.save_memory("key existence check")
        mem_key = f"mem:{result['id']}"
        assert redis_client.exists(mem_key)

    def test_ttl_set_on_key(self, db, redis_client):
        result = db.save_memory("ttl check entry")
        mem_key = f"mem:{result['id']}"
        ttl = redis_client.ttl(mem_key)
        assert ttl > 0

    def test_parent_id_empty_for_single(self, db, redis_client):
        result = db.save_memory("short entry")
        d = redis_client.hgetall(f"mem:{result['id']}")
        assert d[b"parent_id"] == b""

    def test_content_stored_correctly(self, db, redis_client):
        text = "unique content for retrieval"
        result = db.save_memory(text)
        d = redis_client.hgetall(f"mem:{result['id']}")
        assert d[b"content"].decode() == text

    def test_empty_content_rejected(self, db):
        result = db.save_memory("")
        assert result["saved"] is False
        assert "empty" in result["reason"]

    def test_whitespace_only_rejected(self, db):
        result = db.save_memory("   ")
        assert result["saved"] is False


# ── TestDedup ─────────────────────────────────────────────────────────────────

@requires_redis
class TestDedup:
    def test_exact_duplicate_rejected(self, db):
        content = "exact duplicate test content"
        db.save_memory(content)
        result = db.save_memory(content)
        assert result["saved"] is False
        assert result["status"] == "duplicate"

    def test_sha_guard_key_exists(self, db, redis_client):
        import hashlib
        content = "sha guard verification"
        db.save_memory(content)
        sha = hashlib.sha1(content.encode()).hexdigest()
        assert redis_client.exists(f"mem:sha:{sha}")

    def test_different_content_saved(self, db):
        db.save_memory("first unique content abc")
        result = db.save_memory("second unique content xyz")
        assert result["saved"] is True


# ── TestSkipCodeBlocks ────────────────────────────────────────────────────────

@requires_redis
class TestSkipCodeBlocks:
    def test_default_saves_code(self, db):
        result = db.save_memory("see ```python\nprint('hi')\n``` for details")
        assert result["saved"] is True

    def test_flag_on_skips_fenced_code(self, db):
        db.cfg["skip_code_blocks"] = True
        result = db.save_memory("see ```python\nprint('hi')\n``` for details")
        assert result["saved"] is False
        assert result["status"] == "skipped_code"

    def test_flag_on_allows_prose(self, db):
        db.cfg["skip_code_blocks"] = True
        result = db.save_memory("a plain sentence with no fences at all")
        assert result["saved"] is True


# ── TestDelete ────────────────────────────────────────────────────────────────

@requires_redis
class TestDelete:
    def test_delete_existing(self, db, redis_client):
        result = db.save_memory("entry to delete")
        mem_id = result["id"]
        del_result = db.delete_memory(mem_id)
        assert del_result["status"] == "deleted"
        assert not redis_client.exists(f"mem:{mem_id}")

    def test_delete_removes_sha_guard(self, db, redis_client):
        import hashlib
        content = "delete with sha guard"
        result = db.save_memory(content)
        db.delete_memory(result["id"])
        sha = hashlib.sha1(content.encode()).hexdigest()
        assert not redis_client.exists(f"mem:sha:{sha}")

    def test_delete_nonexistent_returns_not_found(self, db):
        result = db.delete_memory("nonexistent-id-12345")
        assert result["status"] == "not_found"

    def test_parent_delete_cascades_chunks(self, db, redis_client):
        long_text = "A" * 500 + " " + "B" * 500
        result = db.save_memory(long_text)
        parent_id = result["parent_id"]
        chunk_ids = result["ids"]
        db.delete_memory(parent_id)
        assert not redis_client.exists(f"doc:{parent_id}")
        for cid in chunk_ids:
            assert not redis_client.exists(f"mem:{cid}")


# ── TestTTL ───────────────────────────────────────────────────────────────────

@requires_redis
class TestTTL:
    def test_ttl_positive(self, db, redis_client):
        result = db.save_memory("ttl positive test")
        ttl = redis_client.ttl(f"mem:{result['id']}")
        assert ttl > 0

    def test_sha_guard_ttl(self, db, redis_client):
        import hashlib
        content = "sha ttl check"
        db.save_memory(content)
        sha = hashlib.sha1(content.encode()).hexdigest()
        ttl = redis_client.ttl(f"mem:sha:{sha}")
        assert ttl > 0


# ── TestFTS ───────────────────────────────────────────────────────────────────

@requires_redis
class TestFTS:
    def test_bm25_finds_english(self, db):
        db.save_memory("Abraham Lincoln was the 16th president of the United States")
        time.sleep(0.5)
        results = db.search_memory("Abraham Lincoln president")
        assert any("Lincoln" in r["content"] for r in results)

    def test_cjk_bigram_hit(self, db):
        db.save_memory("坂本龍馬は幕末の志士であり土佐藩出身の人物です")
        time.sleep(0.5)
        results = db.search_memory("坂本龍馬")
        assert any("坂本龍馬" in r["content"] for r in results)


# ── TestVectorSearch ──────────────────────────────────────────────────────────

@requires_redis
class TestVectorSearch:
    def test_saved_content_findable(self, db):
        db.save_memory("The quick brown fox jumps over the lazy dog")
        time.sleep(0.5)
        results = db.search_memory("fox dog animal")
        assert len(results) > 0

    def test_empty_db_returns_empty(self, db):
        results = db.search_memory("anything at all")
        assert results == []

    def test_owner_visibility(self, cfg, redis_client):
        """Different owner_id records are now visible to all (n3mc-postgres parity).

        owner_id is stored and returned but no longer used as a hard filter.
        Records from any installation are visible; local_id match gives a ranking
        boost (b_local_match=1.0) vs mismatch (b_local_mismatch=0.8).
        """
        import uuid
        from n3mc_mcp.database import Database

        cfg2 = dict(cfg)
        cfg2["owner_id"] = str(uuid.uuid4())

        db1 = Database(cfg)
        db1.connect()
        db1.ensure_index()

        db2 = Database(cfg2)
        db2.connect()
        db2.ensure_index()

        db1.save_memory("owner1 specific content about dolphins")
        time.sleep(0.5)
        results = db2.search_memory("dolphins")
        # Records from owner1 ARE visible to owner2 (team-visibility design)
        assert len(results) >= 1
        assert any("dolphins" in r.get("content", "") for r in results)


# ── TestSha1 ─────────────────────────────────────────────────────────────────

class TestSha1:
    def test_sha1_hex(self):
        from n3mc_mcp.database import _sha1
        result = _sha1("hello")
        assert len(result) == 40
        assert all(c in "0123456789abcdef" for c in result)

    def test_empty_string(self):
        from n3mc_mcp.database import _sha1
        assert len(_sha1("")) == 40

    def test_unicode_content(self):
        from n3mc_mcp.database import _sha1
        result = _sha1("日本語テスト")
        assert len(result) == 40


# ── TestParentDocument ────────────────────────────────────────────────────────

@requires_redis
class TestParentDocument:
    def test_long_content_creates_parent(self, db):
        long_text = "架空都市「不知火」は南太平洋に浮かぶ人工島都市である。" * 40
        result = db.save_memory(long_text)
        assert result["saved"] is True
        assert "parent_id" in result
        assert result["chunks"] > 1

    def test_parent_doc_key_exists(self, db, redis_client):
        long_text = "テスト用の長文コンテンツです。" * 40
        result = db.save_memory(long_text)
        assert redis_client.exists(f"doc:{result['parent_id']}")

    def test_verbatim_recall(self, db):
        long_text = ("架空都市「不知火」設定資料。浮遊する都市、住民は空中庭園に暮らす。"
                     "エネルギー源は反重力結晶。行政は評議会制。通貨は光子。") * 20
        db.save_memory(long_text)
        time.sleep(1.0)
        results = db.search_memory("不知火 浮遊都市 反重力")
        assert any("不知火" in r["content"] for r in results)

    def test_parent_doc_duplicate_rejected(self, db):
        long_text = "重複テスト用の長文です。" * 50
        db.save_memory(long_text)
        result2 = db.save_memory(long_text)
        assert result2["saved"] is False
        assert result2["status"] == "duplicate"


# ── TestRepair ────────────────────────────────────────────────────────────────

@requires_redis
class TestRepair:
    def test_repair_ok(self, db):
        result = db.repair_memory()
        assert result["status"] == "ok"
