"""Layer 4: regression tests (spec §9 Layer 4). The embedding is conftest's deterministic stand-in.
Classes marked (Redis) in the spec use the `rconn` / `db` / `runner` fixtures."""
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import redis

import conftest
from conftest import parse_json
from n3mc_mcp import __version__, cli, config, context, database, paths, processor, tools
from n3mc_mcp.config import DEFAULTS, load_config
from n3mc_mcp.database import DOC, DOCSHA, MEM, SHA, BackendError, Database, sha1_hex, shape_results

ROOT = Path(__file__).resolve().parent.parent
NOW = time.time()


def _cfg(**kw):
    c = dict(DEFAULTS)
    c.update(kw)
    return c


def cand(cid, cos=None, bm25=None, epoch=NOW, session="s", content=None, **kw):
    d = {"id": cid, "content": content if content is not None else f"content of {cid} " + "filler " * 10,
         "timestamp_epoch": epoch, "timestamp": datetime.fromtimestamp(epoch, timezone.utc).isoformat(),
         "session_id": session, "importance": 1.0, "access_count": 0, "cos": cos, "bm25": bm25,
         "_doc": False, "chunk_ids": [], "_hit_key": cid}
    d.update(kw)
    return d


def _all_keys(rconn):
    return [k.decode() for k in rconn.keys("*") if k != conftest.TEST_MARKER.encode()]


def _write_cfg(isolated, **kw):
    p = isolated["data"] / "config.json"
    obj = json.loads(p.read_text(encoding="utf-8"))
    obj.update(kw)
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


LONG = "\n".join(f"第{i}節: 設定項目 key{i} の値は {i * 3} とし、毎週の点検で確認する。" for i in range(1, 30))


class TestQueryCleaning:
    def test_control_and_cjk_punctuation_removed(self):
        assert processor.prepare_query("「記憶装置」は？\x00") == "記憶 憶装 装置 は"
        assert processor.drop_lone_cjk_tokens(processor.prepare_query("「記憶装置」は？")) == "記憶 憶装 装置"

    def test_cjk_punctuation_query_hits(self, db):
        r = db.save_memory("記憶装置の容量を増やす計画")
        assert r["id"] in [h["id"] for h in db._bm25_search("「記憶装置」は？", 10)]

    def test_symbol_queries_do_not_raise(self, db):
        db.save_memory("something to find")
        for q in ["@#$%^&*()", "\x00\x01\x1f", "_", "a-b|c", "~!{}[]", "\\\\", "''\"\"", "😀", "-", "*"]:
            db._bm25_search(q, 10)
            db.search(q)


class TestExactOnlyDedup:
    def test_updated_fact_saved(self, db):
        assert db.save_memory("The project budget is 100 dollars.")["saved"]
        assert db.save_memory("The project budget is 120 dollars.")["saved"]

    def test_remade_document_saved(self, db):
        assert db.save_memory(LONG)["saved"]
        assert db.save_memory(LONG.replace("の値は 3", "の値は 4").replace("の値は 9", "の値は 10"))["saved"]

    def test_exact_duplicate_rejected(self, db):
        db.save_memory("exactly the same")
        assert db.save_memory("exactly the same")["status"] == "duplicate"


class TestTimeWindow:
    def test_window_rows_found_despite_closer_newer_rows(self, db, rconn):
        target = db.save_memory("weekly report about the harbour lights, older entry")
        old = NOW - 3 * 86400
        rconn.hset(MEM + target["id"], "timestamp_epoch", repr(old))
        for i in range(30):
            db.save_memory(f"weekly report about the harbour lights, newer entry {i}")
        normal, weak, _m = db.search("weekly report about the harbour lights", limit=2, until=NOW - 2 * 86400)
        assert target["id"] in [r["id"] for r in normal + weak]
        normal, _w, _m = db.search("weekly report harbour lights", limit=2, since=NOW - 3600)
        assert target["id"] not in [r["id"] for r in normal]


class TestShaping:
    def test_vector_floor(self):
        normal, _ = shape_results([cand("a", cos=0.99), cand("b", cos=0.80)], "q", _cfg(min_results=0), "s", 10)
        assert [r["id"] for r in normal] == ["a"]

    def test_relative_cut(self):
        cfg = _cfg(min_results=0, lexical_rerank_enabled=False, min_score=0.0)
        normal, _ = shape_results([cand("a", cos=1.0, bm25=5.0), cand("b", cos=0.851)], "q", cfg, "s", 10)
        assert [r["id"] for r in normal] == ["a"]

    def _versions(self, cfg):
        same = [1.0] + [0.0] * 767
        text = "設定資料 " * 30
        old = cand("old", cos=0.99, epoch=NOW - 3600, content=text, access_count=0)
        new = cand("new", cos=0.90, epoch=NOW, content=text + " v2")
        reps = {"old": (same, text), "new": (same, text)}
        return shape_results([old, new], "q", cfg, "s", 10, lambda rows: reps)

    def test_newest_version_first_takes_top_score(self):
        cfg = _cfg(min_results=0, lexical_rerank_enabled=False)
        normal, _ = self._versions(cfg)
        assert [r["id"] for r in normal] == ["new", "old"]
        assert normal[0]["score"] > normal[1]["score"]
        assert all(r.get("_version_group") == 1 for r in normal)

    def test_version_group_survives_relative_cut(self):
        cfg = _cfg(min_results=0, lexical_rerank_enabled=False, relative_min_score_ratio=0.99)
        normal, _ = self._versions(cfg)
        assert len(normal) == 2

    def test_short_rows_not_grouped(self):
        same = [1.0] + [0.0] * 767
        a, b = cand("a", cos=0.99, content="short"), cand("b", cos=0.98, content="short2")
        cfg = _cfg(min_results=0, lexical_rerank_enabled=False)
        normal, _ = shape_results([a, b], "q", cfg, "s", 10, lambda rows: {"a": (same, "x"), "b": (same, "y")})
        assert not any(r.get("_version_group") for r in normal)


