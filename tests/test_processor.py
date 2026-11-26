"""Layer 2: ranking math, embeddings, purify, chunking, reranker, encoding safety (spec §3.2,
§3.6, §3.7, §3.11, §3.13). No Redis. TestEmbedding loads the real model (slow)."""
import math
import os
import time

import pytest

from n3mc_mcp import processor as P

CFG = {"access_count_enabled": True, "access_count_weight": 0.02, "access_count_max_boost": 0.5,
       "lexical_rerank_enabled": True, "rerank_weight": 0.3, "rerank_phrase_weight": 0.2}


class TestCosineSim:
    def test_identical(self):
        assert P.cosine([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)

    def test_orthogonal(self):
        assert P.cosine([1, 0], [0, 1]) == pytest.approx(0.0)

    def test_negative_clamped(self):
        assert P.cos_sim_from_distance(1.7) == 0.0

    def test_distance_to_similarity(self):
        assert P.cos_sim_from_distance(0.25) == pytest.approx(0.75)

    def test_bad_distance(self):
        assert P.cos_sim_from_distance("x") == 0.0


class TestTimeDecay:
    def test_now(self):
        assert P.time_decay(time.time(), 3) == pytest.approx(1.0, abs=1e-4)

    def test_half_life(self):
        now = time.time()
        assert P.time_decay(now - 3 * 86400, 3, now) == pytest.approx(0.5)

    def test_seven_days(self):
        now = time.time()
        assert P.time_decay(now - 7 * 86400, 3, now) == pytest.approx(0.198, abs=0.01)

    def test_invalid_timestamp(self):
        assert P.time_decay("bad", 3) == 1.0


class TestKeywordRelevance:
    def test_below_threshold(self):
        assert P.keyword_relevance(0.05, 5.0, 0.1) == 0.0

    def test_normalized(self):
        assert P.keyword_relevance(2.0, 4.0, 0.1) == pytest.approx(0.5)

    def test_zero_max(self):
        assert P.keyword_relevance(0.5, 0.0, 0.1) == pytest.approx(0.5)

    def test_abs(self):
        assert P.keyword_relevance(-2.0, -4.0, 0.1) == pytest.approx(0.5)


class TestFinalScore:
    def test_formula(self):
        base = P.base_score(0.9, 0.5)
        assert base == pytest.approx(0.9 * 0.7 + 0.5 * 0.3)

    def test_b_local_clamp_high(self):
        assert P.b_local(5.0, 0, CFG) == 2.0

    def test_b_local_clamp_low(self):
        assert P.b_local(0.1, 0, CFG) == 0.5

    def test_b_local_nan(self):
        assert P.b_local("nan", 0, CFG) == 1.0


class TestAccessCountBoost:
    def test_boost(self):
        assert P.b_local(1.0, 10, CFG) == pytest.approx(1.2)

    def test_cap(self):
        assert P.b_local(1.0, 10000, CFG) == pytest.approx(1.5)

    def test_clamp(self):
        assert P.b_local(1.9, 50, CFG) == 2.0

    def test_disabled(self):
        assert P.b_local(1.0, 50, dict(CFG, access_count_enabled=False)) == 1.0


class TestLexicalRerank:
    def test_coverage(self):
        assert P.lexical_bonus("apple pie", "an apple pie recipe that is long enough to count fully ok", CFG) > 0.3

    def test_phrase_boost(self):
        long = "x" * 60
        with_phrase = P.lexical_bonus("blue whale", "the blue whale " + long, CFG)
        without = P.lexical_bonus("blue whale", "the whale is blue " + long, CFG)
        assert with_phrase > without

    def test_short_damped(self):
        assert P.lexical_bonus("cat", "cat", CFG) < P.lexical_bonus("cat", "cat " + "y" * 60, CFG)

    def test_zero_coverage(self):
        assert P.lexical_bonus("zebra", "nothing related " * 5, CFG) == 0.0

    def test_disabled(self):
        assert P.lexical_bonus("a", "a" * 80, dict(CFG, lexical_rerank_enabled=False)) == 0.0

    def test_punctuation_token(self):
        assert P.lexical_bonus("note", "note, " + "z" * 60, CFG) > 0


class TestPurification:
    def test_multiline(self):
        assert P.purify("a\n```py\nx=1\n```\nb") == "a\n[code omitted]\nb"

    def test_inline_kept(self):
        assert P.purify("use `x` here") == "use `x` here"

    def test_multiple(self):
        assert P.purify("```a```m```b```") == "[code omitted]m[code omitted]"

    def test_unclosed(self):
        assert P.purify("start ```code to the end") == "start [code omitted]"


class TestChunkText:
    def test_single(self):
        assert P.chunk_text("short", 400, 100) == ["short"]

    def test_split_overlap(self):
        text = "a" * 1000
        chunks = P.chunk_text(text, 400, 100)
        assert len(chunks) >= 3 and all(len(c) <= 419 for c in chunks)
        assert chunks[1].startswith("a") and len(chunks[0]) == 400

    def test_soft_break(self):
        text = ("word " * 100).strip()
        chunks = P.chunk_text(text, 100, 30)
        assert all(not c.startswith(" ") for c in chunks[1:])
        assert "".join(chunks) == text

    def test_short_tail_joined(self):
        text = "x" * 390 + "\n\n" + "PIN: 4471-ZX."
        chunks = P.chunk_text(text, 400, 100)
        assert len(chunks) == 1 and chunks[0].endswith("PIN: 4471-ZX.")

    def test_every_character_covered(self):
        text = ("line of text\n" * 40) + "END"
        chunks = P.chunk_text(text, 100, 30)
        assert "END" in chunks[-1]

    def test_overlap_ge_threshold_terminates(self):
        assert P.chunk_text("z" * 1000, 100, 100)


class TestEmbedding:
    @pytest.fixture(autouse=True)
    def _real_model(self, monkeypatch):
        cache = os.path.join(os.environ["HF_HOME"], "hub", "models--intfloat--multilingual-e5-base")
        if not os.path.isdir(cache):
            pytest.skip("the embedding model is not in the Hugging Face cache")
        monkeypatch.setattr(P, "_FAKE_EMBED", None)

    @pytest.mark.slow
    def test_dimension(self):
        v = P.embed("hello", "passage")
        assert len(v) == 768 and math.isclose(sum(x * x for x in v), 1.0, rel_tol=1e-3)

    @pytest.mark.slow
    def test_prefix_matters(self):
        assert P.embed("hello", "passage") != P.embed("hello", "query")

    @pytest.mark.slow
    def test_same_text(self):
        assert P.cosine(P.embed("坂本龍馬", "query"), P.embed("坂本龍馬", "query")) > 0.999


class TestEncodingSafety:
    def test_str(self):
        assert P.sanitize_surrogates("a\ud800b") == "ab"

    def test_list(self):
        assert P.sanitize_surrogates(["\udfff", "x"]) == ["", "x"]

    def test_dict(self):
        assert P.sanitize_surrogates({"k": {"n": "\ud83dz"}}) == {"k": {"n": "z"}}

    def test_none(self):
        assert P.sanitize_surrogates(None) is None

    def test_all_surrogates(self):
        assert P.sanitize_surrogates("\ud800" + "\ud801" + "\udfff") == ""

    def test_encode_after(self):
        P.sanitize_surrogates("ok\ud800").encode("utf-8")
