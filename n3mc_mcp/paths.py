"""Data folder and its files (spec section 7), plus python_cmd() (section 3.15, safe import path)."""

import os
import re


def data_dir() -> str:
    """N3MC_DATA_DIR (absolute path) or ~/.n3mc-workingmemory. Read on every call."""
    env = os.environ.get("N3MC_DATA_DIR")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(os.path.expanduser("~"), ".n3mc-workingmemory")


def _p(name: str) -> str:
    return os.path.join(data_dir(), name)


def config_path() -> str:
    return _p("config.json")


def pid_path() -> str:
    return _p("n3mc-lite.pid")


def server_log_path() -> str:
    return _p("server.log")


def start_failed_path() -> str:
    return _p("server_start_failed")


def notices_path() -> str:
    return _p("notices.json")


def audit_log_path() -> str:
    return _p("audit.log")


def turn_id_path(session_id=None) -> str:
    """turn_id_<session>.txt; the session id is sanitized (it comes from hook JSON)."""
    if session_id:
        safe = re.sub(r"[^a-zA-Z0-9_-]", "_", str(session_id))[:64]
        if safe:
            return _p("turn_id_%s.txt" % safe)
    return _p("turn_id.txt")


def legacy_config_path() -> str:
    """The v1.6.x location (platformdirs), used for the one-time migration."""
    import platformdirs
    return os.path.join(platformdirs.user_data_dir("n3mc-workingmemory"), "config.json")


def remove_pid_file_if_own(pid=None) -> bool:
    """Delete the PID file when it holds `pid` (default: this process). The file is closed before the
    unlink: Windows refuses to delete a file that is still open."""
    pid = os.getpid() if pid is None else pid
    try:
        with open(pid_path(), "r", encoding="utf-8") as f:
            txt = f.read().strip()
        if txt == str(pid):
            os.unlink(pid_path())
            return True
    except OSError:
        pass
    return False


def ensure_data_dir() -> None:
    os.makedirs(data_dir(), exist_ok=True)


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
