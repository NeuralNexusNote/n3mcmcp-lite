"""config.json: load, validate, create exclusively, complete atomically, migrate once (spec §6, §7).
Also the code_version of the resident server (§3.15) and the Redis display helpers."""
import hashlib
import json
import math
import os
import sys
import tempfile
import threading
import time
import uuid
from urllib.parse import urlsplit

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

# key -> (kind, low, high, low_inclusive)   kind: "int" | "num"
_RANGES = {
    "ttl_seconds": ("int", 1, None, True),
    "search_result_limit": ("int", 1, None, True),
    "search_query_max_chars": ("int", 1, None, True),
    "embedding_dim": ("int", 1, None, True),
    "chunk_threshold": ("int", 50, None, True),
    "chunk_overlap": ("int", 0, None, True),
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

# The in-process notices (cfg["_notices"]) and their "shown" set are changed under this lock (§3.15).
NOTICE_LOCK = threading.Lock()

CODE_FILES = ("app.py", "tools.py", "tooldefs.py", "instructions.py", "context.py", "database.py",
              "processor.py", "config.py", "paths.py", "cli.py")


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def config_value_ok(key, value, chunk_threshold=None) -> bool:
    """Type and range check of one config.json value (§6). Unknown keys and the ids pass."""
    if key not in DEFAULTS:
        return True
    default = DEFAULTS[key]
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, str)
    rng = _RANGES.get(key)
    if rng is None:
        return _is_num(value)
    kind, low, high, low_incl = rng
    if kind == "int" and not _is_int(value):
        return False
    if kind == "num" and not _is_num(value):
        return False
    if low is not None:
        if low_incl and value < low:
            return False
        if not low_incl and value <= low:
            return False
    if high is not None and value > high:
        return False
    if key == "chunk_overlap":
        ct = DEFAULTS["chunk_threshold"] if chunk_threshold is None else chunk_threshold
        if value >= ct:
            return False
    return True


def _fill_defaults(data: dict) -> None:
    """Fill the missing fields with their defaults (§6). A missing chunk_overlap is written as the value
    in effect: the default 100 is not below a chunk_threshold of 100 or less, and writing it would make
    every later start report a chunk_overlap the user never wrote."""
    ct = data.get("chunk_threshold", DEFAULTS["chunk_threshold"])
    if not config_value_ok("chunk_threshold", ct):
        ct = DEFAULTS["chunk_threshold"]
    for k in DEFAULTS:
        if k in data:
            continue
        v = DEFAULTS[k]
        if k == "chunk_overlap" and v >= ct:
            v = ct // 4
        data[k] = v


def _read_bytes_retry(path: str) -> bytes:
    deadline = time.monotonic() + 1.0
    while True:
        try:
            with open(path, "rb") as f:
                return f.read()
        except PermissionError:
            if os.name != "nt" or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _parse(raw: bytes):
    """(dict, None) or (None, reason)."""
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as e:
        return None, f"{type(e).__name__}: {e}"
    if not isinstance(data, dict):
        return None, "the top level is not a JSON object"
    return data, None


def _dump(data: dict) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _write_tmp(folder: str, content: bytes) -> str:
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=folder)
    with os.fdopen(fd, "wb") as f:
        f.write(content)
    return tmp


