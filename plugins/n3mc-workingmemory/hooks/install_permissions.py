"""
SessionStart hook — idempotently pre-approve the n3mc-workingmemory MCP
tools in `~/.claude/settings.json` `permissions.allow` so the auto-save loop
does not block on per-call approval prompts (spec §8), and pre-flight check
that `uvx` is on `PATH` (the plugin manifest launches the MCP server via
`uvx --from n3memorycore-mcp-lite n3mc-workingmemory`).

Rules added (spec §8, v1.7.0):
- `mcp__plugin_n3mc-workingmemory_n3mc-workingmemory__*` — Claude Code names a
  plugin's MCP tools `mcp__plugin_<plugin>_<server>__<tool>`, and permission
  rules must use that plugin-scoped form. v1.6.x added only the plain
  `mcp__n3mc-workingmemory__<tool>` names, which never matched the plugin's
  tools, so plugin users were prompted on every call.
- `mcp__n3mc-workingmemory__*` — the same tools when the server is
  registered directly (`claude mcp add`, `.mcp.json`).
Wildcards cover all seven tools (incl. `recall_thread`) and future ones.

Safe behavior:
- Always exits 0 (must never block session startup).
- Writes settings.json only if a rule is missing; never removes entries;
  preserves unknown fields; 2-space indent, UTF-8, trailing newline.
- Messages the user must act on (missing `uvx`) are printed as ONE JSON
  object on stdout with `systemMessage` (shown to the user) and
  `hookSpecificOutput.additionalContext` (seen by the LLM). A plain stderr
  line would only appear as a terse transcript notice.
- The Python-free fallback for "no Python on PATH" lives in hooks.json.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

PLUGIN_RULE = "mcp__plugin_n3mc-workingmemory_n3mc-workingmemory__*"
DIRECT_RULE = "mcp__n3mc-workingmemory__*"
RULES = [PLUGIN_RULE, DIRECT_RULE]


def _patch(settings_path: Path) -> list[str]:
    """Add missing RULES. Returns the rules that were added ([] = no write)."""
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            return []
    else:
        data = {}
    if not isinstance(data, dict):
        return []

    perms = data.setdefault("permissions", {})
    if not isinstance(perms, dict):
        return []
    allow = perms.setdefault("allow", [])
    if not isinstance(allow, list):
        return []

    existing = {e for e in allow if isinstance(e, str)}
    added = [r for r in RULES if r not in existing]
    if not added:
        return []

    allow.extend(added)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return added


def _uvx_warning() -> str:
    """Install hint when `uvx` is not on PATH ("" when it is)."""
    try:
        if shutil.which("uvx") is not None:
            return ""
    except Exception:
        return ""
    return (
        "[n3mc-workingmemory] `uvx` was not found on PATH. The plugin's MCP "
        "server is launched via `uvx --from n3memorycore-mcp-lite "
        "n3mc-workingmemory`, so it will fail to start until `uv` is installed. "
        "Install (Windows): `pipx install uv` or "
        "`powershell -c \"irm https://astral.sh/uv/install.ps1 | iex\"`; "
        "(Linux): `curl -LsSf https://astral.sh/uv/install.sh | sh`. "
        "Docs: https://docs.astral.sh/uv/ / "
        "[n3mc-workingmemory] `uvx` が PATH 上に見つかりません。`uv` を入れるまで "
        "MCP サーバーは起動しません。上のいずれかのコマンドで `uv` を入れてください。"
    )


def main() -> int:
    notices: list[str] = []
    try:
        added = _patch(Path.home() / ".claude" / "settings.json")
        if added:
            sys.stderr.write(
                "[n3mc-workingmemory] Added " + ", ".join(added) + " to "
                "~/.claude/settings.json permissions.allow (auto-approve, so "
                "the AI never blocks on a permission prompt).\n"
            )
    except Exception:
        pass
    warning = _uvx_warning()
    if warning:
        notices.append(warning)
    if notices:
        text = " ".join(notices)
        sys.stdout.write(json.dumps({
            "systemMessage": text,
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": text,
            },
        }, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
