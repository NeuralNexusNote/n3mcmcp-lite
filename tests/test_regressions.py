"""Layer 4: regression tests for the v1.7.0 fixes (spec §9).

Embeddings are deterministic fakes (no model). Redis classes use the `db`
fixture, i.e. ONLY the dedicated N3MC_REDIS_TEST_URL instance.
"""
import json
import math
import random
import zlib
from datetime import datetime, timezone

import numpy as np
import pytest

from tests.conftest import requires_redis, REDIS_TEST_URL, _norm_url, _live_redis_urls
from n3mc_mcp import processor as P
from n3mc_mcp import config as C

DIM = 768


# ── fake embeddings ──────────────────────────────────────────────────────────

def _unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def fake_vec(text: str) -> np.ndarray:
    rnd = random.Random(zlib.crc32(text.encode("utf-8")))
    return _unit([rnd.gauss(0, 1) for _ in range(DIM)])


def axis(i: int) -> np.ndarray:
    v = np.zeros(DIM, dtype=np.float32)
    v[i] = 1.0
    return v


def at_cos(c: float, other: int = 1) -> np.ndarray:
    v = np.zeros(DIM, dtype=np.float32)
    v[0] = c
    v[other] = math.sqrt(max(0.0, 1 - c * c))
    return v


@pytest.fixture
def fake_embed(monkeypatch):
    """database.embed(text, is_query) -> FLOAT32 bytes from a lookup table,
    falling back to a crc32-seeded random unit vector."""
    import n3mc_mcp.database as D
    table: dict = {}

    def embed(text, is_query=False):
        for key, vec in table.items():
            if key in text:
                return np.asarray(vec, dtype=np.float32).tobytes()
        return fake_vec(text).tobytes()

    monkeypatch.setattr(D, "embed", embed)
    return table


# ── §3.7 query cleaning ──────────────────────────────────────────────────────

class TestQueryCleaning:
    def test_control_chars_and_cjk_punct_removed(self):
        q = P.prepare_query("「記憶装置」は？\x00の\x1f説明")
        for ch in "「」？\x00\x1f":
            assert ch not in q
        assert "記憶" in q and "装置" in q

    def test_fullwidth_symbols_normalized_and_stripped(self):
        assert P.prepare_query("ＡＩ＃タグ＝値") .replace(" ", "").startswith("AI")
        assert "#" not in P.prepare_query("ＡＩ＃タグ") and "=" not in P.prepare_query("Ａ＝Ｂ")

    def test_lone_cjk_tokens_dropped_only_with_others(self):
        assert P.drop_lone_cjk_tokens("記憶 憶装 装置 は") == "記憶 憶装 装置"
        assert P.drop_lone_cjk_tokens("は") == "は"

    def test_ngram_uses_same_cleaning(self):
        assert P.content_ngram("「記憶装置」") == P.prepare_query("「記憶装置」")


@requires_redis
class TestQueryCleaningRedis:
    QUERIES = [r"C:\Users\test #tag = @a * $b % 100", "AND OR NOT", "tab\there\x00nul\x1fend",
               "＃タグ ＝ 値", "-foo ~bar foo|bar %fuzzy% ism* {tag} $var @field:x", "\\", "_",
               "「記憶装置」は？", "（テスト）、。！"]

    def test_no_query_raises(self, db, fake_embed, capsys):
        db.save_memory("設定ファイルの記憶装置 retries=5 under_score")
        for q in self.QUERIES:
            db.search_memory(q)
        err = capsys.readouterr().err
        assert "BM25 search" not in err and "Syntax error" not in err, err

    def test_japanese_question_with_punctuation_hits_bm25(self, db, fake_embed):
        db.save_memory("設定ファイルの記憶装置について")
        assert db._bm25_search("「記憶装置」は？", db.cfg["owner_id"], 20)


# ── §3.8 exact-only dedup ────────────────────────────────────────────────────

