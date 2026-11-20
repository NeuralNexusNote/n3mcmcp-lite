"""config.json load/save and UUID generation (spec §6, §7)."""

import json
import math
import os
import pathlib
import sys
import tempfile
import time
import uuid

from n3mc_mcp import paths

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
    "ttl_refresh_on_search": True,
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

# key -> (kind, low, high, low_inclusive)   kind: "int" | "num"
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


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def config_value_ok(key, value, chunk_threshold=None) -> bool:
    """Type and range check of one config value (§6). Unknown keys and IDs are not checked."""
    if key not in DEFAULTS:
        return True
    if key == "chunk_overlap":
        if not _is_int(value) or value < 0:
            return False
        ct = chunk_threshold if chunk_threshold is not None else DEFAULTS["chunk_threshold"]
        return value < ct
    default = DEFAULTS[key]
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, str)
    rng = _RANGES.get(key)
    if rng is None:
        return _is_num(value)
    kind, lo, hi, lo_incl = rng
    if kind == "int" and not _is_int(value):
        return False
    if kind == "num" and not _is_num(value):
        return False
    if lo is not None and (value < lo if lo_incl else value <= lo):
        return False
    if hi is not None and value > hi:
        return False
    return True


def _stderr(msg: str) -> None:
    try:
        print(msg, file=sys.stderr, flush=True)
    except Exception:
        pass


def _write_tmp(folder: pathlib.Path, data: bytes) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=str(folder))
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return tmp


def _create_exclusive(path: pathlib.Path, data: bytes) -> bool:
    """Create `path` only if it does not exist (§6). True when this call created it."""
    tmp = _write_tmp(path.parent, data)
    try:
        try:
            os.link(tmp, str(path))
            return True
        except FileExistsError:
            return False
        except OSError:
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


def replace_atomic(path: pathlib.Path, data: bytes) -> None:
    tmp = _write_tmp(path.parent, data)
    try:
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _dump(obj) -> bytes:
    return (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _read_json(path: pathlib.Path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def _new_file_content() -> dict:
    d = {"owner_id": str(uuid.uuid4()), "local_id": str(uuid.uuid4())}
    d.update(DEFAULTS)
    return d


def _migrate_legacy(path: pathlib.Path) -> None:
    if os.environ.get("N3MC_DATA_DIR"):
        return
    try:
        legacy = paths.legacy_config_path()
    except Exception:
        return
    if not legacy.is_file():
        return
    try:
        data = legacy.read_bytes()
    except OSError:
        return
    if _create_exclusive(path, data):
        _stderr(f"N3MC Lite: copied the settings from {legacy} to {path} (the old file was left in place).")


def load_config() -> dict:
    """Load (or create) config.json. Never overwrites a file it cannot read (§6)."""
    path = paths.config_path()
    notices = []
    file_data = None
    unreadable = False

    if not path.exists():
        _migrate_legacy(path)
    if not path.exists():
        _create_exclusive(path, _dump(_new_file_content()))

    deadline = time.time() + 2.0
    while True:
        try:
            file_data = _read_json(path)
            if not isinstance(file_data, dict):
                raise ValueError("the top level is not a JSON object")
            break
        except Exception as e:  # noqa: BLE001
            reason = f"{type(e).__name__}: {e}"
            if time.time() < deadline and isinstance(e, (json.JSONDecodeError, ValueError)) and path.exists() \
                    and path.stat().st_size == 0:
                time.sleep(0.1)
                continue
            unreadable = True
            file_data = None
            notices.append(
                f"config_unreadable: {path} could not be read ({reason}), so N3MC Lite is running with "
                f"the default settings and a temporary owner_id for now; memories saved meanwhile will not be "
                f"visible after the server restarts. The file was left unchanged: fix it and the server "
                f"restarts with it on the next call.")
            break

    cfg = dict(DEFAULTS)
    if unreadable:
        cfg["owner_id"] = str(uuid.uuid4())
        cfg["local_id"] = str(uuid.uuid4())
    else:
        bad = []
        for key in DEFAULTS:
            if key == "chunk_overlap" or key not in file_data:
                continue
            if config_value_ok(key, file_data[key]):
                cfg[key] = file_data[key]
            else:
                bad.append(key)
        ct = cfg["chunk_threshold"]
        if "chunk_overlap" in file_data:
            if config_value_ok("chunk_overlap", file_data["chunk_overlap"], ct):
                cfg["chunk_overlap"] = file_data["chunk_overlap"]
            else:
                bad.append("chunk_overlap")
                cfg["chunk_overlap"] = DEFAULTS["chunk_overlap"]
        if cfg["chunk_overlap"] >= ct:
            cfg["chunk_overlap"] = ct // 4
        if bad:
            notices.append(
                f"config_type: these settings in {path} have the wrong type or are out of range and were "
                f"ignored (the defaults are used): {', '.join(bad)}.")
        changed = False
        for k in ("owner_id", "local_id"):
            v = file_data.get(k)
            if isinstance(v, str) and v:
                cfg[k] = v
            else:
                file_data[k] = cfg[k] = str(uuid.uuid4())
                changed = True
        for key, val in DEFAULTS.items():
            if key not in file_data:
                file_data[key] = val
                changed = True
        if changed:
            try:
                replace_atomic(path, _dump(file_data))
            except OSError as e:
                _stderr(f"N3MC Lite: could not update {path}: {e}")

    env_url = os.environ.get("N3MC_REDIS_URL")
    if env_url:
        cfg["redis_url"] = env_url
    cfg["_notices"] = notices
    cfg["_unreadable"] = unreadable
    cfg["_path"] = str(path)
    for n in notices:
        _stderr("N3MC Lite: " + n)
    return cfg


CODE_FILES = ("app.py", "tools.py", "tooldefs.py", "instructions.py", "context.py", "database.py",
              "processor.py", "config.py", "paths.py", "cli.py")


def code_version() -> str:
    """SHA-256 (first 16 hex) of the server's code files plus config.json (§3.15)."""
    import hashlib
    pkg = os.path.dirname(os.path.abspath(__file__))
    h = hashlib.sha256()
    for name in CODE_FILES:
        try:
            with open(os.path.join(pkg, name), "rb") as f:
                h.update(f.read())
        except OSError:
            pass
    h.update(config_fingerprint())
    return h.hexdigest()[:16]


def config_fingerprint() -> bytes:
    """config.json as it counts for code_version (§3.15): canonical JSON, raw bytes, or nothing."""
    path = paths.config_path()
    try:
        raw = path.read_bytes()
    except OSError:
        return b""
    try:
        obj = json.loads(raw.decode("utf-8-sig"))
        if isinstance(obj, dict):
            return json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")
    except Exception:
        pass
    return raw
