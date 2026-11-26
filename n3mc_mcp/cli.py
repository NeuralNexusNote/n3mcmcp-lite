"""CLI `n3mc-lite` (spec §13): registration in Claude Code (--init, §8, §12.8), the hook commands
(--hook-submit, --save-claude-turn, --stop, §12), the tool commands, and the resident server
(--run-server, --stop-server; ensure_server, _start_server, _notify, §3.15)."""
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
import copy  # noqa: E402
import datetime  # noqa: E402
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
import sys  # noqa: E402
import sysconfig  # noqa: E402
import time  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402
import uuid  # noqa: E402

import n3mc_mcp  # noqa: E402
from n3mc_mcp import __version__, paths  # noqa: E402
from n3mc_mcp import config as _config  # noqa: E402
from n3mc_mcp.processor import TimeBoundError, parse_time_bounds, sanitize_surrogates  # noqa: E402

PKG = "n3memorycore-mcp-lite"
MCP_NAME = "n3mc-workingmemory"
PLUGIN_MCP = "n3mc-workingmemory-mcp@neuralnexusnote"
PLUGIN_FULL = "n3mc-workingmemory@neuralnexusnote"
SOURCE_LINE = "Source: N3MC Lite (7-day working memory)"
EMPTY_BLOCK = f"# Recalled Memory Context\n{SOURCE_LINE}\n\n_No memories found._"
_SYSTEM_PROMPT_PREFIXES = ("<task-notification>", "<agent-message from=", "<system-reminder>")
START_WAIT = 60
START_FAILED_WINDOW = 300

_notify = paths.notify
_clear_notice = paths.clear_notice

# Set by ensure_server for the caller's fallback text.
_LAST_REASON = None
_FOREIGN_SERVER_ROOT = None


def _err(msg: str) -> None:
    try:
        print(msg, file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


# ── HTTP to the resident server (never through a proxy, §3.15) ───────────────

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http(cfg, method, path, body=None, timeout=5.0, port=None):
    port = int(port or cfg.get("server_port", 18524))
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                 headers={"Host": f"127.0.0.1:{port}", "Content-Type": "application/json"})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read() or b""
        status = e.code
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        parsed = {}
    return status, parsed


def _get(cfg, path, timeout=5.0, port=None):
    return _http(cfg, "GET", path, None, timeout, port)


def _post(cfg, path, body, timeout=30.0, port=None):
    return _http(cfg, "POST", path, body if body is not None else {}, timeout, port)


def _own_root() -> str:
    return os.path.normcase(os.path.abspath(paths.data_dir()))


def _health(cfg, port=None):
    try:
        status, data = _get(cfg, "/health", 5.0, port)
    except Exception:  # noqa: BLE001
        return None
    return data if status == 200 and isinstance(data, dict) else None


def _request_shutdown(cfg, root, port=None) -> bool:
    try:
        status, _ = _post(cfg, "/shutdown", {"root": root}, 5.0, port)
        return status == 200
    except Exception:  # noqa: BLE001
        return False


def _port_in_use(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.3):
            return True
    except OSError:
        return False


def _wait_port_free(port: int, total: float = 15.0) -> bool:
    deadline = time.monotonic() + total
    while True:
        if not _port_in_use(port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.3)


def classify_request_error(e: BaseException):
    """(reason, sent): sent=False only when the request surely did not reach the server."""
    reason = f"{type(e).__name__}: {e}"
    inner = getattr(e, "reason", None)
    if isinstance(e, ConnectionRefusedError) or isinstance(inner, ConnectionRefusedError):
        return reason, False
    if isinstance(e, urllib.error.URLError) and isinstance(inner, OSError) and getattr(inner, "winerror", None) == 10061:
        return reason, False
    return reason, True


# ── server identity and replacement (§3.15) ───────────────────────────────────

def own_code_version(cfg) -> str:
    return _config.compute_code_version(cfg.get("_raw"))


def _health_decision(health, cfg=None) -> str:
    if not health:
        return "none"
    root = health.get("root")
    if not root:
        return "legacy"
    if os.path.normcase(os.path.abspath(root)) != _own_root():
        return "foreign"
    if cfg is not None and health.get("code_version") != own_code_version(cfg):
        return "stale"
    return "ok"


def _proc_data_root(proc) -> str:
    try:
        env = proc.environ()
    except Exception:  # noqa: BLE001
        return None
    d = env.get("N3MC_DATA_DIR")
    if d:
        return os.path.normcase(os.path.abspath(os.path.expanduser(d)))
    return os.path.normcase(os.path.join(os.path.expanduser("~"), ".n3mc-workingmemory"))


def _is_own_server_process(pid) -> bool:
    if not pid:
        return False
    try:
        import psutil
        p = psutil.Process(int(pid))
        cmd = p.cmdline()
    except Exception:  # noqa: BLE001
        return False
    joined = " ".join(cmd)
    if "n3mc_mcp.cli" not in joined or "--run-server" not in cmd:
        return False
    return _proc_data_root(p) == _own_root()


def _listening_pid(port):
    try:
        import psutil
        for c in psutil.net_connections(kind="tcp"):
            if c.laddr and c.laddr.port == int(port) and c.status == psutil.CONN_LISTEN:
                return c.pid
    except Exception:  # noqa: BLE001
        return None
    return None


def _listening_ports(pid):
    try:
        import psutil
        return sorted({c.laddr.port for c in psutil.net_connections(kind="tcp")
                       if c.pid == int(pid) and c.status == psutil.CONN_LISTEN and c.laddr})
    except Exception:  # noqa: BLE001
        return []


def _terminate(pid) -> None:
    try:
        import psutil
        p = psutil.Process(int(pid))
        p.terminate()
        try:
            p.wait(5)
        except psutil.TimeoutExpired:
            p.kill()
    except Exception:  # noqa: BLE001
        pass


def _stop_stale_server(cfg, health) -> bool:
    port = int(cfg.get("server_port", 18524))
    if _request_shutdown(cfg, _own_root()):
        return _wait_port_free(port)
    pid = (health or {}).get("pid") or _listening_pid(port)
    if pid and _is_own_server_process(pid):
        _terminate(pid)
        return _wait_port_free(port)
    _notify("stale_server", f"Cannot stop the old N3MC Lite server. Another program may be using "
                            f"server_port {port}. Check server_port in config.json and restart manually.")
    return False


