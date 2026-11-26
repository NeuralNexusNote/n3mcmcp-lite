"""MCP thin client `n3mc-workingmemory` (spec §4.6): answers initialize and tools/list itself and
forwards every tools/call to the resident server. No model, and no Redis except the direct save
when the server is not available. stdout carries MCP messages only."""
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

for _stream_name in ("stdin", "stdout", "stderr"):
    _stream = getattr(_sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

import asyncio  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import uuid  # noqa: E402

# The compiled modules the direct save loads are imported before stdio starts (§4.6 item 2).
# redis and database.py are pure Python and are imported at the direct save, so that
# initialize stays within 2 seconds.
import psutil  # noqa: E402,F401

try:
    import hiredis  # noqa: E402,F401
except ImportError:
    pass

import mcp.types as types  # noqa: E402
from mcp.server.lowlevel import Server  # noqa: E402
from mcp.server.stdio import stdio_server  # noqa: E402

from n3mc_mcp import __version__  # noqa: E402
from n3mc_mcp import cli  # noqa: E402
from n3mc_mcp import config as _config  # noqa: E402
from n3mc_mcp.instructions import INSTRUCTIONS  # noqa: E402
from n3mc_mcp.tooldefs import TOOLS  # noqa: E402
from n3mc_mcp.tools import TOOL_NAMES, _NUDGE_AFTER_SAVE, _NUDGE_GENERIC  # noqa: E402

SERVER_NAME = "n3mc-workingmemory"
# The entry's own default session: N3MC_SESSION_ID, else one UUIDv4 for this process (§4.6 6.2).
SESSION = os.environ.get("N3MC_SESSION_ID") or str(uuid.uuid4())

_state = {"cfg": None, "client_name": None}


def current_cfg() -> dict:
    """config.json, read again on every call; an unreadable file keeps the last good settings."""
    cfg = _config.load_config()
    if cfg.get("_unreadable") and _state["cfg"] is not None:
        return _state["cfg"]
    if not cfg.get("_unreadable") or _state["cfg"] is None:
        _state["cfg"] = cfg
    return cfg


def prepare_args(name: str, args) -> dict:
    args = dict(args) if isinstance(args, dict) else {}
    if name in ("search_memory", "save_memory"):
        sid = args.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            args["session_id"] = SESSION
    if name == "save_memory":
        an = args.get("agent_name")
        if not isinstance(an, str) or not an.strip():
            for cand in (os.environ.get("N3MC_AGENT_NAME"), _state["client_name"]):
                if isinstance(cand, str) and cand.strip():
                    args["agent_name"] = cand.strip()
                    break
    return args


def forward(name: str, args: dict) -> str:
    """One tools/call: ensure_server, POST /tools/<name>?nudge=1, or the §4.6 answers."""
    cfg = current_cfg()
    ok = cli.ensure_server(cfg)
    sent = False
    if ok:
        try:
            status, data = cli._post(cfg, f"/tools/{name}?nudge=1", args, 300.0)
            if status == 200 and isinstance(data, dict) and isinstance(data.get("text"), str):
                return data["text"]
            reason, sent = f"HTTPError: HTTP {status}", True
        except Exception as e:  # noqa: BLE001
            reason, sent = cli.classify_request_error(e)
    elif cli._FOREIGN_SERVER_ROOT is not None:
        reason = cli.reason_foreign(cfg)
    else:
        reason = cli.unavailable_reason(cfg)
    if name == "save_memory":
        text, saved_ok = cli.direct_save_text(cfg, args, reason)
        return text + (_NUDGE_AFTER_SAVE if saved_ok else _NUDGE_GENERIC)
    return cli.unavailable_text(name, reason, sent) + _NUDGE_GENERIC


async def handle_call(name: str, arguments) -> list:
    if name not in TOOL_NAMES:
        return [types.TextContent(type="text", text=f"Error: unknown tool '{name}'" + _NUDGE_GENERIC)]
    try:
        text = await asyncio.to_thread(forward, name, prepare_args(name, arguments))
    except Exception as e:  # noqa: BLE001
        print(f"n3mc-workingmemory: {name} failed ({type(e).__name__}: {e})", file=sys.stderr)
        text = f"Error: {type(e).__name__}: {e}" + _NUDGE_GENERIC
    return [types.TextContent(type="text", text=text)]


def build_server() -> Server:
    server = Server(SERVER_NAME, version=__version__, instructions=INSTRUCTIONS)

    @server.list_tools()
    async def list_tools():
        return TOOLS

    async def call_tool(name, arguments):
        try:
            params = server.request_context.session.client_params
            if params is not None and params.clientInfo is not None:
                _state["client_name"] = params.clientInfo.name
        except Exception:  # noqa: BLE001
            pass
        return await handle_call(name, arguments)

    try:
        decorator = server.call_tool(validate_input=False)
        server._n3mc_validate_input = False
    except TypeError:
        decorator = server.call_tool()
        server._n3mc_validate_input = None
    decorator(call_tool)
    return server


def _background_start(cfg) -> None:
    try:
        cli.ensure_server(cfg)
    except Exception as e:  # noqa: BLE001
        print(f"n3mc-workingmemory: could not start the memory server ({type(e).__name__}: {e})", file=sys.stderr)


async def _main() -> None:
    cfg = current_cfg()
    threading.Thread(target=_background_start, args=(cfg,), name="n3mc-ensure-server", daemon=True).start()
    server = build_server()
    options = server.create_initialization_options()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, options)


def run() -> None:
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
