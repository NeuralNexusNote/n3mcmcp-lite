"""Layer 2: ranking math, embeddings, purify, chunking, reranker, encoding safety (no Redis)."""
import math
import time

import pytest

from n3mc_mcp import processor
from n3mc_mcp.config import DEFAULTS

CFG = dict(DEFAULTS)


class TestCosineSim:
    def test_identical(self):
        assert processor.cosine_similarity([1, 2, 3], [1, 2, 3]) == pytest.approx(1.0)

    def test_orthogonal(self):
        assert processor.cosine_similarity([1, 0], [0, 1]) == 0.0

    def test_negative_clamped(self):
        assert processor.cosine_similarity([1, 0], [-1, 0]) == 0.0

    def test_distance_to_similarity(self):
        assert processor.cosine_from_distance(0.25) == pytest.approx(0.75)

    def test_distance_clamped(self):
        assert processor.cosine_from_distance(1.7) == 0.0
        assert processor.cosine_from_distance(-0.1) == 1.0
        assert processor.cosine_from_distance("garbage") == 0.0


class TestTimeDecay:
    def test_now(self):
        now = time.time()
        assert processor.time_decay(now, 3, now) == pytest.approx(1.0)

    def test_half_life(self):
        now = time.time()
        assert processor.time_decay(now - 3 * 86400, 3, now) == pytest.approx(0.5)

    def test_seven_days(self):
        now = time.time()
        assert processor.time_decay(now - 7 * 86400, 3, now) == pytest.approx(2 ** (-7 / 3))

    def test_future_floor_is_one(self):
        now = time.time()
        assert processor.time_decay(now + 86400, 3, now) == 1.0

    def test_bad_timestamp(self):
        assert processor.time_decay("not a time", 3) == 1.0
        assert processor.time_decay(None, 3) == 1.0


class TestKeywordRelevance:
    def test_below_threshold(self):
        assert processor.keyword_relevance(0.05, 2.0, 0.1) == 0.0

    def test_normalised_by_max(self):
        assert processor.keyword_relevance(1.0, 4.0, 0.1) == pytest.approx(0.25)

    def test_small_max_uses_one(self):
        assert processor.keyword_relevance(0.5, 0.5, 0.1) == pytest.approx(0.5)

    def test_none_and_zero_max(self):
        assert processor.keyword_relevance(None, 3.0, 0.1) == 0.0
        assert processor.keyword_relevance(0.3, 0.0, 0.1) == pytest.approx(0.3)


class TestFinalScore:
    def test_formula(self):
        assert processor.final_score(0.9, 0.5, 0.8, 1.0, 1.0) == pytest.approx((0.63 + 0.15) * 0.8)

    def test_session_factor(self):
        assert processor.final_score(1.0, 1.0, 1.0, 1.0, 0.6) == pytest.approx(0.6)

    def test_b_local_clamp_low(self):
        assert processor.b_local(0.1, 0, CFG) == 0.5

    def test_b_local_clamp_high(self):
        assert processor.b_local(5.0, 0, CFG) == 2.0


class TestAccessCountBoost:
    def test_disabled_by_default(self):
        assert CFG["access_count_enabled"] is False
        assert processor.b_local(1.0, 50, CFG) == 1.0

    def test_enabled_adds_boost(self):
        cfg = dict(CFG, access_count_enabled=True)
        assert processor.b_local(1.0, 10, cfg) == pytest.approx(1.2)

    def test_boost_cap(self):
        cfg = dict(CFG, access_count_enabled=True)
        assert processor.b_local(1.0, 1000, cfg) == pytest.approx(1.5)

    def test_total_clamped(self):
        cfg = dict(CFG, access_count_enabled=True)
        assert processor.b_local(1.9, 1000, cfg) == 2.0

    def test_bad_values(self):
        cfg = dict(CFG, access_count_enabled=True)
        assert processor.b_local("x", "y", cfg) == 1.0


class TestLexicalRerank:
    def test_full_coverage(self):
        content = "the quick brown fox jumps over the lazy dog every morning, it seems"
        assert processor.rerank_bonus("quick fox", content, CFG) == pytest.approx(0.3)

    def test_phrase_bonus(self):
        content = "we talked about the quick fox at length during the long meeting today"
        assert processor.rerank_bonus("the quick fox", content, CFG) == pytest.approx(0.5)

    def test_short_content_is_damped(self):
        assert processor.rerank_bonus("quick fox", "quick fox", CFG) == pytest.approx((0.3 + 0.2) * 9 / 50)

    def test_zero_coverage_never_negative(self):
        assert processor.rerank_bonus("zebra", "nothing in common with the query at all here", CFG) == 0.0

    def test_cjk_coverage_and_punctuation(self):
        content = "架空の惑星「アルファ9」の気温設定について、詳細な資料を作成して保存した。" * 2
        assert processor.rerank_bonus("アルファ9 気温", content, CFG) == pytest.approx(0.3)
        assert processor.rerank_bonus("note", "a note, written down for later use in the project", CFG) > 0


