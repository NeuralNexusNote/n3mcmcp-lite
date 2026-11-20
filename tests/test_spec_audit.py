"""Clause checks from the spec comparison (Appendix B phase 2 ②, 2026-11-20)."""

import json

import redis

from n3mc_mcp import cli
from n3mc_mcp.database import Database
from n3mc_mcp.tools import parse_bounds


class TestKnnErrorSkipsOnlyThatPath:
    def test_ft_error_in_knn_keeps_bm25(self, db, runner, monkeypatch):
        db.save_memory("alpha beta gamma words")
        orig = db._ft

        def ft(query, *a, **k):
            if "KNN" in query:
                raise redis.ResponseError("Syntax error")
            return orig(query, *a, **k)
        monkeypatch.setattr(db, "_ft", ft)
        out = runner.call("search_memory", {"query": "alpha beta gamma"}, nudge=False)
        assert not out.startswith("Error:") and "alpha beta gamma words" in out

    def test_connection_error_in_knn_is_backend_error(self, db, runner, monkeypatch):
        def ft(query, *a, **k):
            raise redis.ConnectionError("gone")
        monkeypatch.setattr(db, "_ft", ft)
        out = runner.call("search_memory", {"query": "x"}, nudge=False)
        assert out.startswith("Error: memory backend unreachable.")


class TestListLimits:
    def test_limit_rules(self, db, runner):
        for i in range(25):
            db.save_memory(f"list item number {i}")
        count = lambda out: out.count("**[")
        assert count(runner.call("list_memories", {"limit": -5}, nudge=False)) == 1
        assert count(runner.call("list_memories", {"limit": 0}, nudge=False)) == 20
        assert count(runner.call("list_memories", {}, nudge=False)) == 20


class TestSaveArgs:
    def test_owner_and_importance(self, db, runner, cfg):
        ok = json.loads(runner.call("save_memory", {"content": "owner ok", "owner_id": cfg["owner_id"],
                                                    "importance": 9}, nudge=False))
        assert ok["saved"] and db.client.hget("mem:" + ok["id"], "importance") == b"2.0"
        bad = json.loads(runner.call("save_memory", {"content": "x", "owner_id": "other"}, nudge=False))
        assert bad == {"status": "error", "saved": False, "reason": "owner_id mismatch"}


class TestTimeForms:
    def test_space_and_z(self):
        assert parse_bounds("2026-11-13 09:00:00+09:00", None)[0] == parse_bounds("2026-11-13T00:00:00Z", None)[0]


class TestRecallDefaults:
    def test_before_after_default_two(self, db, runner):
        for i in range(7):
            db.save_memory(f"thread line {i}", turn_id="T" if i == 3 else f"o{i}")
        out = runner.call("recall_thread", {"turn_id": "T"}, nudge=False)
        assert out.count("\n\n---\n\n") == 4 and "▶ " in out


class TestDeleteSessionRequired:
    def test_empty(self, runner):
        assert json.loads(runner.call("delete_memories_by_session", {"session_id": " "}, nudge=False))["status"] \
            == "error"


class TestStopServerForeign:
    def test_never_stops_foreign(self, cfg, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_health", lambda port: {"root": "/elsewhere", "pid": 1})
        called = []
        monkeypatch.setattr(cli, "_request_shutdown", lambda p, r: called.append(1))
        cli.cmd_stop_server(cfg)
        assert not called and "No N3MC Lite server of this data folder is running." in capsys.readouterr().out