class TestRender:
    def test_saved_local_time(self, monkeypatch):
        monkeypatch.setattr(processor, "_local_tz", lambda: timezone(timedelta(hours=9)))
        assert processor.fmt_saved("2026-11-13T01:34:00+00:00") == "2026-11-13 10:34 (UTC+0900)"

    def test_version_line_once(self):
        out = context.render_search([cand("a", score=0.5), cand("b", score=0.4)], [])
        assert out.count("_If several versions") == 1 and out.index("_If several") < out.index("### ")

    def test_notices_once_and_late_notice_next_search(self, cfg):
        n = tools.Notices(cfg)
        cfg["_notices"] = ["config_type: x"]
        assert n.take() == ["config_type: x"]
        assert n.take() == []
        cfg["_notices"].append("shared_redis: y")
        assert n.take() == ["shared_redis: y"]

    def test_weak_after_normal(self):
        out = context.render_search([cand("a", score=0.5)], [cand("w", score=0.1)])
        assert out.index("[a]") < out.index(context.WEAK_HEADING) < out.index("[w]")

    def test_only_weak(self):
        out = context.render_search([], [cand("w", score=0.1)])
        assert out.startswith(context.NO_MEMORIES) and context.WEAK_HEADING in out and "_If several" not in out

    def test_no_weak_no_heading(self):
        assert context.WEAK_HEADING not in context.render_search([cand("a", score=0.5)], [])
        assert context.render_search([], []) == context.NO_MEMORIES


class TestConfig:
    def test_unreadable_file_unchanged(self, isolated):
        p = isolated["data"] / "config.json"
        p.write_text("{not json", encoding="utf-8")
        c = load_config(quiet=True)
        assert p.read_text(encoding="utf-8") == "{not json" and c["_unreadable"]
        assert any(n.startswith("config_unreadable:") for n in c["_notices"])

    def test_wrong_type_ignored_and_reported(self, isolated):
        _write_cfg(isolated, vector_min_cos="0.83", min_score=0.3)
        c = load_config(quiet=True)
        assert c["vector_min_cos"] == 0.85 and c["min_score"] == 0.3
        assert any("vector_min_cos" in n for n in c["_notices"] if n.startswith("config_type:"))

    def test_bool_is_not_a_number(self, isolated):
        _write_cfg(isolated, min_score=True, search_result_limit=False)
        c = load_config(quiet=True)
        assert c["min_score"] == 0.2 and c["search_result_limit"] == 20


class TestTestSafety:
    def test_same_url_refused(self):
        assert conftest.refusal_reason("redis://localhost:6379/0", ["redis://127.0.0.1:6379"])
        assert conftest.refusal_reason("redis://localhost:6390/0", ["redis://localhost:6379/0"]) is None

    def test_live_config_with_bom(self, tmp_path):
        d = tmp_path / ".n3mc-workingmemory"
        d.mkdir()
        (d / "config.json").write_bytes(b"\xef\xbb\xbf" + json.dumps({"redis_url": "redis://h:1/0"}).encode())
        assert conftest.live_config_redis_url(tmp_path) == "redis://h:1/0"

    def test_unmarked_db_not_flushed(self):
        class Fake:
            flushed = False

            def dbsize(self):
                return 5

            def exists(self, k):
                return 0

            def flushdb(self):
                Fake.flushed = True

        with pytest.raises(pytest.skip.Exception, match="not marked as a test DB"):
            conftest.flush_test_db(Fake())
        assert not Fake.flushed


class TestFilterScore:
    def test_old_and_other_session_survive(self):
        cfg = _cfg(min_results=0, lexical_rerank_enabled=False)
        rows = [cand("new", cos=0.86, session="cur"),
                cand("old", cos=0.95, epoch=NOW - 3 * 86400, session="cur"),
                cand("oth1", cos=0.99, epoch=NOW - 86400, session="other"),
                cand("oth2", cos=0.95, epoch=NOW - 86400, session="other")]
        normal, _ = shape_results(rows, "q", cfg, "cur", 10)
        assert {r["id"] for r in normal} == {"new", "old", "oth1", "oth2"}
        assert [r["id"] for r in normal][0] == "new"
        assert [r["score"] for r in normal] == sorted([r["score"] for r in normal], reverse=True)


class TestRedisTimeouts:
    def test_client_timeouts(self, db):
        kw = db.client.connection_pool.connection_kwargs
        assert kw["socket_timeout"] == 5 and kw["socket_connect_timeout"] == 2

    def test_silent_server_fails_fast(self, cfg):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        port = srv.getsockname()[1]
        try:
            d = Database(dict(cfg, redis_url=f"redis://127.0.0.1:{port}/0"))
            t0 = time.monotonic()
            with pytest.raises(BackendError):
                d.ensure_connected()
            assert time.monotonic() - t0 < 8
        finally:
            srv.close()

    def test_reconnect_closes_old_client(self, db):
        closed = []

        class Dead:
            def ping(self):
                raise redis.ConnectionError("gone")

            def close(self):
                closed.append(1)

        db.client = Dead()
        assert db.ensure_connected() == "ok"
        assert closed == [1] and not isinstance(db.client, Dead)


