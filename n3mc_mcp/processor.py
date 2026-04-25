import contextlib
import math
import re
import struct
import sys
import threading
from datetime import datetime, timezone
from typing import Optional

import numpy as np

_model = None
# Serializes get_model() so a synchronous startup preload and any
# late-arriving lazy-retry call from a tool handler cannot both run
# the heavy `from sentence_transformers import SentenceTransformer`
# path at the same time. The double-check pattern (cheap pre-lock
# read, then re-check inside the lock) keeps the fast path lock-free
# once the model is loaded.
_model_lock = threading.Lock()


@contextlib.contextmanager
def _silenced_stdout():
    """Python-level stdout redirect used on the lazy-retry path inside
    `get_model()`. Once `mcp.run()` is active, fd 1 is the JSON-RPC
    channel and we cannot safely fd-redirect (a concurrent tool
    response write would land on stderr and break the client). The
    asyncio main thread serialises tool handlers, so a Python-level
    redirect via `sys.stdout` is race-free here. Startup-time
    preloads should use the fd-level recipe in `server._main`
    instead — see Pro spec §3.9 step 4."""
    with contextlib.redirect_stdout(sys.stderr):
        yield

_CJK_RANGES = [
    (0x3000, 0x9FFF),
    (0xF900, 0xFAFF),
    (0x20000, 0x2A6DF),
]

_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```")
_PUNCT_STRIP_RE = re.compile(r'[,.<>{}\[\]"\':;!@#$%^&*()\-+=~|\\/?`]')
_FTS_SPECIAL_RE = re.compile(r'([,.<>{}\[\]"\':;!@#$%^&*()\-+=~|\\/?])')


def get_model():
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        # Re-check inside the lock: another caller may have finished
        # loading while we were waiting on the lock.
        if _model is None:
            try:
                # Python-level stdout redirect for the lazy-retry path.
                # Startup preload (server._main) does the stronger
                # fd-level redirect outside this function before the
                # MCP stdio session is active.
                with _silenced_stdout():
                    from sentence_transformers import SentenceTransformer
                    _model = SentenceTransformer("intfloat/e5-base-v2")
            except Exception as e:
                print(f"[n3mc] embedding model load failed: {e}", file=sys.stderr)
    return _model


def embed(text: str, is_query: bool = False) -> Optional[bytes]:
    model = get_model()
    if model is None:
        return None
    prefix = "query: " if is_query else "passage: "
    vec = model.encode(prefix + text, normalize_embeddings=True).astype(np.float32)
    return struct.pack(f"{len(vec)}f", *vec)


def embed_to_array(text: str, is_query: bool = False) -> Optional[np.ndarray]:
    model = get_model()
    if model is None:
        return None
    prefix = "query: " if is_query else "passage: "
    return model.encode(prefix + text, normalize_embeddings=True).astype(np.float32)


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def cjk_bigram_expand(text: str) -> str:
    tokens: list[str] = []
    run: list[str] = []

    def flush_run():
        if run:
            s = "".join(run)
            bigrams = [s[i : i + 2] for i in range(len(s) - 1)]
            tokens.extend(bigrams if bigrams else [s])
            run.clear()

    for ch in text:
        if _is_cjk(ch):
            run.append(ch)
        else:
            flush_run()
            if ch.strip():
                tokens.append(ch)
    flush_run()
    return " ".join(t for t in tokens if t.strip())


def strip_fts_punctuation(text: str) -> str:
    return _PUNCT_STRIP_RE.sub(" ", text)


def prepare_query(text: str) -> str:
    stripped = strip_fts_punctuation(text)
    expanded = cjk_bigram_expand(stripped)
    return " ".join(expanded.split()).strip()


def purify(text: str) -> str:
    return _CODE_BLOCK_RE.sub("[code omitted]", text)


def chunk_text(text: str, threshold: int, overlap: int) -> list[str]:
    if len(text) <= threshold:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + threshold
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = end - overlap
    return chunks


def cosine_sim_from_distance(distance: float) -> float:
    return max(0.0, min(1.0, 1.0 - distance))


def time_decay(timestamp_iso: str, half_life_days: float) -> float:
    try:
        ts = datetime.fromisoformat(timestamp_iso.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        days = (now - ts).total_seconds() / 86400.0
        return math.pow(2.0, -days / half_life_days)
    except Exception:
        return 1.0


def keyword_relevance(bm25_score: float, max_bm25: float, threshold: float) -> float:
    score = abs(bm25_score)
    if score < threshold:
        return 0.0
    return score / max(1.0, max_bm25)


def b_local(stored_importance: float, access_count: int, cfg: dict) -> float:
    if cfg.get("access_count_enabled", True):
        boost = min(
            cfg.get("access_count_max_boost", 0.5),
            access_count * cfg.get("access_count_weight", 0.02),
        )
    else:
        boost = 0.0
    return max(0.5, min(2.0, stored_importance + boost))


def final_score(
    cos_sim: float,
    kw_rel: float,
    decay: float,
    b_local: float,
) -> float:
    # Lite intentionally has no `b_session` factor — Pro spec §3.6:
    # "Lite は 7 日窓で自然に収束するため `b_session` を持たない".
    # The 7-day TTL window collapses freshness via `time_decay`, so
    # per-session bias would be redundant and would silently dampen
    # borderline cross-restart hits below `min_score`.
    return (cos_sim * 0.7 + kw_rel * 0.3) * decay * b_local


def lexical_rerank(
    results: list[dict], query: str, weight: float, phrase_weight: float
) -> list[dict]:
    if not query.strip() or not results:
        return results
    query_lower = query.lower()
    # Whitespace tokens + CJK bigrams so Japanese/Chinese queries also
    # contribute a coverage signal (otherwise .split() collapses an entire
    # CJK query into one token and coverage degenerates).
    query_tokens = set(query_lower.split())
    query_tokens |= set(cjk_bigram_expand(query_lower).split())

    for r in results:
        content_lower = r.get("content", "").lower()
        content_tokens = set(content_lower.split())
        content_tokens |= set(cjk_bigram_expand(content_lower).split())
        coverage = len(query_tokens & content_tokens) / max(1, len(query_tokens))
        phrase_bonus = phrase_weight if query_lower in content_lower else 0.0
        r["score"] = r.get("score", 0.0) + coverage * weight + phrase_bonus

    return sorted(results, key=lambda x: x["score"], reverse=True)
