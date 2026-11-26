"""Embedding, ranking math, tokenization (NFKC, punctuation, CJK bigrams), purify, chunking,
the lexical reranker, and the encoding safety helpers (spec §3.2, §3.6, §3.7, §3.9, §3.11, §3.13, §5 (5)–(7)).

This module never imports sentence_transformers or torch at import time: the MCP thin client and the
direct save import it (spec §4.6).
"""
import math
import os
import re
import sys
import threading
import time
import unicodedata

# ── Encoding safety (§3.13) ──────────────────────────────────────────────────

_LONE_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def sanitize_surrogates(text):
    if isinstance(text, str):
        return _LONE_SURROGATE_RE.sub("", text)
    if isinstance(text, list):
        return [sanitize_surrogates(x) for x in text]
    if isinstance(text, dict):
        return {k: sanitize_surrogates(v) for k, v in text.items()}
    return text


_INVISIBLE_CATEGORIES = {"Cc", "Cf", "Zs", "Zl", "Zp"}
_INVISIBLE_CHARS = {0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800}


def _is_invisible(ch: str) -> bool:
    cp = ord(ch)
    if ch.isspace():
        return True
    if unicodedata.category(ch) in _INVISIBLE_CATEGORIES:
        return True
    if 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF:
        return True
    return cp in _INVISIBLE_CHARS


def has_visible_text(text) -> bool:
    """True when the text holds at least one visible character (§3.13). Combining marks count."""
    if not isinstance(text, str):
        return False
    return any(not _is_invisible(ch) for ch in text)


# ── Normalization, punctuation, CJK bigrams (§3.7, §5 (5)) ───────────────────

def normalize_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


_CTRL_RE = re.compile(r'[\x00-\x1f\x7f]')
_PUNCT_STRIP_RE = re.compile(
    r'[,.<>{}\[\]"\':;!@#$%^&*()\-+=~|\\/?`'
    r'「」『』【】（）〈〉《》〔〕［］｛｝・、。，．！？；：―─…“”‘’]'
)
_WS_RE = re.compile(r"\s+")

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


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    for lo, hi in _CJK_RANGES:
        if lo <= cp <= hi:
            return True
    return False


def fold_diacritics(text: str) -> str:
    """NFD, then drop every character of category Mn (café -> cafe)."""
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def strip_fts_punctuation(text: str) -> str:
    return _PUNCT_STRIP_RE.sub(" ", text)


def cjk_bigram_expand(text: str) -> str:
    """Expand each run of CJK characters into overlapping bigrams (a one-character run stays as it is);
    other runs stay, and their diacritic-folded form is added when it differs (§3.7, §5 (5))."""
    out = []
    run = []
    run_cjk = None

    def flush():
        if not run:
            return
        s = "".join(run)
        if run_cjk:
            if len(s) == 1:
                out.append(s)
            else:
                out.append(" ".join(s[i:i + 2] for i in range(len(s) - 1)))
        else:
            out.append(s)
            for word in s.split():
                folded = fold_diacritics(word)
                if folded != word and folded.strip():
                    out.append(folded)
        run.clear()

    for ch in text:
        if ch.isspace():
            flush()
            run_cjk = None
            out.append(" ")
            continue
        c = _is_cjk(ch)
        if run and c != run_cjk:
            flush()
        run_cjk = c
        run.append(ch)
    flush()
    return _WS_RE.sub(" ", " ".join(out)).strip()


def prepare_query(text: str) -> str:
    text = normalize_text(text)              # NFKC
    text = _CTRL_RE.sub(' ', text)
    text = _PUNCT_STRIP_RE.sub(' ', text)
    tokens = cjk_bigram_expand(text).split()
    # Tokens with no letter or digit (e.g. a lone '_') are not indexed by
    # RediSearch and leave an empty '@content_fts:()' → syntax error.
    return ' '.join(t for t in tokens if any(ch.isalnum() for ch in t))


def drop_lone_cjk_tokens(q: str) -> str:
    # BM25 query only: drop single-character CJK tokens (particles such as
    # は / の / を left over by bigram expansion) when other tokens remain.
    tokens = q.split()
    kept = [t for t in tokens if not (len(t) == 1 and _is_cjk(t))] or tokens
    # Hiragana-only tokens (して / ます / どう) too, when
    # another token remains.
    content = [t for t in kept if not all(
        0x3040 <= ord(c) <= 0x309F or c == 'ー' for c in t)]
    return ' '.join(content or kept)


def build_ngram(fts_text: str) -> str:
    """content_ngram: the index text NFKC-normalized, punctuation removed, bigram-expanded (§3.7)."""
    return prepare_query(fts_text)


# ── Code blocks (§3.13) ──────────────────────────────────────────────────────

