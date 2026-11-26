"""Layer 5: hook flow, role prefixes, direct save (spec §9 Layer 5). Stand-in embeddings."""
import io
import json
import os
import subprocess
import sys

import pytest

from n3mc_mcp import cli, context, lite_hook, lite_stop_hook, paths

HEAD = "# Recalled Memory Context\nSource: N3MC Lite (7-day working memory)"


def _audit():
    try:
        return [json.loads(line) for line in open(paths.audit_path(), encoding="utf-8")]
    except OSError:
        return []


def _submit(prompt, session="sess-1", transcript=None, **extra):
    payload = {"session_id": session, "prompt": prompt, "cwd": os.getcwd(), "hook_event_name": "UserPromptSubmit"}
    if transcript:
        payload["transcript_path"] = str(transcript)
    payload.update(extra)
    return cli.hook_submit(json.dumps(payload))


def _stop(message, session="sess-1"):
    cli.save_claude_turn(json.dumps({"session_id": session, "last_assistant_message": message}))


def _records(rconn):
    out = []
    for k in rconn.scan_iter("mem:*"):
        if k.startswith(b"mem:sha:"):
            continue
        h = rconn.hgetall(k)
        out.append({kk.decode(): vv for kk, vv in h.items() if kk != b"embedding"})
    return out


class _StdIO:
    def __init__(self, text):
        self.buffer = io.BytesIO(text.encode("utf-8"))


class TestAuditFirst:
    @pytest.mark.parametrize("entry,name", [(lite_hook, "UserPromptSubmit"), (lite_stop_hook, "Stop")])
    def test_first_and_always(self, monkeypatch, entry, name):
        order = []
        real = lite_hook.audit_first
        monkeypatch.setattr(entry, "audit_first", lambda h, raw: (order.append("audit"), real(h, raw)))
        monkeypatch.setattr(entry, "run_step", lambda *a, **k: order.append("step"))
        monkeypatch.setattr(sys, "stdin", _StdIO("not json " + "x" * 5000))
        assert entry.main() == 0
        assert order[0] == "audit"
        rec = _audit()[-1]
        assert rec["hook"] == name and len(rec["raw"]) == 4096 and rec["payload"]["raw"].startswith("not json")

    def test_audit_failure_does_not_stop(self, monkeypatch):
        monkeypatch.setattr(paths, "audit_path", lambda: os.path.join(paths.data_dir(), "no", "such", "x"))
        monkeypatch.setattr(paths, "ensure_data_dir", lambda: (_ for _ in ()).throw(OSError("nope")))
        steps = []
        monkeypatch.setattr(lite_hook, "run_step", lambda *a, **k: steps.append(a))
        monkeypatch.setattr(sys, "stdin", _StdIO('{"prompt": "x"}'))
        assert lite_hook.main() == 0 and steps


class TestTurnIdHandoff:
    def test_handoff(self, server, rconn):
        _submit("first question")
        turn = open(paths.turn_id_path("sess-1"), encoding="utf-8").read()
        _stop("first answer")
        recs = {r["content"].decode(): r for r in _records(rconn)}
        assert recs["[user] first question"]["turn_id"].decode() == turn
        assert recs["[claude] first answer"]["turn_id"].decode() == turn
        assert open(paths.turn_id_path("sess-1"), encoding="utf-8").read() == turn

    def test_file_name_sanitized(self):
        p = paths.turn_id_path("a/b:c" + "x" * 100)
        assert os.path.basename(p) == "turn_id_" + ("a_b_c" + "x" * 100)[:64] + ".txt"
        assert os.path.basename(paths.turn_id_path("")) == "turn_id.txt"

    def test_no_file_new_uuid(self, server, rconn):
        _stop("orphan answer", session="other")
        assert os.path.exists(paths.turn_id_path("other"))

    def test_image_only(self, server, rconn):
        before = None
        out = _submit([{"type": "image", "source": {}}])
        assert out == context.empty_block()
        assert os.path.exists(paths.turn_id_path("sess-1"))
        assert not _records(rconn)


