"""Layer 9: context size limit (no model, no Redis; rows are prepared by hand)."""

import pytest

from n3mc_mcp import config, context
from n3mc_mcp.tools import _NUDGE_AFTER_SEARCH


def make_row(i, text, parent=False):
    return {"id": "id-%02d" % i, "content": text, "score": 0.9 - i * 0.01, "timestamp": "2026-11-22T00:00:00+00:00",
            "turn_id": "t%d" % i, "_is_parent": parent, "chunk_count": 3 if parent else 0}


def rows(n, size=350):
    return [make_row(i, "r%d " % i + ("word%d " % i) * (size // 7)) for i in range(n)]


class TestContextSize:
    def test_utf16_units_and_newlines(self):
        assert context.context_size("abc") == 3
        assert context.context_size("\U0001F600") == 2
        assert context.context_size("a\nb") == 4
        assert context.context_size("日本語") == 3


class TestRenderLimit:
    def test_no_limit_leaves_output_unchanged(self):
        r = rows(20, 600)
        full = context.render_search(r, [], [])
        assert context.context_size(full) > 10000
        assert "omitted" not in full

    def test_fitting_output_equals_unlimited(self):
        r = rows(3, 100)
        assert context.render_search(r, [], [], max_chars=9500) == context.render_search(r, [], [])

    def test_lower_ranked_rows_are_dropped_in_order(self):
        r = rows(20)
        out = context.render_search(r, [], [], max_chars=4000)
        assert context.context_size(out) + 2 <= 4000
        assert "lower-ranked match(es) omitted to keep this block within the context limit" in out
        kept = [i for i in range(20) if "[id-%02d]" % i in out]
        assert kept == list(range(len(kept)))
        # one more row would not have fit
        more = context.render_search(r[:len(kept) + 1], [], [], max_chars=10 ** 6)
        assert context.context_size(more) + 2 > 4000 or len(kept) == 3
        n = 20 - len(kept)
        assert "_(%d lower-ranked match(es) omitted" % n in out

    def test_omit_line_sits_before_weak_heading(self):
        r, w = rows(20), [make_row(99, "weak text")]
        out = context.render_search(r, w, [], max_chars=4000)
        assert out.index("omitted") < out.index("#### Possibly related")
        assert "weak text" in out

    def test_three_rows_are_cut_with_a_marker(self):
        r = rows(3, 4000)
        out = context.render_search(r, [], [], max_chars=2500)
        assert context.context_size(out) + 2 <= 2500
        assert out.endswith(context.CUT_MARK)

    def test_huge_parent_document_is_cut(self):
        r = [make_row(0, "x" * 30000, parent=True)]
        out = context.render_search(r, [], [], max_chars=4000)
        assert context.context_size(out) + 2 <= 4000
        assert out.endswith(context.CUT_MARK)

    def test_notices_and_weak_rows_survive_step_one(self):
        out = context.render_search(rows(20), [make_row(98, "weak")], ["a notice"], max_chars=4000)
        assert "> ⚠️ N3MC notice: a notice" in out
        assert "#### Possibly related" in out

    def test_japanese_and_emoji_fit(self):
        r = [make_row(i, ("日本語のテキスト\U0001F600" * 60)) for i in range(10)]
        out = context.render_search(r, [], [], max_chars=3000)
        assert context.context_size(out) + 2 <= 3000

    def test_mcp_tail_is_kept_and_counted(self):
        out = context.render_search(rows(20), [], [], tail=_NUDGE_AFTER_SEARCH, max_chars=4000)
        assert out.endswith(_NUDGE_AFTER_SEARCH)
        assert context.context_size(out) + 2 <= 4000

    def test_hook_head(self):
        out = context.render_search([], [], [], head=context.hook_head("q\nx", 2000))
        assert out.startswith("# Recalled Memory Context\nSource: N3MC Lite (7-day working memory)\nSearch query: q x\n\n")
        assert out.endswith("_No memories found._")


class TestHookUsesLimit:
    def test_hook_and_manual_search(self, svc, db):
        for i in range(30):
            body = ("topic zebra crossing %d " % i) + " ".join("alpha%d beta%d" % (j, j + i) for j in range(40))
            assert svc.save({"content": body})["saved"]
        svc.cfg["_notices"].clear()
        svc.cfg["context_max_chars"] = 4000
        svc.cfg["min_score"] = 0.0
        svc.cfg["relative_min_score_ratio"] = 0.0
        hook = svc.search({"query": "topic zebra crossing", "limit": 30}, max_chars=4000, hook=True)
        manual = svc.search({"query": "topic zebra crossing", "limit": 30}, max_chars=None, hook=True)
        assert context.context_size(hook) + 2 <= 4000
        assert "lower-ranked match(es) omitted to keep this block within the context limit" in hook
        assert manual.count("### [") > hook.count("### [")
        assert "omitted to keep" not in manual


class TestConfigContextMaxChars:
    def test_default(self):
        assert config.DEFAULTS["context_max_chars"] == 9500

    @pytest.mark.parametrize("bad", [1999, 10001, 9500.0, "9500"])
    def test_invalid_values_ignored(self, bad):
        assert not config.config_value_ok("context_max_chars", bad)

    @pytest.mark.parametrize("good", [2000, 10000])
    def test_valid_values(self, good):
        assert config.config_value_ok("context_max_chars", good)
