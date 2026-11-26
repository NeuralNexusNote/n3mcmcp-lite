"""Layer 4: regression tests (spec §9 Layer 4). Stand-in embeddings; classes marked Redis need the test Redis."""
import json
import math
import os
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from conftest import MARKER, fake_vector, parse_json, test_url_problem
from n3mc_mcp import config as CF
from n3mc_mcp import database as D
from n3mc_mcp import processor as P
from n3mc_mcp.tools import _NUDGE_AFTER_SAVE, _NUDGE_GENERIC, ToolRunner, parse_bounds


def _data_keys(r):
    return [k for k in r.scan_iter() if k != MARKER.encode()]


def _write_cfg(isolate, **values):
    p = isolate["data"] / "config.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    d.update(values)
    p.write_text(json.dumps(d), encoding="utf-8")
    return p


def _vec_with_cos(base, cos):
    """A unit vector with the given cosine to the unit vector base."""
    other = [0.0] * len(base)
    # an axis orthogonal to base
    idx = max(range(1, len(base)), key=lambda i: -abs(base[i]))
    other[idx] = 1.0
    dot = sum(a * b for a, b in zip(other, base))
    other = [o - dot * b for o, b in zip(other, base)]
    n = math.sqrt(sum(x * x for x in other))
    other = [x / n for x in other]
    s = math.sqrt(max(0.0, 1 - cos * cos))
    return [cos * b + s * o for b, o in zip(base, other)]


class TestQueryCleaning:
    def test_removed(self):
        q = P.prepare_query("「記憶装置」は？\x00；")
        assert "「" not in q and "\x00" not in q and "？" not in q

    def test_brackets_query_hits(self, db):
        db.save_memory("記憶装置の仕様をまとめた")
        rows = db._bm25_search("「記憶装置」は？", 10)
        assert rows

    @pytest.mark.parametrize("q", ["\x00", "!!!", "@#$%^&*()", "_", "-", "|", "*", "~~~", "「」", "'\"", "{}[]"])
    def test_symbols_do_not_raise(self, db, q):
        db._bm25_search(q, 10)
        db.search(q)


class TestExactOnlyDedup:
    def test_updated_fact(self, db):
        assert db.save_memory("The project budget is 100 dollars.")["saved"]
        assert db.save_memory("The project budget is 120 dollars.")["saved"]

    def test_remade_document(self, db):
        doc = "Setting %d: value %d. " * 1
        a = "".join(f"Setting {i}: value {i}. " for i in range(40))
        b = a.replace("value 7.", "value 70.").replace("value 9.", "value 90.")
        assert db.save_memory(a)["saved"] and db.save_memory(b)["saved"]

    def test_exact_rejected(self, db):
        db.save_memory("exact same")
        assert db.save_memory("exact same")["status"] == "duplicate"


class TestTimeWindow:
    def test_window_finds_old_rows(self, db, cfg):
        old = db.save_memory("window target alpha old record")
        old_ts = time.time() - 2 * 86400
        db.client.hset("mem:" + old["id"], "timestamp_epoch", repr(old_ts))
        for i in range(30):
            db.save_memory(f"window target alpha old record {i}")
        until = datetime.fromtimestamp(old_ts + 3600).isoformat(timespec="seconds")
        lo, hi = parse_bounds(None, until)
        normal, weak, _m = db.search("window target alpha old record", limit=3, until=hi)
        assert any(r["id"] == old["id"] for r in normal + weak)


class TestShaping:
    def _rows(self, db, monkeypatch, specs, query="shape query words"):
        """specs: list of (content, cos). Vectors are set so that each row has that cosine to the query."""
        qv = fake_vector(query)
        vectors = {c: _vec_with_cos(qv, cos) for c, cos in specs}
        orig = P.embed_many

        def emb(texts, kind="passage", no_wait=False):
            return [vectors.get(t, qv if kind == "query" else fake_vector(t)) for t in texts]

        monkeypatch.setattr(P, "embed_many", emb)
        ids = {}
        for c, _cos in specs:
            ids[c] = db.save_memory(c)["id"]
        return ids

    def test_vector_floor(self, db, cfg, monkeypatch):
        cfg["min_results"] = 0
        ids = self._rows(db, monkeypatch, [("zz low row", 0.5), ("zz high row", 0.95)])
        normal, weak, _ = db.search("shape query words")
        got = [r["id"] for r in normal]
        assert ids["zz high row"] in got and ids["zz low row"] not in got

    def test_relative_cut(self, db, cfg, monkeypatch):
        cfg["min_results"] = 0
        cfg["vector_min_cos"] = 0.1
        cfg["lexical_rerank_enabled"] = False
        ids = self._rows(db, monkeypatch, [("qq top row", 0.99), ("qq weak row", 0.3)])
        normal, _w, _ = db.search("shape query words")
        assert [r["id"] for r in normal] == [ids["qq top row"]]

    def test_newest_version_first(self, db, cfg, monkeypatch):
        cfg["lexical_rerank_enabled"] = False
        base = "Version text about the configuration of the gateway server and its many settings. " * 2
        older, newer = base + "v1", base + "v2"
        qv = fake_vector("shape query words")
        v_old = _vec_with_cos(qv, 0.97)
        v_new = [x * 0.999 + y * 0.001 for x, y in zip(v_old, qv)]
        n = math.sqrt(sum(x * x for x in v_new))
        v_new = [x / n for x in v_new]

        def emb(texts, kind="passage", no_wait=False):
            out = []
            for t in texts:
                out.append(v_old if t == older else v_new if t == newer else qv)
            return out

        monkeypatch.setattr(P, "embed_many", emb)
        a = db.save_memory(older)
        time.sleep(0.01)
        b = db.save_memory(newer)
        normal, _w, _ = db.search("shape query words")
        assert normal[0]["id"] == b["id"] and normal[1]["id"] == a["id"]
        assert normal[0]["score"] >= normal[1]["score"]
        assert normal[0].get("_version_group") and normal[1].get("_version_group")

    def test_short_rows_not_grouped(self, db, cfg, monkeypatch):
        ids = self._rows(db, monkeypatch, [("short a", 0.97), ("short b", 0.97)])
        normal, _w, _ = db.search("shape query words")
        assert not any(r.get("_version_group") for r in normal)


