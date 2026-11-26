"""Text processing (NFKC, punctuation, CJK bigrams, purify, chunking, encoding safety), the ranking
math (spec §3.6), the result shaping helpers (spec §3.14), time bounds (spec §4.3.1) and the
embedding model (spec §3.2, §3.9).

The model libraries are imported only inside import_backend() / the model loader: the MCP thin
client and the hooks import this module and must not load sentence_transformers or torch.
"""
import math
import os
import re
import sys
import threading
import time
import unicodedata
from datetime import datetime, timezone

# ── text normalisation ───────────────────────────────────────────────────────

_CTRL_RE = re.compile(r'[\x00-\x1f\x7f]')
_PUNCT_STRIP_RE = re.compile(
    r'[,.<>{}\[\]"\':;!@#$%^&*()\-+=~|\\/?`'
    r'「」『』【】（）〈〉《》〔〕［］｛｝・、。，．！？；：―─…“”‘’]'
)
_WS_RE = re.compile(r"\s+")
_LONE_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?(?:```|\Z)")
CODE_OMITTED = "[code omitted]"

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

_SOFT_BREAKS = (
    "\n\n",                      # paragraph break  (highest priority)
    "\n",                        # line break
    ". ", "! ", "? ",            # ASCII sentence ends
    "。", "！", "？", "…",      # CJK / universal sentence ends
    " ", "\t",                   # word boundary    (lowest priority)
)
_MIN_CHUNK_CHARS = 20


def normalize_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def sanitize_surrogates(text):
    if isinstance(text, str):
        return _LONE_SURROGATE_RE.sub("", text)
    if isinstance(text, list):
        return [sanitize_surrogates(x) for x in text]
    if isinstance(text, dict):
        return {k: sanitize_surrogates(v) for k, v in text.items()}
    return text


def purify(text: str) -> str:
    """Every fenced code block (an unclosed fence runs to the end) becomes [code omitted]."""
    return _CODE_BLOCK_RE.sub(CODE_OMITTED, text)


_INVISIBLE_EXTRA = {0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800}


def has_visible_text(text: str) -> bool:
    """False when every character is whitespace, a control / format character (Cc / Cf), a separator
    (Zs / Zl / Zp), a variation selector, or one of the blank-looking Hangul fillers / braille blank."""
    for ch in text:
        if ch.isspace():
            continue
        cp = ord(ch)
        if 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF or cp in _INVISIBLE_EXTRA:
            continue
        if unicodedata.category(ch) in ("Cc", "Cf", "Zs", "Zl", "Zp"):
            continue
        return True
    return False


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    for lo, hi in _CJK_RANGES:
        if lo <= cp <= hi:
            return True
    return False


def fold_diacritics(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def strip_fts_punctuation(text: str) -> str:
    return _PUNCT_STRIP_RE.sub(" ", text)


def cjk_bigram_expand(text: str) -> str:
    """Runs of CJK characters become overlapping bigrams (a run of one character stays itself); other
    runs are kept, plus their diacritic-folded form when it differs (café → café cafe)."""
    out = []

    def flush_other(buf):
        for tok in "".join(buf).split():
            out.append(tok)
            folded = fold_diacritics(tok)
            if folded != tok and folded.strip():
                out.append(folded)

    other, run = [], []
    for ch in text:
        if _is_cjk(ch):
            if other:
                flush_other(other)
                other = []
            run.append(ch)
        else:
            if run:
                out.extend(_bigrams(run))
                run = []
            other.append(ch)
    if run:
        out.extend(_bigrams(run))
    if other:
        flush_other(other)
    return " ".join(out)


def _bigrams(run):
    if len(run) == 1:
        return ["".join(run)]
    return [run[i] + run[i + 1] for i in range(len(run) - 1)]


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


def bm25_terms(query: str) -> list:
    return drop_lone_cjk_tokens(prepare_query(query)).split()


def index_texts(body: str):
    """(content_fts, content_ngram) for the text a record is indexed by: the body before any role
    prefix, with every [code omitted] replaced by a space (spec §3.7)."""
    fts = body.replace(CODE_OMITTED, " ")
    return fts, prepare_query(fts)


# ── chunking (spec §3.11, §5 (7)) ────────────────────────────────────────────

def chunk_text(text: str, threshold: int, overlap: int) -> list:
    n = len(text)
    threshold = max(1, int(threshold))
    overlap = max(0, int(overlap))
    if n <= threshold:
        return [text]
    chunks = []
    start = 0
    prev_end = 0
    while True:
        if n - start <= threshold:
            piece = text[start:]
            if chunks and len(piece) < _MIN_CHUNK_CHARS:
                chunks[-1] += text[max(prev_end, start):]
            else:
                chunks.append(piece)
            break
        end = start + threshold
        region = max(start + 1, end - overlap)
        cut = -1
        for sep in _SOFT_BREAKS:
            idx = text.rfind(sep, region, end)
            if idx >= region:
                cut = idx + len(sep)
                break
        if cut > start:
            chunks.append(text[start:cut])
            prev_end = cut
            start = cut
        else:
            chunks.append(text[start:end])
            prev_end = end
            start = max(start + 1, end - overlap)
    return chunks


# ── ranking math (spec §3.6) ─────────────────────────────────────────────────

def cosine_from_distance(distance) -> float:
    try:
        d = float(distance)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(d):
        return 0.0
    return max(0.0, min(1.0, 1.0 - d))


def cosine_similarity(a, b) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return max(0.0, min(1.0, dot / (na * nb)))


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
    if bm25 is None:
        return 0.0
    v = abs(float(bm25))
    if v < threshold:
        return 0.0
    return v / max(1.0, abs(float(max_bm25 or 0.0)))


def b_local(importance, access_count, cfg) -> float:
    try:
        imp = float(importance)
        if math.isnan(imp):
            imp = 1.0
    except (TypeError, ValueError):
        imp = 1.0
    boost = 0.0
    if cfg.get("access_count_enabled"):
        try:
            ac = max(0.0, float(access_count or 0))
        except (TypeError, ValueError):
            ac = 0.0
        boost = min(float(cfg["access_count_max_boost"]), ac * float(cfg["access_count_weight"]))
    return max(0.5, min(2.0, imp + boost))


def fused(cos_sim, kw) -> float:
    return cos_sim * 0.7 + kw * 0.3


def final_score(cos_sim, kw, decay, blocal, bsession) -> float:
    return fused(cos_sim, kw) * decay * blocal * bsession


def _coverage_tokens(text: str) -> set:
    return set(prepare_query(text).lower().split())


def rerank_bonus(query: str, content: str, cfg) -> float:
    """(coverage × rerank_weight + phrase bonus) × length factor (spec §3.6, §5 (6))."""
    qt = _coverage_tokens(query)
    cov = 0.0
    if qt:
        cov = len(qt & _coverage_tokens(content)) / len(qt)
    q = normalize_text(query).strip().lower()
    norm = normalize_text(content).lower()
    phrase = float(cfg["rerank_phrase_weight"]) if q and q in norm else 0.0
    lf = len(norm) / 50.0 if len(norm) < 50 else 1.0
    return (cov * float(cfg["rerank_weight"]) + phrase) * lf


# ── time bounds (spec §4.3.1) ────────────────────────────────────────────────

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _local_tz():
    """None = the system's local time zone. Tests replace it with a fixed zone."""
    return None


def _bound_error(name, value) -> str:
    return (f"Error: {name} must be an ISO 8601 date or datetime, e.g. 2026-11-13 or "
            f"2026-11-13T09:00:00+09:00 (got '{value}')")


def _parse_one(name, value, end_of_day):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(_bound_error(name, value))
    s = value.strip()
    if not s:
        return None
    try:
        if _DATE_ONLY_RE.match(s):
            d = datetime.strptime(s, "%Y-%m-%d")
            dt = d.replace(hour=23, minute=59, second=59, microsecond=999999) if end_of_day else d
        else:
            t = s
            if t.endswith(("Z", "z")):
                t = t[:-1] + "+00:00"
            dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            tz = _local_tz()
            if tz is not None:
                dt = dt.replace(tzinfo=tz)
        return dt.timestamp()
    except (ValueError, OSError, OverflowError):
        raise ValueError(_bound_error(name, value))


def parse_time_bounds(since=None, until=None):
    """(since_epoch, until_epoch), each None when absent. Raises ValueError with the Error: text."""
    s = _parse_one("since", since, False)
    u = _parse_one("until", until, True)
    if s is not None and u is not None and s > u:
        raise ValueError("Error: since is later than until")
    return s, u


def fmt_saved(timestamp: str) -> str:
    """The saved UTC time in the server's local time with its offset: 2026-11-13 10:34 (UTC+0900)."""
    try:
        dt = datetime.fromisoformat(str(timestamp))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        tz = _local_tz()
        local = dt.astimezone(tz) if tz is not None else dt.astimezone()
        off = local.strftime("%z") or "+0000"
        return f"{local.strftime('%Y-%m-%d %H:%M')} (UTC{off})"
    except Exception:  # noqa: BLE001
        return str(timestamp)


# ── embedding model (spec §3.2, §3.9) ────────────────────────────────────────

MODEL_WAIT_SECONDS = 20
MODEL_RETRY_SECONDS = 60

_model = None
_model_lock = threading.Lock()
_last_error = None
_last_attempt = 0.0
_model_name = "intfloat/multilingual-e5-base"
_bg_thread = None
_backend_imported = False


def configure(cfg) -> None:
    global _model_name
    _model_name = cfg.get("embedding_model") or _model_name


def import_backend() -> None:
    """Import the model libraries synchronously (before the server starts serving), with fd 1 sent to
    stderr so that nothing a library prints reaches stdout (spec §3.9)."""
    global _backend_imported
    if _backend_imported:
        return
    saved = None
    try:
        try:
            sys.stdout.flush()
            saved = os.dup(1)
            os.dup2(2, 1)
        except (OSError, ValueError, AttributeError):
            saved = None
        import sentence_transformers  # noqa: F401
        _backend_imported = True
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[n3mc-lite] could not import the embedding libraries: {type(e).__name__}: {e}\n")
    finally:
        if saved is not None:
            try:
                sys.stdout.flush()
            except Exception:  # noqa: BLE001
                pass
            os.dup2(saved, 1)
            os.close(saved)


def _build_model(name):
    """Build the SentenceTransformer: cache only first, then with a download (spec §3.9 step 4)."""
    import_backend()
    from sentence_transformers import SentenceTransformer
    try:
        import huggingface_hub.constants as hc
    except Exception:  # noqa: BLE001
        hc = None
    old = getattr(hc, "HF_HUB_OFFLINE", None) if hc is not None else None
    try:
        if hc is not None:
            hc.HF_HUB_OFFLINE = True
        return SentenceTransformer(name, local_files_only=True)
    except Exception:  # noqa: BLE001
        pass
    finally:
        if hc is not None:
            hc.HF_HUB_OFFLINE = old
    return SentenceTransformer(name)


def _load_locked() -> None:
    """Load the model; the caller holds _model_lock."""
    global _model, _last_error, _last_attempt
    _last_attempt = time.monotonic()
    try:
        _model = _build_model(_model_name)
        _last_error = None
    except Exception as e:  # noqa: BLE001
        _last_error = f"{type(e).__name__}: {e}"
        sys.stderr.write(f"[n3mc-lite] the embedding model {_model_name} could not be loaded: {_last_error}\n")


def _retry_blocked() -> bool:
    return _last_error is not None and time.monotonic() - _last_attempt < MODEL_RETRY_SECONDS


def _background_load() -> None:
    with _model_lock:
        if _model is None and not _retry_blocked():
            _load_locked()


def start_background_load() -> None:
    """Start loading the model in a background thread (no-op when loaded or already loading)."""
    global _bg_thread
    if _model is not None:
        return
    if _bg_thread is not None and _bg_thread.is_alive():
        return
    _bg_thread = threading.Thread(target=_background_load, name="n3mc-model-load", daemon=True)
    _bg_thread.start()


def get_model(no_model_wait: bool = False):
    """The model, or None. Waits at most MODEL_WAIT_SECONDS for a load in progress; with
    no_model_wait it never waits and starts a background load when none is running."""
    if _model is not None:
        return _model
    if no_model_wait:
        if not _model_lock.locked() and not _retry_blocked():
            start_background_load()
        return None
    if _retry_blocked():
        return None
    if not _model_lock.acquire(timeout=MODEL_WAIT_SECONDS):
        return None
    try:
        if _model is None and not _retry_blocked():
            _load_locked()
        return _model
    finally:
        _model_lock.release()


def model_status():
    """('ready' | 'loading' | 'failed', last error or None)."""
    if _model is not None:
        return "ready", None
    if _last_error is not None and not _model_lock.locked():
        return "failed", _last_error
    return "loading", None


def unavailable_message() -> str:
    state, err = model_status()
    if state == "failed":
        return (f"The embedding model ({_model_name}) could not be loaded ({err}), so search is keyword-only "
                "(BM25) and new entries are saved without vectors. The first start downloads the model "
                "(~1.1 GB) from Hugging Face: check the network or proxy, or pre-download it. The server "
                "retries automatically.")
    return (f"The embedding model ({_model_name}) is still loading (the first start downloads ~1.1 GB), so "
            "this search used keywords only and new entries are saved without vectors. Retry in a minute.")


def embed(text: str, kind: str = "passage", no_model_wait: bool = False):
    """The L2-normalised embedding of "<kind>: " + NFKC(text) as a list of floats, or None when the model
    is not available. Exceptions from the model itself propagate."""
    model = get_model(no_model_wait=no_model_wait)
    if model is None:
        return None
    vec = model.encode([f"{kind}: " + normalize_text(text)], normalize_embeddings=True)[0]
    return [float(x) for x in vec]
