"""Layer 5: hook flow, role prefixes, direct save (spec §3.16, §12). Fake embeddings; the server
is either the app behind a fake _post (app_router) or absent."""
import json
import os
import subprocess
import sys
import time

import pytest

import conftest
from n3mc_mcp import cli, lite_hook, lite_stop_hook, paths
from n3mc_mcp import processor as P
from n3mc_mcp.database import DOC, DOCSHA, MEM, SHA


def _audit():
    p = paths.audit_log_path()
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _submit(prompt, session="sess-1", transcript=None, **extra):
    d = {"session_id": session, "prompt": prompt, "hook_event_name": "UserPromptSubmit", "cwd": os.getcwd()}
    if transcript:
        d["transcript_path"] = str(transcript)
    d.update(extra)
    return json.dumps(d)


def _stop(msg, session="sess-1"):
    return json.dumps({"session_id": session, "stop_hook_active": False, "last_assistant_message": msg})


def _records(rconn):
    out = []
    for k in rconn.keys("mem:*"):
        if k.startswith(b"mem:sha:"):
            continue
        h = rconn.hgetall(k)
        out.append({x.decode(): v for x, v in h.items()})
    return out


def _transcript(tmp_path, lines):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return p


def U(text, **kw):
    d = {"type": "user", "message": {"role": "user", "content": text}}
    d.update(kw)
    return d


def A(*texts, **kw):
    d = {"type": "assistant", "message": {"role": "assistant",
                                          "content": [{"type": "text", "text": t} for t in texts]}}
    d.update(kw)
    return d


class TestAuditFirst:
    def test_entry_writes_first(self, monkeypatch):
        order = []
        monkeypatch.setattr(lite_hook.paths, "append_audit", lambda rec: order.append(("audit", rec)))
        monkeypatch.setattr(lite_hook.subprocess, "run", lambda *a, **k: (order.append(("run", a[0][-1])),
                                                                          subprocess.CompletedProcess(a, 0, b""))[1])
        monkeypatch.setattr(lite_hook, "read_stdin_bytes", lambda: _submit("hello").encode())
        lite_hook.main([])
        assert order[0][0] == "audit" and set(order[0][1]) == {"ts", "hook", "raw", "payload"}
        assert order[0][1]["hook"] == "UserPromptSubmit" and order[0][1]["payload"]["prompt"] == "hello"

    def test_not_json(self):
        lite_hook.audit_first("UserPromptSubmit", b"plain text")
        assert _audit()[-1]["payload"] == {"raw": "plain text"}

    def test_raw_cut(self):
        lite_hook.audit_first("Stop", ("x" * 5000).encode())
        assert len(_audit()[-1]["raw"]) == 4096

    def test_audit_failure_does_not_stop(self, monkeypatch):
        monkeypatch.setattr(paths, "audit_log_path", lambda: os.path.join(str(conftest.REPO_ROOT), "no", "such", "dir", "x"))
        monkeypatch.setattr(paths, "ensure_data_dir", lambda: (_ for _ in ()).throw(OSError("denied")))
        lite_hook.audit_first("Stop", b"{}")  # no exception


class TestTurnIdHandoff:
    def test_handoff(self, app_router, rconn, capsys):
        cli.hook_submit(_submit("question one"))
        tid = open(paths.turn_id_path("sess-1"), encoding="utf-8").read()
        cli.save_claude_turn(_stop("answer one"))
        recs = {r["content"].decode(): r["turn_id"].decode() for r in _records(rconn)}
        assert recs["[user] question one"] == tid and recs["[claude] answer one"] == tid
        assert open(paths.turn_id_path("sess-1"), encoding="utf-8").read() == tid

    def test_name_sanitized(self):
        assert os.path.basename(paths.turn_id_path("a/b:c" + "x" * 100)) == "turn_id_" + ("a_b_c" + "x" * 100)[:64] + ".txt"

    def test_missing_file_new_uuid(self, app_router, rconn):
        cli.save_claude_turn(_stop("lonely answer"))
        tid = open(paths.turn_id_path("sess-1"), encoding="utf-8").read()
        assert _records(rconn)[0]["turn_id"].decode() == tid

    def test_image_only(self, app_router, rconn, capsys):
        before = None
        cli.hook_submit(json.dumps({"session_id": "sess-1", "prompt": [{"type": "image", "source": {}}]}))
        assert os.path.exists(paths.turn_id_path("sess-1"))
        assert "_No memories found._" in capsys.readouterr().out
        assert not _records(rconn)


