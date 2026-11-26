"""MCP thin client `n3mc-workingmemory`: stdio, initialize / tools/list answered here, every tools/call
forwarded to the resident server (spec §4.1, §4.2, §4.6). No model, no Redis (except the direct save)."""
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

import json  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import uuid  # noqa: E402

from n3mc_mcp import __version__  # noqa: E402
from n3mc_mcp.instructions import INSTRUCTIONS  # noqa: E402
from n3mc_mcp.tools import TOOL_NAMES, _NUDGE_GENERIC, nudge_for, unavailable_text  # noqa: E402

SERVER_NAME = "n3mc-workingmemory"
TOOL_TIMEOUT = 300

# The session of this entry process: N3MC_SESSION_ID, else one UUIDv4 made at start (§4.6 item 6.2).
PROCESS_SESSION = os.environ.get("N3MC_SESSION_ID", "").strip() or str(uuid.uuid4())


class ThinClient:
    """The tools/call logic, without the MCP transport (so that tests call it as a function)."""

    def __init__(self, cfg=None):
        from n3mc_mcp.config import load_config
        self.cfg = cfg if cfg is not None else load_config()
        self.last_good = None if self.cfg.get("_config_unreadable") else self.cfg
        self.client_name = ""

    def reload(self):
        from n3mc_mcp.config import load_config
        cfg = load_config()
        if cfg.get("_config_unreadable"):
            cfg = self.last_good or self.cfg
        else:
            self.last_good = cfg
        self.cfg = cfg
        return cfg

    def fill_args(self, name, args):
        args = dict(args) if isinstance(args, dict) else {}
        if name in ("search_memory", "save_memory"):
            s = args.get("session_id")
            if not (isinstance(s, str) and s.strip()):
                args["session_id"] = PROCESS_SESSION
        if name == "save_memory":
            a = args.get("agent_name")
            if not (isinstance(a, str) and a.strip()):
                val = ""
                for cand in (os.environ.get("N3MC_AGENT_NAME", ""), self.client_name or ""):
                    if isinstance(cand, str) and cand.strip():
                        val = cand.strip()
                        break
                args["agent_name"] = val
        return args

    def handle(self, name, args) -> str:
        from n3mc_mcp import cli
        if name not in TOOL_NAMES:
            return f"Error: unknown tool '{name}'" + _NUDGE_GENERIC
        cfg = self.reload()
        args = self.fill_args(name, args)
        ok = cli.ensure_server(cfg)
        if not ok:
            return self.unavailable(cfg, name, args, cli.last_unavailable_reason(), sent=False)
        try:
            status, r = cli._post(cfg["server_port"], f"/tools/{name}?nudge=1", args, timeout=TOOL_TIMEOUT)
        except cli.NotSent as e:
            return self.unavailable(cfg, name, args, str(e), sent=False)
        except cli.NoAnswer as e:
            return self.unavailable(cfg, name, args, str(e), sent=True)
        if status == 200 and isinstance(r, dict) and isinstance(r.get("text"), str):
            return r["text"]
        return self.unavailable(cfg, name, args, f"HTTP {status}", sent=True)

    def unavailable(self, cfg, name, args, reason, sent):
        from n3mc_mcp import cli
        if name == "save_memory":
            r = cli.save_without_server(cfg, args, reason, PROCESS_SESSION)
            is_err = r.get("status") == "error"
            return json.dumps(r, ensure_ascii=False) + nudge_for(name, is_err)
        return unavailable_text(name, reason, sent=sent) + _NUDGE_GENERIC


def _preimport():
    """Compiled modules of the direct save are imported before stdio starts (§4.6 item 2)."""
    for mod in ("psutil", "hiredis"):
        try:
            __import__(mod)
        except Exception:  # noqa: BLE001
            pass


def build_server(thin: ThinClient):
    import asyncio
    from mcp.server.lowlevel import Server
    from mcp.types import TextContent
    from n3mc_mcp.tooldefs import TOOLS

    app = Server(SERVER_NAME, version=__version__, instructions=INSTRUCTIONS)

    @app.list_tools()
    async def list_tools():
        return list(TOOLS)

    async def call_tool(name, arguments):
        try:
            ctx = app.request_context
            ci = getattr(getattr(ctx.session, "client_params", None), "clientInfo", None)
            if ci is not None and getattr(ci, "name", None):
                thin.client_name = ci.name
        except Exception:  # noqa: BLE001
            pass
        try:
            text = await asyncio.to_thread(thin.handle, name, arguments)
        except Exception as e:  # noqa: BLE001
            print(f"[n3mc-lite] {name}: {type(e).__name__}: {e}", file=sys.stderr)
            text = f"Error: {e}" + _NUDGE_GENERIC
        return [TextContent(type="text", text=text)]

    try:
        app.call_tool(validate_input=False)(call_tool)
    except TypeError:
        app.call_tool()(call_tool)
    return app


async def _main():
    from mcp.server.stdio import stdio_server
    from n3mc_mcp import cli
    thin = ThinClient()
    app = build_server(thin)
    threading.Thread(target=_safe_ensure, args=(cli, thin.cfg), name="n3mc-ensure-server", daemon=True).start()
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def _safe_ensure(cli, cfg):
    try:
        cli.ensure_server(cfg)
    except Exception as e:  # noqa: BLE001
        print(f"[n3mc-lite] ensure_server: {type(e).__name__}: {e}", file=sys.stderr)


def run():
    for name in ("stdin", "stdout", "stderr"):
        s = getattr(sys, name, None)
        if s is not None and hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
    _preimport()
    import asyncio
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
