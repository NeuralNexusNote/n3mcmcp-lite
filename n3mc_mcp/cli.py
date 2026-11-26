"""CLI `n3mc-lite`: the Claude Code hooks' steps, the resident server's process management, --init, and the
tool commands (spec §3.15, §12, §13)."""
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

for _stream_name in ("stdin", "stdout", "stderr"):
    _stream = getattr(_sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

import argparse  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import pathlib  # noqa: E402
import re  # noqa: E402
import shlex  # noqa: E402
import shutil  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import sysconfig  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402
import uuid  # noqa: E402
from datetime import datetime  # noqa: E402
from http.client import HTTPException  # noqa: E402

import n3mc_mcp  # noqa: E402
from n3mc_mcp import __version__, context, paths  # noqa: E402
from n3mc_mcp.config import (  # noqa: E402
    atomic_write, backend_hint, compute_code_version, load_config, redis_display,
)
from n3mc_mcp.processor import sanitize_surrogates  # noqa: E402
from n3mc_mcp.tools import (  # noqa: E402
    REASON_STALE, REASON_STARTING, TimeBoundError, direct_save_detail, parse_bounds, reason_could_not_start,
    reason_foreign, unavailable_text, validate_save_args,
)

CHILD_TIMEOUT = 55
START_WAIT_SECONDS = 60
PORT_FREE_WAIT = 15
START_FAILED_HOLD = 300
NOTICE_INTERVAL = 86400
AUDIT_ROTATE_BYTES = 50 * 1024 * 1024
TURN_FILE_MAX_AGE = 30 * 86400
TRANSCRIPT_TAIL_BYTES = 5 * 1024 * 1024
PLUGIN_KEY = "n3mc-workingmemory@neuralnexusnote"
_SYSTEM_PROMPT_PREFIXES = ("<task-notification>", "<agent-message from=", "<system-reminder>")


def _err(msg):
    try:
        print(f"[n3mc-lite] {msg}", file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


def _read_stdin_text() -> str:
    try:
        data = sys.stdin.buffer.read()
        return data.decode("utf-8", errors="surrogateescape")
    except Exception:  # noqa: BLE001
        try:
            return sys.stdin.read()
        except Exception:  # noqa: BLE001
            return ""


# ── HTTP to the resident server (§3.15) ──────────────────────────────────────

class NotSent(Exception):
    """The request did not reach the server (connection refused, no server)."""


class NoAnswer(Exception):
    """The request was sent, but no answer came back (timeout, connection dropped)."""


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _request(port, method, path, body=None, timeout=30):
    """(status, parsed JSON or text). Raises NotSent / NoAnswer."""
    url = f"http://127.0.0.1:{port}{path}"
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8", "surrogatepass")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Host", f"127.0.0.1:{port}")
    if data is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read() if hasattr(e, "read") else b""
        status = e.code
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise NoAnswer(f"timeout: {reason}") from None
        if isinstance(reason, ConnectionRefusedError) or isinstance(reason, OSError) and not isinstance(
                reason, (ConnectionResetError, ConnectionAbortedError)):
            raise NotSent(f"{type(reason).__name__}: {reason}") from None
        raise NoAnswer(f"{type(reason).__name__}: {reason}") from None
    except (socket.timeout, TimeoutError) as e:
        raise NoAnswer(f"timeout: {e}") from None
    except (ConnectionResetError, ConnectionAbortedError, HTTPException) as e:
        raise NoAnswer(f"{type(e).__name__}: {e}") from None
    except OSError as e:
        raise NotSent(f"{type(e).__name__}: {e}") from None
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return status, raw.decode("utf-8", errors="replace")


def _get(port, path, timeout=5):
    return _request(port, "GET", path, None, timeout)


def _post(port, path, body, timeout=30):
    return _request(port, "POST", path, body, timeout)


def _health(port):
    try:
        status, body = _get(port, "/health", timeout=5)
    except (NotSent, NoAnswer):
        return None
    if status == 200 and isinstance(body, dict):
        return body
    if status == 403:
        return {"status": "forbidden"}
    return None


def _request_shutdown(port, root):
    """"ok", "unknown_endpoint", or "failed"."""
    try:
        status, _body = _post(port, "/shutdown", {"root": root}, timeout=5)
    except (NotSent, NoAnswer):
        return "failed"
    if status == 200:
        return "ok"
    if status in (404, 405):
        return "unknown_endpoint"
    return "failed"


def _port_in_use(port) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.5):
            return True
    except OSError:
        return False


def _wait_port_free(port, seconds=PORT_FREE_WAIT) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _port_in_use(port):
            return True
        time.sleep(0.3)
    return not _port_in_use(port)


# ── Notices (§3.15 "_notify") ────────────────────────────────────────────────

_NOTICE_LOCK = threading.Lock()


