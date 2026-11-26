"""SessionStart hook of the N3MC Lite Claude Code plugins (spec §8 "Auto-allow").

The same file ships in both plugins:
  * without --mcp (the full-recall plugin n3mc-workingmemory): pre-approve the CLI commands that the
    rules file shows Claude (uvx form) and the plugin's MCP tools;
  * with --mcp (the MCP plugin n3mc-workingmemory-mcp): pre-approve the plugin's MCP tools only.

The delete commands and tools are never pre-approved. Nothing is ever removed from settings.json,
and ~/.claude.json is only read. Every message goes out as one ASCII JSON line (exit 0)."""
import json
import os
import shlex
import shutil
import sys
import tempfile

VERSION = "2.0.2"
PKG = f"n3memorycore-mcp-lite=={VERSION}"
CLI = f"uvx --from {PKG} n3mc-lite"
SERVER = "n3mc-workingmemory"
MCP_TOOLS = ("search_memory", "save_memory", "list_memories", "recall_thread", "repair_memory")
CLI_RULES = [f"Bash({CLI} --search *)", f"Bash({CLI} --buffer *)", f"Bash({CLI} --list*)",
             f"Bash({CLI} --recall-thread *)", f"Bash({CLI} --repair*)", f"Bash({CLI} --stop*)"]
OURS = {
    "UserPromptSubmit": ("n3mc-lite-hook", "n3mc_mcp.lite_hook"),
    "Stop": ("n3mc-lite-stop-hook", "n3mc_mcp.lite_stop_hook"),
}


def mcp_rules(plugin: str) -> list:
    return [f"mcp__plugin_{plugin}_{SERVER}__{t}" for t in MCP_TOOLS]


def _home() -> str:
    return os.path.expanduser("~")


def _settings_path() -> str:
    return os.path.join(_home(), ".claude", "settings.json")


