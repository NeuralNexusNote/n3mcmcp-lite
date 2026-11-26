"""The data folder (~/.n3mc-workingmemory, or N3MC_DATA_DIR) and its files (spec §7), the legacy
platformdirs location for the one-time migration, python_cmd() (spec §3.15 "Safe import path"),
and the notices.json / audit.log helpers behind _notify (spec §3.15 "Notices").

Every path is computed from the environment on each call, so a test that points N3MC_DATA_DIR
(and HOME / USERPROFILE) at a temporary folder moves all of them at once.
"""
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR_NAME = ".n3mc-workingmemory"
APP_NAME = "n3mc-workingmemory"
PID_FILE_NAME = "n3mc-lite.pid"
NOTICE_INTERVAL = 86400  # each notices.json entry is shown at most once a day


def data_dir() -> Path:
    env = os.environ.get("N3MC_DATA_DIR", "").strip()
    if env:
        return Path(os.path.abspath(os.path.expanduser(env)))
    return Path(os.path.expanduser("~")) / DATA_DIR_NAME


def ensure_data_dir() -> Path:
    d = data_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def config_path() -> Path:
    return data_dir() / "config.json"


def pid_path() -> Path:
    return data_dir() / PID_FILE_NAME


def server_log_path() -> Path:
    return data_dir() / "server.log"


def start_failed_path() -> Path:
    return data_dir() / "server_start_failed"


def notices_path() -> Path:
    return data_dir() / "notices.json"


def audit_log_path() -> Path:
    return data_dir() / "audit.log"


def session_file_part(session_id) -> str:
    """The <session> part of turn_id_<session>.txt: the hook's session_id with every character
    outside [a-zA-Z0-9_-] replaced by '_', cut to 64 characters (spec §12.4)."""
    if not isinstance(session_id, str) or not session_id:
        return ""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", session_id)[:64]


def turn_id_path(session_id) -> Path:
    part = session_file_part(session_id)
    return data_dir() / (f"turn_id_{part}.txt" if part else "turn_id.txt")


def legacy_config_path() -> Path:
    """The pre-1.7 location (platformdirs user data dir), read once for the migration (spec §7)."""
    import platformdirs
    return Path(platformdirs.user_data_dir(APP_NAME)) / "config.json"


def same_folder(a, b) -> bool:
    try:
        return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))
    except Exception:  # noqa: BLE001
        return False


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


def source_checkout_in_place() -> bool:
    """True when the parent folder of this package is the current folder (spec §3.15)."""
    try:
        cwd = os.path.normcase(os.path.abspath(os.getcwd()))
        pkg_parent = os.path.normcase(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return pkg_parent == cwd
    except Exception:  # noqa: BLE001
        return False


# ── audit.log ────────────────────────────────────────────────────────────────

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strip_surrogates(value):
    if isinstance(value, str):
        return re.sub(r"[\ud800-\udfff]", "", value)
    if isinstance(value, list):
        return [_strip_surrogates(v) for v in value]
    if isinstance(value, dict):
        return {k: _strip_surrogates(v) for k, v in value.items()}
    return value


def audit_append(record: dict) -> bool:
    """Append one JSON line to audit.log. Never raises; a failure is one stderr line (spec §12.2)."""
    try:
        ensure_data_dir()
        line = json.dumps(_strip_surrogates(record), ensure_ascii=False)
        with open(audit_log_path(), "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
        return True
    except Exception as e:  # noqa: BLE001
        try:
            sys.stderr.write(f"[n3mc-lite] could not write audit.log: {type(e).__name__}: {e}\n")
        except Exception:  # noqa: BLE001
            pass
        return False


# ── notices.json (spec §3.15 "Notices") ──────────────────────────────────────

def _read_notices() -> dict:
    try:
        with open(notices_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_notices(data: dict) -> None:
    d = ensure_data_dir()
    fd, tmp = tempfile.mkstemp(prefix=".notices.", suffix=".tmp", dir=str(d))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, notices_path())
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def notify(key: str, message: str) -> None:
    """stderr, audit.log (once per key and message), notices.json (shown once a day)."""
    try:
        sys.stderr.write(f"[n3mc-lite] {message}\n")
    except Exception:  # noqa: BLE001
        pass
    try:
        data = _read_notices()
        entry = data.get(key)
        if isinstance(entry, dict) and entry.get("message") == message:
            return
        audit_append({"ts": utc_now_iso(), "hook": "notice", "key": key, "message": message})
        data[key] = {"message": message, "last_shown": None}
        _write_notices(data)
    except Exception as e:  # noqa: BLE001
        try:
            sys.stderr.write(f"[n3mc-lite] could not record the notice {key}: {type(e).__name__}: {e}\n")
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
    """The messages whose time has come (last_shown null, unreadable or a day old); marks them shown."""
    now = time.time() if now is None else now
    data = _read_notices()
    out = []
    changed = False
    for key, entry in data.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("message"), str):
            continue
        last = entry.get("last_shown")
        if isinstance(last, bool) or not isinstance(last, (int, float)):
            last = None
        if last is None or now - last >= NOTICE_INTERVAL:
            out.append(entry["message"])
            entry["last_shown"] = now
            changed = True
    if changed:
        _write_notices(data)
    return out