def _read_pid_file():
    """(pid or None, mtime) of the PID file, or (None, None) when there is none."""
    path = paths.pid_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read().strip()
        return (int(text) if text.isdigit() else None), mtime
    except OSError:
        return None, mtime


def _remove_pid_file(expect_pid=None) -> None:
    path = paths.pid_path()
    if expect_pid is not None:
        pid, _ = _read_pid_file()
        if pid != expect_pid:
            return
    try:
        os.unlink(path)
    except OSError:
        pass


def _write_own_pid() -> None:
    path = paths.pid_path()
    try:
        paths.ensure_data_dir()
        try:
            with open(path, "x", encoding="utf-8") as f:
                f.write(str(os.getpid()))
        except FileExistsError:
            with open(path, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
    except OSError:
        pass


def _pid_is_starting(pid, mtime) -> bool:
    if pid is None:
        return mtime is not None and time.time() - mtime < START_WAIT
    try:
        import psutil
    except ImportError:
        return mtime is not None and time.time() - mtime < START_WAIT
    try:
        if not psutil.pid_exists(int(pid)):
            return False
    except Exception:  # noqa: BLE001
        return False
    return _is_own_server_process(pid)


def _handle_other_port(cfg, pid) -> bool:
    """The PID file's server listens on another port (server_port changed): stop it there."""
    port = int(cfg.get("server_port", 18524))
    for p in _listening_ports(pid):
        if p == port:
            continue
        h = _health(cfg, port=p)
        if not h or not h.get("root") or os.path.normcase(os.path.abspath(h["root"])) != _own_root():
            continue
        _request_shutdown(cfg, _own_root(), port=p)
        _wait_port_free(p)
        _remove_pid_file(expect_pid=int(pid))
        return True
    return False


def _marker_recent() -> bool:
    try:
        return time.time() - os.path.getmtime(paths.start_failed_path()) < START_FAILED_WINDOW
    except OSError:
        return False


def _record_start_failure(detail: str) -> None:
    _notify("server_start_failed", f"The N3MC Lite memory server could not start ({detail}). "
                                   f"See {paths.server_log_path()} for the reason.")
    try:
        paths.ensure_data_dir()
        with open(paths.start_failed_path(), "w", encoding="utf-8") as f:
            f.write(paths.utc_now_iso())
    except OSError:
        pass


def _server_ok(cfg, health) -> bool:
    global _LAST_REASON
    _LAST_REASON = None
    for k in ("stale_server", "port_conflict", "server_start_failed"):
        _clear_notice(k)
    try:
        os.unlink(paths.start_failed_path())
    except OSError:
        pass
    own = _config.redis_display(cfg.get("redis_url", ""))
    theirs = (health or {}).get("redis")
    if theirs and theirs != own:
        _notify("redis_url_mismatch",
                f"The N3MC Lite server uses Redis at {theirs}, but this process is set to {own}. Put "
                f"redis_url into {paths.config_path()} instead of setting N3MC_REDIS_URL per application, "
                "then restart the server (it restarts after config.json changes).")
    else:
        _clear_notice("redis_url_mismatch")
    return True


def reason_starting() -> str:
    return "the memory server is still starting (the first start downloads the embedding model, ~1.1 GB)"


def reason_failed() -> str:
    return f"the memory server could not start; see {paths.server_log_path()}"


def reason_foreign(cfg) -> str:
    return f"another N3MC folder's server is using port {cfg.get('server_port', 18524)}"


REASON_STALE = "an old N3MC Lite memory server could not be stopped"


def unavailable_reason(cfg) -> str:
    return _LAST_REASON or reason_starting()


def _wait_health(cfg, max_wait, proc=None) -> bool:
    deadline = time.monotonic() + max(0.0, float(max_wait))
    while True:
        h = _health(cfg)
        if _health_decision(h, cfg) == "ok":
            return _server_ok(cfg, h)
        if proc is not None and proc.poll() is not None:
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def ensure_server(cfg, max_wait=START_WAIT) -> bool:
    """True when a healthy server of this data folder answers (starting one if needed)."""
    global _LAST_REASON, _FOREIGN_SERVER_ROOT
    _FOREIGN_SERVER_ROOT = None
    port = int(cfg.get("server_port", 18524))
    h = _health(cfg)
    d = _health_decision(h, cfg)
    if d == "legacy":
        pid = h.get("pid") or _listening_pid(port)
        if _is_own_server_process(pid):
            d = "stale"
        else:
            h = dict(h, root="(unknown: the server did not report its folder)")
            d = "foreign"
    if d == "ok":
        return _server_ok(cfg, h)
    if d == "foreign":
        _FOREIGN_SERVER_ROOT = h.get("root")
        _LAST_REASON = reason_foreign(cfg)
        _notify("port_conflict", f"Another N3MC server ({h.get('root')}) is using port {port}. Give N3MC "
                                 f"Lite its own server_port in {paths.config_path()}.")
        return False
    if d == "stale":
        if not _stop_stale_server(cfg, h):
            h2 = _health(cfg)
            d2 = _health_decision(h2, cfg)
            if d2 == "ok":
                return _server_ok(cfg, h2)
            if d2 != "none":
                _LAST_REASON = REASON_STALE
                _notify("stale_server", f"Cannot stop the old N3MC Lite server. Another program may be "
                                        f"using server_port {port}. Check server_port in config.json and "
                                        "restart manually.")
                return False
    # No server answers.
    pid, mtime = _read_pid_file()
    if mtime is not None:
        if _pid_is_starting(pid, mtime):
            if pid and _handle_other_port(cfg, pid):
                pass  # the old server on another port was stopped: start a new one below
            else:
                if _wait_health(cfg, min(max_wait, START_WAIT)):
                    return True
                _LAST_REASON = reason_starting()
                return False
        else:
            _remove_pid_file()
    if _marker_recent():
        _LAST_REASON = reason_failed()
        return False
    return _start_server(cfg, max_wait)


def _start_server(cfg, max_wait=START_WAIT) -> bool:
    global _LAST_REASON
    paths.ensure_data_dir()
    pid_path = paths.pid_path()
    try:
        fh = open(pid_path, "x", encoding="utf-8")
    except FileExistsError:
        if _wait_health(cfg, min(max_wait, START_WAIT)):
            return True
        _LAST_REASON = reason_starting()
        return False
    except OSError as e:
        _err(f"n3mc-lite: could not create the PID file ({e})")
        _LAST_REASON = reason_failed()
        return False
    try:
        log = paths.server_log_path()
        try:
            if os.path.getsize(log) > 1024 * 1024:
                open(log, "wb").close()
        except OSError:
            pass
        logf = open(log, "ab")
        env = dict(os.environ)
        env["N3MC_DATA_DIR"] = paths.data_dir()
        cwd = os.getcwd() if paths.package_parent_is_cwd() else paths.data_dir()
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen([*paths.python_cmd(), "-m", "n3mc_mcp.cli", "--run-server"],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=logf,
                                    cwd=cwd, env=env, **kwargs)
        finally:
            logf.close()
        fh.write(str(proc.pid))
        fh.close()
    except Exception as e:  # noqa: BLE001
        try:
            fh.close()
        except Exception:  # noqa: BLE001
            pass
        _remove_pid_file()
        _err(f"n3mc-lite: could not start the memory server ({type(e).__name__}: {e})")
        _LAST_REASON = reason_failed()
        return False
    if _wait_health(cfg, max_wait, proc):
        return True
    code = proc.poll()
    if code is not None:
        _record_start_failure(f"exit code {code}")
        _remove_pid_file(expect_pid=proc.pid)
        _LAST_REASON = reason_failed()
        return False
    _LAST_REASON = reason_starting()
    return False


# ── the resident server process (§3.15, §13 --run-server) ────────────────────

def run_server() -> None:
    cfg = _config.load_config()
    code_version = _config.compute_code_version(cfg.get("_raw"))
    try:
        _write_own_pid()
        host = cfg.get("bind_host", "127.0.0.1")
        if host != "127.0.0.1":
            _err(f"n3mc-lite: WARNING bind_host is {host}; the memory server has no authentication and "
                 "your memories may be reachable from the network. Keep 127.0.0.1.")
        from n3mc_mcp import processor
        processor.import_backend()
        from n3mc_mcp.database import Database
        db = Database(cfg)
        status = db.ensure_connected()
        if status != "ok":
            _err(f"n3mc-lite: {status}")
        from n3mc_mcp import app as app_mod
        application = app_mod.create_app(cfg, db, code_version=code_version)
        import uvicorn
        _write_own_pid()
        uvicorn.run(application, host=host, port=int(cfg.get("server_port", 18524)), log_level="error")
    except SystemExit as e:
        if e.code not in (0, None):
            _record_start_failure(f"exit code {e.code}")
        raise
    except KeyboardInterrupt:
        raise
    except BaseException as e:  # noqa: BLE001
        _record_start_failure(f"{type(e).__name__}: {e}")
        raise
    finally:
        try:
            from n3mc_mcp.app import remove_pid_file_if_own
            remove_pid_file_if_own()
        except Exception:  # noqa: BLE001
            pass


# ── unavailable server: texts and the direct save (§3.16, §4.6) ──────────────

def server_unavailable_detail(reason: str) -> str:
    return (f"The N3MC Lite memory server was not available ({reason}), so this entry was saved without a "
            "vector. It is found by keywords now and gets its vector from repair_memory once the server runs.")


def unavailable_text(name: str, reason: str, sent: bool = False) -> str:
    """The §4.6 table text without a reminder (save_memory is handled by direct_save_text)."""
    if name in ("search_memory", "list_memories", "recall_thread"):
        return (f"Error: memory backend unreachable. The N3MC Lite memory server is not available ({reason}). "
                "It is started automatically; retry in a minute.")
    if name in ("delete_memory", "delete_memories_by_session"):
        if sent:
            return json.dumps({"status": "unknown", "reason": (
                f"The request reached the N3MC Lite memory server but no answer came back ({reason}). The "
                "delete may have completed; check with list_memories or search_memory before deleting again.")},
                ensure_ascii=False)
        return json.dumps({"status": "error", "reason": (
            f"The N3MC Lite memory server is not available ({reason}). Nothing was deleted; retry in a minute.")},
            ensure_ascii=False)
    if name == "repair_memory":
        return json.dumps({"status": "error", "message": (
            f"The N3MC Lite memory server is not available ({reason}). Retry in a minute.")}, ensure_ascii=False)
    return f"Error: unknown tool '{name}'"


def direct_save(cfg, content, embed_detail=None, **kw):
    """Save straight into Redis without a vector. (result dict, None) or (None, hint)."""
    from n3mc_mcp.database import Database
    import redis
    db = Database(cfg)
    try:
        status = db.ensure_connected(ephemeral=False)
        if status != "ok":
            return None, status
        try:
            return db.save_memory(content, embed=False, embed_detail=embed_detail, **kw), None
        except (redis.ConnectionError, redis.TimeoutError):
            return None, db.hint()
    finally:
        if db.client is not None:
            try:
                db.client.close()
            except Exception:  # noqa: BLE001
                pass


def direct_save_text(cfg, args: dict, reason: str):
    """The MCP save_memory answer without a server (§4.6): (JSON text, ok)."""
    owner = args.get("owner_id")
    if owner not in (None, "") and owner != cfg.get("owner_id"):
        return json.dumps({"status": "error", "saved": False, "reason": "owner_id mismatch"}), False
    imp = args.get("importance", 1.0)
    if imp is not None:
        try:
            if isinstance(imp, bool):
                raise ValueError
            v = float(imp)
            if v != v or v in (float("inf"), float("-inf")):
                raise ValueError
        except (TypeError, ValueError):
            return json.dumps({"saved": False, "status": "error",
                               "reason": f"importance must be numeric (got {imp})"}), False
    res, hint = direct_save(cfg, args.get("content"), embed_detail=server_unavailable_detail(reason),
                            agent_name=args.get("agent_name"), owner_id=owner, importance=imp,
                            session_id=args.get("session_id"), turn_id=args.get("turn_id"))
    if res is None:
        return json.dumps({"status": "error", "saved": False, "reason": hint}, ensure_ascii=False), False
    return json.dumps(res, ensure_ascii=False), res.get("status") != "error"


# ── hooks (§12) ───────────────────────────────────────────────────────────────

def _audit(record: dict) -> None:
    record = dict(record)
    record.setdefault("ts", paths.utc_now_iso())
    paths.append_audit(record)


def _parse_stdin(raw: str):
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    return sanitize_surrogates(payload)


def _extract_text(prompt) -> str:
    def _parts(lst):
        return "\n".join(p.get("text", "") for p in lst
                         if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str))
    if isinstance(prompt, list):
        return _parts(prompt)
    if isinstance(prompt, str):
        s = prompt.strip()
        if s.startswith("["):
            try:
                arr = json.loads(s)
            except ValueError:
                arr = None
            if (isinstance(arr, list) and arr
                    and all(isinstance(p, dict) and isinstance(p.get("type"), str) for p in arr)):
                return _parts(arr)
        return prompt
    return "" if prompt is None else str(prompt)


