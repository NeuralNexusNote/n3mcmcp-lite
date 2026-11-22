"""Layer 4: regressions of v1.7.x - v1.12.x carried over (query cleaning, exact-only dedup, pre-filter, shaping,
config, atomic writes, weak matches ...). Fake embeddings only. *Redis* classes need N3MC_REDIS_TEST_URL."""

import json
import os
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
import redis

from n3mc_mcp import config, context, database, paths, processor
from n3mc_mcp.database import DOC, DOC_SHA, MEM, MEM_SHA, BackendUnreachable, Database
from n3mc_mcp.tools import Service

CFG = dict(config.DEFAULTS, owner_id="o", local_id="l")


def save(svc, content, **kw):
    return json.loads(svc.call_tool("save_memory", dict(content=content, **kw), nudge=False))


def search(svc, query, **kw):
    svc.cfg["_notices"].clear()
    return svc.call_tool("search_memory", dict(query=query, **kw), nudge=False)


def mk_row(i, cos, bm25=None, epoch=None, session="s", text=None, **kw):
    row = {"id": "id%02d" % i, "content": text or ("row %d " % i) + "x" * 150, "timestamp": "2026-11-22T00:00:00+00:00",
           "timestamp_epoch": str(epoch if epoch is not None else time.time()), "owner_id": "o",
           "session_id": session, "turn_id": "", "importance": "1.0", "access_count": "0", "parent_id": "",
           "cos": cos, "bm25": bm25}
    row.update(kw)
    return row


def shape(rows, query="q", cfg=None, session="s", limit=20, reprs=None):
    cfg = dict(CFG, **(cfg or {}))
    return processor.build_results(rows, query, cfg, session, limit, lambda ids: {},
                                   (lambda r: reprs.get(r["id"])) if reprs else (lambda r: None))


class TestQueryCleaning:
    def test_control_and_cjk_punct_removed(self):
        assert processor.prepare_query("a\x00b") == "a b"
        assert "？" not in processor.prepare_query("記憶装置？")

    def test_cjk_question_hits(self, svc, db):
        save(svc, "記憶装置の設計について")
        assert "記憶装置の設計" in search(svc, "「記憶装置」は？")

    def test_weird_queries_never_raise(self, svc, db):
        save(svc, "something plain")
        for q in ["\x00", "\x1b[0m", "()", "@@@", "{}[]", "~!#$%^&*", "'; DROP", "  "]:
            out = search(svc, q)
            assert not out.startswith("Error")


class TestExactOnlyDedup:
    def test_updated_fact_is_saved(self, svc, db):
        assert save(svc, "The project budget is 100 dollars.")["saved"]
        assert save(svc, "The project budget is 120 dollars.")["saved"]

    def test_remade_long_document_is_saved(self, svc, db):
        a = "設定資料。" + "値は100です。" * 120
        b = a.replace("100", "120", 3)
        assert save(svc, a)["saved"] and save(svc, b)["saved"]
        assert save(svc, a)["status"] == "duplicate"

    def test_both_versions_listed_newest_first(self, svc, db):
        svc.cfg["min_score"] = 0.0
        save(svc, "The project budget is 100 dollars.")
        time.sleep(1.1)
        save(svc, "The project budget is 120 dollars.")
        out = search(svc, "project budget")
        assert "100 dollars" in out and "120 dollars" in out


class TestTimeWindow:
    def test_prefilter_returns_rows_inside_window(self, svc, db):
        old_epoch = time.time() - 5 * 86400
        r = save(svc, "needle in the old window")
        db.r.hset(MEM + r["id"], "timestamp_epoch", repr(old_epoch))
        for i in range(30):
            save(svc, "needle haystack filler %d" % i)
        lo = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
        hi = (datetime.now() - timedelta(days=4)).strftime("%Y-%m-%d")
        out = search(svc, "needle", since=lo, until=hi, limit=2)
        assert "needle in the old window" in out
        assert "filler" not in out


class TestShaping:
    def test_vector_min_cos_drops_low_vectors(self):
        rows = [mk_row(1, 0.95), mk_row(2, 0.70)]
        main, weak = shape(rows, cfg={"min_results": 0})
        assert [r["id"] for r in main] == ["id01"]

    def test_relative_cut(self):
        rows = [mk_row(1, 0.99, bm25=5.0), mk_row(2, None, bm25=0.5)]
        main, _ = shape(rows, cfg={"min_results": 0, "min_score": 0.0})
        assert [r["id"] for r in main] == ["id01"]
        main, _ = shape(rows, cfg={"min_results": 0, "min_score": 0.0, "relative_min_score_ratio": 0.0})
        assert len(main) == 2

    def test_newest_version_moves_up_and_inherits_score(self):
        v = [1.0, 0.0]
        old = mk_row(1, 0.99, epoch=time.time() - 200, text="old version " + "x" * 120)
        new = mk_row(2, 0.90, epoch=time.time() - 100, text="new version " + "x" * 120)
        main, _ = shape([old, new], cfg={"min_results": 0}, reprs={"id01": (v, "a" * 120), "id02": (v, "a" * 120)})
        assert [r["id"] for r in main] == ["id02", "id01"]
        assert main[0]["score"] > main[1]["score"]
        assert main[0]["_version_group"] == main[1]["_version_group"] == 1

    def test_version_group_rows_survive_relative_cut(self):
        v = [1.0, 0.0]
        new = mk_row(1, 0.99, epoch=time.time() - 100)
        old = mk_row(2, 0.86, epoch=time.time() - 200)
        main, _ = shape([new, old], cfg={"min_results": 0, "min_score": 0.0},
                        reprs={"id01": (v, "a" * 120), "id02": (v, "a" * 120)})
        assert len(main) == 2

    def test_short_rows_are_not_grouped(self):
        v = [1.0, 0.0]
        rows = [mk_row(1, 0.99, text="short a"), mk_row(2, 0.98, text="short b")]
        main, _ = shape(rows, cfg={"min_results": 0}, reprs={"id01": (v, "short a"), "id02": (v, "short b")})
        assert all("_version_group" not in r for r in main)


