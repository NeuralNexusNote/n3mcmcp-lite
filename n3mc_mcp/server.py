"""
MCP stdio server — 7 tools + startup sequence.

Spec §4.1  transport: stdio
Spec §4.2  initialize: serverInfo, capabilities, instructions
Spec §4.3  tools: search_memory, save_memory, list_memories,
                  delete_memory, delete_memories_by_session, repair_memory
Spec §4.3.1 Retrieval Extensions:
            search_memory += since / until
            save_memory   += turn_id
            recall_thread (new 7th tool)
Spec §3.9  startup: load_config → connect → enforce_ephemeral → ensure_index
                    → stdio loop starts → embedding model loads in background
Spec §3.14 search_memory output: notices, version sentence, saved= local time
Spec §11   every tool response ends with a short auto-save reminder
"""
import sys

# ── Spec §4.1 stdio UTF-8 reconfigure ────────────────────────────────────────
# Must run BEFORE any other import that might write to stdout/stderr.
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
from datetime import datetime, timezone
from typing import Any, Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import __version__
from .config import load_config
from .database import Database, _CONN_ERRORS
from .instructions import INSTRUCTIONS
from .processor import (
    get_model, import_backend, model_unavailable_message, purify, set_model_name,
)

_db: Database = None

# §4.2: serverInfo.version = package __version__; instructions advertised.
app = Server("n3mc-workingmemory", version=__version__, instructions=INSTRUCTIONS)

# §6 notices are prepended to the FIRST successful search_memory output only.
_notices_shown = False

_VERSION_SENTENCE = (
    "_If several versions of the same content appear, the one with the "
    "newest `saved` time is the current one._"
)


# ── §3.14 search_memory rendering ────────────────────────────────────────────

def _fmt_saved(timestamp: str, timestamp_epoch: Optional[float] = None) -> str:
    """Stored UTC time → server-local time with its offset (spec §3.14).

    "2026-11-13T01:34:00+00:00" → "2026-11-13 10:34 (UTC+0900)" on a JST host.
    Falls back to `timestamp_epoch`, then to the raw string, then "?".
    """
    dt = None
    if timestamp:
        try:
            dt = datetime.fromisoformat(timestamp.strip().replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            dt = None
    if dt is None and timestamp_epoch:
        try:
            dt = datetime.fromtimestamp(float(timestamp_epoch), tz=timezone.utc)
        except Exception:
            dt = None
    if dt is None:
        return (timestamp or "")[:19] or "?"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M (UTC%z)")


def _render_search(results: list[dict], notices: Optional[list[str]] = None) -> str:
    """Build the search_memory markdown (spec §3.14 output format).

        > ⚠️ N3MC notice: <message>        (one line per pending notice)

        _If several versions ..._           (once, when there is >= 1 hit)

        ### [doc×N] [<id>] turn=<turn_id> score=0.812 saved=<local> (UTC±hhmm)
        <content>

    `turn=` is omitted when the row has no turn_id; `[doc×N]` only for
    parent documents. Hits are separated by `---`.
    """
    parts: list[str] = []
    if notices:
        parts.append("\n".join(f"> ⚠️ N3MC notice: {n}" for n in notices))
    if not results:
        parts.append("_No memories found._")
        return "\n\n".join(parts)
    parts.append(_VERSION_SENTENCE)
    hits: list[str] = []
    for r in results:
        tag     = r.get("_tag", "")
        tag_str = f"{tag} " if tag else ""
        tid     = r.get("turn_id", "")
        tid_str = f" turn={tid}" if tid else ""
        score   = float(r.get("score", 0.0))
        saved   = _fmt_saved(r.get("timestamp", ""), r.get("timestamp_epoch"))
        hits.append(
            f"### {tag_str}[{r.get('id', '')}]{tid_str} score={score:.3f} "
            f"saved={saved}\n{purify(r.get('content', ''))}"
        )
    parts.append("\n\n---\n\n".join(hits))
    return "\n\n".join(parts)


def _take_notices() -> list[str]:
    """Pending config notices (cfg["_notices"]), returned once per process (§6)."""
    global _notices_shown
    if _notices_shown:
        return []
    _notices_shown = True
    if _db is None:
        return []
    return list(_db.cfg.get("_notices") or [])

# ── §11 turn-end nudges ──────────────────────────────────────────────────────

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
    "long content (>400 chars). Exact duplicates are auto-rejected, so "
    "err on the side of saving._"
)
_NUDGE_GENERIC = (
    "\n\n---\n_Reminder: before closing this turn, call `save_memory` "
    "to persist the user's intent (paraphrased) and any substantive "
    "output you produced. Lite entries expire 7 days after save, so "
    "saving more is safer than saving less._"
)


