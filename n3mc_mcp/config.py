"""config.json load/save, validation, UUID generation, code_version (spec sections 3.9, 3.15, 6, 7)."""

import copy
import hashlib
import json
import os
import sys
import tempfile
import time
import uuid

from . import paths

DEFAULTS = {
    "owner_id": "",
    "local_id": "",
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

_INT_MIN = {
    "ttl_seconds": 1, "search_result_limit": 1, "search_query_max_chars": 1, "embedding_dim": 1,
    "chunk_threshold": 50, "ttl_refresh_top_k": 0, "version_min_chars": 0, "min_results": 0,
}
_NUM_POSITIVE = ("half_life_days",)
_NUM_NONNEG = ("bm25_min_threshold", "access_count_weight", "access_count_max_boost",
               "rerank_weight", "rerank_phrase_weight")
_NUM_UNIT = ("min_score", "vector_min_cos", "relative_min_score_ratio", "version_sim_threshold",
             "b_session_match", "b_session_mismatch")


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")


def config_value_ok(key, value, chunk_threshold=None):
    """Type and range check for one setting (spec section 6). Unknown keys and keys without a default pass."""
    if key not in DEFAULTS or key in ("owner_id", "local_id"):
        return True
    default = DEFAULTS[key]
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, str)
    if key in _INT_MIN:
        return _is_int(value) and value >= _INT_MIN[key]
    if key == "chunk_overlap":
        ct = chunk_threshold if chunk_threshold is not None else DEFAULTS["chunk_threshold"]
        return _is_int(value) and 0 <= value < ct
    if key == "server_port":
        return _is_int(value) and 1 <= value <= 65535
    if key == "context_max_chars":
        return _is_int(value) and 2000 <= value <= 10000
    if key in _NUM_POSITIVE:
        return _is_num(value) and value > 0
    if key in _NUM_NONNEG:
        return _is_num(value) and value >= 0
    if key in _NUM_UNIT:
        return _is_num(value) and 0 <= value <= 1
    return True


