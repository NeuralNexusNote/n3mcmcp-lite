"""UserPromptSubmit hook entry (spec §12.1): audit.log first, then `cli --hook-submit`. Always exits 0."""

import sys

for _stream_name in ("stdin", "stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

import json  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402

from n3mc_mcp import paths  # noqa: E402

HOOK_NAME = "UserPromptSubmit"
STEPS = (["--hook-submit"],)
CHILD_TIMEOUT = 55  # for all steps together: --init registers the hook with a 60-second timeout


def run(hook_name, steps) -> int:
    try:
        raw = sys.stdin.buffer.read()
    except Exception:  # noqa: BLE001
        raw = b""
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        payload = None
    try:
        rawtext = raw.decode("utf-8", "replace")
        paths.append_audit({"ts": paths.utc_now_iso(), "hook": hook_name, "raw": rawtext[:4096],
                            "payload": payload if payload is not None else {"raw": rawtext}})
    except Exception as e:  # noqa: BLE001
        print(f"N3MC Lite: audit failed: {e}", file=sys.stderr)
    uvx = ["--uvx"] if "--uvx" in sys.argv[1:] else []
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    deadline = time.monotonic() + CHILD_TIMEOUT
    for step in steps:
        left = max(1.0, deadline - time.monotonic())  # the steps share CHILD_TIMEOUT (L7)
        try:
            r = subprocess.run([sys.executable, "-m", "n3mc_mcp.cli"] + step + uvx, input=raw,
                               stdout=subprocess.PIPE, timeout=left, **kw)
            if r.stdout:
                sys.stdout.buffer.write(r.stdout)
                sys.stdout.buffer.flush()
            if r.returncode != 0:
                print(f"N3MC Lite: {' '.join(step)} exited with {r.returncode}", file=sys.stderr)
        except subprocess.TimeoutExpired as e:
            if e.stdout:   # keep what was already printed (e.g. the search block) for Claude
                sys.stdout.buffer.write(e.stdout)
                sys.stdout.buffer.flush()
            print(f"N3MC Lite: {' '.join(step)} timed out after {left:.0f} s", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"N3MC Lite: {' '.join(step)} failed: {type(e).__name__}: {e}", file=sys.stderr)
    return 0


def main() -> int:
    try:
        run(HOOK_NAME, STEPS)
    except Exception as e:  # noqa: BLE001
        print(f"N3MC Lite: hook failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
