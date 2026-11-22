"""CLI `n3mc-lite` and the resident-server management (spec sections 3.15, 12, 13).

--init --stop --stop-server --hook-submit --save-claude-turn --run-server --search --buffer --list
--recall-thread --delete --delete-session --repair; ensure_server, _start_server, _notify."""

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
        except Exception:  # noqa: BLE001
            pass

import argparse
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import socket
import subprocess
import sysconfig
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

import n3mc_mcp
from n3mc_mcp import __version__, context, paths, processor
from n3mc_mcp import config as config_mod

AGENT_NAME = "claude-code"
PLUGIN_KEY = "n3mc-workingmemory@neuralnexusnote"
_SYSTEM_PROMPT_PREFIXES = ("<task-notification>", "<agent-message from=", "<system-reminder>")
_FOREIGN_SERVER_ROOT = None
_LAST_STATE = "starting"      # 'starting' | 'failed' | 'foreign'
_notice_lock = threading.Lock()

PERMISSION_RULES = [
    "Bash(python -m n3mc_mcp.cli --search *)",
    "Bash(python -m n3mc_mcp.cli --buffer *)",
    "Bash(python -m n3mc_mcp.cli --list*)",
    "Bash(python -m n3mc_mcp.cli --recall-thread *)",
    "Bash(python -m n3mc_mcp.cli --repair*)",
    "Bash(python -m n3mc_mcp.cli --stop*)",
    "Bash(n3mc-lite --search *)",
    "Bash(n3mc-lite --buffer *)",
    "Bash(n3mc-lite --list*)",
    "Bash(n3mc-lite --recall-thread *)",
    "Bash(n3mc-lite --repair*)",
    "Bash(n3mc-lite --stop*)",
]

# ---------------------------------------------------------------------------
# audit.log, notices
# ---------------------------------------------------------------------------


def _utc_iso():
    return datetime.now(timezone.utc).isoformat()


