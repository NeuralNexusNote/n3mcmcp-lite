"""Tool handlers, reminders, search rendering and notices (spec §4.3, §5 (2)–(4)). Run inside the resident server."""

import datetime
import json
import re
import threading

from n3mc_mcp import context as C
from n3mc_mcp import paths
from n3mc_mcp import processor as P
from n3mc_mcp._verbatim import _NUDGE_AFTER_SAVE, _NUDGE_AFTER_SEARCH, _NUDGE_GENERIC
from n3mc_mcp.database import BACKEND_ERRORS, fmt_saved

TOOL_NAMES = ("search_memory", "save_memory", "list_memories", "delete_memory",
              "delete_memories_by_session", "repair_memory", "recall_thread")


class BoundError(ValueError):
    """An unreadable since/until; str(e) is the full `Error: …` text."""


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_one(value, name, is_until):
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    v = value.strip()
    if not v:
        return None
    err = BoundError(f"Error: {name} must be an ISO 8601 date or datetime, e.g. 2026-11-13 or "
                     f"2026-11-13T09:00:00+09:00 (got '{value}')")
    try:
        if _DATE_RE.match(v):
            d = datetime.date.fromisoformat(v)
            t = datetime.time(23, 59, 59, 999999) if is_until else datetime.time(0, 0, 0)
            return datetime.datetime.combine(d, t).timestamp()
        if "-" not in v[:10]:
            raise ValueError("not an extended ISO 8601 date")
        s = v
        if s.endswith(("Z", "z")):
            s = s[:-1] + "+00:00"
        if len(s) > 10 and s[10] == " ":
            s = s[:10] + "T" + s[11:]
        dt = datetime.datetime.fromisoformat(s)
        return dt.timestamp()
    except (ValueError, OverflowError, OSError):
        raise err from None


def parse_bounds(since, until):
    """(since_epoch, until_epoch) or raise BoundError (§4.3.1)."""
    s = _parse_one(since, "since", False)
    u = _parse_one(until, "until", True)
    if s is not None and u is not None and s > u:
        raise BoundError("Error: since is later than until")
    return s, u


def render_row(r) -> str:
    tag = f"[doc×{r.get('chunk_count', 0)}] " if r.get("_doc") else ""
    turn = f" turn={r['turn_id']}" if r.get("turn_id") else ""
    return (f"### {tag}[{r['id']}]{turn} score={r.get('score', 0.0):.3f} "
            f"saved={fmt_saved(r.get('timestamp_epoch'))}\n{P.purify(r.get('content') or '')}")


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _int_arg(v, default):
    if v is None or v == "" or isinstance(v, bool):
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


