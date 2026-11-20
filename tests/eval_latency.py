"""§10-1 latency harness — spawn n3mc-workingmemory as stdio child,
measure initialize, first search_memory, and 5x steady-state median.

Usage: python tests/eval_latency.py [normal|redis-down]

v2.0.0 (2026-11-20): the MCP entry forwards to the resident server, which
uses the redis_url of config.json, so N3MC_REDIS_URL set for one client no
longer makes Redis unreachable (it only gives the redis_url_mismatch
notice). redis-down therefore runs in a temporary data folder whose
config.json points at an unused port, with its own server_port, and stops
that folder's server afterwards. normal uses the caller's environment."""
from __future__ import annotations
import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def jrpc(stream_in, stream_out, method, params=None, _id=1):
    msg = {"jsonrpc": "2.0", "id": _id, "method": method}
    if params is not None:
        msg["params"] = params
    line = json.dumps(msg) + "\n"
    t0 = time.perf_counter()
    stream_in.write(line.encode("utf-8"))
    stream_in.flush()
    # Read until we get a JSON object terminated by newline that has matching id
    while True:
        raw = stream_out.readline()
        if not raw:
            raise RuntimeError("server closed stdout")
        try:
            obj = json.loads(raw.decode("utf-8"))
        except Exception:
            continue
        if obj.get("id") == _id:
            t1 = time.perf_counter()
            return obj, (t1 - t0)


def notify(stream_in, method, params=None):
    msg = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    stream_in.write((json.dumps(msg) + "\n").encode("utf-8"))
    stream_in.flush()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _isolated_env(redis_url: str):
    """A temporary data folder with its own config.json (redis_url, server_port)."""
    data_dir = tempfile.mkdtemp(prefix="n3mc-eval-")
    with open(os.path.join(data_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"redis_url": redis_url, "server_port": _free_port()}, f)
    env = os.environ.copy()
    env["N3MC_DATA_DIR"] = data_dir
    env.pop("N3MC_REDIS_URL", None)
    return env, data_dir


def _stop_isolated(env, data_dir) -> None:
    """Stop the resident server of the temporary folder, then delete the folder."""
    subprocess.run([sys.executable, "-m", "n3mc_mcp.cli", "--stop-server"], env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    shutil.rmtree(data_dir, ignore_errors=True)


def run(redis_down: bool = False) -> dict:
    env, data_dir = None, None
    if redis_down:
        # Unreachable Redis without stopping the running container: a temporary
        # data folder whose config.json points at an unused port.
        env, data_dir = _isolated_env("redis://127.0.0.1:16379/0")

    proc = subprocess.Popen(
        ["n3mc-workingmemory"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        # initialize
        init_resp, init_dt = jrpc(
            proc.stdin, proc.stdout, "initialize",
            params={
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "eval-latency", "version": "0.1"},
            },
            _id=1,
        )
        notify(proc.stdin, "notifications/initialized")

        # First search
        first_resp, first_dt = jrpc(
            proc.stdin, proc.stdout, "tools/call",
            params={"name": "search_memory",
                    "arguments": {"query": "warmup query alpha"}},
            _id=2,
        )

        if redis_down:
            text = json.dumps(first_resp, ensure_ascii=False)
            return {
                "initialize_s": init_dt,
                "first_search_s": first_dt,
                "redis_down_response_excerpt": text[:600],
                "mentions_unreachable": "not reachable" in text or "unreachable" in text,
                "crashed": False,
            }

        # 5x steady-state
        steady = []
        for i in range(5):
            _, dt = jrpc(
                proc.stdin, proc.stdout, "tools/call",
                params={"name": "search_memory",
                        "arguments": {"query": f"steady probe {i}"}},
                _id=10 + i,
            )
            steady.append(dt)

        return {
            "initialize_s": init_dt,
            "first_search_s": first_dt,
            "steady_s": steady,
            "steady_median_s": statistics.median(steady),
        }
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        if data_dir:
            _stop_isolated(env, data_dir)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
    out = run(redis_down=(mode == "redis-down"))
    print(json.dumps(out, ensure_ascii=False, indent=2))
