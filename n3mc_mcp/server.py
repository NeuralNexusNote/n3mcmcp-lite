import asyncio
import json
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

app = Server("n3memorycore-lite")


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
            return [TextContent(type="text", text=text)]

        elif name == "save_memory":
            result = _db.save_memory(
                arguments.get("content", ""),
                arguments.get("agent_name", ""),
                arguments.get("owner_id", ""),
                float(arguments.get("importance", 1.0)),
            )
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

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
            return [TextContent(type="text", text=text)]

        elif name == "delete_memory":
            result = _db.delete_memory(arguments.get("id", ""))
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

        elif name == "repair_memory":
            result = _db.repair_memory()
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

        else:
            return [TextContent(type="text", text=f"Error: unknown tool '{name}'")]

    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


async def _main() -> None:
    global _db
    cfg = load_config()
    cfg["_session_id"] = str(uuid.uuid4())

    _db = Database(cfg)
    _db.connect()
    _db.enforce_ephemeral()
    _db.ensure_index()

    try:
        get_model()
    except Exception as e:
        print(f"[n3mc] model preload warning: {e}", file=sys.stderr)

    init_opts = app.create_initialization_options()
    try:
        init_opts.instructions = INSTRUCTIONS
    except AttributeError:
        pass

    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, init_opts)


def run() -> None:
    asyncio.run(_main())
