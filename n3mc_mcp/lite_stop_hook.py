"""Stop hook entry (spec §12.1): audit.log first, then `cli --save-claude-turn`, then `cli --stop`. Always exits 0."""
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

from n3mc_mcp.lite_hook import run  # noqa: E402

HOOK_NAME = "Stop"
STEPS = (["--save-claude-turn"], ["--stop"])


def main() -> int:
    try:
        run(HOOK_NAME, STEPS)
    except Exception as e:  # noqa: BLE001
        print(f"N3MC Lite: hook failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
