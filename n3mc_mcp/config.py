"""
Configuration loading (spec §6, §3.9 step 1).

v1.7.0: a config.json the user mistyped is NEVER overwritten.
  - missing file            → write defaults with fresh owner_id / local_id
  - unreadable / not object → leave the file untouched, run on defaults with
                              per-process UUIDs, notice `config_unreadable`
  - wrongly typed item      → ignore only that item (use its default), leave
                              the file untouched, notice `config_type`
  - readable                → fill missing fields with defaults and save
Notices are collected in cfg["_notices"] (list[str]) and printed to stderr;
server.py prepends them to the first search_memory output of the process.
"""
import json
import os
import sys
import uuid
from typing import Any

from .paths import get_config_path

# Complete schema from spec §6 — all fields with their defaults.
# (`dedup_threshold` and `context_char_limit` were removed in v1.7.0; if an
# existing config.json still has them they are kept in the file but ignored.)
DEFAULTS: dict[str, Any] = {
    "redis_url":                "redis://localhost:6379/0",
    "ttl_seconds":              604800,
    "half_life_days":           3,
    "bm25_min_threshold":       0.1,
    "search_result_limit":      20,
    "min_score":                0.2,
    # §3.14 search result shaping (v1.7.0). vector_min_cos / version_sim_threshold
    # were measured with multilingual-e5-base; re-measure for another model.
    "vector_min_cos":           0.85,
    "relative_min_score_ratio": 0.6,
    "version_order_enabled":    True,
    "version_sim_threshold":    0.98,
    "version_min_chars":        100,
    "search_query_max_chars":   2000,
    "chunk_threshold":          400,
    "chunk_overlap":            100,
    "access_count_enabled":     True,
    "access_count_weight":      0.02,
    "access_count_max_boost":   0.5,
    "ttl_refresh_on_search":    True,
    "ttl_refresh_top_k":        5,
    "lexical_rerank_enabled":   True,
    "rerank_weight":            0.3,
    "rerank_phrase_weight":     0.2,
    "b_session_match":          1.0,
    "b_session_mismatch":       0.6,
    "skip_code_blocks":         False,
    # Embedding model (spec §3.2). Operator-selectable so the quality /
    # resource / language tradeoff is the deployment's choice, not baked in.
    # `embedding_dim` MUST match the chosen model's output dimension and is
    # used to build the RediSearch vector index. Changing either requires a
    # process restart and an index flush (different dim = incompatible vectors).
    "embedding_model":          "intfloat/multilingual-e5-base",
    "embedding_dim":            768,
}

# Keys generated per install (no default value; not type-checked, spec §6).
_ID_KEYS = ("owner_id", "local_id")


def config_value_ok(key: str, value: Any) -> bool:
    """Type check for one config item (spec §6 `config_value_ok`).

    Default is a bool   → accept only bool.
    Default is a number → accept int or float, but NOT bool.
    Default is a string → accept only str.
    Keys without a default (owner_id, local_id) and unknown keys → always ok.
    """
    if key not in DEFAULTS:
        return True
    default = DEFAULTS[key]
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, str)
    return True


def _type_name(default: Any) -> str:
    if isinstance(default, bool):
        return "true/false"
    if isinstance(default, (int, float)):
        return "number"
    if isinstance(default, str):
        return "string"
    return type(default).__name__


def _notice(notices: list[str], message: str) -> None:
    notices.append(message)
    print(f"[n3mc] {message}", file=sys.stderr)


def _defaults_with_fresh_ids() -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "owner_id": str(uuid.uuid4()),
        "local_id": str(uuid.uuid4()),
    }
    cfg.update(DEFAULTS)
    return cfg


def load_config() -> dict[str, Any]:
    path = get_config_path()
    notices: list[str] = []
    file_data: dict[str, Any] | None = None   # what is (or will be) on disk
    persist = False

    if not path.exists():
        # Case: file missing → write defaults with fresh UUIDs.
        cfg = _defaults_with_fresh_ids()
        file_data = dict(cfg)
        persist = True
    else:
        data: Any = None
        error = ""
        try:
            # utf-8-sig: tolerate the BOM some Windows editors add.
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                error = "the top level is not a JSON object"
        except Exception as e:
            error = f"{type(e).__name__}: {e}"

        if error:
            # Case: unreadable → do NOT modify the file; defaults + per-process
            # UUIDs (not saved).
            cfg = _defaults_with_fresh_ids()
            _notice(
                notices,
                f"config_unreadable: {path} could not be read ({error}). "
                "The file was left unchanged; this server process is running "
                "on default settings with temporary IDs. Fix the file and "
                "restart the MCP client.",
            )
        else:
            assert isinstance(data, dict)
            cfg = dict(data)
            bad: list[str] = []
            for k, v in data.items():
                if not config_value_ok(k, v):
                    bad.append(
                        f"{k}={json.dumps(v, ensure_ascii=False)} "
                        f"(expected {_type_name(DEFAULTS[k])}; "
                        f"using default {json.dumps(DEFAULTS[k])})"
                    )
                    cfg[k] = DEFAULTS[k]

            missing = [k for k in (*_ID_KEYS, *DEFAULTS) if k not in data]
            for k in missing:
                if k in _ID_KEYS:
                    cfg[k] = str(uuid.uuid4())
                else:
                    cfg[k] = DEFAULTS[k]

            if bad:
                # Case: wrongly typed item(s) → ignore only those items and
                # do NOT modify the file.
                _notice(
                    notices,
                    "config_type: wrong type in " + str(path) + ": "
                    + "; ".join(bad)
                    + ". The file was left unchanged. Fix the value(s) "
                    "(e.g. remove the quotes around a number) and restart "
                    "the MCP client.",
                )
            elif missing:
                # Case: readable → fill missing fields and save. Only the
                # file's own values + filled defaults are written (never the
                # env override or internal `_` keys).
                file_data = dict(data)
                for k in missing:
                    file_data[k] = cfg[k]
                persist = True

    if persist and file_data is not None:
        _save_config(file_data)

    # N3MC_REDIS_URL env var overrides config file (spec §3.3). Applied after
    # persisting so the override never leaks into config.json.
    redis_env = os.environ.get("N3MC_REDIS_URL")
    if redis_env:
        cfg["redis_url"] = redis_env

    cfg["_notices"] = notices
    return cfg


def _save_config(cfg: dict[str, Any]) -> None:
    path = get_config_path()
    data = {k: v for k, v in cfg.items() if not str(k).startswith("_")}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        print(f"[n3mc] could not write {path}: {e}", file=sys.stderr)
