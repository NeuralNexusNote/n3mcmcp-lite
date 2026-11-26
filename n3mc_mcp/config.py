"""config.json: load, validate, create exclusively, fill, migrate (spec §6, §7); code_version (§3.15);
the Redis location texts (§3.9, §3.15)."""
import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
import time
import uuid
from urllib.parse import urlparse

from . import paths

DEFAULTS = {
    "redis_url": "redis://localhost:6379/0",
    "ttl_seconds": 604800,
    "half_life_days": 3,
    "bm25_min_threshold": 0.1,
    "search_result_limit": 20,
    "min_score": 0.2,
    "vector_min_cos": 0.85,
    "relative_min_score_ratio": 0.6,
    "min_results": 3,
    "version_order_enabled": True,
    "version_sim_threshold": 0.98,
    "version_min_chars": 100,
    "search_query_max_chars": 2000,
    "chunk_threshold": 400,
    "chunk_overlap": 100,
    "access_count_enabled": False,
    "access_count_weight": 0.02,
    "access_count_max_boost": 0.5,
    "ttl_refresh_top_k": 5,
    "lexical_rerank_enabled": True,
    "rerank_weight": 0.3,
    "rerank_phrase_weight": 0.2,
    "b_session_match": 1.0,
    "b_session_mismatch": 0.6,
    "skip_code_blocks": False,
    "embedding_model": "intfloat/multilingual-e5-base",
    "embedding_dim": 768,
    "server_port": 18524,
    "bind_host": "127.0.0.1",
    "context_max_chars": 9500,
}

ID_KEYS = ("owner_id", "local_id")

# key -> (kind, low, high, low_inclusive); kind "int" = JSON integer, "num" = any number
_RANGES = {
    "ttl_seconds": ("int", 1, None, True),
    "search_result_limit": ("int", 1, None, True),
    "search_query_max_chars": ("int", 1, None, True),
    "embedding_dim": ("int", 1, None, True),
    "chunk_threshold": ("int", 50, None, True),
    "ttl_refresh_top_k": ("int", 0, None, True),
    "version_min_chars": ("int", 0, None, True),
    "min_results": ("int", 0, None, True),
    "half_life_days": ("num", 0, None, False),
    "bm25_min_threshold": ("num", 0, None, True),
    "access_count_weight": ("num", 0, None, True),
    "access_count_max_boost": ("num", 0, None, True),
    "rerank_weight": ("num", 0, None, True),
    "rerank_phrase_weight": ("num", 0, None, True),
    "min_score": ("num", 0, 1, True),
    "vector_min_cos": ("num", 0, 1, True),
    "relative_min_score_ratio": ("num", 0, 1, True),
    "version_sim_threshold": ("num", 0, 1, True),
    "b_session_match": ("num", 0, 1, True),
    "b_session_mismatch": ("num", 0, 1, True),
    "server_port": ("int", 1, 65535, True),
    "context_max_chars": ("int", 2000, 10000, True),
}

# Keys of earlier versions that are read and ignored (§6).
IGNORED_KEYS = ("dedup_threshold", "context_char_limit", "ttl_refresh_on_search")


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def config_value_ok(key, value, chunk_threshold=None) -> bool:
    """Type and range check of one config key (§6). Keys without a default are not checked."""
    if key not in DEFAULTS:
        return True
    default = DEFAULTS[key]
    if key == "chunk_overlap":
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return False
        thr = chunk_threshold if chunk_threshold is not None else DEFAULTS["chunk_threshold"]
        return value < thr
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, str)
    rng = _RANGES.get(key)
    if rng is None:
        return _is_number(value)
    kind, low, high, low_incl = rng
    if kind == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            return False
    else:
        if not _is_number(value) or (isinstance(value, float) and not math.isfinite(value)):
            return False
    if low is not None and (value < low if low_incl else value <= low):
        return False
    if high is not None and value > high:
        return False
    return True


# ── Reading and writing the file ─────────────────────────────────────────────

def _read_bytes(path):
    """Read a file; on Windows retry a PermissionError every 0.05 s for up to 1 s."""
    deadline = time.monotonic() + 1.0
    while True:
        try:
            with open(path, "rb") as f:
                return f.read()
        except PermissionError:
            if os.name != "nt" or time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def _replace(tmp, path):
    deadline = time.monotonic() + 1.0
    while True:
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if os.name != "nt" or time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def _dump(data: dict) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _write_tmp(folder, data: bytes) -> str:
    fd, tmp = tempfile.mkstemp(prefix=".config.", suffix=".tmp", dir=folder)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return tmp


