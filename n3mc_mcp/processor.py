"""Embedding, ranking math, CJK tokenization, chunking, purify, lexical reranker (spec §3.2, §3.6, §3.7, §3.11, §3.13).

`sentence_transformers` is imported only by `import_backend()` / `get_model()`,
never at module import, so the hooks and the MCP thin client stay light.
"""

import hashlib
import math
import re
import sys
import threading
import time
import unicodedata

# ---------------------------------------------------------------- text rules

_LONE_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def sanitize_surrogates(text):
    if isinstance(text, str):
        return _LONE_SURROGATE_RE.sub("", text)
    if isinstance(text, list):
        return [sanitize_surrogates(x) for x in text]
    if isinstance(text, dict):
        return {k: sanitize_surrogates(v) for k, v in text.items()}
    return text


def normalize_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


_INVISIBLE_EXTRA = {0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800}


def _invisible(ch: str) -> bool:
    cp = ord(ch)
    if ch.isspace():
        return True
    if unicodedata.category(ch) in ("Cc", "Cf", "Zs", "Zl", "Zp"):
        return True
    if 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF:
        return True
    return cp in _INVISIBLE_EXTRA


def has_visible_text(text: str) -> bool:
    return any(not _invisible(ch) for ch in text)


_FENCE_RE = re.compile(r"```[\s\S]*?(?:```|\Z)")


def purify(text: str) -> str:
    return _FENCE_RE.sub("[code omitted]", text)