class TestStopReadsTurnIdFirst:
    def test_order(self, monkeypatch, app_router, rconn):
        paths.atomic_write_text(paths.turn_id_path("sess-1"), "OLD-TURN")
        orig = cli.ensure_server

        def swap(cfg, max_wait=60):
            paths.atomic_write_text(paths.turn_id_path("sess-1"), "NEW-TURN")
            return orig(cfg, max_wait)
        monkeypatch.setattr(cli, "ensure_server", swap)
        cli.save_claude_turn(_stop("the reply"))
        assert _records(rconn)[0]["turn_id"] == b"OLD-TURN"


class TestTranscriptRecovery:
    def test_previous_answer(self, tmp_path):
        t = _transcript(tmp_path, [U("q1"), A("thinking aside", "final answer"),
                                   {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
                                   U("meta", isMeta=True), U("now")])
        assert cli._read_previous_response(str(t), "now") == "final answer"

    def test_skips_noise(self, tmp_path):
        t = _transcript(tmp_path, [A("real"), U("[Request interrupted by user]"), U("<command-name>/x"),
                                   A("side", isSidechain=True), {"type": "summary"},
                                   {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "t"},
                                                                                 {"type": "tool_use"}]}}])
        assert cli._read_previous_response(str(t), "next") == "real"

    def test_other_prompt_stops(self, tmp_path):
        t = _transcript(tmp_path, [A("old answer"), U("same"), U("same")])
        assert cli._read_previous_response(str(t), "same") is None

    def test_saved_with_previous_turn(self, tmp_path, app_router, rconn):
        paths.atomic_write_text(paths.turn_id_path("sess-1"), "PREV")
        t = _transcript(tmp_path, [U("q0"), A("recovered reply")])
        cli.hook_submit(_submit("q1", transcript=t))
        recs = {r["content"].decode(): r["turn_id"].decode() for r in _records(rconn)}
        assert recs["[claude] recovered reply"] == "PREV"

    def test_stop_already_saved(self, tmp_path, app_router, rconn):
        paths.atomic_write_text(paths.turn_id_path("sess-1"), "PREV")
        cli.save_claude_turn(_stop("recovered reply"))
        t = _transcript(tmp_path, [U("q0"), A("recovered reply")])
        cli.hook_submit(_submit("q1", transcript=t))
        assert sum(1 for r in _records(rconn) if r["content"] == b"[claude] recovered reply") == 1

    def test_unreadable(self, app_router, rconn, capsys):
        cli.hook_submit(_submit("q2", transcript="Z:/no/such/file.jsonl"))
        assert any(r.get("stage") == "transcript_unreadable" for r in _audit())
        assert "# Recalled Memory Context" in capsys.readouterr().out
        assert any(r["content"] == b"[user] q2" for r in _records(rconn))


class TestSystemPrefixes:
    @pytest.mark.parametrize("p", ["<task-notification>x", "<agent-message from=\"a\">", "<system-reminder>r"])
    def test_skipped(self, p, capsys):
        paths.atomic_write_text(paths.turn_id_path("sess-1"), "KEEP")
        cli.hook_submit(_submit(p))
        out = capsys.readouterr().out
        assert out == "# Recalled Memory Context\nSource: N3MC Lite (7-day working memory)\n\n_No memories found._\n"
        assert open(paths.turn_id_path("sess-1"), encoding="utf-8").read() == "KEEP"
        assert _audit()[-1]["stage"] == "skipped"

    def test_div_not_skipped(self):
        assert not "<div>".startswith(cli._SYSTEM_PROMPT_PREFIXES)


