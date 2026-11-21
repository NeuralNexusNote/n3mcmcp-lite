"""CLI `n3mc-lite` (spec §3.15, §12, §13): resident-server management, hooks, --init, tool commands."""
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
import contextlib  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import pathlib  # noqa: E402
import re  # noqa: E402
import shlex  # noqa: E402
import shutil  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import sysconfig  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import urllib.error  # noqa: E402
import urllib.parse  # noqa: E402
import urllib.request  # noqa: E402
import uuid  # noqa: E402

import n3mc_mcp  # noqa: E402
from n3mc_mcp import __version__, paths  # noqa: E402
from n3mc_mcp import config as config_mod  # noqa: E402
from n3mc_mcp import context as C  # noqa: E402
from n3mc_mcp._verbatim import _RULES_MD_TEMPLATE  # noqa: E402

SYSTEM_PREFIXES = ("<task-notification>", "<agent-message from=", "<system-reminder>")
PLUGIN_KEY = "n3mc-workingmemory@neuralnexusnote"
UVX_FORM = f"uvx --from n3memorycore-mcp-lite=={__version__} n3mc-lite"
START_WAIT = 60
FAILED_MARKER_SECONDS = 300

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_FOREIGN_SERVER_ROOT = None
_tls = threading.local()


from .paths import python_cmd  # noqa: E402


