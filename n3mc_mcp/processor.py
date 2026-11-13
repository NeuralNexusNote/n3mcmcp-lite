"""
Embedding, scoring, text-processing utilities.

Spec §3.2  — intfloat/multilingual-e5-base, normalize_embeddings=True, passage:/query: prefixes
Spec §3.6  — ranking formula: (cos·0.7 + kw·0.3) × time_decay × b_local × b_session
Spec §3.7  — query cleaning (prepare_query), CJK bigram expansion,
             control-char + ASCII/CJK punctuation stripping
Spec §3.14 — search result shaping (vector floor, newest version first,
             relative cut) as pure, Redis-free functions
"""
import contextlib
import math
import re
import struct
import sys
import threading
import time
import unicodedata
from datetime import datetime, timezone
from typing import Optional

import numpy as np

_model = None
# Embedding model name (spec §3.2 default). Configurable via the
# `embedding_model` config field so each deployment can pick its own
# quality / resource / language tradeoff (e5-base ↔ e5-large ↔ a
# domain-specific multilingual model) without editing code. The product
# stays language-neutral: the choice is the operator's, not baked in.
# Changing the model usually changes the vector dimension too — keep
# `embedding_dim` (config) in sync and flush the index when switching.
_DEFAULT_MODEL_NAME = "intfloat/multilingual-e5-base"
_model_name = _DEFAULT_MODEL_NAME
# Double-check locking: cheap pre-lock read keeps the fast path lock-free
# once the model is loaded; re-check inside the lock prevents a race
# where two callers both see _model is None before the first lock is acquired.
_model_lock = threading.Lock()


def set_model_name(name: str) -> None:
    """Set the embedding model name BEFORE the first get_model() call.

    Called once at startup from the resolved config (server._main). Has no
    effect once the model is already loaded — a model swap requires a process
    restart (and an index flush if the vector dimension changed).
    """
    global _model_name
    if name and isinstance(name, str) and name.strip():
        _model_name = name.strip()

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
# v1.7.0: control characters make FT.SEARCH fail with "Syntax error", and
# CJK / full-width punctuation turned into never-matching bigrams (「記, は？)
# that zeroed the AND-joined BM25 leg. Both are replaced with spaces on the
# query side AND in content_ngram at save time. Every RediSearch special
# character is in this set, so no escaping is needed afterwards.
_CTRL_RE = re.compile(r'[\x00-\x1f\x7f]')
_PUNCT_STRIP_RE = re.compile(
    r'[,.<>{}\[\]"\':;!@#$%^&*()\-+=~|\\/?`'
    r'「」『』【】（）〈〉《》〔〕［］｛｝・、。，．！？；：―─…“”‘’]'
)

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


_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Zs", "Zl", "Zp"})


def _is_variation_selector(ch: str) -> bool:
    cp = ord(ch)
    return 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF


def has_visible_text(text: str) -> bool:
    """True if `text` has at least one visible character (spec §3.13).

    Invisible = whitespace, control / format characters (Cc / Cf, e.g.
    U+0000, U+001B, U+200B, U+FEFF), separators (Zs / Zl / Zp) and variation
    selectors. Only decides whether save_memory accepts the content; the
    stored content is never changed by this check.
    """
    for ch in text:
        if ch.isspace() or _is_variation_selector(ch):
            continue
        if unicodedata.category(ch) in _INVISIBLE_CATEGORIES:
            continue
        return True
    return False


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
    """Python-level stdout redirect around the model load inside get_model().

    The model loads in a background thread after the stdio loop starts
    (spec §3.9 step 4), when fd 1 is the JSON-RPC channel; an fd-level
    redirect is not safe then because a concurrent tool-response write could
    land on stderr. The MCP stdio transport writes through its own wrapper
    of sys.stdout.buffer, so swapping sys.stdout only diverts stray Python
    prints from the library load to stderr.
    """
    with contextlib.redirect_stdout(sys.stderr):
        yield


def import_backend() -> None:
    """Import the embedding library (spec §3.9 step 4, part 1).

    Called synchronously BEFORE the stdio loop starts. Importing
    sentence_transformers (torch, sklearn, ...) can spawn child processes on
    Windows; once the MCP stdio reader has a synchronous read pending on the
    stdin pipe, creating a child process blocks on that handle and the server
    deadlocks. Only the model construction (part 2) runs in the background.
    Non-fatal: on failure the model load is retried lazily.
    """
    try:
        import sentence_transformers  # noqa: F401
    except Exception as e:
        print(f"[n3mc] embedding library import failed: {e}", file=sys.stderr)


# Spec §3.9 step 4 (v1.7.0): after a failed load, retry at most once every
# MODEL_RETRY_SECONDS. Without this, an offline first run retried the
# Hugging Face download inside every tool call (~12-24 s per call).
MODEL_RETRY_SECONDS = 60
_model_error: Optional[str] = None
_model_failed_at: float = 0.0


