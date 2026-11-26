"""config.json: load, validate, create exclusively, fill missing fields under a lock (spec §6, §7);
code_version (spec §3.15); the Redis display helpers and the recovery hints (spec §3.9).

No heavy imports here: the hooks, the CLI and the MCP thin client all read the configuration.
"""
import hashlib
import json
import math
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
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

_INT_MIN = {
    "ttl_seconds": 1, "search_result_limit": 1, "search_query_max_chars": 1, "embedding_dim": 1,
    "chunk_threshold": 50, "ttl_refresh_top_k": 0, "version_min_chars": 0, "min_results": 0,
}
_INT_RANGE = {"server_port": (1, 65535), "context_max_chars": (2000, 10000)}
_NUM_POSITIVE = {"half_life_days"}
_NUM_NONNEG = {"bm25_min_threshold", "access_count_weight", "access_count_max_boost",
               "rerank_weight", "rerank_phrase_weight"}
_NUM_UNIT = {"min_score", "vector_min_cos", "relative_min_score_ratio", "version_sim_threshold",
             "b_session_match", "b_session_mismatch"}

# The package files whose contents make up code_version (spec §3.15).
CODE_FILES = ("app.py", "tools.py", "tooldefs.py", "instructions.py", "context.py", "database.py",
              "processor.py", "config.py", "paths.py", "cli.py")

LOCK_RETRY = 0.05
LOCK_WAIT = 10.0
LOCK_STALE = 10.0
CREATE_READ_WAIT = 2.0


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and not (isinstance(v, float) and not math.isfinite(v)))


def config_value_ok(key, value, chunk_threshold=None) -> bool:
    """Type and range check of one key (spec §6). Keys without a default (owner_id, local_id) and
    unknown keys are not checked."""
    if key not in DEFAULTS and key != "chunk_overlap":
        return True
    if key == "chunk_overlap":
        ct = chunk_threshold if _is_int(chunk_threshold) else DEFAULTS["chunk_threshold"]
        return _is_int(value) and 0 <= value < ct
    default = DEFAULTS[key]
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, str)
    if key in _INT_MIN:
        return _is_int(value) and value >= _INT_MIN[key]
    if key in _INT_RANGE:
        lo, hi = _INT_RANGE[key]
        return _is_int(value) and lo <= value <= hi
    if not _is_num(value):
        return False
    if key in _NUM_POSITIVE:
        return value > 0
    if key in _NUM_NONNEG:
        return value >= 0
    if key in _NUM_UNIT:
        return 0 <= value <= 1
    return True


# ── Redis display and hints (spec §3.9, §3.15) ───────────────────────────────

UNPARSEABLE = "<unparseable redis_url>"


def _parse_redis_url(url):
    p = urlparse(url)
    host = p.hostname or "localhost"
    port = p.port or 6379          # raises ValueError for a non-numeric port
    path = (p.path or "").strip("/")
    db = int(path) if path else 0
    return host, port, db


def redis_display(url) -> str:
    """host:port/db without user name and password; a fixed text when the URL cannot be parsed."""
    try:
        host, port, db = _parse_redis_url(url)
        return f"{host}:{port}/{db}"
    except Exception:  # noqa: BLE001
        return UNPARSEABLE


def _hostport(url) -> str:
    try:
        host, port, _db = _parse_redis_url(url)
        return f"{host}:{port}"
    except Exception:  # noqa: BLE001
        return UNPARSEABLE


def redis_db_index(url) -> int:
    try:
        return _parse_redis_url(url)[2]
    except Exception:  # noqa: BLE001
        return 0


DOCKER_RUN = "docker run -d --name redis-stack -p 127.0.0.1:6379:6379 redis/redis-stack-server:latest"


def backend_hint(url) -> str:
    return (f"Redis Stack is not reachable at {_hostport(url)}. If Docker itself is not running, start "
            f"Docker (Docker Desktop) first. Then run: docker start redis-stack (first time: {DOCKER_RUN}). "
            "The memory server reconnects automatically — no restart needed; just retry.")


def index_hint(url, reason) -> str:
    return (f"The Redis at {_hostport(url)} does not provide RediSearch (FT.CREATE failed: {reason}). "
            f"N3MC Lite needs Redis Stack, not plain Redis: {DOCKER_RUN}")