def _load_notices() -> dict:
    try:
        with open(paths.notices_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_notices(d: dict):
    try:
        paths.ensure_data_dir()
        atomic_write(paths.notices_path(), json.dumps(d, ensure_ascii=False, indent=2).encode("utf-8"))
    except OSError as e:
        _err(f"could not write notices.json: {e}")


def _notify(key, message):
    _err(message)
    with _NOTICE_LOCK:
        d = _load_notices()
        prev = d.get(key) if isinstance(d.get(key), dict) else None
        if prev is not None and prev.get("message") == message:
            return
        paths.append_audit({"hook": "notice", "key": key, "message": message})
        d[key] = {"message": message, "last_shown": None}
        _save_notices(d)


def _clear_notice(key):
    with _NOTICE_LOCK:
        d = _load_notices()
        if key in d:
            del d[key]
            _save_notices(d)


def _clear_notices_with_prefix(prefix):
    with _NOTICE_LOCK:
        d = _load_notices()
        keys = [k for k in d if k.startswith(prefix)]
        for k in keys:
            del d[k]
        if keys:
            _save_notices(d)


def take_due_notices() -> list:
    """Messages of notices.json due to be shown (once a day per key); marks them shown."""
    with _NOTICE_LOCK:
        d = _load_notices()
        now = time.time()
        out = []
        changed = False
        for key, v in d.items():
            if not isinstance(v, dict):
                continue
            ls = v.get("last_shown")
            if not isinstance(ls, (int, float)) or isinstance(ls, bool):
                ls = None
            if ls is None or now - ls >= NOTICE_INTERVAL:
                msg = v.get("message")
                if isinstance(msg, str) and msg:
                    out.append(msg)
                v["last_shown"] = now
                changed = True
        if changed:
            _save_notices(d)
        return out


# ── Server identity and management (§3.15) ───────────────────────────────────

_FOREIGN_SERVER_ROOT = None
_LAST_REASON = None


def last_unavailable_reason():
    return _LAST_REASON or REASON_STARTING


def _set_reason(reason):
    global _LAST_REASON
    _LAST_REASON = reason


def own_code_version(cfg) -> str:
    return compute_code_version(cfg.get("_config_bytes"))


def _health_decision(health, root=None, code_version=None) -> str:
    if not health or health.get("status") != "ok":
        return "none" if not health else "foreign"
    root = root or paths.data_dir_norm()
    h_root = health.get("root")
    if not h_root:
        return "legacy"
    if os.path.normcase(os.path.abspath(h_root)) != root:
        return "foreign"
    if code_version is not None and health.get("code_version") != code_version:
        return "stale"
    return "ok"


def _proc_data_dir(env: dict) -> str:
    v = (env or {}).get("N3MC_DATA_DIR", "").strip()
    if v:
        return os.path.normcase(os.path.abspath(os.path.expanduser(v)))
    return os.path.normcase(os.path.abspath(os.path.join(os.path.expanduser("~"), paths.DATA_DIR_NAME)))


def _is_own_server_process(pid) -> bool:
    """True only when psutil confirms: n3mc_mcp.cli --run-server of this data folder."""
    if not pid:
        return False
    try:
        import psutil
        p = psutil.Process(int(pid))
        cmd = " ".join(p.cmdline())
        if "n3mc_mcp.cli" not in cmd or "--run-server" not in cmd:
            return False
        return _proc_data_dir(p.environ()) == paths.data_dir_norm()
    except Exception:  # noqa: BLE001
        return False


def _listening_pid(port):
    try:
        import psutil
        for c in psutil.net_connections(kind="tcp"):
            if c.laddr and c.laddr.port == int(port) and c.status == psutil.CONN_LISTEN:
                return c.pid
    except Exception:  # noqa: BLE001
        return None
    return None


def _terminate(pid):
    try:
        import psutil
        p = psutil.Process(int(pid))
        p.terminate()
        try:
            p.wait(5)
        except psutil.TimeoutExpired:
            p.kill()
        return True
    except Exception:  # noqa: BLE001
        return False


def _stop_stale_server(cfg, health) -> bool:
    port = cfg["server_port"]
    root = paths.data_dir()
    r = _request_shutdown(port, root)
    if r == "ok":
        return _wait_port_free(port)
    if r == "unknown_endpoint":
        pid = (health or {}).get("pid") or _listening_pid(port)
        if _is_own_server_process(pid) and _terminate(pid):
            return _wait_port_free(port)
    return False


def _read_pid_file():
    """(exists, pid or None, mtime)."""
    p = paths.pid_path()
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        return False, None, 0.0
    try:
        with open(p, encoding="utf-8") as f:
            txt = f.read().strip()
        return True, int(txt) if txt else None, mtime
    except (OSError, ValueError):
        return True, None, mtime


def remove_pid_file_if_own(pid=None):
    pid = pid or os.getpid()
    exists, file_pid, _m = _read_pid_file()
    if exists and file_pid == pid:
        try:
            os.remove(paths.pid_path())
        except OSError:
            pass


def _remove_pid_file():
    try:
        os.remove(paths.pid_path())
    except OSError:
        pass


def _write_own_pid():
    try:
        paths.ensure_data_dir()
        p = paths.pid_path()
        try:
            with open(p, "x", encoding="utf-8") as f:
                f.write(str(os.getpid()))
        except FileExistsError:
            with open(p, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
    except OSError:
        pass


def _pid_is_starting(pid, mtime) -> bool:
    if pid is None:
        return time.time() - mtime < START_WAIT_SECONDS
    try:
        import psutil  # noqa: F401
    except ImportError:
        return time.time() - mtime < START_WAIT_SECONDS
    return _is_own_server_process(pid)


def _handle_other_port(cfg, pid) -> bool:
    """The PID-file server listens on another port (server_port was changed): stop it there."""
    port = cfg["server_port"]
    try:
        import psutil
        conns = psutil.Process(int(pid)).net_connections(kind="tcp") if hasattr(psutil.Process, "net_connections") \
            else psutil.Process(int(pid)).connections(kind="tcp")
    except Exception:  # noqa: BLE001
        return False
    root = paths.data_dir()
    for c in conns:
        try:
            if c.status != "LISTEN" or not c.laddr or c.laddr.port == int(port):
                continue
        except Exception:  # noqa: BLE001
            continue
        other = c.laddr.port
        h = _health(other)
        if not h or _health_decision(h, code_version=None) not in ("ok", "stale"):
            continue
        if _request_shutdown(other, root) == "ok":
            _wait_port_free(other)
            exists, file_pid, _m = _read_pid_file()
            if exists and file_pid == int(pid):
                _remove_pid_file()
            return True
    return False


def _start_failed_recent() -> bool:
    try:
        return time.time() - os.path.getmtime(paths.start_failed_path()) < START_FAILED_HOLD
    except OSError:
        return False


def _mark_start_failed():
    try:
        paths.ensure_data_dir()
        with open(paths.start_failed_path(), "w", encoding="utf-8") as f:
            f.write(paths.utc_now_iso())
    except OSError:
        pass


def _clear_start_failed():
    try:
        os.remove(paths.start_failed_path())
    except OSError:
        pass


def _on_server_ok(cfg, health):
    for k in ("stale_server", "port_conflict", "server_start_failed"):
        _clear_notice(k)
    _clear_start_failed()
    own = redis_display(cfg.get("redis_url"))
    theirs = (health or {}).get("redis")
    if theirs and theirs != own:
        _notify("redis_url_mismatch",
                f"The N3MC Lite server uses Redis at {theirs}, but this process is set to {own}. Put redis_url "
                f"into {paths.config_path()} instead of setting N3MC_REDIS_URL per application, then restart "
                "the server (it restarts after config.json changes).")
    else:
        _clear_notice("redis_url_mismatch")


def _wait_until_ok(cfg, seconds, proc=None) -> str:
    """"ok", "exited" (the child ended), or "timeout"."""
    port = cfg["server_port"]
    root = paths.data_dir_norm()
    cv = own_code_version(cfg)
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        h = _health(port)
        if _health_decision(h, root, cv) == "ok":
            _on_server_ok(cfg, h)
            return "ok"
        if proc is not None and proc.poll() is not None:
            return "exited"
        if time.monotonic() >= deadline:
            return "timeout"
        time.sleep(0.5)


def _popen_server(cfg):
    log = paths.server_log_path()
    try:
        if os.path.getsize(log) > 1024 * 1024:
            open(log, "w").close()
    except OSError:
        pass
    env = dict(os.environ)
    env["N3MC_DATA_DIR"] = paths.data_dir()
    cwd = paths.data_dir()
    try:
        if os.path.normcase(os.path.abspath(os.getcwd())) == paths.package_parent():
            cwd = os.getcwd()
    except OSError:
        pass
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    logf = open(log, "ab")
    try:
        return subprocess.Popen([*paths.python_cmd(), "-m", "n3mc_mcp.cli", "--run-server"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=logf,
                                cwd=cwd, env=env, **kwargs)
    finally:
        logf.close()


def _start_server(cfg, max_wait=START_WAIT_SECONDS) -> bool:
    paths.ensure_data_dir()
    try:
        fd = open(paths.pid_path(), "x", encoding="utf-8")
    except FileExistsError:
        _set_reason(REASON_STARTING)
        return _wait_until_ok(cfg, min(START_WAIT_SECONDS, max_wait)) == "ok"
    except OSError as e:
        _err(f"could not create the PID file: {e}")
        return False
    try:
        try:
            proc = _popen_server(cfg)
        except Exception as e:  # noqa: BLE001
            fd.close()
            _remove_pid_file()
            _err(f"could not start the memory server: {type(e).__name__}: {e}")
            _set_reason(reason_could_not_start(paths.server_log_path()))
            return False
        fd.write(str(proc.pid))
    finally:
        try:
            fd.close()
        except Exception:  # noqa: BLE001
            pass
    state = _wait_until_ok(cfg, min(START_WAIT_SECONDS, max_wait), proc)
    if state == "ok":
        return True
    if state == "exited":
        code = proc.poll()
        _notify("server_start_failed",
                f"The N3MC Lite memory server could not start (exit code {code}). See {paths.server_log_path()} "
                "for the reason.")
        _mark_start_failed()
        remove_pid_file_if_own(proc.pid)
        _set_reason(reason_could_not_start(paths.server_log_path()))
        return False
    _set_reason(REASON_STARTING)
    return False


def ensure_server(cfg, max_wait=START_WAIT_SECONDS) -> bool:
    """True when a healthy server of this data folder and this code answers (§3.15)."""
    global _FOREIGN_SERVER_ROOT
    port = cfg["server_port"]
    root = paths.data_dir_norm()
    cv = own_code_version(cfg)
    health = _health(port)
    decision = _health_decision(health, root, cv)
    if decision == "ok":
        _on_server_ok(cfg, health)
        return True
    if decision == "legacy":
        pid = (health or {}).get("pid") or _listening_pid(port)
        if _is_own_server_process(pid):
            decision = "stale"
        else:
            decision = "foreign"
            health = dict(health or {})
            health["root"] = "(unknown: the server did not report its folder)"
    if decision == "foreign":
        _FOREIGN_SERVER_ROOT = (health or {}).get("root") or "(unknown: the server did not report its folder)"
        _notify("port_conflict",
                f"Another N3MC server ({_FOREIGN_SERVER_ROOT}) is using port {port}. Give N3MC Lite its own "
                f"server_port in {paths.config_path()}.")
        _set_reason(reason_foreign(port))
        return False
    if decision == "stale":
        if not _stop_stale_server(cfg, health):
            h2 = _health(port)
            d2 = _health_decision(h2, root, cv)
            if d2 == "ok":
                _on_server_ok(cfg, h2)
                return True
            if h2 is not None:
                _notify("stale_server",
                        f"Cannot stop the old N3MC Lite server. Another program may be using server_port {port}. "
                        "Check server_port in config.json and restart manually.")
                _set_reason(REASON_STALE)
                return False
    exists, pid, mtime = _read_pid_file()
    if exists:
        if _pid_is_starting(pid, mtime):
            if not (pid and _handle_other_port(cfg, pid)):
                _set_reason(REASON_STARTING)
                return _wait_until_ok(cfg, min(START_WAIT_SECONDS, max_wait)) == "ok"
        else:
            _remove_pid_file()
    if _start_failed_recent():
        _set_reason(reason_could_not_start(paths.server_log_path()))
        return False
    return _start_server(cfg, max_wait)


def _foreign_reason(cfg):
    return reason_foreign(cfg["server_port"])


# ── The resident server process (§13 --run-server) ───────────────────────────

def run_server():
    from n3mc_mcp import processor
    code = 0
    try:
        _write_own_pid()
        cfg = load_config()
        cv = compute_code_version(cfg.get("_config_bytes"))
        if cfg.get("bind_host") != "127.0.0.1":
            _err(f"WARNING: bind_host is {cfg.get('bind_host')}: the server has no authentication, so the "
                 "memory is reachable from the network. Keep 127.0.0.1.")
        processor.configure(cfg.get("embedding_model"))
        from n3mc_mcp.database import Database
        db = Database(cfg)
        db.ensure_connected()
        processor.import_backend()
        from n3mc_mcp.app import create_app
        app = create_app(cfg, db, code_version=cv, port=cfg["server_port"])
        import uvicorn
        _write_own_pid()
        uvicorn.run(app, host=cfg.get("bind_host", "127.0.0.1"), port=int(cfg["server_port"]), log_level="error")
    except SystemExit as e:
        code = e.code
        if code not in (0, None):
            _record_start_failure(f"exit code {code}")
        raise
    except BaseException as e:  # noqa: BLE001
        if not isinstance(e, KeyboardInterrupt):
            _record_start_failure(f"{type(e).__name__}: {e}")
        raise
    finally:
        remove_pid_file_if_own()


def _record_start_failure(what):
    _notify("server_start_failed",
            f"The N3MC Lite memory server could not start ({what}). See {paths.server_log_path()} for the reason.")
    _mark_start_failed()


# ── Saving without the server (§3.16 direct save) ────────────────────────────

class RedisUnreachable(Exception):
    pass


def direct_save(cfg, content, role=None, agent_name=None, session_id=None, turn_id=None, importance=None,
                owner_id=None):
    """Database(cfg).save_memory(..., embed=False) from this process. Raises RedisUnreachable(hint)."""
    from n3mc_mcp.database import Database
    import redis
    db = Database(cfg)
    try:
        check = db.ensure_connected(ephemeral=False)
        if check != "ok":
            raise RedisUnreachable(check)
        try:
            return db.save_memory(content, agent_name=agent_name, owner_id=owner_id, importance=importance,
                                  session_id=session_id, turn_id=turn_id, role=role, embed=False)
        except (redis.ConnectionError, redis.TimeoutError):
            raise RedisUnreachable(backend_hint(cfg.get("redis_url"))) from None
    finally:
        if db.client is not None:
            try:
                db.client.close()
            except Exception:  # noqa: BLE001
                pass


def save_without_server(cfg, args, reason, default_session=None) -> dict:
    """The save_memory answer when the server is not available (§4.6): checks, direct save, warning."""
    from n3mc_mcp.database import add_warning
    err = validate_save_args(cfg, args)
    if err:
        return err
    session = args.get("session_id")
    if not (isinstance(session, str) and session.strip()):
        session = default_session
    try:
        r = direct_save(cfg, args.get("content"), agent_name=args.get("agent_name"), session_id=session,
                        turn_id=args.get("turn_id"), importance=args.get("importance"),
                        owner_id=args.get("owner_id"))
    except RedisUnreachable as e:
        return {"status": "error", "saved": False, "reason": str(e)}
    if r.get("status") == "saved":
        add_warning(r, "embedding_unavailable", direct_save_detail(reason))
    return r


# ── Hook steps (§12) ─────────────────────────────────────────────────────────

def _parse_payload(raw: str):
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload, True
        return {"prompt": raw}, False
    except ValueError:
        return {"prompt": raw}, False


def _extract_text(prompt) -> str:
    def from_parts(parts):
        return "\n".join(p.get("text", "") for p in parts
                         if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str))
    if isinstance(prompt, list):
        return from_parts(prompt)
    if not isinstance(prompt, str):
        return "" if prompt is None else str(prompt)
    s = prompt.strip()
    if s.startswith("["):
        try:
            arr = json.loads(s)
        except ValueError:
            return prompt
        if isinstance(arr, list) and arr and all(isinstance(x, dict) and isinstance(x.get("type"), str) for x in arr):
            return from_parts(arr)
    return prompt


def _read_turn_id(session_id, create=True):
    p = paths.turn_id_path(session_id)
    try:
        with open(p, encoding="utf-8") as f:
            t = f.read().strip()
        if t:
            return t
    except OSError:
        pass
    if not create:
        return None
    t = str(uuid.uuid4())
    _write_turn_id(session_id, t)
    return t


def _write_turn_id(session_id, turn_id):
    try:
        paths.ensure_data_dir()
        atomic_write(paths.turn_id_path(session_id), turn_id.encode("utf-8"))
    except OSError as e:
        _err(f"could not write the turn_id file: {e}")


def _hook_save(cfg, server_ok, content, role, session_id, turn_id):
    body = {"content": content, "role": role, "agent_name": "claude-code", "session_id": session_id or "",
            "turn_id": turn_id, "no_model_wait": True}
    if server_ok:
        try:
            status, r = _post(cfg["server_port"], "/buffer", body, timeout=30)
            if status == 200 and isinstance(r, dict):
                _after_save(r)
                return r
            if status == 503:
                _notify_redis_unreachable(cfg)
                return None
        except (NotSent, NoAnswer):
            pass
    try:
        r = direct_save(cfg, content, role=role, agent_name="claude-code", session_id=session_id or None,
                        turn_id=turn_id)
    except RedisUnreachable:
        _notify_redis_unreachable(cfg)
        return None
    except Exception as e:  # noqa: BLE001
        _notify("save_failed", f"A record could not be saved ({type(e).__name__}: {e}). Current memories may be lost.")
        return None
    _after_save(r)
    return r


def _after_save(r):
    st = r.get("status")
    if st in ("saved", "duplicate"):
        _clear_notice("redis_unreachable")
        _clear_notice("save_failed")
    elif st == "error":
        _err(f"save: {r.get('reason')}")


def _notify_redis_unreachable(cfg):
    _notify("redis_unreachable",
            f"N3MC Lite could not reach Redis, so a message was kept only in {paths.audit_path()}. "
            + backend_hint(cfg.get("redis_url")))


def _last_assistant_from_transcript(transcript_path, current_prompt):
    """The last text block of Claude's previous reply (Free's rules, §12.5), or None."""
    if not transcript_path or not isinstance(transcript_path, str):
        raise ValueError("no transcript_path")
    with open(transcript_path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - TRANSCRIPT_TAIL_BYTES))
        data = f.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    first = True

    def text_of(content):
        if isinstance(content, str):
            return content, True
        if isinstance(content, list):
            texts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
                     and isinstance(p.get("text"), str)]
            return "\n".join(texts), bool(texts)
        return "", False

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
        content = msg.get("content")
        if typ == "user":
            if obj.get("isMeta") is True:
                continue
            text, has = text_of(content)
            if not has:
                continue
            st = text.lstrip()
            if st.startswith(("[Request interrupted by user", "<command-", "<local-command-")) \
                    or st.startswith(_SYSTEM_PROMPT_PREFIXES):
                continue
            if first and current_prompt is not None and text.strip() == current_prompt.strip():
                first = False
                continue
            return None
        first = False
        if isinstance(content, list):
            texts = [p.get("text") for p in content if isinstance(p, dict) and p.get("type") == "text"
                     and isinstance(p.get("text"), str) and p.get("text").strip()]
            if texts:
                return texts[-1]
        elif isinstance(content, str) and content.strip():
            return content
    return None


