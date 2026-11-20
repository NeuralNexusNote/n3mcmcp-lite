"""Fixes from the 2026-11-20 code review (M1-M3, L4-L7)."""

import time

import pytest

from n3mc_mcp import cli, lite_hook, paths
from n3mc_mcp import processor as P

# conftest replaces these with fakes; the M3 tests need the real ones (no model is ever loaded:
# the tests hold the model lock, so nothing builds it).
REAL_GET_MODEL = P.get_model
REAL_ENCODE = P._encode


class TestMovedServer:
    """M1: server_port changed while the server ran."""

    def _setup(self, monkeypatch, cfg, root):
        port = int(cfg["server_port"])
        old = port + 1
        paths.pid_path().write_text("4242", encoding="utf-8")
        monkeypatch.setattr(cli, "_pidfile_starting", lambda p: True)
        monkeypatch.setattr(cli, "_listen_ports", lambda pid: [old])
        monkeypatch.setattr(cli, "_health", lambda p: {"root": root, "pid": 4242} if p == old else None)
        monkeypatch.setattr(cli, "_wait_port_free", lambda p, seconds=15.0: True)
        monkeypatch.setattr(cli, "_wait_health", lambda *a, **k: None)
        sent, started = [], []
        monkeypatch.setattr(cli, "_request_shutdown", lambda p, r: sent.append(p) or (200, {}))
        monkeypatch.setattr(cli, "_start_server", lambda c, max_wait=60: started.append(1) or True)
        return old, sent, started

    def test_old_server_is_stopped_on_its_port_and_a_new_one_starts(self, monkeypatch, cfg):
        old, sent, started = self._setup(monkeypatch, cfg, str(paths.data_dir()))
        assert cli.ensure_server(cfg, max_wait=0) is True
        assert sent == [old] and started and not paths.pid_path().exists()

    def test_server_of_another_folder_is_left_alone(self, monkeypatch, cfg):
        _, sent, started = self._setup(monkeypatch, cfg, "C:/somewhere/else")
        assert cli.ensure_server(cfg, max_wait=0) is False
        assert sent == [] and not started and paths.pid_path().exists()


class TestServerRecordsItsOwnStartFailure:
    """M2: the hooks never see the child exit, so the server writes the marker and the notice."""

    def test_bind_failure_writes_marker_and_notice(self, monkeypatch):
        def boom():
            raise SystemExit(1)

        monkeypatch.setattr(cli, "_run_server", boom)
        with pytest.raises(SystemExit):
            cli.run_server()
        assert paths.start_failed_path().exists()
        assert "server_start_failed" in paths.load_notices()

    def test_exception_writes_marker(self, monkeypatch):
        def boom():
            raise RuntimeError("no backend")

        monkeypatch.setattr(cli, "_run_server", boom)
        with pytest.raises(RuntimeError):
            cli.run_server()
        assert paths.start_failed_path().exists()

    def test_clean_exit_writes_nothing(self, monkeypatch):
        monkeypatch.setattr(cli, "_run_server", lambda: 0)
        assert cli.run_server() == 0
        assert not paths.start_failed_path().exists()

    def test_next_hook_does_not_start_again(self, monkeypatch, cfg):
        paths.start_failed_path().write_text(paths.utc_now_iso(), encoding="utf-8")
        started = []
        monkeypatch.setattr(cli, "_start_server", lambda c, max_wait=60: started.append(1) or True)
        assert cli.ensure_server(cfg, max_wait=0) is False
        assert not started and "could not start" in cli.last_reason()


@pytest.fixture
def loading_model(monkeypatch):
    """The real get_model with the model lock held, as while the server loads the model."""
    monkeypatch.setattr(P, "get_model", REAL_GET_MODEL)
    monkeypatch.setattr(P, "_encode", REAL_ENCODE)
    monkeypatch.setattr(P, "_model", None)
    monkeypatch.setattr(P, "MODEL_WAIT_SECONDS", 2)
    assert P._model_lock.acquire(timeout=5)
    yield
    P._model_lock.release()


