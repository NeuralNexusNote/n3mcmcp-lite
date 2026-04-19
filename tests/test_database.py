"""
Redis store unit tests for n3mc_mcp.database (Lite build).

These require a live Redis Stack — see conftest.py.
"""
import math
import time

from uuid_utils import uuid7 as _gen_uuid7

from n3mc_mcp.database import (
    bytes_to_vector,
    check_exact_duplicate,
    cjk_bigram_expand,
    count_memories,
    delete_memory,
    get_all_memories,
    get_memory_by_id,
    increment_access_counts,
    insert_memory,
    search_fts,
    search_vector,
    sha1_of,
    strip_fts_punctuation,
    vector_to_bytes,
)


def make_vec(dim=768):
    return [1.0 / math.sqrt(dim)] * dim


class TestCjkBigramExpand:
    def test_four_char_run(self):
        result = cjk_bigram_expand("記憶装置")
        assert result == "記憶 憶装 装置"

    def test_single_cjk_char_unchanged(self):
        assert cjk_bigram_expand("記") == "記"

    def test_two_char_run(self):
        assert cjk_bigram_expand("記憶") == "記憶"

    def test_mixed_latin_cjk(self):
        result = cjk_bigram_expand("RAG記憶装置test")
        assert "記憶 憶装 装置" in result
        assert result.startswith("RAG")
        assert result.endswith("test")

    def test_no_cjk_unchanged(self):
        assert cjk_bigram_expand("hello world 123") == "hello world 123"

    def test_empty_string(self):
        assert cjk_bigram_expand("") == ""