def sha1_hex(text: str) -> str:
    return hashlib.sha1(normalize_text(text).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- CJK

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


def cjk_bigram_expand(text: str) -> str:
    """Split into whitespace tokens; every CJK run becomes overlapping bigrams (a 1-char run stays)."""
    out = []
    for word in text.split():
        run = []
        run_cjk = None
        for ch in word:
            c = _is_cjk(ch)
            if run and c != run_cjk:
                out.extend(_emit(run, run_cjk))
                run = []
            run.append(ch)
            run_cjk = c
        if run:
            out.extend(_emit(run, run_cjk))
    return " ".join(out)


def _emit(run, is_cjk):
    s = "".join(run)
    if not is_cjk or len(s) == 1:
        return [s]
    return [s[i:i + 2] for i in range(len(s) - 1)]


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
    text = strip_fts_punctuation(text)
    tokens = cjk_bigram_expand(text).split()
    # Tokens with no letter or digit (e.g. a lone '_') are not indexed by
    # RediSearch and leave an empty '@content:()' → syntax error.
    return ' '.join(t for t in tokens if any(ch.isalnum() for ch in t))


def drop_lone_cjk_tokens(q: str) -> str:
    # BM25 query only: drop single-character CJK tokens (particles such as
    # は / の / を left over by bigram expansion) when other tokens remain.
    tokens = q.split()
    kept = [t for t in tokens if not (len(t) == 1 and _is_cjk(t))] or tokens
    # 2026-11-17: hiragana-only tokens (して / ます / どう) too, when
    # another token remains.
    content = [t for t in kept if not all(
        0x3040 <= ord(c) <= 0x309F or c == 'ー' for c in t)]
    return ' '.join(content or kept)


def fold_diacritics(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def build_ngram(content: str) -> str:
    """The content_ngram side channel: punctuation-stripped, CJK bigrams, plus diacritic-folded words."""
    text = normalize_text(content)
    text = _CTRL_RE.sub(" ", text)
    text = strip_fts_punctuation(text)
    out = []
    for tok in cjk_bigram_expand(text).split():
        out.append(tok)
        if not any(_is_cjk(c) for c in tok):
            folded = fold_diacritics(tok)
            if folded != tok and folded:
                out.append(folded)
    return " ".join(out)


def tokenize_for_coverage(text: str) -> set:
    return {t.lower() for t in prepare_query(text).split()}


# ---------------------------------------------------------------- chunking

_SOFT_BREAKS = (
    "\n\n",                      # paragraph break  (highest priority)
    "\n",                        # line break
    ". ", "! ", "? ",            # ASCII sentence ends
    "。", "！", "？", "…",      # CJK / universal sentence ends
    " ", "\t",                   # word boundary    (lowest priority)
)
_MIN_CHUNK_CHARS = 20


def chunk_text(text: str, threshold: int = 400, overlap: int = 100) -> list:
    if len(text) <= threshold:
        return [text]
    if overlap >= threshold:
        overlap = threshold // 4
    n = len(text)
    spans = []
    start = 0
    while start < n:
        end = min(start + threshold, n)
        if end >= n:
            spans.append([start, n])
            break
        lo = max(start + 1, end - overlap)
        cut = None
        for sep in _SOFT_BREAKS:
            idx = text.rfind(sep, lo, end)
            if idx != -1:
                cut = idx + len(sep)
                break
        if cut is not None and cut > start:
            spans.append([start, cut])
            start = cut
        else:
            spans.append([start, end])
            start = max(start + 1, end - overlap)
    if len(spans) > 1 and spans[-1][1] - spans[-1][0] < _MIN_CHUNK_CHARS:
        last = spans.pop()
        spans[-1][1] = max(spans[-1][1], last[1])
    return [text[s:e] for s, e in spans]


# ---------------------------------------------------------------- ranking math

def cos_from_distance(dist) -> float:
    try:
        d = float(dist)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(d):
        return 0.0
    return max(0.0, min(1.0, 1.0 - d))


def cosine_sim(a, b) -> float:
    import numpy as np
    va = np.asarray(a, dtype="float32")
    vb = np.asarray(b, dtype="float32")
    na = float(np.linalg.norm(va))
    nb = float(np.linalg.norm(vb))
    if na == 0 or nb == 0:
        return 0.0
    return max(0.0, min(1.0, float(np.dot(va, vb)) / (na * nb)))


def keyword_relevance(bm25, max_bm25, threshold=0.1) -> float:
    try:
        b = abs(float(bm25))
    except (TypeError, ValueError):
        return 0.0
    if b < threshold:
        return 0.0
    return min(1.0, b / max(1.0, abs(float(max_bm25 or 0.0))))


def time_decay(ts_epoch, half_life_days, now=None) -> float:
    try:
        ts = float(ts_epoch)
        hl = float(half_life_days)
        if not math.isfinite(ts) or hl <= 0:
            return 1.0
    except (TypeError, ValueError):
        return 1.0
    now = time.time() if now is None else now
    days = max(0.0, (now - ts) / 86400.0)
    return 2.0 ** (-days / hl)


def b_local(importance, access_count, cfg) -> float:
    try:
        imp = float(importance)
        if not math.isfinite(imp):
            imp = 1.0
    except (TypeError, ValueError):
        imp = 1.0
    boost = 0.0
    if cfg.get("access_count_enabled"):
        try:
            cnt = float(access_count or 0)
        except (TypeError, ValueError):
            cnt = 0.0
        boost = min(float(cfg.get("access_count_max_boost", 0.5)), cnt * float(cfg.get("access_count_weight", 0.02)))
    return max(0.5, min(2.0, imp + boost))


def b_session(row_session, effective_session, cfg) -> float:
    if effective_session and row_session == effective_session:
        return float(cfg.get("b_session_match", 1.0))
    return float(cfg.get("b_session_mismatch", 0.6))


def base_score(cos, kw) -> float:
    return cos * 0.7 + kw * 0.3


def rerank_bonus(query: str, content: str, cfg) -> float:
    qtok = tokenize_for_coverage(query)
    norm = normalize_text(content).lower()
    coverage = 0.0
    if qtok:
        ctok = tokenize_for_coverage(content)
        coverage = len(qtok & ctok) / len(qtok)
    phrase = 0.0
    q = normalize_text(query).strip().lower()
    if q and q in norm:
        phrase = float(cfg.get("rerank_phrase_weight", 0.2))
    length_factor = len(norm) / 50.0 if len(norm) < 50 else 1.0
    return (coverage * float(cfg.get("rerank_weight", 0.3)) + phrase) * length_factor


# ---------------------------------------------------------------- embedding model

MODEL_RETRY_SECONDS = 60
MODEL_WAIT_SECONDS = 20

_model = None
_model_name = None
_model_lock = threading.Lock()
_last_error = None
_last_attempt = 0.0
_backend_imported = False
_no_wait = threading.local()  # set per request thread by the server for calls from the hooks (L8, M3)


class no_model_wait:
    """Within this block, this thread never waits for the model and never loads it itself.

    The server uses it for the saves and searches the hooks send (``"no_model_wait": true``): while
    the model loads, they go on without vectors at once (search uses keywords only, /repair adds the
    vectors later). When the model is not loaded and nothing is loading it, a background thread
    starts the load. MCP tools and CLI commands keep the normal wait (MODEL_WAIT_SECONDS)."""

    def __init__(self, on=True):
        self.on = bool(on)

    def __enter__(self):
        self.prev = getattr(_no_wait, "on", False)
        _no_wait.on = self.on
        return self

    def __exit__(self, *exc):
        _no_wait.on = self.prev
        return False


def import_backend() -> None:
    """Import the heavy libraries synchronously (before the server starts listening)."""
    global _backend_imported
    if _backend_imported:
        return
    import os
    saved = None
    try:
        saved = os.dup(1)
        os.dup2(2, 1)
    except OSError:
        saved = None
    try:
        import sentence_transformers  # noqa: F401
        _backend_imported = True
    except Exception as e:  # noqa: BLE001
        print(f"N3MC Lite: could not import sentence_transformers: {e}", file=sys.stderr, flush=True)
    finally:
        if saved is not None:
            try:
                os.dup2(saved, 1)
                os.close(saved)
            except OSError:
                pass


def _build(name):
    from sentence_transformers import SentenceTransformer
    try:
        import huggingface_hub.constants as hc
        old = getattr(hc, "HF_HUB_OFFLINE", False)
        hc.HF_HUB_OFFLINE = True
    except Exception:  # noqa: BLE001
        hc, old = None, None
    model = None
    try:
        model = SentenceTransformer(name, local_files_only=True)
    except Exception:  # noqa: BLE001
        model = None
    finally:
        if hc is not None:
            hc.HF_HUB_OFFLINE = old
    if model is None:
        model = SentenceTransformer(name)
    return model


def loading_message(model_name: str) -> str:
    return (f"The embedding model ({model_name}) is still loading (the first start downloads ~440 MB), "
            f"so this search used keywords only and new entries are saved without vectors. Retry in a minute.")


def unavailable_message(model_name: str, error: str) -> str:
    return (f"The embedding model ({model_name}) could not be loaded ({error}), so search is keyword-only "
            f"(BM25) and new entries are saved without vectors. The first start downloads the model "
            f"(~440 MB) from Hugging Face: check the network or proxy, or pre-download it. "
            f"The server retries automatically.")


def get_model(model_name: str, wait: float = None):
    """Return (model, None) or (None, message). Double-checked locking; waits at most MODEL_WAIT_SECONDS."""
    global _model, _model_name, _last_error, _last_attempt
    if _model is not None and _model_name == model_name:
        return _model, None
    if getattr(_no_wait, "on", False):
        if not _model_lock.locked() and (_last_error is None
                                         or time.time() - _last_attempt >= MODEL_RETRY_SECONDS):
            threading.Thread(target=lambda: get_model(model_name, wait=1), daemon=True).start()
        return None, model_status(model_name) or loading_message(model_name)
    wait = MODEL_WAIT_SECONDS if wait is None else wait
    if not _model_lock.acquire(timeout=wait):
        return None, loading_message(model_name)
    try:
        if _model is not None and _model_name == model_name:
            return _model, None
        if _last_error is not None and time.time() - _last_attempt < MODEL_RETRY_SECONDS:
            return None, unavailable_message(model_name, _last_error)
        _last_attempt = time.time()
        try:
            m = _build(model_name)
        except Exception as e:  # noqa: BLE001
            _last_error = f"{type(e).__name__}: {e}"
            print(f"N3MC Lite: embedding model load failed: {_last_error}", file=sys.stderr, flush=True)
            return None, unavailable_message(model_name, _last_error)
        _model, _model_name, _last_error = m, model_name, None
        return _model, None
    finally:
        _model_lock.release()


def model_status(model_name: str):
    """None when the model is ready, else the notice text."""
    if _model is not None and _model_name == model_name:
        return None
    if _model_lock.locked():
        return loading_message(model_name)
    if _last_error is not None:
        return unavailable_message(model_name, _last_error)
    return loading_message(model_name)


def _encode(texts, model_name):
    """Encode already-prefixed texts. Returns (list of vectors, None) or (None, message)."""
    model, msg = get_model(model_name)
    if model is None:
        return None, msg
    vecs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return [list(map(float, v)) for v in vecs], None


def embed_passages(texts, cfg):
    return _encode(["passage: " + normalize_text(t) for t in texts], cfg["embedding_model"])


def embed_query(text, cfg):
    vecs, msg = _encode(["query: " + normalize_text(text)], cfg["embedding_model"])
    return (vecs[0] if vecs else None), msg


def to_bytes(vec) -> bytes:
    import numpy as np
    return np.asarray(vec, dtype="<f4").tobytes()


def from_bytes(b):
    import numpy as np
    if not b:
        return None
    return np.frombuffer(b, dtype="<f4").astype("float32")
