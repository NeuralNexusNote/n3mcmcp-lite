"""Layer 5: hook flow, audit-first, turn_id hand-off, transcript recovery, direct save."""

import io
import json
import pathlib
import subprocess
import sys

import pytest

from n3mc_mcp import cli, paths


def _stdin(monkeypatch, obj):
    data = json.dumps(obj, ensure_ascii=False) if not isinstance(obj, str) else obj
    monkeypatch.setattr(cli, "_read_stdin_text", lambda: data)


class TestAuditFirst:
    def test_entry_writes_audit_even_without_backend(self, isolated):
        payload = {"session_id": "s", "prompt": "<system-reminder>x"}
        env = {**__import__("os").environ}
        r = subprocess.run([sys.executable, "-m", "n3mc_mcp.lite_hook"], input=json.dumps(payload).encode(),
                           capture_output=True, env=env, timeout=60, cwd=str(isolated))
        assert r.returncode == 0
        lines = paths.audit_log_path().read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        assert first["hook"] == "UserPromptSubmit" and first["payload"]["session_id"] == "s"
        assert b"_No memories found._" in r.stdout

    def test_non_json_stdin(self, isolated):
        r = subprocess.run([sys.executable, "-m", "n3mc_mcp.lite_stop_hook"], input=b"not json",
                           capture_output=True, timeout=60, cwd=str(isolated))
        assert (isolated / ".claude" / "rules" / "n3mc-lite-behavior.md").exists()   # the child's cwd, not ours
        assert r.returncode == 0
        rec = json.loads(paths.audit_log_path().read_text(encoding="utf-8").splitlines()[0])
        assert rec["payload"] == {"raw": "not json"} and rec["hook"] == "Stop"


class TestSystemPrefixes:
    def test_skipped(self, monkeypatch, capsys):
        _stdin(monkeypatch, {"session_id": "s", "prompt": "<task-notification> done"})
        paths.turn_id_path("s").write_text("keep", encoding="utf-8")
        assert cli.cmd_hook_submit() == 0
        assert capsys.readouterr().out == ("# Recalled Memory Context\nSource: N3MC Lite (7-day working memory)"
                                           "\n\n_No memories found._\n")
        assert paths.turn_id_path("s").read_text(encoding="utf-8") == "keep"


def _transcript(tmp_path, rows):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    return str(p)


class TestTranscript:
    def test_last_text_block_of_previous_reply(self, tmp_path):
        tp = _transcript(tmp_path, [
            {"type": "user", "message": {"content": "q1"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "first"},
                                                          {"type": "tool_use", "id": "x"}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "r"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "answer"}]}},
            {"type": "user", "isMeta": True, "message": {"content": "meta"}},
            {"type": "user", "message": {"content": "q2"}},
        ])
        assert cli.last_assistant_text(tp, "q2") == "answer"

    def test_interrupted_and_none(self, tmp_path):
        tp = _transcript(tmp_path, [
            {"type": "user", "message": {"content": "q1"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "partial"}]}},
            {"type": "user", "message": {"content": "[Request interrupted by user]"}},
        ])
        assert cli.last_assistant_text(tp, "q2") == "partial"
        tp2 = _transcript(tmp_path, [{"type": "user", "message": {"content": "only"}}])
        assert cli.last_assistant_text(tp2, "new") is None


class TestTurnIdHandoffAndPrefix:
    def test_flow(self, monkeypatch, capsys, rconn, tmp_path):
        monkeypatch.setattr(cli, "ensure_server", lambda cfg, max_wait=60: False)
        tp = _transcript(tmp_path, [{"type": "user", "message": {"content": "hello there"}}])
        _stdin(monkeypatch, {"session_id": "sess/1", "transcript_path": tp, "prompt": "hello there"})
        cli.cmd_hook_submit()
        out = capsys.readouterr().out
        assert out.startswith("# Recalled Memory Context\nSource: N3MC Lite (7-day working memory)\n\n")
        assert "_(memory search unavailable:" in out
        turn = paths.turn_id_path("sess/1").read_text(encoding="utf-8")
        assert paths.turn_id_path("sess/1").name == "turn_id_sess_1.txt"
        _stdin(monkeypatch, {"session_id": "sess/1", "last_assistant_message": "the reply"})
        cli.cmd_save_claude_turn()
        _stdin(monkeypatch, {"session_id": "sess/1", "last_assistant_message": "the reply"})
        cli.cmd_save_claude_turn()
        recs = {}
        for k in rconn.scan_iter("mem:*"):
            if not k.startswith(b"mem:sha:"):
                h = rconn.hgetall(k)
                recs[h[b"content"].decode()] = h
        assert set(recs) == {"[user] hello there", "[claude] the reply"}
        for h in recs.values():
            assert h[b"turn_id"].decode() == turn and b"embedding" not in h
            assert h[b"agent_name"] == b"claude-code" and h[b"session_id"] == b"sess/1"


class TestReplySavedOnce:
    """Stop with no turn_id file, then the next prompt recovers the same reply from the transcript."""

    def test_stop_then_recovery_is_one_record(self, monkeypatch, capsys, rconn, tmp_path):
        monkeypatch.setattr(cli, "ensure_server", lambda cfg, max_wait=60: False)
        reply = "長い返答。" * 200
        _stdin(monkeypatch, {"session_id": "s9", "last_assistant_message": reply})
        cli.cmd_save_claude_turn()
        assert paths.turn_id_path("s9").exists()
        tp = _transcript(tmp_path, [
            {"type": "user", "message": {"content": "q1"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": reply}]}},
            {"type": "user", "message": {"content": "q2"}},
        ])
        _stdin(monkeypatch, {"session_id": "s9", "transcript_path": tp, "prompt": "q2"})
        cli.cmd_hook_submit()
        docs = [k for k in rconn.scan_iter("doc:*") if not k.startswith(b"docsha:")]
        assert len(docs) == 1


class TestHookTimeoutKeepsOutput:
    def test_partial_stdout_passed_through(self, monkeypatch):
        import subprocess as sp
        from n3mc_mcp import lite_hook

        def boom(*a, **k):
            raise sp.TimeoutExpired(cmd="x", timeout=1, output=b"# Recalled Memory Context\npartial")
        monkeypatch.setattr(lite_hook.subprocess, "run", boom)
        out = io.BytesIO()
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{}")))
        monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(out))
        assert lite_hook.run("UserPromptSubmit", (["--hook-submit"],)) == 0
        sys.stdout.flush()
        assert out.getvalue() == b"# Recalled Memory Context\npartial"


class TestRedisDown:
    def test_notice_and_exit_zero(self, monkeypatch, capsys, cfg):
        p = paths.config_path()
        d = json.loads(p.read_text(encoding="utf-8"))
        d["redis_url"] = "redis://127.0.0.1:1/0"
        p.write_text(json.dumps(d), encoding="utf-8")
        monkeypatch.setattr(cli, "ensure_server", lambda cfg, max_wait=60: False)
        _stdin(monkeypatch, {"session_id": "s", "prompt": "will not be saved"})
        assert cli.cmd_hook_submit() == 0
        n = paths.load_notices()
        assert "docker start redis-stack" in n["redis_unreachable"]["message"]