def _read_turn_id(session_id, create: bool = True) -> str:
    path = paths.turn_id_path(session_id)
    try:
        with open(path, "r", encoding="utf-8") as f:
            tid = f.read().strip()
        if tid:
            return tid
    except OSError:
        pass
    tid = str(uuid.uuid4())
    if create:
        _write_turn_id(session_id, tid)
    return tid


def _write_turn_id(session_id, tid) -> None:
    try:
        paths.ensure_data_dir()
        paths.atomic_write_text(paths.turn_id_path(session_id), tid)
    except OSError as e:
        _err(f"n3mc-lite: could not write the turn id ({e})")


def _user_text(msg):
    content = (msg or {}).get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [p.get("text") for p in content
                 if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)]
        if texts:
            return "\n".join(texts)
    return None


def _is_real_prompt(obj, text) -> bool:
    if obj.get("isMeta") is True or text is None:
        return False
    if text.startswith(("[Request interrupted by user", "<command-", "<local-command-")):
        return False
    return not text.startswith(_SYSTEM_PROMPT_PREFIXES)


def _read_previous_response(transcript_path, prompt: str):
    """Claude's previous answer from the transcript (§12.5), or None."""
    try:
        if not isinstance(transcript_path, str) or not transcript_path:
            raise ValueError("no transcript_path")
        size = os.path.getsize(transcript_path)
        start = max(0, size - 5 * 1024 * 1024)
        with open(transcript_path, "rb") as f:
            f.seek(start)
            data = f.read()
        lines = data.decode("utf-8", "replace").splitlines()
        if start > 0 and lines:
            lines = lines[1:]
        newest = True
        for line in reversed(lines):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict) or obj.get("isSidechain") is True:
                continue
            t = obj.get("type")
            if t not in ("user", "assistant"):
                continue
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            if t == "user":
                text = _user_text(msg)
                if not _is_real_prompt(obj, text):
                    continue
                if newest:
                    newest = False
                    if text == prompt:
                        continue
                return None
            newest = False
            content = msg.get("content")
            texts = []
            if isinstance(content, str) and content.strip():
                texts = [content]
            elif isinstance(content, list):
                texts = [p["text"] for p in content if isinstance(p, dict) and p.get("type") == "text"
                         and isinstance(p.get("text"), str) and p["text"].strip()]
            if texts:
                return sanitize_surrogates(texts[-1])
        return None
    except Exception as e:  # noqa: BLE001
        _audit({"hook": "UserPromptSubmit", "stage": "transcript_unreadable", "reason": f"{type(e).__name__}: {e}"[:300]})
        return None


