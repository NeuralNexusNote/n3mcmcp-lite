"""Stop hook entry: audit.log first, then `cli --save-claude-turn`, then `cli --stop` (spec §12.1)."""
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

import sys  # noqa: E402
import time  # noqa: E402

from n3mc_mcp.lite_hook import CHILD_TIMEOUT, audit_first, read_stdin, run_step  # noqa: E402

HOOK_NAME = "Stop"


def main() -> int:
    try:
        raw = read_stdin()
        audit_first(HOOK_NAME, raw)
        uvx = ["--uvx"] if "--uvx" in sys.argv[1:] else []
        deadline = time.monotonic() + CHILD_TIMEOUT
        run_step(["--save-claude-turn", *uvx], raw, deadline, forward_stdout=False)
        run_step(["--stop", *uvx], raw, deadline, forward_stdout=False)
    except Exception as e:  # noqa: BLE001
        try:
            print(f"[n3mc-lite] stop hook: {type(e).__name__}: {e}", file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
