"""CLI `n3mc-lite` (spec §13): the tool commands through the resident server, the hook steps
(--hook-submit, --save-claude-turn, --stop; spec §12), --init (spec §12.8), --run-server and
--stop-server; ensure_server, _start_server and _notify (spec §3.15).
"""
import os as _os
import sys as _sys

if __name__ == "__main__":
    # Safe import path. Started as `python -m <package>.<module>`, Python has put the current folder
    # first on sys.path, so a file there named like a module imported below would run inside this
    # program. Drop that entry, unless this package itself lives in the current folder (a source
    # checkout run in place). Nothing else may be imported before this block.
    try:
        _cwd = _os.path.normcase(_os.path.abspath(_os.getcwd()))
        _pkg_parent = _os.path.normcase(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
        if _sys.path and _os.path.normcase(_os.path.abspath(_sys.path[0])) == _cwd and _pkg_parent != _cwd:
            del _sys.path[0]
    except Exception:  # noqa: BLE001
        pass

import sys

for _stream_name in ("stdin", "stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

import argparse  # noqa: E402
import copy  # noqa: E402
import hashlib  # noqa: E402
import http.client  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import pathlib  # noqa: E402
import re  # noqa: E402
import shlex  # noqa: E402
import shutil  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import sysconfig  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402
import uuid  # noqa: E402
from datetime import datetime  # noqa: E402
from pathlib import Path  # noqa: E402

import n3mc_mcp  # noqa: E402
from . import __version__, context, paths  # noqa: E402
from .config import (backend_hint, compute_code_version, load_config, read_config_raw,  # noqa: E402
                     redis_display)
from .processor import parse_time_bounds, sanitize_surrogates  # noqa: E402

# ── constants ────────────────────────────────────────────────────────────────

START_WAIT = 60
HEALTH_TIMEOUT = 5
SHUTDOWN_TIMEOUT = 5
SHORT_TIMEOUT = 30          # /buffer /search /repair
TOOL_TIMEOUT = 300          # /tools/<name>
HOOK_REPAIR_TIMEOUT = 10
PORT_FREE_WAIT = 15
START_FAIL_WINDOW = 300
STARTING_GRACE = 60
SERVER_LOG_MAX = 1024 * 1024
AUDIT_ROTATE_BYTES = 50 * 1024 * 1024
TURN_FILE_MAX_AGE = 30 * 86400
CHILD_TIMEOUT = 55

PLUGIN_KEY = "n3mc-workingmemory@neuralnexusnote"
MCP_NAME = "n3mc-workingmemory"
_SYSTEM_PROMPT_PREFIXES = ("<task-notification>", "<agent-message from=", "<system-reminder>")
EMPTY_BLOCK = f"{context.HEADER_LINE}\n{context.SOURCE_LINE}\n\n{context.NO_MEMORIES}"

STILL_STARTING = "the memory server is still starting (the first start downloads the embedding model, ~1.1 GB)"
OLD_SERVER = "an old N3MC Lite memory server could not be stopped"

_FOREIGN_SERVER_ROOT = None
_UNAVAILABLE_REASON = None


def _could_not_start() -> str:
    return f"the memory server could not start; see {paths.server_log_path()}"


def _foreign_reason(port) -> str:
    return f"another N3MC folder's server is using port {port}"


def last_unavailable_reason() -> str:
    return _UNAVAILABLE_REASON or STILL_STARTING


def _err(msg: str) -> None:
    try:
        sys.stderr.write(f"[n3mc-lite] {msg}\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass


def _out(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


# ── notices (spec §3.15) ─────────────────────────────────────────────────────

def _notify(key: str, message: str) -> None:
    paths.notify(key, message)


def _clear_notice(key: str) -> None:
    paths.clear_notice(key)


def _redis_unreachable_message(cfg, hint=None) -> str:
    return (f"N3MC Lite could not reach Redis, so a message was kept only in {paths.audit_log_path()}. "
            + (hint or backend_hint(cfg["redis_url"])))


# ── HTTP to the resident server (spec §3.15 "Transport") ────────────────────

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class _NotSent(Exception):
    """The request did not reach a server (connection refused and the like)."""


class _NoAnswer(Exception):
    """The request was sent but no answer came back (timeout, connection closed)."""


def _request(method, port, path, body=None, timeout=HEALTH_TIMEOUT):
    url = f"http://127.0.0.1:{int(port)}{path}"
    if _port_free(port):
        # Nothing listens there. Windows takes about 2 s to refuse a connection to a closed local port.
        raise _NotSent(f"ConnectionRefusedError: nothing listens on 127.0.0.1:{int(port)}")
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Host": f"127.0.0.1:{int(port)}",
                                          "Content-Type": "application/json"})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            raw = e.read()
        except Exception:  # noqa: BLE001
            raw = b""
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise _NoAnswer(f"TimeoutError: {reason}")
        if isinstance(reason, (ConnectionResetError, ConnectionAbortedError)):
            raise _NoAnswer(f"{type(reason).__name__}: {reason}")
        raise _NotSent(f"{type(reason).__name__}: {reason}")
    except (socket.timeout, TimeoutError) as e:
        raise _NoAnswer(f"TimeoutError: {e}")
    except (ConnectionResetError, ConnectionAbortedError, http.client.HTTPException, OSError) as e:
        raise _NoAnswer(f"{type(e).__name__}: {e}")
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:  # noqa: BLE001
        parsed = {}
    return status, (parsed if isinstance(parsed, dict) else {})


def _get(port, path, timeout=HEALTH_TIMEOUT):
    return _request("GET", port, path, None, timeout)


def _post(port, path, body, timeout=SHORT_TIMEOUT):
    return _request("POST", port, path, body, timeout)


def _request_shutdown(port, root):
    return _post(port, "/shutdown", {"root": str(root)}, SHUTDOWN_TIMEOUT)


def _health(port):
    try:
        status, data = _get(port, "/health", HEALTH_TIMEOUT)
    except Exception:  # noqa: BLE001
        return None
    if status != 200 or data.get("status") != "ok":
        return None
    return data


# ── server identity and replacement (spec §3.15) ────────────────────────────

def _health_decision(health, root=None, code_version=None) -> str:
    if not health:
        return "none"
    root = str(paths.data_dir()) if root is None else root
    hroot = health.get("root")
    if not hroot:
        return "legacy"
    if not paths.same_folder(hroot, root):
        return "foreign"
    if code_version is None:
        code_version = compute_code_version(read_config_raw())
    return "ok" if health.get("code_version") == code_version else "stale"


def _process_data_dir(env) -> str:
    d = (env or {}).get("N3MC_DATA_DIR", "").strip()
    if d:
        return os.path.abspath(os.path.expanduser(d))
    home = (env or {}).get("USERPROFILE") if os.name == "nt" else None
    home = home or (env or {}).get("HOME") or os.path.expanduser("~")
    return os.path.join(home, paths.DATA_DIR_NAME)


def _is_own_server_process(pid) -> bool:
    try:
        import psutil
        proc = psutil.Process(int(pid))
        cmd = proc.cmdline()
        if "n3mc_mcp.cli" not in cmd or "--run-server" not in cmd:
            return False
        return paths.same_folder(_process_data_dir(proc.environ()), paths.data_dir())
    except Exception:  # noqa: BLE001
        return False


def _port_free(port) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _wait_port_free(port, timeout=PORT_FREE_WAIT) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if _port_free(port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.3)


def _terminate(pid) -> None:
    import psutil
    proc = psutil.Process(int(pid))
    proc.terminate()
    try:
        proc.wait(5)
    except Exception:  # noqa: BLE001
        proc.kill()


def _stop_stale_server(port, root, pid=None) -> bool:
    """Ask our old server to stop (POST /shutdown); only a server that does not know /shutdown is
    ended, after psutil confirmed it is ours. Never by the number in the PID file."""
    try:
        status, _data = _request_shutdown(port, root)
    except Exception:  # noqa: BLE001
        status = None
    if status == 200:
        return _wait_port_free(port, PORT_FREE_WAIT)
    if pid is not None and _is_own_server_process(pid):
        try:
            _terminate(pid)
        except Exception as e:  # noqa: BLE001
            _err(f"could not end the old server (pid {pid}): {e}")
        return _wait_port_free(port, PORT_FREE_WAIT)
    _notify("stale_server", f"Cannot stop the old N3MC Lite server. Another program may be using server_port {port}. "
                            "Check server_port in config.json and restart manually.")
    return False


def _listening_ports(pid) -> set:
    try:
        import psutil
        proc = psutil.Process(int(pid))
        conns = proc.net_connections(kind="inet") if hasattr(proc, "net_connections") else proc.connections(kind="inet")
        return {c.laddr.port for c in conns if c.status == psutil.CONN_LISTEN and c.laddr}
    except Exception:  # noqa: BLE001
        return set()


def _handle_other_port(pid, cfg) -> bool:
    """The PID file's server listens on a port other than server_port (the user changed it): stop it
    there when that port's /health reports this data folder (spec §3.15)."""
    root = str(paths.data_dir())
    for p in sorted(_listening_ports(pid) - {int(cfg["server_port"])}):
        h = _health(p)
        if not h or not h.get("root") or not paths.same_folder(h["root"], root):
            continue
        try:
            _request_shutdown(p, root)
        except Exception:  # noqa: BLE001
            pass
        _wait_port_free(p, PORT_FREE_WAIT)
        if _read_pid() == pid:
            _remove_pid_file()
        return True
    return False


def _read_pid():
    try:
        text = paths.pid_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(text)
    except ValueError:
        return ""


def _remove_pid_file() -> None:
    try:
        os.unlink(paths.pid_path())
    except OSError:
        pass


def _pid_file_young() -> bool:
    try:
        return time.time() - paths.pid_path().stat().st_mtime < STARTING_GRACE
    except OSError:
        return False


def _pid_state():
    """('none' | 'own' | 'starting' | 'dead', pid)."""
    if not paths.pid_path().exists():
        return "none", None
    pid = _read_pid()
    if pid is None:
        return "none", None
    if pid == "":
        return ("starting" if _pid_file_young() else "dead"), None
    try:
        import psutil
    except ImportError:
        return ("starting" if _pid_file_young() else "dead"), pid
    try:
        if not psutil.pid_exists(pid):
            return "dead", pid
    except Exception:  # noqa: BLE001
        return "dead", pid
    return ("own" if _is_own_server_process(pid) else "dead"), pid


def _write_own_pid() -> None:
    """The server writes its own PID (spec §13 "PID file"); OSError is ignored."""
    p = paths.pid_path()
    try:
        paths.ensure_data_dir()
        try:
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
        except FileExistsError:
            with open(p, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
    except OSError:
        pass


def remove_pid_file_if_own() -> None:
    try:
        if _read_pid() == os.getpid():
            _remove_pid_file()
    except Exception:  # noqa: BLE001
        pass


def _record_start_failure(detail) -> None:
    _notify("server_start_failed", f"The N3MC Lite memory server could not start ({detail}). "
                                   f"See {paths.server_log_path()} for the reason.")
    try:
        paths.ensure_data_dir()
        paths.start_failed_path().write_text(datetime.now().isoformat(), encoding="utf-8")
    except OSError:
        pass


def _recent_start_failure() -> bool:
    try:
        return time.time() - paths.start_failed_path().stat().st_mtime < START_FAIL_WINDOW
    except OSError:
        return False


def _on_ok(cfg, health) -> bool:
    for key in ("stale_server", "port_conflict", "server_start_failed"):
        _clear_notice(key)
    try:
        os.unlink(paths.start_failed_path())
    except OSError:
        pass
    own = redis_display(cfg["redis_url"])
    theirs = health.get("redis")
    if theirs is not None and theirs != own:
        _notify("redis_url_mismatch",
                f"The N3MC Lite server uses Redis at {theirs}, but this process is set to {own}. Put redis_url "
                f"into {paths.config_path()} instead of setting N3MC_REDIS_URL per application, then restart "
                "the server (it restarts after config.json changes).")
    else:
        _clear_notice("redis_url_mismatch")
    return True


def _foreign(cfg, root) -> bool:
    global _FOREIGN_SERVER_ROOT, _UNAVAILABLE_REASON
    port = int(cfg["server_port"])
    _FOREIGN_SERVER_ROOT = root
    _UNAVAILABLE_REASON = _foreign_reason(port)
    _notify("port_conflict", f"Another N3MC server ({root}) is using port {port}. Give N3MC Lite its own "
                             f"server_port in {paths.config_path()}.")
    return False


def _wait_for_start(cfg, max_wait, proc=None) -> bool:
    global _UNAVAILABLE_REASON
    port = int(cfg["server_port"])
    deadline = time.monotonic() + max(0.0, float(max_wait))
    while True:
        if proc is not None:
            rc = proc.poll()
            if rc is not None:
                _record_start_failure(f"exit code {rc}")
                if _read_pid() == proc.pid:
                    _remove_pid_file()
                _UNAVAILABLE_REASON = _could_not_start()
                return False
        h = _health(port) if max_wait > 0 or proc is None else None
        dec = _health_decision(h)
        if dec == "ok":
            return _on_ok(cfg, h)
        if dec == "foreign":
            return _foreign(cfg, h.get("root"))
        if time.monotonic() >= deadline:
            _UNAVAILABLE_REASON = STILL_STARTING
            return False
        time.sleep(0.5)


def _popen(*args, **kwargs):
    return subprocess.Popen(*args, **kwargs)


def _start_server(cfg, max_wait=START_WAIT) -> bool:
    """Start the resident server detached (spec §3.15 "Starting the server")."""
    global _UNAVAILABLE_REASON
    d = paths.ensure_data_dir()
    pid_file = paths.pid_path()
    try:
        fd = os.open(str(pid_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        return _wait_for_start(cfg, max_wait)
    except OSError as e:
        _err(f"could not create the PID file {pid_file}: {e}")
    log_path = paths.server_log_path()
    try:
        if log_path.exists() and log_path.stat().st_size > SERVER_LOG_MAX:
            log_path.write_bytes(b"")
    except OSError:
        pass
    env = dict(os.environ)
    env["N3MC_DATA_DIR"] = str(d)
    cwd = os.getcwd() if paths.source_checkout_in_place() else str(d)
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    log = None
    try:
        log = open(log_path, "ab")
        proc = _popen([*paths.python_cmd(), "-m", "n3mc_mcp.cli", "--run-server"], stdin=subprocess.DEVNULL,
                      stdout=subprocess.DEVNULL, stderr=log, cwd=cwd, env=env, **kwargs)
    except Exception as e:  # noqa: BLE001
        _err(f"could not start the memory server: {type(e).__name__}: {e}")
        _remove_pid_file()
        _UNAVAILABLE_REASON = _could_not_start()
        return False
    finally:
        if log is not None:
            try:
                log.close()
            except OSError:
                pass
    try:
        with open(pid_file, "w", encoding="utf-8") as f:
            f.write(str(proc.pid))
    except OSError:
        pass
    return _wait_for_start(cfg, max_wait, proc)


def ensure_server(cfg, max_wait=START_WAIT) -> bool:
    """True when a healthy server of this data folder and code answers (started when needed)."""
    global _FOREIGN_SERVER_ROOT, _UNAVAILABLE_REASON
    _FOREIGN_SERVER_ROOT = None
    _UNAVAILABLE_REASON = None
    port = int(cfg["server_port"])
    root = str(paths.data_dir())
    h = _health(port)
    dec = _health_decision(h, root)
    if dec == "ok":
        return _on_ok(cfg, h)
    if dec == "foreign":
        return _foreign(cfg, h.get("root"))
    if dec == "legacy":
        if _is_own_server_process(h.get("pid")):
            dec = "stale"
        else:
            return _foreign(cfg, "(unknown: the server did not report its folder)")
    if dec == "stale":
        if _stop_stale_server(port, root, h.get("pid")):
            if _read_pid() == h.get("pid"):
                _remove_pid_file()
            return _start_or_fail(cfg, max_wait)
        else:
            h2 = _health(port)
            d2 = _health_decision(h2, root)
            if d2 == "ok":
                return _on_ok(cfg, h2)
            if d2 == "foreign":
                return _foreign(cfg, h2.get("root"))
            if d2 != "none":
                _UNAVAILABLE_REASON = OLD_SERVER
                return False
    state, pid = _pid_state()
    if state == "own":
        if _handle_other_port(pid, cfg):
            return _start_or_fail(cfg, max_wait)
        return _wait_for_start(cfg, max_wait)
    if state == "starting":
        return _wait_for_start(cfg, max_wait)
    if state == "dead":
        _remove_pid_file()
    return _start_or_fail(cfg, max_wait)


def _start_or_fail(cfg, max_wait) -> bool:
    global _UNAVAILABLE_REASON
    if _recent_start_failure():
        _UNAVAILABLE_REASON = _could_not_start()
        return False
    return _start_server(cfg, max_wait)


# ── --run-server / --stop-server (spec §13) ──────────────────────────────────

def run_server() -> int:
    try:
        _write_own_pid()
        cfg = load_config()
        code_version = compute_code_version(cfg.get("_config_raw"))
        port = int(cfg["server_port"])
        host = cfg.get("bind_host") or "127.0.0.1"
        if host != "127.0.0.1":
            _err(f"bind_host is {host}: the memory server has no authentication, so your memories are "
                 "reachable from the network. Keep bind_host at 127.0.0.1.")
        from . import processor
        processor.configure(cfg)
        processor.import_backend()
        from .database import BackendError, Database
        db = Database(cfg)
        try:
            db.ensure_connected()
        except BackendError as e:
            _err(str(e))
        from .app import create_app
        app = create_app(cfg, db, code_version=code_version, port=port, root=str(paths.data_dir()),
                         start_model_loader=True)
        _write_own_pid()
        import uvicorn
        uvicorn.run(app, host=host, port=port, log_level="error")
        return 0
    except SystemExit as e:
        if e.code not in (0, None):
            _record_start_failure(f"exit code {e.code}")
        raise
    except KeyboardInterrupt:
        return 0
    except BaseException as e:  # noqa: BLE001
        _record_start_failure(f"{type(e).__name__}: {e}")
        raise
    finally:
        remove_pid_file_if_own()


def stop_server(cfg) -> int:
    port = int(cfg["server_port"])
    root = str(paths.data_dir())
    h = _health(port)
    if not h or not h.get("root") or not paths.same_folder(h["root"], root):
        _out("No N3MC Lite server of this data folder is running.")
        return 0
    pid = h.get("pid")
    reason = None
    try:
        status, _d = _request_shutdown(port, root)
        if status != 200:
            reason = f"/shutdown answered HTTP {status}"
    except Exception as e:  # noqa: BLE001
        reason = f"{type(e).__name__}: {e}"
    if _wait_port_free(port, PORT_FREE_WAIT if reason is None else 0):
        _out(f"Stopped the N3MC Lite server (pid {pid}).")
        return 0
    reason = reason or f"port {port} is still in use after {PORT_FREE_WAIT} seconds"
    sys.stderr.write(f"Could not stop the N3MC Lite server (pid {pid}): {reason}. End the process whose "
                     "command line contains n3mc_mcp.cli and --run-server.\n")
    return 1


# ── the tools through the server, and without it (spec §4.6, §13) ───────────

def _nudges():
    from .tools import _NUDGE_AFTER_SAVE, _NUDGE_GENERIC
    return _NUDGE_GENERIC, _NUDGE_AFTER_SAVE


def direct_save_tool(cfg, args, reason, nudge=True) -> str:
    """save_memory without the server: straight to Redis without a vector (spec §3.16, §4.6)."""
    from .database import CONN_ERRORS, BackendError, Database
    from .tools import dumps
    generic, after_save = _nudges()
    g = generic if nudge else ""
    a = after_save if nudge else ""
    db = Database(cfg, enforce_on_connect=False)
    try:
        r = db.save_memory(args.get("content"), agent_name=args.get("agent_name"), owner_id=args.get("owner_id"),
                           importance=args.get("importance"), session_id=args.get("session_id"),
                           turn_id=args.get("turn_id"), embed=False)
    except (BackendError,) + CONN_ERRORS:
        return dumps({"status": "error", "saved": False, "reason": backend_hint(cfg["redis_url"])}) + g
    except Exception as e:  # noqa: BLE001
        return f"Error: {type(e).__name__}: {e}" + g
    finally:
        db.close()
    if r.get("status") == "error":
        return dumps(r) + g
    detail = (f"The N3MC Lite memory server was not available ({reason}), so this entry was saved without a "
              "vector. It is found by keywords now and gets its vector from repair_memory once the server runs.")
    if r.get("warning"):
        r["warning"] = "embedding_unavailable; " + r["warning"]
        r["detail"] = detail + " " + r.get("detail", "")
    else:
        r["warning"] = "embedding_unavailable"
        r["detail"] = detail
    return dumps(r) + a


def unavailable_text(cfg, name, args, reason, sent=False, nudge=True) -> str:
    """The answer of a tool when the server cannot be used (spec §4.6 "When the server is unavailable")."""
    from .tools import TOOL_NAMES, dumps
    generic, _a = _nudges()
    g = generic if nudge else ""
    if name not in TOOL_NAMES:
        return f"Error: unknown tool '{name}'" + g
    if name == "save_memory":
        return direct_save_tool(cfg, args, reason, nudge)
    if name in ("search_memory", "list_memories", "recall_thread"):
        return (f"Error: memory backend unreachable. The N3MC Lite memory server is not available ({reason}). "
                "It is started automatically; retry in a minute." + g)
    if name in ("delete_memory", "delete_memories_by_session"):
        if not sent:
            return dumps({"status": "error", "reason": f"The N3MC Lite memory server is not available ({reason}). "
                                                       "Nothing was deleted; retry in a minute."}) + g
        return dumps({"status": "unknown",
                      "reason": f"The request reached the N3MC Lite memory server but no answer came back "
                                f"({reason}). The delete may have completed; check with list_memories or "
                                "search_memory before deleting again."}) + g
    return dumps({"status": "error", "message": f"The N3MC Lite memory server is not available ({reason}). "
                                                "Retry in a minute."}) + g


def forward_tool(cfg, name, args, nudge=True, server_ok=True) -> str:
    if not server_ok:
        return unavailable_text(cfg, name, args, last_unavailable_reason(), False, nudge)
    port = int(cfg["server_port"])
    try:
        status, data = _post(port, f"/tools/{name}?nudge={1 if nudge else 0}", args, TOOL_TIMEOUT)
    except _NotSent as e:
        return unavailable_text(cfg, name, args, str(e), False, nudge)
    except _NoAnswer as e:
        return unavailable_text(cfg, name, args, str(e), True, nudge)
    if status != 200 or not isinstance(data.get("text"), str):
        return unavailable_text(cfg, name, args, f"HTTP {status}", True, nudge)
    return data["text"]


# ── hooks (spec §12) ─────────────────────────────────────────────────────────

def _read_stdin() -> bytes:
    try:
        return sys.stdin.buffer.read()
    except Exception:  # noqa: BLE001
        try:
            return sys.stdin.read().encode("utf-8", "surrogateescape")
        except Exception:  # noqa: BLE001
            return b""


def _payload(raw: bytes):
    """(payload dict or None, the decoded stdin) — lone surrogates removed."""
    text = raw.decode("utf-8", "surrogateescape")
    try:
        obj = json.loads(text)
    except ValueError:
        return None, sanitize_surrogates(text)
    if isinstance(obj, dict):
        return sanitize_surrogates(obj), sanitize_surrogates(text)
    return None, sanitize_surrogates(text)


def _extract_text(prompt) -> str:
    if isinstance(prompt, list):
        return "\n".join(p.get("text", "") for p in prompt
                         if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str))
    if not isinstance(prompt, str):
        return ""
    s = prompt.strip()
    if s.startswith("["):
        try:
            arr = json.loads(s)
        except ValueError:
            return prompt
        if (isinstance(arr, list) and arr
                and all(isinstance(p, dict) and isinstance(p.get("type"), str) for p in arr)):
            return _extract_text(arr)
    return prompt


def _audit_stage(stage, reason) -> None:
    paths.audit_append({"ts": paths.utc_now_iso(), "hook": "UserPromptSubmit", "stage": stage, "reason": reason})


def _user_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [p.get("text") for p in content
                 if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)]
        if parts:
            return "\n".join(parts)
    return None


_NOT_PROMPT_PREFIXES = ("[Request interrupted by user", "<command-", "<local-command-") + _SYSTEM_PROMPT_PREFIXES


def _previous_response(transcript_path, current_prompt):
    """Claude's previous reply from the transcript (spec §12.5), or None."""
    try:
        if not isinstance(transcript_path, str) or not transcript_path:
            raise ValueError("no transcript_path")
        p = Path(transcript_path)
        size = p.stat().st_size
        with open(p, "rb") as f:
            start = max(0, size - 5 * 1024 * 1024)
            f.seek(start)
            data = f.read()
        lines = data.decode("utf-8", "replace").splitlines()
        if start > 0 and lines:
            lines = lines[1:]
        newest_seen = False
        cur = (current_prompt or "").strip()
        for line in reversed(lines):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict) or obj.get("isSidechain") is True:
                continue
            typ = obj.get("type")
            if typ not in ("user", "assistant"):
                continue
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            if typ == "user":
                if obj.get("isMeta") is True:
                    continue
                text = _user_text(msg.get("content"))
                if text is None or text.lstrip().startswith(_NOT_PROMPT_PREFIXES):
                    continue
                if not newest_seen and text.strip() == cur:
                    newest_seen = True
                    continue
                return None
            newest_seen = True
            content = msg.get("content")
            texts = []
            if isinstance(content, str) and content.strip():
                texts = [content]
            elif isinstance(content, list):
                texts = [c.get("text") for c in content if isinstance(c, dict) and c.get("type") == "text"
                         and isinstance(c.get("text"), str) and c.get("text").strip()]
            if texts:
                return sanitize_surrogates(texts[-1])
        return None
    except Exception as e:  # noqa: BLE001
        _audit_stage("transcript_unreadable", f"{type(e).__name__}: {e}"[:200])
        return None


def _read_turn_id(session, create=True):
    p = paths.turn_id_path(session)
    try:
        t = p.read_text(encoding="utf-8").strip()
        if t:
            return t
    except OSError:
        pass
    if not create:
        return None
    t = str(uuid.uuid4())
    _write_turn_id(session, t)
    return t


def _write_turn_id(session, turn_id) -> None:
    p = paths.turn_id_path(session)
    try:
        paths.ensure_data_dir()
        fd, tmp = tempfile.mkstemp(prefix=".turn.", suffix=".tmp", dir=str(p.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(turn_id)
        os.replace(tmp, p)
    except OSError as e:
        _err(f"could not write {p}: {e}")


def _clear_save_notices() -> None:
    _clear_notice("redis_unreachable")
    _clear_notice("save_failed")


def _hook_save(cfg, server_ok, content, role, session, turn_id) -> None:
    """A hook save (spec §12.6): through /buffer, else directly to Redis without a vector."""
    if server_ok:
        try:
            status, data = _post(int(cfg["server_port"]), "/buffer",
                                 {"content": content, "role": role, "agent_name": "claude-code",
                                  "session_id": session or None, "turn_id": turn_id, "no_model_wait": True},
                                 SHORT_TIMEOUT)
        except (_NotSent, _NoAnswer) as e:
            _err(f"the memory server did not take the save ({e}); saving directly")
        else:
            if status == 503:
                _notify("redis_unreachable", _redis_unreachable_message(cfg, data.get("reason")))
                return
            if status == 200:
                st = data.get("status")
                if st in ("saved", "duplicate"):
                    _clear_save_notices()
                elif st == "error":
                    _err(f"save: {data.get('reason')}")
                return
            _err(f"the memory server answered HTTP {status}; saving directly")
    from .database import CONN_ERRORS, BackendError, Database
    db = Database(cfg, enforce_on_connect=False)
    try:
        r = db.save_memory(content, role=role, agent_name="claude-code", session_id=session or None,
                           turn_id=turn_id, embed=False)
        if r.get("status") in ("saved", "duplicate"):
            _clear_save_notices()
        elif r.get("status") == "error":
            _err(f"save: {r.get('reason')}")
    except (BackendError,) + CONN_ERRORS:
        _notify("redis_unreachable", _redis_unreachable_message(cfg))
    except Exception as e:  # noqa: BLE001
        _notify("save_failed", f"A record could not be saved ({type(e).__name__}: {e}). Current memories may be lost.")
    finally:
        db.close()


def _fallback(reason) -> str:
    try:
        notes = paths.take_due_notices()
    except Exception:  # noqa: BLE001
        notes = []
    return context.fallback_block(f"_(memory search unavailable: {reason})_", notes)


def _search_block(cfg, server_ok, query, body_extra, hook=False) -> str:
    """The `# Recalled Memory Context` block of --hook-submit / --search (spec §12.7)."""
    if not server_ok:
        if _FOREIGN_SERVER_ROOT is not None:
            return _fallback(_foreign_reason(cfg["server_port"]))
        return _fallback(last_unavailable_reason())
    body = {"query": query}
    body.update(body_extra)
    try:
        status, data = _post(int(cfg["server_port"]), "/search", body, SHORT_TIMEOUT)
    except (_NotSent, _NoAnswer) as e:
        return _fallback(str(e))
    if status == 200 and isinstance(data.get("text"), str):
        return data["text"]
    if status == 503:
        hint = data.get("detail") or backend_hint(cfg["redis_url"])
        _notify("redis_unreachable", _redis_unreachable_message(cfg, hint))
        return _fallback(hint)
    if status == 400 and not hook:
        raise ValueError(data.get("detail") or "Error: bad request")
    return _fallback(f"HTTPError: HTTP {status}")


def cmd_hook_submit(uvx=False) -> int:
    payload, raw_text = _payload(_read_stdin())
    if payload is not None:
        prompt = payload.get("prompt") if "prompt" in payload else payload.get("message")
    else:
        payload, prompt = {}, raw_text
    text = _extract_text(prompt)
    session = payload.get("session_id") if isinstance(payload.get("session_id"), str) else ""
    if text.lstrip().startswith(_SYSTEM_PROMPT_PREFIXES):
        _audit_stage("skipped", "system_prompt")
        _out(EMPTY_BLOCK)
        return 0
    cfg = load_config()
    try:
        ok = ensure_server(cfg, max_wait=0)
    except Exception as e:  # noqa: BLE001
        _err(f"server check failed: {type(e).__name__}: {e}")
        ok = False
    if ok:
        try:
            _post(int(cfg["server_port"]), "/repair", {}, HOOK_REPAIR_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            _err(f"repair: {e}")
    prev = _previous_response(payload.get("transcript_path"), text)
    if prev and prev.strip():
        _hook_save(cfg, ok, prev, "claude", session, _read_turn_id(session, create=True))
    new_tid = str(uuid.uuid4())
    _write_turn_id(session, new_tid)
    try:
        if not text.strip():
            out = EMPTY_BLOCK
        else:
            out = _search_block(cfg, ok, text, {"session_id": session or None,
                                                "max_chars": int(cfg["context_max_chars"]),
                                                "no_model_wait": True}, hook=True)
    except Exception as e:  # noqa: BLE001
        out = (f"{context.HEADER_LINE}\n{context.SOURCE_LINE}\n\n"
               f"_(memory search unavailable: {type(e).__name__})_")
    _out(out)
    if text.strip():
        _hook_save(cfg, ok, text, "user", session, new_tid)
    return 0


def cmd_save_claude_turn() -> int:
    payload, _raw = _payload(_read_stdin())
    payload = payload or {}
    session = payload.get("session_id") if isinstance(payload.get("session_id"), str) else ""
    turn_id = _read_turn_id(session, create=True)      # before the config and the server check
    msg = payload.get("last_assistant_message")
    if isinstance(msg, str) and msg.strip():
        cfg = load_config()
        try:
            ok = ensure_server(cfg, max_wait=0)
        except Exception as e:  # noqa: BLE001
            _err(f"server check failed: {type(e).__name__}: {e}")
            ok = False
        _hook_save(cfg, ok, msg, "claude", session, turn_id)
    return 0


# ── the rules file (spec §12.9) ──────────────────────────────────────────────

_RULES_MD_TEMPLATE = """<!-- n3mc-lite-behavior v1 -->
# N3MemoryCore Lite Behavioral Guidelines

These guidelines apply to every Claude Code session in this project. They are
loaded automatically from `.claude/rules/` at session start. N3MC Lite is a
working memory: every record is deleted 7 days after it was saved.

## Fully Automatic Saving
All conversations are saved by hooks (UserPromptSubmit + Stop). Do NOT call
`--buffer` for them. Every non-empty user message and Claude response is
saved (fenced code blocks as `[code omitted]`); there is no length filter and
no skip-pattern filter. Make NO acknowledgement when a save succeeds —
silence is correct. Run `{cli} --buffer "<text>"` only when the user asks
you to remember something specific.

## Recalled Memory Context
Every turn, the UserPromptSubmit hook adds a `# Recalled Memory Context` block
to your context. Each hit starts with `### ` and shows its `saved=` time and,
when it has one, `turn=<turn_id>`; a `[doc×N]` hit is a long text shown in
full. When several versions of the same content appear, the one with the
newest `saved` time is the current one. If the block contains a
`> ⚠️ N3MC notice:` line, tell the user what it says.

## Weak Matches
Records under `#### Possibly related (weak matches — may be unrelated)` did
not pass the relevance floors and are often unrelated. Use one only when it
is actually relevant to the question; if it is not, ignore it and do not
mention it.

## Active RAG
When prior context would help, run a search proactively. **Always invoke via
`{cli} --search "<keywords>"`** (optional `--since 2026-11-13`,
`--until 2026-11-14`). To read the conversation around a hit, run
`{cli} --recall-thread <turn_id>`. `{cli} --list` shows the newest records.
These commands are auto-allowed via `permissions.allow`.

## Cut Context
The block is kept within a size limit. If it says that lower-ranked matches
were omitted, or ends with `_(cut here to keep this block within the context
limit.)_`, and the missing part is needed, run `{cli} --search` with the same
keywords: a manual search is not limited.

## Deleting
Delete only when the user explicitly asks: `{cli} --delete <id>` or
`{cli} --delete-session <session_id>`. Both are irreversible and are not
auto-allowed; confirm the id or session_id with the user first. They
remove the memory from Redis only: the raw text stays in `audit.log` in
the data folder (no TTL); tell the user so when they ask to forget
something.

## Seven-Day Lifetime
When the user expects something to be kept permanently ("remember this
forever", 「ずっと覚えておいて」), the hooks save it as usual; add ONE short
sentence in the user's language: Lite memories are deleted 7 days after they
were saved; for permanent storage use the Pro build or keep an external copy.

## Recall Acknowledgment
When recalled memory actually shapes your reply (you are recalling something
saved earlier), open the reply with a short acknowledgment in the user's
language — e.g. "Pulling this from earlier memory in this session." or
「前回の回答がメモリに保存されています。」 If no relevant memory was found,
or it did not influence your answer, do NOT announce anything.

## Save Failures
If a `> ⚠️ N3MC notice:` line says that Redis could not be reached or that a
record could not be saved, tell the user prominently and relay the command it
gives (for example `docker start redis-stack`): until then, new messages are
kept only in `audit.log`.
"""

# SHA-256 (first 16 hex characters) of earlier templates, after the command forms are taken out.
_KNOWN_RULES_MD_HASHES = set()

RULES_MD_NAME = "n3mc-lite-behavior.md"
_CLI_FORM_RES = (
    re.compile(r'(?:"[^"\n]*"|[^\s`]+) -m n3mc_mcp\.cli'),
    re.compile(r"uvx --from n3memorycore-mcp-lite==\S+ n3mc-lite"),
)


def _cli_form(uvx: bool) -> str:
    if uvx:
        return f"uvx --from n3memorycore-mcp-lite=={__version__} n3mc-lite"
    return f"{shell_path(sys.executable)} -m n3mc_mcp.cli"


def _rules_text(uvx: bool) -> str:
    return _RULES_MD_TEMPLATE.replace("{cli}", _cli_form(uvx))


def _strip_cli_forms(text: str) -> str:
    for rx in _CLI_FORM_RES:
        text = rx.sub("{cli}", text)
    return text


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".n3mc.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S")


def _ensure_rules_md(project, uvx=False) -> None:
    path = Path(project) / ".claude" / "rules" / RULES_MD_NAME
    want = _rules_text(uvx)
    key = "rules_md_edited:" + hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:12]
    if not path.exists():
        _write_text_atomic(path, want)
        return
    cur = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    if cur == want:
        _clear_notice(key)
        return
    stripped = _strip_cli_forms(cur)
    if (stripped == _RULES_MD_TEMPLATE
            or hashlib.sha256(stripped.encode("utf-8")).hexdigest()[:16] in _KNOWN_RULES_MD_HASHES):
        shutil.copy2(path, path.with_name(f"{RULES_MD_NAME}.bak-n3mc-lite-{_stamp()}"))
        _write_text_atomic(path, want)
        _clear_notice(key)
        return
    _notify(key, f"The rules file {path.resolve()} has been edited by hand, so it was not updated. Merge the new "
                 "template manually if needed.")


def _project_dir(payload) -> str:
    env = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if env and os.path.isdir(env):
        return env
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    if isinstance(cwd, str) and cwd:
        return cwd
    return os.getcwd()


def cmd_stop(uvx=False) -> int:
    payload, _raw = _payload(_read_stdin())
    payload = payload or {}
    try:
        project = _project_dir(payload)
        if not paths.same_folder(project, os.path.expanduser("~")):
            _ensure_rules_md(project, uvx)
    except Exception as e:  # noqa: BLE001
        _err(f"rules file: {type(e).__name__}: {e}")
    try:
        now = time.time()
        for p in paths.data_dir().glob("turn_id*.txt"):
            try:
                if now - p.stat().st_mtime > TURN_FILE_MAX_AGE:
                    p.unlink()
            except OSError:
                pass
    except Exception as e:  # noqa: BLE001
        _err(f"turn_id cleanup: {type(e).__name__}: {e}")
    try:
        a = paths.audit_log_path()
        if a.exists() and a.stat().st_size > AUDIT_ROTATE_BYTES:
            os.replace(a, a.with_name(a.name + ".1"))
    except Exception as e:  # noqa: BLE001
        _err(f"audit.log rotation: {type(e).__name__}: {e}")
    return 0


# ── --init (spec §12.8) ──────────────────────────────────────────────────────

OURS = {  # event -> (entry-point name, module name)
    "UserPromptSubmit": ("n3mc-lite-hook", "n3mc_mcp.lite_hook"),
    "Stop":             ("n3mc-lite-stop-hook", "n3mc_mcp.lite_stop_hook"),
}
PKG_DIR = os.path.normcase(os.path.abspath(os.path.dirname(n3mc_mcp.__file__)))


def _tokens(command: str) -> list:
    command = command.replace("\\", "/")      # posix shlex reads "\" as an escape
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def is_own_hook(command: str, event: str) -> bool:
    if not isinstance(command, str):                   # null or another type: not ours, keep it
        return False
    exe_name, module = OURS[event]
    toks = _tokens(command)
    if not toks:
        return False
    first = os.path.basename(toks[0]).lower()          # (a) our entry point, any folder
    if first.endswith(".exe"):
        first = first[:-4]
    if first == exe_name:
        return True
    for i, t in enumerate(toks[:-1]):                  # (b) python -m n3mc_mcp.lite_hook
        if t == "-m" and toks[i + 1] == module:
            return True
    script = module.split(".")[-1] + ".py"             # (c) a script in this package's folder
    for t in toks[1:]:
        if os.path.basename(t).lower() == script:
            d = os.path.normcase(os.path.abspath(os.path.dirname(os.path.expanduser(t))))
            if d == PKG_DIR:
                return True
    return False


# Characters that make bash (Git Bash on Windows) split or reinterpret an unquoted word. "~" counts only at
# the start of a path (the only place bash expands it); 8.3 short names (PROGRA~1) contain it mid-word.
SHELL_SPECIAL = frozenset(" \t'\"&|;<>()$`\\!#*?[]{}")


def _needs_quotes(s: str) -> bool:
    return s.startswith("~") or any(c in SHELL_SPECIAL for c in s)


def shell_path(p) -> str:
    s = pathlib.Path(p).as_posix()                     # forward slashes: bash reads "\" as an escape
    if _needs_quotes(s) and os.name == "nt":           # a command that starts with a quote
        short = _short_path(p)                         #       does not run as a hook on Windows:
        if short and not _needs_quotes(pathlib.Path(short).as_posix()):
            s = pathlib.Path(short).as_posix()         #       use the 8.3 short name when it needs no quotes
    if not _needs_quotes(s):
        return s
    for ch in ("\\", '"', "$", "`"):                   # still special inside double quotes
        s = s.replace(ch, "\\" + ch)
    return f'"{s}"'                                    # an unquoted ' or & is a bash syntax error


def _short_path(p) -> str:
    """Windows 8.3 short path of an existing file, or "" (8.3 names may be disabled on the volume)."""
    import ctypes
    buf = ctypes.create_unicode_buffer(32768)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(p), buf, len(buf))
    return buf.value if 0 < n < len(buf) else ""


def resolve_cmd(script_name: str) -> str:
    folders = [sysconfig.get_path("scripts")]
    try:
        folders.append(sysconfig.get_path("scripts", sysconfig.get_preferred_scheme("user")))
    except (KeyError, ValueError, AttributeError):
        pass
    for folder in folders:
        for name in (script_name + ".exe", script_name):
            cand = pathlib.Path(folder) / name
            if cand.is_file():
                return shell_path(cand)
    exe = shutil.which(script_name)
    if exe:
        return shell_path(exe)
    module = {"n3mc-lite-hook": "n3mc_mcp.lite_hook",
              "n3mc-lite-stop-hook": "n3mc_mcp.lite_stop_hook"}[script_name]
    return f"{shell_path(sys.executable)} -m {module}"


_RULE_SUFFIXES = ("--search *", "--buffer *", "--list*", "--recall-thread *", "--repair*", "--stop*")


def permission_rules() -> list:
    py = shell_path(sys.executable)
    return ([f"Bash({py} -m n3mc_mcp.cli {s})" for s in _RULE_SUFFIXES]
            + [f"Bash(n3mc-lite {s})" for s in _RULE_SUFFIXES])


PERMISSION_RULES = permission_rules()


def _old_mcp_messages() -> list:
    out = []
    cj = Path(os.path.expanduser("~")) / ".claude.json"
    try:
        data = json.loads(cj.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict):
        top = data.get("mcpServers")
        if isinstance(top, dict) and MCP_NAME in top:
            out.append(f"An MCP registration of {MCP_NAME} was found in Claude Code ({cj}). Claude Code now uses "
                       f"the hooks; remove it with: claude mcp remove {MCP_NAME} -s user")
        projects = data.get("projects")
        if isinstance(projects, dict):
            for ppath, proj in projects.items():
                servers = proj.get("mcpServers") if isinstance(proj, dict) else None
                if isinstance(servers, dict) and MCP_NAME in servers:
                    out.append(f"An MCP registration of {MCP_NAME} was found in Claude Code ({cj}, project "
                               f"{ppath}). Claude Code now uses the hooks; remove it with: claude mcp remove "
                               f"{MCP_NAME} -s local (run it in that project)")
    mj = Path.cwd() / ".mcp.json"
    try:
        mdata = json.loads(mj.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        mdata = None
    if isinstance(mdata, dict) and isinstance(mdata.get("mcpServers"), dict) and MCP_NAME in mdata["mcpServers"]:
        out.append(f"An MCP registration of {MCP_NAME} was found in Claude Code ({mj}). Claude Code now uses "
                   f"the hooks; remove the \"{MCP_NAME}\" entry from {mj}")
    return out


def cmd_init() -> int:
    load_config()
    sp = Path(os.path.expanduser("~")) / ".claude" / "settings.json"
    existed = sp.exists()
    if existed:
        try:
            data = json.loads(sp.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            _out(f"{sp} could not be read ({type(e).__name__}: {e}); nothing was changed.")
            return 1
        if not isinstance(data, dict):
            _out(f"{sp} is not a JSON object; nothing was changed.")
            return 1
    else:
        data = {}
    hooks = data.get("hooks", {})
    perms = data.get("permissions", {})
    allow = perms.get("allow", []) if isinstance(perms, dict) else None
    if not isinstance(hooks, dict) or not isinstance(perms, dict) or not isinstance(allow, list):
        _out(f"{sp} has a \"hooks\", \"permissions\" or \"permissions.allow\" of the wrong type; nothing was changed.")
        return 1
    for event in OURS:
        if event in hooks and not isinstance(hooks[event], list):
            _out(f"{sp} has a \"hooks.{event}\" that is not a list; nothing was changed.")
            return 1
    new = copy.deepcopy(data)
    plugins = data.get("enabledPlugins")
    plugin_on = isinstance(plugins, dict) and plugins.get(PLUGIN_KEY) is True
    messages = []
    new_hooks = new.setdefault("hooks", {}) if (not plugin_on or hooks) else new.get("hooks", {})
    for event, (exe_name, _module) in OURS.items():
        entries = hooks.get(event, [])
        desired = None if plugin_on else {"type": "command", "command": resolve_cmd(exe_name), "timeout": 60}
        kept, placed = [], False
        for e in entries:
            hs = e.get("hooks") if isinstance(e, dict) else None
            if not isinstance(hs, list):
                kept.append(e)
                continue
            rest = []
            for h in hs:
                cmd = h.get("command") if isinstance(h, dict) else None
                if isinstance(h, dict) and is_own_hook(cmd, event):
                    if desired is not None and not placed and h == desired and len(hs) == 1:
                        rest.append(h)
                        placed = True
                    continue
                rest.append(h)
                if isinstance(cmd, str) and "n3mc" in cmd.lower():
                    messages.append(f"Another N3MC hook is registered for {event} and was left in place: {cmd}")
            if rest:
                kept.append(e if rest == hs else dict(e, hooks=rest))
        if desired is not None and not placed:
            kept.append({"hooks": [desired]})
            if desired["command"].startswith('"'):
                messages.append(f"The hook command for {event} starts with a quote because {desired['command']} "
                                "contains a space or another character the shell treats specially; if the hook "
                                "does not run, install Python into a folder without such characters.")
        if kept or event in hooks:
            new_hooks[event] = kept
    if not new_hooks and "hooks" not in data:
        new.pop("hooks", None)
    new_perms = new.setdefault("permissions", {})
    new_allow = new_perms.setdefault("allow", [])
    for rule in PERMISSION_RULES:
        if rule not in new_allow:
            new_allow.append(rule)
    if new != data:
        try:
            sp.parent.mkdir(parents=True, exist_ok=True)
            if existed:
                shutil.copy2(sp, sp.with_name(f"settings.json.bak-n3mc-lite-{_stamp()}"))
            _write_text_atomic(sp, json.dumps(new, indent=2, ensure_ascii=False) + "\n")
        except OSError as e:
            _out(f"{sp} could not be written ({type(e).__name__}: {e}).")
            return 1
    messages.extend(_old_mcp_messages())
    for m in messages:
        _out(m)
    if plugin_on:
        _out("The n3mc-workingmemory plugin is enabled and registers the N3MC Lite hooks, so n3mc-lite --init "
             "added no hooks.")
    else:
        _out(f"N3MC Lite hooks registered in {sp}. Saving starts with the next Claude Code session.")
    return 0


# ── the CLI (spec §13) ───────────────────────────────────────────────────────

def _parser():
    p = argparse.ArgumentParser(prog="n3mc-lite", description="N3MemoryCore MCP-Lite: 7-day working memory.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--search", metavar="KEYWORDS", help="search the memory")
    g.add_argument("--buffer", metavar="TEXT", help="save a text ('-' reads stdin)")
    g.add_argument("--list", action="store_true", help="list the newest records")
    g.add_argument("--recall-thread", metavar="TURN_ID", help="the records around a turn")
    g.add_argument("--delete", metavar="ID", help="delete a record (irreversible)")
    g.add_argument("--delete-session", metavar="SESSION_ID", help="delete a session's records (irreversible)")
    g.add_argument("--repair", action="store_true", help="ensure the index and add missing vectors")
    g.add_argument("--stop-server", action="store_true", help="stop this data folder's memory server")
    g.add_argument("--hook-submit", action="store_true", help=argparse.SUPPRESS)
    g.add_argument("--save-claude-turn", action="store_true", help=argparse.SUPPRESS)
    g.add_argument("--stop", action="store_true", help=argparse.SUPPRESS)
    g.add_argument("--init", action="store_true", help="register the Claude Code hooks")
    g.add_argument("--run-server", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--limit", type=int)
    p.add_argument("--before", type=int)
    p.add_argument("--after", type=int)
    p.add_argument("--agent-name")
    p.add_argument("--session-id")
    p.add_argument("--turn-id")
    p.add_argument("--importance", type=float)
    p.add_argument("--uvx", action="store_true", help=argparse.SUPPRESS)
    return p


def _tool(name, args) -> int:
    cfg = load_config()
    ok = ensure_server(cfg)
    _out(forward_tool(cfg, name, args, nudge=False, server_ok=ok))
    return 0


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    for k, v in list(vars(args).items()):
        if isinstance(v, str):
            setattr(args, k, sanitize_surrogates(v))
    if args.hook_submit:
        return cmd_hook_submit(args.uvx)
    if args.save_claude_turn:
        return cmd_save_claude_turn()
    if args.stop:
        return cmd_stop(args.uvx)
    if args.init:
        return cmd_init()
    if args.run_server:
        return run_server()
    if args.stop_server:
        return stop_server(load_config())
    if args.search is not None:
        try:
            parse_time_bounds(args.since, args.until)
        except ValueError as e:
            sys.stderr.write(str(e) + "\n")
            return 2
        cfg = load_config()
        ok = ensure_server(cfg)
        body = {"since": args.since, "until": args.until}
        if args.limit is not None:
            body["limit"] = args.limit
        sid = os.environ.get("N3MC_SESSION_ID", "").strip()
        if sid:
            body["session_id"] = sid
        try:
            out = _search_block(cfg, ok, args.search, body)
        except ValueError as e:
            sys.stderr.write(str(e) + "\n")
            return 2
        _out(out)
        return 0
    if args.buffer is not None:
        content = args.buffer
        if content == "-":
            content = sanitize_surrogates(_read_stdin().decode("utf-8", "surrogateescape"))
        a = {"content": content, "agent_name": args.agent_name or "claude-code"}
        for k in ("session_id", "turn_id", "importance"):
            if getattr(args, k) is not None:
                a[k] = getattr(args, k)
        return _tool("save_memory", a)
    if args.list:
        a = {} if args.limit is None else {"limit": args.limit}
        return _tool("list_memories", a)
    if args.recall_thread is not None:
        a = {"turn_id": args.recall_thread}
        if args.before is not None:
            a["before"] = args.before
        if args.after is not None:
            a["after"] = args.after
        return _tool("recall_thread", a)
    if args.delete is not None:
        return _tool("delete_memory", {"id": args.delete})
    if args.delete_session is not None:
        return _tool("delete_memories_by_session", {"session_id": args.delete_session})
    if args.repair:
        return _tool("repair_memory", {})
    return 2


if __name__ == "__main__":
    sys.exit(main())