def _redis_unreachable(cfg) -> None:
    _notify("redis_unreachable", f"N3MC Lite could not reach Redis, so a message was kept only in "
                                 f"{paths.audit_log_path()}. " + _config.backend_hint(cfg.get("redis_url", "")))


def _after_save(res) -> None:
    if isinstance(res, dict):
        if res.get("status") in ("saved", "duplicate"):
            _clear_notice("redis_unreachable")
            _clear_notice("save_failed")
        elif res.get("status") == "error":
            _err(f"n3mc-lite: {res.get('reason')}")


def _hook_save(cfg, server_ok: bool, content: str, role: str, session_id, turn_id) -> None:
    body = {"content": content, "role": role, "agent_name": "claude-code", "session_id": session_id or "",
            "turn_id": turn_id, "no_model_wait": True}
    if server_ok:
        try:
            status, data = _post(cfg, "/buffer", body, 30.0)
            if status == 200:
                _after_save(data)
                return
            if status == 503:
                _redis_unreachable(cfg)
                return
        except Exception:  # noqa: BLE001
            pass
    try:
        res, hint = direct_save(cfg, content, role=role, agent_name="claude-code",
                                session_id=session_id or None, turn_id=turn_id)
    except Exception as e:  # noqa: BLE001
        _notify("save_failed", f"A record could not be saved ({type(e).__name__}: {e}). Current memories may be lost.")
        return
    if res is None:
        _redis_unreachable(cfg)
        return
    _after_save(res)


def _fallback_line(cfg) -> str:
    if _FOREIGN_SERVER_ROOT is not None:
        return f"_(memory search unavailable: {reason_foreign(cfg)})_"
    return f"_(memory search unavailable: {unavailable_reason(cfg)})_"


def _fallback(cfg, line: str) -> str:
    try:
        notices = paths.take_due_notices()
    except Exception:  # noqa: BLE001
        notices = []
    from n3mc_mcp.context import fallback_block
    return fallback_block(line, notices)


def _search_text(cfg, server_ok: bool, body: dict) -> str:
    if not server_ok:
        return _fallback(cfg, _fallback_line(cfg))
    try:
        status, data = _post(cfg, "/search", body, 30.0)
    except Exception as e:  # noqa: BLE001
        return _fallback(cfg, f"_(memory search unavailable: {type(e).__name__}: {e})_")
    if status == 200 and isinstance(data.get("text"), str):
        return data["text"]
    if status == 503:
        _redis_unreachable(cfg)
        return _fallback(cfg, f"_(memory search unavailable: {data.get('detail') or _config.backend_hint(cfg.get('redis_url', ''))})_")
    if status == 400:
        raise TimeBoundError(data.get("detail") or "Error: bad request")
    return _fallback(cfg, f"_(memory search unavailable: HTTPError: HTTP {status})_")


