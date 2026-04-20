"""
N3MemoryCore MCP **Lite** server — stdio transport.

Exposes five tools (same shape as the paid variant):
  search_memory, save_memory, list_memories, delete_memory, repair_memory

Storage is Redis Stack (RediSearch) with a 7d TTL per entry. No persistence.

Usage:
    python -m n3mc_mcp          # stdio server
    n3mc-mcp-lite              # via installed console script
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
    check_parent_exact_duplicate,
    count_memories,
    count_parent_docs,
    delete_memory as db_delete_memory,
    ensure_index,
    get_all_memories,
    get_parent_doc,
    get_redis_client,
    increment_access_counts,
    insert_memory,
    insert_parent_doc,
    list_parent_docs,
    ping,
    search_vector,
)
from .instructions import SERVER_INSTRUCTIONS
from .processor import (
    chunk_text,
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

# User-facing hint shown whenever Redis is unreachable. Covers BOTH
# the first-time install (`docker run …`) and the restart case
# (`docker start redis-stack`) so users who already created the
# container once do not hit the "container name already in use" error
# from blindly re-running the `docker run` command.
#
# NOTE: persistence flags (`--appendonly no --save ""`) are passed so a
# fresh container starts in ephemeral mode. The server additionally
# enforces this at startup via CONFIG SET (see _enforce_ephemeral),
# which is the source of truth — the docker flags are just a safety net
# for users reading the command before the server ever runs.
REDIS_STARTUP_HINT = (
    "Start Redis Stack (ephemeral mode required for Lite).\n"
    "  First time (creates the container):\n"
    "    docker run -d --name redis-stack -p 6379:6379 "
    'redis/redis-stack-server:latest --appendonly no --save ""\n'
    "  If the container already exists (just start it):\n"
    "    docker start redis-stack"
)


def _enforce_ephemeral(client) -> None:
    """Force Redis persistence off.

    The Lite build is a volatile scratchpad by design. Persistence is a
    feature of the paid variant; enabling it on Lite would erase the
    product line's differentiator. This runs on every server startup
    and is idempotent — if the user ran `CONFIG SET appendonly yes`
    between sessions, we undo it.

    Both axes must be disabled:
      - `appendonly no`   : AOF log off.
      - `save ""`         : RDB snapshot schedule cleared.
    We also best-effort flush any in-flight AOF via `BGREWRITEAOF` being
    skipped (there is no explicit "forget AOF on disk" command; the user
    must recreate the container for a clean wipe — which is the
    documented fix).

    If CONFIG SET is blocked (e.g. protected-mode, ACL), we warn loudly
    but continue: the tools still work, and the user sees exactly one
    clear message telling them their data may persist longer than
    advertised.
    """
    axes = (("appendonly", "no"), ("save", ""))
    for key, value in axes:
        try:
            client.config_set(key, value)
        except Exception as e:
            print(
                f"[N3MC-Lite] WARNING: could not disable Redis persistence "
                f"(CONFIG SET {key} {value!r} failed: {e}). "
                f"The Lite build requires ephemeral storage — please run a "
                f'dedicated redis-stack container with `--appendonly no --save ""` '
                f"and no ACL restrictions.",
                file=sys.stderr,
            )
            return

    # Sanity-check: confirm both axes are actually off. If the user has
    # a redis.conf that overrides CONFIG SET, we still want to tell them.
    try:
        ao = client.config_get("appendonly").get("appendonly", "")
        sv = client.config_get("save").get("save", "")
        if str(ao).lower() != "no" or str(sv).strip() != "":
            print(
                f"[N3MC-Lite] WARNING: Redis still reports persistence enabled "
                f"after CONFIG SET (appendonly={ao!r}, save={sv!r}). "
                f"The Lite build advertises 7d-TTL ephemeral storage; please "
                f"use a dedicated redis-stack container without a redis.conf "
                f"override.",
                file=sys.stderr,
            )
    except Exception:
        # CONFIG GET failure is non-fatal; we already warned above if relevant.
        pass


def _startup() -> None:
    """One-time initialization: config, Redis connection, index, model preload."""
    global _CONFIG, _CLIENT
    _CONFIG = load_config()

    redis_url = _CONFIG.get("redis_url", "redis://localhost:6379/0")
    _CLIENT = get_redis_client(redis_url)

    if not ping(_CLIENT):
        print(
            f"[N3MC-Lite] WARNING: cannot reach Redis at {redis_url}.\n"
            f"{REDIS_STARTUP_HINT}",
            file=sys.stderr,
        )
        return

    # Lite build invariant: persistence MUST be off. Do this before
    # ensure_index so the index itself is never dumped to disk.
    _enforce_ephemeral(_CLIENT)

    try:
        ensure_index(_CLIENT)
    except Exception as e:
        print(f"[N3MC-Lite] WARNING: failed to ensure RediSearch index: {e}", file=sys.stderr)

    # Preload embedding model so the first tool call isn't slow.
    try:
        from .processor import get_model
        get_model()
    except Exception as e:
        print(f"[N3MC-Lite] Model preload warning: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# MCP server definition
# ---------------------------------------------------------------------------
app: Server = Server(
    name="n3memorycore-lite",
    version="1.1.3",
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
                "NOTE: Lite memories expire 7d after they were saved."
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
                "Long text (>chunk_threshold chars) is automatically split into overlapping chunks. "
                "Lite entries expire after 7 days."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The memory content to save.",
                    },
                    "agent_name": {
                        "type": "string",
                        "description": "Optional agent identifier (e.g. 'claude', 'user').",
                    },
                    "owner_id": {
                        "type": "string",
                        "description": "Optional owner identifier. Must match the server's configured owner_id if provided.",
                    },
                    "importance": {
                        "type": "number",
                        "description": "Importance multiplier 0.5–2.0 (default 1.0). Higher values rank memories higher in search results.",
                        "minimum": 0.5,
                        "maximum": 2.0,
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
                "with the paid variant; the Lite build has no migrations."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------
def _tool_error(text: str) -> types.CallToolResult:
    """Build a proper MCP tool-error response.

    Returning a plain ``list[TextContent]`` with an "Error: ..." string
    inside is **not** the same thing as an MCP tool error: the client
    still sees ``isError=False`` and may treat it as a normal successful
    return — i.e. the LLM can paste the error text back as if it were
    data, or worse, swallow it silently. Setting ``isError=True``
    forces the client (and the LLM on the other side of it) to
    acknowledge the failure.

    This matters for the Lite build specifically: if Redis is down and
    ``save_memory`` silently "succeeds", the LLM will keep generating
    long content under the false assumption that it is being saved —
    which is exactly how the Shiranui test case lost its data.
    """
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        isError=True,
    )


@app.call_tool()
async def call_tool(
    name: str, arguments: dict
) -> list[types.TextContent] | types.CallToolResult:
    # Redis reachability gate — ping on every call, not just at startup,
    # so a mid-session Redis restart / crash is surfaced immediately
    # rather than silently corrupting the save/search contract.
    if _CLIENT is None or not ping(_CLIENT):
        return _tool_error(
            f"Error: Redis is not reachable. "
            f"save_memory and search_memory cannot proceed — "
            f"any content generated this turn will NOT persist.\n"
            f"{REDIS_STARTUP_HINT}"
        )
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
        return _tool_error(f"Unknown tool: {name}")
    except Exception as e:
        # Any exception from the tool implementations (Redis connection
        # reset mid-call, RediSearch index missing, embedding model
        # failure, etc.) is a hard error — surface it with isError=True
        # so the LLM cannot mistake it for a successful "no results"
        # return.
        return _tool_error(f"Error: {e}")


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

    raw = hybrid_search(_CLIENT, query, cfg)

    if not raw:
        return [types.TextContent(type="text", text="(no matching memories)")]

    # Collapse chunks belonging to the same parent doc into a single hit and
    # substitute the parent's full verbatim content. This lets long documents
    # surface exactly as they were saved (Option A).
    results: list[dict] = []
    seen_parents: dict[str, int] = {}
    chunk_hit_ids: list[str] = []  # for access-count bump (chunks only)
    for r in raw:
        parent_id = (r.get("parent_id") or "").strip()
        if parent_id:
            chunk_hit_ids.append(r.get("id", ""))
            if parent_id in seen_parents:
                # Already emitted this parent with a higher score — skip.
                continue
            parent = get_parent_doc(_CLIENT, parent_id)
            if parent is None:
                # Orphan chunk — parent expired or was deleted; fall through.
                seen_parents[parent_id] = len(results)
                results.append({
                    "id": r.get("id", ""),
                    "content": r.get("content", ""),
                    "score": r.get("score", 0),
                    "timestamp": r.get("timestamp", ""),
                })
                continue
            seen_parents[parent_id] = len(results)
            results.append({
                "id": parent_id,
                "content": parent.get("content", ""),
                "score": r.get("score", 0),
                "timestamp": parent.get("timestamp", "") or r.get("timestamp", ""),
                "is_parent": True,
            })
        else:
            results.append({
                "id": r.get("id", ""),
                "content": r.get("content", ""),
                "score": r.get("score", 0),
                "timestamp": r.get("timestamp", ""),
            })

    # Update access counts + extend TTL for top-K hits in a single round-trip.
    # Access counts feed the auto-importance boost in hybrid_search on future queries.
    top_k = int(_CONFIG.get("ttl_refresh_top_k", 5))
    hit_ids = [r["id"] for r in results[:top_k] if not r.get("is_parent")]
    # Also bump the actual chunks that fired so repeated parent hits strengthen
    # their ranking signal.
    hit_ids.extend([cid for cid in chunk_hit_ids[:top_k] if cid])
    if hit_ids and (
        _CONFIG.get("access_count_enabled", True)
        or _CONFIG.get("ttl_refresh_on_search", True)
    ):
        ttl = (
            int(_CONFIG.get("ttl_seconds", 604800))
            if _CONFIG.get("ttl_refresh_on_search", True)
            else None
        )
        increment_access_counts(_CLIENT, hit_ids, ttl_seconds=ttl)

    lines = ["# N3MemoryCore Lite — Relevant Memories", ""]
    for r in results:
        score = r.get("score", 0)
        content = r.get("content", "")
        ts = (r.get("timestamp") or "")[:10]
        rid = r.get("id", "")
        tag = " [doc]" if r.get("is_parent") else ""
        lines.append(f"- [{score:.4f}] ({ts}){tag} {content}  _({rid})_")
    return [types.TextContent(type="text", text="\n".join(lines))]


def _gen_id() -> str:
    try:
        from uuid_utils import uuid7 as _gen_uuid7
        return str(_gen_uuid7())
    except Exception:
        return str(uuid.uuid4())


def _save_one(
    text: str,
    agent_name: Any,
    ttl_seconds: int,
    importance: float,
    parent_id: str | None = None,
    skip_dedup: bool = False,
) -> dict:
    """Save a single purified text entry. Returns a status dict.

    When ``parent_id`` is set, the row is stored as a chunk of a parent doc
    and dedup guards are bypassed (overlapping chunks are near-duplicates by
    design).
    """
    if not skip_dedup and check_exact_duplicate(_CLIENT, text):
        return {"status": "duplicate", "saved": False}

    qvec = None
    try:
        qvec = embed_passage(text)
        if not skip_dedup:
            vec_results = search_vector(
                _CLIENT, qvec, k=1, owner_id=_CONFIG.get("owner_id"),
            )
            if vec_results:
                top_cos = cosine_sim_from_distance(vec_results[0][1])
                if top_cos >= _CONFIG.get("dedup_threshold", 0.95):
                    return {
                        "status": "near_duplicate",
                        "saved": False,
                        "similarity": round(top_cos, 4),
                    }
    except Exception:
        qvec = None

    record_id = _gen_id()
    ts = datetime.now(tz=timezone.utc).isoformat()
    insert_memory(
        _CLIENT,
        record_id,
        text,
        ts,
        _CONFIG["owner_id"],
        qvec,
        _CONFIG.get("local_id"),
        agent_name,
        _SESSION_ID,
        ttl_seconds=ttl_seconds,
        importance=importance,
        parent_id=parent_id,
        skip_sha_guard=bool(parent_id),
    )
    return {"status": "ok", "saved": True, "id": record_id, "ttl_seconds": ttl_seconds}


def _tool_save(args: dict) -> list[types.TextContent]:
    content = (args.get("content") or "").strip()
    if not content:
        return [types.TextContent(type="text", text="save_memory: empty content")]

    caller_owner_id = args.get("owner_id")
    if caller_owner_id is not None and caller_owner_id != _CONFIG.get("owner_id"):
        return [types.TextContent(
            type="text",
            text='{"status":"error","saved":false,"reason":"owner_id mismatch"}',
        )]

    agent_name = args.get("agent_name")
    ttl_seconds = int(_CONFIG.get("ttl_seconds", 604800))
    importance = float(args.get("importance") or 1.0)
    importance = max(0.5, min(2.0, importance))

    text = purify(content)

    # Auto-chunk long content instead of rejecting or truncating.
    chunk_threshold = int(_CONFIG.get("chunk_threshold", 400))
    chunk_overlap = int(_CONFIG.get("chunk_overlap", 100))
    chunks = chunk_text(text, chunk_size=chunk_threshold, overlap=chunk_overlap)

    if len(chunks) == 1:
        result = _save_one(chunks[0], agent_name, ttl_seconds, importance)
        import json as _json
        return [types.TextContent(type="text", text=_json.dumps(result))]

    # Multi-chunk path: persist a parent full-text doc plus overlapping chunks.
    # The parent guarantees verbatim recall; the chunks drive hybrid retrieval.
    import json as _json

    existing_parent = check_parent_exact_duplicate(_CLIENT, text)
    if existing_parent is not None:
        return [types.TextContent(type="text", text=_json.dumps({
            "status": "duplicate",
            "saved": False,
            "parent_id": existing_parent,
        }))]

    parent_id = _gen_id()
    parent_ts = datetime.now(tz=timezone.utc).isoformat()
    insert_parent_doc(
        _CLIENT,
        parent_id,
        text,
        parent_ts,
        _CONFIG["owner_id"],
        local_id=_CONFIG.get("local_id"),
        agent_name=agent_name,
        session_id=_SESSION_ID,
        chunk_count=len(chunks),
        ttl_seconds=ttl_seconds,
    )

    saved_ids: list[str] = []
    skipped = 0
    for chunk in chunks:
        r = _save_one(
            chunk, agent_name, ttl_seconds, importance,
            parent_id=parent_id, skip_dedup=True,
        )
        if r.get("saved"):
            saved_ids.append(r["id"])
        else:
            skipped += 1

    return [types.TextContent(type="text", text=_json.dumps({
        "status": "ok",
        "saved": True,
        "parent_id": parent_id,
        "chunks": len(chunks),
        "saved_count": len(saved_ids),
        "skipped_count": skipped,
        "ids": saved_ids,
        "ttl_seconds": ttl_seconds,
    }))]


def _tool_list(args: dict) -> list[types.TextContent]:
    limit = int(args.get("limit") or 20)
    owner_id = _CONFIG.get("owner_id")
    # Standalone memories exclude chunks by default; parent docs are fetched
    # separately and interleaved as single entries.
    standalone = get_all_memories(_CLIENT, limit=limit, owner_id=owner_id)
    parents = list_parent_docs(_CLIENT, limit=limit, owner_id=owner_id)

    merged: list[dict] = []
    for p in parents:
        merged.append({
            "id": p["id"],
            "timestamp": p.get("timestamp", ""),
            "agent_name": p.get("agent_name", ""),
            "content": p.get("content", ""),
            "is_parent": True,
            "chunk_count": p.get("chunk_count", 0),
        })
    for r in standalone:
        merged.append(dict(r, is_parent=False))

    def _sort_key(row: dict) -> str:
        return row.get("timestamp") or ""
    merged.sort(key=_sort_key, reverse=True)
    merged = merged[:limit]

    total = count_memories(_CLIENT, owner_id=owner_id) + count_parent_docs(
        _CLIENT, owner_id=owner_id
    )

    if not merged:
        return [types.TextContent(type="text", text="(no memories stored)")]

    lines = [f"# Recent memories ({len(merged)} of {total}) — Lite: 7d TTL", ""]
    for r in merged:
        ts = (r.get("timestamp") or "")[:19]
        agent = r.get("agent_name") or "-"
        content = (r.get("content") or "")[:120]
        tag = f" [doc×{r.get('chunk_count',0)}]" if r.get("is_parent") else ""
        lines.append(f"- `{r.get('id','')}` {ts} [{agent}]{tag} {content}")
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
    """Lite variant: simply re-ensure the RediSearch index exists."""
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
