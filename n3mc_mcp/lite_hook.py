"""UserPromptSubmit hook entry (spec §12.1): audit.log first, then `python -m n3mc_mcp.cli
--hook-submit` with the same stdin. Always exits 0."""
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

from n3mc_mcp import paths  # noqa: E402
from n3mc_mcp.processor import sanitize_surrogates  # noqa: E402

CHILD_TIMEOUT = 55
HOOK_NAME = "UserPromptSubmit"
STEPS = (["--hook-submit"],)


def read_stdin_bytes() -> bytes:
    try:
        return sys.stdin.buffer.read()
    except Exception:  # noqa: BLE001
        try:
            return sys.stdin.read().encode("utf-8", "surrogateescape")
        except Exception:  # noqa: BLE001
            return b""


def audit_first(hook: str, data: bytes) -> None:
    """Step 0: the audit record, before anything else (§12.2)."""
    try:
        raw = sanitize_surrogates(data.decode("utf-8", "surrogateescape"))
        try:
            payload = sanitize_surrogates(json.loads(raw))
        except ValueError:
            payload = {"raw": raw}
        paths.append_audit({"ts": paths.utc_now_iso(), "hook": hook, "raw": raw[:4096], "payload": payload})
    except Exception as e:  # noqa: BLE001
        try:
            print(f"n3mc-lite: could not write audit.log ({type(e).__name__}: {e})", file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass


def run_step(args, data: bytes, deadline: float) -> None:
    remaining = max(1.0, deadline - time.monotonic())
    out = b""
    try:
        r = subprocess.run([*paths.python_cmd(), "-m", "n3mc_mcp.cli", *args], input=data,
                           stdout=subprocess.PIPE, timeout=remaining)
        out = r.stdout or b""
    except subprocess.TimeoutExpired as e:
        out = e.output or b""
        print(f"n3mc-lite: {' '.join(args)} timed out", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"n3mc-lite: {' '.join(args)} failed ({type(e).__name__}: {e})", file=sys.stderr)
    if out:
        try:
            sys.stdout.buffer.write(out)
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            pass


def run_hook(hook: str, steps, argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    extra = ["--uvx"] if "--uvx" in argv else []
    try:
        deadline = time.monotonic() + CHILD_TIMEOUT
        data = read_stdin_bytes()
        audit_first(hook, data)
        for step in steps:
            run_step([*step, *extra], data, deadline)
    except Exception as e:  # noqa: BLE001
        try:
            print(f"n3mc-lite: hook error ({type(e).__name__}: {e})", file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass
    return 0


def main(argv=None) -> int:
    run_hook(HOOK_NAME, STEPS, argv)
    return 0


if __name__ == "__main__":
    main()
    sys.exit(0)
