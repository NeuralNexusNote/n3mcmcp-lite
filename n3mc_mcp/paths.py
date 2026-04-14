"""
Path resolution for the N3MemoryCore MCP **Trial** server.

Trial is ephemeral (Redis-backed, 24h TTL) and therefore owns no database
file. Only the local ``config.json`` (owner_id / local_id / ranking
parameters / Redis URL) lives on disk.

Default location:
  - Windows:  %LOCALAPPDATA%\\n3memorycore-trial
  - macOS:    ~/Library/Application Support/n3memorycore-trial
  - Linux:    ~/.local/share/n3memorycore-trial

Override via the environment variable ``N3MC_DATA_DIR``.
"""
from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_data_dir

_APP_NAME = "n3memorycore-trial"


def data_dir() -> Path:
    """Return the directory that holds the local config file."""
    override = os.environ.get("N3MC_DATA_DIR")
    if override:
        p = Path(override).expanduser().resolve()
    else:
        p = Path(user_data_dir(_APP_NAME, appauthor=False))
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_path() -> Path:
    return data_dir() / "config.json"
