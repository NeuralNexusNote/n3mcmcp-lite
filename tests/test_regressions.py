"""Layer 4: regression tests (spec §9 Layer 4). Fake embeddings; classes marked (Redis) in the
spec use the `db` / `rconn` fixtures and skip without N3MC_REDIS_TEST_URL."""
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid

import pytest
import redis

import conftest
from n3mc_mcp import __version__, config, context as C, processor as P, tools
from n3mc_mcp.database import DOC, DOCSHA, MEM, SHA, Database

REPO = conftest.REPO_ROOT


def _j(text):
    return json.JSONDecoder().raw_decode(text)[0]


def _cfg(rconn, name):
    v = rconn.config_get(name)
    v = v.get(name, v.get(name.encode()))
    return v.decode() if isinstance(v, bytes) else v


def _keys(rconn):
    return [k.decode() for k in rconn.keys("*") if k != conftest.TEST_MARK.encode()]


# ── a Redis-free Database for the shaping tests ───────────────────────────────

class _FakePipe:
    def __init__(self, r):
        self.r = r
        self.q = []

    def hmget(self, key, fields):
        self.q.append(self.r.hmget(key, fields))

    def execute(self, raise_on_error=True):
        q, self.q = self.q, []
        return q


class _FakeRedis:
    def __init__(self, hashes=None):
        self.h = hashes or {}
        self.incr = []

    def hmget(self, key, fields):
        return [self.h.get(key, {}).get(f) for f in fields]

    def pipeline(self, transaction=False):
        return _FakePipe(self)

    def register_script(self, src):
        def run(keys=None, args=None, client=None):
            if "HINCRBY" in src:
                self.incr.append(keys[0])
            if client is not None:
                client.q.append(0)
            return 0
        return run


def _vec_for(seed):
    return conftest.fake_embed("passage: " + seed)


def shaping_db(cfg, rows):
    """rows: dicts id, cos, bm, content, age_days, session, parent. Returns (db, fake redis)."""
    now = time.time()
    hashes, vec, bm = {}, [], []
    for r in rows:
        ts = now - r.get("age_days", 0) * 86400
        fields = {"content": r.get("content", "x" * 60), "parent_id": r.get("parent", ""),
                  "owner_id": cfg["owner_id"], "session_id": r.get("session", ""), "turn_id": "",
                  "timestamp": "2026-11-26T00:00:00", "timestamp_epoch": str(ts),
                  "importance": "1.0", "access_count": "0", "agent_name": ""}
        if r.get("cos") is not None:
            vec.append(dict(fields, __key=MEM + r["id"], __dist=str(1 - r["cos"])))
        if r.get("bm") is not None:
            bm.append(dict(fields, __key=MEM + r["id"], __score=r["bm"]))
        emb = r.get("emb")
        hashes[MEM + r["id"]] = {"content": fields["content"].encode(),
                                 "embedding": P.pack_vector(emb) if emb else None}
    for pid, doc in (cfg.get("_docs") or {}).items():
        hashes[DOC + pid] = doc
    d = Database(cfg, default_session="cur")
    fake = _FakeRedis(hashes)
    d.client = fake
    d._vector_search = lambda *a, **k: vec
    d._bm25_search = lambda *a, **k: bm
    return d, fake


class TestQueryCleaning:
    def test_strip(self):
        assert P.prepare_query("「記憶装置」は？\x00") == "記憶 憶装 装置 は"

    def test_japanese_query_hits(self, db):
        db.save_memory("記憶装置の容量は 2TB です")
        assert any("記憶装置" in r["content"] for r in db.search("「記憶装置」は？")[0])

    @pytest.mark.parametrize("q", ["\x00\x01", "@#$%^&*()", "_", "「」『』", "a|b", "-", "~~~", "?"])
    def test_no_exception(self, db, q):
        db.save_memory("some record")
        db.search(q)


class TestExactOnlyDedup:
    def test_updated_fact(self, db):
        assert db.save_memory("The deadline is Friday.")["saved"]
        assert db.save_memory("The deadline is Monday.")["saved"]

    def test_remade_document(self, db):
        doc = "Setting item value 10. " * 30
        assert db.save_memory(doc)["saved"]
        assert db.save_memory(doc.replace("10", "11", 3))["saved"]

    def test_exact_rejected(self, db):
        db.save_memory("exactly this")
        assert db.save_memory("exactly this")["status"] == "duplicate"


class TestTimeWindow:
    def test_window_prefilter(self, db, rconn):
        old = db.save_memory("window target alpha record")
        rconn.hset(MEM + old["id"], "timestamp_epoch", time.time() - 10 * 86400)
        for i in range(8):
            db.save_memory(f"window target alpha record newer {i}")
        since = time.strftime("%Y-%m-%d", time.localtime(time.time() - 11 * 86400))
        until = time.strftime("%Y-%m-%d", time.localtime(time.time() - 9 * 86400))
        rows = db.search("window target alpha record", limit=1, since=since, until=until)[0]
        assert [r["id"] for r in rows] == [old["id"]]


