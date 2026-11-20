"""`# Recalled Memory Context` block, context_size, and the size limit (spec §3.17, §12.7)."""

HEADER = "# Recalled Memory Context"
SOURCE = "Source: N3MC Lite (7-day working memory)"
NO_HITS = "_No memories found._"
VERSION_LINE = ("_If several versions of the same content appear, the one with the newest `saved` time "
                "is the current one._")
WEAK_HEADING = "#### Possibly related (weak matches — may be unrelated)"
CUT_LINE = "_(cut here to keep this block within the context limit.)_"
ROW_SEP = "\n\n---\n\n"


def omitted_line(n: int) -> str:
    return f"_({n} lower-ranked match(es) omitted to keep this block within the context limit.)_"


def context_size(text: str) -> int:
    units = sum(2 if ord(c) > 0xFFFF else 1 for c in text)
    return units + text.count("\n")


def hook_head(query: str) -> str:
    q = query.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    return f"{HEADER}\n{SOURCE}\nSearch query: {q}\n\n"


def fallback_block(line: str, notices=()) -> str:
    parts = [f"> ⚠️ N3MC notice: {n}" for n in notices] + [line]
    return f"{HEADER}\n{SOURCE}\n\n" + "\n\n".join(parts) + "\n"


def empty_block() -> str:
    return f"{HEADER}\n{SOURCE}\n\n{NO_HITS}\n"


def _body(notices, rows, weak_rows, omitted=0):
    parts = [f"> ⚠️ N3MC notice: {n}" for n in notices]
    if rows:
        parts.append(VERSION_LINE)
        r = ROW_SEP.join(rows)
        if omitted:
            r += "\n\n" + omitted_line(omitted)
        parts.append(r)
    else:
        parts.append(NO_HITS)
    if weak_rows:
        parts.append(WEAK_HEADING)
        parts.append("\n\n".join(weak_rows))
    return "\n\n".join(parts)


def assemble(head: str, notices, rows, weak_rows, tail: str, max_chars=None) -> str:
    """Build the output; when max_chars is set, shrink it per §3.17."""
    full = head + _body(notices, rows, weak_rows) + tail
    if not max_chars or context_size(full) + 2 <= max_chars:
        return full
    rows = list(rows)
    omitted = 0
    while len(rows) > 3:
        rows.pop()
        omitted += 1
        out = head + _body(notices, rows, weak_rows, omitted) + tail
        if context_size(out) + 2 <= max_chars:
            return out
    text = head + _body(notices, rows, weak_rows, omitted)
    suffix = "\n\n" + CUT_LINE + tail

    def fits(n):
        return context_size(text[:n].rstrip() + suffix) + 2 <= max_chars

    lo, hi = min(len(head), len(text)), len(text)
    if not fits(lo):
        return text[:lo].rstrip() + suffix
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fits(mid):
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + suffix
