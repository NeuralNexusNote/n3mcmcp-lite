"""`# Recalled Memory Context` block, context_size, size limit (spec sections 3.14, 3.17, 12.7)."""

from datetime import datetime, timezone

from . import processor

SOURCE_LINE = "Source: N3MC Lite (7-day working memory)"
HEADING = "# Recalled Memory Context"
VERSION_SENTENCE = ("_If several versions of the same content appear, the one with the newest `saved` "
                    "time is the current one._")
WEAK_HEADING = "#### Possibly related (weak matches — may be unrelated)"
NO_MEMORIES = "_No memories found._"
CUT_MARK = "_(cut here to keep this block within the context limit.)_"
SEP = "\n\n---\n\n"


def context_size(text: str) -> int:
    """UTF-16 code units plus one extra per newline (a Windows pipe may deliver CR LF)."""
    return len(text.encode("utf-16-le")) // 2 + text.count("\n")


def fits(text: str, max_chars: int) -> bool:
    return context_size(text) + 2 <= max_chars


def fmt_saved(ts_iso: str) -> str:
    """The stored UTC timestamp in the server's local time, with the offset."""
    try:
        dt = datetime.fromisoformat(ts_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone()
        off = local.strftime("%z") or "+0000"
        return "%s (UTC%s)" % (local.strftime("%Y-%m-%d %H:%M"), off)
    except (ValueError, TypeError):
        return str(ts_iso)


def format_row(row) -> str:
    tag = "[doc×%s] " % row.get("chunk_count", 0) if row.get("_is_parent") else ""
    turn = " turn=%s" % row["turn_id"] if row.get("turn_id") else ""
    head = "### %s[%s]%s score=%.3f saved=%s" % (tag, row["id"], turn, row.get("score", 0.0),
                                                  fmt_saved(row.get("timestamp", "")))
    return head + "\n" + processor.purify(row.get("content", ""))


def _assemble(notices, normal, omit_note, weak, head, tail, has_any):
    parts = ["> ⚠️ N3MC notice: %s" % n for n in notices]
    if normal:
        parts.append(VERSION_SENTENCE)
        parts.append(SEP.join(normal))
        if omit_note:
            parts.append(omit_note)
    else:
        parts.append(NO_MEMORIES)
    if weak:
        parts.append(WEAK_HEADING)
        parts.extend(weak)
    return head + "\n\n".join(parts), tail


def render_search(normal_rows, weak_rows, notices, head="", tail="", max_chars=None):
    """Build the search text. head/tail are not shrunk; max_chars applies the 3.17 limit."""
    normal = [format_row(r) for r in normal_rows]
    weak = [format_row(r) for r in weak_rows]
    body, t = _assemble(notices, normal, None, weak, head, tail, True)
    full = body + t
    if max_chars is None or fits(full, max_chars):
        return full
    removed = 0
    while len(normal) > 3:
        normal.pop()
        removed += 1
        note = "_(%d lower-ranked match(es) omitted to keep this block within the context limit.)_" % removed
        body, t = _assemble(notices, normal, note, weak, head, tail, True)
        if fits(body + t, max_chars):
            return body + t
    # Step 2: cut the text (the tail stays).
    lo, hi = 0, len(body)
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = body[:mid].rstrip() + "\n\n" + CUT_MARK + t
        if fits(cand, max_chars):
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return body[:best].rstrip() + "\n\n" + CUT_MARK + t


def hook_head(query: str, max_len: int) -> str:
    q = " ".join(query[:max_len].split("\n")).replace("\r", " ")
    return "%s\n%s\nSearch query: %s\n\n" % (HEADING, SOURCE_LINE, q)


def empty_block() -> str:
    return "%s\n%s\n\n%s\n" % (HEADING, SOURCE_LINE, NO_MEMORIES)
