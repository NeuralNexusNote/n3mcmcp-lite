"""MCP thin client: stdio, tools/list, forwards tools/call to the resident server (spec section 4.6).

No model, no Redis (except the direct save when the server is unavailable). Nothing but MCP messages
goes to stdout."""

import os as _os
import sys as _sys

if __name__ == "__main__":
    # Safe import path. Started as `python -m <package>.<module>`, Python has put the current folder
    # first on sys.path, so a file there named like a module imported below would run inside this
    # program. Drop that entry, unless this package itself lives in the current folder (a source
    # checkout run in place). Nothing else may be imported before this block.
    try:
        _cwd = _os.path.normcase(_os.path.abspath(_os.getcwd()))
        _pkg_parent = _os.path.normcase(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
        if _sys.path and _os.path.normcase(_os.path.abspath(_sys.path[0])) == _cwd and _pkg_parent != _cwd:
            del _sys.path[0]
    except Exception:  # noqa: BLE001
        pass

import sys

for _stream_name in ("stdin", "stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

import asyncio
import os
import threading
import uuid

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent

from n3mc_mcp import __version__
from n3mc_mcp import cli
from n3mc_mcp import config as config_mod
from n3mc_mcp.instructions import INSTRUCTIONS
from n3mc_mcp.tooldefs import list_tools_definitions

SERVER_NAME = "n3mc-workingmemory"
TOOL_NAMES = ("search_memory", "save_memory", "list_memories", "delete_memory",
              "delete_memories_by_session", "repair_memory", "recall_thread")


class ThinState:
    """The entry's own default session and the last readable config."""

    def __init__(self):
        self.session_default = os.environ.get("N3MC_SESSION_ID") or str(uuid.uuid4())
        self.last_cfg = None


STATE = ThinState()


def current_config(state=None):
    """Re-read config.json on every call; keep the last readable one when it cannot be read now."""
    state = state or STATE
    cfg = config_mod.load_config()
    if cfg.get("_unreadable"):
        if state.last_cfg is not None:
            return state.last_cfg
        return cfg
    state.last_cfg = cfg
    return cfg


def _nudge_generic():
    from n3mc_mcp.tools import _NUDGE_GENERIC     # imported late: it pulls in redis (initialize must stay fast)
    return _NUDGE_GENERIC


def _present(v):
    return isinstance(v, str) and v.strip()


def handle_call(name, arguments, client_name=None, state=None):
    """One tools/call: returns the text for the model. Never raises."""
    state = state or STATE
    args = dict(arguments) if isinstance(arguments, dict) else {}
    if name not in TOOL_NAMES:
        return "Error: unknown tool '%s'" % name + _nudge_generic()
    try:
        cfg = current_config(state)
        if name in ("search_memory", "save_memory") and not _present(args.get("session_id")):
            args["session_id"] = state.session_default
        if name == "save_memory" and not _present(args.get("agent_name")):
            for cand in (os.environ.get("N3MC_AGENT_NAME"), client_name):
                if _present(cand):
                    args["agent_name"] = cand.strip()
                    break
            else:
                args.pop("agent_name", None)
        if cli.ensure_server(cfg):
            try:
                return cli._post(cfg["server_port"], "/tools/%s?nudge=1" % name, args, timeout=300)["text"]
            except Exception as exc:  # noqa: BLE001
                # a delete that was sent but got no answer is "unknown", not "Nothing was deleted" (review #7)
                return cli.tool_failed_text(cfg, name, args, exc, nudge=True)
        reason = cli.unavailable_reason(cfg)
        if name == "save_memory":
            return cli.direct_save_text(cfg, args, reason, nudge=True)
        return cli._unavailable_text(name, reason, nudge=True)
    except Exception as exc:  # noqa: BLE001
        print("N3MC Lite MCP: %s failed: %s: %s" % (name, type(exc).__name__, exc), file=sys.stderr)
        return "Error: %s" % exc + _nudge_generic()


def build_server():
    server = Server(SERVER_NAME, version=__version__, instructions=INSTRUCTIONS)

    @server.list_tools()
    async def _list_tools():
        return list_tools_definitions()

    async def _call(name: str, arguments: dict):
        client_name = None
        try:
            client_name = server.request_context.session.client_params.clientInfo.name
        except Exception:  # noqa: BLE001
            pass
        text = await asyncio.to_thread(handle_call, name, arguments, client_name)
        return [TextContent(type="text", text=text)]

    try:
        server.call_tool(validate_input=False)(_call)
    except TypeError:
        server.call_tool()(_call)
    return server


def _ensure_in_background():
    def work():
        try:
            cli.ensure_server(current_config())
        except Exception as exc:  # noqa: BLE001
            print("N3MC Lite MCP: could not start the server (%s: %s)" % (type(exc).__name__, exc),
                  file=sys.stderr)

    threading.Thread(target=work, daemon=True).start()


async def _serve():
    server = build_server()
    _ensure_in_background()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def run():
    asyncio.run(_serve())


if __name__ == "__main__":
    run()
