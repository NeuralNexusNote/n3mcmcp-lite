"""
Embedding, scoring, text-processing utilities.

Spec §3.2  — intfloat/multilingual-e5-base, normalize_embeddings=True, passage:/query: prefixes
Spec §3.6  — ranking formula: (cos·0.7 + kw·0.3) × time_decay × b_local × b_session
Spec §3.7  — CJK bigram expansion, FTS punctuation stripping
"""
import contextlib
import math
import re
import struct
import sys
import threading
import unicodedata
from datetime import datetime, timezone
from typing import Optional

import numpy as np

_model = None
# Double-check locking: cheap pre-lock read keeps the fast path lock-free
# once the model is loaded; re-check inside the lock prevents a race
# where two callers both see _model is None before the first lock is acquired.
_model_lock = threading.Lock()

# Space-less script Unicode ranges — each character in these ranges is
# treated as part of a contiguous run that gets bigram-expanded for the
# `content_ngram` BM25 side channel (spec §3.7). These languages either
# have no inter-word spaces (CJK, Thai, Lao, Khmer, Myanmar) or compose
# words from syllabic blocks where bigram coverage materially helps
# partial-match recall (Hangul). Extending this list is the cheapest
# multilingual RAG lift available without adding morphological tools.
_CJK_RANGES = [
    (0x0E00, 0x0E7F),    # Thai
    (0x0E80, 0x0EFF),    # Lao
    (0x1000, 0x109F),    # Myanmar
    (0x1780, 0x17FF),    # Khmer
    (0x3000, 0x9FFF),    # CJK punct + Hiragana + Katakana + CJK Unified
    (0xAC00, 0xD7AF),    # Hangul Syllables (Korean)
    (0xF900, 0xFAFF),    # CJK Compatibility Ideographs
    (0x20000, 0x2A6DF),  # CJK Unified Ideographs Ext B
]

# Spec §3.7 regex constants.
# Match a closed fence (``` ... ```) OR an unclosed fence that reaches the
# end of the string (e.g. content truncated at a chunk boundary).
_CODE_BLOCK_RE  = re.compile(r"```[\s\S]*?(?:```|\Z)")
_PUNCT_STRIP_RE = re.compile(r'[,.<>{}\[\]"\':;!@#$%^&*()\-+=~|\\/?`]')
_FTS_SPECIAL_RE = re.compile(r'([,.<>{}\[\]"\':;!@#$%^&*()\-+=~|\\/?])')

# Lone UTF-16 surrogate halves — produced by Windows subprocess stdin
# pipes when raw UTF-8 bytes get misdecoded. They round-trip through
# json.loads but explode at .encode("utf-8") with UnicodeEncodeError,
# which in Lite would silently drop the entire save (sha1 → encode →
# raise). Same guard as Free's core/processor.py:_LONE_SURROGATE_RE.
_LONE_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


# ── unicode normalization (multilingual, CPU-only) ───────────────────────────

def normalize_text(text: str) -> str:
    """Unicode NFKC fold — collapses compatibility forms.

    Real-world impact:
      - Japanese half-width katakana (ｱﾙﾌｧ) ↔ full-width (アルファ)
      - Full-width digits/Latin (１２３ＡＢＣ) ↔ ASCII (123ABC)
      - Compat ligatures (ﬁ → fi), circled/parenthesized forms
      - Korean compat jamo, Arabic presentation forms

    Applied to the `content_ngram` side channel, the BM25 query, the
    dedup SHA, and the embedded text — never to the raw `content` field
    (verbatim recall, spec §3.11, must stay byte-for-byte).
    """
    return unicodedata.normalize("NFKC", text)