def _read_json(path: str):
    with open(path, "rb") as f:
        data = json.loads(f.read().decode("utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("the top level is not a JSON object")
    return data


def _merge_rules(rules) -> None:
    """Re-read settings.json just before writing, add the missing rules, replace atomically."""
    path = _settings_path()
    data = _read_json(path) if os.path.exists(path) else {}
    perms = data.get("permissions")
    if perms is None:
        perms = data["permissions"] = {}
    if not isinstance(perms, dict):
        raise ValueError("permissions is not an object")
    allow = perms.get("allow")
    if allow is None:
        allow = perms["allow"] = []
    if not isinstance(allow, list):
        raise ValueError("permissions.allow is not a list")
    missing = [r for r in rules if r not in allow]
    if not missing:
        return
    allow.extend(missing)
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _tokens(command: str) -> list:
    command = command.replace("\\", "/")
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def is_pip_hook(command, event: str) -> bool:
    """Rules (a) and (b) of is_own_hook (spec §12.8): the pip installation's hooks."""
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


def _has_pip_hooks(settings: dict) -> bool:
    hooks = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else {}
    for event in OURS:
        entries = hooks.get(event) if isinstance(hooks.get(event), list) else []
        for e in entries:
            hs = e.get("hooks") if isinstance(e, dict) else None
            for h in hs if isinstance(hs, list) else []:
                if isinstance(h, dict) and is_pip_hook(h.get("command"), event):
                    return True
    return False


def uvx_message(mcp: bool) -> str:
    if mcp:
        return (f'[n3mc-workingmemory-mcp] uvx was not found on PATH. The plugin\'s MCP server runs through '
                f'"uvx --from n3memorycore-mcp-lite=={VERSION} n3mc-workingmemory", so it cannot start until uv '
                'is installed. Install (Windows): pipx install uv or powershell -c "irm '
                'https://astral.sh/uv/install.ps1 | iex"; (Linux): curl -LsSf https://astral.sh/uv/install.sh | '
                'sh. Docs: https://docs.astral.sh/uv/ / [n3mc-workingmemory-mcp] uvx が PATH 上に見つかりません。'
                'uv を入れるまで、記憶の MCP サーバーは起動しません。上のいずれかのコマンドで uv を入れてください。')
    return (f'[n3mc-workingmemory] uvx was not found on PATH. The plugin\'s memory hooks run through '
            f'"uvx --from n3memorycore-mcp-lite=={VERSION} n3mc-lite", so they cannot run until uv is installed. '
            'Install (Windows): pipx install uv or powershell -c "irm https://astral.sh/uv/install.ps1 | iex"; '
            '(Linux): curl -LsSf https://astral.sh/uv/install.sh | sh. Docs: https://docs.astral.sh/uv/ / '
            '[n3mc-workingmemory] uvx が PATH 上に見つかりません。uv を入れるまで、記憶のフックは動きません。'
            '上のいずれかのコマンドで uv を入れてください。')


def update_failed_message(mcp: bool, error: BaseException) -> str:
    err = f"{type(error).__name__}: {error}"
    if mcp:
        return (f"[n3mc-workingmemory-mcp] Could not update ~/.claude/settings.json ({err}), so the memory tools "
                "were not pre-approved and Claude Code will ask for permission when Claude uses the memory. Make "
                'the file writable, or add "mcp__plugin_n3mc-workingmemory-mcp_n3mc-workingmemory__search_memory" '
                "and the other memory tools to permissions.allow yourself.")
    return (f"[n3mc-workingmemory] Could not update ~/.claude/settings.json ({err}), so the memory commands were "
            "not pre-approved and Claude Code will ask for permission when Claude searches the memory. Make the "
            f'file writable, or add "Bash({CLI} --search *)" to permissions.allow yourself.')


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    mcp = "--mcp" in argv
    plugin = "n3mc-workingmemory-mcp" if mcp else "n3mc-workingmemory"
    tag = f"[{plugin}]"
    rules = mcp_rules(plugin) if mcp else CLI_RULES + mcp_rules(plugin)
    messages = []
    if shutil.which("uvx") is None:
        messages.append(uvx_message(mcp))
    try:
        _merge_rules(rules)
    except Exception as e:  # noqa: BLE001
        msg = update_failed_message(mcp, e)
        print(msg.encode("ascii", "backslashreplace").decode("ascii"), file=sys.stderr)
        messages.append(msg)
    # Registered twice? (read only; nothing is removed)
    try:
        settings = _read_json(_settings_path()) if os.path.exists(_settings_path()) else {}
    except Exception:  # noqa: BLE001
        settings = {}
    if not mcp and _has_pip_hooks(settings):
        messages.append('[n3mc-workingmemory] The N3MC Lite hooks are registered twice: by this plugin and by '
                        '"n3mc-lite --init" in ~/.claude/settings.json. Every message is then searched twice. '
                        "Remove the two n3mc-lite-hook / n3mc-lite-stop-hook entries from ~/.claude/settings.json, "
                        "or uninstall the plugin.")
    try:
        cj = _read_json(os.path.join(_home(), ".claude.json"))
    except Exception:  # noqa: BLE001
        cj = {}
    servers = cj.get("mcpServers") if isinstance(cj.get("mcpServers"), dict) else {}
    if SERVER in servers:
        messages.append(f"{tag} The N3MC Lite MCP server is registered twice in Claude Code: by this plugin and as "
                        f"{SERVER} in ~/.claude.json, so every memory tool appears twice. Keep one: claude mcp "
                        f"remove {SERVER} -s user, or uninstall this plugin.")
    enabled = settings.get("enabledPlugins") if isinstance(settings.get("enabledPlugins"), dict) else {}
    if not mcp and enabled.get("n3mc-workingmemory-mcp@neuralnexusnote") is True:
        messages.append("[n3mc-workingmemory] The n3mc-workingmemory-mcp plugin is enabled too, so the N3MC Lite "
                        "MCP server is registered twice and every memory tool appears twice. This plugin already "
                        "registers the MCP server: disable the other one (/plugin disable "
                        "n3mc-workingmemory-mcp@neuralnexusnote).")
    if messages:
        text = "\n".join(messages)
        out = {"systemMessage": text,
               "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
        sys.stdout.write(json.dumps(out, ensure_ascii=True) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    # An unexpected failure exits 1, so that the Python-free fallback of hooks.json speaks instead.
    sys.exit(main())