class TestShaping:
    def test_vector_floor(self, cfg):
        d, _ = shaping_db(cfg, [{"id": "a", "cos": 0.80}, {"id": "b", "cos": 0.95}])
        cfg["min_results"] = 0
        assert [r["id"] for r in d.search("q")[0]] == ["b"]

    def test_relative_cut(self, cfg):
        cfg["min_results"] = 0
        d, _ = shaping_db(cfg, [{"id": "a", "cos": 1.0, "bm": 10}, {"id": "b", "cos": 0.85}])
        ids = [r["id"] for r in d.search("q")[0]]
        assert ids == ["a"]

    def test_newest_version_first(self, cfg):
        cfg["min_results"] = 0
        e = _vec_for("version doc")
        long = "v" * 120
        d, _ = shaping_db(cfg, [{"id": "old", "cos": 0.99, "content": long, "age_days": 1, "emb": e},
                                {"id": "new", "cos": 0.90, "content": long + "2", "age_days": 0, "emb": e}])
        rows = d.search("q")[0]
        assert rows[0]["id"] == "new" and rows[0]["_version_group"] == rows[1]["_version_group"]
        assert rows[0]["score"] > rows[1]["score"]

    def test_version_group_survives_cut(self, cfg):
        cfg["min_results"] = 0
        e = _vec_for("grouped")
        long = "g" * 120
        d, _ = shaping_db(cfg, [{"id": "new", "cos": 0.99, "bm": 10, "content": long, "emb": e},
                                {"id": "old", "cos": 0.86, "content": long + "x", "age_days": 2, "emb": e}])
        assert {r["id"] for r in d.search("q")[0]} == {"new", "old"}

    def test_short_rows_not_grouped(self, cfg):
        cfg["min_results"] = 0
        e = _vec_for("short")
        d, _ = shaping_db(cfg, [{"id": "a", "cos": 0.99, "content": "s" * 60, "emb": e},
                                {"id": "b", "cos": 0.98, "content": "t" * 60, "emb": e, "age_days": 0}])
        assert not any(r.get("_version_group") for r in d.search("q")[0])


class TestRender:
    def test_saved_local_time(self):
        out = C.render_row({"id": "x", "content": "c", "score": 0.5, "timestamp_epoch": 0})
        assert re.search(r"saved=1970-01-01 \d\d:\d\d \(UTC[+-]\d{4}\)", out)

    def test_version_line_once(self):
        rows = [{"id": "a", "content": "a", "score": 1}, {"id": "b", "content": "b", "score": 1}]
        assert C.render(rows, []).count("newest `saved` time") == 1

    def test_notice_once_and_later(self, runner, cfg):
        cfg["_notices"].clear()
        config.add_notice(cfg, "first notice")
        assert "first notice" in runner.call("search_memory", {"query": "x"}, nudge=False)
        assert "first notice" not in runner.call("search_memory", {"query": "x"}, nudge=False)
        config.add_notice(cfg, "later notice")
        assert "later notice" in runner.call("search_memory", {"query": "x"}, nudge=False)

    def test_weak_after_normal(self):
        out = C.render([{"id": "n", "content": "n"}], [{"id": "w", "content": "w"}])
        assert out.index("[n]") < out.index(C.WEAK_HEADING) < out.index("[w]")

    def test_no_normal_with_weak(self):
        out = C.render([], [{"id": "w", "content": "w"}])
        assert out.startswith(C.NO_MEMORIES) and C.WEAK_HEADING in out and "newest" not in out

    def test_no_weak_no_heading(self):
        assert C.WEAK_HEADING not in C.render([{"id": "n", "content": "n"}], [])


class TestConfig:
    def test_unreadable_unchanged(self, isolate):
        p = isolate["data"] / "config.json"
        p.write_text("{broken", encoding="utf-8")
        cfg = config.load_config()
        assert cfg["_unreadable"] and p.read_text(encoding="utf-8") == "{broken"
        assert any(n.startswith("config_unreadable:") for n in cfg["_notices"])

    def test_wrong_type_ignored(self, isolate):
        conftest.write_test_config(str(isolate["data"]), vector_min_cos="0.83", min_score=0.3)
        cfg = config.load_config()
        assert cfg["vector_min_cos"] == 0.85 and cfg["min_score"] == 0.3
        assert any(n.startswith("config_type:") and "vector_min_cos" in n for n in cfg["_notices"])

    def test_bool_not_number(self):
        assert not config.config_value_ok("min_score", True)
        assert not config.config_value_ok("ttl_seconds", True)


class TestTestSafety:
    def test_refuses_live_url(self):
        assert conftest.refusal_reason("redis://localhost:6379/0", ["redis://localhost:6379/0/"])
        assert conftest.refusal_reason("redis://127.0.0.1:6390/0", ["redis://localhost:6379/0"]) is None

    def test_bom(self, tmp_path):
        p = tmp_path / "config.json"
        p.write_bytes(b"\xef\xbb\xbf" + json.dumps({"redis_url": "redis://x:1/0"}).encode())
        assert conftest.read_live_redis_url(str(p)) == "redis://x:1/0"

    def test_unmarked_db_not_flushed(self):
        class C2:
            flushed = False

            def dbsize(self):
                return 5

            def exists(self, k):
                return 0

            def flushdb(self):
                C2.flushed = True
        with pytest.raises(pytest.skip.Exception):
            conftest.safe_flush(C2())
        assert not C2.flushed


class TestFilterScore:
    def test_old_and_other_session_survive(self, cfg):
        cfg["min_results"] = 0
        rows = [{"id": "new", "cos": 0.86, "session": "cur", "content": "n" * 80},
                {"id": "old3d", "cos": 0.95, "session": "cur", "age_days": 3, "content": "o" * 80},
                {"id": "o99", "cos": 0.99, "session": "other", "age_days": 1, "content": "p" * 80},
                {"id": "o95", "cos": 0.95, "session": "other", "age_days": 1, "content": "q" * 80}]
        d, _ = shaping_db(cfg, rows)
        out = d.search("zzz")[0]
        assert {r["id"] for r in out} == {"new", "old3d", "o99", "o95"}
        assert out[0]["id"] == "new"
        assert [r["score"] for r in out] == sorted([r["score"] for r in out], reverse=True)