class TestStopReadsTurnIdFirst:
    def test_order(self, cfg, monkeypatch):
        cli._write_turn_id("s9", "turn-before")
        saved = []

        def ensure(cfg, max_wait=60):
            cli._write_turn_id("s9", "turn-after")
            return False

        monkeypatch.setattr(cli, "ensure_server", ensure)
        monkeypatch.setattr(cli, "_hook_save", lambda cfg, ok, content, role, s, t: saved.append(t))
        _stop("answer", session="s9")
        assert saved == ["turn-before"]


class TestTranscriptRecovery:
    def _write(self, tmp_path, lines):
        p = tmp_path / "t.jsonl"
        p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
        return p

    def _u(self, text, **kw):
        d = {"type": "user", "message": {"role": "user", "content": text}}
        d.update(kw)
        return d

    def _a(self, *parts, **kw):
        d = {"type": "assistant", "message": {"role": "assistant", "content": list(parts)}}
        d.update(kw)
        return d

    def test_last_text_block(self, tmp_path):
        p = self._write(tmp_path, [self._u("q1"),
                                   self._a({"type": "thinking", "thinking": "hm"}, {"type": "text", "text": "part 1"},
                                           {"type": "tool_use", "name": "x"}, {"type": "text", "text": "final part"}),
                                   self._u([{"type": "tool_result", "content": "r"}]),
                                   self._u("meta", isMeta=True),
                                   self._a({"type": "text", "text": "side"}, isSidechain=True),
                                   self._u("<command-name>/x</command-name>"),
                                   self._u("new prompt")])
        assert cli._last_assistant_from_transcript(str(p), "new prompt") == "final part"

    def test_interrupted_reply_found(self, tmp_path):
        p = self._write(tmp_path, [self._u("q"), self._a({"type": "text", "text": "partial"}),
                                   self._u([{"type": "text", "text": "[Request interrupted by user]"}]),
                                   self._u("now")])
        assert cli._last_assistant_from_transcript(str(p), "now") == "partial"

    def test_older_prompt_stops(self, tmp_path):
        p = self._write(tmp_path, [self._a({"type": "text", "text": "old"}), self._u("same"), self._u("same")])
        assert cli._last_assistant_from_transcript(str(p), "same") is None

    def test_saved_with_previous_turn(self, server, rconn, tmp_path):
        _submit("q1")
        turn1 = open(paths.turn_id_path("sess-1"), encoding="utf-8").read()
        p = self._write(tmp_path, [self._u("q1"), self._a({"type": "text", "text": "a1"}), self._u("q2")])
        _submit("q2", transcript=p)
        recs = {r["content"].decode(): r for r in _records(rconn)}
        assert recs["[claude] a1"]["turn_id"].decode() == turn1

    def test_stop_already_saved(self, server, rconn, tmp_path):
        _submit("q1")
        _stop("a1")
        p = self._write(tmp_path, [self._u("q1"), self._a({"type": "text", "text": "a1"}), self._u("q2")])
        _submit("q2", transcript=p)
        assert sum(1 for r in _records(rconn) if r["content"] == b"[claude] a1") == 1

    def test_unreadable(self, server, rconn, tmp_path):
        out = _submit("still works", transcript=tmp_path / "missing.jsonl")
        assert out.startswith(HEAD)
        assert any(r.get("stage") == "transcript_unreadable" for r in _audit())
        assert any(r["content"] == b"[user] still works" for r in _records(rconn))