CODE_OMITTED = "[code omitted]"
_FENCE_RE = re.compile(r"```[\s\S]*?(?:```|\Z)")


def purify(text: str) -> str:
    if not isinstance(text, str):
        return text
    return _FENCE_RE.sub(CODE_OMITTED, text)


def fts_text(body: str) -> str:
    """The index text: the body before the role prefix, with every [code omitted] replaced by a space."""
    return body.replace(CODE_OMITTED, " ")


# ── Chunking (§3.11, §5 (7)) ─────────────────────────────────────────────────

_MIN_CHUNK_CHARS = 20
_SOFT_BREAKS = (
    "\n\n",                      # paragraph break  (highest priority)
    "\n",                        # line break
    ". ", "! ", "? ",            # ASCII sentence ends
    "。", "！", "？", "…",      # CJK / universal sentence ends
    " ", "\t",                   # word boundary    (lowest priority)
)


def chunk_text(text: str, threshold: int, overlap: int) -> list:
    """Sliding-window chunks. Every window moves forward by at least one character; every character of
    the text is in at least one chunk (a final window shorter than 20 characters joins the previous one)."""
    n = len(text)
    if n <= threshold:
        return [text]
    threshold = max(1, int(threshold))
    overlap = max(0, int(overlap))
    spans = []
    start = 0
    while start < n:
        end = min(start + threshold, n)
        if end >= n:
            if spans and (n - start) < _MIN_CHUNK_CHARS:
                ps, _pe = spans[-1]
                spans[-1] = (ps, n)
            else:
                spans.append((start, n))
            break
        search_from = max(start + 1, end - overlap)
        cut = None
        if search_from < end:
            for brk in _SOFT_BREAKS:
                idx = text.rfind(brk, search_from, end)
                if idx != -1:
                    cut = idx + len(brk)
                    break
        if cut is not None and cut > start:
            spans.append((start, cut))
            start = cut
        else:
            spans.append((start, end))
            start = max(start + 1, end - overlap)
    return [text[s:e] for s, e in spans]


# ── Ranking (§3.6) ───────────────────────────────────────────────────────────

def cos_from_distance(distance) -> float:
    try:
        d = float(distance)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(d):
        return 0.0
    return max(0.0, min(1.0, 1.0 - d))


def cosine(a, b) -> float:
    if a is None or b is None:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def time_decay(timestamp_epoch, half_life_days, now=None) -> float:
    try:
        ts = float(timestamp_epoch)
        hl = float(half_life_days)
        if not math.isfinite(ts) or hl <= 0:
            return 1.0
    except (TypeError, ValueError):
        return 1.0
    now = time.time() if now is None else now
    days = max(0.0, (now - ts) / 86400.0)
    return 2.0 ** (-days / hl)


def keyword_relevance(bm25, max_bm25, threshold) -> float:
    try:
        s = abs(float(bm25))
    except (TypeError, ValueError):
        return 0.0
    if s < threshold:
        return 0.0
    return s / max(1.0, abs(float(max_bm25 or 0.0)))


def b_local(importance) -> float:
    """The stored importance, clamped to 0.5-2.0 (1.0 when unreadable). A search writes nothing to a record:
    no hit counter is kept (writing to an indexed hash makes RediSearch index it again, and the stale entries
    lower the BM25 of every term in it until its GC runs)."""
    try:
        imp = float(importance)
    except (TypeError, ValueError):
        imp = 1.0
    if math.isnan(imp):
        imp = 1.0
    return max(0.5, min(2.0, imp))


def final_score(cos, kw, decay, blocal, bsession) -> float:
    return (cos * 0.7 + kw * 0.3) * decay * blocal * bsession


def rerank_tokens(text: str) -> set:
    return set(prepare_query(text or "").lower().split())


def lexical_bonus(query: str, content: str, weight: float, phrase_weight: float) -> float:
    """(coverage × weight + phrase_bonus) × length_factor (§3.6, §5 (6))."""
    qt = rerank_tokens(query)
    norm_content = normalize_text(content or "").lower()
    if not qt:
        coverage = 0.0
    else:
        ct = rerank_tokens(content)
        coverage = len(qt & ct) / len(qt)
    q = normalize_text(query or "").strip().lower()
    phrase = phrase_weight if q and q in norm_content else 0.0
    bonus = coverage * weight + phrase
    n = len(norm_content)
    length_factor = n / 50.0 if n < 50 else 1.0
    return bonus * length_factor


# ── The embedding model (§3.2, §3.9) ─────────────────────────────────────────

MODEL_RETRY_SECONDS = 60
MODEL_WAIT_SECONDS = 20

_MODEL = None
_MODEL_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
_model_name = "intfloat/multilingual-e5-base"
_last_error = None
_last_attempt = 0.0
_loading = False
_last_logged = 0.0


