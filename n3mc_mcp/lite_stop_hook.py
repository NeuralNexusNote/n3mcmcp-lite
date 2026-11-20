"""Stop hook entry (spec §12.1): audit.log first, then `cli --save-claude-turn`, then `cli --stop`. Always exits 0."""

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