def hook_submit(raw: str, uvx=False) -> str:
    """--hook-submit: returns the text to print (§12.3)."""
    payload, _is_json = _parse_payload(raw)
    payload = sanitize_surrogates(payload)
    prompt = payload.get("prompt", payload.get("message", ""))
    text = _extract_text(prompt)
    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else ""
    if text.lstrip().startswith(_SYSTEM_PROMPT_PREFIXES):
        paths.append_audit({"hook": "UserPromptSubmit", "stage": "skipped", "reason": "system_prompt"})
        return context.empty_block()
    cfg = load_config()
    try:
        server_ok = ensure_server(cfg, max_wait=0)
    except Exception as e:  # noqa: BLE001
        _err(f"ensure_server: {type(e).__name__}: {e}")
        server_ok = False
    if server_ok:
        try:
            _post(cfg["server_port"], "/repair", {}, timeout=10)
        except (NotSent, NoAnswer) as e:
            _err(f"repair: {e}")
    try:
        prev = _last_assistant_from_transcript(payload.get("transcript_path"), text)
    except Exception as e:  # noqa: BLE001
        paths.append_audit({"hook": "UserPromptSubmit", "stage": "transcript_unreadable",
                            "reason": f"{type(e).__name__}: {e}"[:200]})
        prev = None
    if prev:
        prev = sanitize_surrogates(prev)
        prev_turn = _read_turn_id(session_id)
        _hook_save(cfg, server_ok, prev, "claude", session_id, prev_turn)
    new_turn = str(uuid.uuid4())
    _write_turn_id(session_id, new_turn)
    try:
        if text.strip():
            out = _hook_search(cfg, server_ok, text, session_id)
        else:
            out = context.empty_block()
    except Exception as e:  # noqa: BLE001
        out = context.degraded_block(type(e).__name__)
    if text.strip():
        _hook_save(cfg, server_ok, text, "user", session_id, new_turn)
    return out