class TestRender:
    def row(self, **kw):
        d = {"id": "abc", "content": "hello", "score": 0.5, "timestamp": "2026-11-13T01:34:00+00:00",
             "turn_id": "T1", "_is_parent": False}
        d.update(kw)
        return d

    def test_local_time_saved_and_turn(self):
        out = context.render_search([self.row()], [], [])
        assert "### [abc] turn=T1 score=0.500 saved=2026-11-13 " in out
        assert "(UTC" in out
        assert "_If several versions of the same content appear" in out

    def test_notice_once_then_later_notice(self, svc, db):
        save(svc, "notice target text")
        svc.cfg["_notices"].append("config_type: first")
        first = svc.call_tool("search_memory", {"query": "notice target"}, nudge=False)
        assert first.count("config_type: first") == 1
        again = svc.call_tool("search_memory", {"query": "notice target"}, nudge=False)
        assert "config_type: first" not in again
        svc.cfg["_notices"].append("shared_redis: later")
        assert "shared_redis: later" in svc.call_tool("search_memory", {"query": "notice target"}, nudge=False)

    def test_weak_rows_after_normal(self):
        out = context.render_search([self.row()], [self.row(id="weak1", _weak=True)], [])
        assert out.index("[abc]") < out.index("#### Possibly related") < out.index("[weak1]")

    def test_no_normal_rows_but_weak(self):
        out = context.render_search([], [self.row(id="weak1")], [])
        assert out.startswith("_No memories found._")
        assert "_If several versions" not in out and "[weak1]" in out

    def test_no_weak_no_heading(self):
        assert "Possibly related" not in context.render_search([self.row()], [], [])


class TestConfig:
    def test_unreadable_config_untouched(self, isolate):
        path = paths.config_path()
        open(path, "w", encoding="utf-8").write("{ not json")
        cfg = config.load_config()
        assert open(path, encoding="utf-8").read() == "{ not json"
        assert cfg["_unreadable"] and any(n.startswith("config_unreadable") for n in cfg["_notices"])
        assert cfg["owner_id"]

    def test_wrong_type_ignored_and_reported(self, isolate):
        path = paths.config_path()
        open(path, "w", encoding="utf-8").write(json.dumps({"vector_min_cos": "0.83", "min_results": 5}))
        cfg = config.load_config()
        assert cfg["vector_min_cos"] == 0.85 and cfg["min_results"] == 5
        assert any(n.startswith("config_type") and "vector_min_cos" in n for n in cfg["_notices"])
        assert json.load(open(path, encoding="utf-8"))["vector_min_cos"] == "0.83"

    def test_bool_is_not_a_number(self):
        assert not config.config_value_ok("ttl_seconds", True)
        assert not config.config_value_ok("min_score", True)

    def test_missing_fields_are_filled(self, isolate):
        cfg = config.load_config()
        saved = json.load(open(paths.config_path(), encoding="utf-8"))
        assert saved["server_port"] == cfg["server_port"] and saved["context_max_chars"] == 9500
        assert saved["owner_id"] == cfg["owner_id"]

    def test_env_overrides_redis_url(self, isolate, monkeypatch):
        monkeypatch.setenv("N3MC_REDIS_URL", "redis://example:1/2")
        assert config.load_config()["redis_url"] == "redis://example:1/2"

    def test_dedup_threshold_is_ignored(self, isolate):
        open(paths.config_path(), "w", encoding="utf-8").write(json.dumps({"dedup_threshold": 0.5}))
        cfg = config.load_config()
        assert cfg["_notices"] == []


class TestTestSafety:
    def test_live_url_is_refused_by_conftest_logic(self):
        import conftest
        assert conftest._same("redis://localhost:6379/0/", "redis://localhost:6379/0")
        assert not conftest._same("redis://localhost:6379/0", "redis://localhost:6390/0")

    def test_bom_config_is_read(self, tmp_path, monkeypatch):
        import conftest
        home = tmp_path / "h"
        (home / ".n3mc-workingmemory").mkdir(parents=True)
        (home / ".n3mc-workingmemory" / "config.json").write_bytes(
            b"\xef\xbb\xbf" + json.dumps({"redis_url": "redis://live:6379/0"}).encode())
        monkeypatch.setattr(conftest, "_REAL_HOME", str(home))
        assert "redis://live:6379/0" in conftest._live_urls()

    def test_marked_db_rule(self, redis_client):
        assert redis_client.exists("__n3mc_test_db__")