def atomic_write(path, data: bytes):
    """Update: write a temporary file in the same folder, then os.replace."""
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    tmp = _write_tmp(folder, data)
    try:
        _replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def create_exclusive(path, data: bytes) -> bool:
    """Create path only if it does not exist (temporary file + os.link, else open "x").
    Returns False when another process created it first."""
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    tmp = _write_tmp(folder, data)
    try:
        try:
            os.link(tmp, path)
            return True
        except FileExistsError:
            return False
        except (OSError, NotImplementedError, AttributeError):
            try:
                with open(path, "xb") as f:
                    f.write(data)
                return True
            except FileExistsError:
                return False
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _parse(data: bytes):
    """(dict, None) or (None, reason)."""
    try:
        obj = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as e:
        return None, f"{type(e).__name__}: {e}"
    if not isinstance(obj, dict):
        return None, "the top level is not a JSON object"
    return obj, None


def _new_ids():
    return {"owner_id": str(uuid.uuid4()), "local_id": str(uuid.uuid4())}


def _lock_path(path):
    return path + ".lock"


def _acquire_lock(path) -> bool:
    lock = _lock_path(path)
    deadline = time.monotonic() + 10.0
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock) > 10.0:
                    os.remove(lock)
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                return False
            time.sleep(0.05)
        except OSError:
            return False


def _release_lock(path):
    try:
        os.remove(_lock_path(path))
    except OSError:
        pass


def _migrate_legacy(path, notices):
    """Copy the 1.6.x config (platformdirs) to the new folder, byte for byte, once (§7)."""
    if os.environ.get("N3MC_DATA_DIR", "").strip():
        return
    legacy = paths.legacy_config_path()
    if not legacy or not os.path.isfile(legacy):
        return
    try:
        data = _read_bytes(legacy)
    except OSError:
        return
    if create_exclusive(path, data):
        print(f"[n3mc-lite] copied the settings from {legacy} to {path} (the old file was kept)",
              file=sys.stderr)


def _create_new(path):
    """Create the missing file with defaults and new ids; if another process wins, read its file."""
    data = dict(_new_ids())
    data.update(DEFAULTS)
    raw = _dump(data)
    if create_exclusive(path, raw):
        return raw
    deadline = time.monotonic() + 2.0
    while True:
        try:
            raw = _read_bytes(path)
            obj, _err = _parse(raw)
            if obj is not None or time.monotonic() > deadline:
                return raw
        except OSError:
            if time.monotonic() > deadline:
                raise
        time.sleep(0.1)


def _fill_ids(path, notices):
    """Add the missing owner_id / local_id under config.json.lock (§6). Returns (obj, raw)."""
    got = _acquire_lock(path)
    try:
        raw = _read_bytes(path)
        obj, err = _parse(raw)
        if obj is None:
            return None, raw
        if all(isinstance(obj.get(k), str) and obj.get(k) for k in ID_KEYS):
            return obj, raw
        ids = _new_ids()
        for k in ID_KEYS:
            if not (isinstance(obj.get(k), str) and obj.get(k)):
                obj[k] = ids[k]
        new_raw = _dump(obj)
        try:
            atomic_write(path, new_raw)
            return obj, new_raw
        except OSError as e:
            notices.append(f"config_unreadable: {path} could not be updated ({type(e).__name__}: {e}); "
                           "this run uses temporary ids.")
            return obj, raw
    finally:
        if got:
            _release_lock(path)


