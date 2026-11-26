"""The search text of §3.14, the `# Recalled Memory Context` block of §12.7, and the size limit
of §3.17 (context_size, dropping the lowest rows, cutting the text)."""
from . import processor as P

TITLE = "# Recalled Memory Context"
SOURCE = "Source: N3MC Lite (7-day working memory)"
VERSION_LINE = ("_If several versions of the same content appear, the one with the newest `saved` "
                "time is the current one._")
WEAK_HEADING = "#### Possibly related (weak matches — may be unrelated)"
NO_MEMORIES = "_No memories found._"
CUT_LINE = "_(cut here to keep this block within the context limit.)_"


def omitted_line(n: int) -> str:
    return f"_({n} lower-ranked match(es) omitted to keep this block within the context limit.)_"


def notice_line(message: str) -> str:
    return f"> ⚠️ N3MC notice: {message}"


def context_size(text: str) -> int:
    """UTF-16 code units, plus one more for every newline (a newline may arrive as CR LF)."""
    return sum(2 if ord(c) > 0xFFFF else 1 for c in text) + text.count("\n")


def fits(text: str, max_chars) -> bool:
    return max_chars is None or context_size(text) + 2 <= max_chars


def render_row(row: dict) -> str:
    tag = f"[doc×{row.get('chunk_count', 0)}] " if row.get("_doc") else ""
    turn = f" turn={row['turn_id']}" if row.get("turn_id") else ""
    head = (f"### {tag}[{row['id']}]{turn} score={row.get('score', 0.0):.3f} "
            f"saved={P.fmt_saved(row.get('timestamp_epoch'))}")
    return head + "\n" + P.purify(row.get("content", ""))


def hook_head(query: str, max_query_chars: int = 2000) -> str:
    q = (query or "")[:max_query_chars].replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    return f"{TITLE}\n{SOURCE}\nSearch query: {q}"


def fallback_block(reason_line: str, notices=()) -> str:
    """The degraded output of §12.7 (not limited)."""
    parts = [f"{TITLE}\n{SOURCE}"]
    parts += [notice_line(n) for n in notices]
    parts.append(reason_line)
    return "\n\n".join(parts)


def _compose(head, notices, row_texts, weak_texts, tail, omitted=0) -> str:
    parts = [notice_line(n) for n in notices]
    if row_texts:
        parts.append(VERSION_LINE)
        parts.append("\n\n---\n\n".join(row_texts))
        if omitted:
            parts.append(omitted_line(omitted))
    else:
        parts.append(NO_MEMORIES)
    if weak_texts:
        parts.append(WEAK_HEADING)
        parts.extend(weak_texts)
    body = "\n\n".join(parts)
    return (head + "\n\n" + body if head else body) + (tail or "")


def render(rows, weak, notices=(), head="", tail="", max_chars=None) -> str:
    row_texts = [render_row(r) for r in rows]
    weak_texts = [render_row(r) for r in weak]
    out = _compose(head, notices, row_texts, weak_texts, tail)
    if fits(out, max_chars):
        return out
    omitted = 0
    while len(row_texts) > 3:
        row_texts.pop()
        omitted += 1
        out = _compose(head, notices, row_texts, weak_texts, tail, omitted)
        if fits(out, max_chars):
            return out
    tail = tail or ""
    body = out[: len(out) - len(tail)] if tail else out
    floor = len(head)

    def candidate(n):
        return body[:n].rstrip() + "\n\n" + CUT_LINE + tail

    lo, hi = floor, len(body)
    best = floor
    while lo <= hi:
        mid = (lo + hi) // 2
        if fits(candidate(mid), max_chars):
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return candidate(best)