@requires_redis
class TestExactOnlyDedup:
    def test_updated_short_fact_is_saved(self, db, fake_embed):
        fake_embed["budget"] = axis(3)  # both versions get the SAME vector
        r1 = db.save_memory("The project budget is 100 dollars.")
        r2 = db.save_memory("The project budget is 120 dollars.")
        assert r1["saved"] and r2["saved"], r2

    def test_remade_long_document_is_saved(self, db, fake_embed):
        fake_embed["設定シート"] = axis(4)
        base = "設定シート R号\n" + "\n".join(f"項目{i:02d}: 値は{i * 7}です。" * 3 for i in range(12))
        r1 = db.save_memory(base)
        r2 = db.save_memory(base.replace("値は14です", "値は15です"))
        assert r1["saved"] and r2["saved"], r2
        assert r1["parent_id"] != r2["parent_id"]

    def test_exact_duplicates_still_rejected(self, db, fake_embed):
        db.save_memory("same fact")
        assert db.save_memory("same fact")["status"] == "duplicate"
        long = "長い本文。" * 120
        db.save_memory(long)
        assert db.save_memory(long)["status"] == "duplicate"


# ── §4.3.1 time window pre-filter ────────────────────────────────────────────

@requires_redis
class TestTimeWindow:
    def test_in_range_rows_found_beyond_global_knn(self, db, fake_embed, monkeypatch):
        import n3mc_mcp.database as D
        fake_embed["NEWROW"] = at_cos(0.999)
        fake_embed["OLDROW"] = at_cos(0.95, other=2)
        fake_embed["QUERY"] = axis(0)
        monkeypatch.setattr(D, "_now_iso", lambda: "2025-01-01T00:00:00+00:00")
        monkeypatch.setattr(D, "_now_epoch", lambda: 1735689600.0)
        for i in range(3):
            db.save_memory(f"OLDROW entry {i}")
        monkeypatch.setattr(D, "_now_iso", lambda: "2026-06-01T00:00:00+00:00")
        monkeypatch.setattr(D, "_now_epoch", lambda: 1780272000.0)
        for i in range(30):  # > limit * 5 for limit=3 → would crowd out the old rows
            db.save_memory(f"NEWROW entry {i}")
        res = db.search_memory("QUERY", limit=3, until="2025-12-31")
        assert sorted(r["content"] for r in res) == [f"OLDROW entry {i}" for i in range(3)]

    def test_clause(self):
        from n3mc_mcp.database import _time_filter_clause
        assert _time_filter_clause("", "") == ""
        assert "-inf" in _time_filter_clause("", "2026-01-01")
        assert "+inf" in _time_filter_clause("2026-01-01", "")


# ── §3.14 shaping (pure) ─────────────────────────────────────────────────────

def _row(i, score, ts, content="x" * 150):
    return {"id": f"id{i}", "score": score, "timestamp_epoch": ts, "content": content}


class TestShaping:
    def test_vector_floor(self):
        res = P.apply_vector_floor({"a": {"cos_sim": 0.90}, "b": {"cos_sim": 0.84}}, 0.85)
        assert list(res) == ["a"]
        assert len(P.apply_vector_floor({"b": {"cos_sim": 0.1}}, 0.0)) == 1

    def test_relative_cut(self):
        rows = [_row(1, 1.0, 1), _row(2, 0.5, 2), _row(3, 0.7, 3)]
        assert [r["id"] for r in P.relative_cut(rows, 0.6)] == ["id1", "id3"]

    def test_newest_version_moves_up_and_keeps_slot_score(self):
        rows = [_row("old", 0.9, 100), _row("other", 0.8, 300), _row("new", 0.7, 200)]
        vecs = [axis(0), axis(5), at_cos(0.99)]
        out = P.order_versions_newest_first(rows, vecs, {"version_sim_threshold": 0.98,
                                                         "version_min_chars": 100})
        assert [r["id"] for r in out] == ["idnew", "idother", "idold"]
        assert [r["score"] for r in out] == [0.9, 0.8, 0.7]
        assert out[0]["_version_group"] and out[2]["_version_group"]
        assert not out[1].get("_version_group")

    def test_group_marked_even_when_newest_already_first(self):
        rows = [_row("new", 0.9, 200), _row("x", 0.3, 50), _row("old", 0.2, 100)]
        out = P.order_versions_newest_first(rows, [axis(0), axis(7), axis(0)],
                                            {"version_sim_threshold": 0.98, "version_min_chars": 100})
        assert [r["id"] for r in out] == ["idnew", "idx", "idold"]
        kept = P.relative_cut(out, 0.6)
        assert [r["id"] for r in kept] == ["idnew", "idold"]

    def test_short_rows_not_grouped(self):
        rows = [_row("a", 0.9, 100, "short"), _row("b", 0.8, 200, "short too")]
        out = P.order_versions_newest_first(rows, [axis(0), axis(0)],
                                            {"version_sim_threshold": 0.98, "version_min_chars": 100})
        assert [r["id"] for r in out] == ["ida", "idb"]
        assert not any(r.get("_version_group") for r in out)