class TestRedisTimeouts:
    def test_client_timeouts(self, db):
        kw = db.client.connection_pool.connection_kwargs
        assert kw["socket_timeout"] == 5 and kw["socket_connect_timeout"] == 2

    def test_unresponsive_server(self, cfg):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        conns = []
        t = threading.Thread(target=lambda: conns.append(srv.accept()), daemon=True)
        t.start()
        cfg["redis_url"] = f"redis://127.0.0.1:{srv.getsockname()[1]}/0"
        d = Database(cfg)
        t0 = time.time()
        status = d.ensure_connected()
        assert status != "ok" and "Redis Stack is not reachable" in status
        assert time.time() - t0 < 9
        srv.close()

    def test_reconnect_closes_old(self, db):
        closed = []
        old = db.client
        orig = old.close
        old.close = lambda: (closed.append(1), orig())
        db.mark_down()
        assert closed and db.ensure_connected() == "ok" and db.client is not old


_CREATE = ("import sys; sys.path.insert(0, %r); from n3mc_mcp import config; "
           "c = config.load_config(); print(c['owner_id'], c['local_id'])")


class TestConfigCreate:
    def _run_many(self, data_dir, n):
        env = dict(os.environ, N3MC_DATA_DIR=str(data_dir))
        procs = [subprocess.Popen([sys.executable, "-c", _CREATE % REPO], env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True) for _ in range(n)]
        return [p.communicate(timeout=60)[0].split() for p in procs]

    def test_concurrent_create(self, tmp_path):
        d = tmp_path / "fresh"
        outs = self._run_many(d, 8)
        disk = json.loads((d / "config.json").read_text(encoding="utf-8"))
        assert all(o == [disk["owner_id"], disk["local_id"]] for o in outs)

    def test_concurrent_fill_ids(self, tmp_path):
        d = tmp_path / "noids"
        d.mkdir()
        (d / "config.json").write_text(json.dumps({"vector_min_cos": 0.83}), encoding="utf-8")
        outs = self._run_many(d, 2)
        disk = json.loads((d / "config.json").read_text(encoding="utf-8"))
        assert all(o == [disk["owner_id"], disk["local_id"]] for o in outs)
        assert disk["vector_min_cos"] == 0.83

    def test_update_uses_replace(self, isolate, monkeypatch):
        (isolate["data"] / "config.json").write_text(json.dumps({"owner_id": "o", "local_id": "l"}), encoding="utf-8")
        calls = []
        orig = config.paths.replace_with_retry
        monkeypatch.setattr(config.paths, "replace_with_retry", lambda a, b, *k: (calls.append(b), orig(a, b))[1])
        config.load_config()
        assert calls

    def test_legacy_uses_link(self, isolate, monkeypatch, tmp_path):
        legacy = tmp_path / "legacy" / "config.json"
        legacy.parent.mkdir()
        legacy.write_text(json.dumps({"owner_id": "legacy-owner", "local_id": "l"}), encoding="utf-8")
        monkeypatch.delenv("N3MC_DATA_DIR")
        monkeypatch.setattr(config.paths, "legacy_config_path", lambda: str(legacy))
        links = []
        orig = os.link
        monkeypatch.setattr(os, "link", lambda a, b: (links.append(b), orig(a, b))[1])
        cfg = config.load_config()
        assert cfg["owner_id"] == "legacy-owner" and links
        assert legacy.exists()


class TestConfigRanges:
    @pytest.mark.parametrize("key,value", [("ttl_seconds", 604800.5), ("ttl_seconds", 0), ("embedding_dim", 768.0),
                                           ("chunk_threshold", 10), ("half_life_days", 0), ("min_score", 1.5),
                                           ("min_results", -1), ("min_results", 1.5)])
    def test_rejected(self, isolate, key, value):
        conftest.write_test_config(str(isolate["data"]), **{key: value})
        cfg = config.load_config()
        assert cfg[key] == config.DEFAULTS[key]
        assert any(n.startswith("config_type:") and key in n for n in cfg["_notices"])

    def test_overlap_ge_threshold(self, isolate):
        conftest.write_test_config(str(isolate["data"]), chunk_threshold=100, chunk_overlap=100)
        cfg = config.load_config()
        assert cfg["chunk_overlap"] == 25 and any("chunk_overlap" in n for n in cfg["_notices"])

    def test_only_threshold_lowered(self, isolate):
        # §6: chunk_overlap absent -> its default; not below the threshold -> threshold // 4, with no notice.
        # The filled-in file must not make the next start report a chunk_overlap the user never wrote.
        conftest.write_test_config(str(isolate["data"]), chunk_threshold=100)
        for _ in range(2):
            cfg = config.load_config()
            assert cfg["chunk_overlap"] == 25
            assert not any(n.startswith("config_type:") for n in cfg["_notices"]), cfg["_notices"]
        with open(os.path.join(str(isolate["data"]), "config.json"), encoding="utf-8") as f:
            assert json.load(f)["chunk_overlap"] == 25

    def test_min_results_zero_ok(self):
        assert config.config_value_ok("min_results", 0)

    def test_chunk_terminates(self):
        assert P.chunk_text("x" * 1000, 100, 100)


class TestLongSaveAtomic:
    def test_embed_failure_leaves_nothing(self, db, rconn, monkeypatch):
        body = "atomic long text. " * 40
        calls = {"n": 0}

        def boom(t):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("embed failed")
            return conftest.fake_embed(t)
        monkeypatch.setattr(P, "_FAKE_EMBED", boom)
        with pytest.raises(RuntimeError):
            db.save_memory(body)
        assert not rconn.keys("doc:*") and not rconn.keys("docsha:*")
        monkeypatch.setattr(P, "_FAKE_EMBED", conftest.fake_embed)
        assert db.save_memory(body)["saved"]

    def test_one_script(self, db, monkeypatch):
        used = []
        orig = db._script
        monkeypatch.setattr(db, "_script", lambda name: (used.append(name), orig(name))[1])
        db.save_memory("one script " * 60)
        assert used == ["SAVE_WITH_GUARD"]


