"""Layer 2: ranking math, purify, chunking, reranker, tokenization, encoding safety (no Redis, no model)."""

import math
import time

import pytest

from n3mc_mcp import config, processor
from n3mc_mcp.processor import (b_local, chunk_text, cjk_bigram_expand, cos_sim_from_distance,
                                drop_lone_cjk_tokens, final_score, has_visible_text, keyword_relevance,
                                prepare_query, purify, rerank_bonus, sanitize_surrogates, time_decay)

CFG = dict(config.DEFAULTS)


class TestCosineSim:
    def test_distance_to_similarity(self):
        assert cos_sim_from_distance(0.0) == 1.0
        assert cos_sim_from_distance(1.0) == 0.0

    def test_negative_clamped(self):
        assert cos_sim_from_distance(1.7) == 0.0
        assert cos_sim_from_distance(-0.3) == 1.0

    def test_vectors(self):
        assert processor.cosine_sim_vectors([1, 0], [1, 0]) == pytest.approx(1.0)
        assert processor.cosine_sim_vectors([1, 0], [0, 1]) == pytest.approx(0.0)


class TestTimeDecay:
    def test_now_is_one(self):
        now = time.time()
        assert time_decay(now, now, 3) == pytest.approx(1.0)

    def test_half_life(self):
        now = time.time()
        assert time_decay(now - 3 * 86400, now, 3) == pytest.approx(0.5)
        assert time_decay(now - 7 * 86400, now, 3) == pytest.approx(0.198, abs=0.01)

    def test_invalid_timestamp(self):
        assert time_decay("not a number", time.time(), 3) == 1.0
        assert time_decay(None, time.time(), 3) == 1.0


class TestKeywordRelevance:
    def test_below_threshold_is_zero(self):
        assert keyword_relevance(0.05, 1.0, 0.1) == 0.0

    def test_normalized_by_max(self):
        assert keyword_relevance(2.0, 4.0, 0.1) == pytest.approx(0.5)

    def test_zero_max_uses_one(self):
        assert keyword_relevance(0.5, 0.0, 0.1) == pytest.approx(0.5)

    def test_abs(self):
        assert keyword_relevance(-2.0, 4.0, 0.1) == pytest.approx(0.5)


class TestFinalScore:
    def test_formula(self):
        assert final_score(0.8, 0.5, 0.5, 1.0, 1.0) == pytest.approx((0.8 * 0.7 + 0.5 * 0.3) * 0.5)

    def test_b_local_clamp(self):
        assert b_local(5.0, 0, CFG) == 2.0
        assert b_local(0.1, 0, CFG) == 0.5

    def test_b_session(self):
        assert processor.b_session("a", "a", CFG) == 1.0
        assert processor.b_session("a", "b", CFG) == 0.6
        assert processor.b_session("a", "", CFG) == 0.6


class TestAccessCountBoost:
    def test_disabled_by_default(self):
        assert b_local(1.0, 10, CFG) == 1.0

    def test_enabled_adds_capped_boost(self):
        cfg = dict(CFG, access_count_enabled=True)
        assert b_local(1.0, 10, cfg) == pytest.approx(1.2)
        assert b_local(1.0, 1000, cfg) == pytest.approx(1.5)
        assert b_local(1.8, 1000, cfg) == 2.0


class TestLexicalRerank:
    def test_phrase_and_coverage(self):
        long_text = "The quick brown fox jumps over the lazy dog near the river bank today."
        hit = rerank_bonus("quick brown fox", long_text, CFG)
        miss = rerank_bonus("quick brown fox", "Something else entirely about cooking rice and soup here ok.", CFG)
        assert hit > miss >= 0.0

    def test_short_text_is_damped(self):
        full = rerank_bonus("quick fox", "quick fox", CFG)
        padded = rerank_bonus("quick fox", "quick fox " + "x" * 60, CFG)
        assert full < padded

    def test_zero_coverage_not_negative(self):
        assert rerank_bonus("zzz", "nothing in common here with this long sentence of text ok", CFG) == 0.0

    def test_punctuation_does_not_block_coverage(self):
        a = rerank_bonus("note", "my note, is here and it is long enough to not be damped at all", CFG)
        b = rerank_bonus("note", "my note is here and it is long enough to not be damped at all x", CFG)
        assert a == pytest.approx(b, abs=1e-9)