def _degraded(reason):
    try:
        notices = take_due_notices()
    except Exception:  # noqa: BLE001
        notices = []
    return context.degraded_block(reason, notices)


def _search_request(cfg, server_ok, body):
    if not server_ok:
        return _degraded(last_unavailable_reason())
    try:
        status, r = _post(cfg["server_port"], "/search", body, timeout=30)
    except (NotSent, NoAnswer) as e:
        return _degraded(str(e))
    if status == 200 and isinstance(r, dict):
        return r.get("text", "")
    if status == 503:
        hint = r.get("detail") if isinstance(r, dict) else str(r)
        _notify_redis_unreachable(cfg)
        return _degraded(hint)
    if status == 400 and isinstance(r, dict):
        raise TimeBoundError(r.get("detail"))
    detail = r.get("detail") if isinstance(r, dict) else r
    return _degraded(f"HTTPError: {status} {detail}")


def _hook_search(cfg, server_ok, text, session_id):
    body = {"query": text, "session_id": session_id or None, "max_chars": int(cfg["context_max_chars"]),
            "no_model_wait": True}
    return _search_request(cfg, server_ok, body)


def save_claude_turn(raw: str, uvx=False):
    payload, _ = _parse_payload(raw)
    payload = sanitize_surrogates(payload)
    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else ""
    turn_id = _read_turn_id(session_id)        # before the config and the server (§12.4)
    msg = payload.get("last_assistant_message")
    if not isinstance(msg, str) or not msg.strip():
        return
    cfg = load_config()
    try:
        server_ok = ensure_server(cfg, max_wait=0)
    except Exception as e:  # noqa: BLE001
        _err(f"ensure_server: {type(e).__name__}: {e}")
        server_ok = False
    _hook_save(cfg, server_ok, msg, "claude", session_id, turn_id)