CREATE_SNIPPET = ("import json,sys; from n3mc_mcp.config import load_config; c=load_config(quiet=True); "
                  "print(json.dumps([c['owner_id'], c['local_id']]))")


def _run_many(n, env):
    procs = [subprocess.Popen([sys.executable, "-c", CREATE_SNIPPET], env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for _ in range(n)]
    return [json.loads(p.communicate(timeout=60)[0]) for p in procs]


class TestConfigCreate:
    def test_concurrent_create_one_owner(self, isolated):
        (isolated["data"] / "config.json").unlink()
        ids = _run_many(8, dict(os.environ))
        disk = json.loads((isolated["data"] / "config.json").read_text(encoding="utf-8"))
        assert {tuple(i) for i in ids} == {(disk["owner_id"], disk["local_id"])}

    def test_concurrent_fill_of_existing_file(self, isolated):
        p = isolated["data"] / "config.json"
        p.write_text(json.dumps({"vector_min_cos": 0.83}), encoding="utf-8")
        ids = _run_many(2, dict(os.environ))
        disk = json.loads(p.read_text(encoding="utf-8"))
        assert {tuple(i) for i in ids} == {(disk["owner_id"], disk["local_id"])}
        assert disk["vector_min_cos"] == 0.83

    def test_update_uses_replace(self, isolated, monkeypatch):
        _write_cfg(isolated)
        p = isolated["data"] / "config.json"
        p.write_text(json.dumps({"owner_id": "o", "local_id": "l"}), encoding="utf-8")
        calls = []
        real = os.replace
        monkeypatch.setattr(config.os, "replace", lambda a, b: (calls.append(str(b)), real(a, b)))
        load_config(quiet=True)
        assert calls and calls[-1] == str(p)

    def test_migration_is_exclusive(self, isolated, monkeypatch, tmp_path):
        monkeypatch.delenv("N3MC_DATA_DIR")
        legacy = tmp_path / "legacy" / "config.json"
        legacy.parent.mkdir()
        legacy.write_text(json.dumps({"owner_id": "legacy-owner", "local_id": "legacy-local"}), encoding="utf-8")
        monkeypatch.setattr(paths, "legacy_config_path", lambda: legacy)
        links = []
        real = os.link
        monkeypatch.setattr(config.os, "link", lambda a, b: (links.append(str(b)), real(a, b)))
        c = load_config(quiet=True)
        assert c["owner_id"] == "legacy-owner" and legacy.exists()
        assert links and links[0].endswith("config.json")
        assert paths.same_folder(c["_path"], isolated["home"] / ".n3mc-workingmemory" / "config.json")


class TestConfigRanges:
    @pytest.mark.parametrize("key,value", [("ttl_seconds", 604800.5), ("ttl_seconds", 0), ("embedding_dim", 768.0),
                                           ("chunk_overlap", 400), ("chunk_threshold", 10),
                                           ("half_life_days", 0), ("min_score", 1.5), ("min_results", -1),
                                           ("min_results", 1.5)])
    def test_rejected(self, isolated, key, value):
        _write_cfg(isolated, **{key: value})
        c = load_config(quiet=True)
        assert c[key] == DEFAULTS[key]
        assert any(n.startswith("config_type:") and key in n for n in c["_notices"])

    def test_min_results_zero_accepted(self, isolated):
        _write_cfg(isolated, min_results=0)
        assert load_config(quiet=True)["min_results"] == 0

    def test_low_threshold_adjusts_overlap(self, isolated):
        _write_cfg(isolated, chunk_threshold=80)
        c = load_config(quiet=True)
        assert c["chunk_threshold"] == 80 and c["chunk_overlap"] == 20

    def test_chunk_text_terminates(self):
        assert processor.chunk_text("z" * 5000, 100, 100)


class TestLongSaveAtomic:
    def test_embedding_failure_leaves_nothing(self, db, rconn, monkeypatch):
        real = processor.embed
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("embedding failed")
            return real(*a, **k)

        monkeypatch.setattr(processor, "embed", flaky)
        with pytest.raises(RuntimeError):
            db.save_memory(LONG)
        assert not _all_keys(rconn)
        monkeypatch.setattr(processor, "embed", real)
        assert db.save_memory(LONG)["saved"]

    def test_one_script_writes_everything(self, db, monkeypatch):
        seen = []
        real = db._scripts["save"]
        db._scripts["save"] = lambda keys, args: (seen.append(keys), real(keys=keys, args=args))[1]
        r = db.save_memory(LONG)
        assert len(seen) == 1 and seen[0][0].startswith(DOCSHA) and len(seen[0]) == 2 + r["chunks"]


class TestGuardScriptAndPortReload:
    @pytest.mark.parametrize("text", ["short text for the guard", LONG])
    def test_interrupted_send(self, db, rconn, text):
        real = db._scripts["save"]

        def boom(keys, args):
            raise RuntimeError("interrupted")

        db._scripts["save"] = boom
        assert db.save_memory(text)["status"] == "error"
        assert not _all_keys(rconn)
        db._scripts["save"] = real
        assert db.save_memory(text)["saved"]
        assert all(rconn.ttl(k) > 0 for k in _all_keys(rconn))
        assert db.save_memory(text)["status"] == "duplicate"

    def test_only_own_guard_released(self, db, rconn):
        real = db._scripts["save"]

        def half(keys, args):
            rconn.set(keys[0], args[0], ex=100)
            raise RuntimeError("failed after the guard")

        db._scripts["save"] = half
        assert db.save_memory("guard owner check")["status"] == "error"
        assert not rconn.exists(SHA + sha1_hex("guard owner check"))
        rconn.set("mem:sha:other", "someone-else", ex=100)
        db._scripts["release"](keys=["mem:sha:other", "mem:none"], args=["mine"])
        assert rconn.exists("mem:sha:other")
        db._scripts["save"] = real

    def test_thin_client_follows_port_change(self, isolated, monkeypatch):
        from n3mc_mcp import server
        monkeypatch.setattr(server, "STATE", server._State())
        ports = []
        monkeypatch.setattr(cli, "ensure_server", lambda cfg, max_wait=60: True)
        monkeypatch.setattr(cli, "_post", lambda p, path, body, timeout=30: (ports.append(p), (200, {"text": "x"}))[1])
        server.handle_call("list_memories", {})
        _write_cfg(isolated, server_port=conftest.SESSION_PORT + 1)
        server.handle_call("list_memories", {})
        assert ports == [conftest.SESSION_PORT, conftest.SESSION_PORT + 1]

    def test_unreadable_config_keeps_last(self, isolated, monkeypatch):
        from n3mc_mcp import server
        monkeypatch.setattr(server, "STATE", server._State())
        first = server.current_config()
        (isolated["data"] / "config.json").write_text("{broken", encoding="utf-8")
        assert server.current_config()["owner_id"] == first["owner_id"]


class TestIndexFailure:
    def test_no_redisearch(self, rconn, cfg, monkeypatch):
        calls = {"n": 0}

        def fail(self):
            calls["n"] += 1
            raise redis.ResponseError("unknown command 'FT.INFO'")

        monkeypatch.setattr(Database, "_ft_info", fail)
        d = Database(cfg)
        with pytest.raises(BackendError, match="does not provide RediSearch"):
            d.ensure_connected()
        out = tools.ToolRunner(cfg, d).call("search_memory", {"query": "x"}, nudge=False)
        assert out.startswith("Error: memory backend unreachable. The Redis at") and "Redis Stack" in out
        assert calls["n"] >= 2
        d.close()

    def test_dim_mismatch_notice(self, rconn, cfg):
        rconn.execute_command("FT.CREATE", "n3mc_idx", "ON", "HASH", "PREFIX", 1, "mem:", "SCHEMA",
                              "content_fts", "TEXT", "embedding", "VECTOR", "FLAT", 6, "TYPE", "FLOAT32",
                              "DIM", 384, "DISTANCE_METRIC", "COSINE")
        d = Database(cfg)
        d.ensure_connected()
        assert any(n.startswith("index_dim_mismatch:") for n in cfg["_notices"])
        d.close()


class TestIndexFtsField:
    def test_prefix_and_code_not_indexed(self, db, rconn):
        r = db.save_memory("hello ```x``` world", role="user", turn_id="t1")
        h = rconn.hgetall(MEM + r["id"])
        assert h[b"content"].startswith(b"[user] ") and b"[user]" not in h[b"content_fts"]
        assert b"[code omitted]" not in h[b"content_fts"] and b"omitted" not in h[b"content_ngram"]
        m = db.save_memory("mcp ```code``` text")
        assert b"[code omitted]" not in rconn.hget(MEM + m["id"], "content_fts")

    def test_chunk_prefix_not_indexed(self, db, rconn):
        r = db.save_memory("word " * 200, role="claude", turn_id="t2")
        h = rconn.hgetall(MEM + r["ids"][0])
        assert h[b"content"].startswith(b"[claude 1/") and b"claude" not in h[b"content_fts"]

    def test_prefix_words_do_not_hit(self, db):
        db.save_memory("nothing special", role="claude", turn_id="t3")
        db.save_memory("more ```c``` stuff", role="user", turn_id="t3")
        assert db._bm25_search("claude", 10) == [] and db._bm25_search("omitted", 10) == []

    def test_old_index_rebuilt_once(self, rconn, cfg, monkeypatch):
        rconn.execute_command("FT.CREATE", "n3mc_idx", "ON", "HASH", "PREFIX", 1, "mem:", "SCHEMA",
                              "content", "TEXT", "embedding", "VECTOR", "FLAT", 6, "TYPE", "FLOAT32",
                              "DIM", 768, "DISTANCE_METRIC", "COSINE")
        rconn.hset("mem:keep", mapping={"content": "kept"})
        drops = []
        real = redis.Redis.execute_command

        def spy(self, *args, **kw):
            if args and args[0] == "FT.DROPINDEX":
                drops.append(args)
            return real(self, *args, **kw)

        monkeypatch.setattr(redis.Redis, "execute_command", spy)
        d = Database(cfg)
        d.ensure_connected()
        d.ensure_index()
        assert len(drops) == 1 and drops[0] == ("FT.DROPINDEX", "n3mc_idx") and rconn.exists("mem:keep")
        d.close()


class TestEnforceEphemeral:
    def _db(self, cfg, rconn):
        d = Database(cfg)
        d.client = conftest._connect(cfg["redis_url"])
        sets = []
        real = d.client.config_set
        d.client.config_set = lambda k, v: (sets.append((k, v)), real(k, v))
        return d, sets

    def test_empty_db_is_configured(self, cfg, rconn):
        rconn.delete(conftest.TEST_MARKER)
        r1 = conftest._connect(cfg["redis_url"].replace("/0", "/1"))
        r1.delete("n3mc:test:otherdb")
        d, sets = self._db(cfg, rconn)
        d.enforce_ephemeral()
        assert ("appendonly", "no") in sets and ("save", "") in sets
        rconn.set("mem:x", "1", ex=60)
        sets.clear()
        d.enforce_ephemeral()
        assert sets
        r1.close()
        rconn.set(conftest.TEST_MARKER, 1)

    def test_foreign_key_not_configured(self, cfg, rconn):
        d, sets = self._db(cfg, rconn)
        d.enforce_ephemeral()            # the test marker is not an N3MC key
        assert not sets and any(n.startswith("shared_redis:") for n in cfg["_notices"])

    def test_key_in_other_db(self, cfg, rconn):
        rconn.delete(conftest.TEST_MARKER)
        r1 = conftest._connect(cfg["redis_url"].replace("/0", "/1"))
        try:
            r1.set("n3mc:test:otherdb", "1", ex=60)
            d, sets = self._db(cfg, rconn)
            d.enforce_ephemeral()
            assert not sets and any(n.startswith("shared_redis:") for n in cfg["_notices"])
        finally:
            r1.delete("n3mc:test:otherdb")
            r1.close()

    def test_info_refused(self, cfg, rconn):
        d, sets = self._db(cfg, rconn)

        def refuse(*a, **k):
            raise redis.ResponseError("NOPERM")

        d.client.info = refuse
        d.enforce_ephemeral()
        assert not sets and any(n.startswith("shared_redis:") for n in cfg["_notices"])

    def test_config_refused(self, cfg, rconn):
        rconn.delete(conftest.TEST_MARKER)
        d = Database(cfg)
        d.client = conftest._connect(cfg["redis_url"])

        def refuse(*a, **k):
            raise redis.ResponseError("unknown command 'CONFIG'")

        d.client.config_set = refuse
        d.enforce_ephemeral()
        assert any(n.startswith("config_refused:") for n in cfg["_notices"])


class TestCodeOmittedOnSave:
    def test_short_and_parent(self, db, rconn):
        r = db.save_memory("a ```x``` b `inline`")
        assert rconn.hget(MEM + r["id"], "content") == b"a [code omitted] b `inline`"
        p = db.save_memory("```code```\n" + LONG)
        assert rconn.hget(DOC + p["parent_id"], "content").startswith(b"[code omitted]\n")

    def test_unclosed_fence(self, db, rconn):
        r = db.save_memory("text ```py\nnever closed")
        assert rconn.hget(MEM + r["id"], "content") == b"text [code omitted]"

    def test_code_only_difference_is_duplicate(self, db):
        db.save_memory("same ```a``` text")
        assert db.save_memory("same ```b``` text")["status"] == "duplicate"

    def test_skip_code_blocks(self, db, cfg):
        cfg["skip_code_blocks"] = True
        assert db.save_memory("has ```code```") == {"status": "skipped_code", "saved": False}

    def test_old_fenced_record_shown_omitted(self, db, rconn, runner):
        r = db.save_memory("old record placeholder text")
        rconn.hset(MEM + r["id"], "content", "old record ```secret``` text")
        assert "secret" not in runner.call("list_memories", {}, nudge=False)


class TestSurroundingWhitespace:
    def test_outer_strip(self, db, rconn):
        r = db.save_memory(" \nAAA\n ")
        assert rconn.hget(MEM + r["id"], "content") == b"AAA"

    def test_parent_keeps_inner_newlines(self, db, rconn):
        p = db.save_memory("\n\n" + LONG + "\n  ")
        assert rconn.hget(DOC + p["parent_id"], "content").decode() == LONG

    def test_whitespace_difference_is_duplicate(self, db):
        db.save_memory("BBB")
        assert db.save_memory("  BBB\n")["status"] == "duplicate"

    def test_whitespace_only(self, db):
        assert db.save_memory(" \n\t ")["reason"] == "empty content"


class TestModelWait:
    def test_loading_model_keyword_only(self, runner, rconn, monkeypatch):
        runner.db.save_memory("keyword only search target")
        monkeypatch.setattr(processor, "_model", None)
        monkeypatch.setattr(processor, "MODEL_WAIT_SECONDS", 0.3)
        processor._model_lock.acquire()
        try:
            out = runner.call("search_memory", {"query": "keyword only search target"}, nudge=False)
            assert "is still loading" in out and "keyword only search target" in out
            r = parse_json(runner.call("save_memory", {"content": "saved while loading"}))
            assert r["saved"] and r["warning"] == "embedding_unavailable" and "still loading" in r["detail"]
            assert not rconn.hexists(MEM + r["id"], "embedding")
        finally:
            processor._model_lock.release()


class TestListPaging:
    def test_single_before_huge_doc_listed(self, db):
        s = db.save_memory("the single record saved first")
        p = db.save_memory(" ".join(f"w{i}" for i in range(18000)))
        assert p["chunks"] >= 260
        ids = [r["id"] for r in db.list_entries(20)]
        assert s["id"] in ids and p["parent_id"] in ids


class TestNoTtlRefresh:
    def test_search_keeps_every_ttl(self, db, rconn):
        db.save_memory("ttl stays the same single")
        db.save_memory("ttl stays the same document\n" + LONG)
        for k in _all_keys(rconn):
            rconn.expire(k, 1000)
        db.search("ttl stays the same")
        db.search("設定項目 点検")
        assert all(0 < rconn.ttl(k) <= 1000 for k in _all_keys(rconn))


class TestOrBm25:
    def test_or_terms(self, db):
        r = db.save_memory("the quick fox jumped")
        for q in ("quick fox elephant", "what did the quick fox do"):
            assert r["id"] in [h["id"] for h in db._bm25_search(q, 10)]

    def test_cjk_or(self, db):
        r = db.save_memory("記憶装置の話")
        assert r["id"] in [h["id"] for h in db._bm25_search("記憶装置 大阪", 10)]

    def test_rerank_counts_punctuated_word(self):
        content = "a note, written down for later use in the long project plan"
        assert processor.rerank_bonus("note", content, DEFAULTS) == pytest.approx(0.3 + 0.2)


class TestTimeBoundsStrict:
    @pytest.mark.parametrize("value", ["yesterday", "2026/11/13", "2026-13-01"])
    def test_unreadable(self, value):
        with pytest.raises(ValueError, match=r"^Error: since must be an ISO 8601"):
            processor.parse_time_bounds(value, None)

    def test_since_after_until(self):
        with pytest.raises(ValueError, match="since is later than until"):
            processor.parse_time_bounds("2026-11-14", "2026-11-13")

    def test_date_only_is_local_midnight(self, monkeypatch):
        tz = timezone(timedelta(hours=9))
        monkeypatch.setattr(processor, "_local_tz", lambda: tz)
        s, u = processor.parse_time_bounds("2026-11-13", "2026-11-13")
        assert s == datetime(2026, 11, 13, tzinfo=tz).timestamp()
        assert u == datetime(2026, 11, 13, 23, 59, 59, 999999, tzinfo=tz).timestamp()

    def test_tool_returns_error(self, cfg):
        d = Database(cfg)
        out = tools.ToolRunner(cfg, d).call("search_memory", {"query": "x", "since": "yesterday"})
        assert out.startswith("Error: since must be") and out.endswith(tools._NUDGE_GENERIC)


class TestRepairReembed:
    def test_reembed(self, db, rconn):
        ids = [db.save_memory(f"no vector {i}", embed=False)["id"] for i in range(3)]
        for i in ids:
            rconn.expire(MEM + i, 1000)
        r = db.repair()
        assert r == {"status": "ok", "message": "index ensured", "reembedded": 3, "remaining": 0}
        assert all(rconn.hexists(MEM + i, "embedding") and rconn.ttl(MEM + i) <= 1000 for i in ids)

    def test_limit_and_remaining(self, db):
        for i in range(3):
            db.save_memory(f"later vector {i}", embed=False)
        r = db.repair(max_reembed=2)
        assert r["reembedded"] == 2 and r["remaining"] == 1


class TestDeleteOwner:
    def test_other_owner_not_found(self, db, cfg):
        d2 = Database(dict(cfg, owner_id="11111111-1111-4111-8111-111111111111"))
        d2.ensure_connected()
        r = d2.save_memory("not yours")
        assert db.delete_memory(r["id"])["status"] == "not_found"
        assert d2.delete_memory(r["id"])["status"] == "deleted"
        d2.close()

    def test_chunks_from_chunk_ids(self, db, rconn):
        p = db.save_memory(LONG)
        assert db.delete_memory(p["parent_id"])["chunks_deleted"] == p["chunks"]
        assert not any(rconn.exists(MEM + c) for c in p["ids"])


class TestGuardNx:
    def test_concurrent_same_text(self, db, cfg, rconn):
        others = [Database(cfg) for _ in range(4)]
        for o in others:
            o.ensure_connected()
        results = []
        ts = [threading.Thread(target=lambda o=o: results.append(o.save_memory("raced text"))) for o in others]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert sum(1 for r in results if r["saved"]) == 1
        assert len([k for k in _all_keys(rconn) if k.startswith(MEM) and not k.startswith(SHA)]) == 1
        [o.close() for o in others]

    def test_guard_released_on_write_failure(self, db, rconn):
        def half(keys, args):
            rconn.set(keys[0], args[0], ex=100)
            raise redis.ResponseError("write failed")

        db._scripts["save"] = half
        db.save_memory("write fails")
        assert not _all_keys(rconn)


class TestAccessCountNoGhost:
    def test_deleted_between_search_and_increment(self, db, rconn, monkeypatch):
        r = db.save_memory("ghost check record")
        real = database.shape_results

        def deleting(*a, **k):
            out = real(*a, **k)
            db.delete_memory(r["id"])
            return out

        monkeypatch.setattr(database, "shape_results", deleting)
        db.search("ghost check record")
        assert not _all_keys(rconn)

    def test_script_creates_nothing(self, db, rconn):
        db._scripts["incr"](keys=["mem:missing"], args=["access_count", 1])
        db._scripts["hset"](keys=["mem:missing"], args=["embedding", b"x"])
        assert not rconn.exists("mem:missing")


class TestRecallOrphanChunks:
    def test_parent_once_then_orphans(self, db, rconn, runner):
        p = db.save_memory(LONG, turn_id="turn-x")
        out = runner.call("recall_thread", {"turn_id": "turn-x"}, nudge=False)
        assert out.count("▶") == 1 and f"[doc×{p['chunks']}] [{p['parent_id']}]" in out and LONG in out
        rconn.delete(DOC + p["parent_id"])
        out = runner.call("recall_thread", {"turn_id": "turn-x"}, nudge=False)
        assert out.count("▶") == p["chunks"] and "not_found" not in out


class TestDeleteIdPrefix:
    def test_prefixes(self, db, rconn):
        p = db.save_memory(LONG)
        assert db.delete_memory("doc:" + p["parent_id"]) == {"status": "deleted", "id": p["parent_id"],
                                                             "chunks_deleted": p["chunks"]}
        s = db.save_memory("prefixed single")
        assert db.delete_memory("mem:" + s["id"]) == {"status": "deleted", "id": s["id"]}
        assert not rconn.exists(SHA + sha1_hex("prefixed single"))
        q = db.save_memory("double prefix")
        assert db.delete_memory("doc:doc:" + q["id"])["status"] == "not_found"


class TestNudgeOnErrors:
    def _runner(self, cfg, monkeypatch):
        d = Database(cfg)
        monkeypatch.setattr(d, "ensure_connected", lambda enforce=True: "ok")
        return tools.ToolRunner(cfg, d), d

    def test_importance_nan(self, cfg, monkeypatch):
        r, _d = self._runner(cfg, monkeypatch)
        out = r.call("save_memory", {"content": "x", "importance": "nan"})
        assert out.startswith('{"saved": false, "status": "error", "reason": "importance must be numeric')
        assert out.endswith(tools._NUDGE_GENERIC)

    def test_since_error(self, cfg, monkeypatch):
        r, _d = self._runner(cfg, monkeypatch)
        out = r.call("search_memory", {"query": "x", "since": "nope"})
        assert out.startswith("Error: since") and out.endswith(tools._NUDGE_GENERIC)

    def test_unexpected_exception(self, cfg, monkeypatch):
        r, d = self._runner(cfg, monkeypatch)
        monkeypatch.setattr(d, "list_entries", lambda limit: 1 / 0)
        out = r.call("list_memories", {})
        assert out.startswith("Error: ZeroDivisionError") and out.endswith(tools._NUDGE_GENERIC)

    def test_backend_error(self, cfg):
        out = tools.ToolRunner(cfg, Database(dict(cfg, redis_url=conftest.UNREACHABLE_URL))).call(
            "search_memory", {"query": "x"})
        assert out.startswith("Error: memory backend unreachable.") and out.endswith(tools._NUDGE_GENERIC)


class TestConditionalWrites:
    def test_scripts_keep_ttl(self, db, rconn):
        r = db.save_memory("conditional write")
        rconn.expire(MEM + r["id"], 500)
        db._scripts["incr"](keys=[MEM + r["id"]], args=["access_count", 2])
        db._scripts["hset"](keys=[MEM + r["id"]], args=["embedding", b"\x00" * 4])
        assert 0 < rconn.ttl(MEM + r["id"]) <= 500 and int(rconn.hget(MEM + r["id"], "access_count")) == 2

    def test_repair_skips_vanished(self, db, rconn, monkeypatch):
        r = db.save_memory("vanishing", embed=False)
        real = processor.embed

        def vanish(*a, **k):
            rconn.delete(MEM + r["id"])
            return real(*a, **k)

        monkeypatch.setattr(processor, "embed", vanish)
        out = db.repair()
        assert out["reembedded"] == 0 and not rconn.exists(MEM + r["id"])

    def test_every_key_has_ttl(self, db, rconn):
        db.save_memory("ttl one")
        db.save_memory(LONG)
        db.save_memory("no vec", embed=False)
        db.search("ttl one")
        db.repair()
        assert all(rconn.ttl(k) > 0 for k in _all_keys(rconn))


class TestHangulFiller:
    @pytest.mark.parametrize("ch", ["\u3164", "\u115f", "\u1160", "\uffa0", "\u2800"])
    def test_rejected(self, ch):
        assert processor.has_visible_text(ch * 3) is False

    def test_verbatim_with_visible(self, db, rconn):
        r = db.save_memory("A\u3164B\u2800C")
        assert rconn.hget(MEM + r["id"], "content").decode() == "A\u3164B\u2800C"


def _load_plugin_script():
    path = ROOT / "plugins" / "n3mc-workingmemory" / "hooks" / "install_permissions.py"
    spec = importlib.util.spec_from_file_location("install_permissions_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestPluginHook:
    def test_rereads_and_replaces(self, isolated, monkeypatch, capsys):
        mod = _load_plugin_script()
        reads, repl = [], []
        real_read = mod._read_settings
        monkeypatch.setattr(mod, "_read_settings", lambda p: (reads.append(1), real_read(p))[1])
        real_replace = os.replace
        monkeypatch.setattr(mod.os, "replace", lambda a, b: (repl.append(b), real_replace(a, b)))
        monkeypatch.setattr(mod.shutil, "which", lambda n: "uvx")
        assert mod.main() == 0
        settings = json.loads((isolated["home"] / ".claude" / "settings.json").read_text(encoding="utf-8"))
        assert len(reads) == 2 and repl
        allow = settings["permissions"]["allow"]
        assert f"Bash(uvx --from n3memorycore-mcp-lite=={__version__} n3mc-lite --search *)" in allow
        assert not any("--delete" in a for a in allow)

    def test_fallback_text(self):
        hooks = (ROOT / "plugins" / "n3mc-workingmemory" / "hooks" / "hooks.json").read_text(encoding="utf-8")
        assert "or the permission script failed" in hooks

    def test_uvx_missing_text(self, isolated, monkeypatch, capsys):
        mod = _load_plugin_script()
        monkeypatch.setattr(mod.shutil, "which", lambda n: None)
        mod.main()
        msg = json.loads(capsys.readouterr().out)["systemMessage"]
        assert "[n3mc-workingmemory] uvx was not found on PATH." in msg
        assert "irm https://astral.sh/uv/install.ps1 | iex" in msg and "uv を入れてください" in msg


class TestWeakMatches:
    def test_fill_to_min_results(self):
        cfg = _cfg(lexical_rerank_enabled=False)
        rows = [cand("good", cos=0.99), cand("w1", cos=0.83), cand("w2", cos=0.81), cand("w3", cos=0.80)]
        normal, weak = shape_results(rows, "q", cfg, "s", 10)
        assert [r["id"] for r in normal] == ["good"]
        assert [r["id"] for r in weak] == ["w1", "w2"] and all(r["_weak"] for r in weak)
        assert weak[0]["filter_score"] == pytest.approx(0.83 * 0.7)

    def test_zero_min_results(self):
        rows = [cand("good", cos=0.99), cand("w1", cos=0.83)]
        assert shape_results(rows, "q", _cfg(min_results=0), "s", 10)[1] == []

    def test_enough_rows(self):
        rows = [cand(f"g{i}", cos=0.99) for i in range(3)] + [cand("w", cos=0.8)]
        assert shape_results(rows, "q", _cfg(), "s", 10)[1] == []

    def test_limit_caps(self):
        rows = [cand("good", cos=0.99), cand("w1", cos=0.83), cand("w2", cos=0.82)]
        normal, weak = shape_results(rows, "q", _cfg(), "s", 2)
        assert len(normal) + len(weak) == 2

    def test_one_row_per_parent(self):
        a = cand("parent", cos=0.83, _doc=True, _hit_key="c1")
        b = cand("parent", cos=0.82, _doc=True, _hit_key="c2")
        good = cand("good", cos=0.99)
        normal, weak = shape_results([good, a, b], "q", _cfg(), "s", 10)
        assert [r["id"] for r in weak] == ["parent"]
        normal, weak = shape_results([cand("parent", cos=0.99, _doc=True), a], "q", _cfg(), "s", 10)
        assert weak == []

    def test_tiny_filter_score_dropped(self):
        rows = [cand("good", cos=0.99), cand("zero", cos=0.0000001)]
        assert shape_results(rows, "q", _cfg(lexical_rerank_enabled=False), "s", 10)[1] == []

    def test_empty_query(self):
        rows = [cand("good", cos=0.99), cand("w1", cos=0.83)]
        assert shape_results(rows, "   ", _cfg(), "s", 10)[1] == []
        assert shape_results(rows, "", _cfg(), "s", 10)[1] == []

    def test_default_min_results(self):
        assert DEFAULTS["min_results"] == 3

    def test_weak_rows_not_counted(self, db, rconn, cfg):
        cfg["vector_min_cos"] = 0.999
        good = db.save_memory("zephyr lantern festival schedule")
        weak = db.save_memory("an entirely unrelated grocery list entry")
        normal, weak_rows, _m = db.search("zephyr lantern festival schedule")
        assert [r["id"] for r in normal] == [good["id"]] and weak["id"] in [w["id"] for w in weak_rows]
        assert int(rconn.hget(MEM + good["id"], "access_count")) == 1
        assert int(rconn.hget(MEM + weak["id"], "access_count")) == 0


class TestGuardReleasedOnAnyFailure:
    @pytest.mark.parametrize("text", ["short body", LONG])
    def test_non_string_turn_id(self, db, rconn, text):
        assert db.save_memory(text, turn_id=12)["status"] == "error"
        assert not _all_keys(rconn)
        assert db.save_memory(text)["saved"]

    def test_surrogates_in_fields(self, db, rconn):
        r = db.save_memory("fields", agent_name="ag" + chr(0xD800), session_id="s" + chr(0xDC00),
                           turn_id="t" + chr(0xDBFF))
        h = rconn.hgetall(MEM + r["id"])
        assert (h[b"agent_name"], h[b"session_id"], h[b"turn_id"]) == (b"ag", b"s", b"t")
        r2 = db.save_memory("fields 2", agent_name="plain", session_id="sess", turn_id="turn")
        assert rconn.hget(MEM + r2["id"], "agent_name") == b"plain"


class TestPluginHookAsciiStdout:
    def test_ascii_one_line(self, isolated):
        script = ROOT / "plugins" / "n3mc-workingmemory" / "hooks" / "install_permissions.py"
        env = dict(os.environ)
        env["PATH"] = os.path.dirname(sys.executable)
        out = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, timeout=60)
        text = out.stdout.decode("ascii")
        assert text.count("\n") == 1
        assert "uv を入れてください" in json.loads(text)["systemMessage"]


class TestPublicRepoHygiene:
    def test_eval_script_has_no_user_folder(self):
        text = (ROOT / "tests" / "eval_uvx_missing.py").read_text(encoding="utf-8")
        assert "C:\\Users\\" not in text and "C:/Users/" not in text and "/home/" not in text

    def test_marketplace_version(self):
        data = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
        assert data["metadata"]["version"] == __version__
