"""Text processing, ranking math, chunking and the embedding model (spec §3.2, §3.6, §3.7, §3.9,
§3.11, §3.13, §5 (5)-(7)).

The model code is lazy: importing this module never imports sentence_transformers or torch (the
MCP thin client imports database.py, which imports this module, §4.6)."""
import contextlib
import datetime
import hashlib
import math
import os
import re
import struct
import sys
import threading
import time
import unicodedata

# ── encoding safety (§3.13) ───────────────────────────────────────────────────

_LONE_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def sanitize_surrogates(text):
    if isinstance(text, str):
        return _LONE_SURROGATE_RE.sub("", text)
    if isinstance(text, list):
        return [sanitize_surrogates(x) for x in text]
    if isinstance(text, dict):
        return {k: sanitize_surrogates(v) for k, v in text.items()}
    return text


_INVISIBLE_EXTRA = {0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800}


def _invisible(ch: str) -> bool:
    cp = ord(ch)
    if ch.isspace():
        return True
    cat = unicodedata.category(ch)
    if cat in ("Cc", "Cf", "Zs", "Zl", "Zp"):
        return True
    if 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF:
        return True
    return cp in _INVISIBLE_EXTRA


def has_visible_text(text: str) -> bool:
    return any(not _invisible(ch) for ch in text)


_CODE_RE = re.compile(r"```[\s\S]*?(?:```|\Z)")


def purify(text: str) -> str:
    return _CODE_RE.sub("[code omitted]", text)


def has_fence(text: str) -> bool:
    return "```" in text


