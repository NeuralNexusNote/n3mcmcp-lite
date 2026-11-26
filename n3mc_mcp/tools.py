"""The tool handlers, the reminders and the search rendering (spec §4.3, §4.4, §5 (2)-(4)).
They run inside the resident server (POST /tools/<name>, §4.5); the CLI calls them with nudge=0."""
import json
import threading

from . import config as _config
from . import context as C
from . import paths
from . import processor as P

_NUDGE_AFTER_SEARCH = (
    "\n\n---\n_Reminder: before closing this turn, call `save_memory` "
    "to persist the user's intent (paraphrased) and any substantive "
    "output you generate. If a persistent N3MC memory (`n3mc-pro`, "
    "`n3mc-pg` or `n3mc-longtermmemory`) is also connected, route "
    "long-lived artifacts there; this server is for "
    "transient task scratch (7-day TTL). Do not skip the save just "
    "because no memory hit was returned above._"
)
_NUDGE_AFTER_SAVE = (
    "\n\n---\n_Reminder: if this turn produced multiple distinct facts or a "
    "long verbatim artifact, make sure each was saved — one "
    "`save_memory` call per fact, or one full-text call for verbatim "
    "long content (>400 chars). Exact duplicates are auto-rejected, so "
    "err on the side of saving._"
)
_NUDGE_GENERIC = (
    "\n\n---\n_Reminder: before closing this turn, call `save_memory` "
    "to persist the user's intent (paraphrased) and any substantive "
    "output you produced. Lite entries expire 7 days after save, so "
    "saving more is safer than saving less._"
)

TOOL_NAMES = ("search_memory", "save_memory", "list_memories", "delete_memory",
              "delete_memories_by_session", "repair_memory", "recall_thread")

REPAIR_BUSY = {"status": "ok", "message": "repair already running", "reembedded": 0, "remaining": 0}


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def backend_error_text(name: str, hint: str) -> str:
    """The backend error of §3.9 item 2, without a reminder."""
    if name in ("search_memory", "list_memories", "recall_thread"):
        return f"Error: memory backend unreachable. {hint}"
    if name == "save_memory":
        return _json({"status": "error", "saved": False, "reason": hint})
    if name == "repair_memory":
        return _json({"status": "error", "message": hint})
    return _json({"status": "error", "reason": hint})


class ToolRunner:
    def __init__(self, cfg: dict, db, repair_lock: threading.Lock = None):
        self.cfg = cfg
        self.db = db
        self.repair_lock = repair_lock or threading.Lock()

    # ── notices (§3.15 "Where notices are shown") ─────────────────────────────

    def take_notices(self, model_msg: str = "") -> list:
        out = _config.take_process_notices(self.cfg)
        try:
            out += paths.take_due_notices()
        except Exception:  # noqa: BLE001
            pass
        if model_msg:
            out.append(model_msg)
        return out

    # ── search ────────────────────────────────────────────────────────────────

    def search_rows(self, args: dict, wait_model: bool = True):
        query = args.get("query")
        if not isinstance(query, str):
            query = "" if query is None else str(query)
        return self.db.search(query, limit=args.get("limit"), session_id=args.get("session_id"),
                              since=args.get("since"), until=args.get("until"), wait_model=wait_model)

    def search_block(self, args: dict, max_chars=None, wait_model: bool = True) -> str:
        """The `# Recalled Memory Context` block of /search (§12.7). Raises TimeBoundError and
        redis.ConnectionError / TimeoutError."""
        normal, weak, msg = self.search_rows(args, wait_model)
        notices = self.take_notices(msg)
        head = C.hook_head(args.get("query") if isinstance(args.get("query"), str) else "",
                           int(self.cfg.get("search_query_max_chars", 2000)))
        return C.render(normal, weak, notices, head=head, tail="", max_chars=max_chars)

    # ── dispatch ──────────────────────────────────────────────────────────────

    def call(self, name: str, args, nudge: bool = True, no_model_wait: bool = False) -> str:
        # redis is imported here, not at the top: the MCP thin client imports this module for its
        # names and reminders and must not load redis before initialize (§4.6 item 2).
        import redis

        if not isinstance(args, dict):
            args = {}
        args = P.sanitize_surrogates(args)
        generic = _NUDGE_GENERIC if nudge else ""
        if name not in TOOL_NAMES:
            return f"Error: unknown tool '{name}'" + generic
        status = self.db.ensure_connected()
        if status != "ok":
            return backend_error_text(name, status) + generic
        try:
            text, ok = getattr(self, "_t_" + name)(args, no_model_wait)
        except P.TimeBoundError as e:
            return str(e) + generic
        except (redis.ConnectionError, redis.TimeoutError):
            self.db.mark_down()
            return backend_error_text(name, self.db.hint()) + generic
        except Exception as e:  # noqa: BLE001
            return f"Error: {type(e).__name__}: {e}" + generic
        if not nudge:
            return text
        if not ok:
            return text + _NUDGE_GENERIC
        if name == "search_memory":
            return text + _NUDGE_AFTER_SEARCH
        if name == "save_memory":
            return text + _NUDGE_AFTER_SAVE
        return text + _NUDGE_GENERIC

    # Each handler returns (text, ok). Search applies its own tail (it counts toward the limit).

    def _t_search_memory(self, args, no_model_wait):
        normal, weak, msg = self.search_rows(args, wait_model=not no_model_wait)
        notices = self.take_notices(msg)
        text = C.render(normal, weak, notices, head="", tail=_NUDGE_AFTER_SEARCH,
                        max_chars=int(self.cfg.get("context_max_chars", 9500)))
        # The tail is part of the limited text; call() adds it again, so strip it here.
        return text[: len(text) - len(_NUDGE_AFTER_SEARCH)], True

    def _t_save_memory(self, args, no_model_wait):
        res = self.db.save_memory(args.get("content"), agent_name=args.get("agent_name"),
                                  owner_id=args.get("owner_id"), importance=args.get("importance", 1.0),
                                  session_id=args.get("session_id"), turn_id=args.get("turn_id"),
                                  no_model_wait=no_model_wait)
        return _json(res), res.get("status") != "error"

    def _t_list_memories(self, args, no_model_wait):
        return self.db.list_memories(args.get("limit", 20)), True

    def _t_recall_thread(self, args, no_model_wait):
        res = self.db.recall_thread(args.get("turn_id"), args.get("before", 2), args.get("after", 2))
        if isinstance(res, dict):
            return _json(res), res.get("status") != "error"
        return res, True

    def _t_delete_memory(self, args, no_model_wait):
        res = self.db.delete_memory(args.get("id"))
        return _json(res), res.get("status") != "error"

    def _t_delete_memories_by_session(self, args, no_model_wait):
        res = self.db.delete_by_session(args.get("session_id"))
        return _json(res), res.get("status") != "error"

    def _t_repair_memory(self, args, no_model_wait):
        res = self.repair()
        return _json(res), res.get("status") != "error"

    def repair(self) -> dict:
        if not self.repair_lock.acquire(blocking=False):
            return dict(REPAIR_BUSY)
        try:
            return self.db.repair()
        finally:
            self.repair_lock.release()
