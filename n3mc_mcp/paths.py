"""The data folder and its files (spec §7), the legacy config location, the child Python command.

Every path is computed when it is asked for, from ``N3MC_DATA_DIR`` (or ``~/.n3mc-workingmemory``),
so that a test that points ``N3MC_DATA_DIR`` at a temporary folder moves all of them at once.
"""
import datetime
import json
import os
import re

DATA_DIR_NAME = ".n3mc-workingmemory"
APP_NAME = "n3mc-workingmemory"

CONFIG_NAME = "config.json"
PID_NAME = "n3mc-lite.pid"
SERVER_LOG_NAME = "server.log"
START_FAILED_NAME = "server_start_failed"
NOTICES_NAME = "notices.json"
AUDIT_NAME = "audit.log"


def data_dir() -> str:
    """The data folder: N3MC_DATA_DIR (made absolute) or ~/.n3mc-workingmemory."""
    env = os.environ.get("N3MC_DATA_DIR", "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(os.path.expanduser("~"), DATA_DIR_NAME)


def data_dir_norm() -> str:
    return os.path.normcase(os.path.abspath(data_dir()))


def ensure_data_dir() -> str:
    d = data_dir()
    os.makedirs(d, exist_ok=True)
    return d


def config_path() -> str:
    return os.path.join(data_dir(), CONFIG_NAME)


def pid_path() -> str:
    return os.path.join(data_dir(), PID_NAME)


def server_log_path() -> str:
    return os.path.join(data_dir(), SERVER_LOG_NAME)


def start_failed_path() -> str:
    return os.path.join(data_dir(), START_FAILED_NAME)


def notices_path() -> str:
    return os.path.join(data_dir(), NOTICES_NAME)


def audit_path() -> str:
    return os.path.join(data_dir(), AUDIT_NAME)


def audit_rotated_path() -> str:
    return os.path.join(data_dir(), AUDIT_NAME + ".1")


_SESSION_SAFE_RE = re.compile(r"[^a-zA-Z0-9_-]")


def turn_id_path(session_id) -> str:
    """turn_id_<session>.txt: the session id with every character outside [a-zA-Z0-9_-] replaced by
    "_", cut to 64 characters; turn_id.txt when there is no session (spec §12.4)."""
    if isinstance(session_id, str) and session_id:
        safe = _SESSION_SAFE_RE.sub("_", session_id)[:64]
        return os.path.join(data_dir(), f"turn_id_{safe}.txt")
    return os.path.join(data_dir(), "turn_id.txt")


def legacy_config_path() -> str:
    """Where 1.6.x and earlier kept config.json (platformdirs user data folder)."""
    try:
        import platformdirs
        return os.path.join(platformdirs.user_data_dir(APP_NAME), CONFIG_NAME)
    except Exception:  # noqa: BLE001
        return ""


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


def package_parent() -> str:
    return os.path.normcase(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def append_audit(record: dict) -> bool:
    """Append one JSON line to audit.log. Never raises; a failure is one stderr line."""
    try:
        ensure_data_dir()
        rec = dict(record)
        rec.setdefault("ts", utc_now_iso())
        line = json.dumps(rec, ensure_ascii=False)
        with open(audit_path(), "a", encoding="utf-8", errors="surrogatepass") as f:
            f.write(line + "\n")
        return True
    except Exception as e:  # noqa: BLE001
        try:
            import sys
            print(f"[n3mc-lite] could not write audit.log: {type(e).__name__}: {e}", file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass
        return False
