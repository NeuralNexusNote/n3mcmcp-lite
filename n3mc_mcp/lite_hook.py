"""UserPromptSubmit hook entry (`n3mc-lite-hook`): audit.log first, then `cli --hook-submit` (spec section 12.1)."""

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

import json
import re
import subprocess
import time
from datetime import datetime, timezone

from n3mc_mcp import paths

CHILD_TIMEOUT = 55          # seconds one hook entry shares between its child steps
_LONE_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def audit(hook, raw, payload):
    """Append the audit record before anything else. Never raises, never stops the hook."""
    try:
        paths.ensure_data_dir()
        record = {"ts": datetime.now(timezone.utc).isoformat(), "hook": hook, "raw": raw[:4096],
                  "payload": payload if payload is not None else {"raw": raw}}
        line = _LONE_SURROGATE_RE.sub("", json.dumps(record, ensure_ascii=False))
        with open(paths.audit_log_path(), "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
    except Exception as exc:  # noqa: BLE001
        print("N3MC Lite hook: could not write audit.log (%s)" % exc, file=sys.stderr)


def parse(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return None


def run_cli(args, data, started):
    """Run `python -m n3mc_mcp.cli <args>` synchronously with the remaining time; returns stdout bytes."""
    remaining = max(1.0, CHILD_TIMEOUT - (time.monotonic() - started))
    try:
        proc = subprocess.run([*paths.python_cmd(), "-m", "n3mc_mcp.cli", *args], input=data,
                              stdout=subprocess.PIPE, timeout=remaining)
        return proc.stdout or b""
    except subprocess.TimeoutExpired as exc:
        print("N3MC Lite hook: %s timed out" % " ".join(args), file=sys.stderr)
        return exc.stdout or b""
    except Exception as exc:  # noqa: BLE001
        print("N3MC Lite hook: %s failed (%s: %s)" % (" ".join(args), type(exc).__name__, exc), file=sys.stderr)
        return b""


def main(argv=None):
    started = time.monotonic()
    argv = sys.argv[1:] if argv is None else argv
    extra = ["--uvx"] if "--uvx" in argv else []
    try:
        data = sys.stdin.buffer.read()
    except Exception:  # noqa: BLE001
        data = b""
    raw = data.decode("utf-8", errors="surrogateescape")
    audit("UserPromptSubmit", raw, parse(raw))
    out = run_cli(["--hook-submit", *extra], data, started)
    try:
        sys.stdout.buffer.write(out)
        sys.stdout.buffer.flush()
    except Exception as exc:  # noqa: BLE001
        print("N3MC Lite hook: could not write stdout (%s)" % exc, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
