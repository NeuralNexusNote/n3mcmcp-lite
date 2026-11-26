"""Layer 9: the context size limit (spec §3.17). No model, no Redis (rows are made by hand)."""
import json

import pytest

from n3mc_mcp import context as C
from n3mc_mcp.tools import _NUDGE_AFTER_SEARCH


def _row(i, text=None, doc=False):
    return {"id": f"id-{i:03d}", "content": text if text is not None else f"row {i} " + "word " * 120,
            "score": 1.0 - i * 0.01, "timestamp": "2026-11-13T01:00:00+00:00", "turn_id": f"t{i}",
            "_doc": doc, "chunk_count": 3 if doc else 0}


class TestContextSize:
    def test_utf16_units(self):
        assert C.context_size("😀") == 2
        assert C.context_size("あ") == 1

    def test_newlines_count_twice(self):
        assert C.context_size("a\nb") == 4


class TestRenderLimit:
    def test_no_limit_unchanged(self):
        rows = [_row(i) for i in range(20)]
        out = C.render_search(rows, [], max_chars=None)
        assert C.context_size(out) > 10000
        assert out.count("### [") == 20

    def test_fitting_output_is_the_same(self):
        rows = [_row(i, "short") for i in range(3)]
        assert C.render_search(rows, [], max_chars=9500) == C.render_search(rows, [], max_chars=None)

    def test_drops_lowest_rows_first(self):
        rows = [_row(i) for i in range(20)]
        head = C.hook_head("q")
        out = C.render_search(rows, [], head=head, max_chars=4000)
        assert C.context_size(out) + 2 <= 4000
        kept = out.count("### [")
        assert 3 <= kept < 20
        for i in range(kept):
            assert f"[id-{i:03d}]" in out
        assert C.omitted_line(20 - kept) in out
        more = C.render_search(rows[:kept + 1], [], head=head, max_chars=None)
        assert C.context_size(more) + 2 > 4000

    def test_omitted_line_before_weak_heading(self):
        rows = [_row(i) for i in range(20)]
        weak = [_row(99, "weak one")]
        out = C.render_search(rows, weak, max_chars=4000)
        assert out.index("lower-ranked match(es) omitted") < out.index(C.WEAK_HEADING)
        assert "weak one" in out

    def test_three_rows_are_cut(self):
        rows = [_row(i, "x" * 3000) for i in range(3)]
        out = C.render_search(rows, [], max_chars=4000)
        assert out.endswith(C.CUT_LINE)
        assert C.context_size(out) + 2 <= 4000

    def test_long_parent_document_is_cut(self):
        rows = [_row(0, "長" * 30000, doc=True), _row(1, "short")]
        out = C.render_search(rows, [], max_chars=9500)
        assert out.endswith(C.CUT_LINE) and C.context_size(out) + 2 <= 9500

    def test_step1_keeps_notices_and_weak(self):
        rows = [_row(i) for i in range(10)]
        weak = [_row(50, "weak keep")]
        out = C.render_search(rows, weak, notices=["config_type: something"], max_chars=4000)
        assert "config_type: something" in out and "weak keep" in out

    def test_japanese_and_emoji_fit(self):
        rows = [_row(i, "日本語😀" * 300) for i in range(10)]
        out = C.render_search(rows, [], max_chars=3000)
        assert C.context_size(out) + 2 <= 3000

    def test_mcp_keeps_reminder(self):
        rows = [_row(i) for i in range(20)]
        out = C.render_search(rows, [], tail=_NUDGE_AFTER_SEARCH, max_chars=4000)
        assert out.endswith(_NUDGE_AFTER_SEARCH)
        assert C.context_size(out) + 2 <= 4000


class TestHookUsesLimit:
    def test_hook_block_is_limited_and_manual_is_not(self, cfg, monkeypatch):
        from n3mc_mcp import cli
        bodies = []

        def fake_post(port, path, body, timeout=30):
            bodies.append((path, body))
            return 200, {"text": "ok"}

        monkeypatch.setattr(cli, "_post", fake_post)
        cli._hook_search(cfg, True, "question", "s1")
        assert bodies[-1][1]["max_chars"] == 9500
        assert bodies[-1][1]["no_model_wait"] is True
        monkeypatch.setattr(cli, "ensure_server", lambda cfg, max_wait=60: True)
        cli.cmd_search(cfg, "question", None, None, None)
        assert "max_chars" not in bodies[-1][1]
        assert "no_model_wait" not in bodies[-1][1]


class TestConfigContextMaxChars:
    def _load(self, isolate, value):
        from n3mc_mcp.config import load_config
        p = isolate["data"] / "config.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        d["context_max_chars"] = value
        p.write_text(json.dumps(d), encoding="utf-8")
        return load_config()

    def test_default(self, cfg):
        assert cfg["context_max_chars"] == 9500

    @pytest.mark.parametrize("bad", [1999, 10001, 9500.0, "9500"])
    def test_rejected(self, isolate, bad):
        c = self._load(isolate, bad)
        assert c["context_max_chars"] == 9500
        assert any(n.startswith("config_type") and "context_max_chars" in n for n in c["_notices"])

    @pytest.mark.parametrize("ok", [2000, 10000])
    def test_accepted(self, isolate, ok):
        c = self._load(isolate, ok)
        assert c["context_max_chars"] == ok and not c["_notices"]