class TestGuardScriptAndPortReload:
    @pytest.mark.parametrize("body", ["short guarded body", "long guarded body. " * 40])
    def test_interrupted_send(self, db, rconn, monkeypatch, body):
        orig = db._script

        def failing(name):
            s = orig(name)
            if name != "SAVE_WITH_GUARD":
                return s

            def call(*a, **k):
                raise redis.ConnectionError("cut")
            return call
        monkeypatch.setattr(db, "_script", failing)
        with pytest.raises(redis.ConnectionError):
            db.save_memory(body)
        assert not _keys(rconn)
        monkeypatch.setattr(db, "_script", orig)
        r = db.save_memory(body)
        assert r["saved"]
        for k in _keys(rconn):
            assert rconn.ttl(k) > 0
        assert db.save_memory(body)["status"] == "duplicate"

    def test_release_only_own_guard(self, db, rconn):
        s = db._script("RELEASE_GUARD")
        rconn.set("mem:sha:g1", "other", ex=100)
        s(keys=["mem:sha:g1", "mem:mine"], args=["mine"])
        assert rconn.exists("mem:sha:g1")
        rconn.set("mem:sha:g2", "mine", ex=100)
        s(keys=["mem:sha:g2", "mem:mine"], args=["mine"])
        assert not rconn.exists("mem:sha:g2")

    def test_port_reload(self, isolate, monkeypatch):
        from n3mc_mcp import cli, server
        monkeypatch.setattr(server, "_state", {"cfg": None, "client_name": None})
        monkeypatch.setattr(cli, "ensure_server", lambda cfg, max_wait=60: True)
        seen = []
        monkeypatch.setattr(cli, "_post", lambda cfg, path, body, timeout=30, port=None: (
            seen.append((cfg["server_port"], cfg["owner_id"])), (200, {"text": "ok"}))[1])
        server.forward("list_memories", {})
        conftest.write_test_config(str(isolate["data"]), server_port=conftest.TEST_PORT + 1)
        server.forward("list_memories", {})
        assert seen[1][0] == conftest.TEST_PORT + 1
        (isolate["data"] / "config.json").write_text("{broken", encoding="utf-8")
        server.forward("list_memories", {})
        assert seen[2] == seen[1]


class TestIndexFailure:
    def test_no_redisearch(self, cfg, rconn, monkeypatch):
        calls = []

        def fail(self):
            calls.append(1)
            raise redis.ResponseError("unknown command 'FT.CREATE'")
        monkeypatch.setattr(Database, "_create_index", fail)
        d = Database(cfg)
        status = d.ensure_connected()
        assert "does not provide RediSearch" in status
        out = tools.ToolRunner(cfg, d).call("search_memory", {"query": "x"}, nudge=False)
        assert out.startswith("Error: memory backend unreachable.") and "Redis Stack" in out
        assert len(calls) >= 2
        d.client.close()

    def test_dim_mismatch(self, cfg, rconn):
        rconn.execute_command("FT.CREATE", "n3mc_idx", "ON", "HASH", "PREFIX", 1, "mem:", "SCHEMA",
                              "content_fts", "TEXT", "embedding", "VECTOR", "FLAT", 6, "TYPE", "FLOAT32",
                              "DIM", 384, "DISTANCE_METRIC", "COSINE")
        d = Database(cfg)
        d.ensure_connected()
        assert any(n.startswith("index_dim_mismatch:") for n in cfg["_notices"])
        d.client.close()


class TestIndexFtsField:
    def test_prefix_and_code_not_indexed(self, db, rconn):
        r = db.save_memory("hello there ```x``` end", role="user", turn_id="t1")
        h = rconn.hgetall(MEM + r["id"])
        assert h[b"content"].startswith(b"[user] ")
        assert b"[user]" not in h[b"content_fts"] and b"omitted" not in h[b"content_fts"]
        assert b"user" not in h[b"content_ngram"] and b"omitted" not in h[b"content_ngram"]

    def test_chunk_prefix_not_indexed(self, db, rconn):
        r = db.save_memory("chunk body words. " * 40, role="claude", turn_id="t2")
        for cid in r["ids"]:
            h = rconn.hgetall(MEM + cid)
            assert h[b"content"].startswith(b"[claude ")
            assert b"claude" not in h[b"content_fts"]

    def test_mcp_fence_not_indexed(self, db, rconn):
        r = db.save_memory("before ```code``` after")
        assert b"omitted" not in rconn.hget(MEM + r["id"], "content_fts")

    def test_queries_do_not_hit(self, db):
        db.save_memory("weather report sunny", role="user", turn_id="t3")
        db.save_memory("x ```y``` z words", role="claude", turn_id="t3")
        assert not db._bm25_search("claude", 10, None, None)
        assert not db._bm25_search("omitted", 10, None, None)

    def test_old_index_rebuilt_once(self, db, rconn, monkeypatch):
        r = db.save_memory("legacy index record")
        rconn.execute_command("FT.DROPINDEX", "n3mc_idx")
        rconn.execute_command("FT.CREATE", "n3mc_idx", "ON", "HASH", "PREFIX", 1, "mem:", "SCHEMA",
                              "content", "TEXT", "embedding", "VECTOR", "FLAT", 6, "TYPE", "FLOAT32",
                              "DIM", 768, "DISTANCE_METRIC", "COSINE")
        db.ensure_index()
        names, _ = db.index_info()
        assert "content_fts" in names and rconn.exists(MEM + r["id"])
        drops = []
        orig = db.client.execute_command
        monkeypatch.setattr(db.client, "execute_command",
                            lambda *a, **k: (drops.append(a[0]) if a[0] == "FT.DROPINDEX" else None, orig(*a, **k))[1])
        db.ensure_index()
        assert not drops

    def test_dim_differs_not_rebuilt(self, cfg, rconn):
        rconn.execute_command("FT.CREATE", "n3mc_idx", "ON", "HASH", "PREFIX", 1, "mem:", "SCHEMA",
                              "content", "TEXT", "embedding", "VECTOR", "FLAT", 6, "TYPE", "FLOAT32",
                              "DIM", 384, "DISTANCE_METRIC", "COSINE")
        d = Database(cfg)
        d.ensure_connected()
        names, dim = d.index_info()
        assert dim == 384 and "content_fts" not in names
        assert any(n.startswith("index_dim_mismatch:") for n in cfg["_notices"])
        d.client.close()