def configure(model_name: str):
    global _model_name
    if model_name:
        _model_name = model_name


def model_name() -> str:
    return _model_name


def import_backend():
    """Import the libraries synchronously (before stdio / uvicorn), with fd 1 pointed at stderr."""
    saved = None
    try:
        try:
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            pass
        try:
            saved = os.dup(1)
            os.dup2(2, 1)
        except OSError:
            saved = None
        import sentence_transformers  # noqa: F401
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[n3mc-lite] could not import sentence_transformers: {type(e).__name__}: {e}", file=sys.stderr)
        return False
    finally:
        if saved is not None:
            try:
                os.dup2(saved, 1)
                os.close(saved)
            except OSError:
                pass


def _build_model():
    from sentence_transformers import SentenceTransformer
    try:
        import huggingface_hub.constants as hc
        old = getattr(hc, "HF_HUB_OFFLINE", False)
        hc.HF_HUB_OFFLINE = True
        try:
            return SentenceTransformer(_model_name, local_files_only=True)
        finally:
            hc.HF_HUB_OFFLINE = old
    except Exception:  # noqa: BLE001
        return SentenceTransformer(_model_name)


def _load_locked():
    """Called with _MODEL_LOCK held."""
    global _MODEL, _last_error, _last_attempt, _last_logged
    if _MODEL is not None:
        return _MODEL
    if _last_error is not None and time.monotonic() - _last_attempt < MODEL_RETRY_SECONDS:
        return None
    _last_attempt = time.monotonic()
    try:
        _MODEL = _build_model()
        _last_error = None
        return _MODEL
    except Exception as e:  # noqa: BLE001
        _last_error = f"{type(e).__name__}: {e}"
        if time.monotonic() - _last_logged >= MODEL_RETRY_SECONDS or _last_logged == 0.0:
            _last_logged = time.monotonic()
            print(f"[n3mc-lite] the embedding model could not be loaded: {_last_error}", file=sys.stderr)
        return None


def _background_load():
    global _loading
    try:
        with _MODEL_LOCK:
            _load_locked()
    finally:
        with _STATE_LOCK:
            _loading = False


def start_background_load():
    """Start loading the model on a background thread unless it is loaded, loading, or waiting for
    its retry."""
    global _loading
    with _STATE_LOCK:
        if _MODEL is not None or _loading:
            return False
        if _last_error is not None and time.monotonic() - _last_attempt < MODEL_RETRY_SECONDS:
            return False
        _loading = True
    t = threading.Thread(target=_background_load, name="n3mc-model-load", daemon=True)
    t.start()
    return True


def is_loading() -> bool:
    return _loading or _MODEL_LOCK.locked()


def get_model(no_wait: bool = False):
    """The model, or None. no_wait: never take the lock or load here; start a background load when
    nothing is loading. Otherwise wait for the lock for at most MODEL_WAIT_SECONDS (§3.9)."""
    if _MODEL is not None:
        return _MODEL
    if no_wait:
        start_background_load()
        return None
    if not _MODEL_LOCK.acquire(timeout=MODEL_WAIT_SECONDS):
        return None
    try:
        return _load_locked()
    finally:
        _MODEL_LOCK.release()


def loading_message() -> str:
    return (f"The embedding model ({_model_name}) is still loading (the first start downloads ~1.1 GB), "
            "so this search used keywords only and new entries are saved without vectors. Retry in a minute.")


def model_unavailable_message():
    """None when the model is loaded; otherwise the text of §3.9 (still loading, or could not be loaded)."""
    if _MODEL is not None:
        return None
    if _last_error is not None and not is_loading():
        return (f"The embedding model ({_model_name}) could not be loaded ({_last_error}), so search is "
                "keyword-only (BM25) and new entries are saved without vectors. The first start downloads "
                "the model (~1.1 GB) from Hugging Face: check the network or proxy, or pre-download it. "
                "The server retries automatically.")
    return loading_message()


def model_status():
    """The last load error, or None."""
    return _last_error


def _encode(texts):
    model = _MODEL
    vecs = model.encode(texts, normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False)
    return [list(map(float, v)) for v in vecs]


def embed_many(texts, kind="passage", no_wait=False):
    """Vectors for texts ("passage: " / "query: " prefix, NFKC), or None when the model is unavailable."""
    if not texts:
        return []
    if get_model(no_wait=no_wait) is None:
        return None
    prefix = "query: " if kind == "query" else "passage: "
    return _encode([prefix + normalize_text(t) for t in texts])


def embed(text, kind="passage", no_wait=False):
    vecs = embed_many([text], kind=kind, no_wait=no_wait)
    if vecs is None:
        return None
    return vecs[0]
