"""MCP thin client `n3mc-workingmemory` (spec §4.6).

stdio only. Answers initialize / tools/list itself and forwards every
tools/call to the resident server. Never imports sentence_transformers or
torch, never writes anything but MCP messages to stdout.
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
import json  # noqa: E402
import os  # noqa: E402
import threading  # noqa: E402
import uuid  # noqa: E402

from mcp.server.lowlevel import Server  # noqa: E402
from mcp.server.stdio import stdio_server  # noqa: E402
from mcp.types import TextContent  # noqa: E402

from n3mc_mcp import __version__  # noqa: E402
from n3mc_mcp import cli  # noqa: E402
from n3mc_mcp import config as config_mod  # noqa: E402
from n3mc_mcp._verbatim import _NUDGE_AFTER_SAVE, _NUDGE_GENERIC  # noqa: E402
from n3mc_mcp.instructions import INSTRUCTIONS  # noqa: E402
from n3mc_mcp.tooldefs import TOOL_NAMES, TOOLS  # noqa: E402

SERVER_NAME = "n3mc-workingmemory"
_SESSION = os.environ.get("N3MC_SESSION_ID") or str(uuid.uuid4())


def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def fill_args(name, args, client_name=None) -> dict:
    """Fill the entry's default session_id / agent_name (§4.6 step 6)."""
    args = dict(args) if isinstance(args, dict) else {}
    if name in ("search_memory", "save_memory") and _blank(args.get("session_id")):
        args["session_id"] = _SESSION
    if name == "save_memory" and _blank(args.get("agent_name")):
        for cand in (os.environ.get("N3MC_AGENT_NAME"), client_name):
            if isinstance(cand, str) and cand.strip():
                args["agent_name"] = cand.strip()
                break
    return args


_cfg_lock = threading.Lock()
_last_cfg = None


def current_cfg(cfg):
    """config.json read again for this call (review of 2026-11-20, P2).

    The MCP entry runs for a whole client session; with the settings read once at start, a changed
    server_port sent every call to the old port. When the file cannot be read now, the last readable
    settings stay in use (a fresh unreadable read would bring a new temporary owner_id each call)."""
    global _last_cfg
    with _cfg_lock:
        try:
            new = config_mod.load_config()
        except Exception:  # noqa: BLE001
            new = None
        if new is not None and not new.get("_unreadable"):
            _last_cfg = new
        elif _last_cfg is None:
            _last_cfg = cfg  # the settings the entry started with
        return _last_cfg


def call_tool_sync(cfg, name, args, client_name=None) -> str:
    """The text answer of one tools/call (runs in a worker thread)."""
    if name not in TOOL_NAMES:
        return f"Error: unknown tool '{name}'" + _NUDGE_GENERIC
    cfg = current_cfg(cfg)
    args = fill_args(name, args, client_name)
    try:
        ok = cli.ensure_server(cfg)
        if ok:
            try:
                status, res = cli._post(f"/tools/{name}?nudge=1", args, int(cfg["server_port"]), 300)
                if status == 200 and isinstance(res, dict) and isinstance(res.get("text"), str):
                    return res["text"]
                cli._set_reason(f"HTTPError: status {status}")
            except Exception as e:  # noqa: BLE001
                cli._set_reason(f"{type(e).__name__}: {e}")
        reason = cli.last_reason()
        if name == "save_memory":
            res, is_err = cli.direct_save_tool(cfg, args, reason)
            return json.dumps(res, ensure_ascii=False) + (_NUDGE_GENERIC if is_err else _NUDGE_AFTER_SAVE)
        return cli.unavailable_text(name, reason) + _NUDGE_GENERIC
    except Exception as e:  # noqa: BLE001
        print(f"N3MC Lite: tool {name} failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return f"Error: {e}" + _NUDGE_GENERIC


def build_server(cfg) -> Server:
    server = Server(SERVER_NAME, version=__version__, instructions=INSTRUCTIONS)

    @server.list_tools()
    async def list_tools():
        return TOOLS

    async def call_tool(name, arguments):
        client_name = None
        try:
            params = server.request_context.session.client_params
            client_name = params.clientInfo.name if params and params.clientInfo else None
        except Exception:  # noqa: BLE001
            client_name = None
        text = await asyncio.to_thread(call_tool_sync, cfg, name, arguments, client_name)
        return [TextContent(type="text", text=text)]

    try:
        server.call_tool(validate_input=False)(call_tool)
    except TypeError:
        server.call_tool()(call_tool)
    return server


def _background_start(cfg):
    try:
        cli.ensure_server(cfg)
    except Exception as e:  # noqa: BLE001
        print(f"N3MC Lite: could not start the memory server: {e}", file=sys.stderr, flush=True)


def run() -> None:
    cfg = config_mod.load_config()
    threading.Thread(target=_background_start, args=(cfg,), daemon=True).start()
    server = build_server(cfg)

    async def main():
        async with stdio_server() as (read, write):
            opts = server.create_initialization_options()
            if getattr(opts, "instructions", None) in (None, ""):
                try:
                    opts.instructions = INSTRUCTIONS
                except Exception:  # noqa: BLE001
                    pass
            await server.run(read, write, opts)

    asyncio.run(main())


if __name__ == "__main__":
    run()
