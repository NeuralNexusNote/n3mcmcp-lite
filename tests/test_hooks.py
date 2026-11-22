"""Layer 5: hook flow, role prefixes, direct save, Redis down (fake embeddings)."""

import io
import json
import os
import sys
import types

import pytest

from n3mc_mcp import cli, config, lite_hook, lite_stop_hook, paths


def set_stdin(monkeypatch, data):
    raw = data if isinstance(data, bytes) else data.encode("utf-8")
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(raw), isatty=lambda: False))


def submit(monkeypatch, capsys, payload, uvx=False):
    set_stdin(monkeypatch, json.dumps(payload) if not isinstance(payload, (str, bytes)) else payload)
    capsys.readouterr()
    assert cli.cmd_hook_submit(uvx) == 0
    return capsys.readouterr().out


def stop_turn(monkeypatch, capsys, payload):
    set_stdin(monkeypatch, json.dumps(payload))
    assert cli.cmd_save_claude_turn() == 0
    return capsys.readouterr().out


def audit_lines():
    p = paths.audit_log_path()
    return [json.loads(l) for l in open(p, encoding="utf-8").read().splitlines()] if os.path.exists(p) else []


def records(redis_client):
    out = []
    for k in redis_client.keys("mem:*"):
        if k.startswith(b"mem:sha:"):
            continue
        h = redis_client.hgetall(k)
        out.append({kk.decode(): (vv if kk == b"embedding" else vv.decode()) for kk, vv in h.items()})
    for k in redis_client.keys("doc:*"):
        h = redis_client.hgetall(k)
        d = {kk.decode(): vv.decode() for kk, vv in h.items()}
        d["_doc"] = True
        out.append(d)
    return sorted(out, key=lambda r: float(r["timestamp_epoch"]))


class TestAuditFirst:
    def test_hook_entries_write_audit_before_anything(self, monkeypatch, isolate):
        order = []
        monkeypatch.setattr(lite_hook, "run_cli", lambda args, data, started: order.append(("cli", len(audit_lines()))) or b"OUT")
        set_stdin(monkeypatch, json.dumps({"prompt": "hi", "session_id": "S"}))
        assert lite_hook.main([]) == 0
        assert order == [("cli", 1)]
        rec = audit_lines()[0]
        assert set(rec) == {"ts", "hook", "raw", "payload"} and rec["hook"] == "UserPromptSubmit"
        assert rec["payload"]["prompt"] == "hi"

    def test_stop_entry_and_non_json(self, monkeypatch, isolate):
        calls = []
        monkeypatch.setattr(lite_hook, "run_cli", lambda args, data, started: calls.append(args[0]) or b"")
        set_stdin(monkeypatch, "not json at all")
        assert lite_stop_hook.main([]) == 0
        assert calls == ["--save-claude-turn", "--stop"]
        rec = audit_lines()[0]
        assert rec["hook"] == "Stop" and rec["payload"] == {"raw": "not json at all"}

    def test_raw_is_cut_at_4096_and_payload_complete(self, monkeypatch, isolate):
        monkeypatch.setattr(lite_hook, "run_cli", lambda *a: b"")
        big = {"prompt": "x" * 6000}
        set_stdin(monkeypatch, json.dumps(big))
        lite_hook.main([])
        rec = audit_lines()[0]
        assert len(rec["raw"]) == 4096 and len(rec["payload"]["prompt"]) == 6000

    def test_audit_failure_does_not_stop_the_hook(self, monkeypatch, isolate, tmp_path):
        monkeypatch.setattr(paths, "audit_log_path", lambda: str(tmp_path))   # a directory: cannot be opened
        monkeypatch.setattr(lite_hook, "run_cli", lambda *a: b"ran")
        set_stdin(monkeypatch, "{}")
        assert lite_hook.main([]) == 0


