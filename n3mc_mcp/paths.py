"""The data folder (~/.n3mc-workingmemory/, or N3MC_DATA_DIR) and its files (spec §7), the
legacy platformdirs location for the one-time migration, the child-Python command (§3.15
"Safe import path"), and the small file helpers shared by the hooks, the CLI and the server:
audit.log (§12.2) and notices.json (§3.15 "Notices").

Every path is computed from the environment on each call, so pointing N3MC_DATA_DIR at another
folder (the tests do) moves all of them at once."""
import datetime
import json
import os
import re
import sys
import tempfile
import time

APP_NAME = "n3mc-workingmemory"
PID_NAME = "n3mc-lite.pid"
AUDIT_ROTATE_BYTES = 50 * 1024 * 1024
NOTICE_INTERVAL = 86400


def data_dir() -> str:
    env = os.environ.get("N3MC_DATA_DIR")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(os.path.expanduser("~"), ".n3mc-workingmemory")


def ensure_data_dir() -> str:
    d = data_dir()
    os.makedirs(d, exist_ok=True)
    return d


def config_path() -> str:
    return os.path.join(data_dir(), "config.json")


def pid_path() -> str:
    return os.path.join(data_dir(), PID_NAME)


def server_log_path() -> str:
    return os.path.join(data_dir(), "server.log")


def start_failed_path() -> str:
    return os.path.join(data_dir(), "server_start_failed")


def notices_path() -> str:
    return os.path.join(data_dir(), "notices.json")


def audit_log_path() -> str:
    return os.path.join(data_dir(), "audit.log")


def safe_session_name(session_id) -> str:
    if not isinstance(session_id, str) or not session_id:
        return ""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", session_id)[:64]


def turn_id_path(session_id) -> str:
    name = safe_session_name(session_id)
    return os.path.join(data_dir(), f"turn_id_{name}.txt" if name else "turn_id.txt")


def legacy_config_path():
    """The pre-1.7 location: platformdirs.user_data_dir("n3mc-workingmemory") + config.json."""
    try:
        import platformdirs
        return os.path.join(platformdirs.user_data_dir(APP_NAME), "config.json")
    except Exception:  # noqa: BLE001
        return None


def python_cmd():
    """The command that starts a child Python: [sys.executable], plus -P on Python 3.11 and later so
    that the child does not put the current folder on sys.path (see the safe-path block at the top of
    the entry modules). -P is left out when this package itself lives in the current folder (a source
    checkout run in place), because the child could then not find the package."""
    import sys
    cmd = [sys.executable]
    try:
        cwd = os.path.normcase(os.path.abspath(os.getcwd()))
        pkg_parent = os.path.normcase(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if sys.version_info >= (3, 11) and pkg_parent != cwd:
            cmd.append("-P")
    except Exception:  # noqa: BLE001
        pass
    return cmd


def package_parent_is_cwd() -> bool:
    try:
        cwd = os.path.normcase(os.path.abspath(os.getcwd()))
        pkg_parent = os.path.normcase(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return pkg_parent == cwd
    except Exception:  # noqa: BLE001
        return False


def utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def atomic_write_text(path: str, text: str) -> None:
    """Write through a temporary file in the same folder and os.replace (§6, §12.9)."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        replace_with_retry(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def replace_with_retry(src: str, dst: str, total: float = 1.0) -> None:
    """os.replace, retried on PermissionError for up to `total` seconds (Windows, §6)."""
    deadline = time.monotonic() + total
    while True:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if os.name != "nt" or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


# ── audit.log (§12.2) ─────────────────────────────────────────────────────────

def append_audit(record: dict) -> None:
    """Append one JSON line to audit.log. A failure is one stderr line, never an exception."""
    try:
        ensure_data_dir()
        line = json.dumps(record, ensure_ascii=False)
        with open(audit_log_path(), "a", encoding="utf-8", errors="replace") as f:
            f.write(line + "\n")
    except Exception as e:  # noqa: BLE001
        try:
            print(f"n3mc-lite: could not write audit.log ({type(e).__name__}: {e})", file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass


def rotate_audit_log(limit: int = None) -> None:
    limit = AUDIT_ROTATE_BYTES if limit is None else limit
    path = audit_log_path()
    if os.path.isfile(path) and os.path.getsize(path) > limit:
        replace_with_retry(path, path + ".1")


# ── notices.json (§3.15 "Notices") ────────────────────────────────────────────

def _read_notices() -> dict:
    try:
        with open(notices_path(), "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_notices(data: dict) -> None:
    atomic_write_text(notices_path(), json.dumps(data, ensure_ascii=False, indent=1))


def notify(key: str, message: str) -> None:
    """Free's _notify: stderr, audit.log (once per key and message), notices.json."""
    try:
        print(f"N3MC notice: {message}", file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass
    try:
        data = _read_notices()
        entry = data.get(key)
        same = isinstance(entry, dict) and entry.get("message") == message
        if not same:
            append_audit({"ts": utc_now_iso(), "hook": "notice", "key": key, "message": message})
            data[key] = {"message": message, "last_shown": None}
            _write_notices(data)
    except Exception as e:  # noqa: BLE001
        try:
            print(f"n3mc-lite: could not record notice {key} ({type(e).__name__}: {e})", file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass


def clear_notice(key: str) -> None:
    try:
        data = _read_notices()
        if key in data:
            del data[key]
            _write_notices(data)
    except Exception:  # noqa: BLE001
        pass


def has_notice(key: str) -> bool:
    return key in _read_notices()


def take_due_notices(now: float = None) -> list:
    """The messages whose time has come (once a day per key); marks them as shown."""
    now = time.time() if now is None else now
    data = _read_notices()
    due = []
    changed = False
    for key, entry in list(data.items()):
        if not isinstance(entry, dict) or not isinstance(entry.get("message"), str):
            continue
        last = entry.get("last_shown")
        if isinstance(last, bool) or not isinstance(last, (int, float)):
            last = None
        if last is None or now - last >= NOTICE_INTERVAL:
            due.append(entry["message"])
            entry["last_shown"] = now
            changed = True
    if changed:
        try:
            _write_notices(data)
        except OSError:
            pass
    return due
