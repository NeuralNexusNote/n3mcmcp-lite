"""Resident FastAPI server: /health /shutdown /buffer /search /repair /tools/{name} (spec §3.15, §4.5).

Every endpoint that touches Redis or the model is a plain `def` (FastAPI runs
it in its thread pool), so a long save or /repair never blocks /health.
"""

import hashlib
import json
import os
import threading

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from n3mc_mcp import config as config_mod
from n3mc_mcp import context as C
from n3mc_mcp import paths
from n3mc_mcp import processor as P
from n3mc_mcp.database import BACKEND_ERRORS, Database, redis_display
from n3mc_mcp.tools import BoundError, ToolRunner, parse_bounds

code_version = config_mod.code_version


def _request_shutdown() -> None:
    """Exit the server process 0.3 s after answering (tests replace this)."""
    def _exit():
        try:
            p = paths.pid_path()
            if p.read_text(encoding="utf-8").strip() == str(os.getpid()):
                p.unlink()
        except OSError:
            pass
        os._exit(0)
    threading.Timer(0.3, _exit).start()


async def _json_body(request: Request):
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def create_app(cfg=None, db=None, runner=None, lifespan=None) -> FastAPI:
    cfg = cfg if cfg is not None else config_mod.load_config()
    db = db if db is not None else Database(cfg)
    runner = runner if runner is not None else ToolRunner(cfg, db)
    version = code_version()
    port = int(cfg["server_port"])
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    app = FastAPI(lifespan=lifespan) if lifespan else FastAPI()
    app.state.cfg, app.state.db, app.state.runner = cfg, db, runner

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        if "origin" in request.headers:
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        if request.headers.get("host", "") not in allowed_hosts:
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        return await call_next(request)

    @app.get("/health")
    async def health():
        return {"status": "ok", "root": str(paths.data_dir()), "code_version": version, "pid": os.getpid(),
                "redis": redis_display(cfg["redis_url"])}

    @app.post("/shutdown")
    async def shutdown(body: dict = Depends(_json_body)):
        root = body.get("root")
        if not isinstance(root, str) or not paths.same_dir(root, paths.data_dir()):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        _request_shutdown_ref()
        return {"status": "ok"}

    def _request_shutdown_ref():
        import n3mc_mcp.app as me
        me._request_shutdown()

    @app.post("/buffer")
    def buffer(body: dict = Depends(_json_body)):
        status = db.ensure_connected()
        if status != "ok":
            return JSONResponse({"status": "error", "saved": False, "reason": db.unavailable_hint(status)},
                                status_code=503)
        role = body.get("role")
        if role not in ("user", "claude"):
            role = None
        agent = body.get("agent_name")
        if agent in (None, "") and role:
            agent = "claude-code"
        try:
            with P.no_model_wait(body.get("no_model_wait") is True):  # the hooks never wait (M3)
                res = db.save_memory(body.get("content"), agent_name=agent, session_id=body.get("session_id"),
                                     turn_id=body.get("turn_id"), role=role)
        except BACKEND_ERRORS:
            db.mark_down()
            return JSONResponse({"status": "error", "saved": False, "reason": db.unavailable_hint("down")},
                                status_code=503)
        return res

    @app.post("/search")
    def search(body: dict = Depends(_json_body)):
        try:
            since, until = parse_bounds(body.get("since"), body.get("until"))
        except BoundError as e:
            return JSONResponse({"detail": str(e)}, status_code=400)
        status = db.ensure_connected()
        if status != "ok":
            return JSONResponse({"detail": db.unavailable_hint(status)}, status_code=503)
        query = body.get("query") if isinstance(body.get("query"), str) else ""
        max_chars = body.get("max_chars")
        if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
            max_chars = None
        head = C.hook_head(query[: int(cfg["search_query_max_chars"])])
        try:
            with P.no_model_wait(body.get("no_model_wait") is True):  # the hooks never wait (M3)
                text = runner.render_search(head, query, body.get("limit"), body.get("session_id"), since,
                                            until, "", max_chars)
        except BACKEND_ERRORS:
            db.mark_down()
            return JSONResponse({"detail": db.unavailable_hint("down")}, status_code=503)
        return {"text": text}

    @app.post("/repair")
    def repair(body: dict = Depends(_json_body)):
        status = db.ensure_connected()
        if status != "ok":
            return JSONResponse({"status": "error", "message": db.unavailable_hint(status)}, status_code=503)
        try:
            return db.repair()
        except BACKEND_ERRORS:
            db.mark_down()
            return JSONResponse({"status": "error", "message": db.unavailable_hint("down")}, status_code=503)

    @app.post("/tools/{name}")
    def tools(name: str, nudge: str = "1", body: dict = Depends(_json_body)):
        return {"text": runner.call(name, body, nudge=(nudge != "0"))}

    return app


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)