class TestRender:
    def _row(self, **kw):
        r = {"id": "abc", "content": "body", "score": 0.5, "timestamp": "2026-11-13T01:34:00+00:00", "turn_id": ""}
        r.update(kw)
        return r

    def test_saved_local(self, monkeypatch):
        from n3mc_mcp import context as C
        monkeypatch.setenv("TZ", "UTC")
        out = C.render_hit(self._row())
        assert "saved=2026-11-13" in out and "(UTC" in out

    def test_version_line_once(self):
        from n3mc_mcp import context as C
        out = C.render_search([self._row(), self._row(id="def")], [])
        assert out.count(C.VERSION_LINE) == 1

    def test_notices_once(self, runner, cfg):
        cfg["_notices"].append("config_type: test notice")
        first = runner.call("search_memory", {"query": "x"}, nudge=False)
        second = runner.call("search_memory", {"query": "x"}, nudge=False)
        assert "config_type: test notice" in first and "config_type: test notice" not in second
        cfg["_notices"].append("shared_redis: later notice")
        assert "later notice" in runner.call("search_memory", {"query": "x"}, nudge=False)

    def test_weak_after_normal(self):
        from n3mc_mcp import context as C
        out = C.render_search([self._row()], [self._row(id="weak1")])
        assert out.index("[abc]") < out.index(C.WEAK_HEADING) < out.index("[weak1]")

    def test_no_normal_with_weak(self):
        from n3mc_mcp import context as C
        out = C.render_search([], [self._row(id="weak1")])
        assert out.startswith(C.NO_MEMORIES) and C.VERSION_LINE not in out and C.WEAK_HEADING in out

    def test_no_weak_heading(self):
        from n3mc_mcp import context as C
        assert C.WEAK_HEADING not in C.render_search([self._row()], [])


class TestConfig:
    def test_unreadable_not_changed(self, isolate):
        p = isolate["data"] / "config.json"
        p.write_text("{not json", encoding="utf-8")
        c = CF.load_config()
        assert p.read_text(encoding="utf-8") == "{not json"
        assert c["_config_unreadable"] and any(n.startswith("config_unreadable") for n in c["_notices"])

    def test_wrong_type_ignored(self, isolate):
        _write_cfg(isolate, vector_min_cos="0.83")
        c = CF.load_config()
        assert c["vector_min_cos"] == 0.85
        assert any("vector_min_cos" in n for n in c["_notices"])

    def test_bool_is_not_number(self, isolate):
        _write_cfg(isolate, min_score=True)
        assert CF.load_config()["min_score"] == 0.2

    def test_missing_filled(self, isolate):
        CF.load_config()
        d = json.loads((isolate["data"] / "config.json").read_text(encoding="utf-8"))
        assert d["ttl_seconds"] == 604800 and d["owner_id"]


class TestTestSafety:
    def test_refuses_live_url(self):
        assert test_url_problem("redis://localhost:6379/0", {"redis://localhost:6379/0"})
        assert test_url_problem("", set())
        assert test_url_problem("redis://localhost:6390/0", {"redis://localhost:6379/0"}) is None

    def test_bom_config_read(self, tmp_path, monkeypatch):
        import conftest
        d = tmp_path / "live"
        d.mkdir()
        (d / "config.json").write_bytes(b"\xef\xbb\xbf" + json.dumps({"redis_url": "redis://x:1/0"}).encode())
        monkeypatch.setenv("N3MC_DATA_DIR", str(d))
        assert "redis://x:1/0" in conftest._live_redis_urls()

    def test_does_not_flush_unmarked(self, rconn):
        import conftest
        rconn.delete(MARKER)
        rconn.set("user-data", 1)
        try:
            with pytest.raises(pytest.skip.Exception):
                conftest.safe_flush(rconn)
            assert rconn.exists("user-data")
        finally:
            rconn.delete("user-data")
            rconn.set(MARKER, 1)


class TestFilterScore:
    def test_old_and_other_session_survive(self, db, cfg, monkeypatch):
        cfg["lexical_rerank_enabled"] = False
        query = "filter score query"
        qv = fake_vector(query)
        specs = {"fs current new": 0.86, "fs same old": 0.95, "fs other a": 0.99, "fs other b": 0.95}
        vecs = {k: _vec_with_cos(qv, v) for k, v in specs.items()}
        monkeypatch.setattr(P, "embed_many",
                            lambda texts, kind="passage", no_wait=False: [vecs.get(t, qv) for t in texts])
        ids = {}
        ids["new"] = db.save_memory("fs current new", session_id="CUR")["id"]
        ids["old"] = db.save_memory("fs same old", session_id="CUR")["id"]
        ids["a"] = db.save_memory("fs other a", session_id="OTHER")["id"]
        ids["b"] = db.save_memory("fs other b", session_id="OTHER")["id"]
        now = time.time()
        db.client.hset("mem:" + ids["old"], "timestamp_epoch", repr(now - 3 * 86400))
        db.client.hset("mem:" + ids["a"], "timestamp_epoch", repr(now - 86400))
        db.client.hset("mem:" + ids["b"], "timestamp_epoch", repr(now - 86400))
        normal, _w, _ = db.search(query, session_id="CUR")
        got = [r["id"] for r in normal]
        assert set(got) == set(ids.values())
        assert got[0] == ids["new"]
        assert [r["score"] for r in normal] == sorted((r["score"] for r in normal), reverse=True)


