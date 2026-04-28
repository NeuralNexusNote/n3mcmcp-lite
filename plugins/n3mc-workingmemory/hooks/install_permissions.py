"""
SessionStart hook — idempotently add `mcp__n3mc-workingmemory__*` tools to
`~/.claude/settings.json` `permissions.allow` so the auto-save loop does
not block on per-call approval prompts.

Safe behavior:
- Exits 0 on any error (must never block session startup).
- Writes back only if at least one required entry is missing.
- Preserves unknown fields in settings.json.
- Pretty-prints with 2-space indent, UTF-8, trailing newline.
- Emits a single-line stderr notice only when a change is actually made.

Mirror of the spec §8 / README "Auto-approve tool calls" guidance: the
six tool names are pinned here so even users who installed the plugin
without reading the docs get the auto-approval block written for them.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Six MCP tools exposed by n3mc-workingmemory (spec §4.3).
TOOLS = [
    "mcp__n3mc-workingmemory__search_memory",
    "mcp__n3mc-workingmemory__save_memory",
    "mcp__n3mc-workingmemory__list_memories",
    "mcp__n3mc-workingmemory__delete_memory",
    "mcp__n3mc-workingmemory__delete_memories_by_session",
    "mcp__n3mc-workingmemory__repair_memory",
]


def _patch(settings_path: Path) -> bool:
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            return False
    else:
        data = {}
    if not isinstance(data, dict):
        return False

    perms = data.setdefault("permissions", {})
    if not isinstance(perms, dict):
        return False
    allow = perms.setdefault("allow", [])
    if not isinstance(allow, list):
        return False

    existing = {e for e in allow if isinstance(e, str)}
    added = [t for t in TOOLS if t not in existing]
    if not added:
        return False

    allow.extend(added)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return True


def main() -> int:
    try:
        settings_path = Path.home() / ".claude" / "settings.json"
        if _patch(settings_path):
            sys.stderr.write(
                "[n3mc-workingmemory] Added mcp__n3mc-workingmemory__* tools to "
                "~/.claude/settings.json permissions.allow (auto-approve "
                "enabled so the AI never blocks on a permission prompt).\n"
            )
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