def sanitize_surrogates(text):
    """Strip lone UTF-16 surrogate halves (U+D800–U+DFFF) from text.

    Why this matters in Lite: every save path eventually calls
    `text.encode("utf-8")` (for SHA1, for the Redis HSET value, for the
    embedding input). Lone surrogates raise `UnicodeEncodeError` at that
    point, which in the current dispatch turns into a generic "Error: …"
    response and the caller's content is lost.

    Recursive over dicts/lists so nested JSON values stay clean (matches
    the Free build's contract — see Free core/processor.py).
    """
    if text is None:
        return text
    if isinstance(text, str):
        return _LONE_SURROGATE_RE.sub("", text)
    if isinstance(text, list):
        return [sanitize_surrogates(x) for x in text]
    if isinstance(text, dict):
        return {k: sanitize_surrogates(v) for k, v in text.items()}
    return text


def fold_diacritics(text: str) -> str:
    """Strip combining marks for Latin/Greek/Cyrillic cross-match.

    `café` ↔ `cafe`, `Ångström` ↔ `Angstrom`, `naïve` ↔ `naive`.
    Decompose to NFD then drop characters in Unicode category Mn (mark,
    non-spacing). Pure CPU, no language detection — letters that have no
    decomposition pass through unchanged, so CJK / Hangul / Thai are
    naturally unaffected.
    """
    nfd = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in nfd if unicodedata.category(ch) != "Mn")


# ── model loading ────────────────────────────────────────────────────────────

@contextlib.contextmanager
def _silenced_stdout():
    """Python-level stdout redirect for the lazy-retry path inside get_model().

    Once mcp.run() is active, fd 1 is the JSON-RPC channel; the stronger
    fd-level redirect used at startup (server._main) is not safe here because
    a concurrent tool-response write could land on stderr.  The asyncio main
    thread serialises tool handlers, so a Python-level redirect is race-free
    on this path.  See spec §3.9 step 4 for the full design rationale.
    """
    with contextlib.redirect_stdout(sys.stderr):
        yield


def get_model():
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is None:
            try:
                with _silenced_stdout():
                    from sentence_transformers import SentenceTransformer
                    _model = SentenceTransformer("intfloat/multilingual-e5-base")
            except Exception as e:
                print(f"[n3mc] embedding model load failed: {e}", file=sys.stderr)
    return _model


# ── embeddings ───────────────────────────────────────────────────────────────

def embed(text: str, is_query: bool = False) -> Optional[bytes]:
    """Return little-endian FLOAT32 bytes for *text*.

    Spec §3.2: always prefix with 'passage: ' at save time and 'query: ' at
    search time; normalize_embeddings=True guarantees L2-norm=1 which the
    RediSearch COSINE distance metric requires.
    """
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


# ── CJK tokenization ─────────────────────────────────────────────────────────

def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def cjk_bigram_expand(text: str) -> str:
    """Expand space-less script runs into overlapping bigrams (spec §3.7).

    Example: "記憶装置" → "記憶 憶装 装置"
    Now covers CJK Unified, Hiragana, Katakana, Hangul, Thai, Lao, Myanmar,
    and Khmer (see `_CJK_RANGES`). Languages with inter-word spaces (Latin,
    Cyrillic, Greek, Arabic, Devanagari, …) keep whole-word tokens.

    Multilingual normalization passes (applied here so save-time and
    query-time tokenization stay symmetric):
      1. NFKC fold — half-width/full-width katakana, full-width digits,
         compat ligatures all collapse to a single canonical form.
      2. Diacritic-folded duplicate of each Latin-alphabet word — emitted
         alongside the original so `café` and `cafe` match through the
         `content_ngram` BM25 channel without affecting `@content`.

    Implementation note: non-script-run, non-whitespace characters are
    accumulated into a `word` buffer and flushed as a single token on
    whitespace or script-run boundary. This preserves whole Latin words
    (e.g. "hello" stays "hello", not "h e l l o") so BM25 queries match
    them as normal tokens.
    """
    text = normalize_text(text)

    tokens: list[str] = []
    run:  list[str] = []   # accumulates a contiguous space-less script run
    word: list[str] = []   # accumulates a non-script-run, non-whitespace word

    def flush_run() -> None:
        if run:
            s = "".join(run)
            bigrams = [s[i : i + 2] for i in range(len(s) - 1)]
            tokens.extend(bigrams if bigrams else [s])
            run.clear()

    def flush_word() -> None:
        if word:
            w = "".join(word)
            tokens.append(w)
            folded = fold_diacritics(w)
            if folded and folded != w:
                tokens.append(folded)
            word.clear()

    for ch in text:
        if _is_cjk(ch):
            flush_word()
            run.append(ch)
        elif ch.strip():   # non-whitespace, non-script-run → accumulate into word
            flush_run()
            word.append(ch)
        else:              # whitespace → boundary for both buffers
            flush_run()
            flush_word()

    flush_run()
    flush_word()
    return " ".join(t for t in tokens if t.strip())