def hook_submit(raw: str, uvx: bool = False) -> int:
    payload = _parse_stdin(raw)
    if isinstance(payload, dict):
        prompt = payload.get("prompt", payload.get("message"))
        session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else ""
        transcript = payload.get("transcript_path")
    else:
        prompt, session_id, transcript = sanitize_surrogates(raw), "", None
    text = _extract_text(prompt)
    if text.startswith(_SYSTEM_PROMPT_PREFIXES):
        _audit({"hook": "UserPromptSubmit", "stage": "skipped", "reason": "system_prompt"})
        print(EMPTY_BLOCK)
        return 0
    cfg = _config.load_config()
    ok = ensure_server(cfg, max_wait=0)
    if ok:
        try:
            _post(cfg, "/repair", {}, 10.0)
        except Exception as e:  # noqa: BLE001
            _err(f"n3mc-lite: repair failed ({type(e).__name__}: {e})")
    prev = _read_previous_response(transcript, text)
    if prev and prev.strip():
        _hook_save(cfg, ok, prev, "claude", session_id, _read_turn_id(session_id, create=True))
    new_turn = str(uuid.uuid4())
    _write_turn_id(session_id, new_turn)
    try:
        if text.strip():
            out = _search_text(cfg, ok, {"query": text, "session_id": session_id,
                                         "max_chars": int(cfg.get("context_max_chars", 9500)),
                                         "no_model_wait": True})
        else:
            out = EMPTY_BLOCK
    except Exception as e:  # noqa: BLE001
        out = f"# Recalled Memory Context\n{SOURCE_LINE}\n\n_(memory search unavailable: {type(e).__name__})_"
    try:
        print(out)
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass
    if text.strip():
        _hook_save(cfg, ok, text, "user", session_id, new_turn)
    return 0


def save_claude_turn(raw: str, uvx: bool = False) -> int:
    payload = _parse_stdin(raw)
    if not isinstance(payload, dict):
        return 0
    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else ""
    turn = _read_turn_id(session_id, create=True)   # before load_config and ensure_server (§12.4)
    msg = payload.get("last_assistant_message")
    if not isinstance(msg, str) or not msg.strip():
        return 0
    cfg = _config.load_config()
    ok = ensure_server(cfg, max_wait=0)
    _hook_save(cfg, ok, msg, "claude", session_id, turn)
    return 0


def _project_dir(payload) -> str:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env and os.path.isdir(env):
        return env
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    if isinstance(cwd, str) and cwd and os.path.isdir(cwd):
        return cwd
    return os.getcwd()


def stop(raw: str, uvx: bool = False) -> int:
    payload = _parse_stdin(raw)
    try:
        project = _project_dir(payload)
        home = os.path.normcase(os.path.abspath(os.path.expanduser("~")))
        if os.path.normcase(os.path.abspath(project)) != home:
            _ensure_rules_md(project, uvx)
    except Exception as e:  # noqa: BLE001
        _err(f"n3mc-lite: could not update the rules file ({type(e).__name__}: {e})")
    try:
        d = paths.data_dir()
        cutoff = time.time() - 30 * 86400
        if os.path.isdir(d):
            for name in os.listdir(d):
                if name.startswith("turn_id") and name.endswith(".txt"):
                    p = os.path.join(d, name)
                    if os.path.getmtime(p) < cutoff:
                        os.unlink(p)
    except Exception as e:  # noqa: BLE001
        _err(f"n3mc-lite: could not clean the turn id files ({type(e).__name__}: {e})")
    try:
        paths.rotate_audit_log()
    except Exception as e:  # noqa: BLE001
        _err(f"n3mc-lite: could not rotate audit.log ({type(e).__name__}: {e})")
    return 0


# ── rules file (§12.9) ────────────────────────────────────────────────────────

_RULES_MD_TEMPLATE = '<!-- n3mc-lite-behavior v1 -->\n# N3MemoryCore Lite Behavioral Guidelines\n\nThese guidelines apply to every Claude Code session in this project. They are\nloaded automatically from `.claude/rules/` at session start. N3MC Lite is a\nworking memory: every record is deleted 7 days after it was saved.\n\n## Fully Automatic Saving\nAll conversations are saved by hooks (UserPromptSubmit + Stop). Do NOT call\n`--buffer` for them. Every non-empty user message and Claude response is\nsaved (fenced code blocks as `[code omitted]`); there is no length filter and\nno skip-pattern filter. Make NO acknowledgement when a save succeeds —\nsilence is correct. Run `{cli} --buffer "<text>"` only when the user asks\nyou to remember something specific.\n\n## Recalled Memory Context\nEvery turn, the UserPromptSubmit hook adds a `# Recalled Memory Context` block\nto your context. Each hit starts with `### ` and shows its `saved=` time and,\nwhen it has one, `turn=<turn_id>`; a `[doc×N]` hit is a long text shown in\nfull. When several versions of the same content appear, the one with the\nnewest `saved` time is the current one. If the block contains a\n`> ⚠️ N3MC notice:` line, tell the user what it says.\n\n## Weak Matches\nRecords under `#### Possibly related (weak matches — may be unrelated)` did\nnot pass the relevance floors and are often unrelated. Use one only when it\nis actually relevant to the question; if it is not, ignore it and do not\nmention it.\n\n## Active RAG\nWhen prior context would help, run a search proactively. **Always invoke via\n`{cli} --search "<keywords>"`** (optional `--since 2026-11-13`,\n`--until 2026-11-14`). To read the conversation around a hit, run\n`{cli} --recall-thread <turn_id>`. `{cli} --list` shows the newest records.\nThese commands are auto-allowed via `permissions.allow`.\n\n## Cut Context\nThe block is kept within a size limit. If it says that lower-ranked matches\nwere omitted, or ends with `_(cut here to keep this block within the context\nlimit.)_`, and the missing part is needed, run `{cli} --search` with the same\nkeywords: a manual search is not limited.\n\n## Deleting\nDelete only when the user explicitly asks: `{cli} --delete <id>` or\n`{cli} --delete-session <session_id>`. Both are irreversible and are not\nauto-allowed; confirm the id or session_id with the user first. They\nremove the memory from Redis only: the raw text stays in `audit.log` in\nthe data folder (no TTL); tell the user so when they ask to forget\nsomething.\n\n## Seven-Day Lifetime\nWhen the user expects something to be kept permanently ("remember this\nforever", 「ずっと覚えておいて」), the hooks save it as usual; add ONE short\nsentence in the user\'s language: Lite memories are deleted 7 days after they\nwere saved; for permanent storage use the Pro build or keep an external copy.\n\n## Recall Acknowledgment\nWhen recalled memory actually shapes your reply (you are recalling something\nsaved earlier), open the reply with a short acknowledgment in the user\'s\nlanguage — e.g. "Pulling this from earlier memory in this session." or\n「前回の回答がメモリに保存されています。」 If no relevant memory was found,\nor it did not influence your answer, do NOT announce anything.\n\n## Save Failures\nIf a `> ⚠️ N3MC notice:` line says that Redis could not be reached or that a\nrecord could not be saved, tell the user prominently and relay the command it\ngives (for example `docker start redis-stack`): until then, new messages are\nkept only in `audit.log`.\n'
_KNOWN_RULES_MD_HASHES = frozenset()
RULES_NAME = "n3mc-lite-behavior.md"