class TestFilterScore:
    def test_old_and_other_session_rows_survive(self):
        now = time.time()
        rows = [mk_row(1, 0.86, epoch=now, session="s"),
                mk_row(2, 0.95, epoch=now - 3 * 86400, session="s"),
                mk_row(3, 0.99, epoch=now - 86400, session="other"),
                mk_row(4, 0.95, epoch=now - 86400, session="other")]
        main, _ = shape(rows, cfg={"min_results": 0})
        assert {r["id"] for r in main} == {"id01", "id02", "id03", "id04"}
        assert [r["id"] for r in main] == [r["id"] for r in sorted(main, key=lambda r: -r["score"])]


class TestRedisTimeouts:
    def test_client_timeouts(self, db):
        kw = db.r.connection_pool.connection_kwargs
        assert kw["socket_timeout"] == 5 and kw["socket_connect_timeout"] == 2

    def test_unresponsive_server_fails_fast(self, isolate):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        conns = []
        stop = threading.Event()

        def accept():
            srv.settimeout(0.2)
            while not stop.is_set():
                try:
                    conns.append(srv.accept()[0])
                except OSError:
                    pass

        t = threading.Thread(target=accept, daemon=True)
        t.start()
        cfg = dict(config.load_config(), redis_url="redis://127.0.0.1:%d/0" % srv.getsockname()[1])
        d = Database(cfg)
        start = time.time()
        with pytest.raises(BackendUnreachable) as ei:
            d.ensure_connected()
        assert time.time() - start < 9
        assert "docker start redis-stack" in str(ei.value)
        stop.set()
        srv.close()

    def test_reconnect_closes_old_client(self, db):
        old = db.r
        closed = []
        orig = old.close
        old.close = lambda: (closed.append(1), orig())[1]
        db.mark_down()
        assert closed and db.r is None
        db.ensure_connected()
        assert db.r is not None


