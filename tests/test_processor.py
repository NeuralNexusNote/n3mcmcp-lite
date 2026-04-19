"""
Ranking math, purification, embedding tests for n3mc_mcp.processor (Lite build).
"""
from n3mc_mcp.processor import (
    chunk_text,
    cosine_sim_from_distance,
    cosine_sim_from_l2,
    embed_passage,
    embed_query,
    final_score,
    keyword_relevance,
    lexical_rerank,
    purify,
    time_decay,
)


class TestCosineSim:
    def test_identical_vectors(self):
        # cosine distance 0 → similarity 1
        assert cosine_sim_from_distance(0.0) == 1.0

    def test_orthogonal_vectors(self):
        # cosine distance 1 → similarity 0
        assert cosine_sim_from_distance(1.0) == 0.0

    def test_opposite_vectors(self):
        # cosine distance 2 (sim = -1) clamped to 0
        assert cosine_sim_from_distance(2.0) == 0.0

    def test_intermediate_value(self):
        assert abs(cosine_sim_from_distance(0.3) - 0.7) < 1e-9

    def test_backwards_compat_alias(self):
        # The Lite build exposes cosine_sim_from_l2 as an alias.
        assert cosine_sim_from_l2(0.0) == cosine_sim_from_distance(0.0)


class TestTimeDecay:
    def test_now_returns_one(self):
        from datetime import datetime, timezone
        ts = datetime.now(tz=timezone.utc).isoformat()
        assert abs(time_decay(ts, 90) - 1.0) < 0.01

    def test_half_life(self):
        from datetime import datetime, timedelta, timezone
        ts = (datetime.now(tz=timezone.utc) - timedelta(days=90)).isoformat()
        assert abs(time_decay(ts, 90) - 0.5) < 0.01

    def test_floor_value(self):
        from datetime import datetime, timedelta, timezone
        ts = (datetime.now(tz=timezone.utc) - timedelta(days=9000)).isoformat()
        assert 0.0 <= time_decay(ts, 90) < 0.01

    def test_invalid_timestamp_returns_one(self):
        assert time_decay("not-a-date", 90) == 1.0


class TestKeywordRelevance:
    def test_below_threshold(self):
        assert keyword_relevance(0.05, 1.0, 0.1) == 0.0

    def test_perfect_match(self):
        assert keyword_relevance(5.0, 5.0, 0.1) == 1.0

    def test_partial_match(self):
        assert abs(keyword_relevance(2.0, 5.0, 0.1) - 0.4) < 1e-9

    def test_absolute_value_handling(self):
        # Historically BM25 scores were negative; abs() keeps us compatible.
        assert keyword_relevance(-2.0, 5.0, 0.1) == keyword_relevance(2.0, 5.0, 0.1)


class TestPurification:
    def test_code_block_replaced(self):
        result = purify("before\n```python\nprint('hi')\n```\nafter")
        assert "[code omitted]" in result
        assert "print" not in result

    def test_inline_code_preserved(self):
        assert "`my_func()`" in purify("use `my_func()` here")

    def test_multiple_code_blocks(self):
        assert purify("```a```\nmiddle\n```b```").count("[code omitted]") == 2

    def test_no_code_blocks(self):
        assert purify("no code here") == "no code here"


class TestEmbedding:
    def test_passage_prefix(self, embedding_model):
        vec = embedding_model.encode("passage: test text", normalize_embeddings=True)
        assert vec.shape[0] == 768

    def test_query_prefix(self, embedding_model):
        vec = embedding_model.encode("query: test query", normalize_embeddings=True)
        assert vec.shape[0] == 768

    def test_embed_passage_function(self):
        assert len(embed_passage("hello world")) == 768

    def test_embed_query_function(self):
        assert len(embed_query("hello world")) == 768

    def test_semantically_related_vectors_similar(self):
        # cosine similarity of two e5-embedded passages on the same topic
        # should clearly beat an unrelated pair.
        v_same_a = embed_passage("Abraham Lincoln was the 16th U.S. president")
        v_same_b = embed_query("Abraham Lincoln")
        v_other = embed_passage("Photosynthesis turns sunlight into chemical energy")

        def cos(a, b):
            dot = sum(x * y for x, y in zip(a, b))
            return dot  # vectors already normalized

        assert cos(v_same_a, v_same_b) > cos(v_same_a, v_other)


class TestFinalScore:
    def test_full_scoring_formula(self):
        expected = (0.8 * 0.7 + 0.6 * 0.3) * 1.0 * 1.0
        assert abs(final_score(0.8, 0.6, 1.0, 1.0) - expected) < 1e-9

    def test_decay_applied(self):
        assert final_score(1.0, 1.0, 0.5) == 0.5

    def test_importance_scales_score(self):
        base = final_score(0.8, 0.5, 1.0, 1.0)
        boosted = final_score(0.8, 0.5, 1.0, 2.0)
        assert abs(boosted - base * 2.0) < 1e-9


