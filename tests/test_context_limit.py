"""Layer 9: the context size limit (spec §3.17). Rows are built by hand (no model, no Redis),
except TestHookUsesLimit."""
import json

import pytest

import conftest
from n3mc_mcp import cli, config
from n3mc_mcp import context as C
from n3mc_mcp.tools import _NUDGE_AFTER_SEARCH


def rows(n, size=600, text="word "):
    return [{"id": f"id{i:02d}", "content": f"row {i} " + text * (size // len(text)), "score": 1 - i / 100,
             "timestamp_epoch": 1.8e9, "turn_id": f"t{i}"} for i in range(n)]


HEAD = C.hook_head("query")


class TestContextSize:
    def test_utf16(self):
        assert C.context_size("😀") == 2 and C.context_size("あa") == 2

    def test_newlines(self):
        assert C.context_size("a\nb") == 4


class TestRenderLimit:
    def test_unlimited(self):
        out = C.render(rows(20), [], head=HEAD)
        assert C.context_size(out) > 10000 and out == C.render(rows(20), [], head=HEAD, max_chars=None)

    def test_fitting_unchanged(self):
        r = rows(2, 100)
        assert C.render(r, [], head=HEAD, max_chars=9500) == C.render(r, [], head=HEAD)

    def test_drop_lowest(self):
        out = C.render(rows(20), [], head=HEAD, max_chars=9500)
        assert C.context_size(out) + 2 <= 9500
        kept = [i for i in range(20) if f"[id{i:02d}]" in out]
        assert kept == list(range(len(kept))) and 3 < len(kept) < 20
        n = 20 - len(kept)
        assert C.omitted_line(n) in out
        bigger = C._compose(HEAD, [], [C.render_row(r) for r in rows(20)[:len(kept) + 1]], [], "", n - 1)
        assert C.context_size(bigger) + 2 > 9500

    def test_omitted_before_weak(self):
        weak = [{"id": "weak1", "content": "w", "score": 0.1}]
        out = C.render(rows(20), weak, head=HEAD, max_chars=9500)
        last = max(out.index(f"[id{i:02d}]") for i in range(20) if f"[id{i:02d}]" in out)
        assert last < out.index("lower-ranked match(es) omitted") < out.index(C.WEAK_HEADING) < out.index("[weak1]")

    def test_three_rows_cut(self):
        out = C.render(rows(3, 5000), [], head=HEAD, max_chars=4000)
        assert out.endswith(C.CUT_LINE) and C.context_size(out) + 2 <= 4000

    def test_big_parent_cut(self):
        r = [{"id": "doc", "content": "x" * 30000, "_doc": True, "chunk_count": 80, "score": 1}]
        out = C.render(r, [], head=HEAD, max_chars=9500)
        assert out.startswith(HEAD) and out.endswith(C.CUT_LINE) and C.context_size(out) + 2 <= 9500

    def test_notices_and_weak_kept(self):
        weak = [{"id": "weakX", "content": "weak", "score": 0.1}]
        out = C.render(rows(15), weak, notices=["keep me"], head=HEAD, max_chars=9500)
        assert "keep me" in out and "[weakX]" in out

    def test_japanese_emoji(self):
        r = rows(20, 600, "記憶😀")
        out = C.render(r, [], head=HEAD, max_chars=9500)
        assert C.context_size(out) + 2 <= 9500

    def test_mcp_tail(self):
        out = C.render(rows(20), [], tail=_NUDGE_AFTER_SEARCH, max_chars=9500)
        assert out.endswith(_NUDGE_AFTER_SEARCH) and C.context_size(out) + 2 <= 9500
        out = C.render(rows(3, 5000), [], tail=_NUDGE_AFTER_SEARCH, max_chars=4000)
        assert out.endswith(C.CUT_LINE + _NUDGE_AFTER_SEARCH) and C.context_size(out) + 2 <= 4000


class TestHookUsesLimit:
    def test_hook_and_manual(self, app_router, db, capsys):
        for i in range(30):
            db.save_memory(f"limit topic statement {i} " + "about the same topic " * 25)
        cli.hook_submit(json.dumps({"session_id": "s", "prompt": "limit topic statement"}))
        hook_out = capsys.readouterr().out
        assert C.context_size(hook_out.rstrip("\n")) + 2 <= 9500
        cli.main(["--search", "limit topic statement"])
        manual = capsys.readouterr().out
        assert manual.count("### [") > hook_out.count("### [") and "omitted to keep" not in manual


class TestConfigContextMaxChars:
    def test_default(self):
        assert config.DEFAULTS["context_max_chars"] == 9500

    @pytest.mark.parametrize("v", [1999, 10001, 9500.0, "9500"])
    def test_rejected(self, isolate, v):
        conftest.write_test_config(str(isolate["data"]), context_max_chars=v)
        cfg = config.load_config()
        assert cfg["context_max_chars"] == 9500 and any("context_max_chars" in n for n in cfg["_notices"])

    @pytest.mark.parametrize("v", [2000, 10000])
    def test_accepted(self, v):
        assert config.config_value_ok("context_max_chars", v)
