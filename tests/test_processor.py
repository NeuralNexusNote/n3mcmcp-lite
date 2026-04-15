"""
Ranking math, purification, embedding tests for n3mc_mcp.processor (Lite build).
"""
from n3mc_mcp.processor import (
    cosine_sim_from_distance,
    cosine_sim_from_l2,
    embed_passage,
    embed_query,
    final_score,
    keyword_relevance,
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
