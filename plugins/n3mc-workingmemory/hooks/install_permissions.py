"""SessionStart hook of the n3mc-workingmemory plugin: pre-approve the CLI commands (spec §8 "Auto-allow").

Runs with any Python on PATH, possibly one without the package, so it imports nothing from n3mc_mcp.
It never removes an entry and always exits 0. Messages go to stdout as one JSON object
(systemMessage + additionalContext), written with ensure_ascii=True.
"""
import json
import os
import pathlib
import shlex
import shutil
import sys
import tempfile

VERSION = "2.0.2"
UVX_CLI = f"uvx --from n3memorycore-mcp-lite=={VERSION} n3mc-lite"
RULE_COMMANDS = ("--search *", "--buffer *", "--list*", "--recall-thread *", "--repair*", "--stop*")
RULES = [f"Bash({UVX_CLI} {c})" for c in RULE_COMMANDS]
TAG = "[n3mc-workingmemory]"

OURS = {  # event -> (entry-point name, module name); a copy of rules (a) and (b) of is_own_hook (spec §12.8)
    "UserPromptSubmit": ("n3mc-lite-hook", "n3mc_mcp.lite_hook"),
    "Stop": ("n3mc-lite-stop-hook", "n3mc_mcp.lite_stop_hook"),
}

UVX_MISSING = (
    f"{TAG} uvx was not found on PATH. The plugin's memory hooks run through \"uvx --from "
    f"n3memorycore-mcp-lite=={VERSION} n3mc-lite\", so they cannot run until uv is installed. Install (Windows): "
    "pipx install uv or powershell -c \"irm https://astral.sh/uv/install.ps1 | iex\"; (Linux): curl -LsSf "
    "https://astral.sh/uv/install.sh | sh. Docs: https://docs.astral.sh/uv/ / "
    f"{TAG} uvx が PATH 上に見つかりません。uv を入れるまで、記憶のフックは動きません。上のいずれかのコマンドで uv を入れてください。"
)
DOUBLE = (
    f"{TAG} The N3MC Lite hooks are registered twice: by this plugin and by \"n3mc-lite --init\" in "
    "~/.claude/settings.json. Every message is then searched twice. Remove the two n3mc-lite-hook / "
    "n3mc-lite-stop-hook entries from ~/.claude/settings.json, or uninstall the plugin."
)


def _tokens(command):
    command = command.replace("\\", "/")
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def is_init_hook(command, event):
    if not isinstance(command, str):
        return False
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


def settings_path():
    return pathlib.Path.home() / ".claude" / "settings.json"


def has_init_hooks(settings):
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    if not isinstance(hooks, dict):
        return False
    for event in OURS:
        for entry in hooks.get(event) or []:
            if not isinstance(entry, dict):
                continue
            for h in entry.get("hooks") or []:
                if isinstance(h, dict) and is_init_hook(h.get("command"), event):
                    return True
    return False


def read_settings(path):
    if not path.exists():
        return {}
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("settings.json is not a JSON object")
    return data


def add_rules(path):
    """Re-read just before writing, add the missing rules, write a temporary file and os.replace.
    Returns (changed, settings)."""
    settings = read_settings(path)
    perms = settings.get("permissions")
    if perms is None:
        perms = {}
    if not isinstance(perms, dict):
        raise ValueError("permissions is not an object")
    allow = perms.get("allow")
    if allow is None:
        allow = []
    if not isinstance(allow, list):
        raise ValueError("permissions.allow is not a list")
    missing = [r for r in RULES if r not in allow]
    if not missing:
        return False, settings
    allow.extend(missing)
    perms["allow"] = allow
    settings["permissions"] = perms
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".settings.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return True, settings


def main():
    messages = []
    if shutil.which("uvx") is None:
        messages.append(UVX_MISSING)
    path = settings_path()
    settings = None
    try:
        _changed, settings = add_rules(path)
    except Exception as e:  # noqa: BLE001
        msg = (f"{TAG} Could not update ~/.claude/settings.json ({type(e).__name__}: {e}), so the memory commands "
               "were not pre-approved and Claude Code will ask for permission when Claude searches the memory. Make "
               f"the file writable, or add \"Bash({UVX_CLI} --search *)\" to permissions.allow yourself.")
        print(msg, file=sys.stderr)
        messages.append(msg)
        try:
            settings = read_settings(path)
        except Exception:  # noqa: BLE001
            settings = None
    if settings is not None and has_init_hooks(settings):
        messages.append(DOUBLE)
    if messages:
        text = "\n".join(messages)
        out = {"systemMessage": text,
               "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
        sys.stdout.write(json.dumps(out, ensure_ascii=True) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        try:
            print(f"{TAG} {type(e).__name__}: {e}", file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass
    sys.exit(0)