@requires_redis
class TestShapingRedis:
    def test_search_lists_newest_version_first(self, db, fake_embed):
        db.cfg.update(version_order_enabled=True, vector_min_cos=0.85,
                      relative_min_score_ratio=0.6)
        fake_embed["QUERY"] = axis(0)
        fake_embed["setting sheet v1"] = axis(0)
        fake_embed["setting sheet v2"] = at_cos(0.99)
        db.save_memory("setting sheet v1 " + "a" * 150)
        db.save_memory("setting sheet v2 " + "b" * 150)
        res = db.search_memory("QUERY")
        assert res[0]["content"].startswith("setting sheet v2")
        assert len(res) == 2


# ── §3.14 render / notices ───────────────────────────────────────────────────

class TestRender:
    def test_saved_local_time_and_version_sentence(self):
        from n3mc_mcp import server as S
        ts = "2026-01-01T00:00:00+00:00"
        local = datetime.fromisoformat(ts).astimezone()
        md = S._render_search([{"id": "x", "content": "c", "score": 0.5, "timestamp": ts,
                                "turn_id": "t1"}], ["config_type: vector_min_cos"])
        assert f"saved={local:%Y-%m-%d %H:%M} (UTC{local:%z})" in md
        assert "newest `saved` time is the current one" in md
        assert "turn=t1" in md
        assert md.index("N3MC notice: config_type") < md.index("### ")

    def test_notices_only_once(self, monkeypatch):
        from n3mc_mcp import server as S

        class FakeDb:
            cfg = {"_notices": ["config_unreadable: x"]}
        monkeypatch.setattr(S, "_db", FakeDb())
        monkeypatch.setattr(S, "_notices_shown", False)
        assert S._take_notices() == ["config_unreadable: x"]
        assert S._take_notices() == []


# ── §6 config ────────────────────────────────────────────────────────────────