def _project_dir(payload):
    env = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if env and os.path.isdir(env):
        return env
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    if isinstance(cwd, str) and cwd and os.path.isdir(cwd):
        return cwd
    return os.getcwd()


def _same_dir(a, b) -> bool:
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    except Exception:  # noqa: BLE001
        return False


def stop_cleanup(raw: str, uvx=False):
    payload, _ = _parse_payload(raw)
    try:
        project = _project_dir(payload)
        if not _same_dir(project, os.path.expanduser("~")):
            _ensure_rules_md(project, uvx)
    except Exception as e:  # noqa: BLE001
        _err(f"rules file: {type(e).__name__}: {e}")
    try:
        now = time.time()
        d = paths.data_dir()
        if os.path.isdir(d):
            for name in os.listdir(d):
                if name.startswith("turn_id") and name.endswith(".txt"):
                    p = os.path.join(d, name)
                    try:
                        if now - os.path.getmtime(p) > TURN_FILE_MAX_AGE:
                            os.remove(p)
                    except OSError:
                        pass
    except Exception as e:  # noqa: BLE001
        _err(f"turn_id cleanup: {e}")
    try:
        a = paths.audit_path()
        if os.path.exists(a) and os.path.getsize(a) > AUDIT_ROTATE_BYTES:
            os.replace(a, paths.audit_rotated_path())
    except Exception as e:  # noqa: BLE001
        _err(f"audit.log rotation: {e}")


