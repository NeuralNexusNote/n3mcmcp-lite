"""
N3MemoryCore MCP **Trial** server — stdio transport.

Exposes five tools (same shape as the paid variant):
  search_memory, save_memory, list_memories, delete_memory, repair_memory

Storage is Redis Stack (RediSearch) with a 24h TTL per entry. No persistence.

Usage:
    python -m n3mc_mcp          # stdio server
    n3mc-mcp-trial              # via installed console script
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from typing import Any

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .config import load_config
from .database import (
    check_exact_duplicate,
    count_memories,
    delete_memory as db_delete_memory,
    ensure_index,
    get_all_memories,
    get_redis_client,
    insert_memory,
    ping,
    search_vector,
)
from .instructions import SERVER_INSTRUCTIONS
from .processor import (
    cosine_sim_from_distance,
    embed_passage,
    hybrid_search,
    purify,
)

# ---------------------------------------------------------------------------
# Server state
# ---------------------------------------------------------------------------
_SESSION_ID = str(uuid.uuid4())
_CONFIG: dict[str, Any] = {}
_CLIENT = None  # redis.Redis


def _startup() -> None:
    """One-time initialization: config, Redis connection, index, model preload."""
    global _CONFIG, _CLIENT
    _CONFIG = load_config()

    redis_url = _CONFIG.get("redis_url", "redis://localhost:6379/0")
    _CLIENT = get_redis_client(redis_url)

    if not ping(_CLIENT):
        print(
            f"[N3MC-Trial] WARNING: cannot reach Redis at {redis_url}. "
            f"Start Redis Stack first:\n"
            f"    docker run -p 6379:6379 redis/redis-stack-server:latest",
            file=sys.stderr,
        )
        return

    try:
        ensure_index(_CLIENT)
    except Exception as e:
        print(f"[N3MC-Trial] WARNING: failed to ensure RediSearch index: {e}", file=sys.stderr)

    # Preload embedding model so the first tool call isn't slow.
    try:
        from .processor import get_model
        get_model()
    except Exception as e:
        print(f"[N3MC-Trial] Model preload warning: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# MCP server definition
# ---------------------------------------------------------------------------
app: Server = Server(
    name="n3memorycore-trial",
    version="1.0.0-trial",
    instructions=SERVER_INSTRUCTIONS,
)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------
@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="search_memory",
            description=(
                "Hybrid (vector + BM25) search over stored memories. "
                "Call this at the start of every user turn with a concise "
                "query representing the user's intent. "
                "NOTE: Trial memories expire 24h after they were saved."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (natural language or keywords).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default: config search_result_limit).",
                        "minimum": 1,
                        "maximum": 100,
                    },
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="save_memory",
            description=(
                "Persist a short memory entry (50-200 chars ideal). "
                "Call once per distinct fact. Exact and near-duplicates are auto-rejected. "
                "Trial entries expire after 24 hours."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The memory content to save.",
                    },
                    "agent_id": {
                        "type": "string",
                        "description": "Optional agent identifier (e.g. 'claude', 'user').",
                    },
                },
                "required": ["content"],
            },
        ),
        types.Tool(
            name="list_memories",
            description="List the most recent memory entries, newest first.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max entries to return (default 20).",
                        "minimum": 1,
                        "maximum": 500,
                    },
                },
            },
        ),
        types.Tool(
            name="delete_memory",
            description="Delete a specific memory entry by its id (UUID).",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "The memory record id to delete.",
                    },
                },
                "required": ["id"],
            },
        ),
        types.Tool(
            name="repair_memory",
            description=(
                "Re-create the RediSearch index if missing. Kept for parity "
                "with the paid variant; the Trial build has no migrations."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------
@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if _CLIENT is None or not ping(_CLIENT):
        return [types.TextContent(
            type="text",
            text=(
                "Error: Redis is not reachable. Start Redis Stack with:\n"
                "  docker run -p 6379:6379 redis/redis-stack-server:latest"
            ),
        )]
    try:
        if name == "search_memory":
            return _tool_search(arguments)
        if name == "save_memory":
            return _tool_save(arguments)
        if name == "list_memories":
            return _tool_list(arguments)
        if name == "delete_memory":
            return _tool_delete(arguments)
        if name == "repair_memory":
            return _tool_repair(arguments)
        return [types.TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as e:
        return [types.TextContent(type="text", text=f"Error: {e}")]


# ---------------------------------------------------------------------------
# Individual tool implementations
# ---------------------------------------------------------------------------
def _tool_search(args: dict) -> list[types.TextContent]:
    query = (args.get("query") or "").strip()
    if not query:
        return [types.TextContent(type="text", text="search_memory: empty query")]

    max_chars = _CONFIG.get("search_query_max_chars", 2000)
    query = query[:max_chars]

    limit_override = args.get("limit")
    cfg = dict(_CONFIG)
    if limit_override:
        cfg["search_result_limit"] = int(limit_override)

    results = hybrid_search(_CLIENT, query, cfg)

    if not results:
        return [types.TextContent(type="text", text="(no matching memories)")]

    lines = ["# N3MemoryCore Trial — Relevant Memories", ""]
    for r in results:
        score = r.get("score", 0)
        content = r.get("content", "")
        ts = r.get("timestamp", "")[:10]
        rid = r.get("id", "")
        lines.append(f"- [{score:.4f}] ({ts}) {content}  _({rid})_")
    return [types.TextContent(type="text", text="\n".join(lines))]


def _tool_save(args: dict) -> list[types.TextContent]:
    content = (args.get("content") or "").strip()
    if not content:
        return [types.TextContent(type="text", text="save_memory: empty content")]

    agent_id = args.get("agent_id")
    ttl_seconds = int(_CONFIG.get("ttl_seconds", 86400))
    text = purify(content)

    # Exact dedup (sha1 key)
    if check_exact_duplicate(_CLIENT, text):
        return [types.TextContent(type="text", text='{"status":"duplicate","saved":false}')]

    # Vector (near-duplicate) dedup
    qvec = None
    try:
        qvec = embed_passage(text)
        vec_results = search_vector(
            _CLIENT, qvec, k=1, owner_id=_CONFIG.get("owner_id"),
        )
        if vec_results:
            top_cos = cosine_sim_from_distance(vec_results[0][1])
            if top_cos >= _CONFIG.get("dedup_threshold", 0.95):
                return [types.TextContent(
                    type="text",
                    text=f'{{"status":"near_duplicate","saved":false,"similarity":{top_cos:.4f}}}',
                )]
    except Exception:
        qvec = None

    try:
        from uuid_utils import uuid7 as _gen_uuid7
        record_id = str(_gen_uuid7())
    except Exception:
        record_id = str(uuid.uuid4())

    ts = datetime.now(tz=timezone.utc).isoformat()

    insert_memory(
        _CLIENT,
        record_id,
        text,
        ts,
        _CONFIG["owner_id"],
        qvec,
        _CONFIG.get("local_id"),
        agent_id,
        _SESSION_ID,
        ttl_seconds=ttl_seconds,
    )

    return [types.TextContent(
        type="text",
        text=f'{{"status":"ok","saved":true,"id":"{record_id}","ttl_seconds":{ttl_seconds}}}',
    )]


def _tool_list(args: dict) -> list[types.TextContent]:
    limit = int(args.get("limit") or 20)
    owner_id = _CONFIG.get("owner_id")
    rows = get_all_memories(_CLIENT, limit=limit, owner_id=owner_id)
    total = count_memories(_CLIENT, owner_id=owner_id)

    if not rows:
        return [types.TextContent(type="text", text="(no memories stored)")]

    lines = [f"# Recent memories ({len(rows)} of {total}) — Trial: 24h TTL", ""]
    for r in rows:
        ts = (r.get("timestamp") or "")[:19]
        agent = r.get("agent_id") or "-"
        content = (r.get("content") or "")[:120]
        lines.append(f"- `{r.get('id','')}` {ts} [{agent}] {content}")
    return [types.TextContent(type="text", text="\n".join(lines))]


def _tool_delete(args: dict) -> list[types.TextContent]:
    record_id = (args.get("id") or "").strip()
    if not record_id:
        return [types.TextContent(type="text", text="delete_memory: missing id")]

    ok = db_delete_memory(_CLIENT, record_id)

    if ok:
        return [types.TextContent(type="text", text=f'{{"status":"ok","deleted":"{record_id}"}}')]
    return [types.TextContent(type="text", text=f'{{"status":"not_found","id":"{record_id}"}}')]


def _tool_repair(_args: dict) -> list[types.TextContent]:
    """Trial variant: simply re-ensure the RediSearch index exists."""
    try:
        ensure_index(_CLIENT)
    except Exception as e:
        return [types.TextContent(type="text", text=f'{{"status":"error","message":"{e}"}}')]
    return [types.TextContent(type="text", text='{"status":"ok","message":"index ensured"}')]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def run() -> None:
    _startup()
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )
