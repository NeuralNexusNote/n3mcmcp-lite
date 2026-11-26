"""Layer 2: ranking math, embeddings, purify, chunking, reranker, encoding safety (spec §9 Layer 2)."""
import time

import pytest

from n3mc_mcp import processor as P


class TestCosineSim:
    def test_identical_is_one(self):
        assert P.cos_from_distance(0.0) == 1.0

    def test_orthogonal_is_zero(self):
        assert P.cos_from_distance(1.0) == 0.0

    def test_negative_is_clamped(self):
        assert P.cos_from_distance(1.7) == 0.0
        assert P.cos_from_distance(-0.2) == 1.0

    def test_distance_to_similarity(self):
        assert P.cos_from_distance(0.25) == pytest.approx(0.75)

    def test_unreadable_distance(self):
        assert P.cos_from_distance(None) == 0.0
        assert P.cos_from_distance("x") == 0.0


class TestTimeDecay:
    def test_now_is_one(self):
        now = time.time()
        assert P.time_decay(now, 3, now=now) == pytest.approx(1.0)

    def test_half_life(self):
        now = time.time()
        assert P.time_decay(now - 3 * 86400, 3, now=now) == pytest.approx(0.5)

    def test_seven_days(self):
        now = time.time()
        assert P.time_decay(now - 7 * 86400, 3, now=now) == pytest.approx(0.198, abs=0.002)

    def test_future_is_floored_at_one(self):
        now = time.time()
        assert P.time_decay(now + 86400, 3, now=now) == 1.0

    def test_bad_timestamp_is_one(self):
        assert P.time_decay("not a time", 3) == 1.0
        assert P.time_decay(None, 3) == 1.0


class TestKeywordRelevance:
    def test_below_threshold_is_zero(self):
        assert P.keyword_relevance(0.05, 2.0, 0.1) == 0.0

    def test_normalized_by_max(self):
        assert P.keyword_relevance(1.0, 2.0, 0.1) == pytest.approx(0.5)

    def test_max_below_one_divides_by_one(self):
        assert P.keyword_relevance(0.5, 0.5, 0.1) == pytest.approx(0.5)

    def test_zero_max(self):
        assert P.keyword_relevance(0.0, 0.0, 0.1) == 0.0

    def test_abs(self):
        assert P.keyword_relevance(-1.0, 2.0, 0.1) == pytest.approx(0.5)


class TestFinalScore:
    def test_formula(self):
        assert P.final_score(0.9, 0.5, 0.5, 1.0, 1.0) == pytest.approx((0.63 + 0.15) * 0.5)

    def test_b_session_multiplies(self):
        assert P.final_score(1.0, 1.0, 1.0, 1.0, 0.6) == pytest.approx(0.6)

    def test_b_local_clamped_low(self):
        assert P.b_local(0.1, 0, False, 0.02, 0.5) == 0.5

    def test_b_local_clamped_high(self):
        assert P.b_local(1.9, 100, True, 0.02, 0.5) == 2.0


class TestAccessCountBoost:
    def test_boost_added(self):
        assert P.b_local(1.0, 10, True, 0.02, 0.5) == pytest.approx(1.2)

    def test_boost_capped(self):
        assert P.b_local(1.0, 1000, True, 0.02, 0.5) == pytest.approx(1.5)

    def test_disabled(self):
        assert P.b_local(1.0, 1000, False, 0.02, 0.5) == pytest.approx(1.0)

    def test_nan_importance(self):
        assert P.b_local(float("nan"), 0, False, 0.02, 0.5) == 1.0


class TestLexicalRerank:
    def test_coverage(self):
        b = P.lexical_bonus("alpha beta", "alpha gamma " * 10, 0.3, 0.0)
        assert b == pytest.approx(0.15)

    def test_phrase_bonus(self):
        content = "the quick brown fox jumps over the lazy dog, again and again and again"
        assert P.lexical_bonus("quick brown", content, 0.0, 0.2) == pytest.approx(0.2)

    def test_phrase_is_case_insensitive(self):
        content = "The Quick Brown fox jumps over the lazy dog, again and again and again"
        assert P.lexical_bonus("quick brown", content, 0.0, 0.2) == pytest.approx(0.2)

    def test_short_content_is_damped(self):
        long_c = "alpha " + "x" * 60
        short_c = "alpha xx"
        assert P.lexical_bonus("alpha", short_c, 0.3, 0.2) < P.lexical_bonus("alpha", long_c, 0.3, 0.2)

    def test_zero_coverage_is_zero(self):
        assert P.lexical_bonus("zzz", "alpha beta " * 10, 0.3, 0.2) == 0.0

    def test_punctuation_counts(self):
        assert "note" in P.rerank_tokens("a note, here")

    def test_cjk_coverage(self):
        assert P.lexical_bonus("記憶装置", "これは記憶装置の説明です。" * 5, 0.3, 0.0) == pytest.approx(0.3)


