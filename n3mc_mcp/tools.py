"""Tool handlers, nudges, search rendering, notices (spec sections 4.3, 4.5, 5 (2) - (4)).

Runs inside the resident server; the CLI and the thin MCP client reach it over HTTP."""

import json
import math
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime

import redis

from . import context, processor
from .database import BackendUnreachable, Database, DOC, MEM

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


class TimeRangeError(ValueError):
    """since/until could not be read; str(exc) is the `Error: ...` sentence."""


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def is_error_text(text: str) -> bool:
    if text.startswith("Error:"):
        return True
    if text.startswith("{"):
        try:
            obj, _ = json.JSONDecoder().raw_decode(text)
            return isinstance(obj, dict) and obj.get("status") == "error"
        except ValueError:
            return False
    return False


def with_nudge(name: str, text: str, nudge: bool = True) -> str:
    if not nudge:
        return text
    if is_error_text(text):
        return text + _NUDGE_GENERIC
    if name == "search_memory":
        return text          # the reminder is part of the (size-limited) search text itself
    if name == "save_memory":
        return text + _NUDGE_AFTER_SAVE
    return text + _NUDGE_GENERIC


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def parse_time(value, end_of_day: bool, name: str):
    """ISO 8601 date/datetime -> epoch seconds. Date-only and offset-less values are server-local time."""
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    v = value.strip()
    if not v:
        return None
    err = TimeRangeError("Error: %s must be an ISO 8601 date or datetime, e.g. 2026-11-13 or "
                         "2026-11-13T09:00:00+09:00 (got '%s')" % (name, value))
    if not _DATE_RE.match(v):
        raise err
    try:
        if len(v) == 10:
            d = datetime.fromisoformat(v)
            if end_of_day:
                d = d.replace(hour=23, minute=59, second=59, microsecond=999999)
            return d.timestamp()
        s = v.replace(" ", "T", 1)
        if s.endswith(("Z", "z")):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        raise err


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class Service:
    """All state of the resident server: config, Redis layer, default session, notices."""

    def __init__(self, cfg, db=None):
        self.cfg = cfg
        self.db = db or Database(cfg)
        self.session_default = (self.db.default_session or os.environ.get("N3MC_SESSION_ID")
                                or str(uuid.uuid4()))
        self.shown = set()
        self.lock = threading.Lock()
        self.repair_lock = threading.Lock()

    # ----------------------------------------------------------- notices
    def take_notices(self):
        with self.lock:
            out = []
            for msg in list(self.cfg.get("_notices", [])):
                if msg not in self.shown:
                    self.shown.add(msg)
                    out.append(msg)
        try:
            from . import cli
            out.extend(cli.due_notices())
        except Exception as exc:  # noqa: BLE001
            print("N3MC Lite: could not read notices.json (%s)" % exc, file=sys.stderr)
        return out

    # ----------------------------------------------------------- helpers
    def _hint(self):
        return self.db.hint()

    def _backend_error(self, name, hint):
        if name == "save_memory":
            return _json({"status": "error", "saved": False, "reason": hint})
        if name in ("delete_memory", "delete_memories_by_session"):
            return _json({"status": "error", "reason": hint})
        if name == "repair_memory":
            return _json({"status": "error", "message": hint})
        return "Error: memory backend unreachable. %s" % hint

    # ----------------------------------------------------------- search
    def search(self, args, max_chars=None, hook=False, no_model_wait=False, session_default=None,
               nudge_tail=False):
        """Run search_memory. Returns the text (without nudge). Raises TimeRangeError / BackendUnreachable."""
        cfg = self.cfg
        query = processor.sanitize_surrogates(args.get("query", ""))
        if not isinstance(query, str):
            query = str(query)
        query = query[:cfg["search_query_max_chars"]]
        since = parse_time(args.get("since"), False, "since")
        until = parse_time(args.get("until"), True, "until")
        if since is not None and until is not None and since > until:
            raise TimeRangeError("Error: since is later than until")
        limit = args.get("limit")
        limit = int(limit) if isinstance(limit, (int, float)) and not isinstance(limit, bool) and limit else \
            cfg["search_result_limit"]
        limit = max(1, limit)
        session = processor.sanitize_surrogates(args.get("session_id") or "")
        if not isinstance(session, str) or not session.strip():
            session = session_default or self.session_default
        self.db.ensure_connected()
        try:
            rows, weak, model_msg = self.db.search(query, session, limit, since, until,
                                                   no_model_wait=no_model_wait)
        except (redis.ConnectionError, redis.TimeoutError):
            self.db.mark_down()
            raise BackendUnreachable(self.db.hint())
        notices = self.take_notices()
        if model_msg:
            notices.append(model_msg)
        head = context.hook_head(query, cfg["search_query_max_chars"]) if hook else ""
        tail = _NUDGE_AFTER_SEARCH if (nudge_tail and not hook) else ""
        return context.render_search(rows, weak, notices, head=head, tail=tail, max_chars=max_chars)

    # ----------------------------------------------------------- save
    def save(self, args, role=None, no_model_wait=False, embed=True):
        cfg = self.cfg
        owner = args.get("owner_id")
        if owner not in (None, "") and owner != cfg["owner_id"]:
            return {"status": "error", "saved": False, "reason": "owner_id mismatch"}
        imp = args.get("importance")
        importance = 1.0
        if imp is not None:
            val = None
            if isinstance(imp, (int, float)) and not isinstance(imp, bool):
                val = float(imp)
            elif isinstance(imp, str):
                try:
                    val = float(imp)
                except ValueError:
                    val = None
            if val is None or not math.isfinite(val):
                return {"status": "error", "saved": False,
                        "reason": "importance must be numeric (got %s)" % (imp,)}
            importance = max(0.5, min(2.0, val))
        session = args.get("session_id")
        if session is None or (isinstance(session, str) and not session.strip()):
            session = self.session_default
        res = self.db.save_memory(
            args.get("content", ""), agent_name=args.get("agent_name") or "", session_id=session,
            turn_id=args.get("turn_id") or "", importance=importance, role=role, embed=embed,
            no_model_wait=no_model_wait)
        details = []
        warns = []
        if res.get("warning") == "embedding_unavailable":
            warns.append("embedding_unavailable")
            details.append(processor.model_unavailable_message(cfg))
        if cfg.get("_unreadable") and res.get("saved"):
            warns.append("config_unreadable")
            details.append("config.json could not be read, so this entry was saved under a temporary "
                           "owner_id and will not be visible after the server restarts. Fix %s and "
                           "restart the MCP client." % cfg.get("_path", "config.json"))
        if warns:
            res["warning"] = "; ".join(warns)
            res["detail"] = " ".join(details)
        return res

    def buffer(self, payload):
        """POST /buffer. Returns (http_status, dict)."""
        role = payload.get("role")
        if role not in ("user", "claude"):
            role = None
        args = {"content": payload.get("content", ""), "agent_name": payload.get("agent_name") or "claude-code",
                "turn_id": payload.get("turn_id") or ""}
        if payload.get("session_id"):
            args["session_id"] = payload["session_id"]
        try:
            self.db.ensure_connected()
            res = self.save(args, role=role, no_model_wait=bool(payload.get("no_model_wait")))
        except BackendUnreachable as exc:
            return 503, {"status": "error", "saved": False, "reason": str(exc)}
        except (redis.ConnectionError, redis.TimeoutError):
            self.db.mark_down()
            return 503, {"status": "error", "saved": False, "reason": self._hint()}
        return 200, res

    # ----------------------------------------------------------- other tools
    def list_memories(self, args):
        lim = args.get("limit")
        lim = int(lim) if isinstance(lim, (int, float)) and not isinstance(lim, bool) and lim else 20
        lim = max(1, lim)
        entries = self.db.list_entries(lim)
        if not entries:
            return "_No memories stored._"
        blocks = []
        for e in entries:
            body = processor.purify(e["content"])
            if e.get("parent"):
                body = body[:200] + ("…" if len(body) > 200 else "")
            tag = e["tag"] + " " if e["tag"] else ""
            blocks.append("**%s[%s]** %s\n%s" % (tag, e["id"], e["timestamp"][:19], body))
        return "\n\n---\n\n".join(blocks)

    def recall_thread(self, args):
        turn_id = args.get("turn_id") or ""
        if not isinstance(turn_id, str) or not turn_id:
            return _json({"status": "error", "reason": "turn_id required"})

        def count(v, d):
            return max(0, int(v)) if isinstance(v, (int, float)) and not isinstance(v, bool) else d

        before, after = count(args.get("before"), 2), count(args.get("after"), 2)
        entries = self.db.thread_entries()
        idx = [i for i, e in enumerate(entries) if e["turn_id"] == turn_id]
        if not idx:
            return _json({"status": "not_found", "turn_id": turn_id})
        lo, hi = max(0, idx[0] - before), min(len(entries) - 1, idx[-1] + after)
        target = set(idx)
        blocks = []
        for i in range(lo, hi + 1):
            e = entries[i]
            mark = "▶ " if i in target else "  "
            tag = e["tag"] + " " if e["tag"] else ""
            blocks.append("%s**%s[%s]** %s\n%s" % (mark, tag, e["id"], e["timestamp"][:19],
                                                  processor.purify(e["content"])))
        return "\n\n---\n\n".join(blocks)

    def repair(self, no_model_wait=False):
        if not self.repair_lock.acquire(blocking=False):
            return {"status": "ok", "message": "repair already running", "reembedded": 0, "remaining": 0}
        try:
            self.db.ensure_connected()
            return self.db.repair(no_model_wait=no_model_wait)
        finally:
            self.repair_lock.release()

    # ----------------------------------------------------------- dispatcher
    def call_tool(self, name, args, nudge=True) -> str:
        """POST /tools/<name>: always returns text (errors are sentences, spec 4.4)."""
        if not isinstance(args, dict):
            args = {}
        try:
            text = self._dispatch(name, args, nudge)
        except TimeRangeError as exc:
            text = str(exc)
        except BackendUnreachable as exc:
            text = self._backend_error(name, str(exc))
        except (redis.ConnectionError, redis.TimeoutError):
            self.db.mark_down()
            text = self._backend_error(name, self._hint())
        except Exception as exc:  # noqa: BLE001
            print("N3MC Lite: tool %s failed: %s: %s" % (name, type(exc).__name__, exc), file=sys.stderr)
            text = "Error: %s" % exc
        return with_nudge(name, text, nudge)

    call = call_tool

    def _dispatch(self, name, args, nudge=True):
        if name == "search_memory":
            return self.search(args, max_chars=self.cfg["context_max_chars"], hook=False, nudge_tail=nudge)
        if name == "save_memory":
            self.db.ensure_connected()
            return _json(self.save(args))
        if name == "list_memories":
            self.db.ensure_connected()
            return self.list_memories(args)
        if name == "recall_thread":
            self.db.ensure_connected()
            return self.recall_thread(args)
        if name == "delete_memory":
            self.db.ensure_connected()
            ident = processor.sanitize_surrogates(args.get("id") or "")
            if not isinstance(ident, str) or not ident.strip():
                return _json({"status": "error", "reason": "id required"})
            return _json(self.db.delete_memory(ident))
        if name == "delete_memories_by_session":
            self.db.ensure_connected()
            sid = processor.sanitize_surrogates(args.get("session_id") or "")
            if not isinstance(sid, str) or not sid.strip():
                return _json({"status": "error", "reason": "session_id required"})
            return _json(self.db.delete_by_session(sid))
        if name == "repair_memory":
            return _json(self.repair())
        return "Error: unknown tool '%s'" % name


ToolRunner = Service