class TestEnforceEphemeral:
    def _db(self, cfg):
        d = Database(cfg)
        d.client = d._new_client()
        return d

    def test_empty_db(self, cfg, rconn):
        other = redis.Redis.from_url(conftest.REDIS_TEST_URL.rsplit("/", 1)[0] + "/1")
        other.delete("n3mc_test_other_db")
        rconn.delete(conftest.TEST_MARK)
        try:
            rconn.config_set("save", "3600 1")
            d = self._db(cfg)
            d.enforce_ephemeral()
            assert _cfg(rconn, "save") == ""
            assert not any("shared_redis" in n for n in cfg["_notices"])
        finally:
            rconn.set(conftest.TEST_MARK, 1)
            other.close()

    def test_n3mc_keys_only(self, cfg, rconn):
        rconn.delete(conftest.TEST_MARK)
        try:
            rconn.set("mem:sha:abc", "1", ex=60)
            rconn.config_set("appendonly", "yes")
            self._db(cfg).enforce_ephemeral()
            assert _cfg(rconn, "appendonly") == "no"
        finally:
            rconn.set(conftest.TEST_MARK, 1)

    def test_other_keys(self, cfg, rconn):
        rconn.set("someone_elses_key", "1", ex=60)
        rconn.config_set("save", "3600 1")
        self._db(cfg).enforce_ephemeral()
        assert _cfg(rconn, "save") == "3600 1"
        assert any(n.startswith("shared_redis:") for n in cfg["_notices"])
        rconn.config_set("save", "")

    def test_other_db(self, cfg, rconn):
        other = redis.Redis.from_url(conftest.REDIS_TEST_URL.rsplit("/", 1)[0] + "/1")
        rconn.delete(conftest.TEST_MARK)
        try:
            other.set("n3mc_test_other_db", "1", ex=60)
            self._db(cfg).enforce_ephemeral()
            assert any(n.startswith("shared_redis:") for n in cfg["_notices"])
        finally:
            other.delete("n3mc_test_other_db")
            other.close()
            rconn.set(conftest.TEST_MARK, 1)

    def test_info_refused(self, cfg, rconn, monkeypatch):
        d = self._db(cfg)
        monkeypatch.setattr(d.client, "info", lambda *a: (_ for _ in ()).throw(redis.ResponseError("NOPERM")))
        d.enforce_ephemeral()
        assert any(n.startswith("shared_redis:") for n in cfg["_notices"])

    def test_config_refused(self, cfg, rconn, monkeypatch):
        rconn.delete(conftest.TEST_MARK)
        try:
            d = self._db(cfg)
            monkeypatch.setattr(d.client, "config_set", lambda *a: (_ for _ in ()).throw(redis.ResponseError("unknown command")))
            d.enforce_ephemeral()
            assert any(n.startswith("config_refused:") for n in cfg["_notices"])
        finally:
            rconn.set(conftest.TEST_MARK, 1)


class TestCodeOmittedOnSave:
    def test_short(self, db, rconn):
        r = db.save_memory("see ```print(1)``` and `inline`")
        assert rconn.hget(MEM + r["id"], "content") == b"see [code omitted] and `inline`"

    def test_parent(self, db, rconn):
        r = db.save_memory("start ```secret code``` " + "text " * 120)
        assert b"secret" not in rconn.hget(DOC + r["parent_id"], "content")

    def test_unclosed(self, db, rconn):
        r = db.save_memory("tail ```never closed")
        assert rconn.hget(MEM + r["id"], "content") == b"tail [code omitted]"

    def test_only_code_differs_is_duplicate(self, db):
        db.save_memory("x ```a``` y")
        assert db.save_memory("x ```b``` y")["status"] == "duplicate"

    def test_skip_code_blocks(self, db, cfg):
        cfg["skip_code_blocks"] = True
        assert db.save_memory("x ```a``` y")["status"] == "skipped_code"

    def test_old_fenced_record_shown_omitted(self, db, rconn, runner):
        r = db.save_memory("legacy fenced placeholder record")
        rconn.hset(MEM + r["id"], "content", "legacy fenced ```old code``` record")
        out = runner.call("list_memories", {}, nudge=False)
        assert "old code" not in out and "[code omitted]" in out


class TestSurroundingWhitespace:
    def test_strip(self, db, rconn):
        r = db.save_memory(" \nAAA\n ")
        assert rconn.hget(MEM + r["id"], "content") == b"AAA"

    def test_parent_inner_kept(self, db, rconn):
        body = "\n  " + "line one\n\nline two " * 30 + "  \n"
        r = db.save_memory(body)
        assert rconn.hget(DOC + r["parent_id"], "content").decode() == body.strip()

    def test_whitespace_only_differs(self, db):
        db.save_memory("BBB")
        assert db.save_memory("  BBB\n")["status"] == "duplicate"

    def test_blank(self, db):
        assert db.save_memory(" \n\t ")["reason"] == "empty content"


