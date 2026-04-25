import asyncio
import json
import os
import sys
import uuid
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .config import load_config
from .database import Database, _DOCKER_HINT
from .instructions import INSTRUCTIONS
from .processor import get_model

_db: Database = None

app = Server("n3mc-workingmemory")

# Short turn-end reminders appended to tool responses. The MCP server
# cannot force the LLM to call save_memory; these nudges arrive in the
# tool-response text mid-turn (when the LLM is most likely to act on
# them) and try to re-anchor the auto-save discipline that the static
# `instructions` field is too easily ignored to enforce on its own.
# See README §"MCP compliance is probabilistic" for the design rationale.
_NUDGE_AFTER_SEARCH = (
    "\n\n---\n_Reminder: before closing this turn, call `save_memory` "
    "to persist the user's intent (paraphrased) and any substantive "
    "output you generate. If Pro (`n3mc-longtermmemory`) is also "
    "connected, route long-lived artifacts there; this server is for "
    "transient task scratch (7-day TTL). Do not skip the save just "
    "because no memory hit was returned above._"
)
_NUDGE_AFTER_SAVE = (
    "\n_Reminder: if this turn produced multiple distinct facts or a "
    "long verbatim artifact, make sure each was saved — one "
    "`save_memory` call per fact, or one full-text call for verbatim "
    "long content (>400 chars). Duplicates are auto-rejected, so err "
    "on the side of saving._"
)
# Spec §11 / §4.3: every tool response ends with a short reminder that
# re-anchors the auto-save discipline mid-turn. list/delete/repair tools
# don't directly carry retrieval or save semantics, but the spec is
# explicit ("each tool's response ends with a short reminder"), so we
# append a generic nudge here too.
_NUDGE_GENERIC = (
    "\n\n---\n_Reminder: before closing this turn, call `save_memory` "
    "to persist the user's intent (paraphrased) and any substantive "
    "output you produced. Lite entries expire 7 days after save, so "
    "saving more is safer than saving less._"
)


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search_memory",
            description=(
                "Hybrid (vector + BM25) search over stored memories. "
                "Call this at the start of every user turn. "
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
                    "session_id": {
                        "type": "string",
                        "description": (
                            "Optional project/task grouping key. Memories whose "
                            "stored session_id matches this value receive a ranking "
                            "boost (b_session_match=1.0); non-matching rows are "
                            "dampened (b_session_mismatch=0.6). Pass the same "
                            "session_id used at save time to surface that "
                            "project's memories above unrelated rows in the same "
                            "Redis instance. Leave blank to use the server's "
                            "default (N3MC_SESSION_ID env var, or per-process "
                            "UUIDv4 — same fallback chain as save_memory)."
                        ),
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="save_memory",
            description=(
                "Save a memory entry (Lite: 7-day TTL). Automatically deduplicates exact and "
                "near-duplicate content. Long content (>chunk_threshold chars) is chunked with "
                "a parent-document for verbatim recall."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Memory content to save."},
                    "agent_name": {
                        "type": "string",
                        "description": "Agent display name (e.g. 'claude-code').",
                    },
                    "owner_id": {
                        "type": "string",
                        "description": "Owner UUID override (must match server config).",
                    },
                    "importance": {
                        "type": "number",
                        "description": "Importance weight 0.5-2.0 (default 1.0).",
                    },
                    "session_id": {
                        "type": "string",
                        "description": (
                            "Optional project/task grouping key. Stored on the row "
                            "and used both as the filter for "
                            "delete_memories_by_session and as the ranking key for "
                            "search_memory's b_session boost (match=1.0, "
                            "mismatch=0.6). Pass the same value across all calls "
                            "for one project so they cluster together at recall "
                            "time. Leave blank to use the server's default "
                            "(N3MC_SESSION_ID env var, or per-process UUIDv4)."
                        ),
                    },
                },
                "required": ["content"],
            },
        ),
        Tool(
            name="list_memories",
            description="List stored memories newest first. Parents shown with [doc×N] tag.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20).",
                    },
                },
            },
        ),
        Tool(
            name="delete_memory",
            description=(
                "Delete a memory by ID. "
                "If ID is a parent document (doc:<uuid>), cascades to all child chunks."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Memory ID or parent document ID to delete.",
                    },
                },
                "required": ["id"],
            },
        ),
        Tool(
            name="delete_memories_by_session",
            description=(
                "Delete every memory (singles, parent docs, child chunks, sha index keys) "
                "whose session_id matches. Scoped to the configured owner. Use this to "
                "wrap up a finished project or reset a polluted session before TTL expiry."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "session_id whose memories should be removed.",
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="repair_memory",
            description="Re-ensure the RediSearch index (idempotent). Returns {status, message}.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    try:
        if name == "search_memory":
            if not _db._ok:
                raise RuntimeError(f"memory backend unreachable. {_DOCKER_HINT}")
            results = _db.search_memory(
                arguments.get("query", ""),
                arguments.get("limit"),
                arguments.get("session_id", ""),
            )
            if not results:
                text = "_No memories found._"
            else:
                lines: list[str] = []
                for r in results:
                    tag = r.get("_tag", "")
                    tag_str = f"{tag} " if tag else ""
                    score = r.get("score", 0.0)
                    mem_id = r.get("id", "")
                    ts = r.get("timestamp", "")[:19]
                    lines.append(
                        f"### {tag_str}[{mem_id}] score={score:.3f} {ts}\n{r['content']}"
                    )
                text = "\n\n---\n\n".join(lines)
            return [TextContent(type="text", text=text + _NUDGE_AFTER_SEARCH)]

        elif name == "save_memory":
            result = _db.save_memory(
                arguments.get("content", ""),
                arguments.get("agent_name", ""),
                arguments.get("owner_id", ""),
                float(arguments.get("importance", 1.0)),
                arguments.get("session_id", ""),
            )
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False) + _NUDGE_AFTER_SAVE)]

        elif name == "delete_memories_by_session":
            result = _db.delete_by_session(arguments.get("session_id", ""))
            return [TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False) + _NUDGE_GENERIC,
            )]

        elif name == "list_memories":
            if not _db._ok:
                raise RuntimeError(f"memory backend unreachable. {_DOCKER_HINT}")
            limit_raw = arguments.get("limit", 20)
            memories = _db.list_memories(int(limit_raw) if limit_raw else 20)
            if not memories:
                text = "_No memories stored._"
            else:
                lines = []
                for m in memories:
                    tag = m.get("tag", "")
                    tag_str = f"{tag} " if tag else ""
                    ts = m.get("timestamp", "")[:19]
                    preview = m.get("content", "")
                    lines.append(f"**{tag_str}[{m['id']}]** {ts}\n{preview}")
                text = "\n\n---\n\n".join(lines)
            return [TextContent(type="text", text=text + _NUDGE_GENERIC)]

        elif name == "delete_memory":
            result = _db.delete_memory(arguments.get("id", ""))
            return [TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False) + _NUDGE_GENERIC,
            )]

        elif name == "repair_memory":
            result = _db.repair_memory()
            return [TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False) + _NUDGE_GENERIC,
            )]

        else:
            return [TextContent(type="text", text=f"Error: unknown tool '{name}'")]

    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


