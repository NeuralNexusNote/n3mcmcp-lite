"""The resident FastAPI server (spec §3.15, §4.5): /health /shutdown /buffer /search /repair /tools/{name}.

Every endpoint that touches Redis or the model is a plain `def` (FastAPI runs it in its thread pool),
so a long save or /repair never stops /health.
"""
import json
import os
import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from . import context, paths, processor
from .config import backend_hint, redis_display
from .database import CONN_ERRORS, BackendError
from .tools import Notices, ToolRunner, parse_limit


def remove_pid_file_if_own() -> None:
    """Remove the PID file when it holds this process's PID (read and close it first: Windows cannot
    delete an open file)."""
    p = paths.pid_path()
    try:
        with open(p, encoding="utf-8") as f:
            text = f.read().strip()
    except OSError:
        return
    if text == str(os.getpid()):
        try:
            os.unlink(p)
        except OSError:
            pass


def _exit_server() -> None:
    remove_pid_file_if_own()
    os._exit(0)


async def _json_body(request: Request) -> dict:
    try:
        raw = await request.body()
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def create_app(cfg, db, code_version, port=None, root=None, runner=None, notices=None,
               start_model_loader=False) -> FastAPI:
    port = int(port if port is not None else cfg["server_port"])
    root = str(root if root is not None else paths.data_dir())
    notices = notices or Notices(cfg)
    runner = runner or ToolRunner(cfg, db, notices)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    repair_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(_app):
        if start_model_loader:
            processor.start_background_load()
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cfg = cfg
    app.state.db = db
    app.state.runner = runner
    app.state.notices = notices

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = request.headers.get("host", "")
        if host not in allowed_hosts or "origin" in request.headers:
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        return await call_next(request)

    @app.get("/health")
    async def health():
        return {"status": "ok", "root": root, "code_version": code_version, "pid": os.getpid(),
                "redis": redis_display(getattr(db, "url", None) or cfg["redis_url"])}

    @app.post("/shutdown")
    async def shutdown(body: dict = Depends(_json_body)):
        if not paths.same_folder(body.get("root", ""), root):
            return JSONResponse({"detail": "this server belongs to another data folder"}, status_code=403)
        threading.Timer(0.3, _exit_server).start()
        return {"status": "ok"}

    @app.post("/buffer")
    def buffer(body: dict = Depends(_json_body)):
        try:
            db.ensure_connected()
        except BackendError as e:
            return JSONResponse({"status": "error", "saved": False, "reason": str(e)}, status_code=503)
        role = body.get("role")
        if role not in ("user", "claude"):
            return {"status": "error", "saved": False, "reason": "role must be \"user\" or \"claude\""}
        try:
            return db.save_memory(body.get("content"), role=role,
                                  agent_name=body.get("agent_name") or "claude-code",
                                  session_id=body.get("session_id") or None,
                                  turn_id=body.get("turn_id") or None,
                                  no_model_wait=bool(body.get("no_model_wait")))
        except (BackendError,) + CONN_ERRORS:
            db.mark_disconnected()
            return JSONResponse({"status": "error", "saved": False, "reason": backend_hint(db.url)},
                                status_code=503)
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "saved": False, "reason": f"{type(e).__name__}: {e}"}

    @app.post("/search")
    def search(body: dict = Depends(_json_body)):
        q = body.get("query")
        q = processor.sanitize_surrogates(q) if isinstance(q, str) else ""
        try:
            since, until = processor.parse_time_bounds(body.get("since"), body.get("until"))
        except ValueError as e:
            return JSONResponse({"detail": str(e)}, status_code=400)
        try:
            db.ensure_connected()
        except BackendError as e:
            return JSONResponse({"detail": str(e)}, status_code=503)
        limit = parse_limit(body.get("limit"), int(cfg["search_result_limit"]), 1, 100)
        sid = body.get("session_id")
        sid = sid if isinstance(sid, str) and sid.strip() else None
        try:
            normal, weak, msg = db.search(q, limit=limit, session_id=sid, since=since, until=until,
                                          no_model_wait=bool(body.get("no_model_wait")))
        except (BackendError,) + CONN_ERRORS:
            db.mark_disconnected()
            return JSONResponse({"detail": backend_hint(db.url)}, status_code=503)
        notes = notices.take()
        if msg:
            notes.append(msg)
        max_chars = body.get("max_chars")
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
            max_chars = None
        head = context.hook_head(q, int(cfg["search_query_max_chars"]))
        return {"text": context.render_search(normal, weak, notes, head=head, tail="", max_chars=max_chars)}

    @app.post("/repair")
    def repair(body: dict = Depends(_json_body)):
        if not repair_lock.acquire(blocking=False):
            return {"status": "ok", "message": "repair already running", "reembedded": 0, "remaining": 0}
        try:
            try:
                db.ensure_connected()
            except BackendError as e:
                return JSONResponse({"status": "error", "message": str(e)}, status_code=503)
            try:
                return db.repair(no_model_wait=True)
            except (BackendError,) + CONN_ERRORS:
                db.mark_disconnected()
                return JSONResponse({"status": "error", "message": backend_hint(db.url)}, status_code=503)
        finally:
            repair_lock.release()

    @app.post("/tools/{name}")
    def tools(name: str, nudge: str = "1", body: dict = Depends(_json_body)):
        return {"text": runner.call(name, body, nudge=(nudge != "0"))}

    return app