def _stderr(msg: str) -> None:
    try:
        print(msg, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        pass


# ============================================================ HTTP (never through a proxy)

def _json(b):
    try:
        return json.loads(b.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _request(method, path, body, port, timeout):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Host": f"127.0.0.1:{port}", "Content-Type": "application/json"})
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, _json(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, _json(e.read())
        finally:
            e.close()


def _get(path, port, timeout=5):
    return _request("GET", path, None, port, timeout)


def _post(path, body, port, timeout=30):
    return _request("POST", path, body, port, timeout)


def _health(port):
    try:
        status, j = _get("/health", port, 5)
    except Exception:  # noqa: BLE001
        return None
    if status == 200 and isinstance(j, dict):
        return j
    return {}


def _health_decision(health):
    if health is None:
        return "none"
    root = health.get("root") if isinstance(health, dict) else None
    if not root:
        return "legacy"
    if not paths.same_dir(root, paths.data_dir()):
        return "foreign"
    if health.get("code_version") != config_mod.code_version():
        return "stale"
    return "ok"


def _port_free(port) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return False
    except OSError:
        return True


def _wait_port_free(port, seconds=15.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _port_free(port):
            return True
        time.sleep(0.3)
    return _port_free(port)


def _redis_display(url):
    u = urllib.parse.urlparse(url or "")
    db = (u.path or "").strip("/")
    return f"{u.hostname or 'localhost'}:{u.port or 6379}/{int(db) if db.isdigit() else 0}"


# ============================================================ notices (§3.15 _notify)

def _notify(key, message):
    _stderr(f"N3MC Lite: {message}")
    if paths.store_notice(key, message):
        paths.append_audit({"ts": paths.utc_now_iso(), "hook": "notice", "key": key, "message": message})


def _clear_notice(key):
    paths.clear_notice(key)


def _redis_unreachable(cfg):
    from n3mc_mcp.database import backend_hint
    _notify("redis_unreachable",
            f"N3MC Lite could not reach Redis, so a message was kept only in {paths.audit_log_path()}. "
            + backend_hint(cfg["redis_url"]))


# ============================================================ the resident server (§3.15)

def _reason_starting():
    return "the memory server is still starting (the first start downloads the embedding model, ~440 MB)"


def _reason_failed():
    return f"the memory server could not start; see {paths.server_log_path()}"


def _reason_foreign(port):
    return f"another N3MC folder's server is using port {port}"


def _set_reason(r):
    _tls.reason = r


def last_reason():
    return getattr(_tls, "reason", None) or _reason_starting()


def _is_own_server_process(pid, port=None) -> bool:
    try:
        import psutil
    except ImportError:
        return False
    try:
        if not pid and port:
            for c in psutil.net_connections(kind="tcp"):
                if c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN:
                    pid = c.pid
                    break
        if not pid:
            return False
        p = psutil.Process(int(pid))
        cmd = p.cmdline()
        if not (any("n3mc_mcp.cli" in t for t in cmd) and "--run-server" in cmd):
            return False
        env = p.environ()
        dd = env.get("N3MC_DATA_DIR")
        d = os.path.abspath(os.path.expanduser(dd)) if dd else os.path.join(os.path.expanduser("~"),
                                                                             ".n3mc-workingmemory")
        return paths.same_dir(d, paths.data_dir())
    except Exception:  # noqa: BLE001
        return False


def _request_shutdown(port, root):
    return _post("/shutdown", {"root": root}, port, 5)


def _stop_stale_server(port, health) -> bool:
    try:
        status, _ = _request_shutdown(port, str(paths.data_dir()))
    except Exception:  # noqa: BLE001
        status = None
    if status == 200:
        return _wait_port_free(port)
    if status in (404, 405):
        pid = (health or {}).get("pid")
        if _is_own_server_process(pid, port):
            try:
                import psutil
                p = psutil.Process(int(pid) if pid else _listening_pid(port))
                p.terminate()
                try:
                    p.wait(5)
                except psutil.TimeoutExpired:
                    p.kill()
            except Exception:  # noqa: BLE001
                return False
            return _wait_port_free(port)
    return False


def _listening_pid(port):
    import psutil
    for c in psutil.net_connections(kind="tcp"):
        if c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN:
            return c.pid
    return None


def _server_good(cfg, health):
    try:
        paths.start_failed_path().unlink()
    except OSError:
        pass
    for k in ("stale_server", "port_conflict", "server_start_failed"):
        _clear_notice(k)
    own = _redis_display(cfg["redis_url"])
    theirs = (health or {}).get("redis")
    if theirs and theirs != own:
        _notify("redis_url_mismatch",
                f"The N3MC Lite server uses Redis at {theirs}, but this process is set to {own}. Put redis_url "
                f"into {paths.config_path()} instead of setting N3MC_REDIS_URL per application, then restart "
                f"the server (it restarts after config.json changes).")
    else:
        _clear_notice("redis_url_mismatch")


def _wait_health(port, max_wait, proc=None):
    deadline = time.time() + max(0.0, float(max_wait))
    while True:
        h = _health(port)
        if _health_decision(h) == "ok":
            return h
        if proc is not None and proc.poll() is not None:
            return "exited"
        if time.time() >= deadline:
            return None
        time.sleep(0.5)


def _pidfile_starting(pidp: pathlib.Path) -> bool:
    try:
        text = pidp.read_text(encoding="utf-8").strip()
        mtime = pidp.stat().st_mtime
    except OSError:
        return False
    young = time.time() - mtime < 60
    if not text.isdigit():
        return young
    try:
        import psutil
    except ImportError:
        return young
    try:
        if not psutil.pid_exists(int(text)):
            return False
    except Exception:  # noqa: BLE001
        return False
    return _is_own_server_process(int(text))


def _listen_ports(pid):
    """TCP ports the process ``pid`` is listening on (empty when psutil cannot tell)."""
    try:
        import psutil
        proc = psutil.Process(int(pid))
        # psutil 6.0 renamed Process.connections() to net_connections(); psutil>=5.9 is allowed.
        getter = getattr(proc, "net_connections", None) or proc.connections
        conns = getter(kind="tcp")
    except Exception:  # noqa: BLE001
        return []
    return sorted({c.laddr.port for c in conns if c.laddr and c.status == psutil.CONN_LISTEN})


def _stop_moved_server(pidp: pathlib.Path, port) -> bool:
    """The PID-file server listens on another port (server_port was changed): stop it there.

    Stopped only through /shutdown, after that port's /health reports this data folder. True when it
    stopped (the PID file is then removed when it still holds that PID)."""
    try:
        text = pidp.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not text.isdigit():
        return False
    for other in _listen_ports(text):
        if other == port:
            continue
        h = _health(other)
        if not isinstance(h, dict) or not h.get("root") or not paths.same_dir(h["root"], paths.data_dir()):
            continue
        try:
            status, _ = _request_shutdown(other, str(paths.data_dir()))
        except Exception:  # noqa: BLE001
            return False
        if status != 200 or not _wait_port_free(other):
            return False
        try:
            if pidp.read_text(encoding="utf-8").strip() == text:
                pidp.unlink()
        except OSError:
            pass
        return True
    return False


def ensure_server(cfg, max_wait=START_WAIT) -> bool:
    """True when a healthy server of this data folder answers (starting one if needed)."""
    global _FOREIGN_SERVER_ROOT
    port = int(cfg["server_port"])
    _FOREIGN_SERVER_ROOT = None
    _set_reason(None)
    h = _health(port)
    d = _health_decision(h)
    if d == "legacy":
        if _is_own_server_process((h or {}).get("pid"), port) and _stop_stale_server(port, h):
            d = "none"
        else:
            d = "foreign"
            h = {"root": "(unknown: the server did not report its folder)"}
    if d == "ok":
        _server_good(cfg, h)
        return True
    if d == "foreign":
        _FOREIGN_SERVER_ROOT = h.get("root")
        _notify("port_conflict",
                f"Another N3MC server ({_FOREIGN_SERVER_ROOT}) is using port {port}. Give N3MC Lite its own "
                f"server_port in {paths.config_path()}.")
        _set_reason(_reason_foreign(port))
        return False
    if d == "stale":
        if not _stop_stale_server(port, h):
            _notify("stale_server",
                    f"Cannot stop the old N3MC Lite server. Another program may be using server_port {port}. "
                    f"Check server_port in config.json and restart manually.")
            return True
        try:
            if paths.pid_path().read_text(encoding="utf-8").strip() == str((h or {}).get("pid")):
                paths.pid_path().unlink()
        except OSError:
            pass
    pidp = paths.pid_path()
    if pidp.exists():
        if _pidfile_starting(pidp) and not _stop_moved_server(pidp, port):
            got = _wait_health(port, min(max_wait, START_WAIT))
            if isinstance(got, dict):
                _server_good(cfg, got)
                return True
            _set_reason(_reason_starting())
            return False
        try:
            pidp.unlink()
        except OSError:
            pass
    marker = paths.start_failed_path()
    try:
        if time.time() - marker.stat().st_mtime < FAILED_MARKER_SECONDS:
            _set_reason(_reason_failed())
            return False
    except OSError:
        pass
    return _start_server(cfg, max_wait)


def _start_server(cfg, max_wait=START_WAIT) -> bool:
    port = int(cfg["server_port"])
    pidp = paths.pid_path()
    pidp.parent.mkdir(parents=True, exist_ok=True)
    try:
        pf = open(pidp, "x", encoding="utf-8")
    except FileExistsError:
        got = _wait_health(port, min(max_wait, START_WAIT))
        if isinstance(got, dict):
            _server_good(cfg, got)
            return True
        _set_reason(_reason_starting())
        return False
    log = paths.server_log_path()
    try:
        if log.exists() and log.stat().st_size > 1024 * 1024:
            log.write_bytes(b"")
    except OSError:
        pass
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    def _popen(logf, **extra):
        return subprocess.Popen([*python_cmd(), "-m", "n3mc_mcp.cli", "--run-server"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=logf,
                                close_fds=True, **extra)

    try:
        with open(log, "ab") as logf:
            proc = _popen(logf, **kw)
    except Exception as e:  # noqa: BLE001
        pf.close()
        try:
            pidp.unlink()
        except OSError:
            pass
        _stderr(f"N3MC Lite: could not start the memory server: {e}")
        _set_reason(_reason_failed())
        return False
    pf.write(str(proc.pid))
    pf.close()
    got = _wait_health(port, max_wait, proc)
    if isinstance(got, dict):
        _server_good(cfg, got)
        return True
    if got == "exited":
        _notify("server_start_failed",
                f"The N3MC Lite memory server could not start (exit code {proc.returncode}). See {log} for "
                f"the reason.")
        try:
            paths.start_failed_path().write_text(paths.utc_now_iso(), encoding="utf-8")
        except OSError:
            pass
        try:
            if pidp.read_text(encoding="utf-8").strip() == str(proc.pid):
                pidp.unlink()
        except OSError:
            pass
        _set_reason(_reason_failed())
        return False
    _set_reason(_reason_starting())
    return False


# ============================================================ server-less paths (§3.16, §4.6)

class BackendDown(Exception):
    pass


def direct_save(cfg, content, role=None, agent_name=None, session_id=None, turn_id=None, importance=None,
                owner_id=None):
    """Save straight into Redis without vectors (embed=False). Raises BackendDown when Redis is unusable."""
    from n3mc_mcp.database import BACKEND_ERRORS, Database
    db = Database(cfg)
    try:
        st = db.ensure_connected(ephemeral=False)
        if st != "ok":
            raise BackendDown(db.unavailable_hint(st))
        return db.save_memory(content, agent_name=agent_name, owner_id=owner_id, importance=importance,
                              session_id=session_id, turn_id=turn_id, role=role, embed=False)
    except BACKEND_ERRORS as e:
        raise BackendDown(db.unavailable_hint("down")) from e
    finally:
        try:
            if db.client is not None:
                db.client.close()
        except Exception:  # noqa: BLE001
            pass


def unavailable_text(name, reason) -> str:
    """§4.6 table, without the reminder."""
    if name in ("search_memory", "list_memories", "recall_thread"):
        return (f"Error: memory backend unreachable. The N3MC Lite memory server is not available ({reason}). "
                f"It is started automatically; retry in a minute.")
    if name in ("delete_memory", "delete_memories_by_session"):
        return json.dumps({"status": "error", "reason": f"The N3MC Lite memory server is not available "
                                                        f"({reason}). Nothing was deleted; retry in a minute."},
                          ensure_ascii=False)
    if name == "repair_memory":
        return json.dumps({"status": "error", "message": f"The N3MC Lite memory server is not available "
                                                         f"({reason}). Retry in a minute."}, ensure_ascii=False)
    return f"Error: unknown tool '{name}'"


def direct_save_tool(cfg, args, reason):
    """save_memory without a server (§4.6). Returns (result dict, is_error)."""
    from n3mc_mcp.database import _parse_importance
    imp = args.get("importance")
    if imp is not None and _parse_importance(imp) is None:
        return {"saved": False, "status": "error", "reason": f"importance must be numeric (got {imp})"}, True
    owner = args.get("owner_id")
    if owner not in (None, "") and owner != cfg["owner_id"]:
        return {"status": "error", "saved": False, "reason": "owner_id mismatch"}, True
    try:
        res = direct_save(cfg, args.get("content"), agent_name=args.get("agent_name"),
                          session_id=args.get("session_id"), turn_id=args.get("turn_id"), importance=imp)
    except BackendDown as e:
        return {"status": "error", "saved": False, "reason": str(e)}, True
    if res.get("saved"):
        detail = (f"The N3MC Lite memory server was not available ({reason}), so this entry was saved without "
                  f"a vector. It is found by keywords now and gets its vector from repair_memory once the "
                  f"server runs.")
        if res.get("warning"):
            res["warning"] = "embedding_unavailable; " + res["warning"]
            res["detail"] = detail + " " + res.get("detail", "")
        else:
            res["warning"] = "embedding_unavailable"
            res["detail"] = detail
    return res, res.get("status") == "error"


# ============================================================ hooks (§12)

def _read_stdin_text() -> str:
    try:
        return sys.stdin.buffer.read().decode("utf-8", "surrogateescape")
    except Exception:  # noqa: BLE001
        try:
            return sys.stdin.read()
        except Exception:  # noqa: BLE001
            return ""


def _read_stdin_buffer() -> str:
    """`--buffer -`: UTF-8, or the OS encoding when the piped bytes are not UTF-8 (e.g. cp932)."""
    import locale
    from n3mc_mcp.processor import sanitize_surrogates
    data = sys.stdin.buffer.read()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return sanitize_surrogates(data.decode(locale.getpreferredencoding(False) or "utf-8", "replace"))


def _parse_hook_json(raw):
    from n3mc_mcp.processor import sanitize_surrogates
    try:
        obj = json.loads(raw)
    except Exception:  # noqa: BLE001
        obj = None
    if not isinstance(obj, dict):
        obj = None
    return sanitize_surrogates(obj) if obj is not None else None, sanitize_surrogates(raw)


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
        except Exception:  # noqa: BLE001
            arr = None
        if isinstance(arr, list) and arr and all(isinstance(x, dict) and isinstance(x.get("type"), str)
                                                 for x in arr):
            return from_parts(arr)
    return prompt


def _read_turn(session_id):
    try:
        t = paths.turn_id_path(session_id).read_text(encoding="utf-8").strip()
        return t or None
    except OSError:
        return None


def _reply_turn(session_id):
    """turn_id for Claude's reply: the file's, or a new one that is written to the file, so that the
    Stop hook and the next prompt's transcript recovery save the same reply under one turn_id
    (the hook's exact-duplicate guard works only within a turn, §3.16)."""
    t = _read_turn(session_id)
    if not t:
        t = str(uuid.uuid4())
        _write_turn(session_id, t)
    return t


def _write_turn(session_id, turn_id):
    p = paths.turn_id_path(session_id)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(turn_id, encoding="utf-8")
    except OSError as e:
        _stderr(f"N3MC Lite: could not write {p}: {e}")


def _user_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [p.get("text") for p in content
                 if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)]
        if texts:
            return "\n".join(texts)
    return None


def last_assistant_text(transcript_path, current_prompt):
    """Claude's previous reply from the transcript (§12.5). Raises on unreadable input."""
    size = os.path.getsize(transcript_path)
    limit = 5 * 1024 * 1024
    with open(transcript_path, "rb") as f:
        if size > limit:
            f.seek(size - limit)
        data = f.read().decode("utf-8", "replace")
    lines = data.splitlines()
    if size > limit and lines:
        lines = lines[1:]
    newest = True
    skip_prefixes = ("[Request interrupted by user", "<command-", "<local-command-") + SYSTEM_PREFIXES
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except Exception:  # noqa: BLE001
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
            txt = _user_text(content)
            if txt is None or txt.startswith(skip_prefixes):
                continue
            if newest and current_prompt is not None and txt.strip() == current_prompt.strip():
                newest = False
                continue
            return None
        newest = False
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            texts = [p.get("text") for p in content
                     if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)
                     and p.get("text").strip()]
            if texts:
                return texts[-1]
    return None


def _hook_save(cfg, server_ok, content, role, session_id, turn_id):
    """§12.6: through the server, else a direct save; never raises."""
    if not isinstance(content, str) or not content.strip():
        return
    port = int(cfg["server_port"])
    body = {"content": content, "role": role, "agent_name": "claude-code", "session_id": session_id or "",
            "turn_id": turn_id, "no_model_wait": True}  # hooks never wait for the model (M3)
    if server_ok:
        try:
            status, res = _post("/buffer", body, port, 30)
        except Exception:  # noqa: BLE001
            status, res = None, None
        if status == 200 and isinstance(res, dict):
            _after_save(res)
            return
        if status == 503:
            _redis_unreachable(cfg)
            return
    try:
        res = direct_save(cfg, content, role=role, agent_name="claude-code", session_id=session_id or None,
                          turn_id=turn_id)
    except BackendDown:
        _redis_unreachable(cfg)
        return
    except Exception as e:  # noqa: BLE001
        _notify("save_failed", f"A record could not be saved ({type(e).__name__}: {e}). "
                               f"Current memories may be lost.")
        return
    _after_save(res)


def _after_save(res):
    st = res.get("status")
    if st in ("saved", "duplicate"):
        _clear_notice("redis_unreachable")
        _clear_notice("save_failed")  # a later save succeeded (L4)
    elif st == "error":
        _stderr(f"N3MC Lite: not saved: {res.get('reason')}")


def _fallback(line):
    return C.fallback_block(line, paths.take_due_notices())


def _search_block(cfg, server_ok, query, session_id=None, since=None, until=None, limit=None, max_chars=None,
                  hook=False):
    port = int(cfg["server_port"])
    if not server_ok:
        return _fallback(f"_(memory search unavailable: {last_reason()})_")
    body = {"query": query}
    if session_id:
        body["session_id"] = session_id
    if since:
        body["since"] = since
    if until:
        body["until"] = until
    if limit is not None:
        body["limit"] = limit
    if max_chars:
        body["max_chars"] = max_chars
    if hook:
        body["no_model_wait"] = True  # the hook never waits for the model (M3); --search does
    try:
        status, res = _post("/search", body, port, 30)
    except Exception as e:  # noqa: BLE001
        return _fallback(f"_(memory search unavailable: {type(e).__name__}: {e})_")
    if status == 200 and isinstance(res, dict) and isinstance(res.get("text"), str):
        return res["text"] + "\n"
    if status == 503:
        _redis_unreachable(cfg)
        hint = (res or {}).get("detail") if isinstance(res, dict) else None
        return _fallback(f"_(memory search unavailable: {hint})_")
    if status == 400 and isinstance(res, dict):
        return _fallback(f"_(memory search unavailable: {res.get('detail')})_")
    return _fallback(f"_(memory search unavailable: HTTPError: status {status})_")


def cmd_hook_submit(uvx=False) -> int:
    raw = _read_stdin_text()
    payload, raw_clean = _parse_hook_json(raw)
    if payload is None:
        prompt = raw_clean
        payload = {}
    else:
        prompt = payload.get("prompt", payload.get("message"))
    text = _extract_text(prompt)
    session = payload.get("session_id") if isinstance(payload.get("session_id"), str) else ""
    if text.lstrip().startswith(SYSTEM_PREFIXES):
        paths.append_audit({"ts": paths.utc_now_iso(), "hook": "UserPromptSubmit", "stage": "skipped",
                            "reason": "system_prompt"})
        sys.stdout.write(C.empty_block())
        return 0
    cfg = config_mod.load_config()
    server_ok = ensure_server(cfg, max_wait=0)
    port = int(cfg["server_port"])
    if server_ok:
        try:
            status, _ = _post("/repair", {}, port, 10)
            if status != 200:
                _stderr(f"N3MC Lite: /repair answered {status}")
        except Exception as e:  # noqa: BLE001
            _stderr(f"N3MC Lite: /repair failed: {type(e).__name__}: {e}")
    tp = payload.get("transcript_path")
    prev = None
    if tp:
        try:
            prev = last_assistant_text(tp, text)
        except Exception as e:  # noqa: BLE001
            paths.append_audit({"ts": paths.utc_now_iso(), "hook": "UserPromptSubmit",
                                "stage": "transcript_unreadable", "reason": f"{type(e).__name__}: {e}"[:300]})
    else:
        paths.append_audit({"ts": paths.utc_now_iso(), "hook": "UserPromptSubmit",
                            "stage": "transcript_unreadable", "reason": "no transcript_path"})
    if prev:
        _hook_save(cfg, server_ok, prev, "claude", session, _reply_turn(session))
    new_turn = str(uuid.uuid4())
    _write_turn(session, new_turn)
    if text.strip():
        sys.stdout.write(_search_block(cfg, server_ok, text, session_id=session or None,
                                       max_chars=int(cfg["context_max_chars"]), hook=True))
        sys.stdout.flush()
        _hook_save(cfg, server_ok, text, "user", session, new_turn)
    else:
        sys.stdout.write(C.empty_block())
    return 0


def cmd_save_claude_turn(uvx=False) -> int:
    raw = _read_stdin_text()
    payload, _ = _parse_hook_json(raw)
    payload = payload or {}
    msg = payload.get("last_assistant_message")
    if not isinstance(msg, str) or not msg.strip():
        return 0
    session = payload.get("session_id") if isinstance(payload.get("session_id"), str) else ""
    cfg = config_mod.load_config()
    server_ok = ensure_server(cfg, max_wait=0)
    _hook_save(cfg, server_ok, msg, "claude", session, _reply_turn(session))
    return 0


# ============================================================ --stop and the rules file (§12.4, §12.9)

_KNOWN_RULES_MD_HASHES = set()
_FORM_RE = re.compile(r'(?:"[^"\n]*"|\S+) -m n3mc_mcp\.cli|uvx --from n3memorycore-mcp-lite==\S+ n3mc-lite')


def shell_path(p) -> str:
    s = pathlib.Path(p).as_posix()                     # forward slashes: bash reads "\" as an escape
    if " " in s and os.name == "nt":                   # (L28) use the 8.3 short name, which has no space
        short = _short_path(p)
        if short and " " not in short:
            s = pathlib.Path(short).as_posix()
    return f'"{s}"' if " " in s else s                 # quote only when a space is left


def _short_path(p) -> str:
    """Windows 8.3 short path of an existing file, or "" (8.3 names may be disabled on the volume)."""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(32768)
        n = ctypes.windll.kernel32.GetShortPathNameW(str(p), buf, len(buf))
        return buf.value if 0 < n < len(buf) else ""
    except Exception:  # noqa: BLE001
        return ""


def cli_form(uvx) -> str:
    return UVX_FORM if uvx else f"{shell_path(sys.executable)} -m n3mc_mcp.cli"


def rules_text(uvx) -> str:
    return _RULES_MD_TEMPLATE.replace("{cli}", cli_form(uvx))


def _ensure_rules_md(cwd, uvx):
    home = os.path.expanduser("~")
    if paths.same_dir(cwd, home):
        return
    target = pathlib.Path(cwd) / ".claude" / "rules" / "n3mc-lite-behavior.md"
    key = "rules_md_edited:" + hashlib.sha256(os.path.abspath(str(target)).encode("utf-8")).hexdigest()[:12]
    want = rules_text(uvx)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        paths._write_atomic(target, want.encode("utf-8"))
        return
    cur = target.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    if cur == want:
        _clear_notice(key)
        return
    stripped = _FORM_RE.sub("{cli}", cur)
    if stripped == _RULES_MD_TEMPLATE or \
            hashlib.sha256(stripped.encode("utf-8")).hexdigest()[:16] in _KNOWN_RULES_MD_HASHES:
        stamp = time.strftime("%Y%m%d%H%M%S")
        shutil.copy2(target, target.with_name(f"n3mc-lite-behavior.md.bak-n3mc-lite-{stamp}"))
        paths._write_atomic(target, want.encode("utf-8"))
        _clear_notice(key)
        return
    _notify(key, f"The rules file {os.path.abspath(str(target))} has been edited by hand, so it was not "
                 f"updated. Merge the new template manually if needed.")


AUDIT_ROTATE_BYTES = 50 * 1024 * 1024


def cmd_stop(uvx=False) -> int:
    raw = _read_stdin_text()
    payload, _ = _parse_hook_json(raw)
    payload = payload or {}
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) and payload.get("cwd") else os.getcwd()
    try:
        _ensure_rules_md(cwd, uvx)
    except Exception as e:  # noqa: BLE001
        _stderr(f"N3MC Lite: rules file: {type(e).__name__}: {e}")
    try:
        cutoff = time.time() - 30 * 86400
        for p in paths.data_dir().glob("turn_id*.txt"):
            if p.stat().st_mtime < cutoff:
                p.unlink()
    except Exception as e:  # noqa: BLE001
        _stderr(f"N3MC Lite: turn_id cleanup: {type(e).__name__}: {e}")
    try:
        a = paths.audit_log_path()
        if a.exists() and a.stat().st_size > AUDIT_ROTATE_BYTES:
            os.replace(str(a), str(a.with_name("audit.log.1")))
    except Exception as e:  # noqa: BLE001
        _stderr(f"N3MC Lite: audit.log rotation: {type(e).__name__}: {e}")
    return 0


# ============================================================ --init (§12.8)

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


def permission_rules() -> list:
    py = shell_path(sys.executable)
    tails = ["--search *", "--buffer *", "--list*", "--recall-thread *", "--repair*", "--stop*"]
    return [f"Bash({py} -m n3mc_mcp.cli {t})" for t in tails] + [f"Bash(n3mc-lite {t})" for t in tails]


def _old_mcp_messages() -> list:
    out = []
    cj = pathlib.Path(os.path.expanduser("~")) / ".claude.json"
    try:
        d = json.loads(cj.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        d = None
    if isinstance(d, dict):
        found = isinstance(d.get("mcpServers"), dict) and "n3mc-workingmemory" in d["mcpServers"]
        projs = d.get("projects") if isinstance(d.get("projects"), dict) else {}
        for proj, v in projs.items():
            if isinstance(v, dict) and isinstance(v.get("mcpServers"), dict) and \
                    "n3mc-workingmemory" in v["mcpServers"]:
                out.append(f"An MCP registration of n3mc-workingmemory was found in Claude Code ({cj}, project "
                           f"{proj}). Claude Code now uses the hooks; remove it with: claude mcp remove "
                           f"n3mc-workingmemory -s local (run it in that project)")
        if found:
            out.insert(0, f"An MCP registration of n3mc-workingmemory was found in Claude Code ({cj}). Claude "
                          f"Code now uses the hooks; remove it with: claude mcp remove n3mc-workingmemory -s user")
    mj = pathlib.Path.cwd() / ".mcp.json"
    try:
        m = json.loads(mj.read_text(encoding="utf-8-sig"))
        if isinstance(m, dict) and isinstance(m.get("mcpServers"), dict) and "n3mc-workingmemory" in m["mcpServers"]:
            out.append(f"An MCP registration of n3mc-workingmemory was found in Claude Code ({mj}). Claude Code "
                       f"now uses the hooks; remove the \"n3mc-workingmemory\" entry from {mj}")
    except Exception:  # noqa: BLE001
        pass
    return out


def cmd_init() -> int:
    config_mod.load_config()
    sp = pathlib.Path(os.path.expanduser("~")) / ".claude" / "settings.json"
    existed = sp.exists()
    if existed:
        try:
            s = json.loads(sp.read_text(encoding="utf-8-sig"))
            if not isinstance(s, dict):
                raise ValueError("the top level is not a JSON object")
        except Exception as e:  # noqa: BLE001
            print(f"Could not read {sp} ({type(e).__name__}: {e}); nothing was changed.")
            return 1
    else:
        s = {}
    hooks = s.get("hooks", {})
    perms = s.get("permissions", {})
    allow = perms.get("allow", []) if isinstance(perms, dict) else None
    if not isinstance(hooks, dict) or not isinstance(perms, dict) or not isinstance(allow, list) or \
            any(not isinstance(hooks.get(ev, []), list) for ev in OURS):
        print(f"{sp} has an unexpected \"hooks\" or \"permissions\" structure; nothing was changed.")
        return 1
    before = json.dumps(s, sort_keys=True, ensure_ascii=False)
    plugins = s.get("enabledPlugins") if isinstance(s.get("enabledPlugins"), dict) else {}
    plugin_on = plugins.get(PLUGIN_KEY) is True
    messages = []
    for event in OURS:
        new_entries = []
        for e in hooks.get(event, []):
            if isinstance(e, dict) and isinstance(e.get("hooks"), list):
                kept = [h for h in e["hooks"]
                        if not (isinstance(h, dict) and isinstance(h.get("command"), str)
                                and is_own_hook(h["command"], event))]
                for h in kept:
                    cmd = h.get("command", "") if isinstance(h, dict) else ""
                    if isinstance(cmd, str) and "n3mc" in cmd.lower():
                        messages.append(f"Another N3MC hook is registered for {event} and was left in place: {cmd}")
                if kept:
                    e2 = dict(e)
                    e2["hooks"] = kept
                    new_entries.append(e2)
            else:
                new_entries.append(e)
        if not plugin_on:
            cmd = resolve_cmd(OURS[event][0])
            if cmd.startswith('"'):
                messages.append(f"The hook command for {event} starts with a quote because {cmd} contains a "
                                f"space; if the hook does not run, install Python into a folder without spaces.")
            new_entries.append({"hooks": [{"type": "command", "command": cmd, "timeout": 60}]})
        hooks[event] = new_entries
    s["hooks"] = hooks
    for r in permission_rules():
        if r not in allow:
            allow.append(r)
    perms["allow"] = allow
    s["permissions"] = perms
    if json.dumps(s, sort_keys=True, ensure_ascii=False) != before:
        sp.parent.mkdir(parents=True, exist_ok=True)
        if existed:
            shutil.copy2(sp, sp.with_name(f"settings.json.bak-n3mc-lite-{time.strftime('%Y%m%d%H%M%S')}"))
        paths._write_atomic(sp, (json.dumps(s, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    for m in messages + _old_mcp_messages():
        print(m)
    if plugin_on:
        print("The n3mc-workingmemory plugin is enabled and registers the N3MC Lite hooks, so n3mc-lite --init "
              "added no hooks.")
    else:
        print(f"N3MC Lite hooks registered in {sp}. Saving starts with the next Claude Code session.")
    return 0


# ============================================================ the server process (--run-server)

def _record_start_failure(what) -> None:
    """The server process could not start: write the marker and the notice itself.

    The hooks call ensure_server with max_wait=0 and never see the child exit, so without this
    a failing server was started again on every hook, with no notice and no 5-minute pause."""
    log = paths.server_log_path()
    _notify("server_start_failed",
            f"The N3MC Lite memory server could not start ({what}). See {log} for the reason.")
    try:
        paths.start_failed_path().write_text(paths.utc_now_iso(), encoding="utf-8")
    except OSError:
        pass


def run_server() -> int:
    try:
        return _run_server()
    except KeyboardInterrupt:
        raise
    except SystemExit as e:  # uvicorn exits with SystemExit when it cannot bind the port
        if e.code not in (None, 0):
            _record_start_failure(f"exit code {e.code}")
        raise
    except BaseException as e:  # noqa: BLE001
        _record_start_failure(f"{type(e).__name__}: {e}")
        raise


def _run_server() -> int:
    cfg = config_mod.load_config()
    if cfg.get("bind_host") != "127.0.0.1":
        _stderr(f"N3MC Lite: WARNING: bind_host is {cfg.get('bind_host')!r}. The server has no "
                f"authentication; any other value exposes the memory to the network.")
    from n3mc_mcp import processor as P
    from n3mc_mcp.database import Database
    db = Database(cfg)
    db.ensure_connected()
    P.import_backend()
    import uvicorn
    from n3mc_mcp.app import create_app

    @contextlib.asynccontextmanager
    async def lifespan(app):
        threading.Thread(target=lambda: P.get_model(cfg["embedding_model"], wait=1), daemon=True).start()
        yield

    app = create_app(cfg, db, lifespan=lifespan)
    pidp = paths.pid_path()
    try:
        with open(pidp, "x", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    try:
        uvicorn.run(app, host=cfg["bind_host"], port=int(cfg["server_port"]), log_level="error")
    finally:
        try:
            if pidp.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pidp.unlink()
        except OSError:
            pass
    return 0


# ============================================================ tool commands (§13)

def _tool(cfg, name, args) -> int:
    if ensure_server(cfg):
        try:
            status, res = _post(f"/tools/{name}?nudge=0", args, int(cfg["server_port"]), 300)
            if status == 200 and isinstance(res, dict) and isinstance(res.get("text"), str):
                print(res["text"])
                return 0
            _set_reason(f"HTTPError: status {status}")
        except Exception as e:  # noqa: BLE001
            _set_reason(f"{type(e).__name__}: {e}")
    reason = last_reason()
    if name == "save_memory":
        res, _ = direct_save_tool(cfg, args, reason)
        print(json.dumps(res, ensure_ascii=False))
    else:
        print(unavailable_text(name, reason))
    return 0


def cmd_stop_server(cfg) -> int:
    port = int(cfg["server_port"])
    h = _health(port)
    if _health_decision(h) in ("ok", "stale"):
        try:
            status, _ = _request_shutdown(port, str(paths.data_dir()))
        except Exception:  # noqa: BLE001
            status = None
        if status == 200:
            _wait_port_free(port)
            print(f"Stopped the N3MC Lite server (pid {h.get('pid')}).")
            return 0
    print("No N3MC Lite server of this data folder is running.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="n3mc-lite", description="N3MemoryCore MCP-Lite CLI")
    g = ap.add_mutually_exclusive_group(required=True)
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
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--before", type=int)
    ap.add_argument("--after", type=int)
    ap.add_argument("--agent-name")
    ap.add_argument("--session-id")
    ap.add_argument("--turn-id")
    ap.add_argument("--importance", type=float)
    ap.add_argument("--uvx", action="store_true")
    a = ap.parse_args(argv)

    if a.hook_submit:
        return cmd_hook_submit(a.uvx)
    if a.save_claude_turn:
        return cmd_save_claude_turn(a.uvx)
    if a.stop:
        return cmd_stop(a.uvx)
    if a.init:
        return cmd_init()
    if a.run_server:
        return run_server()

    cfg = config_mod.load_config()
    if a.stop_server:
        return cmd_stop_server(cfg)
    from n3mc_mcp.processor import sanitize_surrogates
    for name in ("search", "buffer", "recall_thread", "delete", "delete_session", "agent_name", "session_id",
                 "turn_id", "since", "until"):
        setattr(a, name, sanitize_surrogates(getattr(a, name)))
    if a.search is not None:
        from n3mc_mcp.tools import BoundError, parse_bounds
        try:
            parse_bounds(a.since, a.until)
        except BoundError as e:
            _stderr(str(e))
            return 2
        ok = ensure_server(cfg)
        sys.stdout.write(_search_block(cfg, ok, a.search, session_id=os.environ.get("N3MC_SESSION_ID") or None,
                                       since=a.since, until=a.until, limit=a.limit))
        return 0
    if a.buffer is not None:
        text = _read_stdin_buffer() if a.buffer == "-" else a.buffer
        args = {"content": text, "agent_name": a.agent_name or "claude-code"}
        sid = a.session_id or os.environ.get("N3MC_SESSION_ID")
        if sid:
            args["session_id"] = sid
        if a.turn_id:
            args["turn_id"] = a.turn_id
        if a.importance is not None:
            args["importance"] = a.importance
        return _tool(cfg, "save_memory", args)
    if a.list:
        return _tool(cfg, "list_memories", {"limit": a.limit} if a.limit is not None else {})
    if a.recall_thread is not None:
        args = {"turn_id": a.recall_thread}
        if a.before is not None:
            args["before"] = a.before
        if a.after is not None:
            args["after"] = a.after
        return _tool(cfg, "recall_thread", args)
    if a.delete is not None:
        return _tool(cfg, "delete_memory", {"id": a.delete})
    if a.delete_session is not None:
        return _tool(cfg, "delete_memories_by_session", {"session_id": a.delete_session})
    if a.repair:
        return _tool(cfg, "repair_memory", {})
    return 2


if __name__ == "__main__":
    sys.exit(main())
