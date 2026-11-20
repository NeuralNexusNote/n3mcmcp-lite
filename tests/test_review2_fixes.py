"""Fixes from the second review of 2026-11-20 (Lite P1, P2)."""

import json

import pytest

from n3mc_mcp import cli, paths
from n3mc_mcp import server as srv

LONG = "ソラニアは空に浮かぶ架空の都市で、人口は三百万人、風力と太陽光で動いている。" * 20


class _Killed(BaseException):
    """Stands in for the process ending at the moment the save is sent."""


def _records(rconn):
    return [k for k in rconn.scan_iter(match="mem:*") if not k.startswith(b"mem:sha:")]


class TestSaveIsAtomic:
    """P1: the guard and the record(s) are written in one step, so an interrupted save leaves no
    guard behind and the retry is stored."""

    @pytest.mark.parametrize("text", ["a short reply to keep", LONG], ids=["short", "long"])
    def test_retry_after_interrupted_save_is_stored(self, db, rconn, monkeypatch, text):
        real = db._scripts["save"]
        calls = []

        def interrupted(keys, args):
            calls.append(1)
            if len(calls) == 1:
                raise _Killed()
            return real(keys=keys, args=args)

        monkeypatch.setitem(db._scripts, "save", interrupted)
        with pytest.raises(_Killed):
            db.save_memory(text, role="claude", turn_id="t-p1")
        assert not list(rconn.scan_iter(match="mem:sha:*")) and not list(rconn.scan_iter(match="docsha:*"))
        res = db.save_memory(text, role="claude", turn_id="t-p1")
        assert res["status"] == "saved"
        if "parent_id" in res:
            assert rconn.exists("doc:" + res["parent_id"])
            assert len(_records(rconn)) == res["chunks"]
        else:
            assert len(_records(rconn)) == 1
        assert db.save_memory(text, role="claude", turn_id="t-p1")["status"] == "duplicate"

    def test_guard_and_records_share_the_ttl(self, db, rconn, cfg):
        res = db.save_memory(LONG)
        ttl = int(cfg["ttl_seconds"])
        keys = [b"doc:" + res["parent_id"].encode()] + _records(rconn) + list(rconn.scan_iter(match="docsha:*"))
        assert len(keys) == res["chunks"] + 2
        assert all(0 < rconn.ttl(k) <= ttl for k in keys)

    def test_failed_write_releases_only_its_own_guard(self, db, rconn, monkeypatch):
        rconn.set("mem:sha:other", "someone-else")
        db._release_unwritten("mem:sha:other", "mine", "mem:none")
        assert rconn.get("mem:sha:other") == b"someone-else"
        rconn.set("mem:sha:mine", "mine")
        db._release_unwritten("mem:sha:mine", "mine", "mem:none")
        assert not rconn.exists("mem:sha:mine")


class TestMcpEntryRereadsConfig:
    """P2: an MCP entry that stays open follows a changed server_port."""

    def test_second_call_uses_the_new_port(self, monkeypatch, cfg):
        monkeypatch.setattr(srv, "_last_cfg", None)
        ports = []
        monkeypatch.setattr(cli, "ensure_server", lambda c, max_wait=60: ports.append(int(c["server_port"])) or True)
        monkeypatch.setattr(cli, "_post", lambda path, body, port, timeout=30: ports.append(port) or (200, {"text": "ok"}))
        assert srv.call_tool_sync(cfg, "search_memory", {"query": "x"}) == "ok"
        p = paths.config_path()
        d = json.loads(p.read_text(encoding="utf-8"))
        d["server_port"] = int(cfg["server_port"]) + 7
        p.write_text(json.dumps(d), encoding="utf-8")
        assert srv.call_tool_sync(cfg, "search_memory", {"query": "x"}) == "ok"
        new = int(cfg["server_port"]) + 7
        assert ports == [int(cfg["server_port"])] * 2 + [new] * 2

    def test_unreadable_config_keeps_the_last_settings(self, monkeypatch, cfg):
        monkeypatch.setattr(srv, "_last_cfg", None)
        first = srv.current_cfg(cfg)
        paths.config_path().write_text("{oops", encoding="utf-8")
        again = srv.current_cfg(cfg)
        assert again is first and again["owner_id"] == cfg["owner_id"]