def _wait_indexed(client, expected_total, timeout=2.0):
    """RediSearch is eventually consistent; poll briefly for the index to catch up."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if count_memories(client) >= expected_total:
            return
        time.sleep(0.05)


class TestIndexSetup:
    def test_index_created(self, redis_client):
        info = redis_client.ft("n3mc_idx").info()
        # redis-py returns a dict-like; index_name is a bytes field.
        assert info is not None


class TestInsertAndRetrieve:
    def test_insert_and_count(self, redis_client):
        assert count_memories(redis_client) == 0
        insert_memory(
            redis_client, str(_gen_uuid7()), "hello world",
            "2025-01-01T00:00:00+00:00", "owner1", None,
        )
        _wait_indexed(redis_client, 1)
        assert count_memories(redis_client) == 1

    def test_insert_stores_embedding(self, redis_client):
        vec = make_vec()
        rid = str(_gen_uuid7())
        insert_memory(
            redis_client, rid, "test content",
            "2025-01-01T00:00:00+00:00", "owner1", vec,
        )
        raw = redis_client.hget(f"mem:{rid}", "embedding")
        assert raw is not None
        recovered = bytes_to_vector(raw)
        assert len(recovered) == 768

    def test_insert_without_embedding(self, redis_client):
        rid = str(_gen_uuid7())
        insert_memory(
            redis_client, rid, "no embedding",
            "2025-01-01T00:00:00+00:00", "owner1", None,
        )
        assert redis_client.hget(f"mem:{rid}", "embedding") is None

    def test_get_memory_by_id(self, redis_client):
        rid = str(_gen_uuid7())
        insert_memory(
            redis_client, rid, "find me",
            "2025-01-01T00:00:00+00:00", "owner1", None,
        )
        row = get_memory_by_id(redis_client, rid)
        assert row is not None
        assert row["content"] == "find me"

    def test_get_all_memories(self, redis_client):
        insert_memory(
            redis_client, str(_gen_uuid7()), "a",
            "2025-01-01T00:00:00+00:00", "owner1", None,
        )
        insert_memory(
            redis_client, str(_gen_uuid7()), "b",
            "2025-01-02T00:00:00+00:00", "owner1", None,
        )
        _wait_indexed(redis_client, 2)
        rows = get_all_memories(redis_client)
        assert len(rows) == 2

    def test_get_all_memories_limit(self, redis_client):
        for i in range(5):
            insert_memory(
                redis_client, str(_gen_uuid7()), f"entry {i}",
                f"2025-01-0{i+1}T00:00:00+00:00", "owner1", None,
            )
        _wait_indexed(redis_client, 5)
        rows = get_all_memories(redis_client, limit=3)
        assert len(rows) == 3


class TestAccessCount:
    def test_initial_access_count_zero(self, redis_client):
        rid = str(_gen_uuid7())
        insert_memory(
            redis_client, rid, "new entry",
            "2025-01-01T00:00:00+00:00", "owner1", None,
        )
        row = get_memory_by_id(redis_client, rid)
        assert int(row.get("access_count") or 0) == 0

    def test_increment_single(self, redis_client):
        rid = str(_gen_uuid7())
        insert_memory(
            redis_client, rid, "incremented",
            "2025-01-01T00:00:00+00:00", "owner1", None,
        )
        increment_access_counts(redis_client, [rid])
        row = get_memory_by_id(redis_client, rid)
        assert int(row["access_count"]) == 1

    def test_increment_accumulates(self, redis_client):
        rid = str(_gen_uuid7())
        insert_memory(
            redis_client, rid, "popular",
            "2025-01-01T00:00:00+00:00", "owner1", None,
        )
        for _ in range(5):
            increment_access_counts(redis_client, [rid])
        row = get_memory_by_id(redis_client, rid)
        assert int(row["access_count"]) == 5

    def test_increment_batch(self, redis_client):
        ids = [str(_gen_uuid7()) for _ in range(3)]
        for rid in ids:
            insert_memory(
                redis_client, rid, f"entry {rid}",
                "2025-01-01T00:00:00+00:00", "owner1", None,
            )
        increment_access_counts(redis_client, ids)
        for rid in ids:
            row = get_memory_by_id(redis_client, rid)
            assert int(row["access_count"]) == 1

    def test_increment_refreshes_ttl_when_requested(self, redis_client):
        rid = str(_gen_uuid7())
        insert_memory(
            redis_client, rid, "ttl target",
            "2025-01-01T00:00:00+00:00", "owner1", None,
            ttl_seconds=60,
        )
        increment_access_counts(redis_client, [rid], ttl_seconds=3600)
        ttl = redis_client.ttl(f"mem:{rid}")
        assert ttl > 60

    def test_empty_id_list_is_noop(self, redis_client):
        # No keys touched when given an empty list
        increment_access_counts(redis_client, [])


class TestTTL:
    def test_ttl_is_set_on_insert(self, redis_client):
        rid = str(_gen_uuid7())
        insert_memory(
            redis_client, rid, "ephemeral",
            "2025-01-01T00:00:00+00:00", "owner1", None,
            ttl_seconds=3600,
        )
        ttl = redis_client.ttl(f"mem:{rid}")
        assert 0 < ttl <= 3600

    def test_sha_key_shares_ttl(self, redis_client):
        insert_memory(
            redis_client, str(_gen_uuid7()), "same text",
            "2025-01-01T00:00:00+00:00", "owner1", None,
            ttl_seconds=3600,
        )
        sha_ttl = redis_client.ttl(f"mem:sha:{sha1_of('same text')}")
        assert 0 < sha_ttl <= 3600


class TestDelete:
    def test_delete_removes_record_and_sha(self, redis_client):
        rid = str(_gen_uuid7())
        insert_memory(
            redis_client, rid, "to delete",
            "2025-01-01T00:00:00+00:00", "owner1", make_vec(),
        )
        _wait_indexed(redis_client, 1)
        assert count_memories(redis_client) == 1

        assert delete_memory(redis_client, rid) is True
        _wait_indexed(redis_client, 0)

        assert count_memories(redis_client) == 0
        assert redis_client.exists(f"mem:sha:{sha1_of('to delete')}") == 0

    def test_delete_nonexistent_returns_false(self, redis_client):
        assert delete_memory(redis_client, "nonexistent-id") is False


class TestDedup:
    def test_check_exact_duplicate_true(self, redis_client):
        insert_memory(
            redis_client, str(_gen_uuid7()), "duplicate text",
            "2025-01-01T00:00:00+00:00", "owner1", None,
        )
        assert check_exact_duplicate(redis_client, "duplicate text") is True

    def test_check_exact_duplicate_false(self, redis_client):
        assert check_exact_duplicate(redis_client, "unique text") is False


class TestFTS:
    def test_strip_fts_punctuation(self):
        assert strip_fts_punctuation("hello, world!") == "hello  world "
        assert "test" in strip_fts_punctuation("(test) [bracket]")
        assert "bracket" in strip_fts_punctuation("(test) [bracket]")

    def test_search_fts_basic(self, redis_client):
        insert_memory(
            redis_client, str(_gen_uuid7()),
            "Abraham Lincoln president",
            "2025-01-01T00:00:00+00:00", "owner1", None,
        )
        _wait_indexed(redis_client, 1)
        assert len(search_fts(redis_client, "Lincoln")) >= 1

    def test_search_fts_empty_query(self, redis_client):
        assert search_fts(redis_client, "") == []

    def test_search_fts_punctuation_resilience(self, redis_client):
        insert_memory(
            redis_client, str(_gen_uuid7()),
            "Planet Alpha temperature settings",
            "2025-01-01T00:00:00+00:00", "owner1", None,
        )
        _wait_indexed(redis_client, 1)
        assert len(search_fts(redis_client, "Alpha temperature")) >= 1


class TestVectorSearch:
    def test_search_vector_returns_results(self, redis_client):
        vec = make_vec()
        insert_memory(
            redis_client, str(_gen_uuid7()), "vector test",
            "2025-01-01T00:00:00+00:00", "owner1", vec,
        )
        _wait_indexed(redis_client, 1)
        results = search_vector(redis_client, vec, k=5)
        assert len(results) >= 1
        assert results[0][1] < 0.01  # identical vector → near-zero cosine distance

    def test_search_vector_empty_db(self, redis_client):
        assert search_vector(redis_client, make_vec(), k=5) == []


class TestSerialization:
    def test_vector_roundtrip(self):
        v = [float(i) / 768 for i in range(768)]
        recovered = bytes_to_vector(vector_to_bytes(v))
        assert len(recovered) == 768
        assert abs(recovered[0] - v[0]) < 1e-5


class TestSha1:
    def test_sha1_deterministic(self):
        assert sha1_of("hello") == sha1_of("hello")

    def test_sha1_different(self):
        assert sha1_of("hello") != sha1_of("world")