async def _main() -> None:
    global _db
    cfg = load_config()
    # Resolve the auto-fallback session_id used when a tool call does
    # not pass `session_id` explicitly. Pro spec §3.1 / §3.6 fallback
    # order, mirrored here for surface compatibility:
    #   1. `N3MC_SESSION_ID` env var
    #   2. Per-process UUID (fresh each startup)
    #
    # Lite has no `b_session` ranking factor (see config.py), so the
    # value is only used as a default tag for save_memory and as the
    # default scope for delete_memories_by_session. A per-process UUID
    # is therefore safe — it does not affect cross-restart retrieval.
    cfg["_session_id"] = (
        os.environ.get("N3MC_SESSION_ID", "").strip() or str(uuid.uuid4())
    )

    _db = Database(cfg)
    _db.connect()
    _db.enforce_ephemeral()
    _db.ensure_index()

    init_opts = app.create_initialization_options()
    try:
        init_opts.instructions = INSTRUCTIONS
    except AttributeError:
        pass

    # Synchronously preload the embedding model BEFORE entering the
    # stdio_server context. sentence_transformers / transformers /
    # huggingface_hub print progress bars, "BertModel LOAD REPORT",
    # HTTP request logs, and HF auth warnings to **stdout** during
    # load. Once stdio_server is active, fd 1 is the JSON-RPC
    # channel and any stray bytes destroy protocol framing — the
    # observed symptom is the client hanging forever ("Processing
    # request of type CallToolRequest" logged to stderr but no
    # response ever reaches the client over fd 1).
    #
    # `contextlib.redirect_stdout(sys.stderr)` only diverts Python's
    # `sys.stdout` wrapper; C extensions, inherited file descriptors
    # and subprocess writes still go to fd 1. We therefore redirect
    # at the OS level via `os.dup2(2, 1)` for the duration of the
    # preload, then restore fd 1 before mcp.run() takes over.
    #
    # **Synchronous + fd-level is the only safe combination.** A
    # background-thread preload (e.g. via run_in_executor) is
    # actively wrong: while the worker thread is mid-load and fd 1
    # has been redirected, the asyncio main thread can begin writing
    # JSON-RPC responses and they land on stderr, disconnecting the
    # client. See N3MC Pro spec §3.9 step 4 for the full design
    # rationale; this is the same recipe Pro uses, ported to Lite.
    #
    # Cost: `initialize` response is delayed by ~17 s on a cached HF
    # cache (longer on first download). This is well within Claude
    # Code's MCP startup timeout in practice. If the load fails
    # (offline / no HF cache), tool handlers fall back to the lazy
    # path in `processor.get_model()` which uses a Python-level
    # redirect (race-free because handlers are serialised by the
    # asyncio main thread).
    try:
        saved_stdout_fd = os.dup(1)
        try:
            os.dup2(2, 1)
            get_model()
        finally:
            os.dup2(saved_stdout_fd, 1)
            os.close(saved_stdout_fd)
    except Exception as e:
        print(f"[n3mc] model preload failed (will lazy-retry): {e}", file=sys.stderr)

    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, init_opts)


def run() -> None:
    asyncio.run(_main())