class TestModelWait:
    def test_keyword_only_while_loading(self, db, cfg, monkeypatch):
        db.save_memory("loading model keyword record")
        monkeypatch.setattr(P, "_FAKE_EMBED", None)
        monkeypatch.setattr(P, "_model", None)
        monkeypatch.setattr(P, "MODEL_WAIT_SECONDS", 0.2)
        P._lock.acquire()
        monkeypatch.setattr(P, "_loading", True)
        try:
            out = tools.ToolRunner(cfg, db).call("search_memory", {"query": "loading model keyword"}, nudge=False)
            assert "is still loading" in out and "loading model keyword record" in out
            r = db.save_memory("saved while loading")
            assert r["saved"] and r["warning"] == "embedding_unavailable" and "still loading" in r["detail"]
        finally:
            P._lock.release()


class TestListPaging:
    def test_single_and_big_doc(self, db, runner):
        db.save_memory("independent memory before the big document")
        r = db.save_memory(" ".join(f"word{i}" for i in range(14000)))
        assert r["chunks"] >= 250
        out = runner.call("list_memories", {"limit": 20}, nudge=False)
        assert "independent memory" in out and f"[doc×{r['chunks']}]" in out


class TestNoTtlRefresh:
    def test_ttls_unchanged(self, db, rconn):
        db.save_memory("ttl single record words")
        db.save_memory("ttl long record words. " * 40)
        for k in _keys(rconn):
            rconn.expire(k, 500)
        db.search("ttl single record words")
        db.search("ttl long record words")
        for k in _keys(rconn):
            assert rconn.ttl(k) <= 500


class TestOrBm25:
    def test_or_terms(self, db):
        db.save_memory("the quick fox jumped")
        for q in ("quick fox elephant", "what did the quick fox do"):
            assert db._bm25_search(q, 10, None, None)

    def test_japanese_or(self, db):
        db.save_memory("記憶装置の設計")
        assert db._bm25_search("記憶装置 大阪", 10, None, None)

    def test_coverage_punctuation(self):
        assert P.token_set("note,") == {"note"}


class TestTimeBoundsStrict:
    @pytest.mark.parametrize("v", ["yesterday", "2026/11/13", "2026-13-01"])
    def test_unreadable(self, v):
        with pytest.raises(P.TimeBoundError) as e:
            P.parse_time_bounds(v, None)
        assert str(e.value).startswith("Error: since must be")

    def test_since_after_until(self):
        with pytest.raises(P.TimeBoundError, match="since is later than until"):
            P.parse_time_bounds("2026-11-14", "2026-11-13")

    def test_date_local_midnight(self):
        import datetime
        want = datetime.datetime(2026, 11, 13).astimezone().timestamp()
        assert P.parse_time_bound("2026-11-13", "since", False) == want

    def test_tool_error(self, runner):
        out = runner.call("search_memory", {"query": "x", "since": "yesterday"}, nudge=False)
        assert out.startswith("Error: since must be")


class TestRepairReembed:
    def test_reembed(self, db, rconn):
        ids = [db.save_memory(f"needs vector {i}", embed=False)["id"] for i in range(3)]
        for i in ids:
            rconn.expire(MEM + i, 1000)
        res = db.repair()
        assert res["reembedded"] == 3 and res["remaining"] == 0
        for i in ids:
            assert rconn.hget(MEM + i, "embedding") and rconn.ttl(MEM + i) <= 1000

    def test_limit_200(self, db, rconn):
        pipe = rconn.pipeline()
        for i in range(205):
            pipe.hset(f"mem:bulk{i}", mapping={"id": f"bulk{i}", "content": f"bulk {i}", "owner_id": db.cfg["owner_id"],
                                               "parent_id": "", "timestamp_epoch": 1})
            pipe.expire(f"mem:bulk{i}", 600)
        pipe.execute()
        res = db.repair()
        assert res["reembedded"] == 200 and res["remaining"] == 5


class TestDeleteOwner:
    def test_other_owner(self, db, cfg, rconn):
        other = dict(cfg, owner_id="22222222-2222-2222-2222-222222222222")
        d2 = Database(other)
        d2.ensure_connected()
        r = d2.save_memory("other owner record")
        assert db.delete_memory(r["id"])["status"] == "not_found"
        assert rconn.exists(MEM + r["id"])
        d2.client.close()

    def test_chunks_from_chunk_ids(self, db, rconn):
        r = db.save_memory("chunk ids words. " * 50)
        db.delete_memory(r["parent_id"])
        assert not any(rconn.exists(MEM + c) for c in r["ids"])


