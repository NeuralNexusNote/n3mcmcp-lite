"""The tool handlers (spec §4.3, §4.3.1, §4.4), the reminders (spec §5 (2)), the list / recall texts
(spec §5 (4)) and the notices shown on the first successful search (spec §3.15, §6). They run inside
the resident server (POST /tools/<name>); the CLI calls the same handlers with nudge=0.
"""
import json
import sys
import threading

from . import context, paths, processor
from .config import backend_hint

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

BACKEND_PREFIX = "Error: memory backend unreachable. "


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


class Notices:
    """The notices of the first successful search (spec §3.15 "Where notices are shown"): the process
    notices of cfg["_notices"] once each per server process, then the due notices.json entries."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._shown = set()
        self._lock = threading.Lock()

    def take(self) -> list:
        out = []
        with self._lock:
            for m in list(self.cfg.get("_notices") or []):
                if m not in self._shown:
                    self._shown.add(m)
                    out.append(m)
        try:
            out.extend(paths.take_due_notices())
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[n3mc-lite] could not read notices.json: {type(e).__name__}: {e}\n")
        return out


def parse_limit(value, default, lo=1, hi=None):
    if isinstance(value, bool) or value is None or value == "":
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if n < lo:
        n = lo
    if hi is not None and n > hi:
        n = hi
    return n


def render_list(rows) -> str:
    if not rows:
        return "_No memories stored._"
    blocks = []
    for r in rows:
        tag = f"[doc×{r.get('chunk_count', 0)}] " if r.get("_doc") else ""
        body = processor.purify(r.get("content", ""))
        if r.get("_doc") and len(body) > 200:
            body = body[:200] + "…"
        blocks.append(f"**{tag}[{r['id']}]** {r.get('timestamp', '')[:19]}\n{body}")
    return "\n\n---\n\n".join(blocks)


def render_thread(rows, turn_id, before, after):
    idx = [i for i, r in enumerate(rows) if r.get("turn_id") == turn_id]
    if not idx:
        return None
    lo = max(0, idx[0] - before)
    hi = min(len(rows), idx[-1] + after + 1)
    blocks = []
    for r in rows[lo:hi]:
        mark = "▶ " if r.get("turn_id") == turn_id else "  "
        tag = f"[doc×{r.get('chunk_count', 0)}] " if r.get("_doc") else ""
        blocks.append(f"{mark}**{tag}[{r['id']}]** {r.get('timestamp', '')[:19]}\n"
                      f"{processor.purify(r.get('content', ''))}")
    return "\n\n---\n\n".join(blocks)


def backend_error_text(name, hint) -> str:
    """The backend error of spec §3.9 item 2 for a tool (without the reminder)."""
    if name in ("search_memory", "list_memories", "recall_thread"):
        return BACKEND_PREFIX + hint
    if name == "save_memory":
        return dumps({"status": "error", "saved": False, "reason": hint})
    if name in ("delete_memory", "delete_memories_by_session"):
        return dumps({"status": "error", "reason": hint})
    return dumps({"status": "error", "message": hint})


class ToolRunner:
    """Runs the tool handlers against one Database (spec §4.3)."""

    def __init__(self, cfg, db, notices=None):
        self.cfg = cfg
        self.db = db
        self.notices = notices or Notices(cfg)

    def call(self, name, args, nudge=True) -> str:
        if not isinstance(args, dict):
            args = {}
        if name not in TOOL_NAMES:
            return f"Error: unknown tool '{name}'" + (_NUDGE_GENERIC if nudge else "")
        try:
            text, kind = self._dispatch(name, args, nudge)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[n3mc-lite] tool {name} failed: {type(e).__name__}: {e}\n")
            text, kind = f"Error: {type(e).__name__}: {e}", "error"
        if not nudge:
            return text
        if kind == "search":
            return text
        if kind == "save":
            return text + _NUDGE_AFTER_SAVE
        return text + _NUDGE_GENERIC

    def _dispatch(self, name, args, nudge=True):
        from .database import CONN_ERRORS, BackendError   # not at import: the thin client imports this module
        if name == "search_memory":
            # the time bounds are checked before anything else touches the backend
            try:
                since, until = processor.parse_time_bounds(args.get("since"), args.get("until"))
            except ValueError as e:
                return str(e), "error"
        try:
            self.db.ensure_connected()
        except BackendError as e:
            return backend_error_text(name, str(e)), "error"
        try:
            if name == "search_memory":
                return self._search(args, since, until, nudge)
            return getattr(self, "_" + name)(args)
        except (BackendError,) + CONN_ERRORS:
            self.db.mark_disconnected()
            return backend_error_text(name, backend_hint(self.db.url)), "error"

    # ── handlers ─────────────────────────────────────────────────────────────

    def _search(self, args, since, until, nudge=True):
        q = args.get("query")
        q = processor.sanitize_surrogates(q) if isinstance(q, str) else ""
        limit = parse_limit(args.get("limit"), int(self.cfg["search_result_limit"]), 1, 100)
        sid = args.get("session_id")
        sid = sid if isinstance(sid, str) and sid.strip() else None
        normal, weak, msg = self.db.search(q, limit=limit, session_id=sid, since=since, until=until)
        notes = self.notices.take()
        if msg:
            notes.append(msg)
        nudge_tail = _NUDGE_AFTER_SEARCH if nudge else ""
        text = context.render_search(normal, weak, notes, head="", tail=nudge_tail,
                                     max_chars=int(self.cfg["context_max_chars"]))
        return text, "search"

    def _save_memory(self, args):
        r = self.db.save_memory(args.get("content"), agent_name=args.get("agent_name"),
                                owner_id=args.get("owner_id"), importance=args.get("importance"),
                                session_id=args.get("session_id"), turn_id=args.get("turn_id"))
        return dumps(r), ("error" if r.get("status") == "error" else "save")

    def _list_memories(self, args):
        limit = parse_limit(args.get("limit"), 20)
        if args.get("limit") == 0:
            limit = 20
        return render_list(self.db.list_entries(limit)), "list"

    def _delete_memory(self, args):
        mid = args.get("id")
        if not isinstance(mid, str) or not mid.strip():
            return dumps({"status": "error", "reason": "id required"}), "error"
        r = self.db.delete_memory(mid)
        return dumps(r), "delete"

    def _delete_memories_by_session(self, args):
        sid = args.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            return dumps({"status": "error", "reason": "session_id required"}), "error"
        return dumps(self.db.delete_by_session(sid)), "delete"

    def _repair_memory(self, args):
        r = self.db.repair()
        return dumps(r), ("error" if r.get("status") == "error" else "repair")

    def _recall_thread(self, args):
        tid = args.get("turn_id")
        if not isinstance(tid, str) or not tid.strip():
            return dumps({"status": "error", "reason": "turn_id required"}), "error"
        before = parse_limit(args.get("before"), 2, 0, 20)
        after = parse_limit(args.get("after"), 2, 0, 20)
        text = render_thread(self.db.all_entries(), tid, before, after)
        if text is None:
            return dumps({"status": "not_found", "turn_id": tid}), "list"
        return text, "list"
