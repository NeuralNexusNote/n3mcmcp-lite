"""Stop hook entry (`n3mc-lite-stop-hook`): audit.log first, then `cli --save-claude-turn`, then `cli --stop`
(spec section 12.1)."""

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
        except Exception:  # noqa: BLE001
            pass

import time

from n3mc_mcp import lite_hook


def main(argv=None):
    started = time.monotonic()
    argv = sys.argv[1:] if argv is None else argv
    extra = ["--uvx"] if "--uvx" in argv else []
    try:
        data = sys.stdin.buffer.read()
    except Exception:  # noqa: BLE001
        data = b""
    raw = data.decode("utf-8", errors="surrogateescape")
    lite_hook.audit("Stop", raw, lite_hook.parse(raw))
    lite_hook.run_cli(["--save-claude-turn", *extra], data, started)
    # --stop reads the same JSON for its `cwd`; the remaining time of the shared budget is used.
    lite_hook.run_cli(["--stop", *extra], data, started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