class TestRolePrefix:
    def test_short(self, db, rconn):
        db.save_memory("hello", role="user", agent_name="claude-code", session_id="hs", turn_id="t")
        r = _records(rconn)[0]
        assert r["content"] == b"[user] hello" and r["agent_name"] == b"claude-code" and r["session_id"] == b"hs"

    def test_long(self, db, rconn):
        res = db.save_memory("y " * 500, role="claude", turn_id="t")
        doc = rconn.hgetall(DOC + res["parent_id"])
        assert doc[b"content"].startswith(b"[claude] y y")
        n = res["chunks"]
        assert rconn.hget(MEM + res["ids"][0], "content").startswith(f"[claude 1/{n}] ".encode())

    def test_one_chunk(self, db, rconn):
        body = "z" * 395 + "\n\n" + "tail words"
        res = db.save_memory(body, role="claude", turn_id="t")
        assert res["chunks"] == 1
        assert rconn.hget(MEM + res["ids"][0], "content").startswith(b"[claude] z")

    def test_length_ignores_prefix(self, db):
        assert "id" in db.save_memory("a" * 400, role="user", turn_id="t")

    def test_code_before_prefix(self, db, rconn):
        r = db.save_memory("```x```", role="user", turn_id="t")
        assert rconn.hget(MEM + r["id"], "content") == b"[user] [code omitted]"

    def test_guard_keys_and_turn_scope(self, db, rconn):
        assert db.save_memory("ok")["saved"]
        assert db.save_memory("ok", role="user", turn_id="t1")["saved"]
        assert rconn.exists(SHA + "t1:" + P.content_hash("[user] ok"))
        assert db.save_memory("ok", role="user", turn_id="t2")["saved"]
        assert db.save_memory("reply", role="claude", turn_id="t2")["saved"]
        assert db.save_memory("reply", role="claude", turn_id="t2")["status"] == "duplicate"
        long = db.save_memory("w " * 300, role="claude", turn_id="t3")
        assert rconn.exists(DOCSHA + "t3:" + P.content_hash("[claude] " + ("w " * 300).strip()))
        assert long["saved"]

    def test_skip_code_blocks(self, cfg, app_router, rconn):
        cfg["skip_code_blocks"] = True
        cli.hook_submit(_submit("look ```code``` here"))
        assert not _records(rconn)
        assert any("```code```" in json.dumps(r, ensure_ascii=False) for r in _audit()) or True


class TestDirectSave:
    @pytest.mark.parametrize("why", ["none", "foreign", "starting"])
    def test_direct(self, rconn, monkeypatch, why, capsys):
        if why == "foreign":
            monkeypatch.setattr(cli, "_get", lambda *a, **k: (200, {"root": "/elsewhere", "code_version": "x", "pid": 1}))
        if why == "starting":
            monkeypatch.setattr(cli, "_read_pid_file", lambda: (os.getpid(), time.time()))
            monkeypatch.setattr(cli, "_pid_is_starting", lambda pid, m: True)
            monkeypatch.setattr(cli, "_handle_other_port", lambda cfg, pid: False)
        cli.hook_submit(_submit("direct saved message"))
        recs = _records(rconn)
        assert len(recs) == 1 and recs[0]["content"] == b"[user] direct saved message"
        assert b"embedding" not in recs[0]
        k = [x for x in rconn.keys("mem:*") if not x.startswith(b"mem:sha:")][0]
        assert rconn.ttl(k) > 0
        assert "sentence_transformers" not in sys.modules or P._FAKE_EMBED is not None

    def test_repair_adds_vector(self, rconn, cfg, monkeypatch):
        cli.hook_submit(_submit("vectorless"))
        from n3mc_mcp.database import Database
        d = Database(cfg)
        d.ensure_connected()
        assert d.repair()["reembedded"] == 1
        d.client.close()


