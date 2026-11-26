"""The resident FastAPI server: /health /shutdown /buffer /search /repair /tools/{name} (spec §3.15, §4.5)."""
import json
import os
import threading
from contextlib import asynccontextmanager

import redis
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from . import context, paths, processor
from .config import backend_hint
from .tools import TimeBoundError, ToolRunner


def _exit_process():
    os._exit(0)


class NoticeBoard:
    """The notices shown on the first successful search: the process notices of cfg["_notices"] once per
    server process, then the notices.json notices due today (§3.15)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._shown = set()

    def take(self):
        with self._lock:
            out = []
            for n in list(self.cfg.get("_notices") or []):
                if n not in self._shown:
                    self._shown.add(n)
                    out.append(n)
        try:
            from .cli import take_due_notices
            out.extend(take_due_notices())
        except Exception:  # noqa: BLE001
            pass
        return out


def create_app(cfg, db, code_version, port=None, root=None):
    port = int(port if port is not None else cfg["server_port"])
    root = root or paths.data_dir()
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    board = NoticeBoard(cfg)
    runner = ToolRunner(cfg, db, notices=board.take)
    repair_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(_app):
        processor.start_background_load()
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cfg = cfg
    app.state.db = db
    app.state.runner = runner
    app.state.board = board
    app.state.exit = _exit_process

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.headers.get("host", "") not in allowed_hosts or "origin" in request.headers:
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        return await call_next(request)

    @app.get("/health")
    async def health():
        from .config import redis_display
        return {"status": "ok", "root": root, "code_version": code_version, "pid": os.getpid(),
                "redis": redis_display(cfg.get("redis_url"))}

    @app.post("/shutdown")
    async def shutdown(request: Request):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        want = body.get("root") if isinstance(body, dict) else None
        if not isinstance(want, str) or os.path.normcase(os.path.abspath(want)) != os.path.normcase(os.path.abspath(root)):
            return JSONResponse({"detail": "not this server's data folder"}, status_code=403)

        def later():
            from .cli import remove_pid_file_if_own
            remove_pid_file_if_own()
            app.state.exit()

        threading.Timer(0.3, later).start()
        return {"status": "ok"}

    async def _body(request: Request):
        try:
            data = json.loads((await request.body()).decode("utf-8", errors="surrogatepass") or "{}")
        except Exception:  # noqa: BLE001
            return {}
        return data if isinstance(data, dict) else {}

    def _connected():
        return db.ensure_connected()

    def buffer_sync(body):
        check = _connected()
        if check != "ok":
            return JSONResponse({"status": "error", "saved": False, "reason": check}, status_code=503)
        role = body.get("role") if body.get("role") in ("user", "claude") else None
        try:
            r = db.save_memory(body.get("content"), agent_name=body.get("agent_name"),
                               session_id=body.get("session_id"), turn_id=body.get("turn_id"), role=role,
                               no_model_wait=bool(body.get("no_model_wait")))
        except (redis.ConnectionError, redis.TimeoutError):
            db.mark_disconnected()
            return JSONResponse({"status": "error", "saved": False, "reason": backend_hint(db.url)}, status_code=503)
        return r

    def search_sync(body):
        check = _connected()
        if check != "ok":
            return JSONResponse({"detail": check}, status_code=503)
        max_chars = body.get("max_chars")
        if not isinstance(max_chars, int) or isinstance(max_chars, bool):
            max_chars = None
        q = body.get("query") if isinstance(body.get("query"), str) else ""
        head = context.hook_head(q, int(cfg.get("search_query_max_chars", 2000)))
        args = {k: body.get(k) for k in ("query", "session_id", "since", "until", "limit")}
        try:
            text = runner.search_text(args, head=head, tail="", max_chars=max_chars,
                                      no_model_wait=bool(body.get("no_model_wait")))
        except TimeBoundError as e:
            return JSONResponse({"detail": str(e)}, status_code=400)
        except (redis.ConnectionError, redis.TimeoutError):
            db.mark_disconnected()
            return JSONResponse({"detail": backend_hint(db.url)}, status_code=503)
        return {"text": text}

    def repair_sync():
        if not repair_lock.acquire(blocking=False):
            return {"status": "ok", "message": "repair already running", "reembedded": 0, "remaining": 0}
        try:
            check = _connected()
            if check != "ok":
                return JSONResponse({"status": "error", "message": check}, status_code=503)
            try:
                return db.repair(no_model_wait=True)
            except (redis.ConnectionError, redis.TimeoutError):
                db.mark_disconnected()
                return JSONResponse({"status": "error", "message": backend_hint(db.url)}, status_code=503)
        finally:
            repair_lock.release()

    @app.post("/buffer")
    def buffer(body: dict = Depends(_body)):
        return buffer_sync(body)

    @app.post("/search")
    def search(body: dict = Depends(_body)):
        return search_sync(body)

    @app.post("/repair")
    def repair():
        return repair_sync()

    @app.post("/tools/{name}")
    def tools(name: str, nudge: int = 1, body: dict = Depends(_body)):
        return {"text": runner.call(name, body, nudge=bool(nudge))}

    return app