# ── Rules file (§12.9) ───────────────────────────────────────────────────────

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

_KNOWN_RULES_MD_HASHES = frozenset()
_CMD_FORM_RE = re.compile(r'(?:"[^"\n]*"|[^\s`]+) -m n3mc_mcp\.cli|uvx --from n3memorycore-mcp-lite==\S+ n3mc-lite')


def uvx_cli() -> str:
    return f"uvx --from n3memorycore-mcp-lite=={__version__} n3mc-lite"


def cli_form(uvx=False) -> str:
    return uvx_cli() if uvx else f"{shell_path(sys.executable)} -m n3mc_mcp.cli"


def render_rules_md(uvx=False) -> str:
    return _RULES_MD_TEMPLATE.replace("{cli}", cli_form(uvx))


def _strip_cmd_forms(text: str) -> str:
    return _CMD_FORM_RE.sub("{cli}", text.replace("\r\n", "\n"))


def _ensure_rules_md(project, uvx=False):
    folder = os.path.join(project, ".claude", "rules")
    path = os.path.join(folder, "n3mc-lite-behavior.md")
    want = render_rules_md(uvx)
    key = "rules_md_edited:" + hashlib.sha256(os.path.abspath(path).encode("utf-8")).hexdigest()[:12]
    if not os.path.exists(path):
        os.makedirs(folder, exist_ok=True)
        atomic_write(path, want.encode("utf-8"))
        return "written"
    with open(path, encoding="utf-8", newline="") as f:
        have = f.read().replace("\r\n", "\n")
    if have == want:
        _clear_notice(key)
        return "same"
    stripped = _strip_cmd_forms(have)
    if stripped == _RULES_MD_TEMPLATE or hashlib.sha256(stripped.encode("utf-8")).hexdigest()[:16] in _KNOWN_RULES_MD_HASHES:
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        shutil.copyfile(path, path + f".bak-n3mc-lite-{stamp}")
        atomic_write(path, want.encode("utf-8"))
        _clear_notice(key)
        return "replaced"
    _notify(key, f"The rules file {os.path.abspath(path)} has been edited by hand, so it was not updated. "
                 "Merge the new template manually if needed.")
    return "edited"


# ── --init (§12.8) ───────────────────────────────────────────────────────────

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


_RULE_COMMANDS = ("--search *", "--buffer *", "--list*", "--recall-thread *", "--repair*", "--stop*")


def permission_rules(python=None) -> list:
    py = python or shell_path(sys.executable)
    rules = [f"Bash({py} -m n3mc_mcp.cli {c})" for c in _RULE_COMMANDS]
    rules += [f"Bash(n3mc-lite {c})" for c in _RULE_COMMANDS]
    return rules


PERMISSION_RULES = permission_rules("python")


def _settings_path() -> pathlib.Path:
    return pathlib.Path.home() / ".claude" / "settings.json"