def load_config(path=None) -> dict:
    """Load config.json (§6). The returned dict holds the effective values, plus:
    _notices (list of "key: message" strings), _config_path, _config_bytes (the bytes used, for
    code_version), _config_unreadable (bool)."""
    path = path or paths.config_path()
    notices = []
    unreadable = False
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    except OSError:
        pass
    if not os.path.exists(path):
        _migrate_legacy(path, notices)
    raw = None
    obj = None
    if not os.path.exists(path):
        try:
            raw = _create_new(path)
        except OSError as e:
            notices.append(f"config_unreadable: {path} could not be created ({type(e).__name__}: {e}); "
                           "the defaults and temporary ids are used.")
            unreadable = True
    if raw is None and not unreadable:
        try:
            raw = _read_bytes(path)
        except OSError as e:
            notices.append(f"config_unreadable: {path} could not be read ({type(e).__name__}: {e}); "
                           "the defaults and a temporary owner_id are used, and the file was not changed.")
            unreadable = True
    if raw is not None and not unreadable:
        obj, err = _parse(raw)
        if obj is None:
            notices.append(f"config_unreadable: {path} could not be read ({err}); the defaults and a "
                           "temporary owner_id are used, and the file was not changed. Fix the file.")
            unreadable = True
    cfg = {}
    if unreadable:
        cfg.update(DEFAULTS)
        cfg.update(_new_ids())
    else:
        missing_ids = not all(isinstance(obj.get(k), str) and obj.get(k) for k in ID_KEYS)
        if missing_ids:
            filled, raw2 = _fill_ids(path, notices)
            if filled is not None:
                obj, raw = filled, raw2
        bad = []
        for key, default in DEFAULTS.items():
            if key == "chunk_overlap":
                continue
            if key in obj:
                if config_value_ok(key, obj[key]):
                    cfg[key] = obj[key]
                else:
                    bad.append(key)
                    cfg[key] = default
            else:
                cfg[key] = default
        thr = cfg["chunk_threshold"]
        if "chunk_overlap" in obj:
            if config_value_ok("chunk_overlap", obj["chunk_overlap"], chunk_threshold=thr):
                cfg["chunk_overlap"] = obj["chunk_overlap"]
            else:
                bad.append("chunk_overlap")
                cfg["chunk_overlap"] = DEFAULTS["chunk_overlap"] if DEFAULTS["chunk_overlap"] < thr else thr // 4
        else:
            cfg["chunk_overlap"] = DEFAULTS["chunk_overlap"] if DEFAULTS["chunk_overlap"] < thr else thr // 4
        for k in ID_KEYS:
            cfg[k] = obj.get(k) if isinstance(obj.get(k), str) and obj.get(k) else str(uuid.uuid4())
        if bad:
            notices.append(f"config_type: {path}: {', '.join(bad)} had the wrong type or a value out of "
                           "range and was ignored (the default is used). Fix the value in the file.")
        missing = [k for k in DEFAULTS if k not in obj]
        if missing:
            fill = dict(obj)
            for k in missing:
                fill[k] = cfg[k]
            new_raw = _dump(fill)
            try:
                atomic_write(path, new_raw)
                raw = new_raw
            except OSError as e:
                print(f"[n3mc-lite] could not update {path}: {type(e).__name__}: {e}", file=sys.stderr)
    env_url = os.environ.get("N3MC_REDIS_URL", "").strip()
    if env_url:
        cfg["redis_url"] = env_url
    cfg["_notices"] = notices
    cfg["_config_path"] = path
    cfg["_config_bytes"] = raw if (raw is not None) else None
    cfg["_config_unreadable"] = unreadable
    for n in notices:
        print(f"[n3mc-lite] {n}", file=sys.stderr)
    return cfg


def public_config(cfg: dict) -> dict:
    return {k: v for k, v in cfg.items() if not k.startswith("_")}


# ── code_version (§3.15) ─────────────────────────────────────────────────────

CODE_FILES = ("app.py", "tools.py", "tooldefs.py", "instructions.py", "context.py", "database.py",
              "processor.py", "config.py", "paths.py", "cli.py")


def _config_part(config_bytes) -> bytes:
    if config_bytes is None:
        return b""
    obj, _err = _parse(config_bytes)
    if obj is not None:
        return json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return config_bytes


def compute_code_version(config_bytes) -> str:
    h = hashlib.sha256()
    pkg = os.path.dirname(os.path.abspath(__file__))
    for name in CODE_FILES:
        try:
            with open(os.path.join(pkg, name), "rb") as f:
                h.update(f.read())
        except OSError:
            pass
    h.update(_config_part(config_bytes))
    return h.hexdigest()[:16]


def current_config_bytes(path=None):
    path = path or paths.config_path()
    try:
        return _read_bytes(path)
    except OSError:
        return None


# ── The Redis location, without user name and password ──────────────────────

UNPARSEABLE = "<unparseable redis_url>"


def redis_display(url) -> str:
    """host:port/db of a redis URL, or <unparseable redis_url> (never any part of the input)."""
    try:
        p = urlparse(str(url))
        host = p.hostname or "localhost"
        port = p.port or 6379
        db = (p.path or "/0").lstrip("/") or "0"
        int(db)
        return f"{host}:{port}/{db}"
    except Exception:  # noqa: BLE001
        return UNPARSEABLE


def hostport(url) -> str:
    try:
        p = urlparse(str(url))
        return f"{p.hostname or 'localhost'}:{p.port or 6379}"
    except Exception:  # noqa: BLE001
        return UNPARSEABLE


DOCKER_RUN = "docker run -d --name redis-stack -p 127.0.0.1:6379:6379 redis/redis-stack-server:latest"


def backend_hint(url) -> str:
    return (f"Redis Stack is not reachable at {hostport(url)}. If Docker itself is not running, start "
            f"Docker (Docker Desktop) first. Then run: docker start redis-stack (first time: {DOCKER_RUN}). "
            "The memory server reconnects automatically — no restart needed; just retry.")


def no_search_hint(url, reason) -> str:
    return (f"The Redis at {hostport(url)} does not provide RediSearch (FT.CREATE failed: {reason}). "
            f"N3MC Lite needs Redis Stack, not plain Redis: {DOCKER_RUN}")