# ── code_version (spec §3.15) ────────────────────────────────────────────────

def read_config_raw():
    """The bytes of config.json now, or None when there is no file."""
    try:
        return paths.config_path().read_bytes()
    except OSError:
        return None


def compute_code_version(config_raw) -> str:
    h = hashlib.sha256()
    pkg = Path(__file__).resolve().parent
    for name in CODE_FILES:
        try:
            h.update((pkg / name).read_bytes())
        except OSError:
            pass
    if config_raw is not None:
        try:
            obj = json.loads(config_raw.decode("utf-8-sig"))
            if isinstance(obj, dict):
                h.update(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8"))
            else:
                h.update(config_raw)
        except Exception:  # noqa: BLE001
            h.update(config_raw)
    return h.hexdigest()[:16]


# ── reading and writing config.json (spec §6) ────────────────────────────────

def _read_bytes_retry(path: Path) -> bytes:
    deadline = time.monotonic() + 1.0
    while True:
        try:
            return path.read_bytes()
        except PermissionError:
            if os.name != "nt" or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _replace_retry(src, dst) -> None:
    deadline = time.monotonic() + 1.0
    while True:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if os.name != "nt" or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _dump(obj) -> bytes:
    return (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _write_replace(path: Path, data: bytes) -> None:
    """Temp file in the same folder + os.replace: never a half-written config.json."""
    fd, tmp = tempfile.mkstemp(prefix=".config.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        _replace_retry(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _create_exclusive(path: Path, data: bytes) -> bool:
    """Create path with data only if it does not exist (temp file + os.link, else open 'x').
    True when this process created it, False when another process was first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
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


def _parse(raw: bytes):
    obj = json.loads(raw.decode("utf-8-sig"))
    return obj


class _ConfigLock:
    """config.json.lock via open(..., 'x'); 0.05 s retries for up to 10 s; a lock file older than
    10 s is stale and removed. Without the lock after 10 s the caller goes on without it."""

    def __init__(self, path: Path):
        self.lock = path.with_name(path.name + ".lock")
        self.held = False

    def __enter__(self):
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fd = os.open(str(self.lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                self.held = True
                return self
            except FileExistsError:
                try:
                    if time.time() - self.lock.stat().st_mtime > LOCK_STALE:
                        os.unlink(self.lock)
                        continue
                except OSError:
                    pass
            except OSError:
                return self
            if time.monotonic() >= deadline:
                return self
            time.sleep(LOCK_RETRY)

    def __exit__(self, *exc):
        if self.held:
            try:
                os.unlink(self.lock)
            except OSError:
                pass
        return False


def _new_file_object() -> dict:
    obj = {"owner_id": str(uuid.uuid4()), "local_id": str(uuid.uuid4())}
    obj.update(DEFAULTS)
    return obj


def _migrate_legacy(path: Path, notices: list) -> None:
    """One-time copy from the platformdirs location (spec §7), byte for byte, exclusively."""
    if os.environ.get("N3MC_DATA_DIR", "").strip():
        return
    try:
        legacy = paths.legacy_config_path()
    except Exception:  # noqa: BLE001
        return
    try:
        if not legacy.is_file() or paths.same_folder(legacy, path):
            return
        data = legacy.read_bytes()
    except OSError:
        return
    try:
        if _create_exclusive(path, data):
            sys.stderr.write(f"[n3mc-lite] config.json was copied from {legacy} to {path} (the old file is kept).\n")
    except OSError as e:
        sys.stderr.write(f"[n3mc-lite] could not copy {legacy} to {path}: {e}\n")


def load_config(quiet: bool = False) -> dict:
    """The configuration with defaults; never overwrites a file the user broke (spec §6).

    Extra keys: _notices (list of "key: message"), _path, _unreadable, _config_raw (the bytes this
    run is based on, for code_version)."""
    notices = []
    path = paths.config_path()
    cfg = dict(DEFAULTS)
    cfg["owner_id"] = None
    cfg["local_id"] = None
    unreadable = False
    raw = None
    obj = None

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    if not path.exists():
        _migrate_legacy(path, notices)
    if not path.exists():
        try:
            fresh = _dump(_new_file_object())
            if _create_exclusive(path, fresh):
                raw = fresh
        except OSError as e:
            unreadable = True
            notices.append(f"config_unreadable: {path} could not be created ({type(e).__name__}: {e}); "
                           "the defaults and a temporary owner_id are used for this run.")
    if not unreadable:
        reason = None
        deadline = time.monotonic() + CREATE_READ_WAIT
        while True:
            try:
                if raw is None:
                    raw = _read_bytes_retry(path)
                obj = _parse(raw)
                if not isinstance(obj, dict):
                    reason = "the top level is not a JSON object"
                    obj = None
                break
            except FileNotFoundError as e:
                reason = f"{type(e).__name__}: {e}"
                raw = None
            except (OSError, ValueError, UnicodeDecodeError) as e:
                reason = f"{type(e).__name__}: {e}"
            if time.monotonic() >= deadline:
                break
            raw = None
            time.sleep(0.1)
        if obj is None:
            unreadable = True
            notices.append(f"config_unreadable: {path} could not be read ({reason}). The file was not changed; "
                           "the defaults and a temporary owner_id are used until it is fixed. Fix the file; "
                           "the memory server restarts on the next call after config.json changes.")

    if obj is not None:
        missing = [k for k in list(DEFAULTS) + list(ID_KEYS) if k not in obj]
        if [k for k in ID_KEYS if not (isinstance(obj.get(k), str) and obj.get(k))]:
            missing = sorted(set(missing) | {k for k in ID_KEYS
                                             if not (isinstance(obj.get(k), str) and obj.get(k))})
        if missing:
            try:
                with _ConfigLock(path):
                    raw2 = _read_bytes_retry(path)
                    obj2 = _parse(raw2)
                    if isinstance(obj2, dict):
                        obj, raw = obj2, raw2
                        changed = False
                        for k in ID_KEYS:
                            if not (isinstance(obj.get(k), str) and obj.get(k)):
                                obj[k] = str(uuid.uuid4())
                                changed = True
                        for k, v in DEFAULTS.items():
                            if k not in obj:
                                obj[k] = v
                                changed = True
                        if changed:
                            new_raw = _dump(obj)
                            _write_replace(path, new_raw)
                            raw = new_raw
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"[n3mc-lite] config.json could not be updated ({type(e).__name__}: {e}); "
                                 "the missing settings use their defaults for this run.\n")

        bad = []
        for k in ID_KEYS:
            if isinstance(obj.get(k), str) and obj.get(k):
                cfg[k] = obj[k]
        for k in DEFAULTS:
            if k not in obj or k == "chunk_overlap":
                continue
            if config_value_ok(k, obj[k]):
                cfg[k] = obj[k]
            else:
                bad.append(k)
        if "chunk_overlap" in obj:
            if config_value_ok("chunk_overlap", obj["chunk_overlap"], cfg["chunk_threshold"]):
                cfg["chunk_overlap"] = obj["chunk_overlap"]
            else:
                bad.append("chunk_overlap")
        if not (cfg["chunk_overlap"] < cfg["chunk_threshold"]):
            cfg["chunk_overlap"] = cfg["chunk_threshold"] // 4
        if bad:
            notices.append(f"config_type: {path}: these settings have the wrong type or are out of range and "
                           f"were ignored (their defaults are used): {', '.join(bad)}. Fix them in config.json.")

    for k in ID_KEYS:
        if not cfg.get(k):
            cfg[k] = str(uuid.uuid4())

    env_url = os.environ.get("N3MC_REDIS_URL", "").strip()
    if env_url:
        cfg["redis_url"] = env_url

    cfg["_notices"] = notices
    cfg["_path"] = str(path)
    cfg["_unreadable"] = unreadable
    cfg["_config_raw"] = raw
    if not quiet:
        for n in notices:
            try:
                sys.stderr.write(f"[n3mc-lite] {n}\n")
            except Exception:  # noqa: BLE001
                pass
    return cfg


def config_unreadable_detail(cfg) -> str:
    return ("config.json could not be read, so this entry was saved under a temporary owner_id and will not "
            f"be visible after the server restarts. Fix {cfg.get('_path') or paths.config_path()} and restart "
            "the MCP client.")