class TestConfigCreate:
    def test_concurrent_creation_gives_one_owner(self, tmp_path, monkeypatch):
        folder = tmp_path / "fresh"
        monkeypatch.setenv("N3MC_DATA_DIR", str(folder))
        out = []

        def work():
            out.append(config.load_config()["owner_id"])

        ts = [threading.Thread(target=work) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert len(set(out)) == 1
        assert json.load(open(folder / "config.json", encoding="utf-8"))["owner_id"] == out[0]

    def test_update_goes_through_os_replace(self, isolate, monkeypatch):
        calls = []
        real = os.replace
        monkeypatch.setattr(os, "replace", lambda a, b: (calls.append((a, b)), real(a, b))[1])
        open(paths.config_path(), "w", encoding="utf-8").write(json.dumps({"owner_id": "x", "local_id": "y"}))
        config.load_config()
        assert calls and calls[0][1] == paths.config_path()


class TestConfigRanges:
    @pytest.mark.parametrize("key,val", [("ttl_seconds", 604800.5), ("ttl_seconds", 0), ("embedding_dim", 768.0),
                                         ("chunk_threshold", 10), ("half_life_days", 0), ("min_score", 1.5),
                                         ("min_results", -1), ("min_results", 1.5)])
    def test_rejected(self, isolate, key, val):
        open(paths.config_path(), "w", encoding="utf-8").write(json.dumps({key: val}))
        cfg = config.load_config()
        assert cfg[key] == config.DEFAULTS[key]
        assert any(n.startswith("config_type") and key in n for n in cfg["_notices"])

    def test_zero_min_results_accepted(self):
        assert config.config_value_ok("min_results", 0)

    def test_overlap_not_below_threshold(self, isolate):
        open(paths.config_path(), "w", encoding="utf-8").write(json.dumps({"chunk_threshold": 100}))
        cfg = config.load_config()
        assert cfg["chunk_overlap"] == 25 and cfg["_notices"] == []
        open(paths.config_path(), "w", encoding="utf-8").write(json.dumps({"chunk_overlap": 400}))
        assert any("chunk_overlap" in n for n in config.load_config()["_notices"])

    def test_chunk_text_terminates(self):
        assert processor.chunk_text("z" * 1000, 100, 100)


class TestLongSaveAtomic:
    def test_failing_embedding_leaves_nothing(self, svc, db, monkeypatch):
        n = {"i": 0}

        def boom(text, cfg, query=False, no_wait=False):
            n["i"] += 1
            if n["i"] == 2:
                raise RuntimeError("embed failed")
            return [0.0] * 768

        monkeypatch.setattr(processor, "embed", boom)
        with pytest.raises(RuntimeError):
            db.save_memory("long text sentence. " * 80)
        assert db.r.keys("doc:*") == [] and db.r.keys("docsha:*") == [] and db.r.keys("mem:*") == []
        monkeypatch.setattr(processor, "embed", lambda t, c, query=False, no_wait=False: [0.1] * 768)
        assert db.save_memory("long text sentence. " * 80)["saved"]

    def test_single_script_writes_everything(self, db):
        res = db.save_memory("ts. " * 400)
        assert db.r.exists(DOC + res["parent_id"]) and len(res["ids"]) >= 2


class TestReview2Fixes:
    def test_script_failure_leaves_no_guard(self, db, monkeypatch):
        class Boom:
            def __call__(self, *a, **k):
                raise redis.ResponseError("boom")

        monkeypatch.setitem(db._scripts, "save", Boom())
        with pytest.raises(redis.ResponseError):
            db.save_memory("guard cleanup")
        assert db.r.keys("mem:sha:*") == []
        db._scripts["save"] = db.r.register_script(database.SAVE_WITH_GUARD)
        assert db.save_memory("guard cleanup")["status"] == "saved"
        assert db.save_memory("guard cleanup")["status"] == "duplicate"

    def test_only_own_guard_is_deleted(self, db, monkeypatch):
        guard = MEM_SHA + processor.sha1_hex("shared text")
        db.r.set(guard, "someone-elses-id", ex=100)

        class Boom:
            def __call__(self, *a, **k):
                raise redis.ResponseError("boom")

        monkeypatch.setitem(db._scripts, "save", Boom())
        monkeypatch.setattr(db.r, "exists", lambda k: 0 if k != guard else 1)
        # the guard exists, so save_memory answers duplicate before the script runs
        assert db.save_memory("shared text")["status"] == "duplicate"
        assert db.r.get(guard) == b"someone-elses-id"

    def test_thin_client_follows_server_port_change(self, isolate, monkeypatch):
        from n3mc_mcp import cli, server
        ports = []
        monkeypatch.setattr(cli, "ensure_server", lambda cfg, max_wait=60: True)
        monkeypatch.setattr(cli, "_post", lambda port, path, body, timeout=30: (ports.append(port), {"text": "ok"})[1])
        state = server.ThinState()
        server.handle_call("list_memories", {}, state=state)
        p = json.load(open(paths.config_path(), encoding="utf-8"))
        p["server_port"] = 18999
        open(paths.config_path(), "w", encoding="utf-8").write(json.dumps(p))
        server.handle_call("list_memories", {}, state=state)
        assert ports[-1] == 18999 and ports[0] != 18999

    def test_unreadable_config_keeps_last_readable(self, isolate):
        from n3mc_mcp import server
        state = server.ThinState()
        first = server.current_config(state)
        open(paths.config_path(), "w", encoding="utf-8").write("{ broken")
        second = server.current_config(state)
        assert second["owner_id"] == first["owner_id"]


class TestIndexFailure:
    def test_missing_redisearch(self, svc, db, monkeypatch):
        db.r.execute_command("FT.DROPINDEX", database.INDEX)
        db.index_error = "unknown command 'FT.CREATE'"      # the state after a failed startup FT.CREATE
        real = db.r.execute_command

        def fake(*a, **k):
            if a and a[0] == "FT.CREATE":
                raise redis.ResponseError("unknown command 'FT.CREATE'")
            return real(*a, **k)

        monkeypatch.setattr(db.r, "execute_command", fake)
        out = svc.call_tool("search_memory", {"query": "x"}, nudge=False)
        assert out.startswith("Error: memory backend unreachable.") and "does not provide RediSearch" in out
        monkeypatch.setattr(db.r, "execute_command", real)
        assert not svc.call_tool("search_memory", {"query": "x"}, nudge=False).startswith("Error")


class TestEnforceEphemeral:
    def test_empty_db_turns_persistence_off(self, cfg, redis_client):
        redis_client.flushdb()
        # A snapshot written while test_other_db_keys_keep_settings held its key may
        # bring that key back after the test Redis restarts (2026-11-22).
        other = redis.from_url(cfg["redis_url"].rsplit("/", 1)[0] + "/1", protocol=2)
        other.delete("k")
        other.close()
        redis_client.config_set("save", "3600 1")
        d = Database(cfg)
        d.ensure_connected()
        assert redis_client.config_get("save")["save"] == ""
        assert not any(n.startswith("shared_redis") for n in cfg["_notices"])
        d.close()

    def test_foreign_key_keeps_settings(self, cfg, redis_client):
        redis_client.config_set("save", "3600 1")
        redis_client.set("somebody_elses_key", "1")
        d = Database(cfg)
        d.ensure_connected()
        assert redis_client.config_get("save")["save"] == "3600 1"
        assert any(n.startswith("shared_redis") for n in cfg["_notices"])
        redis_client.config_set("save", "")
        d.close()

    def test_other_db_keys_keep_settings(self, cfg, redis_client):
        redis_client.flushdb()
        other = redis.from_url(cfg["redis_url"].rsplit("/", 1)[0] + "/1", protocol=2)
        other.set("k", "1", ex=60)       # never outlives the test, even in a snapshot
        d = None
        try:
            redis_client.config_set("save", "3600 1")
            d = Database(cfg)
            d.ensure_connected()
            assert redis_client.config_get("save")["save"] == "3600 1"
            assert any(n.startswith("shared_redis") for n in cfg["_notices"])
        finally:
            if d is not None:
                d.close()
            other.delete("k")
            other.close()
            redis_client.config_set("save", "")

    def test_refused_config_adds_notice_without_failing(self, cfg, redis_client, monkeypatch):
        redis_client.flushdb()
        d = Database(cfg)
        d._build_client()

        def refuse(*a, **k):
            raise redis.ResponseError("unknown command 'CONFIG'")

        monkeypatch.setattr(d.r, "config_set", refuse)
        d.enforce_ephemeral()
        assert any(n.startswith("config_refused") for n in cfg["_notices"])
        d.close()


class TestCodeOmittedOnSave:
    def test_short_and_long_bodies(self, svc, db):
        r = save(svc, "a ```code here``` b `inline` c")
        assert db.r.hget(MEM + r["id"], "content").decode() == "a [code omitted] b `inline` c"
        long = "para text. " * 60 + "```\nprint(1)\n```" + " tail text." * 10
        r2 = save(svc, long)
        assert "[code omitted]" in db.r.hget(DOC + r2["parent_id"], "content").decode()
        assert b"print(1)" not in db.r.hget(DOC + r2["parent_id"], "content")

    def test_unclosed_fence_and_duplicates(self, svc, db):
        r = save(svc, "x ```never closed\nsecret")
        assert db.r.hget(MEM + r["id"], "content").decode() == "x [code omitted]"
        save(svc, "same ```A``` end")
        assert save(svc, "same ```B``` end")["status"] == "duplicate"


class TestSurroundingWhitespace:
    def test_outer_strip_only(self, svc, db):
        r = save(svc, " \nAAA\n ")
        assert db.r.hget(MEM + r["id"], "content").decode() == "AAA"
        assert save(svc, "AAA")["status"] == "duplicate"
        p = save(svc, "\n" + ("line one.\nline two. " * 40) + "\n")
        content = db.r.hget(DOC + p["parent_id"], "content").decode()
        assert content.startswith("line one.\nline two.") and not content.endswith("\n")
        assert save(svc, "  \t ")["reason"] == "empty content"


class TestModelWait:
    def test_search_and_save_while_loading(self, cfg, redis_client, monkeypatch):
        monkeypatch.setattr(processor, "MODEL_WAIT_SECONDS", 0.3)
        monkeypatch.setattr(processor, "_model", None)
        monkeypatch.setattr(processor, "_last_error", None)
        svc = Service(cfg)
        started = threading.Event()
        release = threading.Event()

        def hold():
            with processor._model_lock:
                started.set()
                release.wait(10)

        t = threading.Thread(target=hold)
        t.start()
        started.wait(5)
        try:
            svc.db.ensure_connected()
            cfg["_notices"].clear()
            r = json.loads(svc.call_tool("save_memory", {"content": "saved while loading text"}, nudge=False))
            assert r["warning"] == "embedding_unavailable" and "still loading" in r["detail"]
            out = svc.call_tool("search_memory", {"query": "saved while loading"}, nudge=False)
            assert "still loading" in out and "saved while loading text" in out
        finally:
            release.set()
            t.join()
            svc.db.close()


class TestListPaging:
    def test_single_after_big_document(self, svc, db):
        save(svc, "the earlier single memory")
        time.sleep(0.05)
        save(svc, "chunk filler sentence. " * 2600)
        out = svc.call_tool("list_memories", {"limit": 20}, nudge=False)
        assert "the earlier single memory" in out and "[doc×" in out


class TestNoTtlRefresh:
    def test_search_changes_no_ttl(self, svc, db):
        # 2026-11-22: every record is deleted 7 days after it was saved; a search never resets a TTL.
        r = save(svc, "refresh guard record text")
        guard = MEM_SHA + processor.sha1_hex("refresh guard record text")
        text = "refresh doc guard. " * 60
        p = save(svc, text)
        dg = DOC_SHA + processor.sha1_hex(text.strip())
        keys = [MEM + r["id"], guard, DOC + p["parent_id"], dg] + [MEM + c for c in p["ids"]]
        for k in keys:
            db.r.expire(k, 50)
        assert "refresh guard record text" in search(svc, "refresh guard record text")
        assert "refresh doc guard" in search(svc, "refresh doc guard")
        for k in keys:
            assert 0 < db.r.ttl(k) <= 50, k


class TestOrBm25:
    def test_extra_words_still_hit(self, svc, db):
        save(svc, "the quick fox jumped over a fence")
        assert "quick fox" in search(svc, "quick fox elephant")
        assert "quick fox" in search(svc, "what did the quick fox do")
        save(svc, "記憶装置の仕様メモ")
        assert "記憶装置の仕様メモ" in search(svc, "記憶装置 大阪")

    def test_rerank_counts_token_before_comma(self):
        assert processor.lex_tokens("my note, here") >= {"note"}


class TestTimeBoundsStrict:
    @pytest.mark.parametrize("val", ["yesterday", "2026/11/13", "2026-13-01"])
    def test_unreadable_since(self, svc, db, val):
        assert search(svc, "x", since=val).startswith("Error: since must be an ISO 8601")
        assert search(svc, "x", until=val).startswith("Error: until must be an ISO 8601")

    def test_date_only_is_local_midnight(self):
        from n3mc_mcp.tools import parse_time
        assert parse_time("2026-11-13", False, "since") == datetime(2026, 11, 13).timestamp()
        assert parse_time("2026-11-13", True, "until") == datetime(2026, 11, 13, 23, 59, 59, 999999).timestamp()
        assert parse_time("2026-11-13T09:00:00+09:00", False, "since") == \
            datetime(2026, 11, 13, 0, 0, tzinfo=timezone.utc).timestamp()
        assert parse_time("2026-11-13 09:00:00Z", False, "since") == \
            datetime(2026, 11, 13, 9, 0, tzinfo=timezone.utc).timestamp()


class TestRepairReembed:
    def test_vectors_are_added_without_changing_ttl(self, svc, db):
        ids = [db.save_memory("novector entry %d" % i, embed=False)["id"] for i in range(3)]
        db.r.expire(MEM + ids[0], 1234)
        r = json.loads(svc.call_tool("repair_memory", {}, nudge=False))
        assert r["reembedded"] == 3 and r["remaining"] == 0
        assert db.r.hget(MEM + ids[0], "embedding") is not None
        assert 1000 < db.r.ttl(MEM + ids[0]) <= 1234

    def test_cap_of_200(self, svc, db):
        for i in range(205):
            db.save_memory("bulk novector %d" % i, embed=False)
        r = json.loads(svc.call_tool("repair_memory", {}, nudge=False))
        assert r["reembedded"] == 200 and r["remaining"] == 5


class TestDeleteOwner:
    def test_other_owner_untouched(self, svc, db):
        other = Database(dict(svc.cfg, owner_id="someone-else"))
        other.ensure_connected()
        rid = other.save_memory("another owner's note")["id"]
        assert json.loads(svc.call_tool("delete_memory", {"id": rid}, nudge=False))["status"] == "not_found"
        assert db.r.exists(MEM + rid)
        other.close()

    def test_chunks_deleted_from_chunk_ids(self, svc, db):
        r = save(svc, "owned doc text. " * 100)
        d = json.loads(svc.call_tool("delete_memory", {"id": r["parent_id"]}, nudge=False))
        assert d["chunks_deleted"] == len(r["ids"])


class TestGuardNx:
    def test_concurrent_identical_saves(self, cfg, redis_client, fake_embed):
        results = []

        def work():
            d = Database(cfg)
            d.ensure_connected()
            results.append(d.save_memory("raced identical content")["status"])
            d.close()

        ts = [threading.Thread(target=work) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert results.count("saved") == 1 and results.count("duplicate") == 5
        keys = [k for k in redis_client.keys("mem:*") if not k.startswith(b"mem:sha:")]
        assert len(keys) == 1


class TestAccessCountNoGhost:
    def test_deleted_between_search_and_refresh(self, svc, db):
        r = save(svc, "ghost candidate memory")
        rows, _, _ = db.search("ghost candidate memory", "s", 5)
        db.delete_memory(r["id"])
        db._count_access(rows)
        assert db.r.keys("mem:*") == []


class TestRecallOrphanChunks:
    def test_parent_once_then_chunks_after_parent_loss(self, svc, db):
        r = save(svc, "orphan chunk text. " * 60, turn_id="turn-o")
        out = svc.call_tool("recall_thread", {"turn_id": "turn-o"}, nudge=False)
        assert out.count("[doc×") == 1
        db.r.delete(DOC + r["parent_id"])
        out2 = svc.call_tool("recall_thread", {"turn_id": "turn-o"}, nudge=False)
        assert not out2.startswith("{") and out2.count("**[") >= len(r["ids"])


class TestDeleteIdPrefix:
    def test_prefixes(self, svc, db):
        p = save(svc, "prefix doc text. " * 50)
        assert json.loads(svc.call_tool("delete_memory", {"id": "doc:" + p["parent_id"]}, nudge=False))["id"] == p["parent_id"]
        s = save(svc, "prefix single")
        d = json.loads(svc.call_tool("delete_memory", {"id": "mem:" + s["id"]}, nudge=False))
        assert d["status"] == "deleted" and d["id"] == s["id"]
        assert json.loads(svc.call_tool("delete_memory", {"id": "doc:doc:x"}, nudge=False))["status"] == "not_found"


class TestNudgeOnErrors:
    def test_errors_end_with_reminder(self, svc, db, monkeypatch):
        from n3mc_mcp.tools import _NUDGE_GENERIC
        a = svc.call_tool("save_memory", {"content": "x", "importance": "nan"}, nudge=True)
        assert a.startswith('{"status": "error"') and a.endswith(_NUDGE_GENERIC)
        b = svc.call_tool("search_memory", {"query": "x", "since": "yesterday"}, nudge=True)
        assert b.startswith("Error: since") and b.endswith(_NUDGE_GENERIC)
        monkeypatch.setattr(svc.db, "list_entries", lambda n: 1 / 0)
        c = svc.call_tool("list_memories", {}, nudge=True)
        assert c.startswith("Error: ") and c.endswith(_NUDGE_GENERIC)
        monkeypatch.setattr(svc.db, "ensure_connected", lambda enforce=True: (_ for _ in ()).throw(BackendUnreachable("hint")))
        d = svc.call_tool("search_memory", {"query": "x"}, nudge=True)
        assert d.startswith("Error: memory backend unreachable. hint") and d.endswith(_NUDGE_GENERIC)


class TestConditionalWrites:
    def test_scripts_do_not_create_keys(self, db):
        s = db._scripts
        assert s["incr"](keys=[MEM + "no1"], args=["access_count", 1]) is None
        assert s["hset"](keys=[MEM + "no2"], args=["embedding", b"x"]) == 0
        assert "guard" not in s
        assert not db.r.exists(MEM + "no1", MEM + "no2")

    def test_existing_record_keeps_ttl(self, db):
        r = db.save_memory("conditional write target")
        key = MEM + r["id"]
        db.r.expire(key, 777)
        db._scripts["incr"](keys=[key], args=["access_count", 2])
        db._scripts["hset"](keys=[key], args=["note", "x"])
        assert db.r.ttl(key) <= 777
        assert int(db.r.hget(key, "access_count")) == 2

    def test_all_keys_have_ttl_after_activity(self, svc, db):
        save(svc, "activity one")
        save(svc, "activity doc. " * 60)
        search(svc, "activity")
        svc.call_tool("repair_memory", {}, nudge=False)
        for k in db.r.scan_iter(count=100):
            if k != b"__n3mc_test_db__":
                assert db.r.ttl(k) > 0


class TestHangulFiller:
    def test_invisible_only_is_rejected(self, svc, db):
        for s in ["ㅤ", "ᅟᅠ", "ﾠ", "⠀"]:
            assert save(svc, s)["reason"] == "empty content (no visible characters)"
        assert save(svc, "ㅤA")["saved"]


class TestPluginHook:
    """The plugin SessionStart script rules (CLI permissions instead of MCP ones)."""

    def load(self):
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugins",
                            "n3mc-workingmemory", "hooks", "install_permissions.py")
        spec = importlib.util.spec_from_file_location("install_permissions_t", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_script_has_cli_rules_and_no_delete(self):
        text = open(self.load().__file__, encoding="utf-8").read()
        if "n3mc-lite --search" not in text:
            pytest.skip("plugins/ still holds the v1.12.2 files (not regenerated: kept as instructed)")
        assert "n3mc-lite --search *" in text and "n3mc-lite --stop*" in text
        assert "--delete" not in text

    def test_rules_are_the_pinned_uvx_commands(self):
        import n3mc_mcp
        mod = self.load()
        assert mod.VERSION == n3mc_mcp.__version__
        assert [r.split("n3mc-lite ")[1] for r in mod.RULES] == [
            "--search *)", "--buffer *)", "--list*)", "--recall-thread *)", "--repair*)", "--stop*)"]
        assert all(r.startswith("Bash(uvx --from n3memorycore-mcp-lite==%s n3mc-lite " % mod.VERSION)
                   for r in mod.RULES)

    def test_double_registration_is_reported(self, tmp_path):
        mod = self.load()
        p = tmp_path / "settings.json"

        def write(event, command):
            p.write_text(json.dumps({"hooks": {event: [{"hooks": [{"type": "command", "command": command}]}]}}),
                         encoding="utf-8")
        write("Stop", "C:/Py/Scripts/n3mc-lite-stop-hook.exe")
        assert "registered twice" in mod._double_registration(p)
        write("UserPromptSubmit", "C:/Py/python.exe -m n3mc_mcp.lite_hook")
        assert "registered twice" in mod._double_registration(p)
        write("UserPromptSubmit", "python C:/x/n3mc_hook.py")          # another N3MC product
        assert mod._double_registration(p) == ""
        p.write_text("{ not json", encoding="utf-8")
        assert mod._double_registration(p) == ""


class TestWeakMatches:
    def weak_cfg(self, **kw):
        return dict({"min_results": 3}, **kw)

    def test_weak_rows_added_after_ordinary(self):
        rows = [mk_row(1, 0.95)] + [mk_row(i, 0.70 + i * 0.01) for i in range(2, 6)]
        main, weak = shape(rows)
        assert [r["id"] for r in main] == ["id01"]
        assert len(weak) == 2 and all(r["_weak"] for r in weak)
        assert [r["filter_score"] for r in weak] == sorted((r["filter_score"] for r in weak), reverse=True)

    def test_min_results_zero_and_enough_rows(self):
        rows = [mk_row(i, 0.9 + i * 0.001) for i in range(1, 5)]
        assert shape(rows, cfg={"min_results": 0})[1] == []
        assert shape(rows)[1] == []
        assert shape([mk_row(1, 0.9), mk_row(2, 0.6)], cfg={"min_results": 0})[1] == []

    def test_limit_caps_total(self):
        rows = [mk_row(1, 0.95)] + [mk_row(i, 0.70) for i in range(2, 6)]
        main, weak = shape(rows, limit=2)
        assert len(main) + len(weak) == 2

    def test_same_parent_collapses_and_present_parent_skipped(self):
        parents = {"P": {"content": "full doc " + "z" * 100, "chunk_count": 2, "chunk_ids": ["a", "b"], "turn_id": ""}}
        rows = [mk_row(1, 0.7, parent_id="P"), mk_row(2, 0.72, parent_id="P"), mk_row(3, 0.71)]
        cfg = dict(CFG, min_results=5)
        main, weak = processor.build_results(rows, "full doc", cfg, "s", 20, lambda ids: parents, lambda r: None)
        ids = [r["id"] for r in weak]
        assert ids.count("P") == 1 and len(ids) == 2
        rows2 = [mk_row(1, 0.99, parent_id="P"), mk_row(2, 0.72, parent_id="P")]
        main2, weak2 = processor.build_results(rows2, "full doc", cfg, "s", 20, lambda ids: parents, lambda r: None)
        assert [r["id"] for r in main2] == ["P"] and all(r["id"] != "P" for r in weak2)

    def test_floor_for_zero_relevance(self):
        main, weak = shape([mk_row(1, 0.0), mk_row(2, 0.0000001)], cfg={})
        assert weak == []

    def test_empty_query_adds_no_weak_rows(self):
        rows = [mk_row(1, 0.7), mk_row(2, 0.71)]
        assert shape(rows, query="   ")[1] == []

    def test_default_min_results_is_3(self):
        assert config.DEFAULTS["min_results"] == 3

    def test_weak_rows_are_not_counted(self, svc, db):
        svc.cfg["min_score"] = 0.9
        a = save(svc, "alpha beta gamma delta")
        b = save(svc, "alpha completely different words here")
        for r in (a, b):
            db.r.expire(MEM + r["id"], 300)
        db.r.hset(MEM + b["id"], "access_count", "0")
        out = search(svc, "alpha beta gamma delta")
        assert "alpha completely different" in out.split("#### Possibly related")[-1] or "Possibly related" not in out
        if "Possibly related" in out:
            assert db.r.ttl(MEM + b["id"]) <= 300
            assert int(db.r.hget(MEM + b["id"], "access_count")) == 0
        assert int(db.r.hget(MEM + a["id"], "access_count")) == 1
        assert 0 < db.r.ttl(MEM + a["id"]) <= 300


class TestGuardReleasedOnAnyFailure:
    def test_bad_field_types(self, svc, db):
        assert save(svc, "turn id int", turn_id=12)["saved"] is False
        assert db.r.keys("mem:sha:*") == []
        assert save(svc, "turn id int")["saved"] is True

    def test_long_body_and_lone_surrogates(self, svc, db):
        assert save(svc, "long. " * 100, turn_id=12)["saved"] is False
        assert db.r.keys("docsha:*") == []
        r = save(svc, "sur", session_id="s\ud800x", turn_id="t\udfff1", agent_name="a\ud801")
        assert r["saved"] is True
        assert db.r.hget(MEM + r["id"], "session_id") == b"sx"
        assert db.r.hget(MEM + r["id"], "turn_id") == b"t1"


class TestPluginHookAsciiStdout:
    def test_missing_uvx_stdout_is_ascii_json(self, tmp_path):
        import subprocess
        import sys
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugins",
                              "n3mc-workingmemory", "hooks", "install_permissions.py")
        env = dict(os.environ, PATH=str(tmp_path), HOME=str(tmp_path), USERPROFILE=str(tmp_path))
        r = subprocess.run([sys.executable, script], capture_output=True, env=env, cwd=str(tmp_path))
        out = r.stdout.decode("ascii")
        if out.strip():
            assert "systemMessage" in json.loads(out)


class TestPublicRepoHygiene:
    def test_marketplace_version_matches(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        mp = json.load(open(os.path.join(root, ".claude-plugin", "marketplace.json"), encoding="utf-8"))
        import n3mc_mcp
        if mp["metadata"]["version"] != n3mc_mcp.__version__ and mp["metadata"]["version"] == "1.12.2":
            pytest.skip("marketplace.json still holds 1.12.2 (not regenerated: kept as instructed)")
        assert mp["metadata"]["version"] == n3mc_mcp.__version__

    def test_plugin_files_agree_with_the_package_version(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        import n3mc_mcp
        plugin = os.path.join(root, "plugins", "n3mc-workingmemory")
        meta = json.load(open(os.path.join(plugin, ".claude-plugin", "plugin.json"), encoding="utf-8"))
        hooks = json.load(open(os.path.join(plugin, "hooks", "hooks.json"), encoding="utf-8"))["hooks"]
        pin = "n3memorycore-mcp-lite==%s" % n3mc_mcp.__version__
        assert meta["version"] == n3mc_mcp.__version__
        assert pin in hooks["UserPromptSubmit"][0]["hooks"][0]["command"]
        assert pin in hooks["Stop"][0]["hooks"][0]["command"]
        assert pin in hooks["SessionStart"][0]["hooks"][0]["command"]

    def test_example_desktop_config_is_valid_json(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cfg = json.load(open(os.path.join(root, "examples", "claude_desktop_config.json"), encoding="utf-8"))
        entry = cfg["mcpServers"]["n3mc-workingmemory"]
        assert entry["command"].endswith("n3mc-workingmemory.exe") and entry["env"]["N3MC_AGENT_NAME"] == "claude-desktop"