def _atomic_write(path, text):
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        _retry_permission(lambda: os.replace(tmp, path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _create_exclusive(path, data: bytes) -> bool:
    """Create path only if it does not exist yet (temp file + os.link, or open(path, 'x'))."""
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
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
            os.unlink(tmp)
        except OSError:
            pass


def _parse(data: bytes):
    obj = json.loads(data.decode("utf-8-sig"))
    if not isinstance(obj, dict):
        raise ValueError("top level is not a JSON object")
    return obj


def _retry_permission(fn, seconds=1.0):
    """Windows: a file another process (or a virus scanner) has open briefly refuses os.replace / open
    with PermissionError. Retry for up to a second so a fill-in is not lost to that."""
    deadline = time.time() + seconds
    while True:
        try:
            return fn()
        except PermissionError:
            if time.time() >= deadline:
                raise
            time.sleep(0.05)


def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def _read_json_bytes(path):
    """(parsed object, the bytes it was parsed from). A ValueError carries the bytes as .config_bytes."""
    data = _retry_permission(lambda: _read_bytes(path))
    try:
        return _parse(data), data
    except ValueError as exc:
        exc.config_bytes = data
        raise


def _read_json(path):
    return _read_json_bytes(path)[0]


def _read_with_retry(path):
    """Read a config another process just created; retry up to 2 s while it is still unparsable."""
    deadline = time.time() + 2.0
    while True:
        try:
            return _read_json_bytes(path)
        except (ValueError, OSError):
            if time.time() >= deadline:
                raise
            time.sleep(0.1)


LOCK_TIMEOUT = 10.0
LOCK_STALE = 10.0


def _acquire_lock(lock):
    """open(lock, 'x'); retry every 0.05 s for up to 10 s; a lock file older than 10 s is stale and removed."""
    deadline = time.time() + LOCK_TIMEOUT
    while True:
        try:
            with open(lock, "x", encoding="utf-8") as f:
                f.write(str(os.getpid()))
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock) > LOCK_STALE:
                    os.unlink(lock)
                    continue
            except OSError:
                pass
        except OSError:
            return False
        if time.time() >= deadline:
            return False
        time.sleep(0.05)


def _release_lock(lock):
    try:
        os.unlink(lock)
    except OSError:
        pass


def _emit(cfg, message):
    cfg["_notices"].append(message)
    try:
        print(message, file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


def _effective(raw, cfg):
    """Fill cfg from raw: valid keys only, invalid keys reported."""
    bad = []
    ct = raw.get("chunk_threshold")
    if "chunk_threshold" not in raw or not config_value_ok("chunk_threshold", ct):
        ct = DEFAULTS["chunk_threshold"]
    for key, default in DEFAULTS.items():
        if key in ("owner_id", "local_id"):
            continue
        if key in raw:
            v = raw[key]
            if config_value_ok(key, v, chunk_threshold=ct):
                cfg[key] = v
            else:
                bad.append(key)
                cfg[key] = default
        else:
            cfg[key] = default
    # chunk_overlap must stay below the effective chunk_threshold
    if cfg["chunk_overlap"] >= cfg["chunk_threshold"]:
        cfg["chunk_overlap"] = cfg["chunk_threshold"] // 4
    for key, v in raw.items():
        if key not in DEFAULTS and key not in cfg:
            cfg[key] = v
    return bad


def load_config():
    """Load config.json (creating it exclusively when missing). Never overwrites an unreadable file."""
    path = paths.config_path()
    cfg = copy.deepcopy(DEFAULTS)
    cfg["_notices"] = []
    cfg["_unreadable"] = False
    cfg["_path"] = path

    # One-time migration from the v1.6.x location.
    if not os.path.exists(path) and not os.environ.get("N3MC_DATA_DIR"):
        try:
            legacy = paths.legacy_config_path()
            if os.path.isfile(legacy):
                with open(legacy, "rb") as f:
                    data = f.read()
                if _create_exclusive(path, data):
                    print("N3MC Lite: migrated config.json from %s to %s" % (legacy, path), file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print("N3MC Lite: could not migrate the old config.json (%s)" % exc, file=sys.stderr)

    # The config.json content this process runs with: the bytes it parsed, or the bytes it wrote.
    # The server's code_version() hashes exactly these (2026-11-22, review #9). None: no file.
    cfg["_config_bytes"] = None
    if not os.path.exists(path):
        new = {"owner_id": str(uuid.uuid4()), "local_id": str(uuid.uuid4())}
        new.update({k: v for k, v in DEFAULTS.items() if k not in new})
        data = (json.dumps(new, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            if _create_exclusive(path, data):
                raw = new
            else:
                raw, data = _read_with_retry(path)
        except (ValueError, OSError) as exc:
            cfg["_config_bytes"] = getattr(exc, "config_bytes", None)
            _unreadable(cfg, path, exc)
            return _finish(cfg)
    else:
        try:
            raw, data = _read_json_bytes(path)
        except (ValueError, OSError) as exc:
            cfg["_config_bytes"] = getattr(exc, "config_bytes", None)
            _unreadable(cfg, path, exc)
            return _finish(cfg)
    cfg["_config_bytes"] = data

    if not all(_id_ok(raw.get(k)) for k in ("owner_id", "local_id")):
        # A missing owner_id / local_id is filled under config.json.lock, from the file as read again under
        # the lock, so every process runs with the IDs that are in the file (2026-11-22, review #2).
        lock = path + ".lock"
        locked = _acquire_lock(lock)
        try:
            if locked:
                try:
                    raw, data = _read_json_bytes(path)
                    cfg["_config_bytes"] = data
                except (ValueError, OSError):
                    pass  # keep the content read before; it is filled in below as before
            merged = _fill_missing(cfg, path, raw)
        finally:
            if locked:
                _release_lock(lock)
    else:
        merged = _fill_missing(cfg, path, raw)

    bad = _effective(raw, cfg)  # the settings as the file had them (filled-in keys are the defaults)
    for key in ("owner_id", "local_id"):
        cfg[key] = merged[key]
    if bad:
        _emit(cfg, "config_type: %s has a wrong type or out-of-range value for: %s. "
                   "Those settings were ignored and the defaults are used." % (path, ", ".join(bad)))
    return _finish(cfg)


def _id_ok(v):
    return isinstance(v, str) and bool(v)


def _fill_missing(cfg, path, raw):
    """Fill missing fields (an ID only when it is still missing) and save; returns the content used."""
    merged = dict(raw)
    missing = [k for k in DEFAULTS if k not in raw]
    for k in ("owner_id", "local_id"):
        if not _id_ok(raw.get(k)):
            merged[k] = str(uuid.uuid4())
            if k not in missing:
                missing.append(k)
    if not missing:
        return raw
    for k in missing:
        if k not in ("owner_id", "local_id"):
            merged[k] = DEFAULTS[k]
    text = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
    try:
        _atomic_write(path, text)
        cfg["_config_bytes"] = text.encode("utf-8")
    except OSError as exc:
        print("N3MC Lite: could not update %s (%s)" % (path, exc), file=sys.stderr)
    return merged


def _unreadable(cfg, path, exc):
    cfg["_unreadable"] = True
    cfg["owner_id"] = str(uuid.uuid4())
    cfg["local_id"] = str(uuid.uuid4())
    _emit(cfg, "config_unreadable: %s could not be read (%s). The file was left unchanged; "
               "defaults and a temporary owner_id are used. Fix the file and restart the client."
          % (path, exc))


def _finish(cfg):
    env_url = os.environ.get("N3MC_REDIS_URL")
    if env_url:
        cfg["redis_url"] = env_url
    return cfg


_CODE_FILES = ("app.py", "tools.py", "tooldefs.py", "instructions.py", "context.py", "database.py",
               "processor.py", "config.py", "paths.py", "cli.py")


_FROM_DISK = object()


def code_version(config_bytes=_FROM_DISK) -> str:
    """First 16 hex chars of SHA-256 over the code files followed by config.json (spec section 3.15).

    config_bytes: the config.json content to hash -- the server passes cfg["_config_bytes"] of the
    load_config() it runs with (None: no file; review #9); by default the file is read (the client side)."""
    h = hashlib.sha256()
    here = os.path.dirname(os.path.abspath(__file__))
    for name in _CODE_FILES:
        try:
            with open(os.path.join(here, name), "rb") as f:
                h.update(f.read())
        except OSError:
            pass
    raw = config_bytes
    if raw is _FROM_DISK:
        try:
            with open(paths.config_path(), "rb") as f:
                raw = f.read()
        except OSError:
            raw = None
    if raw is not None:
        try:
            obj = json.loads(raw.decode("utf-8-sig"))
            if isinstance(obj, dict):
                raw = json.dumps(obj, sort_keys=True).encode("utf-8")
        except ValueError:
            pass
        h.update(raw)
    return h.hexdigest()[:16]
