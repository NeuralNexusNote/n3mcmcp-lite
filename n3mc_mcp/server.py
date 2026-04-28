"""
MCP stdio server — 6 tools + startup sequence.

Spec §4.1  transport: stdio
Spec §4.2  initialize: serverInfo, capabilities, instructions
Spec §4.3  tools: search_memory, save_memory, list_memories,
                  delete_memory, delete_memories_by_session, repair_memory
Spec §3.9  startup: load_config → connect → enforce_ephemeral → ensure_index
                    → preload embedding model (fd-level redirect)
Spec §11   every tool response ends with a short auto-save reminder
"""
import sys

# ── Spec §4.1 stdio UTF-8 reconfigure ────────────────────────────────────────
# Must run BEFORE any other import that might write to stdout/stderr.
# On Windows, Python defaults stdin/stdout/stderr to the active code page
# (cp932 in Japanese locales), which mangles UTF-8 JSON-RPC bytes coming
# from MCP clients and turns Japanese / emoji / non-ASCII content into
# mojibake — and worse, save paths can silently drop a record when the
# decoder produces lone surrogate halves. Reconfiguring all three streams
# to UTF-8 at module import time is the same belt the Free build wears
# (n3mc_hook.py / n3memory.py / n3mc_stop_hook.py).
for _stream_name in ("stdin", "stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

import asyncio
import json
import os
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

# ── §11 turn-end nudges ──────────────────────────────────────────────────────
# MCP has no Stop/UserPromptSubmit hook equivalent, so the only way to
# re-anchor the auto-save discipline mid-turn is to append a short reminder
# to each tool response.  Three variants match the semantic context:

_NUDGE_AFTER_SEARCH = (
    "\n\n---\n_Reminder: before closing this turn, call `save_memory` "
    "to persist the user's intent (paraphrased) and any substantive "
    "output you generate. If Pro (`n3mc-longtermmemory`) is also "
    "connected, route long-lived artifacts there; this server is for "
    "transient task scratch (7-day TTL). Do not skip the save just "
    "because no memory hit was returned above._"
)
_NUDGE_AFTER_SAVE = (
    "\n\n---\n_Reminder: if this turn produced multiple distinct facts or a "
    "long verbatim artifact, make sure each was saved — one "
    "`save_memory` call per fact, or one full-text call for verbatim "
    "long content (>400 chars). Duplicates are auto-rejected, so err "
    "on the side of saving._"
)
_NUDGE_GENERIC = (
    "\n\n---\n_Reminder: before closing this turn, call `save_memory` "
    "to persist the user's intent (paraphrased) and any substantive "
    "output you produced. Lite entries expire 7 days after save, so "
    "saving more is safer than saving less._"
)


# ── tool registration (spec §4.3) ────────────────────────────────────────────

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
                        "description": "Max results (default: config search_result_limit).",
                        "minimum": 1,
                        "maximum": 100,
                    },
                    "session_id": {
                        "type": "string",
                        "description": (
                            "Optional project/task grouping key. Rows whose stored "
                            "session_id matches this value are boosted in ranking "
                            "(b_session_match=1.0); non-matching rows are dampened "
                            "(b_session_mismatch=0.6). Pass the same session_id used "
                            "at save time to surface that project's memories above "
                            "unrelated rows. Leave blank to use the server default "
                            "(N3MC_SESSION_ID env var, or per-process UUIDv4)."
                        ),
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="save_memory",
            description=(
                "Save a memory entry (Lite: 7-day TTL). "
                "Auto-deduplicates exact and near-duplicate content. "
                "Long content (>chunk_threshold chars) is chunked with a "
                "parent-document for verbatim recall."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Memory content to save.",
                    },
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
                        "description": "Importance weight 0.5–2.0 (default 1.0).",
                    },
                    "session_id": {
                        "type": "string",
                        "description": (
                            "Optional project/task grouping key. Stored on the row "
                            "and used as: (a) the ranking key for search_memory's "
                            "b_session boost (match=1.0 / mismatch=0.6), and "
                            "(b) the filter for delete_memories_by_session. "
                            "Pass the same value across all calls for one project. "
                            "Leave blank to use the server default "
                            "(N3MC_SESSION_ID env var, or per-process UUIDv4)."
                        ),
                    },
                },
                "required": ["content"],
            },
        ),
        Tool(
            name="list_memories",
            description=(
                "List stored memories newest first. "
                "Parent documents shown with [doc×N] tag."
            ),
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
                "If the ID is a parent document (doc:<uuid>), cascades to all child chunks."
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
                "Delete every memory (singles, parent docs, child chunks, sha guards) "
                "whose session_id matches. Scoped to the configured owner. "
                "Use this to wrap up a finished project or reset a polluted session "
                "before TTL expiry. IRREVERSIBLE — confirm session_id with the user first."
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
            description=(
                "Re-ensure the RediSearch index (idempotent). "
                "Returns {status, message}."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


# ── tool dispatch (spec §4.3 / §4.4) ────────────────────────────────────────

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
                    tag     = r.get("_tag", "")
                    tag_str = f"{tag} " if tag else ""
                    score   = r.get("score", 0.0)
                    mem_id  = r.get("id", "")
                    ts      = r.get("timestamp", "")[:19]
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
            return [TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False) + _NUDGE_AFTER_SAVE,
            )]

        elif name == "list_memories":
            if not _db._ok:
                raise RuntimeError(f"memory backend unreachable. {_DOCKER_HINT}")
            limit_raw = arguments.get("limit", 20)
            memories  = _db.list_memories(int(limit_raw) if limit_raw else 20)
            if not memories:
                text = "_No memories stored._"
            else:
                lines = []
                for m in memories:
                    tag     = m.get("tag", "")
                    tag_str = f"{tag} " if tag else ""
                    ts      = m.get("timestamp", "")[:19]
                    lines.append(f"**{tag_str}[{m['id']}]** {ts}\n{m.get('content', '')}")
                text = "\n\n---\n\n".join(lines)
            return [TextContent(type="text", text=text + _NUDGE_GENERIC)]

        elif name == "delete_memory":
            result = _db.delete_memory(arguments.get("id", ""))
            return [TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False) + _NUDGE_GENERIC,
            )]

        elif name == "delete_memories_by_session":
            result = _db.delete_by_session(arguments.get("session_id", ""))
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


