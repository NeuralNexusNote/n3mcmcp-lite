"""
Cross-session recall tests.

Verify that memories saved in session 1 are retrievable in session 2 (same
Redis, a different default session id).

Kept from v1.12.2 (spec §2, "Files kept as they are"); ported to the v2.0.0
API on 2026-11-20 because the v1.12.2 calls (`Database.connect()`,
`search_memory()`, `cfg["_session_id"]`, the helpers of the old conftest)
no longer exist. The checks are the same:

  1. Database layer — two Database instances with different default sessions.
  2. Tool layer — two ToolRunner instances (the handlers behind the MCP
     tools that the thin client forwards to, §4.6) with different sessions.
     v1.12.2 started two `python -m n3mc_mcp` processes here; in v2.0.0 that
     entry forwards to the resident server, so the same handlers are called
     directly instead (TestThin* in test_mcp_client.py covers the entry).

The critical invariant: b_session_mismatch=0.6 must NOT push a well-matched
query below min_score=0.2.

The embeddings are conftest's deterministic stand-in (no model download), so
these tests check the ranking and the session handling, not the real model.
"""
import json
import time
import uuid

import pytest


def _pair(cfg):
    """Two Database instances on the test Redis with different default sessions."""
    from n3mc_mcp.database import Database
    s1, s2 = str(uuid.uuid4()), str(uuid.uuid4())
    db1 = Database(cfg, default_session=s1)
    db2 = Database(cfg, default_session=s2)
    assert db1.ensure_connected() == "ok" and db2.ensure_connected() == "ok"
    return db1, db2, s1, s2


def _close(*dbs):
    for d in dbs:
        if d.client is not None:
            d.client.close()


def _hits(db, query):
    normal, _weak, _msg = db.search(query)
    return normal


# ── Layer 1: Database API ─────────────────────────────────────────────────────

class TestCrossSessionDatabaseLayer:
    """A restart is a fresh Database instance with a different default session."""

    @pytest.fixture(autouse=True)
    def _dbs(self, rconn, cfg):
        self.db1, self.db2, self.s1, self.s2 = _pair(cfg)
        yield
        _close(self.db1, self.db2)

    def test_single_memory_found_in_new_session(self):
        """Core invariant: saved memory is retrievable after session restart."""
        content = "クロスセッション検証: Python と Redis Stack はハイブリッド検索の黄金コンビ"
        r = self.db1.save_memory(content)
        assert r["saved"] is True, f"save failed: {r}"
        time.sleep(0.5)

        hits = _hits(self.db2, "Python Redis ハイブリッド検索")
        assert hits, ("cross-session recall returned no results — "
                      "b_session_mismatch may be dropping score below min_score=0.2")
        assert any(content in h["content"] for h in hits), \
            f"saved memory not in results; got: {[h['content'][:60] for h in hits]}"

    def test_b_session_mismatch_does_not_filter_good_match(self):
        """b_session_mismatch=0.6 must not push a good match below min_score=0.2."""
        content = "量子コンピューティングは古典コンピュータでは解けない最適化問題を解く"
        self.db1.save_memory(content)
        time.sleep(0.5)

        hits = _hits(self.db2, "量子コンピュータ 最適化問題")
        scores = [h["score"] for h in hits if content in h["content"]]
        assert scores, "target memory absent from cross-session results"
        assert scores[0] >= 0.2 * 0.6, f"score {scores[0]:.4f} is too low across sessions"

    def test_long_content_parent_doc_found_in_new_session(self):
        """Parent-doc (> chunk_threshold) verbatim recall works cross-session (§3.11)."""
        base = "架空の惑星ゼフィリアは三つの太陽を持つ恒星系に位置する。"
        long_content = base * 16  # > chunk_threshold=400
        r = self.db1.save_memory(long_content)
        assert r["saved"] is True
        assert "parent_id" in r, f"expected chunked save, got: {r}"
        time.sleep(0.5)

        hits = _hits(self.db2, "ゼフィリア 恒星系 太陽")
        assert hits, "parent-doc cross-session recall returned no results"
        assert any(h.get("_doc") and h["content"] == long_content for h in hits), \
            f"parent verbatim not in results; got: {[h['content'][:60] for h in hits]}"

    def test_multiple_saves_all_retrievable(self):
        """Three distinct memories saved in session 1 all appear in session 2."""
        topics = [
            "機械学習アルゴリズム: 勾配降下法はニューラルネットワークの最適化に使う",
            "データベース設計: 正規化により冗長性を排除し整合性を保つ",
            "並行プログラミング: スレッドとコルーチンで I/O バウンド処理を高速化",
        ]
        for t in topics:
            assert self.db1.save_memory(t)["saved"] is True
        time.sleep(0.5)

        for query, expected in [("勾配降下法 ニューラルネット", topics[0]),
                                ("データベース 正規化", topics[1]),
                                ("コルーチン I/O", topics[2])]:
            hits = _hits(self.db2, query)
            assert any(expected in h["content"] for h in hits), \
                f"query '{query}' did not find expected content in session 2"

    def test_session_id_tag_preserved_on_saved_memory(self):
        """The session_id stored in session 1 must be readable from session 2."""
        content = "セッション ID の保存確認テストエントリ"
        assert self.db1.save_memory(content)["saved"] is True
        time.sleep(0.5)

        hits = [h for h in _hits(self.db2, "セッション ID 保存確認") if content in h["content"]]
        assert hits
        assert hits[0].get("session_id", "") == self.s1, \
            f"stored session_id mismatch: got {hits[0].get('session_id')!r}, expected {self.s1!r}"


# ── Layer 2: the MCP tool handlers ───────────────────────────────────────────

class TestCrossSessionToolLayer:
    """save_memory in session 1 and search_memory in session 2 through the tool handlers."""

    def test_save_in_session1_found_in_session2(self, rconn, cfg):
        from n3mc_mcp.tools import ToolRunner
        db1, db2, _s1, _s2 = _pair(cfg)
        try:
            content = "MCP サブプロセス クロスセッション: ベクトルと BM25 のハイブリッド検索"
            query = "MCP ハイブリッド検索 ベクトル BM25"

            text = ToolRunner(cfg, db1).call("save_memory", {"content": content}, nudge=False)
            data = json.JSONDecoder().raw_decode(text)[0]
            assert data["saved"] is True, f"session-1 save failed: {data}"
            time.sleep(0.3)

            text = ToolRunner(cfg, db2).call("search_memory", {"query": query}, nudge=False)
            assert content in text, f"cross-session recall failed:\n{text[:500]}"
        finally:
            _close(db1, db2)
