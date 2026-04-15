"""
Config management for the N3MemoryCore MCP **Lite** server.

Config is a JSON file in the user data directory. Missing fields are filled
with defaults and UUIDs are auto-generated on first run. Unlike the paid
variant there is no DB-side fallback — Redis is ephemeral, so if the config
file is lost a fresh pair of UUIDs is simply generated.

The Redis connection URL can be overridden via:
  - ``N3MC_REDIS_URL`` environment variable (highest priority)
  - ``redis_url`` field in config.json
  - Default: ``redis://localhost:6379/0``
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from typing import Any

from .paths import config_path

DEFAULT_REDIS_URL = "redis://localhost:6379/0"
DEFAULT_TTL_SECONDS = 24 * 60 * 60  # 24 hours

DEFAULT_CONFIG: dict[str, Any] = {
    "owner_id": None,
    "local_id": None,
    "redis_url": DEFAULT_REDIS_URL,
    "ttl_seconds": DEFAULT_TTL_SECONDS,
    "dedup_threshold": 0.95,
    "half_life_days": 90,
    "bm25_min_threshold": 0.1,
    "search_result_limit": 20,
    "context_char_limit": 3000,
    "min_score": 0.2,
    "search_query_max_chars": 2000,
}


def load_config() -> dict:
    """Load config, auto-generating UUIDs and persisting on first run."""
    cfg = dict(DEFAULT_CONFIG)
    path = config_path()

    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
            cfg.update(loaded)
        except Exception as e:
            print(f"[N3MC-Lite] WARNING: config.json parse error: {e}", file=sys.stderr)

    # Env var override for Redis URL (wins over file)
    env_url = os.environ.get("N3MC_REDIS_URL")
    if env_url:
        cfg["redis_url"] = env_url

    changed = False
    if not cfg.get("owner_id"):
        cfg["owner_id"] = str(uuid.uuid4())
        changed = True
    if not cfg.get("local_id"):
        cfg["local_id"] = str(uuid.uuid4())
        changed = True

    for k, v in DEFAULT_CONFIG.items():
        if k not in cfg and v is not None:
            cfg[k] = v
            changed = True

    if changed:
        save_config(cfg)

    return cfg


def save_config(cfg: dict) -> None:
    path = config_path()
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[N3MC-Lite] WARNING: failed to write config.json: {e}", file=sys.stderr)