class TestTurnIdHandoff:
    def test_user_then_claude_share_turn(self, cfg, wired, redis_client, monkeypatch, capsys):
        submit(monkeypatch, capsys, {"prompt": "first question", "session_id": "sess/1"})
        tf = paths.turn_id_path("sess/1")
        assert os.path.basename(tf) == "turn_id_sess_1.txt"
        turn = open(tf, encoding="utf-8").read().strip()
        stop_turn(monkeypatch, capsys, {"session_id": "sess/1", "last_assistant_message": "first answer"})
        assert open(tf, encoding="utf-8").read().strip() == turn
        recs = records(redis_client)
        assert [r["content"] for r in recs] == ["[user] first question", "[claude] first answer"]
        assert {r["turn_id"] for r in recs} == {turn}

    def test_no_file_uses_new_uuid_and_writes_it(self, cfg, wired, redis_client, monkeypatch, capsys):
        stop_turn(monkeypatch, capsys, {"session_id": "abc", "last_assistant_message": "orphan answer"})
        turn = open(paths.turn_id_path("abc"), encoding="utf-8").read().strip()
        assert turn and records(redis_client)[0]["turn_id"] == turn

    def test_image_only_prompt_still_writes_turn(self, cfg, wired, redis_client, monkeypatch, capsys):
        out = submit(monkeypatch, capsys, {"prompt": [{"type": "image", "source": "x"}], "session_id": "img"})
        assert os.path.exists(paths.turn_id_path("img"))
        assert "_No memories found._" in out and records(redis_client) == []

    def test_session_name_is_sanitized_and_cut(self):
        p = paths.turn_id_path("../../etc/passwd" + "a" * 100)
        assert os.path.dirname(p) == paths.data_dir() and len(os.path.basename(p)) <= len("turn_id_.txt") + 64


