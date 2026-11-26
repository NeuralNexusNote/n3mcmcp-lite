"""Layer 9: the context size limit (spec §3.17). No model, no Redis: prepared result rows."""
import io
import json
from datetime import datetime, timezone

import pytest

from n3mc_mcp import cli, context, tools
from n3mc_mcp.config import load_config

TS = datetime.now(timezone.utc).isoformat()


def rows(n, size=600, prefix="row"):
    return [{"id": f"{prefix}-{i:02d}", "content": f"{prefix} {i} " + ("本文テキスト😀 " * (size // 8)),
             "score": 1.0 - i / 100, "timestamp": TS} for i in range(n)]


def head():
    return context.hook_head("question", 2000)


class TestContextSize:
    def test_utf16_units(self):
        assert context.context_size("😀") == 2 and context.context_size("abc") == 3

    def test_newlines_count_twice(self):
        assert context.context_size("a\nb") == 4


class TestRenderLimit:
    def test_no_limit_unchanged(self):
        out = context.render_search(rows(20), [], head=head())
        assert context.context_size(out) > 10_000 and "omitted" not in out and out.count("### ") == 20

    def test_fitting_output_is_identical(self):
        r = rows(2, 100)
        assert context.render_search(r, [], head=head(), max_chars=9500) == context.render_search(r, [], head=head())

    def test_drops_lowest_rows_with_note(self):
        r = rows(20)
        out = context.render_search(r, [], head=head(), max_chars=9500)
        assert context.context_size(out) + 2 <= 9500
        kept = [line for line in out.splitlines() if line.startswith("### ")]
        n = len(kept)
        assert kept == [line for line in context.render_search(r[:n], [], head=head()).splitlines()
                        if line.startswith("### ")]
        assert f"_({20 - n} lower-ranked match(es) omitted" in out
        bigger = context.render_search(r[: n + 1], [], head=head())
        assert context.context_size(bigger) + 2 > 9500

    def test_note_before_weak_heading(self):
        weak = rows(1, 50, "weak")
        out = context.render_search(rows(20), weak, head=head(), max_chars=9500)
        assert out.index("omitted to keep") < out.index(context.WEAK_HEADING)
        assert "weak-00" in out

    def test_three_rows_are_cut(self):
        out = context.render_search(rows(3, 4000), [], head=head(), max_chars=4000)
        assert out.endswith(context.CUT_LINE) and context.context_size(out) + 2 <= 4000

    def test_huge_parent_doc_is_cut(self):
        doc = [{"id": "doc", "content": "x" * 30_000, "score": 1.0, "timestamp": TS, "_doc": True,
                "chunk_count": 80}]
        out = context.render_search(doc, [], head=head(), max_chars=9500)
        assert out.startswith(head()) and out.endswith(context.CUT_LINE)

    def test_notices_and_weak_rows_kept(self):
        out = context.render_search(rows(20), rows(2, 50, "weak"), ["important notice"], head=head(),
                                    max_chars=9500)
        assert "> ⚠️ N3MC notice: important notice" in out and "weak-01" in out

    def test_mcp_tail_is_kept_and_counted(self):
        out = context.render_search(rows(20), [], tail=tools._NUDGE_AFTER_SEARCH, max_chars=5000)
        assert out.endswith(tools._NUDGE_AFTER_SEARCH) and context.context_size(out) + 2 <= 5000


class TestHookUsesLimit:
    def _capture(self, monkeypatch):
        sent = []

        def fake_post(port, path, body, timeout=30):
            sent.append((path, body))
            if path == "/search":
                return 200, {"text": "# Recalled Memory Context\nSource: N3MC Lite (7-day working memory)"}
            return 200, {"status": "saved"}

        monkeypatch.setattr(cli, "_post", fake_post)
        monkeypatch.setattr(cli, "ensure_server", lambda cfg, max_wait=60: True)
        return sent

    def test_hook_sends_max_chars(self, monkeypatch, capsys):
        sent = self._capture(monkeypatch)
        monkeypatch.setattr(cli, "_read_stdin", lambda: json.dumps({"prompt": "hello there",
                                                                    "session_id": "s"}).encode())
        cli.main(["--hook-submit"])
        body = [b for p, b in sent if p == "/search"][0]
        assert body["max_chars"] == 9500 and body["no_model_wait"] is True

    def test_manual_search_unlimited(self, monkeypatch, capsys):
        sent = self._capture(monkeypatch)
        cli.main(["--search", "hello"])
        body = [b for p, b in sent if p == "/search"][0]
        assert "max_chars" not in body and "no_model_wait" not in body


class TestConfigContextMaxChars:
    def _load(self, isolated, value):
        p = isolated["data"] / "config.json"
        obj = json.loads(p.read_text(encoding="utf-8"))
        obj["context_max_chars"] = value
        p.write_text(json.dumps(obj), encoding="utf-8")
        return load_config(quiet=True)

    def test_default(self, cfg):
        assert cfg["context_max_chars"] == 9500

    @pytest.mark.parametrize("value", [1999, 10001, 9500.0, "9500"])
    def test_rejected(self, isolated, value):
        c = self._load(isolated, value)
        assert c["context_max_chars"] == 9500
        assert any(n.startswith("config_type:") and "context_max_chars" in n for n in c["_notices"])

    @pytest.mark.parametrize("value", [2000, 10000])
    def test_accepted(self, isolated, value):
        assert self._load(isolated, value)["context_max_chars"] == value