class TestGuardNx:
    def test_concurrent_same(self, cfg, rconn):
        results = []

        def go():
            d = Database(cfg)
            d.ensure_connected()
            results.append(d.save_memory("raced content"))
            d.client.close()
        ts = [threading.Thread(target=go) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert sum(1 for r in results if r["saved"]) == 1
        assert len([k for k in _keys(rconn) if not k.startswith("mem:sha:")]) == 1


class TestAccessCountNoGhost:
    def test_deleted_between(self, db, rconn, monkeypatch):
        r = db.save_memory("ghost check record text")
        orig = P.lexical_bonus

        def vanish(*a, **k):
            rconn.delete(MEM + r["id"], SHA + P.content_hash("ghost check record text"))
            return orig(*a, **k)
        monkeypatch.setattr(P, "lexical_bonus", vanish)
        db.search("ghost check record text")
        assert _keys(rconn) == []


class TestRecallOrphanChunks:
    def test_parent_once_then_orphans(self, db, rconn):
        r = db.save_memory("orphan chunk words. " * 40, turn_id="T1")
        out = db.recall_thread("T1")
        assert out.count("[doc×") == 1 and r["parent_id"] in out
        rconn.delete(DOC + r["parent_id"])
        out = db.recall_thread("T1")
        assert isinstance(out, str) and all(c in out for c in r["ids"])


class TestDeleteIdPrefix:
    def test_doc_prefix(self, db, rconn):
        r = db.save_memory("prefixed doc words. " * 40)
        res = db.delete_memory("doc:" + r["parent_id"])
        assert res["status"] == "deleted" and res["id"] == r["parent_id"]

    def test_mem_prefix(self, db, rconn):
        r = db.save_memory("prefixed single")
        assert db.delete_memory("mem:" + r["id"])["id"] == r["id"]
        assert not rconn.exists(SHA + P.content_hash("prefixed single"))

    def test_double_prefix(self, db):
        r = db.save_memory("double prefixed words. " * 40)
        assert db.delete_memory("doc:doc:" + r["parent_id"])["status"] == "not_found"


class TestNudgeOnErrors:
    def test_all(self, runner, monkeypatch):
        g = tools._NUDGE_GENERIC
        out = runner.call("save_memory", {"content": "x", "importance": "nan"})
        assert out.startswith('{"saved": false') and out.endswith(g)
        out = runner.call("search_memory", {"query": "x", "since": "bad"})
        assert out.startswith("Error:") and out.endswith(g)
        monkeypatch.setattr(runner.db, "list_memories", lambda *a: 1 / 0)
        out = runner.call("list_memories", {})
        assert out == "Error: division by zero" + g      # §4.4: the message, as in 1.x
        monkeypatch.setattr(runner.db, "ensure_connected", lambda *a, **k: "hint text")
        out = runner.call("search_memory", {"query": "x"})
        assert out.startswith("Error: memory backend unreachable.") and out.endswith(g)


class TestConditionalWrites:
    def test_scripts(self, db, rconn):
        rconn.hset("mem:cw", "a", 1)
        rconn.expire("mem:cw", 300)
        db._script("INCR_IF_EXISTS")(keys=["mem:cw"], args=["access_count", 1])
        db._script("HSET_IF_EXISTS")(keys=["mem:cw"], args=["embedding", b"x"])
        assert rconn.ttl("mem:cw") > 0 and rconn.hget("mem:cw", "access_count") == b"1"
        assert db._script("INCR_IF_EXISTS")(keys=["mem:none"], args=["access_count", 1]) is None
        assert db._script("HSET_IF_EXISTS")(keys=["mem:none"], args=["embedding", b"x"]) == 0
        assert not rconn.exists("mem:none")

    def test_repair_after_vanish(self, db, rconn, monkeypatch):
        r = db.save_memory("vanishing before repair", embed=False)
        orig = P.embed_many

        def vanish(*a, **k):
            rconn.delete(MEM + r["id"])
            return orig(*a, **k)
        monkeypatch.setattr(P, "embed_many", vanish)
        assert db.repair()["reembedded"] == 0
        assert not rconn.exists(MEM + r["id"])

    def test_all_keys_have_ttl(self, db, rconn, runner):
        db.save_memory("ttl everywhere")
        db.save_memory("ttl everywhere long. " * 40, role="user", turn_id="t")
        runner.call("search_memory", {"query": "ttl everywhere"}, nudge=False)
        db.repair()
        for k in _keys(rconn):
            assert rconn.ttl(k) > 0, k


class TestHangulFiller:
    @pytest.mark.parametrize("ch", ["\u3164", "\u115f", "\u1160", "\uffa0", "\u2800"])
    def test_rejected(self, ch):
        d = Database({"owner_id": "o"})
        assert d.save_memory(ch * 3)["reason"] == "empty content (no visible characters)"

    def test_with_visible(self, db, rconn):
        r = db.save_memory("A\u3164B")
        assert rconn.hget(MEM + r["id"], "content").decode() == "A\u3164B"


def _load_install_permissions():
    path = os.path.join(REPO, "plugins", "n3mc-workingmemory", "hooks", "install_permissions.py")
    spec = importlib.util.spec_from_file_location("install_permissions_t", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestPluginHook:
    def test_rules_and_atomic(self, isolate, monkeypatch, capsys):
        ip = _load_install_permissions()
        s = isolate["home"] / ".claude" / "settings.json"
        s.parent.mkdir()
        s.write_text(json.dumps({"model": "x", "permissions": {"allow": ["Other"]}}), encoding="utf-8")
        replaced = []
        orig = ip.os.replace
        monkeypatch.setattr(ip.os, "replace", lambda a, b: (replaced.append(b), orig(a, b))[1])
        ip.main([])
        data = json.loads(s.read_text(encoding="utf-8"))
        allow = data["permissions"]["allow"]
        assert data["model"] == "x" and "Other" in allow and replaced
        assert f"Bash(uvx --from n3memorycore-mcp-lite=={__version__} n3mc-lite --search *)" in allow
        assert "mcp__plugin_n3mc-workingmemory_n3mc-workingmemory__search_memory" in allow
        assert not any("delete" in r for r in allow)

    def test_mcp_mode(self, isolate):
        ip = _load_install_permissions()
        ip.main(["--mcp"])
        allow = json.loads((isolate["home"] / ".claude" / "settings.json").read_text(encoding="utf-8"))["permissions"]["allow"]
        assert allow == [f"mcp__plugin_n3mc-workingmemory-mcp_n3mc-workingmemory__{t}" for t in ip.MCP_TOOLS]

    def test_fallback_text(self):
        for plugin in ("n3mc-workingmemory", "n3mc-workingmemory-mcp"):
            with open(os.path.join(REPO, "plugins", plugin, "hooks", "hooks.json"), encoding="utf-8") as f:
                assert "or the permission script failed" in f.read()

    def test_no_uvx(self, isolate, monkeypatch, capsys):
        ip = _load_install_permissions()
        monkeypatch.setattr(ip.shutil, "which", lambda n: None)
        ip.main([])
        out = json.loads(capsys.readouterr().out)
        assert ip.uvx_message(False) in out["systemMessage"]
        ip.main(["--mcp"])
        out = json.loads(capsys.readouterr().out)
        assert ip.uvx_message(True) in out["systemMessage"]


class TestWeakMatches:
    def _rows(self):
        return [{"id": "top", "cos": 0.99, "bm": 10, "content": "a" * 80},
                {"id": "w1", "cos": 0.84, "content": "b" * 80},
                {"id": "w2", "cos": 0.83, "content": "c" * 80},
                {"id": "w3", "cos": 0.82, "content": "d" * 80}]

    def test_fill_two(self, cfg):
        d, fake = shaping_db(cfg, self._rows())
        normal, weak, _ = d.search("query words")
        assert [r["id"] for r in normal] == ["top"] and [r["id"] for r in weak] == ["w1", "w2"]
        assert all(r["_weak"] for r in weak)
        assert fake.incr == ["mem:top"]

    def test_min_results_zero(self, cfg):
        cfg["min_results"] = 0
        d, _ = shaping_db(cfg, self._rows())
        assert d.search("query words")[1] == []

    def test_enough_rows(self, cfg):
        rows = [{"id": f"n{i}", "cos": 0.95, "content": str(i) * 80} for i in range(3)] + self._rows()[1:]
        d, _ = shaping_db(cfg, rows)
        assert d.search("query words")[1] == []

    def test_limit_smaller(self, cfg):
        d, _ = shaping_db(cfg, self._rows())
        normal, weak, _ = d.search("query words", limit=2)
        assert len(normal) + len(weak) == 2

    def test_one_per_parent(self, cfg):
        cfg["_docs"] = {"P1": {"content": b"parent body " * 10, "chunk_count": b"2", "chunk_ids": b"c1 c2",
                               "turn_id": b"", "timestamp": b"t", "timestamp_epoch": str(time.time()).encode(),
                               "owner_id": cfg["owner_id"].encode(), "session_id": b""}}
        rows = self._rows()[:1] + [{"id": "c1", "cos": 0.84, "parent": "P1"}, {"id": "c2", "cos": 0.83, "parent": "P1"}]
        d, _ = shaping_db(cfg, rows)
        weak = d.search("query words")[1]
        assert [r["id"] for r in weak] == ["P1"]

    def test_tiny_scores_skipped(self, cfg):
        d, _ = shaping_db(cfg, self._rows()[:1] + [{"id": "z", "cos": 0.0000001}])
        assert d.search("query words")[1] == []

    def test_empty_query(self, cfg):
        d, _ = shaping_db(cfg, self._rows())
        assert d.search("   ")[1] == []

    def test_default(self):
        assert config.DEFAULTS["min_results"] == 3

    def test_weak_access_count_unchanged(self, db, rconn, cfg):
        cfg["vector_min_cos"] = 0.999
        a = db.save_memory("alpha beta gamma delta")
        b = db.save_memory("totally different words here")
        normal, weak, _ = db.search("alpha beta gamma delta")
        assert any(r["id"] == b["id"] for r in weak)
        assert rconn.hget(MEM + b["id"], "access_count") == b"0"
        assert rconn.hget(MEM + a["id"], "access_count") == b"1"


class TestGuardReleasedOnAnyFailure:
    @pytest.mark.parametrize("body", ["guard fail short", "guard fail long text. " * 40])
    def test_non_string_turn_id(self, db, rconn, body):
        assert db.save_memory(body, turn_id=12)["status"] == "error"
        assert not _keys(rconn)
        assert db.save_memory(body)["saved"]

    def test_surrogates_removed(self, db, rconn):
        r = db.save_memory("surrogate fields", agent_name="ag\ud800", session_id="s\udc00", turn_id="t\ud801")
        h = rconn.hgetall(MEM + r["id"])
        assert h[b"agent_name"] == b"ag" and h[b"session_id"] == b"s" and h[b"turn_id"] == b"t"

    def test_clean_fields_unchanged(self, db, rconn):
        r = db.save_memory("clean fields", agent_name="agent", session_id="sess", turn_id="turn")
        assert rconn.hget(MEM + r["id"], "agent_name") == b"agent"


class TestPluginHookAsciiStdout:
    def test_ascii_line(self, isolate):
        script = os.path.join(REPO, "plugins", "n3mc-workingmemory", "hooks", "install_permissions.py")
        env = dict(os.environ, PATH=os.path.dirname(sys.executable))
        p = subprocess.run([sys.executable, script], env=env, capture_output=True, timeout=60)
        out = p.stdout.decode("ascii")
        assert p.returncode == 0 and out.count("\n") == 1
        msg = json.loads(out)["systemMessage"]
        assert "uvx が PATH 上に見つかりません" in msg


class TestPublicRepoHygiene:
    def test_eval_uvx_no_user_folder(self):
        with open(os.path.join(REPO, "tests", "eval_uvx_missing.py"), encoding="utf-8") as f:
            text = f.read()
        assert not re.search(r"[A-Za-z]:\\\\?Users\\\\?\w", text) and "/home/" not in text

    def test_versions(self):
        with open(os.path.join(REPO, ".claude-plugin", "marketplace.json"), encoding="utf-8") as f:
            m = json.load(f)
        assert m["metadata"]["version"] == __version__
        assert [p["name"] for p in m["plugins"]] == ["n3mc-workingmemory-mcp", "n3mc-workingmemory"]
        for name in ("n3mc-workingmemory", "n3mc-workingmemory-mcp"):
            with open(os.path.join(REPO, "plugins", name, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
                assert json.load(f)["version"] == __version__