def _check_old_mcp_registrations() -> list:
    msgs = []
    home_json = pathlib.Path.home() / ".claude.json"
    try:
        with open(home_json, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            if isinstance(data.get("mcpServers"), dict) and "n3mc-workingmemory" in data["mcpServers"]:
                msgs.append(f"An MCP registration of n3mc-workingmemory was found in Claude Code ({home_json}). "
                            "Claude Code now uses the hooks; remove it with: claude mcp remove n3mc-workingmemory -s user")
            projects = data.get("projects")
            if isinstance(projects, dict):
                for proj, v in projects.items():
                    if isinstance(v, dict) and isinstance(v.get("mcpServers"), dict) \
                            and "n3mc-workingmemory" in v["mcpServers"]:
                        msgs.append(f"An MCP registration of n3mc-workingmemory was found in Claude Code ({home_json}, "
                                    f"project {proj}). Claude Code now uses the hooks; remove it with: claude mcp "
                                    "remove n3mc-workingmemory -s local (run it in that project)")
    except (OSError, ValueError):
        pass
    local = pathlib.Path(os.getcwd()) / ".mcp.json"
    try:
        with open(local, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("mcpServers"), dict) \
                and "n3mc-workingmemory" in data["mcpServers"]:
            msgs.append(f"An MCP registration of n3mc-workingmemory was found in Claude Code ({local}). Claude Code "
                        f'now uses the hooks; remove the "n3mc-workingmemory" entry from {local}')
    except (OSError, ValueError):
        pass
    return msgs


def run_init() -> int:
    load_config()
    sp = _settings_path()
    if sp.exists():
        try:
            with open(sp, encoding="utf-8-sig") as f:
                settings = json.load(f)
        except (OSError, ValueError) as e:
            print(f"Could not read {sp} ({type(e).__name__}: {e}); nothing was changed.")
            return 1
    else:
        settings = {}
    if not isinstance(settings, dict):
        print(f"{sp} is not a JSON object; nothing was changed.")
        return 1
    hooks = settings.get("hooks", {})
    perms = settings.get("permissions", {})
    if not isinstance(hooks, dict) or not isinstance(perms, dict) \
            or not isinstance(perms.get("allow", []), list):
        print(f"{sp} has a hooks or permissions entry of the wrong type; nothing was changed.")
        return 1
    before = json.dumps(settings, sort_keys=True, ensure_ascii=False)
    plugin_on = (settings.get("enabledPlugins") or {}).get(PLUGIN_KEY) is True \
        if isinstance(settings.get("enabledPlugins"), dict) else False
    messages = []
    for event, (exe_name, _module) in OURS.items():
        entries = hooks.get(event, [])
        if not isinstance(entries, list):
            print(f"{sp}: hooks.{event} is not a list; nothing was changed.")
            return 1
        kept = []
        for entry in entries:
            if not isinstance(entry, dict):
                kept.append(entry)
                continue
            inner = entry.get("hooks")
            if not isinstance(inner, list):
                kept.append(entry)
                continue
            new_inner = [h for h in inner if not (isinstance(h, dict) and is_own_hook(h.get("command"), event))]
            for h in new_inner:
                cmd = h.get("command") if isinstance(h, dict) else None
                if isinstance(cmd, str) and "n3mc" in cmd.lower():
                    messages.append(f"Another N3MC hook is registered for {event} and was left in place: {cmd}")
            if new_inner:
                e2 = dict(entry)
                e2["hooks"] = new_inner
                kept.append(e2)
            elif not inner:
                kept.append(entry)
        if not plugin_on:
            cmd = resolve_cmd(exe_name)
            if cmd.startswith('"'):
                messages.append(f"The hook command for {event} starts with a quote because {cmd} contains a space or "
                                "another character the shell treats specially; if the hook does not run, install "
                                "Python into a folder without such characters.")
            kept.append({"hooks": [{"type": "command", "command": cmd, "timeout": 60}]})
        if kept:
            hooks[event] = kept
        elif event in hooks:
            del hooks[event]
    settings["hooks"] = hooks
    allow = perms.get("allow", [])
    for rule in permission_rules():
        if rule not in allow:
            allow.append(rule)
    perms["allow"] = allow
    settings["permissions"] = perms
    after = json.dumps(settings, sort_keys=True, ensure_ascii=False)
    if after != before:
        sp.parent.mkdir(parents=True, exist_ok=True)
        if sp.exists():
            stamp = datetime.now().strftime("%Y%m%d%H%M%S")
            shutil.copyfile(sp, str(sp) + f".bak-n3mc-lite-{stamp}")
        atomic_write(str(sp), (json.dumps(settings, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    for m in messages:
        print(m)
    for m in _check_old_mcp_registrations():
        print(m)
    if plugin_on:
        print("The n3mc-workingmemory plugin is enabled and registers the N3MC Lite hooks, so n3mc-lite --init "
              "added no hooks.")
    else:
        print(f"N3MC Lite hooks registered in {sp}. Saving starts with the next Claude Code session.")
    return 0


# ── Tool commands (§13) ──────────────────────────────────────────────────────

def _tool_via_server(cfg, name, args):
    """(text, exit code). Prints nothing."""
    ok = ensure_server(cfg)
    if not ok:
        reason = last_unavailable_reason()
        if name == "save_memory":
            r = save_without_server(cfg, args, reason, os.environ.get("N3MC_SESSION_ID", "").strip() or None)
            return json.dumps(r, ensure_ascii=False), 0
        return unavailable_text(name, reason, sent=False), 0
    try:
        status, r = _post(cfg["server_port"], f"/tools/{name}?nudge=0", args, timeout=300)
    except NotSent as e:
        if name == "save_memory":
            return json.dumps(save_without_server(cfg, args, str(e)), ensure_ascii=False), 0
        return unavailable_text(name, str(e), sent=False), 0
    except NoAnswer as e:
        return unavailable_text(name, str(e), sent=True), 0
    if status == 200 and isinstance(r, dict):
        return r.get("text", ""), 0
    reason = f"HTTP {status}"
    if name == "save_memory":
        return json.dumps(save_without_server(cfg, args, reason), ensure_ascii=False), 0
    return unavailable_text(name, reason, sent=True), 0


def cmd_search(cfg, query, since, until, limit) -> int:
    try:
        parse_bounds(since, until)
    except TimeBoundError as e:
        print(str(e), file=sys.stderr)
        return 2
    body = {"query": query, "since": since, "until": until}
    if limit is not None:
        body["limit"] = limit
    sess = os.environ.get("N3MC_SESSION_ID", "").strip()
    if sess:
        body["session_id"] = sess
    ok = ensure_server(cfg)
    try:
        text = _search_request(cfg, ok, body)
    except TimeBoundError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(text)
    return 0


def cmd_stop_server(cfg) -> int:
    port = cfg["server_port"]
    h = _health(port)
    if _health_decision(h, paths.data_dir_norm(), None) not in ("ok", "stale"):
        print("No N3MC Lite server of this data folder is running.")
        return 0
    pid = h.get("pid")
    r = _request_shutdown(port, paths.data_dir())
    if _wait_port_free(port):
        print(f"Stopped the N3MC Lite server (pid {pid}).")
        return 0
    reason = "the port is still in use after the stop request" if r == "ok" else "the stop request failed"
    print(f"Could not stop the N3MC Lite server (pid {pid}): {reason}. End the process whose command line contains "
          "n3mc_mcp.cli and --run-server.", file=sys.stderr)
    return 1


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    argv = sanitize_surrogates(list(argv))
    p = argparse.ArgumentParser(prog="n3mc-lite", description="N3MemoryCore Lite (7-day working memory) CLI")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--search", metavar="KEYWORDS")
    g.add_argument("--buffer", metavar="TEXT")
    g.add_argument("--list", action="store_true")
    g.add_argument("--recall-thread", metavar="TURN_ID")
    g.add_argument("--delete", metavar="ID")
    g.add_argument("--delete-session", metavar="SESSION_ID")
    g.add_argument("--repair", action="store_true")
    g.add_argument("--stop-server", action="store_true")
    g.add_argument("--hook-submit", action="store_true")
    g.add_argument("--save-claude-turn", action="store_true")
    g.add_argument("--stop", action="store_true")
    g.add_argument("--init", action="store_true")
    g.add_argument("--run-server", action="store_true")
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--limit", type=int)
    p.add_argument("--before", type=int, default=2)
    p.add_argument("--after", type=int, default=2)
    p.add_argument("--agent-name", default="claude-code")
    p.add_argument("--session-id")
    p.add_argument("--turn-id")
    p.add_argument("--importance", type=float)
    p.add_argument("--uvx", action="store_true")
    a = p.parse_args(argv)

    if a.run_server:
        run_server()
        return 0
    if a.hook_submit:
        raw = _read_stdin_text()
        try:
            out = hook_submit(raw, a.uvx)
        except Exception as e:  # noqa: BLE001
            _err(f"--hook-submit: {type(e).__name__}: {e}")
            out = context.degraded_block(type(e).__name__)
        print(out)
        return 0
    if a.save_claude_turn:
        try:
            save_claude_turn(_read_stdin_text(), a.uvx)
        except Exception as e:  # noqa: BLE001
            _err(f"--save-claude-turn: {type(e).__name__}: {e}")
        return 0
    if a.stop:
        try:
            stop_cleanup(_read_stdin_text(), a.uvx)
        except Exception as e:  # noqa: BLE001
            _err(f"--stop: {type(e).__name__}: {e}")
        return 0
    if a.init:
        return run_init()
    cfg = load_config()
    if a.stop_server:
        return cmd_stop_server(cfg)
    if a.search is not None:
        return cmd_search(cfg, a.search, a.since, a.until, a.limit)
    if a.buffer is not None:
        content = _read_stdin_text() if a.buffer == "-" else a.buffer
        args = {"content": sanitize_surrogates(content), "agent_name": a.agent_name}
        if a.session_id is not None:
            args["session_id"] = a.session_id
        if a.turn_id is not None:
            args["turn_id"] = a.turn_id
        if a.importance is not None:
            args["importance"] = a.importance
        text, code = _tool_via_server(cfg, "save_memory", args)
    elif a.list:
        args = {} if a.limit is None else {"limit": a.limit}
        text, code = _tool_via_server(cfg, "list_memories", args)
    elif a.recall_thread is not None:
        text, code = _tool_via_server(cfg, "recall_thread",
                                      {"turn_id": a.recall_thread, "before": a.before, "after": a.after})
    elif a.delete is not None:
        text, code = _tool_via_server(cfg, "delete_memory", {"id": a.delete})
    elif a.delete_session is not None:
        text, code = _tool_via_server(cfg, "delete_memories_by_session", {"session_id": a.delete_session})
    else:
        text, code = _tool_via_server(cfg, "repair_memory", {})
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
