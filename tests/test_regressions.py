"""Layer 4: regression tests for the v1.7.0 and v1.7.1 fixes (spec §9).

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
        monkeypatch.setattr(S, "_notices_seen", set())
        assert S._take_notices() == ["config_unreadable: x"]
        assert S._take_notices() == []

    def test_later_notice_shown_on_next_search(self, monkeypatch):
        """v1.7.1: a notice added after the first search (e.g. shared_redis on
        a reconnect) is shown once on the next search."""
        from n3mc_mcp import server as S

        class FakeDb:
            cfg = {"_notices": ["config_type: a"]}
        monkeypatch.setattr(S, "_db", FakeDb())
        monkeypatch.setattr(S, "_notices_seen", set())
        assert S._take_notices() == ["config_type: a"]
        FakeDb.cfg["_notices"].append("shared_redis: b")
        assert S._take_notices() == ["shared_redis: b"]
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
        assert "Docker Desktop" in h  # A4: Docker itself may be stopped
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


# ── §7 config location: home dir + one-time migration (MSIX redirect fix) ────

@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    from pathlib import Path
    import n3mc_mcp.paths as paths
    home = tmp_path / "home"
    legacy = tmp_path / "legacy" / "config.json"
    home.mkdir()
    monkeypatch.delenv("N3MC_DATA_DIR", raising=False)
    monkeypatch.delenv("N3MC_REDIS_URL", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(paths, "get_legacy_config_path", lambda: legacy)
    monkeypatch.setattr(C, "get_legacy_config_path", lambda: legacy)
    return home, legacy


class TestConfigLocation:
    def test_default_is_home_dir(self, fake_home):
        home, _ = fake_home
        from n3mc_mcp.paths import get_config_path
        assert get_config_path() == home / ".n3mc-workingmemory" / "config.json"

    def test_legacy_config_migrated_once_byte_for_byte(self, fake_home):
        home, legacy = fake_home
        legacy.parent.mkdir(parents=True)
        raw = json.dumps({"owner_id": "legacy-owner", "local_id": "legacy-local",
                          "ttl_seconds": 3600}, indent=2).encode("utf-8")
        legacy.write_bytes(raw)
        cfg = C.load_config()
        assert cfg["owner_id"] == "legacy-owner" and cfg["ttl_seconds"] == 3600
        new = home / ".n3mc-workingmemory" / "config.json"
        assert new.exists() and legacy.read_bytes() == raw          # legacy kept
        legacy.write_text('{"owner_id": "changed-later"}', encoding="utf-8")
        assert C.load_config()["owner_id"] == "legacy-owner"         # not re-migrated

    def test_no_legacy_creates_new_config(self, fake_home):
        home, _ = fake_home
        cfg = C.load_config()
        assert (home / ".n3mc-workingmemory" / "config.json").exists()
        assert cfg["owner_id"]

    def test_data_dir_override_skips_migration(self, fake_home, tmp_path, monkeypatch):
        _, legacy = fake_home
        legacy.parent.mkdir(parents=True)
        legacy.write_text('{"owner_id": "legacy-owner", "local_id": "l"}', encoding="utf-8")
        monkeypatch.setenv("N3MC_DATA_DIR", str(tmp_path / "override"))
        assert C.load_config()["owner_id"] != "legacy-owner"


# ── §8 plugin SessionStart hook (A2 fixes) ───────────────────────────────────

import importlib.util
import shutil as _shutil
import subprocess as _subprocess
from pathlib import Path as _Path

_PLUGIN = _Path(__file__).resolve().parent.parent / "plugins" / "n3mc-workingmemory"


def _hook_module():
    spec = importlib.util.spec_from_file_location(
        "install_permissions", _PLUGIN / "hooks" / "install_permissions.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _hook_command() -> str:
    data = json.loads((_PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    return data["hooks"]["SessionStart"][0]["hooks"][0]["command"]


def _hook_bash():
    """The shell Claude Code uses for hooks: Git Bash on Windows (never the
    WSL launcher in System32), plain bash elsewhere."""
    import os
    if os.name == "nt":
        for c in (r"C:\Program Files\Git\bin\bash.exe",
                  r"C:\Program Files\Git\usr\bin\bash.exe"):
            if os.path.exists(c):
                return c
        return None
    return _shutil.which("bash")


class TestPluginHook:
    def test_rules_cover_plugin_scoped_names(self):
        mod = _hook_module()
        assert "mcp__plugin_n3mc-workingmemory_n3mc-workingmemory__*" in mod.RULES
        assert "mcp__n3mc-workingmemory__*" in mod.RULES

    def test_patch_is_idempotent_and_keeps_other_entries(self, tmp_path):
        mod = _hook_module()
        p = tmp_path / "settings.json"
        p.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}, "theme": "dark"}),
                     encoding="utf-8")
        assert len(mod._patch(p)) == 2
        assert mod._patch(p) == []
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["theme"] == "dark" and data["permissions"]["allow"][0] == "Bash(ls)"

    def test_python_free_fallback_prints_valid_json(self):
        cmd = _hook_command()
        last = cmd.split("||")[-1].strip()
        assert last.startswith("echo '") and last.endswith("'")
        payload = json.loads(last[len("echo '"):-1])
        assert "Python was not found" in payload["systemMessage"]
        assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert "mcp__plugin_n3mc-workingmemory_n3mc-workingmemory__*" in payload["systemMessage"]

    @pytest.mark.skipif(_hook_bash() is None, reason="Git Bash / bash not available")
    def test_fallback_runs_when_no_python(self, tmp_path):
        """Run the real command with a PATH that has bash builtins only."""
        empty = tmp_path / "empty"
        empty.mkdir()
        env = {"PATH": str(empty), "CLAUDE_PLUGIN_ROOT": str(_PLUGIN).replace("\\", "/"),
               "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
        bash = _hook_bash()
        r = _subprocess.run([bash, "-c", _hook_command()], env=env,
                            capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0, r.stderr
        assert "Python was not found" in json.loads(r.stdout)["systemMessage"]


# ── §3.9 model unavailable: retry throttle + degraded notice (A6 fixes) ──────

import types as _types


@pytest.fixture
def failing_model(monkeypatch):
    """sentence_transformers.SentenceTransformer that always fails; counts calls."""
    calls = {"n": 0}

    class Boom:
        def __init__(self, *a, **k):
            calls["n"] += 1
            raise OSError("We couldn't connect to 'https://huggingface.co' (offline)")

    fake = _types.ModuleType("sentence_transformers")
    fake.SentenceTransformer = Boom
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)
    monkeypatch.setattr(P, "_model", None)
    monkeypatch.setattr(P, "_model_error", None)
    monkeypatch.setattr(P, "_model_failed_at", 0.0)
    return calls


import sys  # noqa: E402  (used by the fixture above)


class TestModelUnavailable:
    def test_failed_load_is_not_retried_within_window(self, failing_model, monkeypatch):
        assert P.get_model() is None and failing_model["n"] == 1
        assert P.embed("x") is None and P.get_model() is None
        assert failing_model["n"] == 1                       # no retry inside the window
        monkeypatch.setattr(P, "_model_failed_at", P._model_failed_at - P.MODEL_RETRY_SECONDS - 1)
        P.get_model()
        assert failing_model["n"] == 2                       # retried after the window

    def test_message_names_model_and_error(self, failing_model):
        assert P.model_unavailable_message() is None         # not tried yet
        P.get_model()
        msg = P.model_unavailable_message()
        assert "keyword-only" in msg and "huggingface" in msg.lower()
        assert P._model_name in msg

    def test_search_output_carries_notice_every_time(self, monkeypatch):
        import asyncio
        import n3mc_mcp.server as S

        class FakeDb:
            cfg = {}
            def ensure_connected(self):
                return True
            def search_memory(self, *a, **k):
                return []
        monkeypatch.setattr(S, "_db", FakeDb())
        monkeypatch.setattr(S, "_notices_seen", set())
        monkeypatch.setattr(S, "model_unavailable_message", lambda: "MODEL DOWN")
        for _ in range(2):
            out = asyncio.run(S.call_tool("search_memory", {"query": "x"}))[0].text
            assert "> ⚠️ N3MC notice: MODEL DOWN" in out


class TestPluginHookSettingsErrors:
    """A8: settings.json that cannot be updated is reported, never silent."""

    def _run_main(self, home, monkeypatch, capsys):
        mod = _hook_module()
        monkeypatch.setattr(mod.Path, "home", classmethod(lambda cls: home))
        monkeypatch.setattr(mod, "_uvx_warning", lambda: "")
        assert mod.main() == 0
        return capsys.readouterr()

    def test_read_only_file_is_reported(self, tmp_path, monkeypatch, capsys):
        import os, stat
        sj = tmp_path / ".claude" / "settings.json"
        sj.parent.mkdir()
        sj.write_text('{"permissions": {"allow": ["Bash(ls)"]}}', encoding="utf-8")
        os.chmod(sj, stat.S_IREAD)
        try:
            out = self._run_main(tmp_path, monkeypatch, capsys)
        finally:
            os.chmod(sj, stat.S_IREAD | stat.S_IWRITE)
        msg = json.loads(out.out)["systemMessage"]
        assert "Could not update ~/.claude/settings.json" in msg
        assert "mcp__plugin_n3mc-workingmemory_n3mc-workingmemory__*" in msg
        assert "Could not update" in out.err
        assert "Bash(ls)" in sj.read_text(encoding="utf-8")       # unchanged

    def test_read_only_file_with_rules_is_quiet(self, tmp_path, monkeypatch, capsys):
        import os, stat
        mod = _hook_module()
        sj = tmp_path / ".claude" / "settings.json"
        sj.parent.mkdir()
        sj.write_text(json.dumps({"permissions": {"allow": mod.RULES}}), encoding="utf-8")
        os.chmod(sj, stat.S_IREAD)
        try:
            out = self._run_main(tmp_path, monkeypatch, capsys)
        finally:
            os.chmod(sj, stat.S_IREAD | stat.S_IWRITE)
        assert out.out == "" and out.err == ""

    def test_broken_json_is_reported_and_left_alone(self, tmp_path, monkeypatch, capsys):
        sj = tmp_path / ".claude" / "settings.json"
        sj.parent.mkdir()
        sj.write_text('{"permissions": {"allow": [}', encoding="utf-8")
        out = self._run_main(tmp_path, monkeypatch, capsys)
        assert "cannot read it as JSON" in json.loads(out.out)["systemMessage"]
        assert sj.read_text(encoding="utf-8") == '{"permissions": {"allow": [}'


# ── §3.13 invisible-only content (A10) ───────────────────────────────────────

INVISIBLE_ONLY = ["\x00", "\x00\x00 \x1b", "\u200b", "\ufeff", "\u200b\u200d\u2060",
                  "\ufe0f", "\U000E0100", "\u3000\x00"]
VISIBLE = ["A\x00B", "ABC\u200bDEF", "\u309a", "\u309c", "\u30ab\u3099",
           "\u795e\U000E0100 \u9ad9", "\u2764\ufe0f", "A\x1b[31mB\x7fC", "\uff8a\uff9f"]


class TestHasVisibleText:
    @pytest.mark.parametrize("text", INVISIBLE_ONLY + ["", "   \n\t"])
    def test_invisible(self, text):
        assert P.has_visible_text(text) is False

    @pytest.mark.parametrize("text", VISIBLE)
    def test_visible(self, text):
        assert P.has_visible_text(text) is True


@requires_redis
class TestInvisibleOnlyRejected:
    @pytest.mark.parametrize("text", INVISIBLE_ONLY)
    def test_rejected_and_nothing_written(self, db, redis_client, fake_embed, text):
        before = redis_client.dbsize()
        res = db.save_memory(text)
        assert res == {"status": "error", "saved": False,
                       "reason": "empty content (no visible characters)"}
        assert redis_client.dbsize() == before

    def test_whitespace_only_keeps_plain_reason(self, db, fake_embed):
        assert db.save_memory("  \n ")["reason"] == "empty content"

    @pytest.mark.parametrize("text", VISIBLE)
    def test_visible_content_stored_verbatim(self, db, redis_client, fake_embed, text):
        res = db.save_memory(text)
        assert res["saved"] is True
        stored = redis_client.hget(f"mem:{res['id']}", "content").decode("utf-8")
        assert stored == text

# ════════════════════════════════════════════════════════════════════════════
# v1.7.1 regressions (spec §9 Layer 4, rows marked v1.7.1)
# ════════════════════════════════════════════════════════════════════════════

import os as _os
import threading as _threading
import time as _time
from datetime import timedelta as _timedelta

import redis as _redis

from tests.conftest import TEST_DB_MARKER, safe_flushdb


def _iso_days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - _timedelta(days=days)).isoformat()


def _epoch_days_ago(days: float) -> float:
    return (datetime.now(timezone.utc) - _timedelta(days=days)).timestamp()


def _broken_pipeline(client, monkeypatch):
    """Make every pipeline of *client* fail at execute(); returns the real factory."""
    real = client.pipeline

    def broken(*a, **k):
        p = real(*a, **k)

        def execute(*ea, **ek):
            raise _redis.ResponseError("OOM command not allowed")
        p.execute = execute
        return p
    monkeypatch.setattr(client, "pipeline", broken)
    return real


# ── LH1 filter_score ─────────────────────────────────────────────────────────

class TestFilterScore:
    """A fresh current-session hit at cos 0.86 must not hide older or
    other-session records at cos 0.95-0.99 (spec §3.6 filter_score, §3.14)."""

    CASES = [  # (id, cos, days_old, same_session)
        ("fresh", 0.86, 0.0, True),
        ("old3d", 0.95, 3.0, True),
        ("other99", 0.99, 1.0, False),
        ("other95", 0.95, 1.0, False),
    ]

    def test_pure_pipeline(self):
        cfg = {"access_count_enabled": False}
        rows = []
        for rid, cos, days, same in self.CASES:
            b = P.b_local(1.0, 0, cfg)
            rows.append({
                "id": rid,
                "score": P.final_score(cos, 0.0, P.time_decay(_iso_days_ago(days), 3), b,
                                       1.0 if same else 0.6),
                "filter_score": P.filter_score(cos, 0.0, b),
            })
        rows.sort(key=lambda r: r["score"], reverse=True)
        kept = [r for r in rows if P.judged_score(r) >= 0.2]
        kept = P.relative_cut(kept, 0.6)
        assert {r["id"] for r in kept} == {c[0] for c in self.CASES}
        assert kept[0]["id"] == "fresh"
        assert [r["score"] for r in kept] == sorted((r["score"] for r in kept), reverse=True)
        # v1.7.0 behaviour for contrast: the final score would cut them.
        assert len(P.relative_cut([{"score": r["score"]} for r in rows], 0.6)) == 1

    def test_version_reorder_moves_filter_score_with_position(self):
        rows = [{"id": "old", "score": 0.9, "filter_score": 0.95, "timestamp_epoch": 1,
                 "content": "x" * 150},
                {"id": "new", "score": 0.5, "filter_score": 0.55, "timestamp_epoch": 2,
                 "content": "x" * 150}]
        out = P.order_versions_newest_first(rows, [axis(0), axis(0)],
                                            {"version_sim_threshold": 0.98,
                                             "version_min_chars": 100})
        assert out[0]["id"] == "new" and out[0]["score"] == 0.9
        assert out[0]["filter_score"] == 0.95 and out[1]["filter_score"] == 0.55

    def test_rerank_boost_added_to_both(self):
        rows = [{"content": "alpha beta gamma " * 5, "score": 0.1, "filter_score": 0.3}]
        P.lexical_rerank(rows, "alpha", 0.3, 0.2)
        assert rows[0]["score"] == pytest.approx(0.1 + 0.5)
        assert rows[0]["filter_score"] == pytest.approx(0.3 + 0.5)

    @requires_redis
    def test_search_memory_keeps_older_and_other_session(self, db, fake_embed, monkeypatch):
        import n3mc_mcp.database as D
        db.cfg.update(vector_min_cos=0.85, relative_min_score_ratio=0.6, min_score=0.2,
                      version_order_enabled=True, lexical_rerank_enabled=False)
        fake_embed["QUERY"] = axis(0)
        other_axis = 1
        for rid, cos, days, same in self.CASES:
            fake_embed[f"REC-{rid}"] = at_cos(cos, other=other_axis)
            other_axis += 1
            monkeypatch.setattr(D, "_now_iso", lambda d=days: _iso_days_ago(d))
            monkeypatch.setattr(D, "_now_epoch", lambda d=days: _epoch_days_ago(d))
            sess = db.cfg["_session_id"] if same else "another-process-session"
            assert db.save_memory(f"REC-{rid}", session_id=sess)["saved"]
        res = db.search_memory("QUERY")
        assert {r["content"] for r in res} == {f"REC-{c[0]}" for c in self.CASES}
        assert res[0]["content"] == "REC-fresh"
        assert [r["score"] for r in res] == sorted((r["score"] for r in res), reverse=True)


# ── LH2 / LL11 Redis timeouts ────────────────────────────────────────────────

def _silent_server():
    """A TCP server that accepts connections and never answers."""
    import socket
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    srv.settimeout(0.2)
    conns = []
    stop = _threading.Event()

    def loop():
        while not stop.is_set():
            try:
                c, _ = srv.accept()
                conns.append(c)
            except OSError:
                pass

    t = _threading.Thread(target=loop, daemon=True)
    t.start()

    def close():
        stop.set()
        t.join(1)
        for c in conns:
            c.close()
        srv.close()
    return srv.getsockname()[1], close


@requires_redis
class TestRedisTimeouts:
    def test_client_has_socket_timeout(self, db):
        kw = db._client.connection_pool.connection_kwargs
        assert kw["socket_timeout"] == 5 and kw["socket_connect_timeout"] == 2

    def test_silent_redis_does_not_hang(self, cfg, monkeypatch):
        from n3mc_mcp.database import Database
        import n3mc_mcp.server as S
        port, close = _silent_server()
        try:
            d = Database(dict(cfg, redis_url=f"redis://127.0.0.1:{port}/0"))
            monkeypatch.setattr(S, "_db", d)
            t0 = _time.monotonic()
            out = _call("search_memory", {"query": "x"})
            elapsed = _time.monotonic() - t0
        finally:
            close()
        assert out.startswith("Error: memory backend unreachable."), out
        assert elapsed < 8, elapsed

    def test_timeout_error_is_a_lost_connection(self, db, fake_embed, monkeypatch):
        import n3mc_mcp.server as S
        monkeypatch.setattr(S, "_db", db)
        monkeypatch.setattr(db, "ensure_connected", lambda: True)

        def slow(*a, **k):
            raise _redis.TimeoutError("Timeout reading from socket")
        monkeypatch.setattr(db._client, "execute_command", slow)
        assert _call("search_memory", {"query": "x"}).startswith("Error: memory backend unreachable.")
        assert db._ok is False

    def test_old_client_closed_on_reconnect(self, db):
        old = db._client
        closed = {"n": 0}
        real_close = old.close

        def spy():
            closed["n"] += 1
            real_close()
        old.close = spy
        assert db.connect(quiet=True)
        assert closed["n"] == 1 and db._client is not old


# ── LH3 config.json exclusive create / atomic update ─────────────────────────

class TestConfigCreate:
    def test_parallel_processes_share_one_owner_id(self, tmp_path):
        import subprocess
        env = dict(_os.environ, N3MC_DATA_DIR=str(tmp_path))
        env.pop("N3MC_REDIS_URL", None)
        code = ("import time, sys\n"
                "from n3mc_mcp.config import load_config\n"
                "time.sleep(max(0.0, float(sys.argv[1]) - time.time()))\n"
                "print(load_config()['owner_id'])\n")
        start = _time.time() + 4.0
        root = str(_Path(__file__).resolve().parent.parent)
        procs = [subprocess.Popen([sys.executable, "-c", code, str(start)], env=env,
                                  cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True) for _ in range(8)]
        outs = [p.communicate(timeout=60)[0].strip() for p in procs]
        file_owner = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))["owner_id"]
        assert set(outs) == {file_owner}, outs
        assert not [f for f in _os.listdir(tmp_path) if f.endswith(".tmp")]

    def test_lost_race_uses_the_winners_file(self, cfg_dir, monkeypatch):
        winner = {"owner_id": "winner-owner", "local_id": "winner-local"}

        def link(src, dst):
            _Path(dst).write_text(json.dumps(winner), encoding="utf-8")  # other process
            raise FileExistsError(dst)
        monkeypatch.setattr(C.os, "link", link)
        cfg = C.load_config()
        assert cfg["owner_id"] == "winner-owner" and cfg["local_id"] == "winner-local"

    def test_lost_race_waits_for_a_half_written_file(self, cfg_dir, monkeypatch):
        def link(src, dst):
            _Path(dst).write_text('{"owner_id": "slow', encoding="utf-8")

            def finish():
                _time.sleep(0.3)
                _Path(dst).write_text('{"owner_id": "slow-owner", "local_id": "l"}',
                                      encoding="utf-8")
            _threading.Thread(target=finish).start()
            raise FileExistsError(dst)
        monkeypatch.setattr(C.os, "link", link)
        assert C.load_config()["owner_id"] == "slow-owner"

    def test_update_goes_through_temp_and_replace(self, cfg_dir, monkeypatch):
        cfg_dir.write_text(json.dumps({"owner_id": "o", "local_id": "l"}), encoding="utf-8")
        calls = []
        real = _os.replace

        def spy(src, dst):
            calls.append((_Path(src), _Path(dst)))
            return real(src, dst)
        monkeypatch.setattr(C.os, "replace", spy)
        C.load_config()
        assert len(calls) == 1
        src, dst = calls[0]
        assert dst == cfg_dir and src.parent == cfg_dir.parent and src != dst
        assert "ttl_seconds" in json.loads(cfg_dir.read_text(encoding="utf-8"))

    def test_migration_is_an_exclusive_create(self, fake_home, monkeypatch):
        home, legacy = fake_home
        legacy.parent.mkdir(parents=True)
        legacy.write_text('{"owner_id": "legacy-owner", "local_id": "l"}', encoding="utf-8")
        links = []
        real = _os.link

        def spy(src, dst):
            links.append((_Path(src), _Path(dst)))
            return real(src, dst)
        monkeypatch.setattr(C.os, "link", spy)
        assert C.load_config()["owner_id"] == "legacy-owner"
        new = home / ".n3mc-workingmemory" / "config.json"
        assert links and links[0][1] == new and links[0][0].parent == new.parent


# ── LM2 per-key ranges / chunk_text advance ──────────────────────────────────

class TestConfigRanges:
    @pytest.mark.parametrize("key,value", [
        ("ttl_seconds", 604800.5), ("ttl_seconds", 0), ("ttl_seconds", -1),
        ("embedding_dim", 768.0), ("chunk_threshold", 10), ("half_life_days", 0),
        ("min_score", 1.5), ("vector_min_cos", -0.1), ("search_result_limit", 0),
    ])
    def test_out_of_range_ignored_with_notice(self, cfg_dir, key, value):
        text = json.dumps({"owner_id": "o", "local_id": "l", key: value})
        cfg_dir.write_text(text, encoding="utf-8")
        cfg = C.load_config()
        assert cfg[key] == C.DEFAULTS[key]
        assert any(n.startswith("config_type") and key in n for n in cfg["_notices"])
        assert cfg_dir.read_text(encoding="utf-8") == text

    def test_overlap_not_below_threshold_ignored(self, cfg_dir):
        cfg_dir.write_text(json.dumps({"owner_id": "o", "local_id": "l",
                                       "chunk_threshold": 200, "chunk_overlap": 200}),
                           encoding="utf-8")
        cfg = C.load_config()
        assert cfg["chunk_threshold"] == 200 and cfg["chunk_overlap"] < 200
        assert any("chunk_overlap" in n for n in cfg["_notices"])

    def test_valid_values_accepted(self):
        assert C.config_value_ok("ttl_seconds", 60)
        assert C.config_value_ok("half_life_days", 0.5)
        assert C.config_value_ok("min_score", 0) and C.config_value_ok("min_score", 1)
        assert C.config_value_ok("chunk_overlap", 99, chunk_threshold=100)
        assert not C.config_value_ok("chunk_overlap", 100, chunk_threshold=100)
        assert not C.config_value_ok("ttl_seconds", True)

    def test_chunk_text_always_advances(self):
        text = "x" * 1000
        chunks = P.chunk_text(text, 100, 100)
        assert chunks and len(chunks) <= 1000
        assert P.chunk_text(text, 100, 150)


# ── LM1 long save is all-or-nothing ──────────────────────────────────────────

def _no_break_body(n_chunks: int, threshold: int = 400, overlap: int = 100) -> str:
    """A body without soft breaks: hard cuts only, windows advance
    threshold - overlap characters, so it splits into exactly n_chunks."""
    import string
    alphabet = string.ascii_letters + string.digits
    rnd = random.Random(7)
    length = (threshold - overlap) * (n_chunks - 1) + threshold
    return "".join(rnd.choice(alphabet) for _ in range(length))


@requires_redis
class TestLongSaveAtomic:
    def test_embedding_failure_leaves_nothing(self, db, redis_client, fake_embed, monkeypatch):
        import n3mc_mcp.database as D
        body = "長い本文のテスト。" * 120
        good = D.embed
        calls = {"n": 0}

        def flaky(text, is_query=False):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("embedding exploded")
            return good(text, is_query)
        monkeypatch.setattr(D, "embed", flaky)
        with pytest.raises(RuntimeError):
            db.save_memory(body)
        assert not list(redis_client.scan_iter("doc:*"))
        assert not list(redis_client.scan_iter("docsha:*"))
        assert not list(redis_client.scan_iter("mem:*"))
        monkeypatch.setattr(D, "embed", good)
        assert db.save_memory(body)["saved"] is True

    def test_parent_and_chunks_in_one_pipeline(self, db, redis_client, fake_embed, monkeypatch):
        real = db._client.pipeline
        executed = []

        def spy(*a, **k):
            p = real(*a, **k)
            orig_exec = p.execute

            def execute(*ea, **ek):
                executed.append([c[0][0] for c in p.command_stack])
                return orig_exec(*ea, **ek)
            p.execute = execute
            return p
        monkeypatch.setattr(db._client, "pipeline", spy)
        res = db.save_memory("段落の文章です。" * 120)
        assert res["saved"]
        assert len(executed) == 1, executed
        assert executed[0].count("HSET") == 1 + res["chunks"]
        assert redis_client.exists(f"doc:{res['parent_id']}")
        guards = list(redis_client.scan_iter("docsha:*"))
        assert len(guards) == 1 and redis_client.get(guards[0]).decode() == res["parent_id"]

    def test_failed_write_releases_docsha(self, db, redis_client, fake_embed, monkeypatch):
        real = _broken_pipeline(db._client, monkeypatch)
        body = "文書の本文。" * 150
        with pytest.raises(_redis.ResponseError):
            db.save_memory(body)
        assert not list(redis_client.scan_iter("docsha:*"))
        monkeypatch.setattr(db._client, "pipeline", real)
        assert db.save_memory(body)["saved"] is True


# ── LM3 / LL8 index failure ──────────────────────────────────────────────────

class _FakeRedis:
    """Plain-Redis stand-in: PING / SCAN / CONFIG work, FT.CREATE fails."""

    def __init__(self, ft_create_error="unknown command 'FT.CREATE'", dim=768):
        self.ft_create_error = ft_create_error
        self.dim = dim
        self.ft_create_calls = 0

    def ping(self):
        return True

    def scan(self, cursor=0, match=None, count=None):
        return 0, []

    def config_set(self, k, v):
        return True

    def close(self):
        pass

    def execute_command(self, *args):
        if args[0] == "FT.CREATE":
            self.ft_create_calls += 1
            if self.ft_create_error:
                raise _redis.ResponseError(self.ft_create_error)
            return b"OK"
        if args[0] == "FT.INFO":
            return [b"index_name", b"n3mc_idx", b"attributes", [
                [b"identifier", b"content", b"attribute", b"content", b"type", b"TEXT"],
                [b"identifier", b"embedding", b"attribute", b"embedding", b"type", b"VECTOR",
                 b"algorithm", b"FLAT", b"data_type", b"FLOAT32", b"dim", self.dim,
                 b"distance_metric", b"COSINE"]]]
        raise _redis.ResponseError(f"unexpected {args[0]}")


def _fake_connect(fake):
    def connect(self, quiet=False):
        self._client = fake
        self._ok = True
        return True
    return connect


class TestIndexFailure:
    def test_main_survives_and_tools_return_hint(self, cfg, monkeypatch):
        import asyncio
        import contextlib
        import n3mc_mcp.server as S
        from n3mc_mcp.database import Database
        fake = _FakeRedis()
        cfg = dict(cfg, redis_url="redis://127.0.0.1:6390/0", embedding_dim=768)
        monkeypatch.setattr(S, "load_config", lambda: cfg)
        monkeypatch.setattr(Database, "connect", _fake_connect(fake))
        monkeypatch.setattr(S, "import_backend", lambda: None)
        monkeypatch.setattr(S, "_preload_model", lambda: None)

        @contextlib.asynccontextmanager
        async def fake_stdio():
            yield (None, None)

        async def fake_run(*a, **k):
            return None
        monkeypatch.setattr(S, "stdio_server", fake_stdio)
        monkeypatch.setattr(S.app, "run", fake_run)
        monkeypatch.setattr(S, "_db", None)
        asyncio.run(S._main())                       # must not raise
        assert S._db is not None and S._db._index_ok is False
        out = _call("search_memory", {"query": "x"})
        assert out.startswith("Error: memory backend unreachable."), out
        assert "does not provide RediSearch" in out and "Redis Stack, not plain Redis" in out
        assert "unknown command" in out
        save = _parse_first_json(_call("save_memory", {"content": "x"}))
        assert save["status"] == "error" and "redis-stack-server" in save["reason"]
        assert fake.ft_create_calls >= 3             # retried on every call
        fake.ft_create_error = ""                    # Redis Stack now available
        assert S._db.ensure_connected() is True and S._db._index_ok

    def test_dim_mismatch_notice(self, cfg, monkeypatch):
        from n3mc_mcp.database import Database
        fake = _FakeRedis(ft_create_error="Index already exists", dim=384)
        monkeypatch.setattr(Database, "connect", _fake_connect(fake))
        d = Database(dict(cfg, embedding_dim=768, _notices=[]))
        d.connect()
        d.ensure_index()
        assert d._index_ok
        notes = [n for n in d.cfg["_notices"] if n.startswith("index_dim_mismatch")]
        assert len(notes) == 1 and "384" in notes[0] and "768" in notes[0]
        d.ensure_index()
        assert len([n for n in d.cfg["_notices"] if n.startswith("index_dim_mismatch")]) == 1

    def test_same_dim_no_notice(self, cfg, monkeypatch):
        from n3mc_mcp.database import Database
        fake = _FakeRedis(ft_create_error="Index already exists", dim=768)
        monkeypatch.setattr(Database, "connect", _fake_connect(fake))
        d = Database(dict(cfg, embedding_dim=768, _notices=[]))
        d.connect()
        d.ensure_index()
        assert d._index_ok and d.cfg["_notices"] == []


# ── LM4 enforce_ephemeral only on an N3MC-only DB ────────────────────────────

@requires_redis
class TestEnforceEphemeral:
    def _spy(self, db, monkeypatch):
        calls = []
        real = db._client.config_set

        def spy(k, v):
            calls.append((k, v))
            return real(k, v)
        monkeypatch.setattr(db._client, "config_set", spy)
        return calls

    def test_empty_db_gets_config_set(self, db, redis_client, monkeypatch):
        calls = self._spy(db, monkeypatch)
        redis_client.delete(TEST_DB_MARKER)
        try:
            assert redis_client.dbsize() == 0
            db.enforce_ephemeral()
        finally:
            redis_client.set(TEST_DB_MARKER, b"1")
        assert ("appendonly", "no") in calls and ("save", "") in calls

    def test_n3mc_only_db_gets_config_set(self, db, redis_client, fake_embed, monkeypatch):
        db.save_memory("short")
        db.save_memory("long body. " * 60)
        calls = self._spy(db, monkeypatch)
        redis_client.delete(TEST_DB_MARKER)
        try:
            db.enforce_ephemeral()
        finally:
            redis_client.set(TEST_DB_MARKER, b"1")
        assert len(calls) == 2
        assert not any(n.startswith("shared_redis") for n in db.cfg.get("_notices", []))

    def test_foreign_key_leaves_config_and_notices(self, db, redis_client, monkeypatch):
        calls = self._spy(db, monkeypatch)
        redis_client.set("someone-else:data", b"1")
        db.enforce_ephemeral()
        assert calls == []
        notes = [n for n in db.cfg["_notices"] if n.startswith("shared_redis")]
        assert len(notes) == 1 and ":6390" in notes[0]
        assert "docker run -d --name redis-stack" in notes[0]

    def test_refused_config_is_a_notice_not_a_failure(self, db, redis_client, monkeypatch):
        def refuse(k, v):
            raise _redis.ResponseError("unknown command 'CONFIG'")
        monkeypatch.setattr(db._client, "config_set", refuse)
        redis_client.delete(TEST_DB_MARKER)
        try:
            db.enforce_ephemeral()
        finally:
            redis_client.set(TEST_DB_MARKER, b"1")
        assert db._ok
        assert any(n.startswith("config_refused") for n in db.cfg["_notices"])


# ── LM5 model wait ───────────────────────────────────────────────────────────

@pytest.fixture
def model_loading(monkeypatch):
    """The background load holds the model lock; tool calls wait 0.2 s."""
    monkeypatch.setattr(P, "_model", None)
    monkeypatch.setattr(P, "_model_error", None)
    monkeypatch.setattr(P, "_model_still_loading", False)
    monkeypatch.setattr(P, "MODEL_WAIT_SECONDS", 0.2)
    assert P._model_lock.acquire(timeout=5)
    yield
    P._model_lock.release()


class TestModelWait:
    def test_embed_gives_up_after_the_wait(self, model_loading):
        t0 = _time.monotonic()
        assert P.embed("x", is_query=True) is None
        assert _time.monotonic() - t0 < 2
        msg = P.model_unavailable_message()
        assert "is still loading" in msg and "keywords only" in msg

    def test_search_output_has_still_loading_notice(self, model_loading, monkeypatch):
        import asyncio
        import n3mc_mcp.server as S

        class FakeDb:
            cfg = {}

            def ensure_connected(self):
                return True

            def search_memory(self, *a, **k):
                assert P.embed("q", is_query=True) is None
                return []
        monkeypatch.setattr(S, "_db", FakeDb())
        monkeypatch.setattr(S, "_notices_seen", set())
        out = asyncio.run(S.call_tool("search_memory", {"query": "x"}))[0].text
        assert "> ⚠️ N3MC notice: The embedding model" in out and "still loading" in out

    @requires_redis
    def test_save_without_vector_with_detail(self, db, redis_client, model_loading):
        res = db.save_memory("saved while the model is loading")
        assert res["saved"] and res["warning"] == "embedding_unavailable"
        assert "still loading" in res["detail"]
        assert not redis_client.hexists(f"mem:{res['id']}", "embedding")

    @requires_redis
    def test_long_save_waits_once(self, db, model_loading):
        t0 = _time.monotonic()
        res = db.save_memory("段落の文章です。" * 200)
        assert res["saved"] and res["warning"] == "embedding_unavailable"
        assert _time.monotonic() - t0 < 3      # 0.2 s once, not per chunk


# ── LM6 list paging ──────────────────────────────────────────────────────────

@requires_redis
class TestListPaging:
    def test_standalone_listed_after_260_chunk_doc(self, db, fake_embed, monkeypatch):
        import n3mc_mcp.database as D
        monkeypatch.setattr(D, "_now_iso", lambda: _iso_days_ago(0.01))
        monkeypatch.setattr(D, "_now_epoch", lambda: _epoch_days_ago(0.01))
        single = db.save_memory("standalone before the long document")
        monkeypatch.setattr(D, "_now_iso", lambda: _iso_days_ago(0))
        monkeypatch.setattr(D, "_now_epoch", lambda: _epoch_days_ago(0))
        doc = db.save_memory(_no_break_body(260))
        assert doc["chunks"] == 260
        ids = [m["id"] for m in db.list_memories(20)]
        assert single["id"] in ids and doc["parent_id"] in ids


# ── LM7 guard TTL refresh ────────────────────────────────────────────────────

@requires_redis
class TestGuardRefresh:
    def test_standalone_guard_refreshed(self, db, redis_client, fake_embed):
        db.cfg.update(ttl_refresh_on_search=True)
        fake_embed["guarded"] = axis(0)
        res = db.save_memory("guarded fact for refresh")
        key = f"mem:{res['id']}"
        guard = next(redis_client.scan_iter("mem:sha:*"))
        redis_client.expire(key, 100)
        redis_client.expire(guard, 100)
        assert db.search_memory("guarded")
        assert redis_client.ttl(key) > 1000 and redis_client.ttl(guard) > 1000
        assert redis_client.get(guard).decode() == res["id"]

    def test_docsha_refreshed(self, db, redis_client, fake_embed):
        db.cfg.update(ttl_refresh_on_search=True)
        fake_embed["refreshdoc"] = axis(0)
        res = db.save_memory("refreshdoc " + "本文です。" * 120)
        doc_key = f"doc:{res['parent_id']}"
        guard = next(redis_client.scan_iter("docsha:*"))
        redis_client.expire(doc_key, 100)
        redis_client.expire(guard, 100)
        assert db.search_memory("refreshdoc")
        assert redis_client.ttl(doc_key) > 1000 and redis_client.ttl(guard) > 1000


# ── LM8 OR-joined BM25 / rerank tokens ───────────────────────────────────────

@requires_redis
class TestOrBm25:
    def test_extra_terms_still_hit(self, db, fake_embed):
        db.save_memory("the quick brown fox jumps over the dog")
        owner = db.cfg["owner_id"]
        assert db._bm25_search("quick fox elephant", owner, 20)
        assert db._bm25_search("what did the quick fox do", owner, 20)

    def test_japanese_terms_ored(self, db, fake_embed):
        db.save_memory("記憶装置の設定を変更した")
        assert db._bm25_search("記憶装置 大阪", db.cfg["owner_id"], 20)

    def test_rerank_counts_word_before_punctuation(self):
        rows = [{"content": "this is a note, kept for later reference only", "score": 0.0}]
        P.lexical_rerank(rows, "note", 1.0, 0.0)
        assert rows[0]["score"] > 0.5


# ── LM9 strict time bounds ───────────────────────────────────────────────────

class TestTimeBoundsStrict:
    def _db(self, cfg, monkeypatch):
        from n3mc_mcp.database import Database
        import n3mc_mcp.server as S
        d = Database(cfg)
        monkeypatch.setattr(d, "ensure_connected", lambda: True)
        monkeypatch.setattr(S, "_db", d)
        return d

    @pytest.mark.parametrize("value", ["yesterday", "2026/11/13", "2026-13-01", "13-11-2026"])
    def test_unreadable_bound_is_an_error(self, cfg, value, monkeypatch):
        self._db(cfg, monkeypatch)
        out = _call("search_memory", {"query": "x", "since": value})
        assert out == ("Error: since must be an ISO 8601 date or datetime, e.g. 2026-11-13 "
                       f"or 2026-11-13T09:00:00+09:00 (got '{value}')")
        out = _call("search_memory", {"query": "x", "until": value})
        assert out.startswith("Error: until must be an ISO 8601 date or datetime")

    def test_since_after_until(self, cfg, monkeypatch):
        self._db(cfg, monkeypatch)
        out = _call("search_memory", {"query": "x", "since": "2026-11-14", "until": "2026-11-13"})
        assert out == "Error: since is later than until"

    def test_local_time_for_offsetless_values(self, monkeypatch):
        import n3mc_mcp.database as D
        jst = timezone(_timedelta(hours=9))
        monkeypatch.setattr(D, "_LOCAL_TZ", jst)
        utc = timezone.utc
        assert D._parse_time_bound("2026-11-13") == datetime(2026, 11, 12, 15, tzinfo=utc).timestamp()
        assert D._parse_time_bound("2026-11-13", is_until=True) == pytest.approx(
            datetime(2026, 11, 13, 14, 59, 59, 999999, tzinfo=utc).timestamp())
        assert D._parse_time_bound("2026-11-13 09:00") == datetime(2026, 11, 13, 0, tzinfo=utc).timestamp()
        assert D._parse_time_bound("2026-11-13T09:00:00Z") == datetime(2026, 11, 13, 9, tzinfo=utc).timestamp()
        assert D._parse_time_bound("2026-11-13T09:00:00+09:00") == datetime(2026, 11, 13, 0, tzinfo=utc).timestamp()

    def test_empty_string_is_no_bound(self):
        from n3mc_mcp.database import _time_filter_clause
        assert _time_filter_clause("", "") == ""
        assert _time_filter_clause("  ", "2026-01-01").startswith("@timestamp_epoch:[-inf ")


# ── LL1 repair re-embeds ─────────────────────────────────────────────────────

@requires_redis
class TestRepairReembed:
    def test_missing_vectors_added(self, db, redis_client, fake_embed, monkeypatch):
        import n3mc_mcp.database as D
        good = D.embed
        monkeypatch.setattr(D, "embed", lambda text, is_query=False: None)
        ids = [db.save_memory(f"no vector entry {i}")["id"] for i in range(3)]
        other = D.Database(dict(db.cfg, owner_id="someone-else"))
        other._client, other._ok = db._client, True
        foreign = other.save_memory("foreign entry without vector")["id"]
        for i in ids:
            redis_client.expire(f"mem:{i}", 1000)
        res = db.repair_memory()
        assert res["status"] == "ok" and res["message"] == "index ensured"
        assert (res["reembedded"], res["remaining"]) == (0, 3)
        assert res.get("warning") == "embedding_unavailable"
        monkeypatch.setattr(D, "embed", good)
        monkeypatch.setattr(D.Database, "REEMBED_PER_CALL", 2)
        res = db.repair_memory()
        assert (res["reembedded"], res["remaining"]) == (2, 1)
        res = db.repair_memory()
        assert (res["reembedded"], res["remaining"]) == (1, 0)
        for i in ids:
            assert redis_client.hexists(f"mem:{i}", "embedding")
            assert 0 < redis_client.ttl(f"mem:{i}") <= 1000
        assert not redis_client.hexists(f"mem:{foreign}", "embedding")


# ── LL3 delete_memory owner check ────────────────────────────────────────────

@requires_redis
class TestDeleteOwner:
    def test_other_owner_gets_not_found(self, db, redis_client, fake_embed):
        from n3mc_mcp.database import Database
        mine = db.save_memory("my private note")
        doc = db.save_memory("my private document. " * 40)
        other = Database(dict(db.cfg, owner_id="someone-else"))
        other._client, other._ok = db._client, True
        assert other.delete_memory(mine["id"]) == {"status": "not_found", "id": mine["id"]}
        assert other.delete_memory(doc["parent_id"])["status"] == "not_found"
        assert redis_client.exists(f"mem:{mine['id']}")
        assert redis_client.exists(f"doc:{doc['parent_id']}")
        assert all(redis_client.exists(f"mem:{c}") for c in doc["ids"])

    def test_chunks_deleted_from_chunk_ids(self, db, redis_client, fake_embed, monkeypatch):
        doc = db.save_memory("cascade document body. " * 40)

        def no_legacy(*a, **k):
            raise AssertionError("chunk_ids should be used")
        monkeypatch.setattr(db, "_legacy_chunk_keys", no_legacy)
        res = db.delete_memory(doc["parent_id"])
        assert res["status"] == "deleted" and res["chunks_deleted"] == doc["chunks"]
        assert not any(redis_client.exists(f"mem:{c}") for c in doc["ids"])
        assert not list(redis_client.scan_iter("docsha:*"))


# ── LL10 SET NX guard ────────────────────────────────────────────────────────

@requires_redis
class TestGuardNx:
    def test_simultaneous_saves_store_one(self, db, redis_client, fake_embed, monkeypatch):
        import n3mc_mcp.database as D
        good = D.embed
        barrier = _threading.Barrier(2, timeout=10)

        def slow(text, is_query=False):
            barrier.wait()            # both saves have passed the EXISTS check
            return good(text, is_query)
        monkeypatch.setattr(D, "embed", slow)
        results = []
        ts = [_threading.Thread(target=lambda: results.append(db.save_memory("racing fact")))
              for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(20)
        assert sorted(r["status"] for r in results) == ["duplicate", "saved"]
        records = [k for k in redis_client.scan_iter("mem:*") if not k.startswith(b"mem:sha:")]
        assert len(records) == 1

    def test_failed_write_releases_guard(self, db, redis_client, fake_embed, monkeypatch):
        real = _broken_pipeline(db._client, monkeypatch)
        with pytest.raises(_redis.ResponseError):
            db.save_memory("write fails after the guard")
        assert not list(redis_client.scan_iter("mem:sha:*"))
        monkeypatch.setattr(db._client, "pipeline", real)
        assert db.save_memory("write fails after the guard")["saved"] is True


# ── LL4 no ghost hash from HINCRBY ───────────────────────────────────────────

@requires_redis
class TestAccessCountNoGhost:
    def test_expired_key_not_recreated(self, db, redis_client, fake_embed, monkeypatch):
        db.cfg.update(ttl_refresh_on_search=True)
        fake_embed["ghost"] = axis(0)
        res = db.save_memory("ghost candidate record")
        key = f"mem:{res['id']}"
        real = db._refresh_ttl

        def expire_first(rows):
            redis_client.delete(key)                       # expired in between
            for g in list(redis_client.scan_iter("mem:sha:*")):
                redis_client.delete(g)
            return real(rows)
        monkeypatch.setattr(db, "_refresh_ttl", expire_first)
        assert db.search_memory("ghost")
        assert not redis_client.exists(key)
        assert not list(redis_client.scan_iter("mem:sha:*"))


# ── LL5 Hangul fillers / braille blank ───────────────────────────────────────

BLANK_LETTERS = ["ㅤ", "ᅟ", "ᅠ", "ﾠ", "⠀"]


class TestHangulFiller:
    @pytest.mark.parametrize("ch", BLANK_LETTERS)
    def test_alone_is_invisible(self, ch):
        assert P.has_visible_text(ch) is False
        assert P.has_visible_text(ch * 3 + " ​") is False

    @pytest.mark.parametrize("ch", BLANK_LETTERS)
    def test_with_visible_char(self, ch):
        assert P.has_visible_text("A" + ch) is True

    @requires_redis
    @pytest.mark.parametrize("ch", BLANK_LETTERS)
    def test_rejected_and_stored_verbatim(self, db, redis_client, fake_embed, ch):
        assert db.save_memory(ch)["reason"] == "empty content (no visible characters)"
        res = db.save_memory("A" + ch + "B")
        assert redis_client.hget(f"mem:{res['id']}", "content").decode("utf-8") == "A" + ch + "B"


# ── LL2 / LL7 plugin hook additions ──────────────────────────────────────────

class TestPluginHookV171:
    def test_reread_before_write(self, tmp_path):
        mod = _hook_module()
        p = tmp_path / "settings.json"
        p.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}}), encoding="utf-8")
        real = mod._read_settings
        calls = {"n": 0}

        def wrapped(path):
            calls["n"] += 1
            if calls["n"] == 2:   # another program writes between read and write
                path.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)", "Bash(new)"]},
                                            "model": "x"}), encoding="utf-8")
            return real(path)
        mod._read_settings = wrapped
        assert len(mod._patch(p)) == 2
        data = json.loads(p.read_text(encoding="utf-8"))
        assert "Bash(new)" in data["permissions"]["allow"] and data["model"] == "x"
        assert set(mod.RULES) <= set(data["permissions"]["allow"])

    def test_atomic_replace(self, tmp_path, monkeypatch):
        mod = _hook_module()
        p = tmp_path / "settings.json"
        p.write_text("{}", encoding="utf-8")
        calls = []
        real = mod.os.replace

        def spy(src, dst):
            calls.append((_Path(src), _Path(dst)))
            return real(src, dst)
        monkeypatch.setattr(mod.os, "replace", spy)
        mod._patch(p)
        assert len(calls) == 1 and calls[0][1] == p and calls[0][0].parent == p.parent
        assert not [f for f in _os.listdir(tmp_path) if f.endswith(".tmp")]

    def test_fallback_names_script_failure(self):
        last = _hook_command().split("||")[-1].strip()
        payload = json.loads(last[len("echo '"):-1])
        assert "or the permission script failed" in payload["systemMessage"]
        assert payload["hookSpecificOutput"]["additionalContext"] == payload["systemMessage"]


# ── LL6 test-safety additions ────────────────────────────────────────────────

class TestTestSafetyV171:
    def test_live_config_with_bom_is_read(self, tmp_path, monkeypatch):
        monkeypatch.setenv("N3MC_DATA_DIR", str(tmp_path))
        (tmp_path / "config.json").write_bytes(
            b"\xef\xbb\xbf" + json.dumps({"redis_url": "redis://live-host:7777/3"}).encode())
        assert _norm_url("redis://live-host:7777/3") in _live_redis_urls()

    @requires_redis
    def test_unmarked_db_with_data_not_flushed(self, redis_client):
        redis_client.delete(TEST_DB_MARKER)
        redis_client.set("precious", b"1")
        try:
            assert safe_flushdb(redis_client) is False
            assert redis_client.get("precious") == b"1"
        finally:
            redis_client.delete("precious")
            redis_client.set(TEST_DB_MARKER, b"1")
        assert safe_flushdb(redis_client) is True
        assert redis_client.exists(TEST_DB_MARKER)


# ── LL9 save warning while config.json is unreadable ─────────────────────────

class TestConfigUnreadableSave:
    def test_load_config_marks_unreadable(self, cfg_dir):
        cfg_dir.write_text("{broken", encoding="utf-8")
        assert C.load_config()["_config_unreadable"] == str(cfg_dir)

    @requires_redis
    def test_every_save_warns(self, db, fake_embed):
        db.cfg["_config_unreadable"] = "/x/config.json"
        for text in ("first entry", "second entry"):
            res = db.save_memory(text)
            assert res["saved"] and res["warning"] == "config_unreadable"
            assert "temporary owner_id" in res["detail"] and "/x/config.json" in res["detail"]