class TestHooksDoNotWaitForTheModel:
    """M3: hook saves and searches go on at once while the model loads."""

    def test_no_model_wait_returns_at_once(self, loading_model, cfg):
        t0 = time.monotonic()
        with P.no_model_wait():
            vec, msg = P.embed_query("hello", cfg)
        assert vec is None and "still loading" in msg and time.monotonic() - t0 < 1.0

    def test_without_it_the_call_still_waits(self, loading_model, cfg):
        t0 = time.monotonic()
        vec, _ = P.embed_query("hello", cfg)
        assert vec is None and time.monotonic() - t0 >= 1.5

    def test_hook_save_and_search_through_the_server(self, loading_model, client, db):
        t0 = time.monotonic()
        r = client.post("/buffer", json={"content": "the hook saved this", "role": "user",
                                         "turn_id": "t-m3", "no_model_wait": True})
        assert r.status_code == 200 and r.json()["status"] == "saved"
        r = client.post("/search", json={"query": "hook saved", "max_chars": 9500, "no_model_wait": True})
        assert r.status_code == 200 and "the hook saved this" in r.json()["text"]
        assert time.monotonic() - t0 < 1.5
        keys = [k for k in db.client.scan_iter(match="mem:*") if not k.startswith(b"mem:sha:")]
        assert keys and not any(db.client.hexists(k, "embedding") for k in keys)

    def test_hook_requests_carry_the_flag_and_cli_search_does_not(self, monkeypatch, cfg):
        bodies = []
        monkeypatch.setattr(cli, "_post", lambda path, body, port, timeout=30: bodies.append((path, body))
                            or (200, {"status": "saved", "text": ""}))
        cli._hook_save(cfg, True, "hi", "user", "s", "t")
        cli._search_block(cfg, True, "hi", max_chars=9500, hook=True)
        cli._search_block(cfg, True, "hi", max_chars=9500)
        assert [b.get("no_model_wait") for _, b in bodies] == [True, True, None]


class TestLowFindings:
    def test_save_failed_is_cleared_by_a_later_save(self):
        """L4."""
        cli._notify("save_failed", "A record could not be saved (x).")
        assert "save_failed" in paths.load_notices()
        cli._after_save({"status": "saved"})
        assert "save_failed" not in paths.load_notices()

    def test_repair_does_not_run_twice_at_once(self, db):
        """L5."""
        from n3mc_mcp import database
        assert database._REPAIR_LOCK.acquire(blocking=False)
        try:
            assert db.repair()["message"] == "repair already running"
        finally:
            database._REPAIR_LOCK.release()
        assert db.repair()["message"] == "index ensured"

    def test_search_argument_is_sanitized(self, monkeypatch):
        """L6."""
        seen = []
        monkeypatch.setattr(cli, "ensure_server", lambda cfg, max_wait=60: False)
        monkeypatch.setattr(cli, "_search_block", lambda cfg, ok, q, **k: seen.append(q) or "")
        assert cli.main(["--search", "abc\ud800def"]) == 0
        assert seen and "\ud800" not in seen[0]

    def test_stop_hook_steps_share_the_timeout(self, monkeypatch):
        """L7: the steps together stay within CHILD_TIMEOUT (the hook is registered with 60 s)."""
        ticks = [0.0, 0.0, 40.0]  # start, before step 1, before step 2 (step 1 took 40 s)
        monkeypatch.setattr(lite_hook.time, "monotonic", lambda: ticks.pop(0) if len(ticks) > 1 else ticks[0])
        timeouts = []

        class R:
            stdout = b""
            returncode = 0

        monkeypatch.setattr(lite_hook.subprocess, "run", lambda *a, **k: timeouts.append(k["timeout"]) or R())
        monkeypatch.setattr(lite_hook.sys, "stdin", type("S", (), {"buffer": type("B", (), {"read": lambda s: b"{}"})()})())
        lite_hook.run("Stop", (["--save-claude-turn"], ["--stop"]))
        assert timeouts == [lite_hook.CHILD_TIMEOUT, lite_hook.CHILD_TIMEOUT - 40.0]
        assert 40.0 + timeouts[1] <= 60  # step 1's 40 s plus the most step 2 may take