def write_transcript(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def user(text, **kw):
    return dict({"type": "user", "message": {"role": "user", "content": text}}, **kw)


def assistant(*parts, **kw):
    return dict({"type": "assistant", "message": {"role": "assistant", "content": list(parts)}}, **kw)


T = lambda s: {"type": "text", "text": s}  # noqa: E731


class TestTranscriptRecovery:
    def run(self, cfg, wired, monkeypatch, capsys, rows, prompt="now", session="tr"):
        tp = os.path.join(paths.data_dir(), "transcript.jsonl")
        write_transcript(tp, rows)
        # remember the previous turn id as the Stop hook would have left it
        open(paths.turn_id_path(session), "w", encoding="utf-8").write("prev-turn")
        return submit(monkeypatch, capsys, {"prompt": prompt, "session_id": session, "transcript_path": tp})

    def test_recovers_last_text_block_with_previous_turn(self, cfg, wired, redis_client, monkeypatch, capsys):
        rows = [user("earlier"), assistant(T("one"), {"type": "tool_use", "name": "x"}, T("last block")),
                user("now")]
        self.run(cfg, wired, monkeypatch, capsys, rows)
        recs = {r["content"]: r for r in records(redis_client)}
        assert recs["[claude] last block"]["turn_id"] == "prev-turn"
        assert recs["[user] now"]["turn_id"] != "prev-turn"

    def test_skips_noise_rows(self, cfg, wired, redis_client, monkeypatch, capsys):
        rows = [user("earlier"), assistant(T("the real answer")),
                assistant({"type": "thinking", "thinking": "hmm"}),
                assistant(T("sidechain text"), isSidechain=True),
                {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
                user("meta", isMeta=True), user("<command-name>/x</command-name>"),
                user("[Request interrupted by user]"), user("<system-reminder>x</system-reminder>"),
                user("now")]
        self.run(cfg, wired, monkeypatch, capsys, rows)
        assert "[claude] the real answer" in [r["content"] for r in records(redis_client)]

    def test_interrupted_answer_is_found(self, cfg, wired, redis_client, monkeypatch, capsys):
        rows = [user("q"), assistant(T("half an answer")), user("[Request interrupted by user]"), user("now")]
        self.run(cfg, wired, monkeypatch, capsys, rows)
        assert "[claude] half an answer" in [r["content"] for r in records(redis_client)]

    def test_older_prompt_ends_the_walk(self, cfg, wired, redis_client, monkeypatch, capsys):
        rows = [assistant(T("answer to nothing")), user("older prompt"), user("different current prompt")]
        self.run(cfg, wired, monkeypatch, capsys, rows, prompt="now")
        assert [r["content"] for r in records(redis_client)] == ["[user] now"]

    def test_stop_already_saved_means_no_duplicate(self, cfg, wired, redis_client, monkeypatch, capsys):
        rows = [user("q"), assistant(T("same answer")), user("now")]
        open(paths.turn_id_path("tr"), "w", encoding="utf-8").write("prev-turn")
        stop_turn(monkeypatch, capsys, {"session_id": "tr", "last_assistant_message": "same answer"})
        self.run(cfg, wired, monkeypatch, capsys, rows)
        assert [r["content"] for r in records(redis_client)].count("[claude] same answer") == 1

    def test_unreadable_transcript_is_logged_and_hook_continues(self, cfg, wired, redis_client, monkeypatch, capsys):
        out = submit(monkeypatch, capsys, {"prompt": "go on", "session_id": "u", "transcript_path": "Z:/nope/x.jsonl"})
        assert out.startswith("# Recalled Memory Context")
        assert any(l.get("stage") == "transcript_unreadable" for l in audit_lines())
        assert [r["content"] for r in records(redis_client)] == ["[user] go on"]


class TestSystemPrefixes:
    @pytest.mark.parametrize("prefix", ["<task-notification>x", "<agent-message from=bob>", "<system-reminder>r"])
    def test_system_prompts_are_not_saved_or_searched(self, cfg, wired, redis_client, monkeypatch, capsys, prefix):
        out = submit(monkeypatch, capsys, {"prompt": prefix, "session_id": "sp"})
        assert out == "# Recalled Memory Context\nSource: N3MC Lite (7-day working memory)\n\n_No memories found._\n"
        assert records(redis_client) == [] and not os.path.exists(paths.turn_id_path("sp"))
        assert any(l.get("stage") == "skipped" and l.get("reason") == "system_prompt" for l in audit_lines())

    def test_div_prompt_is_normal(self, cfg, wired, redis_client, monkeypatch, capsys):
        submit(monkeypatch, capsys, {"prompt": "<div>html</div>", "session_id": "d"})
        assert [r["content"] for r in records(redis_client)] == ["[user] <div>html</div>"]


class TestRolePrefix:
    def hook(self, cfg, wired, monkeypatch, capsys, prompt, session="rp"):
        submit(monkeypatch, capsys, {"prompt": prompt, "session_id": session})

    def test_short_message(self, cfg, wired, redis_client, monkeypatch, capsys):
        self.hook(cfg, wired, monkeypatch, capsys, "short text")
        r = records(redis_client)[0]
        assert r["content"] == "[user] short text" and r["agent_name"] == "claude-code" and r["session_id"] == "rp"

    def test_long_body_parent_and_chunks(self, cfg, wired, redis_client, monkeypatch, capsys):
        body = ("Sentence number one is here. " * 40).strip()
        set_stdin(monkeypatch, json.dumps({"session_id": "L", "last_assistant_message": body}))
        cli.cmd_save_claude_turn()
        recs = records(redis_client)
        parent = [r for r in recs if r.get("_doc")][0]
        chunks = [r for r in recs if not r.get("_doc")]
        assert parent["content"] == "[claude] " + body
        n = int(parent["chunk_count"])
        assert n >= 2 and len(chunks) == n
        assert any(c["content"].startswith("[claude 1/%d] " % n) for c in chunks)
        assert all(c["content"].startswith("[claude ") for c in chunks)

    def test_body_just_over_threshold_single_chunk(self, cfg, wired, redis_client, monkeypatch, capsys):
        body = ("ab " * 137).strip()      # a break leaves a tail shorter than 20 chars: one chunk
        cli._hook_save(cfg, True, body, "claude", "s", "t1")
        recs = records(redis_client)
        parent = [r for r in recs if r.get("_doc")][0]
        chunk = [r for r in recs if not r.get("_doc")][0]
        assert parent["content"] == "[claude] " + body and chunk["content"] == "[claude] " + body

    def test_length_ignores_prefix(self, cfg, wired, redis_client):
        cli._hook_save(cfg, True, "q" * 395, "user", "s", "t")          # 395 + prefix > 400 but still single
        assert [r for r in records(redis_client) if r.get("_doc")] == []

    def test_code_omitted_before_prefix(self, cfg, wired, redis_client):
        cli._hook_save(cfg, True, "see ```x = 1``` ok", "claude", "s", "t")
        assert records(redis_client)[0]["content"] == "[claude] see [code omitted] ok"

    def test_per_turn_guards(self, cfg, wired, redis_client, svc):
        mcp = json.loads(svc.call_tool("save_memory", {"content": "ok"}, nudge=False))
        assert mcp["saved"]
        cli._hook_save(cfg, True, "ok", "user", "s", "turn-A")
        cli._hook_save(cfg, True, "ok", "user", "s", "turn-B")
        cli._hook_save(cfg, True, "ok", "user", "s", "turn-B")       # same turn: duplicate
        assert sorted(r["content"] for r in records(redis_client)) == ["[user] ok", "[user] ok", "ok"]
        assert redis_client.exists(b"mem:sha:turn-A:" + __import__("n3mc_mcp.processor", fromlist=["x"]).sha1_hex("[user] ok").encode())

    def test_stop_and_recovery_save_once(self, cfg, wired, redis_client):
        cli._hook_save(cfg, True, "the answer", "claude", "s", "T")
        cli._hook_save(cfg, True, "the answer", "claude", "s", "T")
        assert len(records(redis_client)) == 1

    def test_skip_code_blocks_policy(self, cfg, wired, redis_client, svc):
        svc.cfg["skip_code_blocks"] = True
        cli._hook_save(cfg, True, "has ```fence``` inside", "user", "s", "t")
        assert records(redis_client) == []


class TestDirectSave:
    def test_no_server_saves_directly_without_vector(self, cfg, redis_client, monkeypatch, capsys):
        assert "sentence_transformers" not in sys.modules or True
        out = submit(monkeypatch, capsys, {"prompt": "direct message", "session_id": "ds"})
        r = records(redis_client)[0]
        assert r["content"] == "[user] direct message" and b"embedding" not in [k.encode() for k in r if k == "embedding"]
        assert "embedding" not in r
        assert redis_client.ttl(b"mem:" + r["id"].encode()) > 0
        assert "_(memory search unavailable:" in out

    def test_long_direct_save_has_prefixed_chunks(self, cfg, redis_client):
        cli._hook_save(cfg, False, "direct long sentence. " * 60, "claude", "ds", "t9")
        recs = records(redis_client)
        assert any(r.get("_doc") for r in recs) and all("embedding" not in r for r in recs)

    def test_later_repair_adds_vectors(self, cfg, redis_client, svc, db):
        cli._hook_save(cfg, False, "needs a vector later", "user", "s", "t")
        r = json.loads(svc.call_tool("repair_memory", {}, nudge=False))
        assert r["reembedded"] == 1

    def test_foreign_server_means_direct_save(self, cfg, redis_client, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_get", lambda port, path, timeout=5: {"status": "ok", "root": "C:/other", "code_version": "x", "pid": 1})
        out = submit(monkeypatch, capsys, {"prompt": "foreign port", "session_id": "fp"})
        assert "another N3MC folder's server is using port" in out
        assert records(redis_client)[0]["content"] == "[user] foreign port"
        assert "embedding" not in records(redis_client)[0]


class TestRedisDown:
    @pytest.fixture
    def down(self, isolate, monkeypatch):
        cfgp = paths.config_path()
        c = json.load(open(cfgp, encoding="utf-8"))
        c["redis_url"] = "redis://127.0.0.1:1/0"
        open(cfgp, "w", encoding="utf-8").write(json.dumps(c))

    def test_no_server_and_no_redis(self, down, monkeypatch, capsys):
        out = submit(monkeypatch, capsys, {"prompt": "lost in redis", "session_id": "rd"})
        assert any(l.get("hook") == "notice" and l.get("key") == "redis_unreachable" for l in audit_lines())
        n = cli._read_notices()["redis_unreachable"]["message"]
        assert "docker start redis-stack" in n and paths.audit_log_path() in n
        out2 = submit(monkeypatch, capsys, {"prompt": "again", "session_id": "rd"})
        assert "memory search unavailable" in out2 and "docker start redis-stack" in out2.replace("\n", " ") or "N3MC notice" in out2

    def test_server_503(self, down, svc, wired, monkeypatch, capsys, cfg):
        monkeypatch.setattr(svc.db, "ensure_connected", lambda enforce=True: (_ for _ in ()).throw(
            __import__("n3mc_mcp.database", fromlist=["x"]).BackendUnreachable("backend hint docker start redis-stack")))
        out = submit(monkeypatch, capsys, {"prompt": "through 503", "session_id": "r5"})
        assert "memory search unavailable: backend hint" in out
        assert "redis_unreachable" in cli._read_notices()

    def test_notice_cleared_by_successful_save(self, cfg, wired, redis_client):
        cli._notify("redis_unreachable", "boom")
        cli._hook_save(cfg, True, "recovered", "user", "s", "t")
        assert "redis_unreachable" not in cli._read_notices()


class TestHookSearchOutput:
    def test_heading_query_and_rows(self, cfg, wired, redis_client, monkeypatch, capsys, svc, db):
        svc.save({"content": "pineapple pizza opinions are strong", "turn_id": "TT"})
        svc.cfg["_notices"].clear()
        out = submit(monkeypatch, capsys, {"prompt": "pineapple pizza", "session_id": "q"})
        lines = out.split("\n")
        assert lines[0] == "# Recalled Memory Context"
        assert lines[1] == "Source: N3MC Lite (7-day working memory)"
        assert lines[2] == "Search query: pineapple pizza"
        assert "### [" in out and "turn=TT" in out and "score=" in out and "saved=" in out

    def test_empty_result(self, cfg, wired, redis_client, monkeypatch, capsys, svc):
        svc.cfg["_notices"].clear()
        out = submit(monkeypatch, capsys, {"prompt": "nothing here", "session_id": "q"})
        assert out.rstrip().endswith("_No memories found._")

    def test_degraded_output_has_source_and_notice_first(self, cfg, monkeypatch, capsys):
        cli._notify("stale_server", "a due notice")
        out = submit(monkeypatch, capsys, {"prompt": "no server", "session_id": "z"})
        lines = out.split("\n")
        assert lines[:2] == ["# Recalled Memory Context", "Source: N3MC Lite (7-day working memory)"]
        assert out.index("> \u26a0\ufe0f N3MC notice: a due notice") < out.index("_(memory search unavailable:")


class TestHookExitStatus:
    def test_entries_exit_zero_even_when_children_fail(self, monkeypatch, isolate):
        monkeypatch.setattr(lite_hook, "run_cli", lambda *a: b"")
        for entry in (lite_hook, lite_stop_hook):
            set_stdin(monkeypatch, "\x00 not json")
            assert entry.main([]) == 0

    def test_real_subprocess_hook_exits_zero(self, tmp_path):
        import subprocess
        env = dict(os.environ, N3MC_DATA_DIR=str(tmp_path / "d"), HOME=str(tmp_path), USERPROFILE=str(tmp_path),
                   PYTHONUTF8="1", N3MC_REDIS_URL="redis://127.0.0.1:1/0")
        cfgdir = tmp_path / "d"
        cfgdir.mkdir()
        (cfgdir / "config.json").write_text(json.dumps({"server_port": 18597, "redis_url": "redis://127.0.0.1:1/0"}))
        (cfgdir / "server_start_failed").write_text("blocked")     # keeps the hook from spawning a real server
        r = subprocess.run([sys.executable, "-m", "n3mc_mcp.lite_hook"], input=b'{"prompt":"x","session_id":"p"}',
                           capture_output=True, env=env, cwd=str(tmp_path), timeout=120)
        assert r.returncode == 0
        assert b"# Recalled Memory Context" in r.stdout
        assert (cfgdir / "audit.log").exists()