@pytest.fixture
def cfg_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("N3MC_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("N3MC_REDIS_URL", raising=False)
    return tmp_path / "config.json"


class TestConfig:
    def test_unreadable_not_modified(self, cfg_dir):
        cfg_dir.write_text('{"ttl_seconds": 60,,}', encoding="utf-8")
        cfg = C.load_config()
        assert cfg_dir.read_text(encoding="utf-8") == '{"ttl_seconds": 60,,}'
        assert cfg["ttl_seconds"] == C.DEFAULTS["ttl_seconds"]
        assert any(n.startswith("config_unreadable") for n in cfg["_notices"])

    def test_wrong_type_item_ignored(self, cfg_dir):
        text = json.dumps({"owner_id": "o", "local_id": "l", "ttl_seconds": 60,
                           "vector_min_cos": "0.83"})
        cfg_dir.write_text(text, encoding="utf-8")
        cfg = C.load_config()
        assert cfg["ttl_seconds"] == 60
        assert cfg["vector_min_cos"] == 0.85
        assert cfg_dir.read_text(encoding="utf-8") == text
        assert any("vector_min_cos" in n for n in cfg["_notices"])

    def test_type_rules(self):
        assert not C.config_value_ok("search_result_limit", True)
        assert C.config_value_ok("vector_min_cos", 1)
        assert not C.config_value_ok("lexical_rerank_enabled", 1)
        assert C.config_value_ok("owner_id", 123)

    def test_removed_keys(self):
        assert "dedup_threshold" not in C.DEFAULTS
        assert "context_char_limit" not in C.DEFAULTS


# ── §9 test safety ───────────────────────────────────────────────────────────

class TestTestSafety:
    def test_live_default_is_refused(self):
        assert _norm_url("redis://localhost:6379/0") in _live_redis_urls()

    def test_current_test_url_is_not_live(self):
        if REDIS_TEST_URL:
            assert _norm_url(REDIS_TEST_URL) not in _live_redis_urls()


# ── §3.9 Redis availability on every tool call (A1 fixes) ────────────────────

BAD_URL = "redis://127.0.0.1:1/0"  # nothing listens here


def _call(name, args):
    import asyncio
    from n3mc_mcp.server import call_tool
    return asyncio.run(call_tool(name, args))[0].text


class TestRedisAvailability:
    def test_hint_is_english_without_password(self):
        from n3mc_mcp.database import backend_hint
        h = backend_hint("redis://user:s3cret@myhost:6390/0")
        assert "myhost:6390" in h and "s3cret" not in h and "user" not in h
        assert "docker start redis-stack" in h and "no restart needed" in h
        assert not any("\u3040" <= ch <= "\u9fff" for ch in h)  # no Japanese

    def test_down_at_startup_every_tool_returns_hint(self, cfg, monkeypatch):
        from n3mc_mcp.database import Database
        import n3mc_mcp.server as S
        cfg = dict(cfg, redis_url=BAD_URL)
        d = Database(cfg)
        assert d.connect(quiet=True) is False
        monkeypatch.setattr(S, "_db", d)
        assert _call("search_memory", {"query": "x"}).startswith("Error: memory backend unreachable.")
        assert _call("list_memories", {}).startswith("Error: memory backend unreachable.")
        assert _call("recall_thread", {"turn_id": "t"}).startswith("Error: memory backend unreachable.")
        save = _parse_first_json(_call("save_memory", {"content": "x"}))
        assert save["status"] == "error" and save["saved"] is False and "127.0.0.1:1" in save["reason"]
        assert _parse_first_json(_call("repair_memory", {}))["status"] == "error"
        assert _parse_first_json(_call("delete_memory", {"id": "x"}))["status"] == "error"


def _parse_first_json(text):
    return json.JSONDecoder().raw_decode(text)[0]


@requires_redis
class TestRedisAvailabilityLive:
    def test_started_down_then_redis_comes_up(self, cfg, redis_client, fake_embed, monkeypatch):
        """Server started while Redis was down recovers WITHOUT a restart."""
        from n3mc_mcp.database import Database
        import n3mc_mcp.server as S
        d = Database(dict(cfg, redis_url=BAD_URL))
        assert d.connect(quiet=True) is False
        monkeypatch.setattr(S, "_db", d)
        assert _call("search_memory", {"query": "x"}).startswith("Error:")
        d.cfg["redis_url"] = REDIS_TEST_URL          # "docker start redis-stack"
        save = _parse_first_json(_call("save_memory", {"content": "after recovery"}))
        assert save["saved"] is True                 # reconnected + index re-created
        assert "after recovery" in _call("search_memory", {"query": "after recovery"})

    def test_redis_lost_while_running_is_an_error_not_empty(self, db, fake_embed, monkeypatch):
        import redis
        import n3mc_mcp.server as S
        monkeypatch.setattr(S, "_db", db)
        db.save_memory("saved before the outage")
        db.cfg["redis_url"] = BAD_URL                # Redis stops
        db._client = redis.Redis.from_url(BAD_URL, socket_connect_timeout=1)
        out = _call("search_memory", {"query": "saved before the outage"})
        assert out.startswith("Error: memory backend unreachable."), out
        assert "_No memories found._" not in out
        save = _parse_first_json(_call("save_memory", {"content": "during outage"}))
        assert save["status"] == "error" and "docker start redis-stack" in save["reason"]

    def test_connection_error_inside_search_is_not_swallowed(self, db, fake_embed, monkeypatch):
        import redis
        import n3mc_mcp.server as S
        monkeypatch.setattr(S, "_db", db)
        monkeypatch.setattr(db, "ensure_connected", lambda: True)   # pass the pre-check
        def boom(*a, **k):
            raise redis.ConnectionError("Error 10061 connecting to localhost:6379")
        monkeypatch.setattr(db._client, "execute_command", boom)
        out = _call("search_memory", {"query": "anything"})
        assert out.startswith("Error: memory backend unreachable."), out
        assert db._ok is False
