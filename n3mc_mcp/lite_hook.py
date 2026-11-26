"""UserPromptSubmit hook entry: audit.log first, then `python -m n3mc_mcp.cli --hook-submit` (spec §12.1)."""
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

import json  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402

CHILD_TIMEOUT = 55
HOOK_NAME = "UserPromptSubmit"


def read_stdin() -> str:
    try:
        return sys.stdin.buffer.read().decode("utf-8", errors="surrogateescape")
    except Exception:  # noqa: BLE001
        try:
            return sys.stdin.read()
        except Exception:  # noqa: BLE001
            return ""


def audit_first(hook_name: str, raw: str):
    """Step 0: the audit record, before anything else (§12.2). Never raises."""
    try:
        from n3mc_mcp import paths
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"raw": raw}
        paths.append_audit({"ts": paths.utc_now_iso(), "hook": hook_name, "raw": raw[:4096], "payload": payload})
    except Exception as e:  # noqa: BLE001
        try:
            print(f"[n3mc-lite] audit.log: {type(e).__name__}: {e}", file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass


def run_step(args, raw: str, deadline: float, forward_stdout: bool) -> None:
    """One synchronous child step with the remaining share of CHILD_TIMEOUT (at least 1 s)."""
    from n3mc_mcp.paths import python_cmd
    timeout = max(1.0, deadline - time.monotonic())
    out = b""
    try:
        cp = subprocess.run([*python_cmd(), "-m", "n3mc_mcp.cli", *args],
                            input=raw.encode("utf-8", errors="surrogateescape"),
                            stdout=subprocess.PIPE, timeout=timeout)
        out = cp.stdout or b""
    except subprocess.TimeoutExpired as e:
        out = e.stdout or b""
        print(f"[n3mc-lite] {' '.join(args)} timed out after {timeout:.0f} s", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"[n3mc-lite] {' '.join(args)}: {type(e).__name__}: {e}", file=sys.stderr)
    if forward_stdout and out:
        try:
            sys.stdout.buffer.write(out)
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    try:
        raw = read_stdin()
        audit_first(HOOK_NAME, raw)
        uvx = ["--uvx"] if "--uvx" in sys.argv[1:] else []
        deadline = time.monotonic() + CHILD_TIMEOUT
        run_step(["--hook-submit", *uvx], raw, deadline, forward_stdout=True)
    except Exception as e:  # noqa: BLE001
        try:
            print(f"[n3mc-lite] hook: {type(e).__name__}: {e}", file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