def get_model():
    global _model, _model_error, _model_failed_at
    if _model is not None:
        return _model
    if _model_error and time.monotonic() - _model_failed_at < MODEL_RETRY_SECONDS:
        return None
    with _model_lock:
        if _model is None:
            if _model_error and time.monotonic() - _model_failed_at < MODEL_RETRY_SECONDS:
                return None
            try:
                with _silenced_stdout():
                    from sentence_transformers import SentenceTransformer
                    _model = SentenceTransformer(_model_name)
                _model_error = None
            except Exception as e:
                _model_error = f"{type(e).__name__}: {e}"
                _model_failed_at = time.monotonic()
                print(f"[n3mc] embedding model load failed ({_model_name}): {e} "
                      f"- retrying in {MODEL_RETRY_SECONDS}s. "
                      "Check the network / proxy (the first start downloads it "
                      "from Hugging Face).", file=sys.stderr)
    return _model


def model_status() -> tuple[bool, Optional[str]]:
    """(loaded, last load error). Spec §3.9 degraded-mode notice."""
    return _model is not None, (None if _model is not None else _model_error)


def model_unavailable_message() -> Optional[str]:
    """Degraded-mode text (English) or None when the model is fine / not yet tried."""
    loaded, error = model_status()
    if loaded or not error:
        return None
    return (
        f"The embedding model ({_model_name}) could not be loaded ({error}), so "
        "search is keyword-only (BM25) and new entries are saved without "
        "vectors. The first start downloads the model (~440 MB) from Hugging "
        "Face: check the network or proxy, or pre-download it. The server "
        "retries automatically."
    )


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

def strip_control_chars(text: str) -> str:
    """Replace C0 control characters and DEL with spaces (spec §3.7)."""
    return _CTRL_RE.sub(" ", text)


def strip_fts_punctuation(text: str) -> str:
    """Replace ASCII and CJK / full-width punctuation with spaces (spec §3.7)."""
    return _PUNCT_STRIP_RE.sub(" ", text)


def prepare_query(text: str) -> str:
    """BM25 query cleaning (spec §3.7, v1.7.0).

    NFKC → control chars to space → strip ASCII + CJK punctuation →
    CJK bigram expansion → collapse whitespace. The result contains no
    RediSearch special characters, so it is used without escaping.
    An empty result means "skip BM25, vector only".
    """
    text = normalize_text(text)
    text = strip_control_chars(text)
    text = strip_fts_punctuation(text)
    tokens = cjk_bigram_expand(text).split()
    # Tokens with no letter or digit (e.g. a lone "_") are not indexed by
    # RediSearch and leave an empty "@content:()" -> syntax error (spec §3.7).
    return " ".join(t for t in tokens if any(ch.isalnum() for ch in t))


def drop_lone_cjk_tokens(fts_query: str) -> str:
    """Drop single-character space-less-script tokens from a cleaned query.

    RediSearch ANDs query terms. After punctuation stripping, a one-character
    CJK run such as the particle `は` in `「記憶装置」は？` stays a lone token
    (`記憶 憶装 装置 は`); it only matches a document whose content_ngram has
    that character standing alone, so it zeroes the whole BM25 leg. Lone
    tokens are dropped when at least one other token remains (a query that
    is a single character is kept as is). Applied by Database._bm25_search
    on top of `prepare_query`, whose output stays exactly as in spec §3.7.
    """
    tokens = fts_query.split()
    kept = [t for t in tokens if not (len(t) == 1 and _is_cjk(t))]
    return " ".join(kept) if kept else fts_query


def content_ngram(text: str) -> str:
    """Save-time `content_ngram` field value (spec §3.7).

    Built with the same cleaning as `prepare_query` (NFKC, control chars,
    ASCII + CJK punctuation stripping, bigram expansion) so the save side
    and the query side tokenize symmetrically. The raw `content` field is
    stored verbatim and is never passed through this.
    """
    return prepare_query(text)


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


# ── search result shaping (spec §3.14, v1.7.0) ───────────────────────────────
#
# Pure functions so they can be tested without Redis or the embedding model.
# database.Database.search_memory runs them in this order:
#   apply_vector_floor (before fusion) → fusion → parent resolution →
#   lexical rerank → min_score → order_versions_newest_first →
#   relative_cut → TTL refresh / access_count (top-K) → truncate to limit.

def apply_vector_floor(vec_results: dict, min_cos: float) -> dict:
    """Drop vector candidates whose `cos_sim < min_cos` (spec §3.14 step 2).

    *vec_results* maps a key to a row dict carrying `cos_sim`. A dropped row
    can still enter the fusion through BM25, where it gets cos_sim = 0.0.
    `min_cos <= 0` disables the floor.
    """
    if not min_cos or min_cos <= 0:
        return dict(vec_results)
    return {
        k: v for k, v in vec_results.items()
        if float(v.get("cos_sim", 0.0)) >= min_cos
    }


