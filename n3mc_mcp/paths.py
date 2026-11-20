"""Data folder (~/.n3mc-workingmemory/) and its files (spec §7).

Every path is computed when it is asked for, from the environment of that
moment, so tests that point N3MC_DATA_DIR at a temporary folder move all of
them at once.
"""

import os
import pathlib
import re

APP_NAME = "n3mc-workingmemory"


def data_dir() -> pathlib.Path:
    env = os.environ.get("N3MC_DATA_DIR")
    if env:
        return pathlib.Path(os.path.abspath(os.path.expanduser(env)))
    return pathlib.Path(os.path.expanduser("~")) / ".n3mc-workingmemory"


def config_path() -> pathlib.Path:
    return data_dir() / "config.json"


def pid_path() -> pathlib.Path:
    return data_dir() / "n3mc-lite.pid"


def server_log_path() -> pathlib.Path:
    return data_dir() / "server.log"


def start_failed_path() -> pathlib.Path:
    return data_dir() / "server_start_failed"


def notices_path() -> pathlib.Path:
    return data_dir() / "notices.json"


def audit_log_path() -> pathlib.Path:
    return data_dir() / "audit.log"


def safe_session(session_id) -> str:
    if not session_id or not isinstance(session_id, str):
        return ""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", session_id)[:64]


def turn_id_path(session_id) -> pathlib.Path:
    s = safe_session(session_id)
    return data_dir() / (f"turn_id_{s}.txt" if s else "turn_id.txt")


def legacy_config_path() -> pathlib.Path:
    """The v1.6.x location (platformdirs user data dir), used once for migration."""
    import platformdirs
    return pathlib.Path(platformdirs.user_data_dir(APP_NAME)) / "config.json"


def same_dir(a, b) -> bool:
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


# ------------------------------------------------------------ audit.log (§12.2)

def utc_now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def append_audit(record: dict) -> None:
    """Append one JSON line to audit.log. Never raises (errors go to stderr)."""
    import json
    import sys
    try:
        p = audit_log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with open(p, "a", encoding="utf-8", errors="replace") as f:
            f.write(line + "\n")
    except Exception as e:  # noqa: BLE001
        try:
            print(f"N3MC Lite: could not write audit.log: {e}", file=sys.stderr, flush=True)
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------ notices.json (§3.15 _notify)

NOTICE_INTERVAL = 86400


def _write_atomic(path: pathlib.Path, data: bytes) -> None:
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".n3mc-", suffix=".tmp", dir=str(path.parent))
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    try:
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def load_notices() -> dict:
    import json
    try:
        d = json.loads(notices_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def save_notices(d: dict) -> None:
    import json
    _write_atomic(notices_path(), (json.dumps(d, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))


def store_notice(key: str, message: str) -> bool:
    """Keep a notice in notices.json. True when it is new (or its message changed)."""
    d = load_notices()
    old = d.get(key)
    if isinstance(old, dict) and old.get("message") == message:
        return False
    d[key] = {"message": message, "last_shown": None}
    try:
        save_notices(d)
    except OSError:
        pass
    return True


def clear_notice(key: str) -> None:
    d = load_notices()
    if key in d:
        del d[key]
        try:
            save_notices(d)
        except OSError:
            pass


def take_due_notices(now=None) -> list:
    """Messages whose time has come (once a day per key); marks them shown."""
    import time
    now = time.time() if now is None else now
    d = load_notices()
    out = []
    changed = False
    for key, v in d.items():
        if not isinstance(v, dict):
            continue
        last = v.get("last_shown")
        if last is None or now - float(last) >= NOTICE_INTERVAL:
            out.append(str(v.get("message", "")))
            v["last_shown"] = now
            changed = True
    if changed:
        try:
            save_notices(d)
        except OSError:
            pass
    return out