def _create_exclusive(path: str, content: bytes) -> bool:
    """Create `path` with `content` only if it does not exist. True if this call created it."""
    folder = os.path.dirname(path)
    tmp = _write_tmp(folder, content)
    try:
        try:
            os.link(tmp, path)
            return True
        except FileExistsError:
            return False
        except (OSError, NotImplementedError, AttributeError):
            try:
                with open(path, "xb") as f:
                    f.write(content)
                return True
            except FileExistsError:
                return False
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _replace_atomic(path: str, content: bytes) -> None:
    tmp = _write_tmp(os.path.dirname(path), content)
    try:
        paths.replace_with_retry(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_winner(path: str):
    """After losing the exclusive create: read the other process's file (retry up to 2 s)."""
    deadline = time.monotonic() + 2.0
    while True:
        try:
            raw = _read_bytes_retry(path)
            data, _ = _parse(raw)
            if data is not None:
                return raw, data
        except OSError:
            pass
        if time.monotonic() >= deadline:
            return None, None
        time.sleep(0.1)


class _IdLock:
    """config.json.lock: open(..., "x"), retry every 0.05 s for up to 10 s; older than 10 s is stale."""

    def __init__(self, path: str):
        self.path = path + ".lock"
        self.held = False

    def __enter__(self):
        deadline = time.monotonic() + 10.0
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self.held = True
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > 10.0:
                        os.unlink(self.path)
                        continue
                except OSError:
                    pass
            except OSError:
                return self
            if time.monotonic() >= deadline:
                return self
            time.sleep(0.05)

    def __exit__(self, *exc):
        if self.held:
            try:
                os.unlink(self.path)
            except OSError:
                pass


def _migrate_legacy(path: str) -> None:
    """One-time copy of the pre-1.7 config.json (§7), with the exclusive-create rule."""
    if os.environ.get("N3MC_DATA_DIR"):
        return
    legacy = paths.legacy_config_path()
    if not legacy or not os.path.isfile(legacy):
        return
    try:
        raw = _read_bytes_retry(legacy)
    except OSError:
        return
    if _create_exclusive(path, raw):
        print(f"n3mc-lite: copied the configuration from {legacy} to {path}", file=sys.stderr)


def load_config() -> dict:
    """Load (create) config.json. Never overwrites a file the user broke (§6)."""
    notices = []
    path = paths.config_path()
    paths.ensure_data_dir()
    if not os.path.exists(path):
        _migrate_legacy(path)
    raw = None
    data = None
    unreadable_reason = None
    if not os.path.exists(path):
        fresh = {"owner_id": str(uuid.uuid4()), "local_id": str(uuid.uuid4())}
        fresh.update(DEFAULTS)
        content = _dump(fresh)
        if _create_exclusive(path, content):
            raw, data = content, fresh
        else:
            raw, data = _read_winner(path)
            if data is None:
                try:
                    raw = _read_bytes_retry(path)
                except OSError:
                    raw = None
                unreadable_reason = "it is not valid JSON"
    if data is None and unreadable_reason is None:
        try:
            raw = _read_bytes_retry(path)
            data, unreadable_reason = _parse(raw)
        except OSError as e:
            raw = None
            unreadable_reason = f"{type(e).__name__}: {e}"

    cfg = dict(DEFAULTS)
    cfg["_path"] = path
    cfg["_unreadable"] = False
    if data is None:
        cfg["_unreadable"] = True
        cfg["owner_id"] = str(uuid.uuid4())
        cfg["local_id"] = str(uuid.uuid4())
        notices.append(f"config_unreadable: {path} could not be read ({unreadable_reason}), so the "
                       "default settings and a temporary owner_id are used and the file was left "
                       "unchanged. Fix the file; the memory server restarts after config.json changes.")
    else:
        missing_ids = not (isinstance(data.get("owner_id"), str) and data.get("owner_id")) or \
            not (isinstance(data.get("local_id"), str) and data.get("local_id"))
        missing = [k for k in DEFAULTS if k not in data]
        written = {k for k in DEFAULTS if k in data}   # the keys the user wrote (only these are checked)
        if missing_ids or missing:
            try:
                if missing_ids:
                    with _IdLock(path):
                        raw2 = _read_bytes_retry(path)
                        d2, _ = _parse(raw2)
                        if d2 is not None:
                            data, raw = d2, raw2
                        for k in ("owner_id", "local_id"):
                            if not (isinstance(data.get(k), str) and data.get(k)):
                                data[k] = str(uuid.uuid4())
                        _fill_defaults(data)
                        content = _dump(data)
                        _replace_atomic(path, content)
                        raw = content
                else:
                    _fill_defaults(data)
                    content = _dump(data)
                    _replace_atomic(path, content)
                    raw = content
            except OSError as e:
                print(f"n3mc-lite: could not update {path} ({type(e).__name__}: {e})", file=sys.stderr)
                for k in ("owner_id", "local_id"):
                    if not (isinstance(data.get(k), str) and data.get(k)):
                        data[k] = str(uuid.uuid4())
        bad = []
        ct = data.get("chunk_threshold", DEFAULTS["chunk_threshold"])
        if not config_value_ok("chunk_threshold", ct):
            ct = DEFAULTS["chunk_threshold"]
        for k in DEFAULTS:
            if k not in data or k not in written:
                continue
            v = data[k]
            if config_value_ok(k, v, chunk_threshold=ct):
                cfg[k] = v
            else:
                bad.append(k)
        cfg["chunk_threshold"] = ct
        if cfg["chunk_overlap"] >= ct:
            cfg["chunk_overlap"] = ct // 4
        cfg["owner_id"] = data["owner_id"]
        cfg["local_id"] = data["local_id"]
        if bad:
            one = len(bad) == 1
            notices.append(f"config_type: {path}: {', '.join(bad)} {'has' if one else 'have'} the wrong "
                           f"type or a value out of range and {'was' if one else 'were'} ignored "
                           "(the default is used).")
    cfg["_raw"] = raw
    env_url = os.environ.get("N3MC_REDIS_URL")
    if env_url:
        cfg["redis_url"] = env_url
    if cfg.get("bind_host") != "127.0.0.1":
        pass  # the warning is printed by the server (§3.15)
    cfg["_notices"] = []
    cfg["_notices_shown"] = set()
    for n in notices:
        add_notice(cfg, n)
    return cfg


def add_notice(cfg: dict, message: str) -> None:
    """Add an in-process notice (§6) and print it to stderr. Duplicates are kept once."""
    with NOTICE_LOCK:
        lst = cfg.setdefault("_notices", [])
        if message in lst:
            return
        lst.append(message)
    try:
        print(f"N3MC notice: {message}", file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


def take_process_notices(cfg: dict) -> list:
    """The in-process notices not yet shown; marks them as shown (one time per process)."""
    with NOTICE_LOCK:
        shown = cfg.setdefault("_notices_shown", set())
        out = [n for n in cfg.get("_notices", []) if n not in shown]
        shown.update(out)
    return out


def config_unreadable_detail(cfg: dict) -> str:
    return ("config.json could not be read, so this entry was saved under a temporary owner_id and "
            f"will not be visible after the server restarts. Fix {cfg.get('_path')} and restart the "
            "MCP client.")


# ── code_version (§3.15) ──────────────────────────────────────────────────────

def compute_code_version(config_raw) -> str:
    h = hashlib.sha256()
    pkg = os.path.dirname(os.path.abspath(__file__))
    for name in CODE_FILES:
        try:
            with open(os.path.join(pkg, name), "rb") as f:
                h.update(f.read())
        except OSError:
            pass
    if config_raw is not None:
        data, _ = _parse(config_raw)
        if data is not None:
            h.update(json.dumps(data, sort_keys=True).encode("utf-8"))
        else:
            h.update(config_raw)
    return h.hexdigest()[:16]


# ── Redis display (§3.3, §3.15) ───────────────────────────────────────────────

UNPARSEABLE = "<unparseable redis_url>"


def _hostport(url: str) -> str:
    try:
        p = urlsplit(url)
        host = p.hostname or "localhost"
        port = p.port or 6379
        return f"{host}:{port}"
    except Exception:  # noqa: BLE001
        return UNPARSEABLE


def redis_display(url: str) -> str:
    """<host>:<port>/<db>, without user name and password."""
    try:
        p = urlsplit(url)
        host = p.hostname or "localhost"
        port = p.port or 6379
        db = (p.path or "/0").lstrip("/") or "0"
        if not db.isdigit():
            return UNPARSEABLE
        return f"{host}:{port}/{db}"
    except Exception:  # noqa: BLE001
        return UNPARSEABLE


def backend_hint(url: str) -> str:
    return (f"Redis Stack is not reachable at {_hostport(url)}. If Docker itself is not running, start "
            "Docker (Docker Desktop) first. Then run: docker start redis-stack (first time: docker run -d "
            "--name redis-stack -p 127.0.0.1:6379:6379 redis/redis-stack-server:latest). The memory "
            "server reconnects automatically — no restart needed; just retry.")