class TestSystemPrefixes:
    @pytest.mark.parametrize("p", ["<task-notification>x", "<agent-message from=a>x", "<system-reminder>x"])
    def test_skipped(self, cfg, p, monkeypatch):
        cli._write_turn_id("sess-1", "keep-me")
        monkeypatch.setattr(cli, "ensure_server", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no")))
        out = _submit(p)
        assert out == HEAD + "\n\n_No memories found._"
        assert open(paths.turn_id_path("sess-1"), encoding="utf-8").read() == "keep-me"
        assert _audit()[-1]["stage"] == "skipped"

    def test_div_not_skipped(self, cfg, monkeypatch):
        called = []
        monkeypatch.setattr(cli, "ensure_server", lambda *a, **k: called.append(1) or False)
        monkeypatch.setattr(cli, "_hook_save", lambda *a: None)
        _submit("<div>hello</div>")
        assert called


class TestRolePrefix:
    def test_short(self, server, rconn):
        _submit("short message")
        rec = [r for r in _records(rconn) if r["content"] == b"[user] short message"][0]
        assert rec["agent_name"] == b"claude-code" and rec["session_id"] == b"sess-1"

    def test_long(self, db, rconn, cfg):
        body = "x" * 1000
        r = db.save_memory(body, role="claude", turn_id="t", agent_name="claude-code")
        assert rconn.hget("doc:" + r["parent_id"], "content").decode() == "[claude] " + body
        n = r["chunks"]
        assert rconn.hget("mem:" + r["ids"][0], "content").decode().startswith(f"[claude 1/{n}] ")

    def test_one_chunk(self, db, rconn):
        body = "y" * 399 + " " + "z" * 10   # cut at the space; the 10-character tail joins the chunk
        r = db.save_memory(body, role="claude", turn_id="t")
        assert r["chunks"] == 1
        assert rconn.hget("mem:" + r["ids"][0], "content").decode() == "[claude] " + body

    def test_length_ignores_prefix(self, db):
        assert "id" in db.save_memory("z" * 400, role="user", turn_id="t")

    def test_code_before_prefix(self, db, rconn):
        r = db.save_memory("```\ncode\n```", role="user", turn_id="t")
        assert rconn.hget("mem:" + r["id"], "content") == b"[user] [code omitted]"

    def test_guard_per_turn(self, db):
        assert db.save_memory("ok")["saved"]
        assert db.save_memory("ok", role="user", turn_id="t1")["saved"]
        assert db.save_memory("ok", role="user", turn_id="t2")["saved"]
        assert db.save_memory("ok", role="user", turn_id="t2")["status"] == "duplicate"

    def test_skip_code_blocks(self, server, cfg, rconn, isolate):
        p = isolate["data"] / "config.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        d["skip_code_blocks"] = True
        p.write_text(json.dumps(d), encoding="utf-8")
        server.app_obj.state.cfg["skip_code_blocks"] = True
        r = cli._hook_save(cfg, True, "x\n```\ny\n```", "user", "s", "t")
        assert r["status"] == "skipped_code"


class TestDirectSave:
    @pytest.mark.parametrize("why", ["none", "foreign", "starting"])
    def test_direct(self, cfg, rconn, monkeypatch, why):
        monkeypatch.setattr(cli, "ensure_server", lambda cfg, max_wait=60: False)
        _submit("direct message")
        rec = [r for r in _records(rconn) if r["content"] == b"[user] direct message"][0]
        assert rec["agent_name"] == b"claude-code"
        key = [k for k in rconn.scan_iter("mem:*") if not k.startswith(b"mem:sha:")
               and rconn.hget(k, "content") == b"[user] direct message"][0]
        assert not rconn.hexists(key, "embedding") and rconn.ttl(key) > 0

    def test_no_model_import(self):
        code = ("import sys, json, os\nfrom n3mc_mcp import cli\n"
                "cfg = cli.load_config()\n"
                "try:\n    cli.direct_save(cfg, 'x', role='user', turn_id='t')\nexcept Exception:\n    pass\n"
                "print('sentence_transformers' in sys.modules, 'torch' in sys.modules)")
        cp = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                            env=dict(os.environ))
        assert cp.stdout.strip() == "False False", cp.stderr

    def test_repair_adds_vector(self, cfg, db, rconn, monkeypatch):
        r = cli.direct_save(cfg, "repair later", role="user", turn_id="t")
        assert not rconn.hexists("mem:" + r["id"], "embedding")
        db.repair()
        assert rconn.hexists("mem:" + r["id"], "embedding")


