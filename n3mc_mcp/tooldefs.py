"""The seven MCP Tool definitions of spec §5 (3), shared by the thin client and the resident server.
Written as the specification gives them: the connected model reads these texts."""
from mcp.types import Tool

TOOLS = [
    Tool(
        name="search_memory",
        description=(
            "Hybrid (vector + BM25) search over stored memories. "
            "Call this at the start of every user turn. "
            "Each hit shows its saved= time; when several versions of the "
            "same content appear, the newest is listed first and is the "
            "current one. "
            "When fewer than min_results (default 3) rows pass the "
            "relevance floors, the closest dropped rows are added under "
            "'Possibly related (weak matches — may be unrelated)': they "
            "are often unrelated — use one only when it is actually "
            "relevant to the question; if it is not, ignore it and do not "
            "mention it. "
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
                        "'2026-05-01T09:00:00+09:00'). Only entries saved ON OR "
                        "AFTER this timestamp are returned. Date-only values and "
                        "values without an offset are read in the server's local "
                        "time (the clock of saved=); date-only means 00:00:00. "
                        "An unreadable value is an error."
                    ),
                },
                "until": {
                    "type": "string",
                    "description": (
                        "§4.3.1 — ISO 8601 date or datetime. Only entries saved "
                        "ON OR BEFORE this timestamp are returned. Read like "
                        "since; date-only means 23:59:59.999999 local time."
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
            "Delete a memory by ID (only memories of the configured owner). "
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
            "Re-ensure the RediSearch index (idempotent) and add missing "
            "vectors (up to 200 per call) to entries saved while the "
            "embedding model was unavailable. "
            "Returns {status, message, reembedded, remaining}."
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
            "Only entries of the session of that turn are returned. "
            "Returns {status:'not_found'} if turn_id is unknown, and "
            "{status:'ambiguous', session_ids} if the turn_id is in several "
            "sessions: call again with one of them as session_id."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "turn_id": {
                    "type": "string",
                    "description": "The turn_id from a search_memory result.",
                },
                "session_id": {
                    "type": "string",
                    "description": "Optional. The session of the turn, when the same turn_id is in several sessions.",
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

TOOL_NAMES = tuple(t.name for t in TOOLS)
