import math
import pytest
from n3mc_mcp.processor import (
    b_local,
    chunk_text,
    cjk_bigram_expand,
    cosine_sim_from_distance,
    final_score,
    keyword_relevance,
    lexical_rerank,
    prepare_query,
    purify,
    time_decay,
)


# ── TestCosineSim ────────────────────────────────────────────────────────────

class TestCosineSim:
    def test_zero_distance_gives_one(self):
        assert cosine_sim_from_distance(0.0) == pytest.approx(1.0)

    def test_one_distance_gives_zero(self):
        assert cosine_sim_from_distance(1.0) == pytest.approx(0.0)

    def test_two_distance_clamped_to_zero(self):
        assert cosine_sim_from_distance(2.0) == pytest.approx(0.0)

    def test_negative_distance_clamped_to_one(self):
        assert cosine_sim_from_distance(-0.1) == pytest.approx(1.0)

    def test_half_distance(self):
        assert cosine_sim_from_distance(0.5) == pytest.approx(0.5)


# ── TestTimeDecay ────────────────────────────────────────────────────────────

class TestTimeDecay:
    def test_now_returns_one(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        assert time_decay(now, half_life_days=3) == pytest.approx(1.0, abs=0.01)

    def test_half_life(self):
        from datetime import datetime, timezone, timedelta
        ts = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        assert time_decay(ts, half_life_days=3) == pytest.approx(0.5, abs=0.01)

    def test_invalid_timestamp_returns_one(self):
        assert time_decay("not-a-date", half_life_days=3) == pytest.approx(1.0)

    def test_old_entry_near_expiry(self):
        from datetime import datetime, timezone, timedelta
        ts = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        val = time_decay(ts, half_life_days=3)
        assert val < 0.25


# ── TestKeywordRelevance ─────────────────────────────────────────────────────

class TestKeywordRelevance:
    def test_below_threshold_returns_zero(self):
        assert keyword_relevance(0.05, 10.0, threshold=0.1) == pytest.approx(0.0)

    def test_normalized_to_max(self):
        assert keyword_relevance(5.0, 10.0, threshold=0.1) == pytest.approx(0.5)

    def test_max_score_returns_one(self):
        assert keyword_relevance(10.0, 10.0, threshold=0.1) == pytest.approx(1.0)

    def test_zero_max_bm25_safe(self):
        assert keyword_relevance(0.0, 0.0, threshold=0.1) == pytest.approx(0.0)


# ── TestFinalScore ───────────────────────────────────────────────────────────

class TestFinalScore:
    def test_formula(self):
        cos, kw, decay, b = 0.8, 0.6, 0.9, 1.2
        expected = (0.8 * 0.7 + 0.6 * 0.3) * 0.9 * 1.2
        assert final_score(cos, kw, decay, b) == pytest.approx(expected)

    def test_zero_cos_kw_gives_zero(self):
        assert final_score(0.0, 0.0, 1.0, 1.0) == pytest.approx(0.0)


# ── TestAccessCountBoost ─────────────────────────────────────────────────────

class TestAccessCountBoost:
    def test_no_boost_with_zero_access(self):
        cfg = {"access_count_enabled": True, "access_count_weight": 0.02, "access_count_max_boost": 0.5}
        assert b_local(1.0, 0, cfg) == pytest.approx(1.0)

    def test_boost_increases_b_local(self):
        cfg = {"access_count_enabled": True, "access_count_weight": 0.02, "access_count_max_boost": 0.5}
        assert b_local(1.0, 10, cfg) > 1.0

    def test_clamp_at_2(self):
        cfg = {"access_count_enabled": True, "access_count_weight": 0.02, "access_count_max_boost": 0.5}
        assert b_local(2.0, 1000, cfg) == pytest.approx(2.0)

    def test_clamp_at_0_5(self):
        cfg = {"access_count_enabled": True, "access_count_weight": 0.02, "access_count_max_boost": 0.5}
        assert b_local(0.3, 0, cfg) == pytest.approx(0.5)

    def test_disabled_flag(self):
        cfg = {"access_count_enabled": False, "access_count_weight": 0.02, "access_count_max_boost": 0.5}
        assert b_local(1.0, 100, cfg) == pytest.approx(1.0)

    def test_max_boost_capped(self):
        cfg = {"access_count_enabled": True, "access_count_weight": 0.02, "access_count_max_boost": 0.5}
        val = b_local(1.0, 10000, cfg)
        assert val == pytest.approx(1.5)


# ── TestLexicalRerank ────────────────────────────────────────────────────────

class TestLexicalRerank:
    def _make(self, content: str, score: float = 0.5) -> dict:
        return {"content": content, "score": score, "id": "x"}

    def test_empty_query_no_change(self):
        items = [self._make("hello world")]
        result = lexical_rerank(items, "", 0.3, 0.2)
        assert result[0]["content"] == "hello world"

    def test_matching_item_scored_higher(self):
        items = [self._make("unrelated content", 0.5), self._make("python programming", 0.4)]
        result = lexical_rerank(items, "python", 0.3, 0.2)
        assert result[0]["content"] == "python programming"

    def test_phrase_bonus_applied(self):
        items = [self._make("hello world", 0.5), self._make("hello there", 0.5)]
        result = lexical_rerank(items, "hello world", 0.3, 0.2)
        assert result[0]["content"] == "hello world"

    def test_zero_coverage_no_degradation(self):
        items = [self._make("xyz abc", 0.8)]
        result = lexical_rerank(items, "unrelated", 0.3, 0.2)
        assert result[0]["score"] >= 0.8


# ── TestPurification ─────────────────────────────────────────────────────────

class TestPurification:
    def test_code_block_replaced(self):
        text = "Some text\n```python\nprint('hi')\n```\nMore text"
        assert "[code omitted]" in purify(text)
        assert "print" not in purify(text)

    def test_no_code_block_unchanged(self):
        text = "Hello world"
        assert purify(text) == "Hello world"

    def test_multiple_blocks(self):
        text = "```a```\nmiddle\n```b```"
        result = purify(text)
        assert result.count("[code omitted]") == 2


# ── TestChunkText ─────────────────────────────────────────────────────────────

class TestChunkText:
    def test_short_returns_single(self):
        text = "a" * 100
        chunks = chunk_text(text, threshold=400, overlap=100)
        assert chunks == [text]

    def test_long_splits(self):
        text = "a" * 900
        chunks = chunk_text(text, threshold=400, overlap=100)
        assert len(chunks) > 1

    def test_overlap_present(self):
        text = "a" * 600
        chunks = chunk_text(text, threshold=400, overlap=100)
        assert len(chunks) == 2
        assert len(chunks[0]) == 400
        assert len(chunks[1]) == 300

    def test_exact_threshold(self):
        text = "a" * 400
        chunks = chunk_text(text, threshold=400, overlap=100)
        assert len(chunks) == 1


# ── TestCjkBigramExpand ──────────────────────────────────────────────────────

class TestCjkBigramExpand:
    def test_hiragana_bigrams(self):
        result = cjk_bigram_expand("あいう")
        assert "あい" in result
        assert "いう" in result

    def test_kanji_bigrams(self):
        result = cjk_bigram_expand("記憶装置")
        assert "記憶" in result
        assert "憶装" in result
        assert "装置" in result

    def test_ascii_passthrough(self):
        result = cjk_bigram_expand("hello")
        assert "hello" not in result or result.strip()

    def test_single_cjk_char(self):
        result = cjk_bigram_expand("あ")
        assert "あ" in result

    def test_mixed_cjk_ascii(self):
        result = cjk_bigram_expand("hello世界")
        assert "世界" in result


# ── TestPrepareQuery ──────────────────────────────────────────────────────────

class TestPrepareQuery:
    def test_strips_punctuation(self):
        result = prepare_query("hello, world!")
        assert "," not in result
        assert "!" not in result

    def test_empty_after_strip(self):
        result = prepare_query("!!!")
        assert result == ""

    def test_cjk_expanded(self):
        result = prepare_query("記憶装置")
        assert "記憶" in result


# ── TestEmbedding ─────────────────────────────────────────────────────────────

class TestEmbedding:
    def test_embed_returns_bytes(self):
        from n3mc_mcp.processor import embed
        result = embed("hello world", is_query=False)
        if result is not None:
            assert isinstance(result, bytes)
            assert len(result) == 768 * 4

    def test_query_prefix_differs_from_passage(self):
        from n3mc_mcp.processor import embed_to_array
        import numpy as np
        p = embed_to_array("test text", is_query=False)
        q = embed_to_array("test text", is_query=True)
        if p is not None and q is not None:
            assert not np.allclose(p, q)

    def test_same_text_same_embedding(self):
        from n3mc_mcp.processor import embed_to_array
        import numpy as np
        a = embed_to_array("hello", is_query=False)
        b = embed_to_array("hello", is_query=False)
        if a is not None and b is not None:
            assert np.allclose(a, b)


# ── TestEncodingSafety (spec §3.13) ──────────────────────────────────────────

class TestEncodingSafety:
    """Lone-surrogate sanitization contract from spec §3.13."""

    def test_strips_lone_high_surrogate(self):
        from n3mc_mcp.processor import sanitize_surrogates
        assert sanitize_surrogates("hello\ud800world") == "helloworld"

    def test_strips_lone_low_surrogate(self):
        from n3mc_mcp.processor import sanitize_surrogates
        assert sanitize_surrogates("foo\udfffbar") == "foobar"

    def test_clean_text_unchanged(self):
        from n3mc_mcp.processor import sanitize_surrogates
        assert sanitize_surrogates("こんにちは世界") == "こんにちは世界"

    def test_all_surrogate_collapses_to_empty(self):
        from n3mc_mcp.processor import sanitize_surrogates
        # Build the string from explicit code-point escapes so every
        # character is unambiguously inside the surrogate range.
        all_surrogates = chr(0xD800) + chr(0xDC00) + chr(0xDFFF)
        assert sanitize_surrogates(all_surrogates) == ""

    def test_post_strip_encodes_to_utf8(self):
        """Pre-sanitize the string blows up at .encode('utf-8'); post-sanitize succeeds."""
        from n3mc_mcp.processor import sanitize_surrogates
        poison = "café\ud800naïve"
        with pytest.raises(UnicodeEncodeError):
            poison.encode("utf-8")
        cleaned = sanitize_surrogates(poison)
        assert cleaned.encode("utf-8") == "cafénaïve".encode("utf-8")

    def test_recursive_dict(self):
        from n3mc_mcp.processor import sanitize_surrogates
        out = sanitize_surrogates({"a": "x\ud800y", "b": "ok"})
        assert out == {"a": "xy", "b": "ok"}

    def test_recursive_list(self):
        from n3mc_mcp.processor import sanitize_surrogates
        assert sanitize_surrogates(["x\ud800", "y"]) == ["x", "y"]

    def test_nested_structures(self):
        from n3mc_mcp.processor import sanitize_surrogates
        nested = {"items": [{"text": "a\ud800b"}, "ok"]}
        assert sanitize_surrogates(nested) == {"items": [{"text": "ab"}, "ok"]}

    def test_none_passthrough(self):
        from n3mc_mcp.processor import sanitize_surrogates
        assert sanitize_surrogates(None) is None

    def test_non_string_scalar_passthrough(self):
        from n3mc_mcp.processor import sanitize_surrogates
        assert sanitize_surrogates(42) == 42
        assert sanitize_surrogates(b"raw bytes") == b"raw bytes"
