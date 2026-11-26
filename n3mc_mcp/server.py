"""The MCP thin client `n3mc-workingmemory` (spec §4.6): stdio only; answers initialize and tools/list
itself and forwards every tools/call to the resident server. No model, no Redis (except the direct
save when the server cannot be used). stdout carries MCP messages only.
"""
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
        except Exception:
            pass

import asyncio  # noqa: E402
import os  # noqa: E402
import threading  # noqa: E402
import uuid  # noqa: E402

from mcp.server.lowlevel import Server  # noqa: E402
from mcp.types import TextContent, Tool  # noqa: E402

from n3mc_mcp import __version__, cli  # noqa: E402
# The direct save (§4.6) needs database.py and redis. Import them now, before stdio starts: on Windows an
# import of a compiled module in a worker thread while stdio reads the stdin pipe can hang (spec §3.9).
from n3mc_mcp import database as _database  # noqa: E402,F401
try:  # psutil (compiled) is used by ensure_server in worker threads
    import psutil as _psutil  # noqa: E402,F401
except ImportError:  # pragma: no cover
    _psutil = None
from n3mc_mcp.config import load_config  # noqa: E402
from n3mc_mcp.instructions import INSTRUCTIONS  # noqa: E402
from n3mc_mcp.tooldefs import TOOL_NAMES, TOOLS  # noqa: E402
from n3mc_mcp.tools import _NUDGE_GENERIC  # noqa: E402

SERVER_NAME = "n3mc-workingmemory"

# The entry's default session: N3MC_SESSION_ID of this process, else one UUIDv4 made at start (spec §4.6).
SESSION_ID = os.environ.get("N3MC_SESSION_ID", "").strip() or str(uuid.uuid4())


class _State:
    cfg = None


STATE = _State()


def current_config():
    """Re-read config.json on every call; while it cannot be read, keep the last readable one (or the
    start-up one), so that the temporary owner_id does not change with every call."""
    try:
        new = load_config(quiet=True)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[n3mc-lite] config.json: {type(e).__name__}: {e}\n")
        new = None
    if new is not None and (not new.get("_unreadable") or STATE.cfg is None):
        STATE.cfg = new
    return STATE.cfg


def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def prepare_args(name, arguments, client_name=None) -> dict:
    args = dict(arguments) if isinstance(arguments, dict) else {}
    if name in ("search_memory", "save_memory") and _blank(args.get("session_id")):
        args["session_id"] = SESSION_ID
    if name == "save_memory" and _blank(args.get("agent_name")):
        for cand in (os.environ.get("N3MC_AGENT_NAME"), client_name):
            if isinstance(cand, str) and cand.strip():
                args["agent_name"] = cand.strip()
                break
    return args


def handle_call(name, arguments, client_name=None) -> str:
    """One tools/call, synchronously (run in a worker thread). Never raises."""
    try:
        if name not in TOOL_NAMES:
            return f"Error: unknown tool '{name}'" + _NUDGE_GENERIC
        args = prepare_args(name, arguments, client_name)
        cfg = current_config()
        ok = cli.ensure_server(cfg)
        return cli.forward_tool(cfg, name, args, nudge=True, server_ok=ok)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[n3mc-lite] tool {name} failed: {type(e).__name__}: {e}\n")
        return f"Error: {type(e).__name__}: {e}" + _NUDGE_GENERIC


def build_server() -> Server:
    server = Server(SERVER_NAME, version=__version__, instructions=INSTRUCTIONS)

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return list(TOOLS)

    async def call_tool(name, arguments):
        client_name = None
        try:
            params = server.request_context.session.client_params
            client_name = params.clientInfo.name if params and params.clientInfo else None
        except Exception:  # noqa: BLE001
            client_name = None
        text = await asyncio.to_thread(handle_call, name, arguments, client_name)
        return [TextContent(type="text", text=text)]

    try:
        server.call_tool(validate_input=False)(call_tool)
    except TypeError:
        server.call_tool()(call_tool)
    return server


def _background_ensure():
    try:
        cli.ensure_server(STATE.cfg)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[n3mc-lite] could not start the memory server: {type(e).__name__}: {e}\n")


def start_background_ensure() -> threading.Thread:
    t = threading.Thread(target=_background_ensure, name="n3mc-ensure-server", daemon=True)
    t.start()
    return t


async def _serve():
    from mcp.server.stdio import stdio_server
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def run():
    current_config()
    start_background_ensure()
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