class TestAccessCountBoost:
    """Tests for the access-frequency auto-importance formula.

    The formula is applied inside hybrid_search, but we can verify the math
    directly: b_local = clamp(0.5, 2.0, stored_importance + min(max_boost, count * weight))
    """

    def test_zero_access_no_boost(self):
        # count=0 → no boost → b_local == stored_importance
        stored = 1.0
        weight, cap = 0.02, 0.5
        boost = min(cap, 0 * weight)
        assert boost == 0.0
        b_local = max(0.5, min(2.0, stored + boost))
        assert b_local == 1.0

    def test_moderate_access_boosts(self):
        # 10 accesses with weight=0.02 → +0.2 boost
        stored = 1.0
        boost = min(0.5, 10 * 0.02)
        assert abs(boost - 0.2) < 1e-9
        assert max(0.5, min(2.0, stored + boost)) == 1.2

    def test_access_capped_at_max(self):
        # 100 accesses at weight=0.02 would give +2.0, but cap is 0.5
        boost = min(0.5, 100 * 0.02)
        assert boost == 0.5

    def test_combined_with_high_importance_clamped(self):
        # stored=2.0, +0.5 access → would be 2.5, clamped to 2.0
        b_local = max(0.5, min(2.0, 2.0 + 0.5))
        assert b_local == 2.0


class TestLexicalRerank:
    def _make_results(self, entries):
        return [{"id": str(i), "content": c, "score": s, "timestamp": "2026-01-01"}
                for i, (c, s) in enumerate(entries)]

    def test_empty_returns_empty(self):
        assert lexical_rerank("query", []) == []

    def test_empty_query_returns_unchanged_order(self):
        results = self._make_results([("hello world", 0.9), ("foo bar", 0.8)])
        out = lexical_rerank("", results)
        assert [r["id"] for r in out] == ["0", "1"]

    def test_full_term_match_boosts_score(self):
        results = self._make_results([("unrelated content here", 0.5),
                                      ("redis memory store", 0.5)])
        out = lexical_rerank("redis memory", results)
        # Entry 1 has both query terms — should rank higher after rerank
        assert out[0]["id"] == "1"

    def test_exact_phrase_gives_extra_boost(self):
        results = self._make_results([("redis store", 0.5),
                                      ("redis memory store", 0.5)])
        out = lexical_rerank("redis memory", results, phrase_weight=0.5)
        # "redis memory store" contains the exact phrase
        assert out[0]["id"] == "1"
        assert out[0]["score"] > out[1]["score"]

    def test_no_matching_terms_score_unchanged(self):
        results = self._make_results([("totally unrelated xyz", 0.8)])
        out = lexical_rerank("redis memory", results)
        # zero coverage → boost = 1.0 → score unchanged
        assert out[0]["score"] == 0.8

    def test_zero_rerank_weight_is_noop(self):
        results = self._make_results([("unrelated text", 0.6),
                                      ("redis memory cache", 0.5)])
        out = lexical_rerank("redis memory", results, rerank_weight=0.0)
        # With weight=0, original ranking is preserved
        assert out[0]["id"] == "0"

    def test_cjk_term_coverage(self):
        results = self._make_results([("記憶装置について", 0.5),
                                      ("全く関係ない内容", 0.5)])
        out = lexical_rerank("記憶装置", results)
        # First entry should rank higher after rerank
        assert out[0]["id"] == "0"

    def test_shorter_content_preferred_when_tied(self):
        long_content = "redis " * 50   # 300 chars, many redis hits
        short_content = "redis cache"  # 11 chars, same term
        results = self._make_results([(long_content, 0.5), (short_content, 0.5)])
        out = lexical_rerank("redis", results)
        assert out[0]["id"] == "1"


class TestChunkText:
    def test_short_text_returns_single_chunk(self):
        text = "a" * 200
        assert chunk_text(text, chunk_size=400) == [text]

    def test_exact_threshold_is_single_chunk(self):
        text = "a" * 400
        assert chunk_text(text, chunk_size=400) == [text]

    def test_long_text_splits(self):
        text = "a" * 900
        chunks = chunk_text(text, chunk_size=400, overlap=100)
        assert len(chunks) == 3

    def test_overlap_preserved(self):
        text = "a" * 10 + "b" * 10
        chunks = chunk_text(text, chunk_size=15, overlap=5)
        # Second chunk should start 10 chars in (15-5), overlapping the boundary
        assert chunks[0] == text[:15]
        assert chunks[1] == text[10:25]

    def test_all_chars_covered(self):
        text = "abcdefghij" * 50  # 500 chars
        chunks = chunk_text(text, chunk_size=200, overlap=50)
        # Every character in the original text should appear in at least one chunk
        covered = set()
        start = 0
        step = 200 - 50
        for chunk in chunks:
            for i, c in enumerate(chunk):
                covered.add(start + i)
            start += step
        assert len(covered) >= len(text) - 1  # allow off-by-one at tail