class TestPurification:
    def test_multiline_block(self):
        assert processor.purify("a\n```py\nx = 1\ny = 2\n```\nb") == "a\n[code omitted]\nb"

    def test_inline_kept(self):
        assert processor.purify("use `x` here") == "use `x` here"

    def test_several_blocks(self):
        assert processor.purify("```a```mid```b```") == "[code omitted]mid[code omitted]"

    def test_unclosed_fence(self):
        assert processor.purify("text ```python\nstill code") == "text [code omitted]"

    def test_no_code(self):
        assert processor.purify("plain") == "plain"


class TestChunkText:
    def test_short_single(self):
        assert processor.chunk_text("short", 400, 100) == ["short"]

    def test_long_split_covers_everything(self):
        text = "".join(chr(0x4E00 + i % 500) for i in range(1000))
        chunks = processor.chunk_text(text, 400, 100)
        assert len(chunks) >= 3 and all(len(c) <= 419 for c in chunks)
        assert chunks[0] + chunks[1][100:] == text[: len(chunks[0]) + len(chunks[1]) - 100]

    def test_forced_cut_overlaps(self):
        text = "x" * 900
        chunks = processor.chunk_text(text, 400, 100)
        assert chunks[0] == "x" * 400 and len(chunks[1]) == 400

    def test_break_at_paragraph(self):
        text = "a" * 350 + "\n\n" + "b" * 300
        chunks = processor.chunk_text(text, 400, 100)
        assert chunks[0] == "a" * 350 + "\n\n" and chunks[1] == "b" * 300

    def test_short_tail_joins_previous(self):
        text = "a" * 390 + "\n\n" + "PIN: 4471-ZX."
        chunks = processor.chunk_text(text, 400, 100)
        assert chunks == [text]

    def test_every_char_in_some_chunk(self):
        text = ("word " * 120) + "\nPIN: 4471-ZX."
        chunks = processor.chunk_text(text, 400, 100)
        assert "4471-ZX." in chunks[-1]

    def test_overlap_not_smaller_than_threshold_terminates(self):
        chunks = processor.chunk_text("y" * 1000, 100, 100)
        assert chunks and "".join(chunks).count("y") >= 1000


class TestEmbedding:
    def test_passage_prefix(self, fake_model):
        processor.embed("hello", "passage")
        assert fake_model.calls[-1] == "passage: hello"

    def test_query_prefix(self, fake_model):
        processor.embed("hello", "query")
        assert fake_model.calls[-1] == "query: hello"

    def test_dimension(self, fake_model):
        assert len(processor.embed("dimension")) == 768

    def test_normalised(self, fake_model):
        v = processor.embed("norm check")
        assert math.sqrt(sum(x * x for x in v)) == pytest.approx(1.0)

    def test_same_text_same_vector_and_nfkc(self, fake_model):
        assert processor.embed("ＡＩ model") == processor.embed("AI model")
        assert processor.cosine_similarity(processor.embed("same"), processor.embed("same")) == pytest.approx(1.0)


class TestEncodingSafety:
    def test_str(self):
        assert processor.sanitize_surrogates("a\ud800b") == "ab"

    def test_list(self):
        assert processor.sanitize_surrogates(["\udfff", "x"]) == ["", "x"]

    def test_dict(self):
        assert processor.sanitize_surrogates({"k": {"n": "a\udc80"}}) == {"k": {"n": "a"}}

    def test_none_and_scalars(self):
        assert processor.sanitize_surrogates(None) is None
        assert processor.sanitize_surrogates(5) == 5
        assert processor.sanitize_surrogates(b"\xff") == b"\xff"

    def test_all_surrogates_become_empty(self):
        assert processor.sanitize_surrogates(chr(0xDFFF) + chr(0xD800) + chr(0xDC80) + chr(0xDBFF)) == ""

    def test_encode_after_sanitize(self):
        processor.sanitize_surrogates("日本\ud83d語").encode("utf-8")

    def test_clean_text_unchanged(self):
        s = "神󠄀 é 😀"
        assert processor.sanitize_surrogates(s) == s
