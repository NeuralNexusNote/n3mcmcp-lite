"""§A1 — Redis 停止状態で n3mc-workingmemory を spawn し、
ユーザー向けに正しいエラーメッセージが返るか・スタックトレースが
漏れないか・ハングしないかを検証する。

v2.0.0（2026-11-20）：動いている Redis を止めなくてよいように、一時的な
データフォルダを作り、その config.json の redis_url を使われていないポート
（16379）に、server_port を空いているポートにして起動する。MCP の入口が
起動した常駐サーバーは、最後にそのフォルダの --stop-server で止め、
フォルダを消す。"""
from __future__ import annotations
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def jrpc(p, method, params, _id, timeout=20.0):
    msg = {"jsonrpc": "2.0", "id": _id, "method": method, "params": params}
    p.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
    p.stdin.flush()
    t0 = time.perf_counter()
    while True:
        if time.perf_counter() - t0 > timeout:
            return {"_timeout": True}
        raw = p.stdout.readline()
        if not raw:
            return {"_eof": True}
        try:
            obj = json.loads(raw.decode("utf-8"))
        except Exception:
            continue
        if obj.get("id") == _id:
            return obj


def notify(p, method):
    p.stdin.write((json.dumps({"jsonrpc": "2.0", "method": method}) + "\n").encode("utf-8"))
    p.stdin.flush()


def main():
    # A temporary data folder whose Redis is an unused port (= Redis down)
    data_dir = tempfile.mkdtemp(prefix="n3mc-eval-redis-down-")
    with open(os.path.join(data_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"redis_url": "redis://127.0.0.1:16379/0", "server_port": _free_port()}, f)
    env = os.environ.copy()
    env["N3MC_DATA_DIR"] = data_dir
    env.pop("N3MC_REDIS_URL", None)
    proc = subprocess.Popen(
        ["n3mc-workingmemory"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env,
    )

    out: dict = {}

    t0 = time.perf_counter()
    init = jrpc(proc, "initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "redis-down-startup", "version": "0.1"},
    }, _id=1, timeout=60)
    out["initialize_seconds"] = round(time.perf_counter() - t0, 3)
    out["initialize_ok"] = (
        not init.get("_timeout") and not init.get("_eof") and "result" in init
    )
    if "result" in init:
        out["initialize_server_name"] = (
            init["result"].get("serverInfo", {}).get("name", "?")
        )
        out["instructions_present"] = bool(init["result"].get("instructions"))

    notify(proc, "notifications/initialized")

    # tools/list — should succeed even with Redis down
    tl = jrpc(proc, "tools/list", {}, _id=2, timeout=10)
    out["tools_list_ok"] = "result" in tl
    if "result" in tl:
        out["tools_count"] = len(tl["result"].get("tools", []))

    # save_memory call → expect user-friendly error
    sv = jrpc(proc, "tools/call", {
        "name": "save_memory",
        "arguments": {"content": "redis-down probe save"},
    }, _id=3, timeout=10)
    save_text = ""
    if "result" in sv:
        for blk in sv["result"].get("content", []):
            if blk.get("type") == "text":
                save_text += blk.get("text", "")
    out["save_returned"] = bool(save_text)
    out["save_isError"] = sv.get("result", {}).get("isError", None)
    out["save_text"] = save_text[:500]

    # search_memory call → expect user-friendly error
    sr = jrpc(proc, "tools/call", {
        "name": "search_memory",
        "arguments": {"query": "redis-down probe"},
    }, _id=4, timeout=10)
    search_text = ""
    if "result" in sr:
        for blk in sr["result"].get("content", []):
            if blk.get("type") == "text":
                search_text += blk.get("text", "")
    out["search_returned"] = bool(search_text)
    out["search_isError"] = sr.get("result", {}).get("isError", None)
    out["search_text"] = search_text[:500]

    # Heuristic checks
    combined = (save_text + " " + search_text).lower()
    out["mentions_redis"] = "redis" in combined
    out["mentions_docker_start"] = "docker start" in combined or "docker run" in combined
    out["mentions_unreachable"] = (
        "unreachable" in combined or "起動していません" in (save_text + search_text)
        or "起動して" in (save_text + search_text)
    )

    # Tear down + collect stderr
    try: proc.stdin.close()
    except Exception: pass
    try:
        proc.terminate()
        stderr = proc.stderr.read() or b""
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
        stderr = proc.stderr.read() or b""

    subprocess.run([sys.executable, "-m", "n3mc_mcp.cli", "--stop-server"], env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    shutil.rmtree(data_dir, ignore_errors=True)

    stderr_text = stderr.decode("utf-8", errors="replace")
    out["stderr_lines"] = len(stderr_text.splitlines())
    # Check for stack traces leaking to stderr
    out["stderr_has_traceback"] = "Traceback" in stderr_text
    out["stderr_excerpt"] = stderr_text[-1500:]

    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
