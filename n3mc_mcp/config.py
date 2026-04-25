import json
import os
import sys
import uuid
from typing import Any

from .paths import get_config_path

DEFAULTS: dict[str, Any] = {
    "redis_url": "redis://localhost:6379/0",
    "ttl_seconds": 604800,
    "dedup_threshold": 0.95,
    "half_life_days": 3,
    "bm25_min_threshold": 0.1,
    "search_result_limit": 20,
    "context_char_limit": 3000,
    "min_score": 0.2,
    "search_query_max_chars": 2000,
    "chunk_threshold": 400,
    "chunk_overlap": 100,
    "access_count_enabled": True,
    "access_count_weight": 0.02,
    "access_count_max_boost": 0.5,
    "ttl_refresh_on_search": True,
    "ttl_refresh_top_k": 5,
    "lexical_rerank_enabled": True,
    "rerank_weight": 0.3,
    "rerank_phrase_weight": 0.2,
    "skip_code_blocks": False,
}


def load_config() -> dict[str, Any]:
    path = get_config_path()
    cfg: dict[str, Any] = {}

    if path.exists():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            print("[n3mc] config.json parse error — resetting to defaults", file=sys.stderr)
            cfg = {}

    changed = False
    if "owner_id" not in cfg:
        cfg["owner_id"] = str(uuid.uuid4())
        changed = True
    if "local_id" not in cfg:
        cfg["local_id"] = str(uuid.uuid4())
        changed = True
    # `session_id` is intentionally NOT persisted in config.json. Pro
    # spec §3.1 / §3.6 defines it as a per-process UUID, used by Pro's
    # `b_session` ranking factor to prefer the current session's writes
    # over stale cross-session ones. **Lite has no `b_session`** (Pro
    # spec §3.6: "Lite は 7 日窓で自然に収束するため `b_session` を
    # 持たない") because the 7-day TTL window already collapses
    # freshness via `time_decay`, so a per-process fallback that
    # changes across restarts is harmless here. The field is still
    # stored on each row for compatibility with Pro and for
    # `delete_memories_by_session`, but it is not a ranking signal.

    for k, v in DEFAULTS.items():
        if k not in cfg:
            cfg[k] = v
            changed = True

    redis_env = os.environ.get("N3MC_REDIS_URL")
    if redis_env:
        cfg["redis_url"] = redis_env

    if changed:
        _save_config(cfg)

    return cfg


def _save_config(cfg: dict[str, Any]) -> None:
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