# ── query preparation ────────────────────────────────────────────────────────

def strip_fts_punctuation(text: str) -> str:
    return _PUNCT_STRIP_RE.sub(" ", text)


def prepare_query(text: str) -> str:
    """Normalize, strip FTS punct, expand bigrams, collapse whitespace.

    NFKC happens inside `cjk_bigram_expand`; calling it explicitly here
    too is harmless (idempotent) but keeps the pipeline order obvious:
    normalize → strip → bigram-expand → collapse-whitespace.
    """
    text = normalize_text(text)
    stripped = strip_fts_punctuation(text)
    expanded = cjk_bigram_expand(stripped)
    return " ".join(expanded.split()).strip()


def purify(text: str) -> str:
    """Replace triple-backtick code blocks with '[code omitted]' for display."""
    return _CODE_BLOCK_RE.sub("[code omitted]", text)


# ── chunking ─────────────────────────────────────────────────────────────────

_MIN_CHUNK_CHARS = 20  # tail chunks smaller than this are dropped (parent stores verbatim)

# Ordered by priority: prefer splitting at the earliest (highest-priority)
# natural boundary found within the lookback window. Language-agnostic:
# paragraph / line breaks cover all scripts; CJK sentence terminators
# (。！？…) avoid mid-sentence cuts in space-less languages.
_SOFT_BREAKS = (
    "\n\n",                      # paragraph break  (highest priority)
    "\n",                        # line break
    ". ", "! ", "? ",            # ASCII sentence ends
    "。", "！", "？", "…",      # CJK / universal sentence ends
    " ", "\t",                   # word boundary    (lowest priority)
)


def chunk_text(text: str, threshold: int, overlap: int) -> list[str]:
    """Sliding-window chunker with soft-boundary awareness (spec §3.11).

    Bodies at or below *threshold* chars are returned as a single-element list.
    Longer bodies are split at the best natural boundary found within the last
    *overlap* chars of each window (paragraph > line > sentence > word).
    When no soft break exists in that lookback zone a hard cut is made and the
    next window begins *overlap* chars before the cut (shared-context overlap).

    After a soft break the next window starts immediately after the break
    character(s) — no artificial overlap is added because the break is already
    a clean semantic boundary.

    Tail chunks shorter than _MIN_CHUNK_CHARS are discarded: the parent document
    stores the full body verbatim, so a tiny tail chunk adds near-zero search
    value while producing a low-quality embedding.
    """
    if len(text) <= threshold:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + threshold, len(text))
        if end == len(text):
            # Last (tail) chunk: only add if large enough or it's the only chunk.
            chunk = text[start:end]
            if len(chunk) >= _MIN_CHUNK_CHARS or not chunks:
                chunks.append(chunk)
            break
        # Search for the best natural break within the lookback window.
        lookback_start = max(start + 1, end - overlap)
        cut = -1
        for sep in _SOFT_BREAKS:
            pos = text.rfind(sep, lookback_start, end)
            if pos != -1:
                cut = pos + len(sep)
                break
        if cut != -1:
            # Soft cut: clean semantic boundary — start fresh after it.
            chunks.append(text[start:cut])
            start = cut
        else:
            # Hard cut: no break found — overlap next window for context.
            chunks.append(text[start:end])
            start = end - overlap
    return chunks


