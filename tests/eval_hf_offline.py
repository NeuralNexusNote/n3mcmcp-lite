"""§A6 layer-3 — HuggingFace unreachable. Spawn n3mc-workingmemory with
HF_HUB_OFFLINE=1 and HF_HOME pointing at an empty dir, simulating "no
cache + no network". Verify (a) initialize fails or recovers gracefully,
(b) error mentions the missing model, (c) no hang."""
from __future__ import annotations
import json
import os
import subprocess
import tempfile
import time

with tempfile.TemporaryDirectory(prefix="hf-empty-") as empty_hf_home:
    env = os.environ.copy()
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    env["HF_HOME"] = empty_hf_home
    env["HUGGINGFACE_HUB_CACHE"] = empty_hf_home

    print(f"HF_HOME = {empty_hf_home} (fresh empty dir)")
    print("HF_HUB_OFFLINE = 1 (no network allowed)")
    print("Spawning n3mc-workingmemory ...")

    t0 = time.perf_counter()
    proc = subprocess.Popen(
        ["n3mc-workingmemory"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env,
    )

    # Send initialize and wait
    init_msg = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "hf-offline-probe", "version": "0.1"}}}) + "\n"
    proc.stdin.write(init_msg.encode("utf-8"))
    proc.stdin.flush()

    # Wait for either initialize response or process death, with hard timeout
    def deadline_loop(deadline):
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
                    if obj.get("id") == 1:
                        return obj
                except Exception:
                    pass
        return "deadline"

    DEADLINE = 90.0
    outcome = deadline_loop(t0 + DEADLINE)
    elapsed = round(time.perf_counter() - t0, 2)

    # Capture stderr for diagnostic
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    except Exception:
        pass
    stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""

    result = {
        "elapsed_seconds": elapsed,
        "outcome_class": (
            "initialize_succeeded" if isinstance(outcome, dict)
            else "process_exited_before_initialize" if outcome == "exited"
            else "deadline_reached_no_response"),
        "exit_code_after_terminate": proc.returncode,
        "stderr_lines": len(stderr.splitlines()),
        "stderr_has_traceback": "Traceback" in stderr,
        "stderr_excerpt": stderr[-1500:],
    }
    if isinstance(outcome, dict):
        result["initialize_response"] = outcome

    # Heuristic checks
    combined = stderr.lower()
    result["mentions_huggingface"] = (
        "huggingface" in combined or "hf hub" in combined
        or "intfloat" in combined or "e5-base" in combined)
    result["mentions_offline"] = "offline" in combined
    result["mentions_cache"] = "cache" in combined or "kvasir" in combined
    print(json.dumps(result, ensure_ascii=False, indent=2))
