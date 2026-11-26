"""UserPromptSubmit hook entry `n3mc-lite-hook` (spec §12.1): step 0 appends the audit record, then
`python -m n3mc_mcp.cli --hook-submit` runs with the same stdin; its stdout is this hook's stdout.
Always exits 0.
"""
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

import json  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402

from n3mc_mcp import paths  # noqa: E402

CHILD_TIMEOUT = 55   # all steps of one entry share this many seconds (spec §12.1)


def read_stdin() -> bytes:
    try:
        return sys.stdin.buffer.read()
    except Exception:  # noqa: BLE001
        try:
            return sys.stdin.read().encode("utf-8", "surrogateescape")
        except Exception:  # noqa: BLE001
            return b""


def audit_first(hook: str, data: bytes) -> None:
    """Step 0: {"ts", "hook", "raw", "payload"} before anything else; never stops the hook."""
    try:
        text = data.decode("utf-8", "surrogateescape")
        try:
            payload = json.loads(text)
        except ValueError:
            payload = {"raw": text}
        paths.audit_append({"ts": paths.utc_now_iso(), "hook": hook, "raw": text[:4096], "payload": payload})
    except Exception as e:  # noqa: BLE001
        try:
            sys.stderr.write(f"[n3mc-lite] audit.log: {type(e).__name__}: {e}\n")
        except Exception:  # noqa: BLE001
            pass


def run_step(args, data: bytes, deadline: float) -> None:
    """One synchronous child step (`python -m n3mc_mcp.cli <args>`), sharing CHILD_TIMEOUT."""
    remaining = max(1.0, deadline - time.monotonic())
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass
    try:
        subprocess.run([*paths.python_cmd(), "-m", "n3mc_mcp.cli", *args], input=data, timeout=remaining,
                       check=False, **kwargs)
    except subprocess.TimeoutExpired:
        sys.stderr.write(f"[n3mc-lite] {' '.join(args)} did not finish within {int(remaining)} s\n")
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[n3mc-lite] {' '.join(args)} failed: {type(e).__name__}: {e}\n")


def main() -> int:
    deadline = time.monotonic() + CHILD_TIMEOUT
    try:
        uvx = ["--uvx"] if "--uvx" in sys.argv[1:] else []
        data = read_stdin()
        audit_first("UserPromptSubmit", data)
        run_step(["--hook-submit", *uvx], data, deadline)
    except Exception as e:  # noqa: BLE001
        try:
            sys.stderr.write(f"[n3mc-lite] hook: {type(e).__name__}: {e}\n")
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