# ── tool registration (spec §4.3 / §4.3.1) ───────────────────────────────────

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search_memory",
            description=(
                "Hybrid (vector + BM25) search over stored memories. "
                "Call this at the start of every user turn. "
                "Each hit shows its saved= time; when several versions of the "
                "same content appear, the newest is listed first and is the "
                "current one. "
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
                    "since": {
                        "type": "string",
                        "description": (
                            "§4.3.1 — ISO 8601 date or datetime (e.g. '2026-05-01' or "
                            "'2026-05-01T09:00:00Z'). Only entries saved ON OR AFTER "
                            "this timestamp are returned. Date-only values are treated "
                            "as 00:00:00 UTC."
                        ),
                    },
                    "until": {
                        "type": "string",
                        "description": (
                            "§4.3.1 — ISO 8601 date or datetime. Only entries saved "
                            "ON OR BEFORE this timestamp are returned. Date-only values "
                            "are treated as 23:59:59 UTC."
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
                "Rejects exact duplicates only; an updated fact or a re-made "
                "document is saved as a new entry (search_memory lists the "
                "newest version first). "
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
                    "turn_id": {
                        "type": "string",
                        "description": (
                            "§4.3.1 — Optional turn grouping label. "
                            "Tag all memories saved within one conversation turn with "
                            "the same turn_id. Enables recall_thread to surface the "
                            "surrounding context later. Any non-empty string works; "
                            "a short UUID, timestamp, or 'turn-N' format is recommended."
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
        Tool(
            name="recall_thread",
            description=(
                "§4.3.1 — Retrieve the memories sharing a turn_id plus N entries "
                "before and after in chronological order. "
                "Use this when a search_memory hit lacks enough context: pass the "
                "hit's turn_id here to surface the surrounding conversation thread. "
                "Returns {status:'not_found'} if turn_id is unknown."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "turn_id": {
                        "type": "string",
                        "description": "The turn_id from a search_memory result.",
                    },
                    "before": {
                        "type": "integer",
                        "description": "Number of earlier entries to include (default 2).",
                        "minimum": 0,
                        "maximum": 20,
                    },
                    "after": {
                        "type": "integer",
                        "description": "Number of later entries to include (default 2).",
                        "minimum": 0,
                        "maximum": 20,
                    },
                },
                "required": ["turn_id"],
            },
        ),
    ]


# ── tool dispatch (spec §4.3 / §4.4) ────────────────────────────────────────

_TOOL_NAMES = {
    "search_memory", "save_memory", "list_memories", "delete_memory",
    "delete_memories_by_session", "repair_memory", "recall_thread",
}


def _backend_error(name: str) -> list[TextContent]:
    """Spec §3.9: the backend error with the recovery hint, per tool shape."""
    hint = _db.hint()
    if name == "save_memory":
        body = {"status": "error", "saved": False, "reason": hint}
    elif name in ("delete_memory", "delete_memories_by_session"):
        body = {"status": "error", "reason": hint}
    elif name == "repair_memory":
        body = {"status": "error", "message": hint}
    else:  # search_memory / list_memories / recall_thread
        return [TextContent(type="text",
                            text=f"Error: memory backend unreachable. {hint}")]
    return [TextContent(type="text",
                        text=json.dumps(body, ensure_ascii=False) + _NUDGE_GENERIC)]


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    # Spec §3.9 "Redis availability on every tool call": PING / reconnect
    # before running the tool; a stopped Redis never reaches the tool body.
    if name in _TOOL_NAMES and not _db.ensure_connected():
        return _backend_error(name)
    try:
        if name == "search_memory":
            results = _db.search_memory(
                arguments.get("query", ""),
                arguments.get("limit"),
                arguments.get("session_id", ""),
                arguments.get("since", ""),
                arguments.get("until", ""),
            )
            notices = _take_notices()
            degraded = model_unavailable_message()  # spec §3.9: every degraded call
            if degraded:
                notices = notices + [degraded]
            text = _render_search(results, notices)
            return [TextContent(type="text", text=text + _NUDGE_AFTER_SEARCH)]

        elif name == "save_memory":
            raw_imp = arguments.get("importance", 1.0)
            try:
                importance_val = float(raw_imp)
            except (ValueError, TypeError):
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "saved":  False,
                        "status": "error",
                        "reason": f"importance must be numeric (got {raw_imp!r})",
                    }, ensure_ascii=False),
                )]
            result = _db.save_memory(
                arguments.get("content", ""),
                arguments.get("agent_name", ""),
                arguments.get("owner_id", ""),
                importance_val,
                arguments.get("session_id", ""),
                arguments.get("turn_id", ""),
            )
            return [TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False) + _NUDGE_AFTER_SAVE,
            )]

        elif name == "list_memories":
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
                    lines.append(f"**{tag_str}[{m['id']}]** {ts}\n{purify(m.get('content', ''))}")
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

        elif name == "recall_thread":
            turn_id = arguments.get("turn_id", "").strip()
            if not turn_id:
                return [TextContent(
                    type="text",
                    text=json.dumps({"status": "error", "reason": "turn_id required"})
                    + _NUDGE_GENERIC,
                )]
            before = int(arguments.get("before", 2))
            after  = int(arguments.get("after", 2))
            entries = _db.recall_thread(turn_id, before, after)
            if not entries:
                text = json.dumps({"status": "not_found", "turn_id": turn_id})
            else:
                lines = []
                for e in entries:
                    tag     = e.get("tag", "")
                    tag_str = f"{tag} " if tag else ""
                    ts      = e.get("timestamp", "")[:19]
                    tid     = e.get("turn_id", "")
                    marker  = "▶ " if tid == turn_id else "  "
                    lines.append(
                        f"{marker}**{tag_str}[{e['id']}]** {ts}\n{purify(e.get('content', ''))}"
                    )
                text = "\n\n---\n\n".join(lines)
            return [TextContent(type="text", text=text + _NUDGE_GENERIC)]

        else:
            return [TextContent(type="text", text=f"Error: unknown tool '{name}'")]

    except _CONN_ERRORS:
        # Lost during the tool: report it as the backend error, never as
        # "no results" (spec §3.9).
        _db.mark_disconnected()
        return _backend_error(name)
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