# ── startup (spec §3.9) ──────────────────────────────────────────────────────

async def _main() -> None:
    global _db
    cfg = load_config()

    # session_id fallback resolution (spec §3.1 / §3.6):
    #   1. N3MC_SESSION_ID env var
    #   2. Per-process UUIDv4 (fresh each startup)
    #
    # Lite 1.5.0+ applies the same b_session ranking as Pro (match=1.0,
    # mismatch=0.6). Rows from a previous restart's UUID receive
    # b_session_mismatch dampening unless pinned via env var or per-call arg.
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

    # Spec §3.9 step 4: preload embedding model BEFORE entering the stdio_server
    # context.  sentence_transformers / HuggingFace write progress bars and HTTP
    # logs to stdout.  Once stdio_server is active fd 1 is the JSON-RPC channel —
    # any stray bytes destroy protocol framing (observed symptom: client hangs
    # forever waiting for the first response).
    #
    # `contextlib.redirect_stdout(sys.stderr)` only diverts Python's sys.stdout
    # wrapper; C extensions, inherited FDs, and subprocess writes still go to
    # fd 1.  We therefore redirect at the OS level via os.dup2(2, 1) for the
    # duration of the preload, then restore fd 1 before mcp.run() takes over.
    #
    # A background-thread preload is unsafe: while the worker is mid-load with
    # fd 1 redirected, the asyncio main thread can begin writing JSON-RPC responses
    # that land on stderr — disconnecting the client.  Synchronous + fd-level is
    # the only safe combination.
    try:
        saved_fd = os.dup(1)
        try:
            os.dup2(2, 1)
            get_model()
        finally:
            os.dup2(saved_fd, 1)
            os.close(saved_fd)
    except Exception as e:
        print(f"[n3mc] model preload failed (will lazy-retry): {e}", file=sys.stderr)

    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, init_opts)


def run() -> None:
    asyncio.run(_main())