class TestPurification:
    def test_multiline_block(self):
        assert P.purify("a\n```py\nx=1\n```\nb") == "a\n[code omitted]\nb"

    def test_inline_kept(self):
        assert P.purify("use `x` here") == "use `x` here"

    def test_several_blocks(self):
        assert P.purify("```a```m```b```") == "[code omitted]m[code omitted]"

    def test_unclosed_fence(self):
        assert P.purify("x\n```\nrest of it") == "x\n[code omitted]"


class TestChunkText:
    def test_short_is_single(self):
        assert P.chunk_text("abc", 400, 100) == ["abc"]

    def test_long_is_split_and_covers(self):
        text = "".join(f"Sentence number {i}. " for i in range(200))
        chunks = P.chunk_text(text, 400, 100)
        assert len(chunks) > 1
        assert all(len(c) <= 400 + 19 for c in chunks)
        joined = "".join(chunks)
        for i in range(200):
            assert f"number {i}." in joined

    def test_forced_cut_overlaps(self):
        text = "x" * 1000
        chunks = P.chunk_text(text, 400, 100)
        assert chunks[0] == "x" * 400
        assert len(chunks[1]) == 400   # started 100 before the end of the first

    def test_break_on_boundary(self):
        text = ("a" * 350) + "\n\n" + ("b" * 300)
        chunks = P.chunk_text(text, 400, 100)
        assert chunks[0].endswith("\n\n")
        assert chunks[1].startswith("b")

    def test_short_tail_joins_previous(self):
        text = ("a" * 390) + "\n" + "PIN: 4471-ZX."
        chunks = P.chunk_text(text, 400, 100)
        assert len(chunks) == 1 or chunks[-1].endswith("PIN: 4471-ZX.")
        assert "PIN: 4471-ZX." in chunks[-1]
        assert all(len(c) >= 20 for c in chunks)

    def test_terminates_with_overlap_equal_threshold(self):
        chunks = P.chunk_text("y" * 500, 100, 100)
        assert "".join(chunks).count("y") >= 500


class TestEmbedding:
    @pytest.mark.real_model
    @pytest.mark.slow
    def test_real_model_prefix_and_dim(self):
        try:
            import sentence_transformers  # noqa: F401
        except ImportError:
            pytest.skip("sentence_transformers not installed")
        import os
        if not os.path.isdir(os.path.join(os.environ.get("HF_HOME", ""), "hub",
                                          "models--intfloat--multilingual-e5-base")):
            pytest.skip("the model is not in the Hugging Face cache")
        v = P.embed("hello", "passage")
        q = P.embed("hello", "query")
        assert len(v) == 768 and len(q) == 768
        assert abs(sum(x * x for x in v) - 1.0) < 1e-3
        assert P.cosine(v, P.embed("hello", "passage")) > 0.999

    def test_fake_same_text_similar(self):
        a = P.embed("記憶装置のテスト", "passage")
        b = P.embed("記憶装置のテスト", "query")
        assert P.cosine(a, b) == pytest.approx(1.0)

    def test_fake_dimension(self):
        assert len(P.embed("x")) == 768


class TestEncodingSafety:
    def test_str(self):
        assert P.sanitize_surrogates("a\ud800b") == "ab"

    def test_list_and_dict(self):
        assert P.sanitize_surrogates({"k": ["a\udfffb", 1, None]}) == {"k": ["ab", 1, None]}

    def test_none(self):
        assert P.sanitize_surrogates(None) is None

    def test_all_surrogates_is_empty(self):
        assert P.sanitize_surrogates(chr(0xD800) + chr(0xDC00) + chr(0xDFFF)) == ""

    def test_encode_succeeds(self):
        P.sanitize_surrogates("x\ud83dy").encode("utf-8")


class TestVisibleText:
    @pytest.mark.parametrize("s", ["\x00", "​", "﻿", " \t\n", "ㅤ", "ᅟ", "⠀", "️"])
    def test_invisible(self, s):
        assert not P.has_visible_text(s)

    @pytest.mark.parametrize("s", ["A\x00B", "゚", "神\U000e0100", "x"])
    def test_visible(self, s):
        assert P.has_visible_text(s)


class TestQueryPreparation:
    def test_brackets_and_question(self):
        q = P.prepare_query("「記憶装置」は？")
        assert "記憶" in q.split() and "装置" in q.split()
        assert "「" not in q and "？" not in q

    def test_control_chars(self):
        assert "\x00" not in P.prepare_query("a\x00b")

    def test_lone_underscore_dropped(self):
        assert P.prepare_query("_") == ""

    def test_drop_lone_cjk(self):
        assert P.drop_lone_cjk_tokens("記憶 憶装 装置 は") == "記憶 憶装 装置"

    def test_hiragana_only_dropped(self):
        assert "どう" not in P.drop_lone_cjk_tokens("株価 はど どう").split()

    def test_fold_diacritics(self):
        assert "cafe" in P.prepare_query("café").split()

    def test_nfkc(self):
        assert P.prepare_query("ＡＩ２０２６") == "AI2026"