def normalize_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def sha1_hex(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def content_hash(text: str) -> str:
    """The hash of the dedup guards: SHA-1 of the NFKC form (§3.7, §3.8)."""
    return sha1_hex(normalize_text(text))


# ── tokenization (§3.7, §5 (5)) ───────────────────────────────────────────────

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
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def fold_diacritics(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def cjk_bigram_expand(text: str) -> str:
    """Overlapping bigrams for runs of _is_cjk characters (a 1-character run stays as it is); other
    runs are kept, and their diacritic-folded form is added when it differs."""
    out = []
    i = 0
    n = len(text)
    while i < n:
        if _is_cjk(text[i]):
            j = i
            while j < n and _is_cjk(text[j]):
                j += 1
            run = text[i:j]
            if len(run) == 1:
                out.append(" " + run + " ")
            else:
                out.append(" " + " ".join(run[k:k + 2] for k in range(len(run) - 1)) + " ")
            i = j
        else:
            j = i
            while j < n and not _is_cjk(text[j]):
                j += 1
            run = text[i:j]
            out.append(run)
            folded = fold_diacritics(run)
            if folded != run:
                out.append(" " + folded + " ")
            i = j
    return re.sub(r"\s+", " ", "".join(out)).strip()


_CTRL_RE = re.compile(r'[\x00-\x1f\x7f]')
_PUNCT_STRIP_RE = re.compile(
    r'[,.<>{}\[\]"\':;!@#$%^&*()\-+=~|\\/?`'
    r'「」『』【】（）〈〉《》〔〕［］｛｝・、。，．！？；：―─…“”‘’]'
)


def strip_fts_punctuation(text: str) -> str:
    return _PUNCT_STRIP_RE.sub(" ", text)


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


def fts_text(body: str) -> str:
    """content_fts: the body without the role prefix, every [code omitted] replaced by a space."""
    return body.replace("[code omitted]", " ")


def ngram_text(body: str) -> str:
    """content_ngram: fts_text, NFKC, control characters and punctuation removed, bigram-expanded."""
    t = normalize_text(fts_text(body))
    t = _CTRL_RE.sub(" ", t)
    t = strip_fts_punctuation(t)
    return cjk_bigram_expand(t)


def bm25_query(query: str):
    """The OR query of §3.7, or None when no term remains."""
    terms = drop_lone_cjk_tokens(prepare_query(query)).split()
    if not terms:
        return None
    q = "|".join(terms)
    return f"(@content_fts:({q}) | @content_ngram:({q}))"


def token_set(text: str) -> set:
    return set(prepare_query(text).lower().split())


# ── chunking (§3.11, §5 (7)) ──────────────────────────────────────────────────

_SOFT_BREAKS = (
    "\n\n",                      # paragraph break  (highest priority)
    "\n",                        # line break
    ". ", "! ", "? ",            # ASCII sentence ends
    "。", "！", "？", "…",      # CJK / universal sentence ends
    " ", "\t",                   # word boundary    (lowest priority)
)
_MIN_CHUNK_CHARS = 20


def chunk_text(text: str, threshold: int, overlap: int) -> list:
    if len(text) <= threshold:
        return [text]
    spans = []  # (start, end) of each chunk
    start = 0
    n = len(text)
    while start < n:
        end = min(start + threshold, n)
        if end >= n:
            if spans and n - start < _MIN_CHUNK_CHARS:
                # Too short for a chunk of its own: every character still goes into a chunk,
                # so the previous chunk is extended to the end of the text.
                spans[-1] = (spans[-1][0], n)
            else:
                spans.append((start, n))
            break
        lo = max(start + 1, end - overlap)
        cut = -1
        for brk in _SOFT_BREAKS:
            pos = text.rfind(brk, lo, end)
            if pos != -1:
                cut = pos + len(brk)
                break
        if cut != -1:
            spans.append((start, cut))
            next_start = cut
        else:
            spans.append((start, end))
            next_start = end - overlap
        start = max(start + 1, next_start)
    return [text[a:b] for a, b in spans]


# ── ranking (§3.6) ────────────────────────────────────────────────────────────

def cos_sim_from_distance(distance) -> float:
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


def time_decay(epoch, half_life_days: float, now: float = None) -> float:
    try:
        ts = float(epoch)
    except (TypeError, ValueError):
        return 1.0
    if math.isnan(ts) or half_life_days <= 0:
        return 1.0
    now = time.time() if now is None else now
    days = max(0.0, (now - ts) / 86400.0)
    return 2.0 ** (-days / half_life_days)


def keyword_relevance(score, max_score, threshold: float) -> float:
    try:
        s = abs(float(score))
    except (TypeError, ValueError):
        return 0.0
    if s < threshold:
        return 0.0
    try:
        m = abs(float(max_score))
    except (TypeError, ValueError):
        m = 0.0
    return s / max(1.0, m)


def b_local(importance, access_count, cfg: dict) -> float:
    try:
        imp = float(importance)
    except (TypeError, ValueError):
        imp = 1.0
    if math.isnan(imp):
        imp = 1.0
    boost = 0.0
    if cfg.get("access_count_enabled"):
        try:
            ac = float(access_count)
        except (TypeError, ValueError):
            ac = 0.0
        boost = min(float(cfg.get("access_count_max_boost", 0.5)), ac * float(cfg.get("access_count_weight", 0.02)))
    return max(0.5, min(2.0, imp + boost))


def base_score(cos: float, kw: float) -> float:
    return cos * 0.7 + kw * 0.3


def lexical_bonus(query: str, content: str, cfg: dict) -> float:
    if not cfg.get("lexical_rerank_enabled", True):
        return 0.0
    q_tokens = token_set(query)
    norm_content = normalize_text(content).lower()
    coverage = 0.0
    if q_tokens:
        c_tokens = token_set(content)
        coverage = len(q_tokens & c_tokens) / len(q_tokens)
    phrase = 0.0
    q = normalize_text(query).strip().lower()
    if q and q in norm_content:
        phrase = float(cfg.get("rerank_phrase_weight", 0.2))
    lf = len(norm_content) / 50.0 if len(norm_content) < 50 else 1.0
    return (coverage * float(cfg.get("rerank_weight", 0.3)) + phrase) * lf


# ── vectors ───────────────────────────────────────────────────────────────────

def pack_vector(vec) -> bytes:
    return struct.pack("<%df" % len(vec), *[float(x) for x in vec])


def unpack_vector(data: bytes):
    if not data or len(data) % 4:
        return None
    return list(struct.unpack("<%df" % (len(data) // 4), data))


# ── the embedding model (§3.2, §3.9) ──────────────────────────────────────────

MODEL_WAIT_SECONDS = 20
MODEL_RETRY_SECONDS = 60

_model = None
_lock = threading.Lock()
_loading = False
_last_error = None
_last_attempt = 0.0
_backend_imported = False
_model_name = None
# Tests set a deterministic stand-in: fn(text) -> list[float] (already prefixed text).
_FAKE_EMBED = None


def import_backend() -> None:
    """Import the libraries synchronously (before uvicorn / stdio starts), fd 1 sent to stderr."""
    global _backend_imported
    if _backend_imported or _FAKE_EMBED is not None:
        return
    saved = None
    try:
        try:
            sys.stdout.flush()
            saved = os.dup(1)
            os.dup2(2, 1)
        except OSError:
            saved = None
        import sentence_transformers  # noqa: F401
        _backend_imported = True
    except Exception as e:  # noqa: BLE001
        print(f"n3mc-lite: could not import sentence_transformers ({type(e).__name__}: {e})", file=sys.stderr)
    finally:
        if saved is not None:
            try:
                sys.stdout.flush()
                os.dup2(saved, 1)
                os.close(saved)
            except OSError:
                pass


@contextlib.contextmanager
def _hf_offline():
    try:
        import huggingface_hub.constants as hc
    except Exception:  # noqa: BLE001
        yield
        return
    old = getattr(hc, "HF_HUB_OFFLINE", False)
    hc.HF_HUB_OFFLINE = True
    try:
        yield
    finally:
        hc.HF_HUB_OFFLINE = old


def _build(name: str):
    from sentence_transformers import SentenceTransformer
    try:
        with _hf_offline():
            return SentenceTransformer(name, local_files_only=True)
    except Exception:  # noqa: BLE001
        return SentenceTransformer(name)


def get_model(cfg: dict = None, wait: bool = True):
    """The model, or None. wait=False (the hooks' no_model_wait) never takes the lock (§3.9)."""
    global _model, _loading, _last_error, _last_attempt, _model_name
    if _FAKE_EMBED is not None:
        return "fake"
    if _model is not None:
        return _model
    name = (cfg or {}).get("embedding_model", "intfloat/multilingual-e5-base")
    if not wait:
        if not _loading and not _lock.locked() and (time.time() - _last_attempt >= MODEL_RETRY_SECONDS or not _last_error):
            start_background_load(cfg)
        return None
    if not _lock.acquire(timeout=MODEL_WAIT_SECONDS):
        return None
    try:
        if _model is not None:
            return _model
        if _last_error and time.time() - _last_attempt < MODEL_RETRY_SECONDS:
            return None
        _loading = True
        _last_attempt = time.time()
        try:
            import_backend()
            _model = _build(name)
            _model_name = name
            _last_error = None
            return _model
        except Exception as e:  # noqa: BLE001
            _last_error = f"{type(e).__name__}: {e}"
            print(f"n3mc-lite: the embedding model could not be loaded ({_last_error})", file=sys.stderr)
            return None
        finally:
            _loading = False
    finally:
        _lock.release()


def start_background_load(cfg: dict = None) -> None:
    global _loading
    if _FAKE_EMBED is not None or _model is not None:
        return
    _loading = True

    def _run():
        global _loading
        try:
            get_model(cfg, wait=True)
        finally:
            _loading = False

    threading.Thread(target=_run, name="n3mc-model-load", daemon=True).start()


def model_ready() -> bool:
    return _FAKE_EMBED is not None or _model is not None


def model_status():
    """The last load error, or None."""
    return _last_error


def unavailable_message(cfg: dict) -> str:
    name = (cfg or {}).get("embedding_model", "intfloat/multilingual-e5-base")
    if _last_error and not _loading and not _lock.locked():
        return (f"The embedding model ({name}) could not be loaded ({_last_error}), so search is "
                "keyword-only (BM25) and new entries are saved without vectors. The first start "
                "downloads the model (~1.1 GB) from Hugging Face: check the network or proxy, or "
                "pre-download it. The server retries automatically.")
    return (f"The embedding model ({name}) is still loading (the first start downloads ~1.1 GB), so "
            "this search used keywords only and new entries are saved without vectors. Retry in a minute.")


def embed(text: str, kind: str = "passage", cfg: dict = None, wait: bool = True):
    """L2-normalized vector of `kind: text` (NFKC), or None when the model is not available."""
    prefixed = f"{kind}: " + normalize_text(text)
    if _FAKE_EMBED is not None:
        return list(_FAKE_EMBED(prefixed))
    model = get_model(cfg, wait=wait)
    if model is None:
        return None
    vec = model.encode(prefixed, normalize_embeddings=True)
    return [float(x) for x in vec]


def embed_many(texts, kind: str = "passage", cfg: dict = None, wait: bool = True):
    if _FAKE_EMBED is not None:
        return [embed(t, kind, cfg, wait) for t in texts]
    model = get_model(cfg, wait=wait)
    if model is None:
        return None
    prefixed = [f"{kind}: " + normalize_text(t) for t in texts]
    vecs = model.encode(prefixed, normalize_embeddings=True, batch_size=16)
    return [[float(x) for x in v] for v in vecs]


# ── time (§3.14, §4.3.1) ──────────────────────────────────────────────────────

def fmt_saved(epoch) -> str:
    try:
        dt = datetime.datetime.fromtimestamp(float(epoch), datetime.timezone.utc).astimezone()
    except (TypeError, ValueError, OSError, OverflowError):
        return "?"
    return dt.strftime("%Y-%m-%d %H:%M (UTC%z)")


_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class TimeBoundError(ValueError):
    pass


def _bound_error(name: str, value) -> TimeBoundError:
    return TimeBoundError(f"Error: {name} must be an ISO 8601 date or datetime, e.g. 2026-11-13 or "
                          f"2026-11-13T09:00:00+09:00 (got '{value}')")


def parse_time_bound(value, name: str, end: bool):
    """Epoch seconds, or None for an empty value. Raises TimeBoundError (§4.3.1)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise _bound_error(name, value)
    v = value.strip()
    if not v:
        return None
    try:
        if _DATE_ONLY.match(v):
            d = datetime.date.fromisoformat(v)
            t = datetime.time(23, 59, 59, 999999) if end else datetime.time(0, 0, 0)
            dt = datetime.datetime.combine(d, t)
        else:
            s = v[:-1] + "+00:00" if v.endswith(("Z", "z")) else v
            if not re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", s):
                raise ValueError(v)
            dt = datetime.datetime.fromisoformat(s.replace(" ", "T", 1))
        if dt.tzinfo is None:
            dt = dt.astimezone()  # the server's local time zone
        return dt.timestamp()
    except (ValueError, OSError, OverflowError):
        raise _bound_error(name, value)


def parse_time_bounds(since, until):
    s = parse_time_bound(since, "since", False)
    u = parse_time_bound(until, "until", True)
    if s is not None and u is not None and s > u:
        raise TimeBoundError("Error: since is later than until")
    return s, u
