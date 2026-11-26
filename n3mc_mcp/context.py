"""Rendering of the search results (spec §3.14, §5 (4)), the `# Recalled Memory Context` block
(spec §12.7) and the context size limit (spec §3.17).
"""
from . import processor

HEADER_LINE = "# Recalled Memory Context"
SOURCE_LINE = "Source: N3MC Lite (7-day working memory)"
NO_MEMORIES = "_No memories found._"
VERSION_LINE = ("_If several versions of the same content appear, the one with the newest `saved` time is "
                "the current one._")
WEAK_HEADING = "#### Possibly related (weak matches — may be unrelated)"
NOTICE_PREFIX = "> ⚠️ N3MC notice: "
OMITTED = "_({n} lower-ranked match(es) omitted to keep this block within the context limit.)_"
CUT_LINE = "_(cut here to keep this block within the context limit.)_"
ROW_SEP = "\n\n---\n\n"
PART_SEP = "\n\n"


def context_size(text: str) -> int:
    """UTF-16 code units (a character outside the BMP counts 2) plus one more per newline."""
    return sum(2 if ord(c) > 0xFFFF else 1 for c in text) + text.count("\n")


def fits(text: str, max_chars) -> bool:
    return max_chars is None or context_size(text) + 2 <= max_chars


def render_row(row: dict) -> str:
    tag = f"[doc×{row.get('chunk_count', 0)}] " if row.get("_doc") else ""
    turn = f" turn={row['turn_id']}" if row.get("turn_id") else ""
    head = (f"### {tag}[{row.get('id', '')}]{turn} score={float(row.get('score', 0.0)):.3f} "
            f"saved={processor.fmt_saved(row.get('timestamp', ''))}")
    return head + "\n" + processor.purify(row.get("content", ""))


def hook_head(query: str, max_query_chars: int) -> str:
    q = (query or "")[:max_query_chars].replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return f"{HEADER_LINE}\n{SOURCE_LINE}\nSearch query: {q}\n\n"


def fallback_block(message: str, notices=()) -> str:
    """The block the CLI prints itself when there is no search (spec §12.7). No final newline."""
    parts = [f"{NOTICE_PREFIX}{m}" for m in notices] + [message]
    return f"{HEADER_LINE}\n{SOURCE_LINE}\n\n" + PART_SEP.join(parts)


def _assemble(head, notices, rows, omitted, weak_rows, tail):
    parts = [f"{NOTICE_PREFIX}{m}" for m in notices]
    if rows:
        parts.append(VERSION_LINE)
        text = ROW_SEP.join(rows)
        if omitted:
            text += PART_SEP + OMITTED.format(n=omitted)
        parts.append(text)
    else:
        parts.append(NO_MEMORIES)
    if weak_rows:
        parts.append(WEAK_HEADING)
        parts.extend(weak_rows)
    return head + PART_SEP.join(parts) + tail


def render_search(normal, weak, notices=(), head: str = "", tail: str = "", max_chars=None) -> str:
    """The search text (spec §3.14), shrunk to max_chars by the steps of spec §3.17 when given."""
    rows = [render_row(r) for r in normal]
    weak_rows = [render_row(r) for r in weak]
    notices = list(notices)
    out = _assemble(head, notices, rows, 0, weak_rows, tail)
    if fits(out, max_chars):
        return out
    kept = list(rows)
    omitted = 0
    while len(kept) > 3:
        kept.pop()
        omitted += 1
        out = _assemble(head, notices, kept, omitted, weak_rows, tail)
        if fits(out, max_chars):
            return out
    base = _assemble(head, notices, kept, omitted, weak_rows, "")

    def cut(k):
        prefix = base[:k].rstrip()
        if len(prefix) < len(head.rstrip()):
            prefix = head.rstrip()
        return prefix + PART_SEP + CUT_LINE + tail

    lo, hi = len(head.rstrip()), len(base)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fits(cut(mid), max_chars):
            lo = mid
        else:
            hi = mid - 1
    while lo > 0 and not fits(cut(lo), max_chars):
        lo -= 1
    return cut(lo)
