"""
Cross-session recall tests.

Verify that memories saved in session 1 (MCP server process A) are
retrievable in session 2 (MCP server process B, same Redis, new _session_id).

Two test layers:
  1. Database-layer (fast)  — two Database instances with different _session_id
  2. MCP subprocess (E2E)   — two real server sub-processes via MCP stdio client

The critical invariant: b_session_mismatch=0.6 must NOT push a
well-matched query below min_score=0.2.

  threshold: cos_sim >= 0.2 / (0.7 * 0.6) ≈ 0.476
  multilingual-e5-base on identical-domain queries reliably delivers 0.7+.
"""
import json
import time
import uuid
import asyncio
import sys
import os
from pathlib import Path

import pytest
from tests.conftest import requires_redis


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_cfg(redis_url: str, owner_id: str, session_id: str) -> dict:
    """Production-default config with the given owner/session."""
    return {
        "redis_url":              redis_url,
        "owner_id":               owner_id,
        "local_id":               str(uuid.uuid4()),
        "_session_id":            session_id,
        "ttl_seconds":            604800,
        "dedup_threshold":        0.95,
        "half_life_days":         3,
        "bm25_min_threshold":     0.1,
        "search_result_limit":    20,
        "context_char_limit":     3000,
        "min_score":              0.2,   # production default — the real threshold
        "search_query_max_chars": 2000,
        "chunk_threshold":        400,
        "chunk_overlap":          100,
        "access_count_enabled":   True,
        "access_count_weight":    0.02,
        "access_count_max_boost": 0.5,
        "ttl_refresh_on_search":  False,
        "ttl_refresh_top_k":      5,
        "lexical_rerank_enabled": False,
        "rerank_weight":          0.3,
        "rerank_phrase_weight":   0.2,
        "b_session_match":        1.0,
        "b_session_mismatch":     0.6,
        "skip_code_blocks":       False,
    }


def _parse(text: str) -> dict:
    return json.JSONDecoder().raw_decode(text)[0]


# ── Layer 1: Database API (no subprocess) ────────────────────────────────────

@requires_redis
class TestCrossSessionDatabaseLayer:
    """Simulate MCP server restart by constructing a fresh Database instance
    with a different _session_id pointing at the same Redis DB."""

    REDIS_URL = "redis://localhost:6379/0"

    def _pair(self) -> tuple:
        """Return (db1, db2) sharing owner_id but having different _session_id."""
        from n3mc_mcp.database import Database
        owner_id   = str(uuid.uuid4())
        session_id1 = str(uuid.uuid4())
        session_id2 = str(uuid.uuid4())
        db1 = Database(_make_cfg(self.REDIS_URL, owner_id, session_id1))
        db2 = Database(_make_cfg(self.REDIS_URL, owner_id, session_id2))
        return db1, db2

    # ── setup helpers ────────────────────────────────────────────────────────

    def _connect(self, db) -> None:
        db.connect()
        db.ensure_index()

    # ── fixture: fresh Redis for each test ───────────────────────────────────

    @pytest.fixture(autouse=True)
    def _flush(self):
        """Flush DB 0 before and after every test in this class."""
        import redis as redis_lib
        r = redis_lib.Redis.from_url(self.REDIS_URL, decode_responses=False)
        r.flushdb()
        yield
        r.flushdb()

    # ── tests ────────────────────────────────────────────────────────────────

    def test_single_memory_found_in_new_session(self):
        """Core invariant: saved memory is retrievable after session restart."""
        db1, db2 = self._pair()
        self._connect(db1)
        self._connect(db2)

        content = "クロスセッション検証: Python と Redis Stack はハイブリッド検索の黄金コンビ"
        r = db1.save_memory(content)
        assert r["saved"] is True, f"save failed: {r}"
        time.sleep(0.5)

        hits = db2.search_memory("Python Redis ハイブリッド検索")
        assert hits, (
            "cross-session recall returned no results — "
            "b_session_mismatch may be dropping score below min_score=0.2"
        )
        assert any(content in h["content"] for h in hits), (
            f"saved memory not in results; got: {[h['content'][:60] for h in hits]}"
        )

    def test_b_session_mismatch_does_not_filter_good_match(self):
        """b_session_mismatch=0.6 × cos_sim≥0.7 ≥ min_score=0.2.

        This would regress if b_session_mismatch were raised or min_score
        were raised disproportionately.
        """
        db1, db2 = self._pair()
        self._connect(db1)
        self._connect(db2)

        content = "量子コンピューティングは古典コンピュータでは解けない最適化問題を解く"
        db1.save_memory(content)
        time.sleep(0.5)

        hits = db2.search_memory("量子コンピュータ 最適化問題")
        scores = [h["score"] for h in hits if content in h["content"]]
        assert scores, "target memory absent from cross-session results"
        # score must exceed min_score=0.2 even with b_session_mismatch=0.6
        assert scores[0] >= 0.2, (
            f"score {scores[0]:.4f} is below min_score=0.2 — "
            "b_session_mismatch penalty is too aggressive"
        )

    def test_long_content_parent_doc_found_in_new_session(self):
        """Parent-doc (>chunk_threshold) verbatim recall works cross-session."""
        db1, db2 = self._pair()
        self._connect(db1)
        self._connect(db2)

        # Construct content > 400 chars (chunk_threshold default).
        # base = 28 chars; 16 × 28 = 448 > 400
        base = "架空の惑星ゼフィリアは三つの太陽を持つ恒星系に位置する。"
        long_content = base * 16  # 448 chars > chunk_threshold=400
        r = db1.save_memory(long_content)
        assert r["saved"] is True
        assert "parent_id" in r, f"expected chunked save, got: {r}"
        time.sleep(0.5)

        hits = db2.search_memory("ゼフィリア 恒星系 太陽")
        assert hits, "parent-doc cross-session recall returned no results"
        # §3.11: parent's full content must be returned (verbatim)
        assert any(base in h["content"] for h in hits), (
            f"parent verbatim not in results; got snippets: "
            f"{[h['content'][:60] for h in hits]}"
        )
        # §3.11: [doc×N] tag must be present
        assert any("[doc×" in h.get("_tag", "") for h in hits), (
            f"[doc×N] tag missing; hit keys: {[list(h.keys()) for h in hits]}"
        )

    def test_multiple_saves_all_retrievable(self):
        """Three distinct memories saved in session 1 all appear in session 2."""
        db1, db2 = self._pair()
        self._connect(db1)
        self._connect(db2)

        topics = [
            "機械学習アルゴリズム: 勾配降下法はニューラルネットワークの最適化に使う",
            "データベース設計: 正規化により冗長性を排除し整合性を保つ",
            "並行プログラミング: スレッドとコルーチンで I/O バウンド処理を高速化",
        ]
        for t in topics:
            r = db1.save_memory(t)
            assert r["saved"] is True

        time.sleep(0.5)

        # Each topic should be findable independently
        queries = [
            ("勾配降下法 ニューラルネット", topics[0]),
            ("データベース 正規化", topics[1]),
            ("コルーチン I/O", topics[2]),
        ]
        for query, expected_content in queries:
            hits = db2.search_memory(query)
            assert any(expected_content in h["content"] for h in hits), (
                f"query '{query}' did not find expected content in session 2"
            )

    def test_session_id_tag_preserved_on_saved_memory(self):
        """The session_id stored in session 1 must be readable from session 2."""
        db1, db2 = self._pair()
        self._connect(db1)
        self._connect(db2)

        content = "セッション ID の保存確認テストエントリ"
        r = db1.save_memory(content)
        assert r["saved"] is True
        time.sleep(0.5)

        hits = db2.search_memory("セッション ID 保存確認")
        assert hits
        # The stored session_id must match db1's session, not db2's
        saved_session = hits[0].get("session_id", "")
        assert saved_session == db1.cfg["_session_id"], (
            f"stored session_id mismatch: got {saved_session!r}, "
            f"expected {db1.cfg['_session_id']!r}"
        )


