"""The tool handlers, the reminders, the time bounds and the texts for an unavailable server
(spec §4.3, §4.3.1, §4.4, §4.6, §5 (2)–(4)). The handlers run inside the resident server.

This module does not import redis at the top: the MCP thin client imports it for the tool names and the
reminders (spec §4.6 item 2). ToolRunner.call imports what it needs.
"""
import json
import math
import re
from datetime import datetime, time as dtime

from . import context
from .processor import purify, sanitize_surrogates

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


def nudge_for(name: str, is_error: bool = False) -> str:
    if name == "search_memory" and not is_error:
        return _NUDGE_AFTER_SEARCH
    if name == "save_memory" and not is_error:
        return _NUDGE_AFTER_SAVE
    return _NUDGE_GENERIC


def _json(d) -> str:
    return json.dumps(d, ensure_ascii=False)


# ── Time bounds (§4.3.1) ─────────────────────────────────────────────────────

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class TimeBoundError(ValueError):
    pass


def _bound_error(name, value):
    return TimeBoundError(f"Error: {name} must be an ISO 8601 date or datetime, e.g. 2026-11-13 or "
                          f"2026-11-13T09:00:00+09:00 (got '{value}')")


def parse_time_bound(value, name: str, end: bool = False):
    """Epoch seconds, or None for "no bound". Raises TimeBoundError with the Error: text."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise _bound_error(name, value)
    s = value.strip()
    if not s:
        return None
    try:
        if _DATE_RE.match(s):
            d = datetime.strptime(s, "%Y-%m-%d").date()
            dt = datetime.combine(d, dtime(23, 59, 59, 999999) if end else dtime(0, 0, 0))
        else:
            if len(s) < 11 or s[10] not in ("T", " ", "t"):
                raise ValueError("not an ISO 8601 date or datetime")
            dt = datetime.fromisoformat(s.replace("Z", "+00:00").replace("z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.astimezone()   # the server's local time
        ts = dt.timestamp()
        if not math.isfinite(ts):
            raise ValueError("not finite")
        return ts
    except (ValueError, OSError, OverflowError):
        raise _bound_error(name, value) from None


def parse_bounds(since, until):
    lo = parse_time_bound(since, "since")
    hi = parse_time_bound(until, "until", end=True)
    if lo is not None and hi is not None and lo > hi:
        raise TimeBoundError("Error: since is later than until")
    return lo, hi


# ── Argument checks that need no Redis (§4.3; the thin client's direct save, §4.6) ─

def check_importance(value):
    """(importance or None, error dict or None)."""
    if value is None or value == "":
        return None, None
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or (isinstance(value, float) and math.isnan(value)):
        return None, {"saved": False, "status": "error", "reason": f"importance must be numeric (got {value})"}
    return value, None


def validate_save_args(cfg, args):
    owner = args.get("owner_id")
    if owner not in (None, "") and owner != cfg.get("owner_id"):
        return {"status": "error", "saved": False, "reason": "owner_id mismatch"}
    _imp, err = check_importance(args.get("importance"))
    return err


# ── Texts when the resident server is not available (§4.6) ───────────────────

REASON_STARTING = "the memory server is still starting (the first start downloads the embedding model, ~1.1 GB)"
REASON_STALE = "an old N3MC Lite memory server could not be stopped"


def reason_could_not_start(log_path):
    return f"the memory server could not start; see {log_path}"


def reason_foreign(port):
    return f"another N3MC folder's server is using port {port}"


def unavailable_text(name: str, reason: str, sent: bool = False) -> str:
    """The answer of a tool (not save_memory) when the server is not available; without reminder."""
    if name in ("search_memory", "list_memories", "recall_thread"):
        return (f"Error: memory backend unreachable. The N3MC Lite memory server is not available ({reason}). "
                "It is started automatically; retry in a minute.")
    if name in ("delete_memory", "delete_memories_by_session"):
        if sent:
            return _json({"status": "unknown", "reason": (
                f"The request reached the N3MC Lite memory server but no answer came back ({reason}). "
                "The delete may have completed; check with list_memories or search_memory before deleting again.")})
        return _json({"status": "error", "reason": f"The N3MC Lite memory server is not available ({reason}). "
                      "Nothing was deleted; retry in a minute."})
    if name == "repair_memory":
        return _json({"status": "error", "message": f"The N3MC Lite memory server is not available ({reason}). "
                      "Retry in a minute."})
    return f"Error: unknown tool '{name}'"


def direct_save_detail(reason: str) -> str:
    return (f"The N3MC Lite memory server was not available ({reason}), so this entry was saved without a "
            "vector. It is found by keywords now and gets its vector from repair_memory once the server runs.")


# ── List texts (§5 (4)) ──────────────────────────────────────────────────────

def render_list(rows) -> str:
    if not rows:
        return "_No memories stored._"
    blocks = []
    for r in rows:
        tag = (r.get("tag") + " ") if r.get("tag") else ""
        body = purify(r.get("content", ""))
        if r.get("_doc"):
            body = body[:200] + ("…" if len(body) > 200 else "")
        blocks.append(f"**{tag}[{r['id']}]** {str(r.get('timestamp', ''))[:19]}\n{body}")
    return context.HIT_SEP.join(blocks)


def render_thread(rows) -> str:
    blocks = []
    for r in rows:
        mark = "▶ " if r.get("_target") else "  "
        tag = (r.get("tag") + " ") if r.get("tag") else ""
        blocks.append(f"{mark}**{tag}[{r['id']}]** {str(r.get('timestamp', ''))[:19]}\n{purify(r.get('content', ''))}")
    return context.HIT_SEP.join(blocks)


def _int_arg(value, default):
    if value is None or value == "" or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def backend_error_text(name: str, hint: str) -> str:
    if name in ("search_memory", "list_memories", "recall_thread"):
        return f"Error: memory backend unreachable. {hint}"
    if name == "save_memory":
        return _json({"status": "error", "saved": False, "reason": hint})
    if name in ("delete_memory", "delete_memories_by_session"):
        return _json({"status": "error", "reason": hint})
    return _json({"status": "error", "message": hint})


class ToolRunner:
    """Runs the tool handlers on one Database. notices: a callable that returns the notice messages to
    show on the next successful search (the server's _take_notices); by default the process notices of
    cfg["_notices"], each once."""

    def __init__(self, cfg, db, notices=None):
        self.cfg = cfg
        self.db = db
        self._notices = notices
        self._shown = set()

    def take_notices(self):
        if self._notices is not None:
            return list(self._notices())
        out = []
        for n in list(self.cfg.get("_notices") or []):
            if n not in self._shown:
                self._shown.add(n)
                out.append(n)
        return out

    def call(self, name, args=None, nudge=True) -> str:
        if name not in TOOL_NAMES:
            return f"Error: unknown tool '{name}'" + (_NUDGE_GENERIC if nudge else "")
        if not isinstance(args, dict):
            args = {}
        args = sanitize_surrogates(args)
        import redis
        is_error = False
        try:
            check = self.db.ensure_connected()
            if check != "ok":
                text, is_error = backend_error_text(name, check), True
            elif name == "search_memory":
                try:
                    # §3.17: the MCP search text, reminder included, stays within context_max_chars.
                    return self.search_text(args, tail=_NUDGE_AFTER_SEARCH if nudge else "",
                                            max_chars=int(self.cfg.get("context_max_chars", 9500)))
                except TimeBoundError as e:
                    text, is_error = str(e), True
            else:
                text, is_error = getattr(self, "_t_" + name)(args)
        except (redis.ConnectionError, redis.TimeoutError):
            self.db.mark_disconnected()
            from .config import backend_hint
            text, is_error = backend_error_text(name, backend_hint(self.db.url)), True
        except Exception as e:  # noqa: BLE001
            text, is_error = f"Error: {e}", True
        if nudge:
            text += nudge_for(name, is_error)
        return text

    # each handler returns (text, is_error)

    def search_text(self, args, head="", tail="", max_chars=None, no_model_wait=False):
        """The search text, or raises TimeBoundError. Notices are taken only on success."""
        since, until = parse_bounds(args.get("since"), args.get("until"))
        query = args.get("query")
        if not isinstance(query, str):
            query = "" if query is None else str(query)
        normal, weak, msg = self.db.search(query, limit=_int_arg(args.get("limit"), None),
                                           session_id=args.get("session_id") if isinstance(args.get("session_id"), str) else None,
                                           since=since, until=until, no_model_wait=no_model_wait)
        notices = self.take_notices()
        if msg:
            notices = notices + [msg]
        return context.render_search(normal, weak, notices, head=head, tail=tail, max_chars=max_chars)

    def _t_search_memory(self, args):
        try:
            text = self.search_text(args, tail="", max_chars=None)
        except TimeBoundError as e:
            return str(e), True
        return text, False

    def _t_save_memory(self, args):
        imp, err = check_importance(args.get("importance"))
        if err:
            return _json(err), True
        r = self.db.save_memory(args.get("content"), agent_name=args.get("agent_name"),
                                owner_id=args.get("owner_id"), importance=imp,
                                session_id=args.get("session_id"), turn_id=args.get("turn_id"))
        return _json(r), r.get("status") == "error"

    def _t_list_memories(self, args):
        limit = _int_arg(args.get("limit"), 20) or 20
        limit = max(1, limit)
        return render_list(self.db.list_memories(limit)), False

    def _t_delete_memory(self, args):
        return _json(self.db.delete_memory(args.get("id"))), False

    def _t_delete_memories_by_session(self, args):
        r = self.db.delete_by_session(args.get("session_id"))
        return _json(r), r.get("status") == "error"

    def _t_repair_memory(self, args):
        r = self.db.repair()
        return _json(r), r.get("status") == "error"

    def _t_recall_thread(self, args):
        turn_id = args.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id.strip():
            return _json({"status": "error", "reason": "turn_id required"}), True
        rows = self.db.recall_thread(turn_id, _int_arg(args.get("before"), 2), _int_arg(args.get("after"), 2))
        if not rows:
            return _json({"status": "not_found", "turn_id": turn_id}), False
        return render_thread(rows), False
