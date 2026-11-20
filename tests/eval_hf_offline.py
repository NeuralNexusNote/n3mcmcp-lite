"""§A6 layer-3 — HuggingFace unreachable. Spawn n3mc-workingmemory with
HF_HUB_OFFLINE=1 and HF_HOME pointing at an empty dir, simulating "no
cache + no network". Verify (a) initialize fails or recovers gracefully,
(b) error mentions the missing model, (c) no hang.

v2.0.0 (2026-11-20): the MCP entry answers `initialize` itself and never
loads the model; the resident server loads it. So the script also calls
`search_memory`, and checks (b) in that answer: the "could not be loaded"
notice (§3.9) with a keyword-only search. It runs in a temporary data
folder with its own server_port, so the resident server is started by this
probe under the offline environment (an already running server would have
the model loaded), and that server is stopped at the end.

The search needs Redis. The temporary folder uses N3MC_EVAL_REDIS_URL, or
the Lite default redis://127.0.0.1:6379/0. The folder has a new owner_id,
so the search sees none of the existing memories and saves nothing.

Usage: python tests/eval_hf_offline.py
"""
from __future__ import annotations
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main() -> None:
    redis_url = os.environ.get("N3MC_EVAL_REDIS_URL") or "redis://127.0.0.1:6379/0"
    data_dir = tempfile.mkdtemp(prefix="n3mc-eval-hf-")
    with open(os.path.join(data_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"redis_url": redis_url, "server_port": _free_port()}, f)

    with tempfile.TemporaryDirectory(prefix="hf-empty-") as empty_hf_home:
        env = os.environ.copy()
        env["HF_HUB_OFFLINE"] = "1"
        env["TRANSFORMERS_OFFLINE"] = "1"
        env["HF_HOME"] = empty_hf_home
        env["HUGGINGFACE_HUB_CACHE"] = empty_hf_home
        env["N3MC_DATA_DIR"] = data_dir
        env.pop("N3MC_REDIS_URL", None)

        print(f"HF_HOME = {empty_hf_home} (fresh empty dir)")
        print("HF_HUB_OFFLINE = 1 (no network allowed)")
        print(f"N3MC_DATA_DIR = {data_dir} (temporary, redis_url = {redis_url})")
        print("Spawning n3mc-workingmemory ...")

        t0 = time.perf_counter()
        proc = subprocess.Popen(
            ["n3mc-workingmemory"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env,
        )

        def send(obj):
            proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
            proc.stdin.flush()

        # Wait for the answer with `want_id`, or process death, with a hard timeout
        def deadline_loop(want_id, deadline):
            while time.perf_counter() < deadline:
                if proc.poll() is not None:
                    return "exited"
                line = b""
                try:
                    line = proc.stdout.readline()
                except Exception:
                    pass
                if line:
                    try:
                        obj = json.loads(line.decode("utf-8"))
                        if obj.get("id") == want_id:
                            return obj
                    except Exception:
                        pass
            return "deadline"

        try:
            send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                             "clientInfo": {"name": "hf-offline-probe", "version": "0.1"}}})
            outcome = deadline_loop(1, t0 + 90.0)
            init_elapsed = round(time.perf_counter() - t0, 2)

            search_outcome, search_elapsed = None, None
            if isinstance(outcome, dict):
                send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                t1 = time.perf_counter()
                send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "search_memory",
                                 "arguments": {"query": "hf offline probe"}}})
                # The server start (library import) and the failed model load take a while.
                search_outcome = deadline_loop(2, t1 + 240.0)
                search_elapsed = round(time.perf_counter() - t1, 2)
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
            except Exception:
                pass
            stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
            subprocess.run([sys.executable, "-m", "n3mc_mcp.cli", "--stop-server"], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)

    search_text = ""
    if isinstance(search_outcome, dict):
        for blk in search_outcome.get("result", {}).get("content", []):
            if blk.get("type") == "text":
                search_text += blk.get("text", "")

    result = {
        "initialize_seconds": init_elapsed,
        "outcome_class": (
            "initialize_succeeded" if isinstance(outcome, dict)
            else "process_exited_before_initialize" if outcome == "exited"
            else "deadline_reached_no_response"),
        "server_info": (outcome.get("result", {}).get("serverInfo")
                        if isinstance(outcome, dict) else None),
        "search_seconds": search_elapsed,
        "search_outcome_class": (
            None if search_outcome is None
            else "search_answered" if isinstance(search_outcome, dict)
            else "process_exited_before_search_answer" if search_outcome == "exited"
            else "deadline_reached_no_search_answer"),
        "search_text_excerpt": search_text[:800],
        "exit_code_after_terminate": proc.returncode,
        "stderr_lines": len(stderr.splitlines()),
        "stderr_has_traceback": "Traceback" in stderr,
        "stderr_excerpt": stderr[-1500:],
    }

    # Heuristic checks (on the search answer, where v2.0.0 reports the model state)
    combined = (search_text + " " + stderr).lower()
    result["mentions_model_not_loaded"] = "could not be loaded" in combined
    result["mentions_huggingface"] = (
        "huggingface" in combined or "hf hub" in combined
        or "intfloat" in combined or "e5-base" in combined)
    result["mentions_keyword_only"] = "keyword-only" in combined or "bm25" in combined
    result["mentions_backend_unreachable"] = "not reachable" in combined
    shutil.rmtree(data_dir, ignore_errors=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