def _audit(record):
    """Append one JSON line to audit.log. Never raises."""
    try:
        paths.ensure_data_dir()
        line = json.dumps(record, ensure_ascii=False)
        line = processor.sanitize_surrogates(line)
        with open(paths.audit_log_path(), "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
    except Exception as exc:  # noqa: BLE001
        print("N3MC Lite: could not write audit.log (%s)" % exc, file=sys.stderr)


def _read_notices():
    try:
        with open(paths.notices_path(), "r", encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_notices(obj):
    paths.ensure_data_dir()
    fd, tmp = tempfile.mkstemp(prefix=".notices-", suffix=".tmp", dir=paths.data_dir())
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, paths.notices_path())
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _notify(key, message):
    """stderr + audit.log (once per key and message) + notices.json (shown once a day)."""
    print(message, file=sys.stderr)
    try:
        with _notice_lock:
            obj = _read_notices()
            cur = obj.get(key)
            if not (isinstance(cur, dict) and cur.get("message") == message):
                _audit({"ts": _utc_iso(), "hook": "notice", "key": key, "message": message})
                obj[key] = {"message": message, "last_shown": None}
                _write_notices(obj)
    except Exception as exc:  # noqa: BLE001
        print("N3MC Lite: could not record a notice (%s)" % exc, file=sys.stderr)


def _clear_notice(key):
    try:
        with _notice_lock:
            obj = _read_notices()
            if key in obj:
                del obj[key]
                _write_notices(obj)
    except Exception:  # noqa: BLE001
        pass


def due_notices():
    """Messages of notices.json that are due (once per day each); marks them shown."""
    out = []
    with _notice_lock:
        obj = _read_notices()
        now = time.time()
        changed = False
        for key, v in obj.items():
            if not isinstance(v, dict) or not v.get("message"):
                continue
            last = v.get("last_shown")
            if last is None or now - float(last) >= 86400:
                out.append(v["message"])
                v["last_shown"] = now
                changed = True
        if changed:
            try:
                _write_notices(obj)
            except Exception:  # noqa: BLE001
                pass
    return out


# ---------------------------------------------------------------------------
# HTTP (never through a proxy)
# ---------------------------------------------------------------------------

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class HttpStatusError(Exception):
    def __init__(self, status, body=""):
        super().__init__("HTTP %s %s" % (status, body[:200]))
        self.status = status
        self.body = body


def _request(method, port, path, body=None, timeout=30):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request("http://127.0.0.1:%s%s" % (port, path), data=data, method=method,
                                 headers={"Host": "127.0.0.1:%s" % port, "Content-Type": "application/json"})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            text = ""
        raise HttpStatusError(exc.code, text)


def _get(port, path, timeout=5):
    return _request("GET", port, path, None, timeout)


def _post(port, path, body, timeout=30):
    return _request("POST", port, path, body, timeout)


def _health(port):
    try:
        h = _get(port, "/health", timeout=5)
        return h if isinstance(h, dict) else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Server identity, stop, start (3.15)
# ---------------------------------------------------------------------------


def _norm(p):
    return os.path.normcase(os.path.abspath(p))


def _own_root():
    return paths.data_dir()


def _health_decision(health):
    """'ok' | 'stale' | 'foreign' | 'legacy' | 'none'."""
    if not health:
        return "none"
    root = health.get("root")
    if not root:
        return "legacy"
    if _norm(root) != _norm(_own_root()):
        return "foreign"
    if health.get("code_version") != config_mod.code_version():
        return "stale"
    return "ok"


def _psutil():
    try:
        import psutil
        return psutil
    except Exception:  # noqa: BLE001
        return None


def _is_own_server_process(pid):
    ps = _psutil()
    if ps is None or not pid:
        return False
    try:
        p = ps.Process(int(pid))
        cmd = " ".join(p.cmdline())
        if "n3mc_mcp.cli" not in cmd or "--run-server" not in cmd:
            return False
        env = p.environ().get("N3MC_DATA_DIR")
        if env:
            folder = os.path.abspath(os.path.expanduser(env))
        else:
            folder = os.path.join(os.path.expanduser("~"), ".n3mc-workingmemory")
        return _norm(folder) == _norm(_own_root())
    except Exception:  # noqa: BLE001
        return False


def _listener_pid(port):
    ps = _psutil()
    if ps is None:
        return None
    try:
        for c in ps.net_connections(kind="inet"):
            if c.status == "LISTEN" and c.laddr and c.laddr.port == port and c.pid:
                return c.pid
    except Exception:  # noqa: BLE001
        pass
    return None


def _listening_ports(pid):
    ps = _psutil()
    if ps is None:
        return []
    try:
        p = ps.Process(int(pid))
        fn = getattr(p, "net_connections", None) or p.connections
        return [c.laddr.port for c in fn(kind="inet") if c.status == "LISTEN" and c.laddr]
    except Exception:  # noqa: BLE001
        return []


def _port_free(port):
    s = socket.socket()
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", port)) != 0
    finally:
        s.close()


def _wait_port_free(port, seconds=15):
    end = time.time() + seconds
    while time.time() < end:
        if _port_free(port):
            return True
        time.sleep(0.3)
    return _port_free(port)


def _request_shutdown(port):
    return _post(port, "/shutdown", {"root": _own_root()}, timeout=5)


def _kill_process(pid):
    ps = _psutil()
    if ps is None:
        return False
    try:
        p = ps.Process(int(pid))
        p.terminate()
        try:
            p.wait(5)
        except Exception:  # noqa: BLE001
            p.kill()
        return True
    except Exception:  # noqa: BLE001
        return False


def _stop_stale_server(port, health=None):
    """Stop our own server on `port`: POST /shutdown; psutil-checked kill only for servers without /shutdown."""
    try:
        _request_shutdown(port)
    except HttpStatusError as exc:
        if exc.status in (404, 405):
            pid = (health or {}).get("pid") or _listener_pid(port)
            if _is_own_server_process(pid):
                _kill_process(pid)
            else:
                return False
        else:
            return False
    except Exception:  # noqa: BLE001
        return False
    ok = _wait_port_free(port, 15)
    if (health or {}).get("pid"):
        paths.remove_pid_file_if_own(health["pid"])
    return ok


def _read_pid():
    """('missing',) | ('empty', age_seconds) | ('pid', int, age_seconds)."""
    p = paths.pid_path()
    try:
        age = time.time() - os.path.getmtime(p)
    except OSError:
        return ("missing",)
    try:
        with open(p, "r", encoding="utf-8") as f:
            txt = f.read().strip()
        if not txt:
            return ("empty", age)
        return ("pid", int(txt), age)
    except (OSError, ValueError):
        return ("empty", age)


def _remove_pid_file():
    try:
        os.unlink(paths.pid_path())
    except OSError:
        pass


def _poll_health(cfg, seconds):
    end = time.time() + seconds
    while True:
        h = _health(cfg["server_port"])
        if h:
            return h
        if time.time() >= end:
            return None
        time.sleep(0.5)


def _after_ok(cfg, health):
    """A healthy server of ours answered: compare its Redis with ours (L7)."""
    from .database import redis_display
    mine = redis_display(cfg["redis_url"])
    theirs = health.get("redis")
    if theirs and theirs == mine:
        _clear_notice("redis_url_mismatch")  # the setting was fixed (2026-11-22, L2)
    elif theirs and theirs != mine:
        _notify("redis_url_mismatch",
                "The N3MC Lite server uses Redis at %s, but this process is set to %s. Put redis_url into %s "
                "instead of setting N3MC_REDIS_URL per application, then restart the server (it restarts "
                "after config.json changes)." % (theirs, mine, paths.config_path()))
    for key in ("stale_server", "port_conflict", "server_start_failed"):
        _clear_notice(key)


def _mark_foreign(cfg, root):
    global _FOREIGN_SERVER_ROOT, _LAST_STATE
    _FOREIGN_SERVER_ROOT = root
    _LAST_STATE = "foreign"
    _notify("port_conflict", "Another N3MC server (%s) is using port %s. Give N3MC Lite its own "
                             "server_port in %s." % (root, cfg["server_port"], paths.config_path()))
    return False


def _handle_other_port(cfg, pid):
    """M1: the PID file's server listens on another port than server_port: stop it. True if handled."""
    own_port = cfg["server_port"]
    handled = False
    for port in _listening_ports(pid):
        if port == own_port:
            continue
        h = _health(port)
        if h and h.get("root") and _norm(h["root"]) == _norm(_own_root()):
            try:
                _request_shutdown(port)
            except Exception:  # noqa: BLE001
                continue
            _wait_port_free(port, 15)
            handled = True
    if handled:
        try:
            if _read_pid()[0] == "pid" and _read_pid()[1] == pid:
                _remove_pid_file()
        except Exception:  # noqa: BLE001
            pass
    return handled


def ensure_server(cfg, max_wait=60):
    """True when a usable server of this data folder answers; False otherwise (see _LAST_STATE)."""
    global _LAST_STATE, _FOREIGN_SERVER_ROOT
    port = cfg["server_port"]
    h = _health(port)
    d = _health_decision(h)
    if d == "ok":
        _after_ok(cfg, h)
        return True
    if d == "foreign":
        return _mark_foreign(cfg, h["root"])
    if d == "stale":
        if not _stop_stale_server(port, h):
            _notify("stale_server", "Cannot stop the old N3MC Lite server. Another program may be using "
                                    "server_port %s. Check server_port in config.json and restart manually."
                    % port)
            return True
    elif d == "legacy":
        pid = h.get("pid") or _listener_pid(port)
        if _is_own_server_process(pid):
            _kill_process(pid)
            _wait_port_free(port, 15)
        else:
            return _mark_foreign(cfg, "(unknown: the server did not report its folder)")
    return _wait_or_start(cfg, max_wait)


def _wait_or_start(cfg, max_wait):
    global _LAST_STATE
    info = _read_pid()
    if info[0] != "missing":
        starting = False
        pid = None
        if info[0] == "empty":
            starting = info[1] < 60
        else:
            pid, age = info[1], info[2]
            if _psutil() is None:
                starting = age < 60
            else:
                starting = _is_own_server_process(pid)
        if starting:
            if pid and _handle_other_port(cfg, pid):
                _remove_pid_file()
            else:
                h = _poll_health(cfg, min(60, max_wait)) if max_wait > 0 else _health(cfg["server_port"])
                if h:
                    return ensure_server(cfg, max_wait=0)
                _LAST_STATE = "starting"
                return False
        else:
            _remove_pid_file()
    return _start_server(cfg, max_wait)


def _start_server(cfg, max_wait=60):
    global _LAST_STATE
    marker = paths.start_failed_path()
    try:
        if os.path.exists(marker) and time.time() - os.path.getmtime(marker) < 300:
            _LAST_STATE = "failed"
            return False
    except OSError:
        pass
    paths.ensure_data_dir()
    log_path = paths.server_log_path()
    try:
        if os.path.getsize(log_path) > 1024 * 1024:
            open(log_path, "w").close()
    except OSError:
        pass
    try:
        pid_f = open(paths.pid_path(), "x", encoding="utf-8")
    except FileExistsError:
        return _wait_or_start(cfg, max_wait) if max_wait > 0 else False
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    try:
        logf = open(log_path, "ab")
        try:
            proc = subprocess.Popen([*paths.python_cmd(), "-m", "n3mc_mcp.cli", "--run-server"],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=logf,
                                    **kwargs)
        finally:
            logf.close()
    except Exception as exc:  # noqa: BLE001
        print("N3MC Lite: could not start the server (%s: %s)" % (type(exc).__name__, exc), file=sys.stderr)
        pid_f.close()
        _remove_pid_file()
        _LAST_STATE = "failed"
        return False
    pid_f.write(str(proc.pid))
    pid_f.close()
    _LAST_STATE = "starting"
    if max_wait <= 0:
        return False
    end = time.time() + max_wait
    while True:
        h = _health(cfg["server_port"])
        if h and _health_decision(h) in ("ok", "stale"):
            try:
                os.unlink(marker)
            except OSError:
                pass
            for key in ("stale_server", "port_conflict", "server_start_failed"):
                _clear_notice(key)
            return True
        if proc.poll() is not None:
            _notify("server_start_failed", "The N3MC Lite memory server could not start (exit code %s). See %s "
                                           "for the reason." % (proc.returncode, log_path))
            try:
                with open(marker, "w", encoding="utf-8") as f:
                    f.write(_utc_iso())
            except OSError:
                pass
            info = _read_pid()
            if info[0] == "pid" and info[1] == proc.pid:
                _remove_pid_file()
            _LAST_STATE = "failed"
            return False
        if time.time() >= end:
            return False
        time.sleep(0.5)


def unavailable_reason(cfg):
    if _LAST_STATE == "foreign":
        return "another N3MC folder's server is using port %s" % cfg["server_port"]
    if _LAST_STATE == "failed":
        return "the memory server could not start; see %s" % paths.server_log_path()
    return "the memory server is still starting (the first start downloads the embedding model, ~440 MB)"


def run_server():
    """Run the resident server in this process."""
    cfg = config_mod.load_config()
    # Right after reading config.json, so the hash matches the settings this server runs with (L4, 2026-11-22).
    version = config_mod.code_version()
    if cfg["bind_host"] != "127.0.0.1":
        print("N3MC Lite WARNING: bind_host is %s; the server has no authentication, so your memories "
              "are exposed to the network." % cfg["bind_host"], file=sys.stderr)
    try:
        _run_server(cfg, version)
    except SystemExit as exc:
        if exc.code not in (0, None):
            _record_start_failure(cfg, exc.code)
        raise
    except BaseException as exc:  # noqa: BLE001
        _record_start_failure(cfg, "%s: %s" % (type(exc).__name__, exc))
        raise
    finally:
        paths.remove_pid_file_if_own()


def _record_start_failure(cfg, what):
    _notify("server_start_failed", "The N3MC Lite memory server could not start (%s). See %s for the reason."
            % (what, paths.server_log_path()))
    try:
        with open(paths.start_failed_path(), "w", encoding="utf-8") as f:
            f.write(_utc_iso())
    except OSError:
        pass


def _write_own_pid():
    """Write this process's pid into the PID file (spec section 13, L1 2026-11-22). The starter writes
    Popen.pid, which on Windows is the venv / uv launcher of python.exe, not the server: the launcher listens
    on no port, so the moved-port check (M1) found nothing and remove_pid_file_if_own() never matched."""
    try:
        with open(paths.pid_path(), "x", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except FileExistsError:
        try:
            with open(paths.pid_path(), "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
        except OSError:
            pass
    except OSError:
        pass


def _run_server(cfg, version=None):
    import uvicorn
    from . import app as app_mod
    from .tools import Service
    paths.ensure_data_dir()
    _write_own_pid()
    svc = Service(cfg)
    try:
        svc.db.ensure_connected()
    except Exception as exc:  # noqa: BLE001
        print("N3MC Lite: Redis is not ready (%s); the server keeps running and retries per call" % exc,
              file=sys.stderr)
    processor.import_backend()
    application = app_mod.create_app(svc, port=cfg["server_port"], start_model=True, code_version=version)
    _write_own_pid()  # again just before listening: the starter writes Popen.pid right after Popen returns
    uvicorn.run(application, host=cfg["bind_host"], port=cfg["server_port"], log_level="error")


# ---------------------------------------------------------------------------
# Talking to the server from CLI / hooks
# ---------------------------------------------------------------------------


def _degraded(cfg, reason):
    parts = ["%s\n%s\n" % (context.HEADING, context.SOURCE_LINE)]
    for m in due_notices():
        parts.append("> ⚠️ N3MC notice: %s\n" % m)
    parts.append("_(memory search unavailable: %s)_\n" % reason)
    return "\n".join(parts)


def _search_text(cfg, srv_ok, body):
    """Search through the server; falls back to the degraded output. Returns text."""
    if not srv_ok:
        return _degraded(cfg, unavailable_reason(cfg))
    try:
        res = _post(cfg["server_port"], "/search", body, timeout=30)
        return res["text"]
    except HttpStatusError as exc:
        if exc.status == 503:
            try:
                hint = json.loads(exc.body).get("detail", "")
            except ValueError:
                hint = exc.body
            _notify("redis_unreachable", "N3MC Lite could not reach Redis, so a message was kept only in %s. %s"
                    % (paths.audit_log_path(), hint))
            return _degraded(cfg, hint)
        if exc.status == 400:
            try:
                return "ERR400:" + json.loads(exc.body).get("detail", "")
            except ValueError:
                pass
        return _degraded(cfg, "HttpStatusError: %s" % exc)
    except Exception as exc:  # noqa: BLE001
        return _degraded(cfg, "%s: %s" % (type(exc).__name__, exc))


def direct_save(cfg, args, role=None):
    """Save from this process with no vector (spec 3.16). Returns the result dict."""
    from . import database
    from .tools import Service
    svc = Service(cfg)
    try:
        svc.db.ensure_connected(enforce=False)
        res = svc.save(args, role=role, embed=False)
    except database.BackendUnreachable as exc:
        return {"status": "error", "saved": False, "reason": str(exc), "_unreachable": True}
    except Exception as exc:  # noqa: BLE001
        if exc.__class__.__module__.startswith("redis"):
            return {"status": "error", "saved": False, "reason": svc.db.hint(), "_unreachable": True}
        return {"status": "error", "saved": False, "reason": "%s: %s" % (type(exc).__name__, exc),
                "_failed": True}
    finally:
        svc.db.close()
    return res


def _hook_save(cfg, srv_ok, content, role, session_id, turn_id):
    """Save one hook record through the server, else directly. Never raises."""
    payload = {"content": content, "role": role, "agent_name": AGENT_NAME, "turn_id": turn_id,
               "no_model_wait": True}
    if session_id:
        payload["session_id"] = session_id
    res = None
    if srv_ok:
        try:
            res = _post(cfg["server_port"], "/buffer", payload, timeout=30)
        except HttpStatusError as exc:
            if exc.status == 503:
                try:
                    hint = json.loads(exc.body).get("reason", "")
                except ValueError:
                    hint = exc.body
                _notify("redis_unreachable", "N3MC Lite could not reach Redis, so a message was kept only in "
                                             "%s. %s" % (paths.audit_log_path(), hint))
                return
        except Exception:  # noqa: BLE001
            res = None
    if res is None:
        args = {"content": content, "agent_name": AGENT_NAME, "turn_id": turn_id}
        if session_id:
            args["session_id"] = session_id
        res = direct_save(cfg, args, role=role)
        if res.get("_unreachable"):
            _notify("redis_unreachable", "N3MC Lite could not reach Redis, so a message was kept only in %s. %s"
                    % (paths.audit_log_path(), res.get("reason", "")))
            return
        if res.get("_failed"):
            _notify("save_failed", "A record could not be saved (%s). Current memories may be lost."
                    % res.get("reason"))
            return
    status = res.get("status")
    if status in ("saved", "duplicate"):
        _clear_notice("redis_unreachable")
        _clear_notice("save_failed")
    elif status == "error":
        print("N3MC Lite: save failed: %s" % res.get("reason"), file=sys.stderr)


# ---------------------------------------------------------------------------
# Hook commands
# ---------------------------------------------------------------------------


def _stdin_text():
    try:
        data = sys.stdin.buffer.read()
    except Exception:  # noqa: BLE001
        return ""
    return data.decode("utf-8", errors="surrogateescape")


def _parse_payload(raw):
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    obj = processor.sanitize_surrogates(obj)
    return obj if isinstance(obj, dict) else None


def _is_typed_array(v):
    return isinstance(v, list) and v and all(isinstance(x, dict) and isinstance(x.get("type"), str) for x in v)


def _extract_text(prompt):
    """Text of a prompt: multimodal arrays give their text parts, everything else is the user's own text."""
    if prompt is None:
        return ""
    if isinstance(prompt, str):
        s = prompt.strip()
        if s.startswith("["):
            try:
                v = json.loads(s)
            except ValueError:
                v = None
            if _is_typed_array(v):
                prompt = v
            else:
                return prompt
        else:
            return prompt
    if isinstance(prompt, list):
        if _is_typed_array(prompt):
            return "\n".join(str(x.get("text", "")) for x in prompt if x.get("type") == "text")
        return json.dumps(prompt, ensure_ascii=False)
    return str(prompt)


def _out(text):
    if not text.endswith("\n"):
        text += "\n"
    sys.stdout.write(text)
    sys.stdout.flush()


def _read_turn_id(session_id):
    p = paths.turn_id_path(session_id)
    try:
        with open(p, "r", encoding="utf-8") as f:
            t = f.read().strip()
        if t:
            return t
    except OSError:
        pass
    return None


def _write_turn_id(session_id, turn_id):
    try:
        paths.ensure_data_dir()
        with open(paths.turn_id_path(session_id), "w", encoding="utf-8") as f:
            f.write(turn_id)
    except OSError as exc:
        print("N3MC Lite: could not write the turn id (%s)" % exc, file=sys.stderr)


def _text_of_content(content):
    """(has_text_part, joined text) of a transcript message content."""
    if isinstance(content, str):
        return True, content
    if isinstance(content, list):
        texts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
        return bool(texts), "\n".join(texts)
    return False, ""


def _is_real_prompt(row):
    if row.get("type") != "user" or row.get("isMeta") is True:
        return False, ""
    msg = row.get("message") or {}
    has, text = _text_of_content(msg.get("content") if isinstance(msg, dict) else None)
    if not has:
        return False, ""
    s = text.lstrip()
    if s.startswith(("[Request interrupted by user", "<command-", "<local-command-") + _SYSTEM_PROMPT_PREFIXES):
        return False, ""
    return True, text


def _last_assistant_text(transcript_path, current_text):
    """Claude's previous answer from the transcript (spec 12.5); None if there is none. Never raises."""
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as f:
            start = max(0, size - 5 * 1024 * 1024)
            f.seek(start)
            data = f.read()
        lines = data.split(b"\n")
        if start > 0 and lines:
            lines = lines[1:]
    except Exception as exc:  # noqa: BLE001
        _audit({"ts": _utc_iso(), "hook": "UserPromptSubmit", "stage": "transcript_unreadable",
                "reason": "%s: %s" % (type(exc).__name__, exc)})
        return None
    first = True
    try:
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(row, dict) or row.get("isSidechain") is True:
                continue
            kind = row.get("type")
            if kind not in ("user", "assistant"):
                continue
            was_first, first = first, False
            if kind == "user":
                real, text = _is_real_prompt(row)
                if not real:
                    continue
                if was_first and text.strip() == (current_text or "").strip():
                    continue
                return None
            msg = row.get("message") or {}
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, str):
                parts = [content] if content.strip() else []
            elif isinstance(content, list):
                parts = [p.get("text", "") for p in content if isinstance(p, dict)
                         and p.get("type") == "text" and str(p.get("text", "")).strip()]
            else:
                parts = []
            if parts:
                return parts[-1]
    except Exception as exc:  # noqa: BLE001
        _audit({"ts": _utc_iso(), "hook": "UserPromptSubmit", "stage": "transcript_unreadable",
                "reason": "%s: %s" % (type(exc).__name__, exc)})
    return None


def cmd_hook_submit(uvx=False):
    raw = _stdin_text()
    payload = _parse_payload(raw)
    if payload is not None:
        prompt = payload.get("prompt", payload.get("message"))
        session_id = payload.get("session_id") or ""
        transcript = payload.get("transcript_path")
    else:
        prompt = processor.sanitize_surrogates(raw)
        session_id, transcript = "", None
    if not isinstance(session_id, str):
        session_id = str(session_id)
    text = _extract_text(prompt)
    if text.lstrip().startswith(_SYSTEM_PROMPT_PREFIXES):
        _audit({"ts": _utc_iso(), "hook": "UserPromptSubmit", "stage": "skipped", "reason": "system_prompt"})
        _out(context.empty_block())
        return 0
    cfg = config_mod.load_config()
    srv = ensure_server(cfg, max_wait=0)
    if srv:
        try:
            _post(cfg["server_port"], "/repair", {}, timeout=10)
        except Exception as exc:  # noqa: BLE001
            print("N3MC Lite: repair failed (%s: %s)" % (type(exc).__name__, exc), file=sys.stderr)
    if transcript:
        prev = _last_assistant_text(transcript, text)
        if prev:
            turn = _read_turn_id(session_id)
            if not turn:
                turn = str(uuid.uuid4())
                _write_turn_id(session_id, turn)
            _hook_save(cfg, srv, prev, "claude", session_id, turn)
    new_turn = str(uuid.uuid4())
    _write_turn_id(session_id, new_turn)
    if text.strip():
        body = {"query": text, "max_chars": cfg["context_max_chars"], "no_model_wait": True}
        if session_id:
            body["session_id"] = session_id
        _out(_search_text(cfg, srv, body))
        _hook_save(cfg, srv, text, "user", session_id, new_turn)
    else:
        _out(context.empty_block())
    return 0


def cmd_save_claude_turn():
    raw = _stdin_text()
    payload = _parse_payload(raw) or {}
    msg = payload.get("last_assistant_message")
    session_id = payload.get("session_id") or ""
    if isinstance(msg, str) and msg.strip():
        cfg = config_mod.load_config()
        srv = ensure_server(cfg, max_wait=0)
        turn = _read_turn_id(session_id)
        if not turn:
            turn = str(uuid.uuid4())
            _write_turn_id(session_id, turn)
        _hook_save(cfg, srv, msg, "claude", session_id, turn)
    return 0


# ---------------------------------------------------------------------------
# --stop and the rules file
# ---------------------------------------------------------------------------

_RULES_MD_TEMPLATE = '''<!-- n3mc-lite-behavior v1 -->
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
'''

_KNOWN_RULES_MD_HASHES = set()
_CLI_FORM_RE = re.compile(r'(?:"[^"\n]*"|\S+) -m n3mc_mcp\.cli|uvx --from n3memorycore-mcp-lite==\S+ n3mc-lite')


def _project_dir(cwd):
    """The folder for the rules file (spec 12.4 step 1, F3 2026-11-22): $CLAUDE_PROJECT_DIR when it names an
    existing folder (Claude Code sets it for hook commands: the project root), otherwise `cwd`. The hook's
    cwd is the session's current folder, a subfolder after Claude has cd'ed into it."""
    p = os.environ.get("CLAUDE_PROJECT_DIR")
    try:
        if p and os.path.isdir(p):
            return os.path.abspath(p)
    except Exception:  # noqa: BLE001
        pass
    return cwd


def _is_dir_home(folder):
    try:
        return _norm(folder) == _norm(os.path.expanduser("~"))
    except Exception:  # noqa: BLE001
        return False


def shell_path(p):
    s = pathlib.Path(p).as_posix()
    if " " in s and os.name == "nt":
        short = _short_path(p)
        if short and " " not in short:
            s = pathlib.Path(short).as_posix()
    return '"%s"' % s if " " in s else s


def _short_path(p):
    """Windows 8.3 short path of an existing file, or "" (8.3 names may be disabled on the volume)."""
    import ctypes
    buf = ctypes.create_unicode_buffer(32768)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(p), buf, len(buf))
    return buf.value if 0 < n < len(buf) else ""


def _cli_form(uvx):
    if uvx:
        return "uvx --from n3memorycore-mcp-lite==%s n3mc-lite" % __version__
    return "%s -m n3mc_mcp.cli" % shell_path(sys.executable)


def _ensure_rules_md(cwd, uvx=False):
    folder = os.path.join(cwd, ".claude", "rules")
    path = os.path.join(folder, "n3mc-lite-behavior.md")
    want = _RULES_MD_TEMPLATE.replace("{cli}", _cli_form(uvx))
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", newline="") as f:
            cur = f.read().replace("\r\n", "\n")
        if cur == want:
            _clear_notice("rules_md_edited:%s" % hashlib.sha256(os.path.abspath(path).encode()).hexdigest()[:12])
            return
        stripped = _CLI_FORM_RE.sub("{cli}", cur)
        known = stripped == _RULES_MD_TEMPLATE or \
            hashlib.sha256(stripped.encode("utf-8")).hexdigest()[:16] in _KNOWN_RULES_MD_HASHES
        if not known:
            h = hashlib.sha256(os.path.abspath(path).encode("utf-8")).hexdigest()[:12]
            _notify("rules_md_edited:%s" % h, "The rules file %s has been edited by hand, so it was not "
                                              "updated. Merge the new template manually if needed."
                    % os.path.abspath(path))
            return
        shutil.copy2(path, path + ".bak-n3mc-lite-" + datetime.now().strftime("%Y%m%d%H%M%S"))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".rules-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(want)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def cmd_stop(uvx=False):
    cwd = None
    if not sys.stdin.isatty():
        payload = _parse_payload(_stdin_text())
        if payload and isinstance(payload.get("cwd"), str) and payload["cwd"]:
            cwd = payload["cwd"]
    cwd = cwd or os.getcwd()
    project = _project_dir(cwd)
    try:
        if not _is_dir_home(project):
            _ensure_rules_md(project, uvx)
    except Exception as exc:  # noqa: BLE001
        print("N3MC Lite: could not write the rules file (%s: %s)" % (type(exc).__name__, exc), file=sys.stderr)
    try:
        cutoff = time.time() - 30 * 86400
        for name in os.listdir(paths.data_dir()):
            if name.startswith("turn_id") and name.endswith(".txt"):
                fp = os.path.join(paths.data_dir(), name)
                if os.path.getmtime(fp) < cutoff:
                    os.unlink(fp)
    except OSError as exc:
        print("N3MC Lite: turn_id cleanup failed (%s)" % exc, file=sys.stderr)
    try:
        ap = paths.audit_log_path()
        if os.path.exists(ap) and os.path.getsize(ap) > _AUDIT_MAX_BYTES:
            os.replace(ap, ap + ".1")
    except OSError as exc:
        print("N3MC Lite: audit.log rotation failed (%s)" % exc, file=sys.stderr)
    return 0


_AUDIT_MAX_BYTES = 50 * 1024 * 1024


# ---------------------------------------------------------------------------
# --init
# ---------------------------------------------------------------------------

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


def resolve_cmd(script_name: str) -> str:
    folders = [sysconfig.get_path("scripts")]
    try:
        folders.append(sysconfig.get_path("scripts", sysconfig.get_preferred_scheme("user")))
    except (KeyError, ValueError):
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


def permission_rules(python="python"):
    return [r.replace("python -m n3mc_mcp.cli", "%s -m n3mc_mcp.cli" % python) for r in PERMISSION_RULES]


def _scan_old_mcp():
    msgs = []
    name = "n3mc-workingmemory"
    cj = pathlib.Path.home() / ".claude.json"
    try:
        data = json.loads(cj.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict):
        servers = data.get("mcpServers")
        if isinstance(servers, dict) and name in servers:
            msgs.append("An MCP registration of %s was found in Claude Code (%s). Claude Code now uses the "
                        "hooks; remove it with: claude mcp remove %s -s user" % (name, cj, name))
        projects = data.get("projects")
        if isinstance(projects, dict):
            for ppath, proj in projects.items():
                s = proj.get("mcpServers") if isinstance(proj, dict) else None
                if isinstance(s, dict) and name in s:
                    msgs.append("An MCP registration of %s was found in Claude Code (%s, project %s). Claude "
                                "Code now uses the hooks; remove it with: claude mcp remove %s -s local (run "
                                "it in that project)" % (name, cj, ppath, name))
    mj = pathlib.Path(os.getcwd()) / ".mcp.json"
    try:
        data = json.loads(mj.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict) and isinstance(data.get("mcpServers"), dict) and name in data["mcpServers"]:
        msgs.append("An MCP registration of %s was found in Claude Code (%s). Claude Code now uses the hooks; "
                    "remove the \"%s\" entry from %s" % (name, mj, name, mj))
    return msgs


def cmd_init():
    cfg = config_mod.load_config()  # creates config.json
    del cfg
    sp = pathlib.Path.home() / ".claude" / "settings.json"
    existed = sp.exists()
    if existed:
        try:
            settings = json.loads(sp.read_text(encoding="utf-8-sig"))
            if not isinstance(settings, dict):
                raise ValueError("the top level is not a JSON object")
            if "hooks" in settings and not isinstance(settings["hooks"], dict):
                raise ValueError("'hooks' is not an object")
            if "permissions" in settings:
                if not isinstance(settings["permissions"], dict):
                    raise ValueError("'permissions' is not an object")
                if "allow" in settings["permissions"] and not isinstance(settings["permissions"]["allow"], list):
                    raise ValueError("'permissions.allow' is not a list")
        except (OSError, ValueError) as exc:
            print("Could not read %s (%s). Nothing was changed." % (sp, exc))
            return 1
    else:
        settings = {}
    before = json.dumps(settings, sort_keys=True)
    messages = []
    plugin_on = isinstance(settings.get("enabledPlugins"), dict) and         settings["enabledPlugins"].get(PLUGIN_KEY) is True
    hooks = settings.get("hooks", {})
    for event, (exe_name, module) in OURS.items():
        groups = hooks.get(event, []) if isinstance(hooks, dict) else []
        if not isinstance(groups, list):
            groups = []
        desired = {"type": "command", "command": resolve_cmd(exe_name), "timeout": 60}
        found = False
        new_groups = []
        for g in groups:
            hl = g.get("hooks") if isinstance(g, dict) else None
            if not isinstance(hl, list):
                new_groups.append(g)
                continue
            kept = []
            for h in hl:
                cmd = h.get("command", "") if isinstance(h, dict) else ""
                if isinstance(h, dict) and cmd and is_own_hook(cmd, event):
                    if plugin_on:
                        continue
                    if not found and h == desired:
                        found = True
                        kept.append(h)
                    # any other own entry is replaced
                    continue
                if isinstance(h, dict) and "n3mc" in cmd.lower():
                    messages.append("Another N3MC hook is registered for %s and was left in place: %s"
                                    % (event, cmd))
                kept.append(h)
            if kept:
                ng = dict(g)
                ng["hooks"] = kept
                new_groups.append(ng)
        if not plugin_on and not found:
            new_groups.append({"hooks": [desired]})
        if new_groups:
            hooks[event] = new_groups
        elif event in hooks:
            del hooks[event]
    if hooks or "hooks" in settings:
        settings["hooks"] = hooks
    perms = settings.setdefault("permissions", {})
    allow = perms.setdefault("allow", [])
    for rule in permission_rules(shell_path(sys.executable)):
        if rule not in allow:
            allow.append(rule)
    after = json.dumps(settings, sort_keys=True)
    if after != before:
        sp.parent.mkdir(parents=True, exist_ok=True)
        if existed:
            shutil.copy2(sp, str(sp) + ".bak-n3mc-lite-" + datetime.now().strftime("%Y%m%d%H%M%S"))
        fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=str(sp.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump(settings, f, indent=2, ensure_ascii=False)
                f.write("\n")
            os.replace(tmp, sp)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    for event in OURS:
        cmd = resolve_cmd(OURS[event][0])
        if cmd.startswith('"'):
            messages.append("The hook command for %s starts with a quote because %s contains a space; if "
                            "the hook does not run, install Python into a folder without spaces."
                            % (event, cmd.strip('"')))
    messages.extend(_scan_old_mcp())
    for m in messages:
        print(m)
    if plugin_on:
        print("The n3mc-workingmemory plugin is enabled and registers the N3MC Lite hooks, so n3mc-lite "
              "--init added no hooks.")
    else:
        print("N3MC Lite hooks registered in %s. Saving starts with the next Claude Code session." % sp)
    return 0


# ---------------------------------------------------------------------------
# Tool commands
# ---------------------------------------------------------------------------


def _unavailable_text(name, reason, nudge=False):
    if name in ("search_memory", "list_memories", "recall_thread"):
        text = ("Error: memory backend unreachable. The N3MC Lite memory server is not available (%s). It is "
                "started automatically; retry in a minute." % reason)
    elif name in ("delete_memory", "delete_memories_by_session"):
        text = json.dumps({"status": "error", "reason": "The N3MC Lite memory server is not available (%s). "
                           "Nothing was deleted; retry in a minute." % reason}, ensure_ascii=False)
    else:
        text = json.dumps({"status": "error", "message": "The N3MC Lite memory server is not available (%s). "
                           "Retry in a minute." % reason}, ensure_ascii=False)
    if nudge:
        from .tools import _NUDGE_GENERIC
        text += _NUDGE_GENERIC
    return text


def direct_save_text(cfg, args, reason, nudge=False):
    """Direct save for MCP/CLI when the server is not available (spec 4.6): JSON text."""
    res = direct_save(cfg, dict(args))
    res.pop("_unreachable", None)
    res.pop("_failed", None)
    if res.get("saved"):
        res["warning"] = "embedding_unavailable"
        res["detail"] = ("The N3MC Lite memory server was not available (%s), so this entry was saved "
                         "without a vector. It is found by keywords now and gets its vector from "
                         "repair_memory once the server runs." % reason)
    text = json.dumps(res, ensure_ascii=False)
    if nudge:
        from .tools import _NUDGE_AFTER_SAVE, _NUDGE_GENERIC
        text += _NUDGE_AFTER_SAVE if res.get("saved") or res.get("status") in ("duplicate", "skipped_code") \
            else _NUDGE_GENERIC
    return text


def run_tool(cfg, name, args):
    """CLI tool command: ensure the server, POST /tools/<name>?nudge=0, return the answer text."""
    srv = ensure_server(cfg)
    if srv:
        try:
            return _post(cfg["server_port"], "/tools/%s?nudge=0" % name, args, timeout=300)["text"]
        except Exception as exc:  # noqa: BLE001
            reason = "%s: %s" % (type(exc).__name__, exc)
    else:
        reason = unavailable_reason(cfg)
    if name == "save_memory":
        return direct_save_text(cfg, args, reason)
    return _unavailable_text(name, reason)


def cmd_stop_server():
    cfg = config_mod.load_config()
    h = _health(cfg["server_port"])
    if h and h.get("root") and _norm(h["root"]) == _norm(_own_root()):
        pid = h.get("pid")
        try:
            _request_shutdown(cfg["server_port"])
        except Exception:  # noqa: BLE001
            pass
        _wait_port_free(cfg["server_port"], 15)
        print("Stopped the N3MC Lite server (pid %s)." % pid)
    else:
        print("No N3MC Lite server of this data folder is running.")
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="n3mc-lite", allow_abbrev=False,
                                description="N3MemoryCore MCP-Lite CLI (7-day working memory).")
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
    p.add_argument("--before", type=int)
    p.add_argument("--after", type=int)
    p.add_argument("--agent-name")
    p.add_argument("--session-id")
    p.add_argument("--turn-id")
    p.add_argument("--importance")
    p.add_argument("--uvx", action="store_true")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    for k in ("search", "buffer", "recall_thread", "delete", "delete_session", "since", "until",
              "agent_name", "session_id", "turn_id", "importance"):
        v = getattr(args, k, None)
        if isinstance(v, str):
            setattr(args, k, processor.sanitize_surrogates(v))
    if args.run_server:
        run_server()
        return 0
    if args.init:
        return cmd_init()
    if args.hook_submit:
        return cmd_hook_submit(args.uvx)
    if args.save_claude_turn:
        return cmd_save_claude_turn()
    if args.stop:
        return cmd_stop(args.uvx)
    if args.stop_server:
        return cmd_stop_server()
    cfg = config_mod.load_config()
    if args.search is not None:
        from .tools import TimeRangeError, parse_time
        try:
            s = parse_time(args.since, False, "since")
            u = parse_time(args.until, True, "until")
            if s is not None and u is not None and s > u:
                raise TimeRangeError("Error: since is later than until")
        except TimeRangeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        srv = ensure_server(cfg)
        body = {"query": args.search}
        for k in ("since", "until", "limit"):
            if getattr(args, k) is not None:
                body[k] = getattr(args, k)
        sid = os.environ.get("N3MC_SESSION_ID")
        if sid:
            body["session_id"] = sid
        text = _search_text(cfg, srv, body)
        if text.startswith("ERR400:"):
            print(text[7:], file=sys.stderr)
            return 2
        _out(text)
        return 0
    if args.buffer is not None:
        text = args.buffer
        if text == "-":
            text = processor.sanitize_surrogates(_stdin_text())
        a = {"content": text, "agent_name": args.agent_name or AGENT_NAME}
        if args.session_id:
            a["session_id"] = args.session_id
        if args.turn_id:
            a["turn_id"] = args.turn_id
        if args.importance is not None:
            a["importance"] = args.importance
        _out(run_tool(cfg, "save_memory", a))
        return 0
    if args.list:
        a = {"limit": args.limit} if args.limit is not None else {}
        _out(run_tool(cfg, "list_memories", a))
        return 0
    if args.recall_thread is not None:
        a = {"turn_id": args.recall_thread}
        if args.before is not None:
            a["before"] = args.before
        if args.after is not None:
            a["after"] = args.after
        _out(run_tool(cfg, "recall_thread", a))
        return 0
    if args.delete is not None:
        _out(run_tool(cfg, "delete_memory", {"id": args.delete}))
        return 0
    if args.delete_session is not None:
        _out(run_tool(cfg, "delete_memories_by_session", {"session_id": args.delete_session}))
        return 0
    if args.repair:
        _out(run_tool(cfg, "repair_memory", {}))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