# ── Layer 2: MCP subprocess (actual JSON-RPC) ────────────────────────────────

WORKTREE = Path(__file__).parent.parent  # …/adoring-moser-cffb55


def _is_redis_available() -> bool:
    try:
        import redis
        r = redis.Redis.from_url("redis://localhost:6379/0", decode_responses=False)
        r.ping()
        return True
    except Exception:
        return False


def _is_mcp_client_available() -> bool:
    try:
        from mcp.client.stdio import stdio_client  # noqa: F401
        from mcp import ClientSession               # noqa: F401
        return True
    except ImportError:
        return False


skip_subprocess = pytest.mark.skipif(
    not (_is_redis_available() and _is_mcp_client_available()),
    reason="Redis or mcp client SDK not available",
)


@skip_subprocess
class TestCrossSessionMCPSubprocess:
    """Start two real server sub-processes; verify cross-session recall via
    the MCP JSON-RPC stdio protocol."""

    @pytest.fixture(autouse=True)
    def _flush(self):
        import redis as redis_lib
        r = redis_lib.Redis.from_url("redis://localhost:6379/0", decode_responses=False)
        r.flushdb()
        yield
        r.flushdb()

    def _run(self, coro):
        return asyncio.run(coro)

    def test_save_in_session1_found_in_session2(self):
        self._run(self._async_cross_session_test())

    async def _async_cross_session_test(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        content = "MCP サブプロセス クロスセッション: ベクトルと BM25 のハイブリッド検索"
        query   = "MCP ハイブリッド検索 ベクトル BM25"

        env = {**os.environ}
        # No N3MC_SESSION_ID → each process gets a fresh UUID (simulates restart)
        env.pop("N3MC_SESSION_ID", None)

        server_params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "n3mc_mcp"],
            cwd=str(WORKTREE),
            env=env,
        )

        # ── Session 1: save ───────────────────────────────────────────────
        async with stdio_client(server_params) as (read1, write1):
            async with ClientSession(read1, write1) as s1:
                await s1.initialize()
                result = await s1.call_tool(
                    "save_memory", {"content": content}
                )
                text = result.content[0].text
                data = json.JSONDecoder().raw_decode(text)[0]
                assert data["saved"] is True, f"session-1 save failed: {data}"

        # brief pause so Redis finishes indexing
        await asyncio.sleep(0.3)

        # ── Session 2: search (fresh process = new _session_id) ──────────
        async with stdio_client(server_params) as (read2, write2):
            async with ClientSession(read2, write2) as s2:
                await s2.initialize()
                result = await s2.call_tool(
                    "search_memory", {"query": query}
                )
                text = result.content[0].text
                assert content in text, (
                    f"cross-session recall failed — saved content not found.\n"
                    f"search_memory returned:\n{text[:500]}"
                )
