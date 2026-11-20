"""Layer 9: context size limit (§3.17). No model, no Redis."""

import json

from n3mc_mcp import config as config_mod
from n3mc_mcp import context as C
from n3mc_mcp import paths
from n3mc_mcp._verbatim import _NUDGE_AFTER_SEARCH


def rows(n, size=600):
    return [f"### [id{i}] score=0.5 saved=x\n" + ("記憶" * (size // 2)) for i in range(n)]


class TestContextSize:
    def test_units(self):
        assert C.context_size("😀") == 2
        assert C.context_size("a\nb") == 4


class TestRenderLimit:
    def test_no_limit_unchanged(self):
        full = C.assemble("H\n\n", [], rows(20), [], "")
        assert C.context_size(full) > 10000
        assert C.assemble("H\n\n", [], rows(20), [], "", None) == full

    def test_rows_dropped_with_omitted_line(self):
        out = C.assemble("H\n\n", ["n1"], rows(20), ["### weak"], _NUDGE_AFTER_SEARCH, 4000)
        assert C.context_size(out) + 2 <= 4000
        assert "lower-ranked match(es) omitted" in out and out.endswith(_NUDGE_AFTER_SEARCH)
        assert "> ⚠️ N3MC notice: n1" in out and "### weak" in out
        assert out.index("omitted") < out.index(C.WEAK_HEADING)

    def test_cut_long_document(self):
        out = C.assemble("H\n\n", [], ["### [d] x\n" + "長" * 30000], [], "", 2000)
        assert out.endswith(C.CUT_LINE) and C.context_size(out) + 2 <= 2000


class TestConfigContextMaxChars:
    def test_range(self):
        for bad in (1999, 10001, 9500.0, "9500"):
            assert not config_mod.config_value_ok("context_max_chars", bad)
        for good in (2000, 10000):
            assert config_mod.config_value_ok("context_max_chars", good)

    def test_default_and_notice(self):
        p = paths.config_path()
        d = json.loads(p.read_text(encoding="utf-8"))
        d["context_max_chars"] = 1999
        p.write_text(json.dumps(d), encoding="utf-8")
        cfg = config_mod.load_config()
        assert cfg["context_max_chars"] == 9500 and any("context_max_chars" in n for n in cfg["_notices"])