def _cli_form(uvx: bool) -> str:
    if uvx:
        return f"uvx --from {PKG}=={__version__} n3mc-lite"
    return f"{shell_path(sys.executable)} -m n3mc_mcp.cli"


def _strip_forms(text: str) -> str:
    text = re.sub(r'(?:"[^"\n]*"|[^\s`]+) -m n3mc_mcp\.cli', "{cli}", text)
    return re.sub(r"uvx --from n3memorycore-mcp-lite==\S+ n3mc-lite", "{cli}", text)


def _ensure_rules_md(project: str, uvx: bool) -> None:
    path = os.path.join(project, ".claude", "rules", RULES_NAME)
    want = _RULES_MD_TEMPLATE.replace("{cli}", _cli_form(uvx))
    key = "rules_md_edited:" + hashlib.sha256(os.path.abspath(path).encode("utf-8")).hexdigest()[:12]
    if not os.path.exists(path):
        paths.atomic_write_text(path, want)
        return
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        cur = f.read().replace("\r\n", "\n")
    if cur == want:
        _clear_notice(key)
        return
    stripped = _strip_forms(cur)
    if stripped == _RULES_MD_TEMPLATE or \
            hashlib.sha256(stripped.encode("utf-8")).hexdigest()[:16] in _KNOWN_RULES_MD_HASHES:
        stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        shutil.copyfile(path, f"{path}.bak-n3mc-lite-{stamp}")
        paths.atomic_write_text(path, want)
        _clear_notice(key)
        return
    _notify(key, f"The rules file {os.path.abspath(path)} has been edited by hand, so it was not updated. "
                 "Merge the new template manually if needed.")


# ── --init (§8, §12.8) ────────────────────────────────────────────────────────

OURS = {  # event -> (entry-point name, module name)
    "UserPromptSubmit": ("n3mc-lite-hook", "n3mc_mcp.lite_hook"),
    "Stop":             ("n3mc-lite-stop-hook", "n3mc_mcp.lite_stop_hook"),
}
PKG_DIR = os.path.normcase(os.path.abspath(os.path.dirname(n3mc_mcp.__file__)))

MCP_TOOLS_ALLOWED = ("search_memory", "save_memory", "list_memories", "recall_thread", "repair_memory")
MCP_PERMISSION_RULES = [f"mcp__{MCP_NAME}__{t}" for t in MCP_TOOLS_ALLOWED]


def permission_rules(python: str = None) -> list:
    python = python or shell_path(sys.executable)
    out = []
    for prefix in (f"{python} -m n3mc_mcp.cli", "n3mc-lite"):
        out += [f"Bash({prefix} --search *)", f"Bash({prefix} --buffer *)", f"Bash({prefix} --list*)",
                f"Bash({prefix} --recall-thread *)", f"Bash({prefix} --repair*)", f"Bash({prefix} --stop*)"]
    return out


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


def _script_folders() -> list:
    folders = [sysconfig.get_path("scripts")]
    try:
        folders.append(sysconfig.get_path("scripts", sysconfig.get_preferred_scheme("user")))
    except (KeyError, ValueError, AttributeError):
        pass
    return folders


def find_script(script_name: str):
    """The absolute path of a console script of this installation, or None."""
    for folder in _script_folders():
        for name in (script_name + ".exe", script_name):
            cand = pathlib.Path(folder) / name
            if cand.is_file():
                return str(cand)
    return shutil.which(script_name)


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


