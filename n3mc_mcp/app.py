"""The resident FastAPI server (spec §3.15, §4.5): /health /shutdown /buffer /search /repair
/tools/{name}. Endpoints that touch Redis or the model are plain `def` (run in the thread pool)."""
import contextlib
import os
import threading
from typing import Any

import redis
from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse

from . import config as _config
from . import paths
from . import processor as P
from .tools import ToolRunner, _NUDGE_GENERIC


def _exit_process() -> None:
    """End the server process (tests replace this)."""
    os._exit(0)


def remove_pid_file_if_own(pid: int = None) -> None:
    pid = os.getpid() if pid is None else pid
    path = paths.pid_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
    except OSError:
        return
    if content == str(pid):
        try:
            os.unlink(path)
        except OSError:
            pass


def create_app(cfg: dict, db, code_version: str, port: int = None, load_model: bool = True) -> FastAPI:
    port = int(port if port is not None else cfg.get("server_port", 18524))
    root = os.path.normcase(os.path.abspath(paths.data_dir()))
    runner = ToolRunner(cfg, db)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        if load_model:
            P.start_background_load(cfg)
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cfg = cfg
    app.state.db = db
    app.state.runner = runner
    app.state.code_version = code_version
    app.state.root = root

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        host = request.headers.get("host", "")
        if host not in allowed_hosts or "origin" in request.headers:
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        return await call_next(request)

    @app.get("/health")
    async def health():
        return {"status": "ok", "root": root, "code_version": code_version, "pid": os.getpid(),
                "redis": _config.redis_display(db.url)}

    @app.post("/shutdown")
    async def shutdown(payload: Any = Body(default=None)):
        given = payload.get("root") if isinstance(payload, dict) else None
        if not isinstance(given, str) or os.path.normcase(os.path.abspath(given)) != root:
            return JSONResponse({"detail": "not this server's data folder"}, status_code=403)

        def _later():
            remove_pid_file_if_own()
            _exit_process()

        threading.Timer(0.3, _later).start()
        return {"status": "ok"}

    @app.post("/buffer")
    def buffer(payload: Any = Body(default=None)):
        body = payload if isinstance(payload, dict) else {}
        body = P.sanitize_surrogates(body)
        status = db.ensure_connected()
        if status != "ok":
            return JSONResponse({"status": "error", "saved": False, "reason": status}, status_code=503)
        role = body.get("role")
        if role not in ("user", "claude"):
            role = None
        try:
            res = db.save_memory(body.get("content"), agent_name=body.get("agent_name") or "claude-code",
                                 session_id=body.get("session_id"), turn_id=body.get("turn_id") or "",
                                 role=role, no_model_wait=bool(body.get("no_model_wait")))
        except (redis.ConnectionError, redis.TimeoutError):
            db.mark_down()
            return JSONResponse({"status": "error", "saved": False, "reason": db.hint()}, status_code=503)
        return res

    @app.post("/search")
    def search(payload: Any = Body(default=None)):
        body = payload if isinstance(payload, dict) else {}
        body = P.sanitize_surrogates(body)
        status = db.ensure_connected()
        if status != "ok":
            return JSONResponse({"detail": status}, status_code=503)
        max_chars = body.get("max_chars")
        if not isinstance(max_chars, int) or isinstance(max_chars, bool):
            max_chars = None
        try:
            text = runner.search_block(body, max_chars=max_chars, wait_model=not body.get("no_model_wait"))
        except P.TimeBoundError as e:
            return JSONResponse({"detail": str(e)}, status_code=400)
        except (redis.ConnectionError, redis.TimeoutError):
            db.mark_down()
            return JSONResponse({"detail": db.hint()}, status_code=503)
        return {"text": text}

    @app.post("/repair")
    def repair(payload: Any = Body(default=None)):
        status = db.ensure_connected()
        if status != "ok":
            return JSONResponse({"status": "error", "message": status}, status_code=503)
        try:
            return runner.repair()
        except (redis.ConnectionError, redis.TimeoutError):
            db.mark_down()
            return JSONResponse({"status": "error", "message": db.hint()}, status_code=503)

    @app.post("/tools/{name}")
    def tools(name: str, nudge: int = 1, payload: Any = Body(default=None)):
        args = payload if isinstance(payload, dict) else {}
        text = runner.call(name, args, nudge=bool(nudge))
        return {"text": text}

    return app


__all__ = ["create_app", "remove_pid_file_if_own", "_exit_process", "_NUDGE_GENERIC"]
