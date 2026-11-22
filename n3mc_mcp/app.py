"""Resident FastAPI server: /health /shutdown /buffer /search /repair /tools/{name} (spec sections 3.15, 4.5).

Every endpoint that touches Redis or the model is a plain `def` (FastAPI runs it in the thread pool),
so a long save or /repair never blocks /health."""

import os
import threading
from contextlib import asynccontextmanager
from typing import Any

import redis
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import config as config_mod
from . import paths, processor
from .database import BackendUnreachable, redis_display
from .tools import Service, TimeRangeError


def _norm(p: str) -> str:
    return os.path.normcase(os.path.abspath(p))


def _default_exit():
    paths.remove_pid_file_if_own()
    os._exit(0)


def create_app(service=None, port=None, start_model=False, code_version=None):
    svc = service or Service(config_mod.load_config())
    serve_port = port or svc.cfg["server_port"]
    # The version of the code and config.json THIS server started with: a later edit of config.json makes
    # the next CLI/hook/MCP call see a different version and replace the server (spec 3.15).
    # run_server computes it right after load_config() and passes it in (L4, 2026-11-22).
    started_version = code_version or config_mod.code_version()

    @asynccontextmanager
    async def lifespan(app):
        if start_model:
            processor.start_background_load(svc.cfg["embedding_model"])
        yield

    app = FastAPI(title="N3MC Lite resident server", lifespan=lifespan)
    app.state.service = svc
    app.state.port = serve_port
    app.state.exit_fn = _default_exit

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "")
        allowed = ("127.0.0.1:%s" % app.state.port, "localhost:%s" % app.state.port)
        if host not in allowed:
            return JSONResponse({"detail": "forbidden host"}, status_code=403)
        if "origin" in request.headers:
            return JSONResponse({"detail": "forbidden origin"}, status_code=403)
        return await call_next(request)

    @app.get("/health")
    def health():
        return {"status": "ok", "root": paths.data_dir(), "code_version": started_version,
                "pid": os.getpid(), "redis": redis_display(svc.cfg["redis_url"])}

    @app.post("/shutdown")
    def shutdown(payload: Any = Body(default=None)):
        root = payload.get("root") if isinstance(payload, dict) else None
        if not isinstance(root, str) or _norm(root) != _norm(paths.data_dir()):
            raise HTTPException(status_code=403, detail="root mismatch")
        t = threading.Timer(0.3, lambda: app.state.exit_fn())
        t.daemon = True
        t.start()
        return {"status": "ok"}

    @app.post("/buffer")
    def buffer(payload: Any = Body(default=None)):
        if not isinstance(payload, dict):
            payload = {}
        status, res = svc.buffer(payload)
        return JSONResponse(res, status_code=status)

    @app.post("/search")
    def search(payload: Any = Body(default=None)):
        if not isinstance(payload, dict):
            payload = {}
        args = {k: payload.get(k) for k in ("query", "session_id", "since", "until", "limit")}
        args["query"] = args["query"] or ""
        max_chars = payload.get("max_chars")
        if not isinstance(max_chars, int) or isinstance(max_chars, bool):
            max_chars = None
        try:
            text = svc.search(args, max_chars=max_chars, hook=True,
                              no_model_wait=bool(payload.get("no_model_wait")))
        except TimeRangeError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except BackendUnreachable as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except (redis.ConnectionError, redis.TimeoutError):
            svc.db.mark_down()
            raise HTTPException(status_code=503, detail=svc.db.hint())
        return {"text": text}

    @app.post("/repair")
    def repair(payload: Any = Body(default=None)):
        try:
            return svc.repair(no_model_wait=True)
        except BackendUnreachable as exc:
            return JSONResponse({"status": "error", "message": str(exc)}, status_code=503)
        except (redis.ConnectionError, redis.TimeoutError):
            svc.db.mark_down()
            return JSONResponse({"status": "error", "message": svc.db.hint()}, status_code=503)

    @app.post("/tools/{name}")
    def tools(name: str, nudge: int = 1, body: Any = Body(default=None)):
        args = body if isinstance(body, dict) else {}
        return {"text": svc.call_tool(name, args, nudge=bool(nudge))}

    return app
