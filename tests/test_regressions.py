"""Layer 4 (non-Redis part): config safety, test safety, time bounds."""

import json
import multiprocessing
import time

import pytest

from n3mc_mcp import config as config_mod
from n3mc_mcp import paths
from n3mc_mcp.tools import BoundError, parse_bounds


class TestConfig:
    def test_unreadable_not_overwritten(self):
        p = paths.config_path()
        p.write_text("{oops", encoding="utf-8")
        cfg = config_mod.load_config()
        assert cfg["_unreadable"] and p.read_text(encoding="utf-8") == "{oops"
        assert cfg["_notices"][0].startswith("config_unreadable:")

    def test_type_errors_ignored_and_reported(self):
        p = paths.config_path()
        d = json.loads(p.read_text(encoding="utf-8"))
        d.update({"vector_min_cos": "0.83", "ttl_seconds": 604800.5, "embedding_dim": 768.0,
                  "min_score": 1.5, "half_life_days": 0, "min_results": -1, "access_count_enabled": 1})
        p.write_text(json.dumps(d), encoding="utf-8")
        cfg = config_mod.load_config()
        assert cfg["vector_min_cos"] == 0.85 and cfg["ttl_seconds"] == 604800 and cfg["embedding_dim"] == 768
        n = cfg["_notices"][0]
        for k in ("vector_min_cos", "ttl_seconds", "embedding_dim", "min_score", "half_life_days",
                  "min_results", "access_count_enabled"):
            assert k in n
        assert json.loads(p.read_text(encoding="utf-8"))["vector_min_cos"] == "0.83"

    def test_overlap_follows_threshold(self):
        p = paths.config_path()
        d = json.loads(p.read_text(encoding="utf-8"))
        d.update({"chunk_threshold": 100})
        d.pop("chunk_overlap", None)
        p.write_text(json.dumps(d), encoding="utf-8")
        assert config_mod.load_config()["chunk_overlap"] == 25

    def test_missing_fields_filled(self):
        cfg = config_mod.load_config()
        d = json.loads(paths.config_path().read_text(encoding="utf-8"))
        assert d["owner_id"] == cfg["owner_id"] and d["context_max_chars"] == 9500


def _load(_):
    from n3mc_mcp import config
    return config.load_config()["owner_id"]


class TestConfigCreate:
    def test_parallel_create_one_owner(self, isolated):
        paths.config_path().unlink()
        with multiprocessing.get_context("spawn").Pool(6) as pool:
            owners = pool.map(_load, range(6))
        assert len(set(owners)) == 1


class TestTimeBoundsStrict:
    @pytest.mark.parametrize("bad", ["yesterday", "2026/11/13", "2026-13-01"])
    def test_unreadable(self, bad):
        with pytest.raises(BoundError) as e:
            parse_bounds(bad, None)
        assert str(e.value).startswith("Error: since must be an ISO 8601 date or datetime")

    def test_order_and_local_midnight(self):
        with pytest.raises(BoundError, match="since is later than until"):
            parse_bounds("2026-11-14", "2026-11-13")
        s, u = parse_bounds("2026-11-13", "2026-11-13")
        assert time.localtime(s)[3:6] == (0, 0, 0) and u - s > 86399
        assert parse_bounds("2026-11-13T09:00:00+09:00", None)[0] == parse_bounds("2026-11-13T00:00:00Z", None)[0]
        assert parse_bounds("", None) == (None, None)
