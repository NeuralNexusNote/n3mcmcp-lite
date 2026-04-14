"""Entry point: ``python -m n3mc_mcp`` or ``n3mc-mcp``."""
from __future__ import annotations

import asyncio
import sys

from .server import run


def _reconfigure_utf8() -> None:
    """Windows cp932 fix: force UTF-8 on stdio."""
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass


def main() -> None:
    _reconfigure_utf8()
    asyncio.run(run())


if __name__ == "__main__":
    main()
