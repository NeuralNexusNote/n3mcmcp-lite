"""Layer 5: hook flow, role prefixes, direct save (spec §3.16, §12). Fake embedding, no model. The
server is either a FastAPI TestClient behind the faked _post, or absent."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import conftest
from n3mc_mcp import cli, context, database, lite_hook, lite_stop_hook, paths
from n3mc_mcp.database import DOC, MEM, SHA, Database

EMPTY = "# Recalled Memory Context\nSource: N3MC Lite (7-day working memory)\n\n_No memories found._\n"


def run_cli(monkeypatch, capsys, args, payload):
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    monkeypatch.setattr(cli, "_read_stdin", lambda: raw)
    code = cli.main(args)
    out = capsys.readouterr().out
    return code, out


def submit(monkeypatch, capsys, prompt, session="sess-1", **extra):
    payload = {"session_id": session, "prompt": prompt, "hook_event_name": "UserPromptSubmit"}
    payload.update(extra)
    return run_cli(monkeypatch, capsys, ["--hook-submit"], payload)


def stop_turn(monkeypatch, capsys, message, session="sess-1", **extra):
    payload = {"session_id": session, "last_assistant_message": message, "stop_hook_active": False}
    payload.update(extra)
    return run_cli(monkeypatch, capsys, ["--save-claude-turn"], payload)


def records(rconn):
    out = []
    for k in rconn.keys("mem:*"):
        if k.startswith(b"mem:sha:"):
            continue
        h = rconn.hgetall(k)
        out.append({kk.decode(): (v if kk == b"embedding" else v.decode()) for kk, v in h.items()})
    return out


def audit_lines():
    p = paths.audit_log_path()
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


class TestAuditFirst:
    def _run(self, monkeypatch, entry, data):
        order = []
        monkeypatch.setattr(lite_hook, "read_stdin", lambda: data)
        monkeypatch.setattr(lite_hook, "run_step", lambda args, d, dl: order.append((args, audit_lines())))
        assert entry.main() == 0
        return order

    def test_submit_audit_before_step(self, monkeypatch):
        order = self._run(monkeypatch, lite_hook, json.dumps({"prompt": "hi"}).encode())
        args, seen = order[0]
        assert args[0] == "--hook-submit" and seen[0]["hook"] == "UserPromptSubmit"
        assert set(seen[0]) == {"ts", "hook", "raw", "payload"} and seen[0]["payload"]["prompt"] == "hi"

    def test_stop_two_steps(self, monkeypatch):
        order = self._run(monkeypatch, lite_stop_hook, json.dumps({"last_assistant_message": "x"}).encode())
        assert [o[0][0] for o in order] == ["--save-claude-turn", "--stop"]
        assert order[0][1][0]["hook"] == "Stop"

    def test_not_json_and_cut(self, monkeypatch):
        data = ("y" * 5000).encode()
        self._run(monkeypatch, lite_hook, data)
        rec = audit_lines()[0]
        assert len(rec["raw"]) == 4096 and rec["payload"] == {"raw": "y" * 5000}

    def test_audit_failure_does_not_stop(self, monkeypatch, isolated):
        paths.audit_log_path().mkdir()
        order = []
        monkeypatch.setattr(lite_hook, "read_stdin", lambda: b"{}")
        monkeypatch.setattr(lite_hook, "run_step", lambda args, d, dl: order.append(args))
        assert lite_hook.main() == 0 and order


class TestTurnIdHandoff:
    def test_handoff(self, monkeypatch, capsys, rconn):
        submit(monkeypatch, capsys, "what is the plan")
        tid = paths.turn_id_path("sess-1").read_text(encoding="utf-8")
        stop_turn(monkeypatch, capsys, "the plan is ready")
        recs = {r["content"]: r for r in records(rconn)}
        assert recs["[user] what is the plan"]["turn_id"] == tid
        assert recs["[claude] the plan is ready"]["turn_id"] == tid
        assert paths.turn_id_path("sess-1").read_text(encoding="utf-8") == tid

    def test_session_name_is_sanitised(self):
        assert paths.turn_id_path("a/b:c" + "x" * 80).name == "turn_id_a_b_c" + "x" * 59 + ".txt"
        assert paths.turn_id_path("").name == "turn_id.txt"

    def test_no_file_new_uuid(self, monkeypatch, capsys, rconn):
        stop_turn(monkeypatch, capsys, "answer without a file", session="fresh")
        tid = paths.turn_id_path("fresh").read_text(encoding="utf-8")
        assert len(tid) == 36 and records(rconn)[0]["turn_id"] == tid

    def test_image_only_prompt(self, monkeypatch, capsys, rconn):
        prompt = json.dumps([{"type": "image", "source": {}}])
        _c, out = submit(monkeypatch, capsys, prompt)
        assert out == EMPTY and paths.turn_id_path("sess-1").exists()
        assert not records(rconn)


class TestStopReadsTurnIdFirst:
    def test_turn_id_read_before_server(self, monkeypatch, capsys):
        cli._write_turn_id("s", "before")
        saved = []

        def rewrite(cfg, max_wait=60):
            cli._write_turn_id("s", "after")
            return False

        monkeypatch.setattr(cli, "ensure_server", rewrite)
        monkeypatch.setattr(cli, "_hook_save", lambda cfg, ok, c, role, s, tid: saved.append(tid))
        stop_turn(monkeypatch, capsys, "reply", session="s")
        assert saved == ["before"]


def write_transcript(path, lines):
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return str(path)


def u(text, **kw):
    d = {"type": "user", "message": {"role": "user", "content": text}}
    d.update(kw)
    return d


def a(*parts, **kw):
    d = {"type": "assistant", "message": {"role": "assistant", "content": list(parts)}}
    d.update(kw)
    return d


def t(text):
    return {"type": "text", "text": text}


class TestTranscriptRecovery:
    def test_cases(self, tmp_path):
        tp = write_transcript(tmp_path / "t.jsonl", [
            u("first question"),
            a(t("draft"), {"type": "thinking", "thinking": "hmm"}, t("final answer"),
              {"type": "tool_use", "name": "x"}),
            u([{"type": "tool_result", "content": "r"}]),
            u("meta", isMeta=True),
            u("<command-name>/clear</command-name>"),
            a(t("sidechain"), isSidechain=True),
            {"type": "summary", "summary": "x"},
            u("current prompt"),
        ])
        assert cli._previous_response(tp, "current prompt") == "final answer"

    def test_interrupted(self, tmp_path):
        tp = write_transcript(tmp_path / "t.jsonl", [u("q"), a(t("partial reply")),
                                                     u("[Request interrupted by user]"), u("next")])
        assert cli._previous_response(tp, "next") == "partial reply"

    def test_older_same_prompt_stops(self, tmp_path):
        tp = write_transcript(tmp_path / "t.jsonl", [a(t("old")), u("again"), u("again")])
        assert cli._previous_response(tp, "again") is None

    def test_saved_with_previous_turn_and_dedup(self, monkeypatch, capsys, rconn, tmp_path):
        submit(monkeypatch, capsys, "q1")
        tid1 = paths.turn_id_path("sess-1").read_text(encoding="utf-8")
        stop_turn(monkeypatch, capsys, "answer one")
        tp = write_transcript(tmp_path / "t.jsonl", [u("q1"), a(t("answer one")), u("q2")])
        submit(monkeypatch, capsys, "q2", transcript_path=tp)
        claude = [r for r in records(rconn) if r["content"] == "[claude] answer one"]
        assert len(claude) == 1 and claude[0]["turn_id"] == tid1

    def test_recovered_when_stop_missed(self, monkeypatch, capsys, rconn, tmp_path):
        submit(monkeypatch, capsys, "q1")
        tid1 = paths.turn_id_path("sess-1").read_text(encoding="utf-8")
        tp = write_transcript(tmp_path / "t.jsonl", [u("q1"), a(t("missed by stop")), u("q2")])
        submit(monkeypatch, capsys, "q2", transcript_path=tp)
        rec = [r for r in records(rconn) if r["content"] == "[claude] missed by stop"]
        assert rec and rec[0]["turn_id"] == tid1

    def test_unreadable(self, monkeypatch, capsys, rconn, tmp_path):
        _c, out = submit(monkeypatch, capsys, "still saved", transcript_path=str(tmp_path / "missing.jsonl"))
        assert out.startswith("# Recalled Memory Context")
        assert any(r.get("stage") == "transcript_unreadable" for r in audit_lines())
        assert any(r["content"] == "[user] still saved" for r in records(rconn))


class TestSystemPrefixes:
    @pytest.mark.parametrize("prompt", ["<task-notification>x", "<agent-message from=a>x", "<system-reminder>x"])
    def test_skipped(self, monkeypatch, capsys, prompt):
        cli._write_turn_id("sess-1", "keep-me")
        _c, out = submit(monkeypatch, capsys, prompt)
        assert out == EMPTY
        assert paths.turn_id_path("sess-1").read_text(encoding="utf-8") == "keep-me"
        assert any(r.get("stage") == "skipped" and r.get("reason") == "system_prompt" for r in audit_lines())

    def test_div_not_skipped(self, monkeypatch, capsys):
        cli._write_turn_id("sess-1", "keep-me")
        submit(monkeypatch, capsys, "<div>hello</div>")
        assert paths.turn_id_path("sess-1").read_text(encoding="utf-8") != "keep-me"


class TestRolePrefix:
    def test_short_user(self, monkeypatch, capsys, rconn):
        submit(monkeypatch, capsys, "short message")
        r = records(rconn)[0]
        assert r["content"] == "[user] short message" and r["agent_name"] == "claude-code"
        assert r["session_id"] == "sess-1"

    def test_long_claude(self, db, rconn):
        body = "長い応答の本文。" * 125
        r = db.save_memory(body, role="claude", turn_id="t", agent_name="claude-code")
        assert rconn.hget(DOC + r["parent_id"], "content").decode() == "[claude] " + body
        n = r["chunks"]
        assert rconn.hget(MEM + r["ids"][0], "content").decode().startswith(f"[claude 1/{n}] ")

    def test_one_chunk_over_threshold(self, db, rconn):
        body = "x" * 395 + " " + "y" * 14
        r = db.save_memory(body, role="claude", turn_id="t")
        assert r["chunks"] == 1
        assert rconn.hget(MEM + r["ids"][0], "content").decode() == "[claude] " + body

    def test_length_ignores_prefix(self, db, rconn):
        r = db.save_memory("y" * 400, role="user", turn_id="t")
        assert "id" in r and "parent_id" not in r

    def test_code_omitted_before_prefix(self, db, rconn):
        r = db.save_memory("```a```", role="user", turn_id="t")
        assert rconn.hget(MEM + r["id"], "content") == b"[user] [code omitted]"

    def test_turn_scoped_guards(self, db, rconn):
        assert db.save_memory("ok")["saved"]
        assert db.save_memory("ok", role="user", turn_id="t1")["saved"]
        assert db.save_memory("ok", role="user", turn_id="t2")["saved"]
        assert db.save_memory("reply", role="claude", turn_id="t1")["saved"]
        assert db.save_memory("reply", role="claude", turn_id="t1")["status"] == "duplicate"
        assert rconn.exists("mem:sha:t1:" + database.sha1_hex("[user] ok"))
        long = "z " * 300
        db.save_memory(long, role="claude", turn_id="t1")
        assert rconn.exists("docsha:t1:" + database.sha1_hex("[claude] " + long.strip()))

    def test_skip_code_blocks(self, monkeypatch, capsys, rconn, isolated):
        p = isolated["data"] / "config.json"
        obj = json.loads(p.read_text(encoding="utf-8"))
        obj["skip_code_blocks"] = True
        p.write_text(json.dumps(obj), encoding="utf-8")
        submit(monkeypatch, capsys, "see ```code```")
        assert not records(rconn)


class TestDirectSave:
    def test_no_server(self, monkeypatch, capsys, rconn):
        submit(monkeypatch, capsys, "direct save please")
        r = records(rconn)[0]
        assert r["content"] == "[user] direct save please" and "embedding" not in r
        assert all(rconn.ttl(k) > 0 for k in rconn.keys("mem:*"))
        assert "sentence_transformers" not in sys.modules

    @pytest.mark.parametrize("decision", ["foreign", "starting"])
    def test_foreign_or_starting(self, monkeypatch, capsys, rconn, decision):
        def ensure(cfg, max_wait=60):
            if decision == "foreign":
                return cli._foreign(cfg, "/other/folder")
            cli._UNAVAILABLE_REASON = cli.STILL_STARTING
            return False

        monkeypatch.setattr(cli, "ensure_server", ensure)
        _c, out = submit(monkeypatch, capsys, "while unavailable")
        assert records(rconn)[0]["content"] == "[user] while unavailable"
        assert "_(memory search unavailable:" in out

    def test_repair_adds_vector(self, monkeypatch, capsys, rconn, db):
        submit(monkeypatch, capsys, "vector later")
        assert db.repair()["reembedded"] == 1
        assert "embedding" in records(rconn)[0]


class TestRedisDown:
    def _down(self, isolated):
        p = isolated["data"] / "config.json"
        obj = json.loads(p.read_text(encoding="utf-8"))
        good = obj["redis_url"]
        obj["redis_url"] = conftest.UNREACHABLE_URL
        p.write_text(json.dumps(obj), encoding="utf-8")
        return p, obj, good

    def test_no_server_redis_down(self, monkeypatch, capsys, isolated, rconn):
        p, obj, good = self._down(isolated)
        code, _out = submit(monkeypatch, capsys, "kept in the audit only")
        assert code == 0
        n = json.loads(paths.notices_path().read_text(encoding="utf-8"))
        assert "docker start redis-stack" in n["redis_unreachable"]["message"]
        _c, out = submit(monkeypatch, capsys, "next message")
        assert "N3MC Lite could not reach Redis" in out
        obj["redis_url"] = good
        p.write_text(json.dumps(obj), encoding="utf-8")
        submit(monkeypatch, capsys, "redis is back")
        assert "redis_unreachable" not in json.loads(paths.notices_path().read_text(encoding="utf-8"))

    def test_server_503(self, monkeypatch, capsys, app_factory, cfg):
        from fastapi.testclient import TestClient
        down = Database(dict(cfg, redis_url=conftest.UNREACHABLE_URL))
        conftest.route_http_to(monkeypatch, TestClient(app_factory(down)), cfg["server_port"])
        code, out = submit(monkeypatch, capsys, "server up redis down")
        assert code == 0 and "memory search unavailable" in out
        assert "redis_unreachable" in json.loads(paths.notices_path().read_text(encoding="utf-8"))


class TestHookSearchOutput:
    def test_with_server(self, monkeypatch, capsys, server_client, db):
        db.save_memory("the harbour light schedule is weekly")
        _c, out = submit(monkeypatch, capsys, "harbour light schedule")
        lines = out.splitlines()
        assert lines[:3] == ["# Recalled Memory Context", "Source: N3MC Lite (7-day working memory)",
                             "Search query: harbour light schedule"]
        assert any(line.startswith("### [") and " score=" in line and " saved=" in line for line in lines)

    def test_empty(self, monkeypatch, capsys, server_client):
        _c, out = submit(monkeypatch, capsys, "nothing stored yet")
        assert "_No memories found._" in out

    def test_no_server(self, monkeypatch, capsys):
        cli._record_start_failure("test")
        _c, out = submit(monkeypatch, capsys, "anything")
        assert out.startswith("# Recalled Memory Context\nSource: N3MC Lite (7-day working memory)\n\n")
        assert "_(memory search unavailable: the memory server could not start" in out
        assert out.index("> ⚠️ N3MC notice:") < out.index("_(memory search unavailable")


class TestHookSearchException:
    def test_bad_last_shown(self, monkeypatch, capsys):
        paths.notices_path().write_text(json.dumps({"k": {"message": "hello notice", "last_shown": "bad"}}),
                                        encoding="utf-8")
        _c, out = submit(monkeypatch, capsys, "q")
        assert "hello notice" in out
        assert isinstance(json.loads(paths.notices_path().read_text(encoding="utf-8"))["k"]["last_shown"], float)

    def test_search_exception(self, monkeypatch, capsys, rconn):
        def boom(*a, **k):
            raise RuntimeError("render failed")

        monkeypatch.setattr(cli, "_search_block", boom)
        _c, out = submit(monkeypatch, capsys, "message survives")
        assert "_(memory search unavailable: RuntimeError)_" in out
        tid = paths.turn_id_path("sess-1").read_text(encoding="utf-8")
        assert [r["turn_id"] for r in records(rconn) if r["content"] == "[user] message survives"] == [tid]


class TestHookExitStatus:
    @pytest.mark.parametrize("module,data", [
        ("n3mc_mcp.lite_hook", b'{"prompt": "hello", "session_id": "x"}'),
        ("n3mc_mcp.lite_hook", b"not json at all"),
        ("n3mc_mcp.lite_stop_hook", b'{"last_assistant_message": "bye", "session_id": "x"}'),
        ("n3mc_mcp.lite_stop_hook", b"\xff\xfe broken"),
    ])
    def test_exit_zero(self, isolated, module, data):
        cli._record_start_failure("no server in this test")     # never start a real server here
        p = isolated["data"] / "config.json"
        obj = json.loads(p.read_text(encoding="utf-8"))
        obj["redis_url"] = conftest.UNREACHABLE_URL
        p.write_text(json.dumps(obj), encoding="utf-8")
        env = dict(os.environ)
        env["CLAUDE_PROJECT_DIR"] = str(isolated["home"] / "proj")
        (isolated["home"] / "proj").mkdir()
        r = subprocess.run([sys.executable, "-m", module], input=data, env=env, capture_output=True,
                           timeout=120, cwd=str(isolated["home"]))
        assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
        assert audit_lines()