class TestPurification:
    def test_multiline_block(self):
        assert purify("a\n```python\nx = 1\ny = 2\n```\nb") == "a\n[code omitted]\nb"

    def test_inline_code_kept(self):
        assert purify("use `ls` here") == "use `ls` here"

    def test_multiple_blocks(self):
        assert purify("```a``` x ```b```") == "[code omitted] x [code omitted]"

    def test_unclosed_fence_runs_to_end(self):
        assert purify("keep ```never closed\nmore") == "keep [code omitted]"


class TestChunkText:
    def test_short_is_single(self):
        assert chunk_text("abc", 400, 100) == ["abc"]

    def test_long_is_split_with_overlap(self):
        text = "".join("%04d " % i for i in range(400))
        chunks = chunk_text(text, 400, 100)
        assert len(chunks) > 1
        assert all(len(c) <= 400 + 19 for c in chunks)
        assert text.startswith(chunks[0])

    def test_every_char_lands_in_a_chunk(self):
        text = "x" * 399 + "\n" + "PIN: 4471-ZX."
        assert any("PIN: 4471-ZX." in c for c in chunk_text(text, 400, 100))

    def test_short_tail_joins_previous_chunk(self):
        text = "a" * 400 + "b" * 5
        chunks = chunk_text(text, 400, 100)
        assert chunks[-1].endswith("bbbbb")

    def test_terminates_when_overlap_ge_threshold(self):
        chunks = chunk_text("y" * 500, 100, 100)
        assert chunks and "".join(chunks).count("y") >= 500

    def test_breaks_at_paragraph(self):
        text = ("p" * 350) + "\n\n" + ("q" * 300)
        chunks = chunk_text(text, 400, 100)
        assert chunks[0].endswith("\n\n")


class TestEncodingSafety:
    def test_lone_surrogates_removed(self):
        assert sanitize_surrogates("a\ud800b\udfffc") == "abc"
        assert sanitize_surrogates(["a\ud800", {"k": "b\udc00"}]) == ["a", {"k": "b"}]
        assert sanitize_surrogates(None) is None
        assert sanitize_surrogates(5) == 5

    def test_all_surrogates_degrade_to_empty(self):
        assert sanitize_surrogates("\ud800\ud801") == ""
        assert sanitize_surrogates("x\ud800").encode("utf-8") == b"x"

    def test_visible_text(self):
        assert not has_visible_text("\x00")
        assert not has_visible_text("​")
        assert not has_visible_text("ㅤ⠀")
        assert has_visible_text("A\x00B")
        assert has_visible_text("゚")


class TestTokenization:
    def test_bigrams(self):
        assert cjk_bigram_expand("記憶装置") == "記憶 憶装 装置"
        assert cjk_bigram_expand("a") == "a"
        assert cjk_bigram_expand("字") == "字"

    def test_diacritics_folded(self):
        assert "cafe" in cjk_bigram_expand("café").split()

    def test_prepare_query_strips_cjk_punctuation(self):
        assert prepare_query("「記憶装置」は？") == "記憶 憶装 装置 は"
        assert prepare_query("a\x00b _ !!") == "a b"

    def test_drop_lone_cjk_and_hiragana(self):
        assert drop_lone_cjk_tokens("記憶 憶装 装置 は") == "記憶 憶装 装置"
        assert drop_lone_cjk_tokens("して ます 天気") == "天気"
        assert drop_lone_cjk_tokens("は") == "は"

    def test_fullwidth_normalized(self):
        assert prepare_query("ＡＩ ２０２６") == "AI 2026"


class TestSha1:
    def test_nfkc_applied(self):
        assert processor.sha1_hex("ＡＩ") == processor.sha1_hex("AI")
        assert len(processor.sha1_hex("")) == 40


@pytest.mark.slow
class TestEmbedding:
    def test_real_model(self):
        import os
        from conftest import REAL_HF_HOME
        if not os.path.isdir(os.path.join(REAL_HF_HOME, "hub", "models--intfloat--multilingual-e5-base")):
            pytest.skip("multilingual-e5-base is not cached")
        os.environ["HF_HOME"] = REAL_HF_HOME
        cfg = dict(CFG)
        a = processor.embed("今日は晴れです", cfg)
        b = processor.embed("今日は晴れです", cfg)
        assert len(a) == 768
        assert math.sqrt(sum(x * x for x in a)) == pytest.approx(1.0, abs=1e-3)
        assert processor.cosine_sim_vectors(a, b) > 0.999
