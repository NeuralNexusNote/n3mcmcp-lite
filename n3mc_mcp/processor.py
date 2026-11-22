"""Embedding, ranking, CJK tokenization, reranker, result shaping (spec sections 3.2, 3.6, 3.7, 3.9, 3.11, 3.13, 3.14)."""

import hashlib
import math
import re
import struct
import sys
import threading
import time
import unicodedata
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Text helpers (3.13)
# ---------------------------------------------------------------------------

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


_INVISIBLE_LETTERS = {0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800}


def has_visible_text(text: str) -> bool:
    """True if the text has at least one visible character (3.13)."""
    for ch in text:
        cp = ord(ch)
        if ch.isspace():
            continue
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf", "Zs", "Zl", "Zp"):
            continue
        if 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF:
            continue
        if cp in _INVISIBLE_LETTERS:
            continue
        return True
    return False


_CODE_RE = re.compile(r"```[\s\S]*?(?:```|\Z)")


def purify(text: str) -> str:
    """Replace every fenced code block with [code omitted] (an unclosed fence runs to the end)."""
    return _CODE_RE.sub("[code omitted]", text)


def sha1_hex(text: str) -> str:
    return hashlib.sha1(normalize_text(text).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# CJK tokenization (3.7)
# ---------------------------------------------------------------------------

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


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    for lo, hi in _CJK_RANGES:
        if lo <= cp <= hi:
            return True
    return False


def fold_diacritics(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def cjk_bigram_expand(text: str) -> str:
    """Expand each CJK run into overlapping bigrams (single characters stay); other runs keep their
    text and, when folding diacritics changes them, also their folded form."""
    out = []
    run = []
    run_cjk = None

    def flush():
        if not run:
            return
        seg = "".join(run)
        if run_cjk:
            if len(seg) == 1:
                out.append(seg)
            else:
                out.extend(seg[i:i + 2] for i in range(len(seg) - 1))
        else:
            out.append(seg)
            folded = fold_diacritics(seg)
            if folded != seg:
                out.append(folded)

    for ch in text:
        if ch.isspace():
            flush()
            run, run_cjk = [], None
            continue
        c = _is_cjk(ch)
        if run_cjk is None or c == run_cjk:
            run.append(ch)
            run_cjk = c
        else:
            flush()
            run, run_cjk = [ch], c
    flush()
    return " ".join(out)


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
    # RediSearch and leave an empty '@content:()' -> syntax error.
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


def ngram_text(content: str) -> str:
    """The content_ngram side channel: NFKC, control chars and punctuation removed, CJK bigrams."""
    return prepare_query(content)


def lex_tokens(text: str) -> set:
    """Tokens for the lexical rerank: both query and content go through prepare_query (space-separated
    words plus CJK bigrams), lower case."""
    return {t.lower() for t in prepare_query(text).split()}


# ---------------------------------------------------------------------------
# Chunking (3.11)
# ---------------------------------------------------------------------------

_MIN_CHUNK_CHARS = 20


def chunk_text(text: str, threshold: int, overlap: int):
    """Sliding windows of `threshold` chars; every char lands in at least one chunk; always progresses."""
    if len(text) <= threshold:
        return [text]
    n = len(text)
    chunks = []
    start = 0
    while start < n:
        end = min(start + threshold, n)
        if end >= n:
            chunks.append(text[start:])
            break
        lo = max(start + 1, end - overlap)
        cut = None
        for sep in _SOFT_BREAKS:
            idx = text.rfind(sep, lo, end)
            if idx != -1:
                cut = idx + len(sep)
                break
        if cut is not None:
            chunks.append(text[start:cut])
            start = max(start + 1, cut)
        else:
            chunks.append(text[start:end])
            start = max(start + 1, end - overlap)
    # A trailing piece shorter than _MIN_CHUNK_CHARS is appended to the previous chunk.
    if len(chunks) > 1 and len(chunks[-1]) < _MIN_CHUNK_CHARS:
        tail = chunks.pop()
        chunks[-1] = chunks[-1] + tail
    return chunks


# ---------------------------------------------------------------------------
# Vectors
# ---------------------------------------------------------------------------

def pack_vector(vec) -> bytes:
    return struct.pack("<%df" % len(vec), *[float(x) for x in vec])


def unpack_vector(data: bytes):
    n = len(data) // 4
    return list(struct.unpack("<%df" % n, data[:n * 4]))


def cosine_sim_vectors(a, b) -> float:
    da = math.sqrt(sum(x * x for x in a))
    db = math.sqrt(sum(x * x for x in b))
    if da == 0 or db == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (da * db)


# ---------------------------------------------------------------------------
# Ranking math (3.6)
# ---------------------------------------------------------------------------

def cos_sim_from_distance(dist) -> float:
    try:
        return max(0.0, min(1.0, 1.0 - float(dist)))
    except (TypeError, ValueError):
        return 0.0


def time_decay(timestamp_epoch, now: float, half_life_days: float) -> float:
    try:
        ts = float(timestamp_epoch)
    except (TypeError, ValueError):
        return 1.0
    days = max(0.0, (now - ts) / 86400.0)
    return 2.0 ** (-days / half_life_days)


def keyword_relevance(bm25, max_bm25, threshold: float) -> float:
    if bm25 is None:
        return 0.0
    b = abs(float(bm25))
    if b < threshold:
        return 0.0
    return b / max(1.0, abs(float(max_bm25 or 0.0)))


def b_local(importance, access_count, cfg) -> float:
    try:
        imp = float(importance)
    except (TypeError, ValueError):
        imp = 1.0
    boost = 0.0
    if cfg.get("access_count_enabled"):
        try:
            ac = float(access_count or 0)
        except (TypeError, ValueError):
            ac = 0.0
        boost = min(cfg["access_count_max_boost"], ac * cfg["access_count_weight"])
    return max(0.5, min(2.0, imp + boost))


def b_session(row_session, effective_session, cfg) -> float:
    if effective_session and row_session == effective_session:
        return cfg["b_session_match"]
    return cfg["b_session_mismatch"]


def final_score(cos, kw, decay, bl, bs) -> float:
    return (cos * 0.7 + kw * 0.3) * decay * bl * bs


def rerank_bonus(query: str, content: str, cfg) -> float:
    """(coverage * weight + phrase_bonus) * length_factor (3.6, section 5 (6))."""
    q = query.strip()
    if not q:
        return 0.0
    qt = lex_tokens(q)
    ct = lex_tokens(content)
    coverage = (len(qt & ct) / len(qt)) if qt else 0.0
    norm = normalize_text(content).lower()
    phrase = cfg["rerank_phrase_weight"] if normalize_text(q).lower() in norm else 0.0
    length_factor = len(norm) / 50.0 if len(norm) < 50 else 1.0
    return (coverage * cfg["rerank_weight"] + phrase) * length_factor


# ---------------------------------------------------------------------------
# Result shaping (3.14)
# ---------------------------------------------------------------------------

def _row_sort_key(r):
    try:
        ep = float(r.get("timestamp_epoch") or 0)
    except (TypeError, ValueError):
        ep = 0.0
    return (ep, str(r.get("id", "")))


def _group_versions(rows, cfg, get_repr):
    """Step 4: move the newest version of the same content to the front of its group's positions."""
    n = len(rows)
    reps = [None] * n
    for i, r in enumerate(rows):
        try:
            rep = get_repr(r)
        except Exception:  # noqa: BLE001
            rep = None
        if rep and rep[0] is not None and rep[1] is not None and len(rep[1]) >= cfg["version_min_chars"]:
            reps[i] = rep
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        if reps[i] is None:
            continue
        for j in range(i + 1, n):
            if reps[j] is None:
                continue
            if cosine_sim_vectors(reps[i][0], reps[j][0]) >= cfg["version_sim_threshold"]:
                parent[find(i)] = find(j)
    groups = {}
    for i in range(n):
        if reps[i] is not None:
            groups.setdefault(find(i), []).append(i)
    gno = 0
    out = list(rows)
    for members in groups.values():
        if len(members) < 2:
            continue
        gno += 1
        members.sort()
        by_new = sorted((rows[i] for i in members), key=_row_sort_key, reverse=True)
        for pos, row in zip(members, by_new):
            new = dict(row)
            new["score"] = rows[pos]["score"]
            new["filter_score"] = rows[pos]["filter_score"]
            new["_version_group"] = gno
            out[pos] = new
    return out


def build_results(rows, query, cfg, effective_session, limit, resolve_parents, get_repr, now=None):
    """Turn merged chunk-level candidate rows into (ordinary rows, weak rows).

    rows: dicts with id (bare), content, timestamp, timestamp_epoch, session_id, turn_id, importance,
          access_count, parent_id, cos (real cosine similarity or None), bm25 (raw BM25 or None).
    resolve_parents(ids) -> {parent_id: {"content", "chunk_count", "chunk_ids", "turn_id"}}.
    get_repr(row) -> (vector, text) or None.
    """
    now = time.time() if now is None else now
    if not rows:
        return [], []
    bm_values = [abs(float(r["bm25"])) for r in rows if r.get("bm25") is not None]
    max_bm = max(bm_values) if bm_values else 0.0
    vmin = cfg["vector_min_cos"]

    cands = []
    for r in rows:
        kw = keyword_relevance(r.get("bm25"), max_bm, cfg["bm25_min_threshold"])
        cos_real = r.get("cos") or 0.0
        bl = b_local(r.get("importance"), r.get("access_count"), cfg)
        decay = time_decay(r.get("timestamp_epoch"), now, cfg["half_life_days"])
        bs = b_session(r.get("session_id", ""), effective_session, cfg)
        c = dict(r)
        c["_chunk_id"] = r["id"]
        c["_cos_real"] = cos_real
        c["_kw"] = kw
        c["_bl"], c["_decay"], c["_bs"] = bl, decay, bs
        c["_in_main"] = (r.get("cos") is not None and cos_real >= vmin) or r.get("bm25") is not None
        cands.append(c)

    # Parent resolution happens before the lexical rerank (3.11).
    pids = sorted({c["parent_id"] for c in cands if c.get("parent_id")})
    parents = resolve_parents(pids) if pids else {}

    def scores(c, cos):
        base = cos * 0.7 + c["_kw"] * 0.3
        return base * c["_bl"] * c["_decay"] * c["_bs"], base * c["_bl"]

    def resolve(c, cos):
        score, fscore = scores(c, cos)
        d = dict(c)
        d["score"], d["filter_score"] = score, fscore
        d["_doc"] = False
        pid = c.get("parent_id")
        if pid and pid in parents:
            p = parents[pid]
            d["content"] = p["content"]
            d["id"] = pid
            d["_is_parent"] = True
            d["_doc"] = True
            d["chunk_count"] = p.get("chunk_count", 0)
            d["chunk_ids"] = p.get("chunk_ids", [])
            d["turn_id"] = p.get("turn_id", d.get("turn_id", ""))
        else:
            d["_is_parent"] = False
        return d

    def dedupe(items, key):
        best = {}
        for d in items:
            k = d["id"]
            if k not in best or d[key] > best[k][key]:
                best[k] = d
        return list(best.values())

    rerank_on = cfg["lexical_rerank_enabled"]

    def apply_rerank(d):
        if rerank_on:
            bonus = rerank_bonus(query, d["content"], cfg)
            d["score"] += bonus
            d["filter_score"] += bonus
        return d

    main = dedupe([resolve(c, c["_cos_real"] if c["_cos_real"] >= vmin else 0.0)
                   for c in cands if c["_in_main"]], "score")
    main = [apply_rerank(d) for d in main]
    if cfg["min_score"] > 0:
        main = [d for d in main if d["filter_score"] >= cfg["min_score"]]
    main.sort(key=lambda d: (d["score"], _row_sort_key(d)), reverse=True)

    if cfg["version_order_enabled"] and len(main) > 1:
        main = _group_versions(main, cfg, get_repr)

    ratio = cfg["relative_min_score_ratio"]
    if ratio > 0 and main:
        top = max(d["filter_score"] for d in main)
        main = [d for d in main if d.get("_version_group") or d["filter_score"] >= top * ratio]

    # Weak candidates (step 6).
    weak = []
    target = min(cfg["min_results"], limit)
    if target > 0 and len(main) < target and query.strip():
        taken = {d["id"] for d in main}
        pool = dedupe([resolve(c, c["_cos_real"]) for c in cands], "filter_score")
        pool = [apply_rerank(d) for d in pool]
        pool = [d for d in pool if d["id"] not in taken and d["filter_score"] >= 1e-6]
        pool.sort(key=lambda d: (d["filter_score"], _row_sort_key(d)), reverse=True)
        for d in pool:
            if len(main) + len(weak) >= target:
                break
            d["_weak"] = True
            weak.append(d)
    return main, weak


# ---------------------------------------------------------------------------
# Embedding model (3.2, 3.9)
# ---------------------------------------------------------------------------

MODEL_WAIT_SECONDS = 20
MODEL_RETRY_SECONDS = 60

_model = None
_model_name = None
_model_lock = threading.Lock()
_last_error = None
_last_attempt = 0.0
_bg_thread = None
_last_log = 0.0
_ST = None


def import_backend():
    """Import sentence_transformers synchronously (before uvicorn starts); fd 1 is pointed at stderr meanwhile."""
    global _ST
    if _ST is not None:
        return _ST
    saved = None
    try:
        sys.stdout.flush()
        saved = __import__("os").dup(1)
        __import__("os").dup2(2, 1)
    except Exception:  # noqa: BLE001
        saved = None
    try:
        import sentence_transformers  # noqa: WPS433
        _ST = sentence_transformers
    finally:
        if saved is not None:
            try:
                __import__("os").dup2(saved, 1)
                __import__("os").close(saved)
            except Exception:  # noqa: BLE001
                pass
    return _ST


def _log_error(msg):
    global _last_log
    now = time.time()
    if now - _last_log >= 60:
        _last_log = now
        try:
            print(msg, file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass


def _build_model(name):
    st = import_backend()
    try:
        from huggingface_hub import constants as hc
        prev = hc.HF_HUB_OFFLINE
        hc.HF_HUB_OFFLINE = True
        try:
            return st.SentenceTransformer(name, local_files_only=True)
        finally:
            hc.HF_HUB_OFFLINE = prev
    except Exception:  # noqa: BLE001
        pass
    return st.SentenceTransformer(name)


def _load_locked(name):
    """Caller holds _model_lock."""
    global _model, _model_name, _last_error, _last_attempt
    if _model is not None and _model_name == name:
        return _model
    _last_attempt = time.time()
    try:
        _model = _build_model(name)
        _model_name = name
        _last_error = None
    except Exception as exc:  # noqa: BLE001
        _model = None
        _last_error = "%s: %s" % (type(exc).__name__, exc)
        _log_error("N3MC Lite: embedding model could not be loaded (%s)" % _last_error)
    return _model


def _background_load(name):
    with _model_lock:
        _load_locked(name)


def get_model(name, no_wait=False):
    """Double-checked-locking singleton. no_wait: never lock, never wait; start a background load when idle."""
    global _bg_thread
    if _model is not None and _model_name == name:
        return _model
    if no_wait:
        if not _model_lock.locked():
            retry_ok = _last_error is None or time.time() - _last_attempt >= MODEL_RETRY_SECONDS
            if retry_ok and (_bg_thread is None or not _bg_thread.is_alive()):
                _bg_thread = threading.Thread(target=_background_load, args=(name,), daemon=True)
                _bg_thread.start()
        return None
    if not _model_lock.acquire(timeout=MODEL_WAIT_SECONDS):
        return None
    try:
        if _model is not None and _model_name == name:
            return _model
        if _last_error is not None and time.time() - _last_attempt < MODEL_RETRY_SECONDS:
            return None
        return _load_locked(name)
    finally:
        _model_lock.release()


def start_background_load(name):
    """Start loading the model in a background thread (resident server lifespan)."""
    global _bg_thread
    if _model is not None or (_bg_thread is not None and _bg_thread.is_alive()):
        return _bg_thread
    _bg_thread = threading.Thread(target=_background_load, args=(name,), daemon=True)
    _bg_thread.start()
    return _bg_thread


def model_status():
    """The last load error, or None."""
    return _last_error


def model_unavailable_message(cfg) -> str:
    name = cfg.get("embedding_model", "intfloat/multilingual-e5-base")
    if _last_error:
        return ("The embedding model (%s) could not be loaded (%s), so search is keyword-only (BM25) "
                "and new entries are saved without vectors. The first start downloads the model "
                "(~1.1 GB) from Hugging Face: check the network or proxy, or pre-download it. "
                "The server retries automatically." % (name, _last_error))
    return ("The embedding model (%s) is still loading (the first start downloads ~1.1 GB), so this "
            "search used keywords only and new entries are saved without vectors. "
            "Retry in a minute." % name)


def embed(text: str, cfg, query: bool = False, no_wait: bool = False):
    """Embed text with the e5 prefixes; None when the model is not available."""
    name = cfg.get("embedding_model", "intfloat/multilingual-e5-base")
    model = get_model(name, no_wait=no_wait)
    if model is None:
        return None
    prefix = "query: " if query else "passage: "
    vec = model.encode(prefix + normalize_text(text), normalize_embeddings=True)
    return [float(x) for x in vec]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
