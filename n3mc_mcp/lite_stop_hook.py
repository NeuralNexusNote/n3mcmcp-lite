"""Stop hook entry (spec §12.1): audit.log first, then `python -m n3mc_mcp.cli --save-claude-turn`,
then `python -m n3mc_mcp.cli --stop`, both with the same stdin. Always exits 0."""
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

from n3mc_mcp import lite_hook  # noqa: E402

HOOK_NAME = "Stop"
STEPS = (["--save-claude-turn"], ["--stop"])


def main(argv=None) -> int:
    lite_hook.run_hook(HOOK_NAME, STEPS, argv)
    return 0


if __name__ == "__main__":
    main()
    sys.exit(0)
