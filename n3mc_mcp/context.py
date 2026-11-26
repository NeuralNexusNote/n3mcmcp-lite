"""The search result text, the `# Recalled Memory Context` block, and its size limit (spec §3.14, §3.17, §12.7)."""
from datetime import datetime, timezone

from .processor import purify

TITLE = "# Recalled Memory Context"
SOURCE_LINE = "Source: N3MC Lite (7-day working memory)"
NO_MEMORIES = "_No memories found._"
VERSION_LINE = ("_If several versions of the same content appear, the one with the newest `saved` time is "
                "the current one._")
WEAK_HEADING = "#### Possibly related (weak matches — may be unrelated)"
CUT_LINE = "_(cut here to keep this block within the context limit.)_"
HIT_SEP = "\n\n---\n\n"
NOTICE_PREFIX = "> ⚠️ N3MC notice: "


def omitted_line(n: int) -> str:
    return f"_({n} lower-ranked match(es) omitted to keep this block within the context limit.)_"


def context_size(text: str) -> int:
    """UTF-16 code units, plus one more for every line feed (§3.17)."""
    units = sum(2 if ord(c) > 0xFFFF else 1 for c in text)
    return units + text.count("\n")


def fits(text: str, max_chars) -> bool:
    return max_chars is None or context_size(text) + 2 <= max_chars


def fmt_saved(ts) -> str:
    """The stored UTC time in the server's local time, with its offset: 2026-11-13 10:34 (UTC+0900)."""
    try:
        s = str(ts)
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone()
        return local.strftime("%Y-%m-%d %H:%M") + " (UTC" + local.strftime("%z") + ")"
    except Exception:  # noqa: BLE001
        return str(ts)


def render_hit(row: dict) -> str:
    tag = f"[doc×{row.get('chunk_count', 0)}] " if row.get("_doc") else ""
    turn = f" turn={row['turn_id']}" if row.get("turn_id") else ""
    head = f"### {tag}[{row['id']}]{turn} score={float(row.get('score', 0.0)):.3f} saved={fmt_saved(row.get('timestamp'))}"
    return head + "\n" + purify(row.get("content", ""))


def notice_lines(notices) -> list:
    return [NOTICE_PREFIX + n for n in notices or []]


def _assemble(head, notices, normal, omitted, weak, tail):
    parts = list(notice_lines(notices))
    if normal:
        parts.append(VERSION_LINE)
        parts.append(HIT_SEP.join(normal))
        if omitted:
            parts.append(omitted_line(omitted))
    else:
        parts.append(NO_MEMORIES)
    if weak:
        parts.append(WEAK_HEADING + "\n\n" + HIT_SEP.join(weak))
    return head + "\n\n".join(parts) + tail


def render_search(normal_rows, weak_rows, notices=(), head="", tail="", max_chars=None) -> str:
    """The search text of §3.14 (with head and tail), shrunk to max_chars as §3.17 says."""
    normal = [render_hit(r) for r in normal_rows]
    weak = [render_hit(r) for r in weak_rows]
    out = _assemble(head, notices, normal, 0, weak, tail)
    if fits(out, max_chars):
        return out
    kept = list(normal)
    omitted = 0
    while len(kept) > 3:
        kept.pop()
        omitted += 1
        out = _assemble(head, notices, kept, omitted, weak, tail)
        if fits(out, max_chars):
            return out
    body = _assemble(head, notices, kept, omitted, weak, "").rstrip()
    suffix = "\n\n" + CUT_LINE + tail
    lo, hi = 0, len(body)
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if fits(body[:mid].rstrip() + suffix, max_chars):
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return body[:best].rstrip() + suffix


def hook_head(query: str, max_query_chars: int = 2000) -> str:
    q = (query or "")[:max_query_chars].replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    return f"{TITLE}\n{SOURCE_LINE}\nSearch query: {q}\n\n"


def empty_block() -> str:
    return f"{TITLE}\n{SOURCE_LINE}\n\n{NO_MEMORIES}"


def degraded_block(reason: str, notices=()) -> str:
    """The fallback output of §12.7 (not limited)."""
    parts = [f"{TITLE}\n{SOURCE_LINE}"]
    parts.extend(notice_lines(notices))
    parts.append(f"_(memory search unavailable: {reason})_")
    return "\n\n".join(parts)