def vector_from_bytes(raw) -> Optional[np.ndarray]:
    """Decode a FLOAT32 little-endian embedding blob; None when unusable."""
    if not raw:
        return None
    try:
        vec = np.frombuffer(raw, dtype="<f4").astype(np.float32)
    except Exception:
        return None
    if vec.size == 0:
        return None
    return vec


def _row_sort_key(row: dict) -> tuple:
    try:
        ts = float(row.get("timestamp_epoch") or 0.0)
    except (TypeError, ValueError):
        ts = 0.0
    return (ts, str(row.get("id", "")))


def order_versions_newest_first(
    rows: list[dict],
    rep_vectors: list,
    cfg: dict,
    rep_texts: Optional[list] = None,
) -> list[dict]:
    """Put the newest version of the same content first (spec §3.14 step 4).

    rows         -- ranked result rows (score-descending), already through
                    fusion, parent resolution, rerank and min_score.
    rep_vectors  -- one entry per row: the row's representative embedding
                    (standalone row → its own embedding; parent document →
                    the embedding of its first chunk `chunk_ids[0]`), as a
                    numpy array, raw FLOAT32 bytes, or None (row is skipped).
    rep_texts    -- one entry per row: the representative text (standalone
                    `content`, or the first chunk's `content`). Defaults to
                    each row's `content`. Rows whose representative text is
                    shorter than `version_min_chars` are skipped.
    cfg          -- uses version_order_enabled / version_sim_threshold /
                    version_min_chars.

    Rows whose representative cosine is >= version_sim_threshold are grouped
    transitively. Inside each group the rows are re-ordered newest first by
    (timestamp_epoch, id) over the SAME positions; every position keeps its
    score, so the newest version inherits the group's best score. Every
    member of a group of two or more rows is marked with `_version_group`
    (the group number, 1-based). Nothing is deleted. Returns a new list.
    """
    rows = list(rows)
    if not cfg.get("version_order_enabled", True) or len(rows) < 2:
        return rows
    threshold = float(cfg.get("version_sim_threshold", 0.98))
    min_chars = int(cfg.get("version_min_chars", 100))
    if rep_texts is None:
        rep_texts = [r.get("content", "") for r in rows]

    # Eligible rows: usable vector + long-enough representative text.
    idx: list[int] = []
    vecs: list[np.ndarray] = []
    for i, row in enumerate(rows):
        v = rep_vectors[i] if i < len(rep_vectors) else None
        if v is not None and not isinstance(v, np.ndarray):
            v = vector_from_bytes(v)
        text = rep_texts[i] if i < len(rep_texts) else ""
        if v is None or len(text or "") < min_chars:
            continue
        norm = float(np.linalg.norm(v))
        if norm == 0.0:
            continue
        idx.append(i)
        vecs.append(v / norm)
    if len(idx) < 2:
        return rows
    dims = {v.shape for v in vecs}
    if len(dims) != 1:
        return rows

    mat = np.vstack(vecs)
    sims = mat @ mat.T

    # Transitive grouping (union-find over eligible rows).
    parent = list(range(len(idx)))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    n = len(idx)
    for a in range(n):
        for b in range(a + 1, n):
            if sims[a, b] >= threshold:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[rb] = ra

    groups: dict[int, list[int]] = {}
    for a in range(n):
        groups.setdefault(find(a), []).append(idx[a])

    out = list(rows)
    group_no = 0
    # Iterate groups in order of their best (first) position for stable numbering.
    for members in sorted(groups.values(), key=lambda m: min(m)):
        if len(members) < 2:
            continue
        group_no += 1
        positions = sorted(members)
        slot_scores = [rows[p].get("score", 0.0) for p in positions]
        newest_first = sorted(
            (rows[p] for p in positions), key=_row_sort_key, reverse=True
        )
        for pos, score, row in zip(positions, slot_scores, newest_first):
            moved = dict(row)
            moved["score"] = score
            moved["_version_group"] = group_no
            out[pos] = moved
    return out


def relative_cut(rows: list[dict], ratio: float) -> list[dict]:
    """Drop rows scoring below `ratio × top score` (spec §3.14 step 5).

    Rows marked `_version_group` are always kept (old versions stay visible
    next to the new one). `ratio <= 0` disables the cut. Order is preserved.
    """
    if not rows or not ratio or ratio <= 0:
        return list(rows)
    top = max(float(r.get("score", 0.0)) for r in rows)
    floor = ratio * top
    return [
        r for r in rows
        if r.get("_version_group") or float(r.get("score", 0.0)) >= floor
    ]