def _read_json_file(path: pathlib.Path):
    """(dict, None) or (None, reason). A missing file is {}."""
    if not path.exists():
        return {}, None
    try:
        data = json.loads(path.read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        return None, f"{type(e).__name__}: {e}"
    if not isinstance(data, dict):
        return None, "the top level is not a JSON object"
    return data, None


def _write_json_file(path: pathlib.Path, data: dict, backup: bool = True) -> None:
    if backup and path.exists():
        stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        shutil.copyfile(path, f"{path}.bak-n3mc-lite-{stamp}")
    paths.atomic_write_text(str(path), json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def mcp_entry() -> dict:
    exe = find_script(MCP_NAME)
    if exe:
        entry = {"type": "stdio", "command": os.path.abspath(exe), "args": []}
    else:
        entry = {"type": "stdio", "command": sys.executable, "args": ["-m", "n3mc_mcp"]}
    env = {"N3MC_AGENT_NAME": "claude-code"}
    if os.environ.get("N3MC_DATA_DIR"):
        env["N3MC_DATA_DIR"] = paths.data_dir()
    entry["env"] = env
    return entry


def _settings_type_error(settings: dict):
    hooks = settings.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        return "hooks is not an object"
    if isinstance(hooks, dict):
        for ev in OURS:
            if ev in hooks and not isinstance(hooks[ev], list):
                return f"hooks.{ev} is not a list"
    perms = settings.get("permissions")
    if perms is not None and not isinstance(perms, dict):
        return "permissions is not an object"
    if isinstance(perms, dict):
        for k in ("allow", "deny"):
            if k in perms and not isinstance(perms[k], list):
                return f"permissions.{k} is not a list"
    return None


def _has_own_hooks(settings: dict) -> bool:
    hooks = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else {}
    for ev in OURS:
        for e in hooks.get(ev, []) or []:
            if isinstance(e, dict) and isinstance(e.get("hooks"), list):
                for h in e["hooks"]:
                    if isinstance(h, dict) and is_own_hook(h.get("command"), ev):
                        return True
    return False


def _add_rules(settings: dict, rules) -> None:
    perms = settings.setdefault("permissions", {})
    allow = perms.setdefault("allow", [])
    for r in rules:
        if r not in allow:
            allow.append(r)


def _register_hooks(settings: dict, messages: list, settings_path) -> None:
    enabled = settings.get("enabledPlugins") if isinstance(settings.get("enabledPlugins"), dict) else {}
    plugin_on = enabled.get(PLUGIN_FULL) is True
    hooks = settings.setdefault("hooks", {})
    for event, (exe_name, _module) in OURS.items():
        entries = hooks.get(event, []) or []
        new_entries = []
        insert_at = None
        for e in entries:
            if not isinstance(e, dict) or not isinstance(e.get("hooks"), list):
                new_entries.append(e)
                continue
            kept = []
            had_own = False
            for h in e["hooks"]:
                if isinstance(h, dict) and is_own_hook(h.get("command"), event):
                    had_own = True
                    continue
                kept.append(h)
                cmd = h.get("command") if isinstance(h, dict) else None
                if isinstance(cmd, str) and "n3mc" in cmd.lower():
                    messages.append(f"Another N3MC hook is registered for {event} and was left in place: {cmd}")
            if had_own and insert_at is None:
                insert_at = len(new_entries)
            if kept:
                e2 = dict(e)
                e2["hooks"] = kept
                new_entries.append(e2)
            elif not had_own:
                new_entries.append(e)
        if not plugin_on:
            cmd = resolve_cmd(exe_name)
            entry = {"hooks": [{"type": "command", "command": cmd, "timeout": 60}]}
            if insert_at is None:
                new_entries.append(entry)
            else:
                new_entries.insert(insert_at, entry)
            if cmd.startswith('"'):
                messages.append(f"The hook command for {event} starts with a quote because {sys.executable} "
                                "contains a space or another character the shell treats specially; if the hook "
                                "does not run, install Python into a folder without such characters.")
        if new_entries:
            hooks[event] = new_entries
        elif event in hooks:
            hooks[event] = new_entries
    _add_rules(settings, permission_rules())
    if plugin_on:
        messages.append("The n3mc-workingmemory plugin is enabled and registers the N3MC Lite hooks, so "
                        "n3mc-lite --init added no hooks.")
    else:
        messages.append(f"N3MC Lite hooks registered in {settings_path}. Saving starts with the next "
                        "Claude Code session.")


def _apply_mcp(cj: dict, plugin: str) -> None:
    servers = cj.setdefault("mcpServers", {})
    if plugin:
        servers.pop(MCP_NAME, None)
    else:
        servers[MCP_NAME] = mcp_entry()


def init(hooks: bool = False) -> int:
    _config.load_config()
    home = pathlib.Path(os.path.expanduser("~"))
    settings_path = home / ".claude" / "settings.json"
    cj_path = home / ".claude.json"
    settings, err = _read_json_file(settings_path)
    if err is None:
        err = _settings_type_error(settings)
    if err is not None:
        print(f"Could not read {settings_path} ({err}); nothing was changed.")
        return 1
    cj, err = _read_json_file(cj_path)
    if err is None and "mcpServers" in cj and not isinstance(cj["mcpServers"], dict):
        err = "mcpServers is not an object"
    if err is not None:
        print(f"Could not read {cj_path} ({err}); nothing was changed.")
        return 1
    messages = []
    orig_settings = copy.deepcopy(settings)
    enabled = settings.get("enabledPlugins") if isinstance(settings.get("enabledPlugins"), dict) else {}
    plugin = next((p.split("@")[0] for p in (PLUGIN_MCP, PLUGIN_FULL) if enabled.get(p) is True), None)
    own_hooks = _has_own_hooks(settings)
    do_hooks = hooks or own_hooks

    orig_cj = copy.deepcopy(cj)
    _apply_mcp(cj, plugin)
    cj_changed = cj != orig_cj
    if not orig_cj.get("mcpServers") and cj.get("mcpServers") == {} and "mcpServers" not in orig_cj:
        cj_changed = False

    _add_rules(settings, MCP_PERMISSION_RULES)
    perms = settings.get("permissions", {})
    deny = perms.get("deny")
    if isinstance(deny, list):
        kept = []
        for r in deny:
            if isinstance(r, str) and (r == f"mcp__{MCP_NAME}" or r.startswith(f"mcp__{MCP_NAME}__")):
                messages.append(f'Removed "{r}" from permissions.deny in {settings_path}: it hid the N3MC Lite '
                                "MCP tools from Claude Code.")
                continue
            kept.append(r)
        perms["deny"] = kept

    hook_messages = []
    if do_hooks:
        _register_hooks(settings, hook_messages, settings_path)
        if not hooks and own_hooks:
            hook_messages.insert(0, f"N3MC Lite hooks are already registered in {settings_path}, so n3mc-lite "
                                    "--init also refreshed them (MCP + hooks).")

    if settings != orig_settings:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json_file(settings_path, settings)
    if cj_changed:
        fresh, err = _read_json_file(cj_path)   # Claude Code writes this file too: re-read it
        if err is None and (not isinstance(fresh.get("mcpServers", {}), dict)):
            err = "mcpServers is not an object"
        if err is not None:
            print(f"Could not read {cj_path} ({err}); the MCP server was not registered.")
            return 1
        _apply_mcp(fresh, plugin)
        _write_json_file(cj_path, fresh)

    projects = cj.get("projects") if isinstance(cj.get("projects"), dict) else {}
    for proj, pd in projects.items():
        if isinstance(pd, dict) and isinstance(pd.get("mcpServers"), dict) and MCP_NAME in pd["mcpServers"]:
            messages.append(f"Another registration of {MCP_NAME} was found in {cj_path} (project {proj}). In that "
                            f"project Claude Code uses it instead of the one n3mc-lite --init wrote; remove it with: "
                            f"claude mcp remove {MCP_NAME} -s local (run it in that project)")
    mcp_json = pathlib.Path(os.getcwd()) / ".mcp.json"
    mj, err = _read_json_file(mcp_json)
    if err is None and isinstance(mj.get("mcpServers"), dict) and MCP_NAME in mj["mcpServers"]:
        messages.append(f"Another registration of {MCP_NAME} was found in {mcp_json}. In that project Claude Code "
                        f"uses it instead of the one n3mc-lite --init wrote; remove the \"{MCP_NAME}\" entry from "
                        f"{mcp_json}")

    if plugin:
        head = (f"The {plugin} plugin is enabled and registers the N3MC Lite MCP server, so n3mc-lite --init "
                "added no MCP server.")
    else:
        head = (f"N3MC Lite MCP server registered in {cj_path} (user scope). Restart Claude Code; the memory "
                "tools appear in the next session.")
    for m in messages:
        print(m)
    print(head)
    for m in hook_messages:
        print(m)
    return 0


# ── the tool commands (§13) ───────────────────────────────────────────────────

def _tool(cfg, name: str, args: dict) -> int:
    ok = ensure_server(cfg)
    sent = False
    if ok:
        try:
            status, data = _post(cfg, f"/tools/{name}?nudge=0", args, 300.0)
            if status == 200 and isinstance(data.get("text"), str):
                print(data["text"])
                return 0
            reason, sent = f"HTTPError: HTTP {status}", True
        except Exception as e:  # noqa: BLE001
            reason, sent = classify_request_error(e)
    elif _FOREIGN_SERVER_ROOT is not None:
        reason = reason_foreign(cfg)
    else:
        reason = unavailable_reason(cfg)
    if name == "save_memory":
        text, _ = direct_save_text(cfg, args, reason)
        print(text)
    else:
        print(unavailable_text(name, reason, sent))
    return 0


def _search_cmd(args) -> int:
    try:
        parse_time_bounds(args.since, args.until)
    except TimeBoundError as e:
        _err(str(e))
        return 2
    cfg = _config.load_config()
    ok = ensure_server(cfg)
    body = {"query": args.search}
    for k in ("since", "until"):
        v = getattr(args, k)
        if v is not None:
            body[k] = v
    if args.limit is not None:
        body["limit"] = args.limit
    if os.environ.get("N3MC_SESSION_ID"):
        body["session_id"] = os.environ["N3MC_SESSION_ID"]
    try:
        print(_search_text(cfg, ok, body))
    except TimeBoundError as e:
        _err(str(e))
        return 2
    return 0


def stop_server() -> int:
    cfg = _config.load_config()
    port = int(cfg.get("server_port", 18524))
    h = _health(cfg)
    if not h or not h.get("root") or os.path.normcase(os.path.abspath(h["root"])) != _own_root():
        print("No N3MC Lite server of this data folder is running.")
        return 0
    pid = h.get("pid")
    ok = _request_shutdown(cfg, _own_root())
    freed = _wait_port_free(port, 15.0) if ok else not _port_in_use(port)
    if ok and freed:
        print(f"Stopped the N3MC Lite server (pid {pid}).")
        return 0
    reason = "the stop request failed" if not ok else "the port was still in use after 15 seconds"
    _err(f"Could not stop the N3MC Lite server (pid {pid}): {reason}. End the process whose command line "
         "contains n3mc_mcp.cli and --run-server.")
    return 1


def _read_stdin_text() -> str:
    try:
        data = sys.stdin.buffer.read()
        return data.decode("utf-8", "surrogateescape")
    except Exception:  # noqa: BLE001
        try:
            return sys.stdin.read()
        except Exception:  # noqa: BLE001
            return ""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="n3mc-lite", description="N3MemoryCore MCP-Lite command line.")
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
    p.add_argument("--hooks", action="store_true", help="with --init: also register the hooks")
    p.add_argument("--uvx", action="store_true")
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--limit", type=int)
    p.add_argument("--agent-name")
    p.add_argument("--session-id")
    p.add_argument("--turn-id")
    p.add_argument("--importance", type=float)
    p.add_argument("--before", type=int)
    p.add_argument("--after", type=int)
    return p


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = parser.parse_args([sanitize_surrogates(a) for a in argv])
    for k, v in vars(args).items():
        if isinstance(v, str):
            setattr(args, k, sanitize_surrogates(v))
    if args.hooks and not args.init:
        parser.error("--hooks can only be used with --init")
    if args.run_server:
        run_server()
        return 0
    if args.init:
        return init(hooks=args.hooks)
    if args.hook_submit:
        return hook_submit(_read_stdin_text(), args.uvx)
    if args.save_claude_turn:
        return save_claude_turn(_read_stdin_text(), args.uvx)
    if args.stop:
        return stop(_read_stdin_text(), args.uvx)
    if args.stop_server:
        return stop_server()
    if args.search is not None:
        return _search_cmd(args)
    cfg = _config.load_config()
    if args.buffer is not None:
        content = _read_stdin_text() if args.buffer == "-" else args.buffer
        a = {"content": sanitize_surrogates(content), "agent_name": args.agent_name or "claude-code"}
        if args.session_id is not None:
            a["session_id"] = args.session_id
        if args.turn_id is not None:
            a["turn_id"] = args.turn_id
        if args.importance is not None:
            a["importance"] = args.importance
        return _tool(cfg, "save_memory", a)
    if args.list:
        return _tool(cfg, "list_memories", {"limit": args.limit} if args.limit is not None else {})
    if args.recall_thread is not None:
        a = {"turn_id": args.recall_thread}
        if args.before is not None:
            a["before"] = args.before
        if args.after is not None:
            a["after"] = args.after
        return _tool(cfg, "recall_thread", a)
    if args.delete is not None:
        return _tool(cfg, "delete_memory", {"id": args.delete})
    if args.delete_session is not None:
        return _tool(cfg, "delete_memories_by_session", {"session_id": args.delete_session})
    if args.repair:
        return _tool(cfg, "repair_memory", {})
    parser.error("no command")
    return 2


if __name__ == "__main__":
    sys.exit(main())
