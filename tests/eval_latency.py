"""§10-1 latency harness — spawn n3mc-workingmemory as stdio child,
measure initialize, first search_memory, and 5x steady-state median."""
from __future__ import annotations
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path


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


def run(redis_down: bool = False) -> dict:
    env = None
    if redis_down:
        # Point at unreachable Redis to simulate down state without
        # actually stopping the running container (safer for shared instance).
        import os as _os
        env = _os.environ.copy()
        env["N3MC_REDIS_URL"] = "redis://127.0.0.1:16379/0"  # bad port

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


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
    out = run(redis_down=(mode == "redis-down"))
    print(json.dumps(out, ensure_ascii=False, indent=2))