class TestRedisTimeouts:
    def test_socket_timeout(self, db):
        kw = db.client.connection_pool.connection_kwargs
        assert kw["socket_timeout"] == 5 and kw["socket_connect_timeout"] == 2

    def test_unresponsive_server(self, cfg):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        port = srv.getsockname()[1]
        c = dict(cfg)
        c["redis_url"] = f"redis://127.0.0.1:{port}/0"
        d = D.Database(c)
        t = time.monotonic()
        res = d.ensure_connected()
        assert res != "ok" and "not reachable" in res
        assert time.monotonic() - t < 8
        srv.close()

    def test_reconnect_closes_old(self, db, monkeypatch):
        old = db.client
        closed = []
        monkeypatch.setattr(old, "close", lambda: closed.append(True))
        db.connected = False
        assert db.ensure_connected() == "ok"
        assert closed and db.client is not old


class TestConfigCreate:
    def test_concurrent_create_one_owner(self, isolate):
        p = isolate["data"] / "config.json"
        p.unlink()
        out = []

        def go():
            out.append(CF.load_config()["owner_id"])

        ts = [threading.Thread(target=go) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        disk = json.loads(p.read_text(encoding="utf-8"))["owner_id"]
        assert set(out) == {disk}

    def test_fill_ids_concurrently(self, isolate):
        p = _write_cfg(isolate, vector_min_cos=0.83)
        out = []

        def go():
            c = CF.load_config()
            out.append((c["owner_id"], c["local_id"]))

        ts = [threading.Thread(target=go) for _ in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        disk = json.loads(p.read_text(encoding="utf-8"))
        assert set(out) == {(disk["owner_id"], disk["local_id"])}
        assert disk["vector_min_cos"] == 0.83

    def test_update_uses_replace(self, isolate, monkeypatch):
        calls = []
        real = os.replace
        monkeypatch.setattr(os, "replace", lambda a, b: (calls.append((a, b)), real(a, b))[1])
        CF.load_config()
        assert calls

    def test_migration_exclusive(self, isolate, monkeypatch, tmp_path):
        legacy = tmp_path / "legacy" / "config.json"
        legacy.parent.mkdir()
        legacy.write_text(json.dumps({"owner_id": "legacy-owner", "local_id": "l"}), encoding="utf-8")
        monkeypatch.delenv("N3MC_DATA_DIR")
        monkeypatch.setattr(CF.paths, "legacy_config_path", lambda: str(legacy))
        links = []
        real = os.link
        monkeypatch.setattr(os, "link", lambda a, b: (links.append(b), real(a, b))[1])
        c = CF.load_config()
        assert c["owner_id"] == "legacy-owner" and links


class TestConfigRanges:
    @pytest.mark.parametrize("key,value", [("ttl_seconds", 604800.5), ("ttl_seconds", 0), ("embedding_dim", 768.0),
                                           ("chunk_threshold", 10), ("half_life_days", 0), ("min_score", 1.5),
                                           ("min_results", -1), ("min_results", 1.5)])
    def test_rejected(self, isolate, key, value):
        _write_cfg(isolate, **{key: value})
        c = CF.load_config()
        assert c[key] == CF.DEFAULTS[key]
        assert any(n.startswith("config_type") and key in n for n in c["_notices"])

    def test_overlap_ge_threshold(self, isolate):
        _write_cfg(isolate, chunk_threshold=200, chunk_overlap=200)
        c = CF.load_config()
        assert c["chunk_overlap"] == 100 and any("chunk_overlap" in n for n in c["_notices"])

    def test_min_results_zero_ok(self, isolate):
        _write_cfg(isolate, min_results=0)
        assert CF.load_config()["min_results"] == 0

    def test_chunk_text_terminates(self):
        assert P.chunk_text("z" * 1000, 100, 100)

    def test_threshold_only(self, isolate):
        _write_cfg(isolate, chunk_threshold=100)
        c = CF.load_config()
        assert c["chunk_overlap"] == 25 and not c["_notices"]
        assert json.loads((isolate["data"] / "config.json").read_text(encoding="utf-8"))["chunk_overlap"] == 25
        assert not CF.load_config()["_notices"]


class TestLongSaveAtomic:
    def test_embedding_failure_leaves_nothing(self, db, rconn, monkeypatch):
        def boom(texts, kind="passage", no_wait=False):
            raise RuntimeError("embedding failed")

        monkeypatch.setattr(P, "embed_many", boom)
        with pytest.raises(RuntimeError):
            db.save_memory("long atomic sentence. " * 40)
        assert not _data_keys(rconn)
        monkeypatch.setattr(P, "embed_many", lambda t, kind="passage", no_wait=False: [fake_vector(x) for x in t])
        assert db.save_memory("long atomic sentence. " * 40)["saved"]

    def test_one_script(self, db, monkeypatch):
        calls = []
        real = db._run_save
        monkeypatch.setattr(db, "_run_save", lambda *a: (calls.append(a), real(*a))[1])
        r = db.save_memory("one script sentence. " * 40)
        assert len(calls) == 1 and len(calls[0][3]) == 1 + r["chunks"]


class TestGuardScriptAndPortReload:
    @pytest.mark.parametrize("body", ["short body", "long body sentence. " * 40])
    def test_interrupted_save(self, db, rconn, monkeypatch, body):
        import redis
        real = D.Database._script

        def broken(self, name, src):
            if name == "save":
                def fail(**kw):
                    raise redis.ConnectionError("interrupted")
                return fail
            return real(self, name, src)

        monkeypatch.setattr(D.Database, "_script", broken)
        with pytest.raises(redis.ConnectionError):
            db.save_memory(body)
        assert not _data_keys(rconn)
        monkeypatch.setattr(D.Database, "_script", real)
        assert db.save_memory(body)["saved"]
        assert all(rconn.ttl(k) > 0 for k in _data_keys(rconn))
        assert db.save_memory(body)["status"] == "duplicate"

    def test_release_only_own_guard(self, db, rconn):
        rconn.set("mem:sha:x", "other-id")
        db._release_guard("mem:sha:x", "my-id", "mem:my-id")
        assert rconn.get("mem:sha:x") == b"other-id"
        rconn.set("mem:sha:y", "my-id")
        db._release_guard("mem:sha:y", "my-id", "mem:my-id")
        assert not rconn.exists("mem:sha:y")

    def test_entry_follows_port_change(self, isolate, monkeypatch):
        from n3mc_mcp import cli, server
        thin = server.ThinClient()
        ports = []
        monkeypatch.setattr(cli, "ensure_server", lambda cfg, max_wait=60: (ports.append(cfg["server_port"]), True)[1])
        monkeypatch.setattr(cli, "_post", lambda port, path, body, timeout=30: (200, {"text": "ok"}))
        thin.handle("list_memories", {})
        _write_cfg(isolate, server_port=23456)
        thin.handle("list_memories", {})
        assert ports[-1] == 23456

    def test_unreadable_keeps_last_good(self, isolate, monkeypatch):
        from n3mc_mcp import cli, server
        thin = server.ThinClient()
        owner = thin.cfg["owner_id"]
        monkeypatch.setattr(cli, "ensure_server", lambda cfg, max_wait=60: True)
        monkeypatch.setattr(cli, "_post", lambda port, path, body, timeout=30: (200, {"text": "ok"}))
        (isolate["data"] / "config.json").write_text("{broken", encoding="utf-8")
        thin.handle("list_memories", {})
        assert thin.cfg["owner_id"] == owner


class TestIndexFailure:
    def test_unknown_command(self, cfg, monkeypatch):
        import redis

        class FakeClient:
            connection_pool = type("P", (), {"connection_kwargs": {"db": 0}})()

            def ping(self):
                return True

            def info(self, *a):
                return {}

            def scan_iter(self, **kw):
                return iter(())

            def config_set(self, *a):
                return True

            def execute_command(self, *a):
                if a[0] == "FT.CREATE":
                    raise redis.ResponseError("unknown command 'FT.CREATE'")
                raise AssertionError(a)

            def close(self):
                pass

        d = D.Database(cfg)
        monkeypatch.setattr(d, "_make_client", lambda: FakeClient())
        res = d.ensure_connected()
        assert "does not provide RediSearch" in res
        text = ToolRunner(cfg, d).call("search_memory", {"query": "x"}, nudge=False)
        assert text.startswith("Error: memory backend unreachable.") and "Redis Stack" in text
        calls = []
        monkeypatch.setattr(d, "ensure_index", lambda: calls.append(1) or False)
        d.ensure_connected()
        assert calls

    def test_dim_mismatch(self, db, cfg):
        cfg2 = dict(cfg)
        cfg2["embedding_dim"] = 1024
        cfg2["_notices"] = []
        d2 = D.Database(cfg2)
        d2.ensure_connected()
        assert any(n.startswith("index_dim_mismatch") for n in cfg2["_notices"])


class TestIndexFtsField:
    def test_no_prefix_in_index_fields(self, db, rconn):
        r = db.save_memory("hello claude user words", role="user", turn_id="t1")
        h = rconn.hgetall("mem:" + r["id"])
        assert h[b"content"].startswith(b"[user] ")
        assert b"[user]" not in h[b"content_fts"] and b"user" not in h[b"content_ngram"].split()[:1]
        long = db.save_memory("long body text. " * 40, role="claude", turn_id="t2")
        h2 = rconn.hgetall("mem:" + long["ids"][0])
        assert h2[b"content"].startswith(b"[claude 1/")
        assert b"[claude" not in h2[b"content_fts"]

    def test_code_omitted_not_indexed(self, db, rconn):
        r = db.save_memory("text\n```\ncode\n```\nmore")
        h = rconn.hgetall("mem:" + r["id"])
        assert b"omitted" not in h[b"content_fts"] and b"omitted" not in h[b"content_ngram"]

    def test_prefix_words_do_not_hit(self, db):
        db.save_memory("a message about gardens", role="user", turn_id="t1")
        db.save_memory("x\n```\ny\n```\nz", role="claude", turn_id="t1")
        assert not db._bm25_search("claude", 10)
        assert not db._bm25_search("omitted", 10)

    def test_old_index_rebuilt_once(self, db, rconn):
        r = db.save_memory("kept record")
        rconn.execute_command("FT.DROPINDEX", D.INDEX_NAME)
        rconn.execute_command("FT.CREATE", D.INDEX_NAME, "ON", "HASH", "PREFIX", "1", "mem:", "SCHEMA",
                              "content", "TEXT", "embedding", "VECTOR", "FLAT", "6", "TYPE", "FLOAT32", "DIM", "768",
                              "DISTANCE_METRIC", "COSINE")
        db.ensure_index()
        assert "content_fts" in db._index_info()
        assert rconn.exists("mem:" + r["id"])
        drops = []
        real = db.client.execute_command
        db.client.execute_command = lambda *a: (drops.append(a[0]) if a[0] == "FT.DROPINDEX" else None, real(*a))[1]
        db.ensure_index()
        assert "FT.DROPINDEX" not in drops

    def test_other_dim_not_rebuilt(self, db, rconn, cfg):
        rconn.execute_command("FT.DROPINDEX", D.INDEX_NAME)
        rconn.execute_command("FT.CREATE", D.INDEX_NAME, "ON", "HASH", "PREFIX", "1", "mem:", "SCHEMA",
                              "content", "TEXT", "embedding", "VECTOR", "FLAT", "6", "TYPE", "FLOAT32", "DIM", "4",
                              "DISTANCE_METRIC", "COSINE")
        cfg["_notices"] = []
        db.ensure_index()
        assert "content_fts" not in db._index_info()
        assert any(n.startswith("index_dim_mismatch") for n in cfg["_notices"])


class TestEnforceEphemeral:
    def _db(self, cfg):
        c = dict(cfg)
        c["_notices"] = []
        return D.Database(c), c

    def _clear_db1(self):
        import redis
        r1 = redis.from_url(os.environ["N3MC_REDIS_TEST_URL"].rsplit("/", 1)[0] + "/1")
        r1.delete("__n3mc_other_db_key__")
        r1.close()

    def test_empty_db(self, rconn, cfg):
        self._clear_db1()
        rconn.delete(MARKER)
        try:
            d, c = self._db(cfg)
            d.ensure_connected()
            assert not any(n.startswith("shared_redis") for n in c["_notices"])
            assert list(rconn.config_get("appendonly").values())[0] in (b"no", "no")
        finally:
            rconn.set(MARKER, 1)

    def test_n3mc_keys_only(self, rconn, cfg):
        self._clear_db1()
        rconn.delete(MARKER)
        try:
            rconn.set("mem:sha:abc", "x", ex=60)
            rconn.hset("doc:abc", "content", "x")
            rconn.expire("doc:abc", 60)
            d, c = self._db(cfg)
            assert d.ensure_connected() == "ok"
            assert not any(n.startswith("shared_redis") for n in c["_notices"])
        finally:
            rconn.set(MARKER, 1)

    def test_other_keys(self, rconn, cfg):
        rconn.set("someone-else", 1, ex=60)
        d, c = self._db(cfg)
        d.ensure_connected()
        assert any(n.startswith("shared_redis") for n in c["_notices"])

    def test_other_db(self, rconn, cfg):
        import redis
        r1 = redis.from_url(os.environ["N3MC_REDIS_TEST_URL"].rsplit("/", 1)[0] + "/1")
        rconn.delete(MARKER)
        try:
            r1.set("__n3mc_other_db_key__", 1, ex=60)
            d, c = self._db(cfg)
            d.ensure_connected()
            assert any(n.startswith("shared_redis") for n in c["_notices"])
        finally:
            r1.delete("__n3mc_other_db_key__")
            r1.close()
            rconn.set(MARKER, 1)

    def test_info_refused_and_config_refused(self, rconn, cfg, monkeypatch):
        import redis
        d, c = self._db(cfg)
        d.ensure_connected()
        c["_notices"].clear()
        monkeypatch.setattr(d.client, "info", lambda *a: (_ for _ in ()).throw(redis.ResponseError("NOPERM")))
        d.enforce_ephemeral()
        assert any(n.startswith("shared_redis") for n in c["_notices"])
        c["_notices"].clear()
        rconn.delete(MARKER)
        try:
            self._clear_db1()
            monkeypatch.setattr(d.client, "info", lambda *a: {})
            monkeypatch.setattr(d.client, "config_set",
                                lambda *a: (_ for _ in ()).throw(redis.ResponseError("unknown command CONFIG")))
            d.enforce_ephemeral()
            assert any(n.startswith("config_refused") for n in c["_notices"])
        finally:
            rconn.set(MARKER, 1)


class TestCodeOmittedOnSave:
    def test_short_and_long(self, db, rconn):
        r = db.save_memory("a `inline` b\n```\nsecret()\n```\nc")
        assert rconn.hget("mem:" + r["id"], "content") == "a `inline` b\n[code omitted]\nc".encode()
        long = db.save_memory("long text with code. " * 30 + "\n```\nx\n```")
        assert b"[code omitted]" in rconn.hget("doc:" + long["parent_id"], "content")

    def test_unclosed(self, db, rconn):
        r = db.save_memory("start\n```\nnever closed")
        assert rconn.hget("mem:" + r["id"], "content") == b"start\n[code omitted]"

    def test_code_only_difference_is_duplicate(self, db):
        db.save_memory("same\n```\na\n```")
        assert db.save_memory("same\n```\nb\n```")["status"] == "duplicate"

    def test_skip_code_blocks(self, db, cfg):
        cfg["skip_code_blocks"] = True
        assert db.save_memory("x\n```\ny\n```") == {"status": "skipped_code", "saved": False}

    def test_old_fenced_record_shown_omitted(self, runner, db, rconn):
        r = db.save_memory("legacy record words")
        rconn.hset("mem:" + r["id"], "content", "legacy record words\n```\nold code\n```")
        text = runner.call("search_memory", {"query": "legacy record words"}, nudge=False)
        assert "old code" not in text and "[code omitted]" in text


class TestSurroundingWhitespace:
    def test_strip(self, db, rconn):
        r = db.save_memory(" \nAAA\n ")
        assert rconn.hget("mem:" + r["id"], "content") == b"AAA"
        assert db.save_memory("AAA")["status"] == "duplicate"

    def test_parent_keeps_inner(self, db, rconn):
        body = "line one\n\nline two " * 30
        r = db.save_memory("  " + body + "\n\n")
        assert rconn.hget("doc:" + r["parent_id"], "content").decode() == body.strip()

    def test_whitespace_only(self, db):
        assert db.save_memory(" \n\t ")["reason"] == "empty content"


class TestModelWait:
    @pytest.mark.real_model
    def test_loading_lock(self, monkeypatch):
        monkeypatch.setattr(P, "MODEL_WAIT_SECONDS", 0.2)
        monkeypatch.setattr(P, "_MODEL", None)
        monkeypatch.setattr(P, "_last_error", None)
        P._MODEL_LOCK.acquire()
        try:
            t = time.monotonic()
            assert P.get_model() is None
            assert time.monotonic() - t < 1.0
            assert "still loading" in P.model_unavailable_message()
            assert P.get_model(no_wait=True) is None
        finally:
            P._MODEL_LOCK.release()

    def test_search_and_save_without_model(self, db, runner, monkeypatch):
        msg = P.loading_message()
        monkeypatch.setattr(P, "embed_many", lambda t, kind="passage", no_wait=False: None)
        monkeypatch.setattr(P, "model_unavailable_message", lambda: msg)
        r = parse_json(runner.call("save_memory", {"content": "saved while loading words"}))
        assert r["saved"] and r["warning"] == "embedding_unavailable" and r["detail"] == msg
        text = runner.call("search_memory", {"query": "saved while loading words"}, nudge=False)
        assert "still loading" in text and "saved while loading words" in text


class TestListPaging:
    def test_single_after_big_document(self, db, cfg):
        db.save_memory("independent before the big document")
        db.save_memory("".join(f"Paragraph {i} of the long document.\n\n" for i in range(2600)))
        rows = db.list_memories(20)
        assert any("independent before" in r["content"] for r in rows)
        assert any(r.get("_doc") for r in rows)


class TestNoTtlRefresh:
    def test_ttl_unchanged(self, db, rconn):
        db.save_memory("no refresh single words")
        db.save_memory("no refresh long sentence. " * 40)
        for k in _data_keys(rconn):
            rconn.expire(k, 5000)
        db.search("no refresh single words")
        db.search("no refresh long sentence")
        for k in _data_keys(rconn):
            assert rconn.ttl(k) <= 5000


class TestOrBm25:
    def test_or(self, db):
        db.save_memory("the quick fox jumped")
        assert db._bm25_search("quick fox elephant", 10)
        assert db._bm25_search("what did the quick fox do", 10)

    def test_cjk_or(self, db):
        db.save_memory("記憶装置のテスト")
        assert db._bm25_search("記憶装置 大阪", 10)


class TestTimeBoundsStrict:
    @pytest.mark.parametrize("v", ["yesterday", "2026/11/13", "2026-13-01"])
    def test_unreadable(self, v):
        from n3mc_mcp.tools import TimeBoundError
        with pytest.raises(TimeBoundError) as e:
            parse_bounds(v, None)
        assert str(e.value).startswith("Error: since must be an ISO 8601 date or datetime") and v in str(e.value)

    def test_since_after_until(self):
        from n3mc_mcp.tools import TimeBoundError
        with pytest.raises(TimeBoundError, match="since is later than until"):
            parse_bounds("2026-11-14", "2026-11-13")

    def test_date_is_local_midnight(self, monkeypatch):
        if not hasattr(time, "tzset"):
            lo, _ = parse_bounds("2026-11-13", None)
            assert lo == datetime(2026, 11, 13).timestamp()
            return
        monkeypatch.setenv("TZ", "Asia/Tokyo")
        time.tzset()
        try:
            lo, _ = parse_bounds("2026-11-13", None)
            assert lo == datetime(2026, 11, 12, 15, tzinfo=timezone.utc).timestamp()
        finally:
            monkeypatch.delenv("TZ")
            time.tzset()

    def test_search_returns_error_text(self, runner):
        text = runner.call("search_memory", {"query": "x", "since": "yesterday"})
        assert text.startswith("Error: since must be") and text.endswith(_NUDGE_GENERIC)


class TestRepairReembed:
    def test_reembed(self, db, rconn):
        r = db.save_memory("needs a vector", embed=False)
        rconn.expire("mem:" + r["id"], 3000)
        out = db.repair()
        assert out["reembedded"] == 1 and out["remaining"] == 0
        assert rconn.hget("mem:" + r["id"], "embedding")
        assert rconn.ttl("mem:" + r["id"]) <= 3000

    def test_batch_of_200(self, db):
        for i in range(205):
            db.save_memory(f"no vector {i}", embed=False)
        out = db.repair()
        assert out["reembedded"] == 200 and out["remaining"] == 5


class TestDeleteOwner:
    def test_other_owner(self, db, cfg, rconn):
        c2 = dict(cfg)
        c2["owner_id"] = "other-owner"
        d2 = D.Database(c2)
        d2.ensure_connected()
        r = d2.save_memory("belongs to someone else")
        assert db.delete_memory(r["id"])["status"] == "not_found"
        assert rconn.exists("mem:" + r["id"])

    def test_chunks_from_chunk_ids(self, db, rconn):
        r = db.save_memory("chunk ids sentence. " * 40)
        db.delete_memory(r["parent_id"])
        assert not any(rconn.exists("mem:" + c) for c in r["ids"])


class TestGuardNx:
    def test_concurrent_same(self, db, cfg):
        results = []

        def go():
            d = D.Database(cfg)
            d.ensure_connected()
            results.append(d.save_memory("concurrent same text")["status"])

        ts = [threading.Thread(target=go) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert results.count("saved") == 1

    def test_failure_after_guard_releases(self, db, rconn, monkeypatch):
        import redis
        real = D.Database._script

        def half(self, name, src):
            if name == "save":
                def run(keys, args):
                    rconn.set(keys[0], args[0], nx=True, ex=60)
                    raise redis.ResponseError("script failed midway")
                return run
            return real(self, name, src)

        monkeypatch.setattr(D.Database, "_script", half)
        with pytest.raises(redis.ResponseError):
            db.save_memory("guard release text")
        assert not list(rconn.scan_iter("mem:sha:*"))


class TestAccessCountNoGhost:
    def test_no_ghost(self, db, rconn, monkeypatch):
        r = db.save_memory("ghost check words")
        real = db._resolve_parents

        def delete_then(rows, docs):
            rconn.delete("mem:" + r["id"])
            for k in rconn.scan_iter("mem:sha:*"):
                rconn.delete(k)
            return real(rows, docs)

        monkeypatch.setattr(db, "_resolve_parents", delete_then)
        db.search("ghost check words")
        assert not _data_keys(rconn)


class TestRecallOrphanChunks:
    def test_parent_then_orphans(self, db, rconn):
        r = db.save_memory("orphan sentence. " * 40, turn_id="T1")
        rows = db.recall_thread("T1")
        assert len(rows) == 1 and rows[0]["tag"].startswith("[doc×")
        rconn.delete("doc:" + r["parent_id"])
        rows = db.recall_thread("T1")
        assert len(rows) == r["chunks"]


class TestDeleteIdPrefix:
    def test_prefixes(self, db, rconn):
        r = db.save_memory("prefix doc sentence. " * 40)
        out = db.delete_memory("doc:" + r["parent_id"])
        assert out["status"] == "deleted" and out["id"] == r["parent_id"]
        s = db.save_memory("prefix single")
        out = db.delete_memory("mem:" + s["id"])
        assert out == {"status": "deleted", "id": s["id"]}
        assert not list(rconn.scan_iter("mem:sha:*"))
        d = db.save_memory("prefix doc two sentence. " * 40)
        assert db.delete_memory("doc:doc:" + d["parent_id"])["status"] == "not_found"


class TestNudgeOnErrors:
    def test_errors_end_with_reminder(self, runner, db, monkeypatch):
        t = runner.call("save_memory", {"content": "x", "importance": "nan"})
        assert t.startswith('{"saved": false, "status": "error", "reason": "importance must be numeric (got nan)"}')
        assert t.endswith(_NUDGE_GENERIC)
        t = runner.call("search_memory", {"query": "x", "since": "bad"})
        assert t.startswith("Error:") and t.endswith(_NUDGE_GENERIC)
        monkeypatch.setattr(db, "list_memories", lambda limit: (_ for _ in ()).throw(ValueError("boom")))
        assert runner.call("list_memories", {}) == "Error: boom" + _NUDGE_GENERIC
        monkeypatch.setattr(db, "ensure_connected", lambda ephemeral=True: "hint text")
        t = runner.call("search_memory", {"query": "x"})
        assert t.startswith("Error: memory backend unreachable. hint text") and t.endswith(_NUDGE_GENERIC)


class TestConditionalWrites:
    def test_scripts(self, db, rconn):
        rconn.hset("mem:cw", "a", 1)
        rconn.expire("mem:cw", 500)
        db._script("incr", D._INCR_IF_EXISTS)(keys=["mem:cw"], args=["access_count", 1])
        db._script("hset", D._HSET_IF_EXISTS)(keys=["mem:cw"], args=["embedding", b"x"])
        assert rconn.ttl("mem:cw") <= 500 and rconn.hget("mem:cw", "access_count") == b"1"
        db._script("incr", D._INCR_IF_EXISTS)(keys=["mem:none"], args=["access_count", 1])
        db._script("hset", D._HSET_IF_EXISTS)(keys=["mem:none"], args=["embedding", b"x"])
        assert not rconn.exists("mem:none")

    def test_repair_after_delete(self, db, rconn, monkeypatch):
        r = db.save_memory("repair vanish", embed=False)
        real = P.embed_many

        def vanish(texts, kind="passage", no_wait=False):
            rconn.delete("mem:" + r["id"])
            return real(texts, kind, no_wait)

        monkeypatch.setattr(P, "embed_many", vanish)
        out = db.repair()
        assert out["reembedded"] == 0 and not rconn.exists("mem:" + r["id"])

    def test_all_keys_have_ttl(self, db, rconn):
        db.save_memory("ttl all one")
        db.save_memory("ttl all long sentence. " * 40)
        db.save_memory("ttl all no vec", embed=False)
        db.search("ttl all one")
        db.repair()
        for k in _data_keys(rconn):
            assert rconn.ttl(k) > 0, k


class TestHangulFiller:
    @pytest.mark.parametrize("ch", ["ㅤ", "ᅟ", "ᅠ", "ﾠ", "⠀"])
    def test_rejected(self, db, ch):
        assert db.save_memory(ch * 3)["reason"] == "empty content (no visible characters)"

    def test_with_visible(self, db, rconn):
        r = db.save_memory("AㅤB")
        assert rconn.hget("mem:" + r["id"], "content").decode() == "AㅤB"


class TestWeakMatches:
    def _setup(self, db, monkeypatch, specs, query="weak query words"):
        qv = fake_vector(query)
        vecs = {c: _vec_with_cos(qv, cos) for c, cos in specs}
        monkeypatch.setattr(P, "embed_many",
                            lambda texts, kind="passage", no_wait=False: [vecs.get(t, qv) for t in texts])
        return {c: db.save_memory(c) for c, _ in specs}

    def test_fill(self, db, cfg, rconn, monkeypatch):
        cfg["lexical_rerank_enabled"] = False
        ids = self._setup(db, monkeypatch, [("wk strong", 0.99), ("wk mid", 0.8), ("wk low", 0.6), ("wk lower", 0.5)])
        normal, weak, _ = db.search("weak query words")
        assert [r["id"] for r in normal] == [ids["wk strong"]["id"]]
        assert len(weak) == 2 and all(r["_weak"] for r in weak)
        assert [r["id"] for r in weak] == [ids["wk mid"]["id"], ids["wk low"]["id"]]
        assert weak[0]["filter_score"] >= weak[1]["filter_score"]
        assert int(rconn.hget("mem:" + ids["wk mid"]["id"], "access_count")) == 0
        assert int(rconn.hget("mem:" + ids["wk strong"]["id"], "access_count")) == 1

    def test_min_results_zero(self, db, cfg, monkeypatch):
        cfg["min_results"] = 0
        self._setup(db, monkeypatch, [("wz strong", 0.99), ("wz mid", 0.8)])
        assert db.search("weak query words")[1] == []

    def test_enough_rows(self, db, cfg, monkeypatch):
        cfg["min_results"] = 1
        self._setup(db, monkeypatch, [("we strong", 0.99), ("we mid", 0.8)])
        assert db.search("weak query words")[1] == []

    def test_limit_caps(self, db, cfg, monkeypatch):
        self._setup(db, monkeypatch, [("wl strong", 0.99), ("wl a", 0.8), ("wl b", 0.7)])
        normal, weak, _ = db.search("weak query words", limit=2)
        assert len(normal) + len(weak) == 2

    def test_one_row_per_parent(self, db, cfg, monkeypatch):
        cfg["lexical_rerank_enabled"] = False
        body = ("parent sentence about rivers. " * 40).strip()
        qv = fake_vector("weak query words")
        chunks = P.chunk_text(body, 400, 100)
        vecs = {c: _vec_with_cos(qv, 0.7) for c in chunks}
        vecs["pw strong"] = _vec_with_cos(qv, 0.99)
        monkeypatch.setattr(P, "embed_many",
                            lambda texts, kind="passage", no_wait=False: [vecs.get(t, qv) for t in texts])
        db.save_memory("pw strong")
        db.save_memory(body)
        normal, weak, _ = db.search("weak query words")
        assert len(weak) == 1 and weak[0]["_doc"]

    def test_tiny_score_not_added(self, db, cfg, monkeypatch):
        cfg["lexical_rerank_enabled"] = False
        self._setup(db, monkeypatch, [("wt strong", 0.99), ("wt zero", 0.0)])
        normal, weak, _ = db.search("weak query words")
        assert all(r["filter_score"] >= 1e-6 for r in weak)
        assert not any("wt zero" == r["content"] for r in weak)

    @pytest.mark.parametrize("q", ["", "   "])
    def test_empty_query(self, db, q):
        db.save_memory("anything at all")
        assert db.search(q)[1] == []

    def test_default(self):
        assert CF.DEFAULTS["min_results"] == 3


class TestGuardReleasedOnAnyFailure:
    @pytest.mark.parametrize("body", ["short failing", "long failing sentence. " * 40])
    def test_non_string_turn_id(self, db, rconn, body):
        r = db.save_memory(body, turn_id=12)
        assert r == {"status": "error", "saved": False, "reason": "turn_id must be a string (got int)"}
        assert not _data_keys(rconn)
        assert db.save_memory(body)["saved"]

    def test_lone_surrogates_removed(self, db, rconn):
        r = db.save_memory("surrogate fields", agent_name="a" + chr(0xD800), session_id="s" + chr(0xDC00),
                           turn_id="t" + chr(0xDFFF))
        h = rconn.hgetall("mem:" + r["id"])
        assert h[b"agent_name"] == b"a" and h[b"session_id"] == b"s" and h[b"turn_id"] == b"t"


class TestPublicRepoHygiene:
    def test_eval_script_no_user_folder(self):
        import re
        p = os.path.join(os.path.dirname(__file__), "eval_uvx_missing.py")
        src = open(p, encoding="utf-8").read()
        assert not re.search(r"[A-Za-z]:\\\\Users\\\\[^<\\\\]+|/home/[a-z]+/", src)

    def test_marketplace_version(self):
        import n3mc_mcp
        p = os.path.join(os.path.dirname(__file__), "..", ".claude-plugin", "marketplace.json")
        assert json.load(open(p, encoding="utf-8"))["metadata"]["version"] == n3mc_mcp.__version__


class TestLocalhostIpv4First:
    """§3.3: host localhost connects to 127.0.0.1 when it answers there (no 2 s IPv6 wait on Windows)."""

    def _db(self, url):
        return D.Database({"redis_url": url, "embedding_model": "x"})

    def test_rewritten_when_ipv4_answers(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(16)
        port = srv.getsockname()[1]
        try:
            assert self._db(f"redis://localhost:{port}/2")._connect_url() == f"redis://127.0.0.1:{port}/2"
            assert self._db(f"redis://u:pw@localhost:{port}/0")._connect_url() == f"redis://u:pw@127.0.0.1:{port}/0"
        finally:
            srv.close()

    def test_kept_when_nothing_on_ipv4(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.close()
        assert self._db(f"redis://localhost:{port}/0")._connect_url() == f"redis://localhost:{port}/0"

    def test_other_hosts_untouched(self):
        assert self._db("redis://example.invalid:6379/0")._connect_url() == "redis://example.invalid:6379/0"

    def test_connect_is_fast(self, rconn):
        url = os.environ["N3MC_REDIS_TEST_URL"].replace("127.0.0.1", "localhost")
        d = self._db(url)
        d.cfg["owner_id"] = "x"
        t = time.monotonic()
        assert d.ensure_connected(ephemeral=False) == "ok"
        assert time.monotonic() - t < 1.0
        d.client.close()