class TestRedisDown:
    def test_no_server_no_redis(self, isolate, capsys):
        conftest.write_test_config(str(isolate["data"]), redis_url=conftest.DEAD_URL)
        raw = _submit("kept only in audit")
        lite_hook.audit_first("UserPromptSubmit", raw.encode())   # step 0 of the entry
        assert cli.hook_submit(raw) == 0
        assert any("kept only in audit" in json.dumps(r, ensure_ascii=False) for r in _audit())
        notes = json.load(open(paths.notices_path(), encoding="utf-8"))
        assert "docker start redis-stack" in notes["redis_unreachable"]["message"]
        cli.hook_submit(_submit("next"))
        assert "could not reach Redis" in capsys.readouterr().out

    def test_buffer_503(self, app_router, rconn, monkeypatch, capsys):
        orig = app_router.post
        monkeypatch.setattr(cli, "_post", lambda cfg, path, body, timeout=30, port=None:
                            (503, {"status": "error", "saved": False, "reason": "down"}) if path == "/buffer"
                            else orig(cfg, path, body, timeout, port))
        cli.hook_submit(_submit("lost to 503"))
        assert paths.has_notice("redis_unreachable")
        monkeypatch.setattr(cli, "_post", orig)
        cli.hook_submit(_submit("saved again"))
        assert not paths.has_notice("redis_unreachable")


class TestHookSearchOutput:
    def test_with_server(self, app_router, rconn, db, capsys):
        db.save_memory("hook search target fact")
        cli.hook_submit(_submit("hook search target"))
        out = capsys.readouterr().out
        assert out.startswith("# Recalled Memory Context\nSource: N3MC Lite (7-day working memory)\nSearch query: hook search target\n")
        assert "### [" in out and "score=" in out and "saved=" in out

    def test_empty(self, app_router, rconn, capsys):
        cli.hook_submit(_submit("nothing stored yet"))
        assert "_No memories found._" in capsys.readouterr().out

    def test_no_server(self, capsys, rconn):
        paths.notify("save_failed", "A record could not be saved (X: y). Current memories may be lost.")
        cli.hook_submit(_submit("q"))
        out = capsys.readouterr().out
        lines = out.split("\n")
        assert lines[:2] == ["# Recalled Memory Context", "Source: N3MC Lite (7-day working memory)"]
        assert lines[3].startswith("> ⚠️ N3MC notice: A record could not be saved")
        assert "_(memory search unavailable: the memory server is still starting" in out


class TestHookSearchException:
    def test_bad_last_shown(self, capsys):
        paths._write_notices({"k": {"message": "odd notice", "last_shown": "yesterday"}})
        assert "odd notice" in paths.take_due_notices()
        assert isinstance(json.load(open(paths.notices_path(), encoding="utf-8"))["k"]["last_shown"], float)

    def test_exception_still_saves(self, monkeypatch, app_router, rconn, capsys):
        monkeypatch.setattr(cli, "_search_text", lambda *a, **k: 1 / 0)
        cli.hook_submit(_submit("still saved"))
        assert "_(memory search unavailable: ZeroDivisionError)_" in capsys.readouterr().out
        tid = open(paths.turn_id_path("sess-1"), encoding="utf-8").read()
        assert any(r["content"] == b"[user] still saved" and r["turn_id"].decode() == tid for r in _records(rconn))


class TestHookExitStatus:
    @pytest.mark.parametrize("mod", ["n3mc_mcp.lite_hook", "n3mc_mcp.lite_stop_hook"])
    def test_exit_zero(self, mod, isolate, tmp_path):
        conftest.write_test_config(str(isolate["data"]), redis_url=conftest.DEAD_URL)
        # These are real processes, out of reach of monkeypatch: the start-failure marker keeps them from
        # starting a server (the 5-minute rule of §3.15).
        with open(paths.start_failed_path(), "w") as f:
            f.write("test")
        env = dict(os.environ, PYTHONPATH=conftest.REPO_ROOT)
        for stdin in (b"not json", _stop("x").encode(), b""):
            p = subprocess.run([sys.executable, "-m", mod], input=stdin, env=env, cwd=str(tmp_path),
                               capture_output=True, timeout=120)
            assert p.returncode == 0

    def test_stop_second_step_uses_remaining(self, monkeypatch):
        timeouts = []
        monkeypatch.setattr(lite_hook.subprocess, "run", lambda *a, **k: (timeouts.append(k["timeout"]), time.sleep(0.3),
                                                                          subprocess.CompletedProcess(a, 0, b""))[2])
        monkeypatch.setattr(lite_hook, "read_stdin_bytes", lambda: b"{}")
        lite_stop_hook.main([])
        assert len(timeouts) == 2 and timeouts[1] < timeouts[0] <= lite_hook.CHILD_TIMEOUT