# ── scoring functions (spec §3.6) ────────────────────────────────────────────

def cosine_sim_from_distance(distance: float) -> float:
    """Convert RediSearch cosine distance [0,2] to similarity [0,1] (spec §3.6)."""
    return max(0.0, min(1.0, 1.0 - distance))


def time_decay(timestamp_iso: str, half_life_days: float) -> float:
    """Exponential decay: 2^(-days_elapsed / half_life_days) (spec §3.6).

    Malformed timestamps return 1.0 (no decay applied).
    """
    try:
        ts = datetime.fromisoformat(timestamp_iso.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        days = (now - ts).total_seconds() / 86400.0
        return math.pow(2.0, -days / half_life_days)
    except Exception:
        return 1.0


def keyword_relevance(bm25_score: float, max_bm25: float, threshold: float) -> float:
    """Normalize BM25 score to [0,1] (spec §3.6).

    |bm25_score| / max(1.0, max_|bm25_score|)
    Scores below *threshold* are treated as zero; abs() mirrors Pro's FTS5
    which can return negative values.
    """
    score = abs(bm25_score)
    if score < threshold:
        return 0.0
    return score / max(1.0, max_bm25)


def b_local(stored_importance: float, access_count: int, cfg: dict) -> float:
    """Compute the b_local importance coefficient (spec §3.6).

    b_local = clamp(0.5, 2.0, stored_importance + access_boost)
    access_boost = min(access_count_max_boost, access_count × access_count_weight)
    """
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
    b_session: float = 1.0,
) -> float:
    """Full ranking formula (spec §3.6).

    Final Score = (cos_sim × 0.7 + kw_rel × 0.3) × time_decay × b_local × b_session
    """
    return (cos_sim * 0.7 + kw_rel * 0.3) * decay * b_local * b_session


# ── lexical reranker (spec §3.6) ─────────────────────────────────────────────

def lexical_rerank(
    results: list[dict],
    query: str,
    weight: float,
    phrase_weight: float,
) -> list[dict]:
    """Lightweight post-fusion lexical rerank (spec §3.6).

    Boosts each candidate's score by:
      coverage × weight         (fraction of query tokens present in content)
      phrase_weight             (added when the full query string is a substring)

    CJK bigram tokens are added to both token sets so Japanese/Chinese queries
    contribute a real coverage signal rather than degenerating to a binary match.
    """
    if not query.strip() or not results:
        return results

    # NFKC + lowercase the query once. cjk_bigram_expand normalizes again
    # internally; the redundant pass costs nothing and keeps both sides
    # symmetric when the query has compat-form characters.
    query_lower = normalize_text(query).lower()
    query_tokens = set(query_lower.split())
    query_tokens |= set(cjk_bigram_expand(query_lower).split())

    for r in results:
        content_lower = normalize_text(r.get("content", "")).lower()
        content_tokens = set(content_lower.split())
        content_tokens |= set(cjk_bigram_expand(content_lower).split())
        coverage = len(query_tokens & content_tokens) / max(1, len(query_tokens))
        phrase_bonus = phrase_weight if query_lower in content_lower else 0.0
        # Short-document dampening: docs shorter than 50 chars receive a
        # proportional fraction of the lexical boost to reduce false-positive
        # promotion of tiny snippets that happen to contain all query tokens.
        length_factor = min(1.0, len(content_lower) / 50) if len(content_lower) < 50 else 1.0
        r["score"] = r.get("score", 0.0) + (coverage * weight + phrase_bonus) * length_factor

    return sorted(results, key=lambda x: x["score"], reverse=True)
