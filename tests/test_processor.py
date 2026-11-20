"""Layer 2: ranking math, purify, chunking, tokenization, encoding safety (no Redis, no model)."""

import time

from n3mc_mcp import processor as P


class TestCosineSim:
    def test_identical_and_orthogonal(self):
        assert abs(P.cosine_sim([1, 0], [1, 0]) - 1.0) < 1e-6
        assert P.cosine_sim([1, 0], [0, 1]) == 0.0

    def test_distance_conversion_clamps(self):
        assert P.cos_from_distance(0.0) == 1.0
        assert P.cos_from_distance(1.5) == 0.0
        assert P.cos_from_distance(None) == 0.0


class TestTimeDecay:
    def test_now_and_half_life(self):
        now = time.time()
        assert abs(P.time_decay(now, 3, now) - 1.0) < 1e-9
        assert abs(P.time_decay(now - 3 * 86400, 3, now) - 0.5) < 1e-9

    def test_bad_timestamp_is_one(self):
        assert P.time_decay("bad", 3) == 1.0


class TestKeywordRelevance:
    def test_threshold_and_normalization(self):
        assert P.keyword_relevance(0.05, 2.0, 0.1) == 0.0
        assert P.keyword_relevance(1.0, 2.0, 0.1) == 0.5
        assert P.keyword_relevance(0.5, 0.5, 0.1) == 0.5   # max(1.0, max) floor


class TestBLocal:
    def test_clamp_and_boost(self):
        cfg = {"access_count_enabled": True, "access_count_weight": 0.02, "access_count_max_boost": 0.5}
        assert P.b_local(1.0, 10, cfg) == 1.2
        assert P.b_local(1.9, 100, cfg) == 2.0
        assert P.b_local(1.0, 10, {"access_count_enabled": False}) == 1.0


class TestLexicalRerank:
    def test_coverage_counts_punctuated_words(self):
        cfg = {"rerank_weight": 0.3, "rerank_phrase_weight": 0.2}
        long_tail = " " + "x" * 60
        assert P.rerank_bonus("note", "a note," + long_tail, cfg) > 0.0

    def test_short_content_is_damped(self):
        cfg = {"rerank_weight": 0.3, "rerank_phrase_weight": 0.2}
        assert P.rerank_bonus("abc", "abc", cfg) < P.rerank_bonus("abc", "abc " + "y" * 60, cfg)


class TestPurification:
    def test_blocks_replaced_inline_kept(self):
        t = "a ```py\nx=1\n``` b `inline` ```open"
        assert P.purify(t) == "a [code omitted] b `inline` [code omitted]"


class TestChunkText:
    def test_short_single(self):
        assert P.chunk_text("abc", 400, 100) == ["abc"]

    def test_every_char_covered_and_tail_merged(self):
        text = ("word " * 200).strip() + " PIN: 4471-ZX."
        chunks = P.chunk_text(text, 100, 20)
        assert "PIN: 4471-ZX." in chunks[-1]
        assert all(len(c) >= 20 for c in chunks)

    def test_overlap_not_less_than_threshold_terminates(self):
        chunks = P.chunk_text("x" * 1000, 100, 100)
        assert len(chunks) > 1


class TestTokenize:
    def test_prepare_query_strips_cjk_punctuation(self):
        assert P.prepare_query("「記憶装置」は？") == "記憶 憶装 装置 は"

    def test_drop_lone_and_hiragana(self):
        assert P.drop_lone_cjk_tokens("記憶 憶装 装置 は") == "記憶 憶装 装置"
        assert P.drop_lone_cjk_tokens("どう") == "どう"

    def test_bigram_mixed(self):
        assert P.cjk_bigram_expand("アルファ9") == "アル ルフ ファ 9"

    def test_ngram_folds_diacritics(self):
        assert "cafe" in P.build_ngram("café").split()


class TestEncodingSafety:
    def test_sanitize_nested(self):
        assert P.sanitize_surrogates({"a": ["x\ud800y"], "b": None}) == {"a": ["xy"], "b": None}
        assert P.sanitize_surrogates("\udc00\udc01") == ""

    def test_visible_text(self):
        assert not P.has_visible_text("\x00​﻿ ㅤ⠀")
        assert P.has_visible_text("A\x00B")