class TestRedisDown:
    def test_server_503(self, cfg, monkeypatch):
        monkeypatch.setattr(cli, "_post", lambda port, path, body, timeout=30: (503, {"status": "error"}))
        assert cli._hook_save(cfg, True, "lost?", "user", "s", "t") is None
        n = json.load(open(paths.notices_path(), encoding="utf-8"))
        assert "docker start redis-stack" in n["redis_unreachable"]["message"]

    def test_no_server_no_redis(self, cfg, monkeypatch):
        monkeypatch.setattr(cli, "ensure_server", lambda cfg, max_wait=60: False)
        cfg2 = dict(cfg)
        monkeypatch.setattr(cli, "load_config", lambda: dict(cfg2, redis_url="redis://127.0.0.1:1/0"))
        out = _submit("kept in audit")
        assert out.startswith(HEAD)
        assert any(r.get("hook") == "notice" and r.get("key") == "redis_unreachable" for r in _audit())
        n = json.load(open(paths.notices_path(), encoding="utf-8"))
        assert "redis_unreachable" in n

    def test_shown_then_cleared(self, cfg, rconn, monkeypatch):
        cli._notify("redis_unreachable", "Redis was down")
        monkeypatch.setattr(cli, "ensure_server", lambda cfg, max_wait=60: False)
        out = _submit("after recovery")
        assert "> ⚠️ N3MC notice: Redis was down" in out
        assert "redis_unreachable" not in json.load(open(paths.notices_path(), encoding="utf-8"))


class TestHookSearchOutput:
    def test_block(self, server, db):
        db.save_memory("hook output words")
        out = _submit("hook output words")
        assert out.startswith(HEAD + "\nSearch query: hook output words\n\n")
        assert "### [" in out and "score=" in out and "saved=" in out

    def test_empty(self, server):
        out = _submit("nothing stored yet")
        assert "_No memories found._" in out

    def test_no_server(self, cfg, monkeypatch, rconn):
        monkeypatch.setattr(cli, "ensure_server", lambda cfg, max_wait=60: False)
        out = _submit("anything")
        assert out.startswith(HEAD + "\n\n") and "_(memory search unavailable: " in out

    def test_due_notice_after_heading(self, cfg, monkeypatch, rconn):
        cli._notify("stale_server", "old server stuck")
        monkeypatch.setattr(cli, "ensure_server", lambda cfg, max_wait=60: False)
        out = _submit("anything")
        assert out.startswith(HEAD + "\n\n> ⚠️ N3MC notice: old server stuck")


class TestHookSearchException:
    def test_bad_last_shown(self, cfg):
        paths.ensure_data_dir()
        json.dump({"k": {"message": "m", "last_shown": "garbage"}}, open(paths.notices_path(), "w"))
        assert cli.take_due_notices() == ["m"]
        assert isinstance(json.load(open(paths.notices_path()))["k"]["last_shown"], float)

    def test_exception_still_saves(self, server, rconn, monkeypatch):
        monkeypatch.setattr(cli, "_hook_search", lambda *a: (_ for _ in ()).throw(KeyError("x")))
        out = _submit("save me anyway")
        assert out == HEAD + "\n\n_(memory search unavailable: KeyError)_"
        assert any(r["content"] == b"[user] save me anyway" for r in _records(rconn))


class TestHookExitStatus:
    @pytest.mark.parametrize("entry", [lite_hook, lite_stop_hook])
    def test_exit_zero(self, entry, monkeypatch):
        monkeypatch.setattr(entry, "run_step", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        monkeypatch.setattr(sys, "stdin", _StdIO("{}"))
        assert entry.main() == 0

    def test_real_process_exit_zero(self, tmp_path):
        env = dict(os.environ)
        env["N3MC_DATA_DIR"] = str(tmp_path / "d")
        os.makedirs(env["N3MC_DATA_DIR"])
        with open(os.path.join(env["N3MC_DATA_DIR"], "config.json"), "w") as f:
            json.dump({"redis_url": "redis://127.0.0.1:1/0", "server_port": 1}, f)
        env.pop("CLAUDE_PROJECT_DIR", None)
        work = tmp_path / "w"
        work.mkdir()
        payload = json.dumps({"last_assistant_message": "", "cwd": str(work)}).encode()
        cp = subprocess.run([sys.executable, "-m", "n3mc_mcp.lite_stop_hook"], input=payload, cwd=work,
                            capture_output=True, env=env, timeout=120)
        assert (work / ".claude" / "rules" / "n3mc-lite-behavior.md").exists()
        assert cp.returncode == 0
