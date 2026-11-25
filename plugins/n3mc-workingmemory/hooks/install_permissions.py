"""
SessionStart hook — idempotently pre-approve the N3MC Lite CLI commands in
`~/.claude/settings.json` `permissions.allow`, so Claude can search the memory
without a permission prompt each time (spec §8), and pre-flight check that
`uvx` is on `PATH` (the plugin's hooks run through
`uvx --from n3memorycore-mcp-lite==<version> n3mc-lite-hook`).

Rules added (spec §8, v2.0.0): the CLI commands of §13 in their `uvx` form,
pinned to the package version. The commands that delete memories are never
pre-approved: they are irreversible, so Claude Code asks the user each time.
The v1.12.2 rules for the MCP tools are no longer added; entries that are
already there are left alone.

Safe behavior:
- Always exits 0 (must never block session startup).
- Writes settings.json only if a rule is missing; never removes entries;
  preserves unknown fields; 2-space indent, UTF-8, trailing newline.
  The file is re-read just before writing and merged into what is there;
  the write goes through a temp file in the same folder + os.replace().
- If a rule is missing but settings.json cannot be parsed or written
  (read-only, no permission, policy-locked), changes nothing and reports it
  (stderr + systemMessage) instead of failing silently (spec §8).
- Messages the user must act on (missing `uvx`, hooks registered twice) are
  printed as ONE JSON object on stdout with `systemMessage` (shown to the
  user) and `hookSpecificOutput.additionalContext` (seen by the LLM). A plain
  stderr line would only appear as a terse transcript notice.
- This script may run on a Python that does not have the package installed,
  so it carries its own copy of rules (a) and (b) of `is_own_hook` (§12.8).
- The Python-free fallback for "no Python on PATH" lives in hooks.json.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

# The package version (spec §2). Release: change it together with hooks.json,
# plugin.json and marketplace.json.
VERSION = "2.0.2"

RULES = [
    "Bash(uvx --from n3memorycore-mcp-lite==2.0.2 n3mc-lite --search *)",
    "Bash(uvx --from n3memorycore-mcp-lite==2.0.2 n3mc-lite --buffer *)",
    "Bash(uvx --from n3memorycore-mcp-lite==2.0.2 n3mc-lite --list*)",
    "Bash(uvx --from n3memorycore-mcp-lite==2.0.2 n3mc-lite --recall-thread *)",
    "Bash(uvx --from n3memorycore-mcp-lite==2.0.2 n3mc-lite --repair*)",
    "Bash(uvx --from n3memorycore-mcp-lite==2.0.2 n3mc-lite --stop*)",
]
SEARCH_RULE = RULES[0]

# event -> (entry-point name, module name), as in cli.OURS (spec §12.8)
OURS = {
    "UserPromptSubmit": ("n3mc-lite-hook", "n3mc_mcp.lite_hook"),
    "Stop": ("n3mc-lite-stop-hook", "n3mc_mcp.lite_stop_hook"),
}


class SettingsUpdateError(Exception):
    """settings.json needs a rule but cannot be read or written (spec §8)."""


def _read_settings(settings_path: Path) -> tuple[dict, list]:
    """(data, allow) of settings.json; raises SettingsUpdateError when it
    cannot be read as a JSON object with the expected shape."""
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception as e:
            raise SettingsUpdateError(f"cannot read it as JSON: {type(e).__name__}: {e}")
    else:
        data = {}
    if not isinstance(data, dict):
        raise SettingsUpdateError("the top level is not a JSON object")

    perms = data.get("permissions", {})
    if not isinstance(perms, dict):
        raise SettingsUpdateError("\"permissions\" is not an object")
    allow = perms.get("allow", [])
    if not isinstance(allow, list):
        raise SettingsUpdateError("\"permissions.allow\" is not a list")
    return data, allow


def _missing(allow: list) -> list[str]:
    existing = {e for e in allow if isinstance(e, str)}
    return [r for r in RULES if r not in existing]


def _write_atomic(settings_path: Path, text: str) -> None:
    """Temp file in the same folder + os.replace (spec §8): an interrupted
    write never leaves a truncated settings.json."""
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists() and not os.access(settings_path, os.W_OK):
        # os.replace would succeed on a read-only file on POSIX; a read-only
        # settings.json is the user's choice and is reported instead.
        raise PermissionError(f"[Errno 13] Permission denied: '{settings_path}'")
    fd, tmp = tempfile.mkstemp(prefix=".settings.", suffix=".tmp",
                               dir=str(settings_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, settings_path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _patch(settings_path: Path) -> list[str]:
    """Add missing RULES. Returns the rules that were added ([] = no write).

    The file is re-read immediately before writing and the missing rules are
    merged into what is there at that moment (another program may have changed
    it since the first read); the write goes through a temporary file +
    os.replace.

    Raises SettingsUpdateError when a rule is missing but the file cannot be
    parsed as a JSON object or cannot be written (read-only, no permission,
    policy-locked). Nothing is changed in that case.
    """
    _, allow = _read_settings(settings_path)
    if not _missing(allow):
        return []

    # Re-read right before writing.
    data, allow = _read_settings(settings_path)
    added = _missing(allow)
    if not added:
        return []
    perms = dict(data.get("permissions", {}), allow=allow + added)
    data = dict(data, permissions=perms)
    try:
        _write_atomic(settings_path,
                      json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    except Exception as e:
        raise SettingsUpdateError(f"{type(e).__name__}: {e}")
    return added


def _uvx_warning() -> str:
    """Install hint when `uvx` is not on PATH ("" when it is)."""
    try:
        if shutil.which("uvx") is not None:
            return ""
    except Exception:
        return ""
    return (
        "[n3mc-workingmemory] uvx was not found on PATH. The plugin's memory "
        f"hooks run through \"uvx --from n3memorycore-mcp-lite=={VERSION} "
        "n3mc-lite\", so they cannot run until uv is installed. "
        "Install (Windows): pipx install uv or "
        "powershell -c \"irm https://astral.sh/uv/install.ps1 | iex\"; "
        "(Linux): curl -LsSf https://astral.sh/uv/install.sh | sh. "
        "Docs: https://docs.astral.sh/uv/ / "
        "[n3mc-workingmemory] uvx が PATH 上に見つかりません。uv を入れるまで、"
        "記憶のフックは動きません。上のいずれかのコマンドで uv を入れてください。"
    )


def _tokens(command: str) -> list:
    command = command.replace("\\", "/")      # posix shlex reads "\" as an escape
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def _is_own_hook(command: str, event: str) -> bool:
    """Rules (a) and (b) of is_own_hook (spec §12.8): our entry point in any
    folder, or `-m n3mc_mcp.lite_hook`. Rule (c) needs the package folder and
    is not used here."""
    exe_name, module = OURS[event]
    toks = _tokens(command)
    if not toks:
        return False
    first = os.path.basename(toks[0]).lower()
    if first.endswith(".exe"):
        first = first[:-4]
    if first == exe_name:
        return True
    for i, t in enumerate(toks[:-1]):
        if t == "-m" and toks[i + 1] == module:
            return True
    return False


def _double_registration(settings_path: Path) -> str:
    """The notice when `n3mc-lite --init` also registered the hooks ("" when not)."""
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        hooks = data.get("hooks", {})
        for event in OURS:
            for entry in hooks.get(event, []):
                for h in entry.get("hooks", []):
                    cmd = h.get("command")
                    if isinstance(cmd, str) and _is_own_hook(cmd, event):
                        return (
                            "[n3mc-workingmemory] The N3MC Lite hooks are registered twice: by this "
                            "plugin and by \"n3mc-lite --init\" in ~/.claude/settings.json. Every "
                            "message is then searched twice. Remove the two n3mc-lite-hook / "
                            "n3mc-lite-stop-hook entries from ~/.claude/settings.json, or uninstall "
                            "the plugin."
                        )
    except Exception:
        return ""
    return ""


def main() -> int:
    notices: list[str] = []
    settings_path = Path.home() / ".claude" / "settings.json"
    try:
        added = _patch(settings_path)
        if added:
            sys.stderr.write(
                "[n3mc-workingmemory] Added " + ", ".join(added) + " to "
                "~/.claude/settings.json permissions.allow (so Claude can search "
                "the memory without a permission prompt).\n"
            )
    except SettingsUpdateError as e:
        msg = (
            f"[n3mc-workingmemory] Could not update ~/.claude/settings.json ({e}), "
            "so the memory commands were not pre-approved and Claude Code will ask "
            "for permission when Claude searches the memory. Make the file writable, "
            f"or add \"{SEARCH_RULE}\" to permissions.allow yourself."
        )
        sys.stderr.write(msg + "\n")
        notices.append(msg)
    except Exception as e:  # never block session startup
        sys.stderr.write(f"[n3mc-workingmemory] permission install skipped: {e}\n")
    warning = _uvx_warning()
    if warning:
        notices.append(warning)
    try:
        twice = _double_registration(settings_path)
    except Exception:
        twice = ""
    if twice:
        notices.append(twice)
    if notices:
        text = " ".join(notices)
        sys.stdout.write(json.dumps({
            "systemMessage": text,
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": text,
            },
        }, ensure_ascii=True) + "\n")    # ASCII: stdout is cp932 on a Japanese Windows
    return 0


if __name__ == "__main__":
    sys.exit(main())