# ── startup (spec §3.9) ──────────────────────────────────────────────────────

async def _main() -> None:
    global _db
    cfg = load_config()

    cfg["_session_id"] = (
        os.environ.get("N3MC_SESSION_ID", "").strip() or str(uuid.uuid4())
    )

    # Apply the operator-selected embedding model (config §3.2) BEFORE the
    # first get_model() call below. The index is built with embedding_dim,
    # which must match the chosen model's output dimension.
    set_model_name(cfg.get("embedding_model", "intfloat/multilingual-e5-base"))

    _db = Database(cfg)
    _db.connect()
    _db.enforce_ephemeral()
    _db.ensure_index()

    init_opts = app.create_initialization_options()
    try:
        init_opts.instructions = INSTRUCTIONS
    except AttributeError:
        pass

    # Spec §3.9 step 4, part 1: import the embedding library synchronously
    # BEFORE stdio starts (child processes spawned during the import would
    # deadlock on Windows once the stdin pipe has a pending read). fd 1 is
    # pointed at stderr meanwhile so stray library output cannot reach the
    # JSON-RPC channel, which is not open yet.
    try:
        saved_fd = os.dup(1)
        try:
            os.dup2(2, 1)
            import_backend()
        finally:
            os.dup2(saved_fd, 1)
            os.close(saved_fd)
    except Exception as e:
        print(f"[n3mc] embedding library import failed: {e}", file=sys.stderr)

    async with stdio_server() as (read_stream, write_stream):
        # Spec §3.9 step 4: load the embedding model in the BACKGROUND after
        # stdio starts listening, so the initialize handshake is answered
        # right away (a synchronous ~30-40 s load times the client out).
        # get_model() uses double-checked locking and a Python-level stdout
        # redirect; the JSON-RPC writer holds its own handle on stdout.buffer.
        loop = asyncio.get_running_loop()
        preload = loop.run_in_executor(None, _preload_model)
        try:
            await app.run(read_stream, write_stream, init_opts)
        finally:
            preload.cancel()


def _preload_model() -> None:
    try:
        get_model()
    except Exception as e:
        print(f"[n3mc] model preload failed (will lazy-retry): {e}", file=sys.stderr)


def run() -> None:
    asyncio.run(_main())