class ToolRunner:
    def __init__(self, cfg, db):
        self.cfg = cfg
        self.db = db
        self._shown = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------ notices
    def take_notices(self) -> list:
        out = []
        with self._lock:
            for n in list(self.cfg.get("_notices", [])):
                if n not in self._shown:
                    self._shown.add(n)
                    out.append(n)
            out.extend(paths.take_due_notices())
        return out

    # ------------------------------------------------------------ dispatch
    def call(self, name, args, nudge=True) -> str:
        if not isinstance(args, dict):
            args = {}
        args = P.sanitize_surrogates(args)
        if name not in TOOL_NAMES:
            return f"Error: unknown tool '{name}'" + (_NUDGE_GENERIC if nudge else "")
        try:
            if name == "search_memory":
                text, kind = self._search_memory(args, nudge)
            else:
                text, kind = getattr(self, "_" + name)(args)
        except BACKEND_ERRORS:
            self.db.mark_down()
            text, kind = self._backend_error(name, self.db.unavailable_hint("down")), "error"
        except Exception as e:  # noqa: BLE001
            text, kind = f"Error: {type(e).__name__}: {e}", "error"
        if not nudge:
            return text
        if kind == "search":
            return text
        if kind == "save":
            return text + _NUDGE_AFTER_SAVE
        return text + _NUDGE_GENERIC

    @staticmethod
    def _backend_error(name, hint) -> str:
        if name in ("search_memory", "list_memories", "recall_thread"):
            return f"Error: memory backend unreachable. {hint}"
        if name == "save_memory":
            return _dump({"status": "error", "saved": False, "reason": hint})
        if name == "repair_memory":
            return _dump({"status": "error", "message": hint})
        return _dump({"status": "error", "reason": hint})

    def _ready(self, name):
        status = self.db.ensure_connected()
        if status != "ok":
            return self._backend_error(name, self.db.unavailable_hint(status))
        return None

    # ------------------------------------------------------------ tools
    def search_rows(self, query, limit=None, session_id=None, since=None, until=None):
        return self.db.search(query, limit=limit, session_id=session_id, since=since, until=until)

    def render_search(self, head, query, limit, session_id, since, until, tail, max_chars):
        rows, weak, model_msg = self.search_rows(query, limit, session_id, since, until)
        notices = self.take_notices()
        if model_msg:
            notices.append(model_msg)
        return C.assemble(head, notices, [render_row(r) for r in rows], [render_row(w) for w in weak],
                          tail, max_chars)

    def _search_memory(self, args, nudge=True):
        try:
            since, until = parse_bounds(args.get("since"), args.get("until"))
        except BoundError as e:
            return str(e), "error"
        err = self._ready("search_memory")
        if err:
            return err, "error"
        # the reminder is the tail of the size-limited output (§3.17), so it is added here
        text = self.render_search("", args.get("query") or "", args.get("limit"), args.get("session_id"),
                                  since, until, _NUDGE_AFTER_SEARCH if nudge else "",
                                  self.cfg["context_max_chars"])
        return text, "search"

    def _save_memory(self, args):
        err = self._ready("save_memory")
        if err:
            return err, "error"
        res = self.db.save_memory(args.get("content"), agent_name=args.get("agent_name"),
                                  owner_id=args.get("owner_id"), importance=args.get("importance"),
                                  session_id=args.get("session_id"), turn_id=args.get("turn_id"))
        return _dump(res), ("error" if res.get("status") == "error" else "save")

    def _list_memories(self, args):
        limit = _int_arg(args.get("limit"), 20)
        if limit == 0:
            limit = 20
        limit = max(1, limit)
        err = self._ready("list_memories")
        if err:
            return err, "error"
        items = self.db.list_memories(limit)
        if not items:
            return "_No memories stored._", "other"
        blocks = []
        for it in items:
            content = P.purify(it.get("content") or "")
            if it.get("_doc"):
                tag = f"[doc×{it.get('chunk_count') or 0}] "
                if len(content) > 200:
                    content = content[:200] + "…"
            else:
                tag = ""
            blocks.append(f"**{tag}[{it['id']}]** {(it.get('timestamp') or '')[:19]}\n{content}")
        return "\n\n---\n\n".join(blocks), "other"

    def _recall_thread(self, args):
        turn_id = args.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id.strip():
            return _dump({"status": "error", "reason": "turn_id required"}), "error"
        before = max(0, _int_arg(args.get("before"), 2))
        after = max(0, _int_arg(args.get("after"), 2))
        err = self._ready("recall_thread")
        if err:
            return err, "error"
        rows = self.db.recall_rows()
        hits = [i for i, r in enumerate(rows) if r.get("turn_id") == turn_id]
        if not hits:
            return _dump({"status": "not_found", "turn_id": turn_id}), "other"
        lo = max(0, hits[0] - before)
        hi = min(len(rows), hits[-1] + after + 1)
        blocks = []
        for r in rows[lo:hi]:
            mark = "▶ " if r.get("turn_id") == turn_id else "  "
            tag = f"[doc×{r.get('chunk_count') or 0}] " if r.get("_doc") else ""
            blocks.append(f"{mark}**{tag}[{r['id']}]** {(r.get('timestamp') or '')[:19]}\n"
                          f"{P.purify(r.get('content') or '')}")
        return "\n\n---\n\n".join(blocks), "other"

    def _delete_memory(self, args):
        err = self._ready("delete_memory")
        if err:
            return err, "error"
        res = self.db.delete_memory(args.get("id") or "")
        return _dump(res), "other"

    def _delete_memories_by_session(self, args):
        err = self._ready("delete_memories_by_session")
        if err:
            return err, "error"
        res = self.db.delete_by_session(args.get("session_id"))
        return _dump(res), ("error" if res.get("status") == "error" else "other")

    def _repair_memory(self, args):
        err = self._ready("repair_memory")
        if err:
            return err, "error"
        return _dump(self.db.repair()), "other"
