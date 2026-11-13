"""
Configuration loading (spec §6, §3.9 step 1).

v1.7.0: a config.json the user mistyped is NEVER overwritten.
  - missing file            → create it EXCLUSIVELY (v1.7.1) with fresh
                              owner_id / local_id; if another process won,
                              read and use that file
  - unreadable / not object → leave the file untouched, run on defaults with
                              per-process UUIDs, notice `config_unreadable`
                              (save_memory also warns, v1.7.1)
  - wrong type / out of range (v1.7.1 table) → ignore only that item (use its
                              default), leave the file untouched, notice
                              `config_type`
  - readable                → fill missing fields with defaults and save
                              (temp file + os.replace, v1.7.1)
Notices are collected in cfg["_notices"] (list[str]) and printed to stderr;
server.py prepends them to the first search_memory output of the process.
"""
import json
import math
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from .paths import get_config_path, get_legacy_config_path

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


# Spec §6 "Allowed values" (v1.7.1, Lite review LM2): key -> (kind, range).
#   kind "int"    = JSON integer only (604800.5 and 768.0 are rejected)
#   kind "number" = int or float
#   range: (lo, lo_inclusive, hi, hi_inclusive); None = unbounded.
# chunk_overlap is special: >= 0 and < chunk_threshold (the value in effect).
_NUMERIC_RULES: dict[str, tuple[str, tuple]] = {
    "ttl_seconds":              ("int",    (1, True, None, False)),
    "search_result_limit":      ("int",    (1, True, None, False)),
    "search_query_max_chars":   ("int",    (1, True, None, False)),
    "embedding_dim":            ("int",    (1, True, None, False)),
    "chunk_threshold":          ("int",    (50, True, None, False)),
    "chunk_overlap":            ("int",    (0, True, None, False)),
    "ttl_refresh_top_k":        ("int",    (0, True, None, False)),
    "version_min_chars":        ("int",    (0, True, None, False)),
    "half_life_days":           ("number", (0, False, None, False)),
    "bm25_min_threshold":       ("number", (0, True, None, False)),
    "access_count_weight":      ("number", (0, True, None, False)),
    "access_count_max_boost":   ("number", (0, True, None, False)),
    "rerank_weight":            ("number", (0, True, None, False)),
    "rerank_phrase_weight":     ("number", (0, True, None, False)),
    "min_score":                ("number", (0, True, 1, True)),
    "vector_min_cos":           ("number", (0, True, 1, True)),
    "relative_min_score_ratio": ("number", (0, True, 1, True)),
    "version_sim_threshold":    ("number", (0, True, 1, True)),
    "b_session_match":          ("number", (0, True, 1, True)),
    "b_session_mismatch":       ("number", (0, True, 1, True)),
}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def config_value_ok(key: str, value: Any, chunk_threshold: Any = None) -> bool:
    """Type and range check for one config item (spec §6 `config_value_ok`).

    Default is a bool   → accept only bool.
    Default is a string → accept only str.
    Numbers follow the per-key table `_NUMERIC_RULES` (v1.7.1): "int" keys
    accept only JSON integers, every number key has an allowed range, and a
    bool is never a number.
    `chunk_overlap` must also be < *chunk_threshold* (the value in effect
    after checking chunk_threshold; defaults to DEFAULTS["chunk_threshold"]).
    Keys without a default (owner_id, local_id) and unknown keys → always ok.
    """
    if key not in DEFAULTS:
        return True
    default = DEFAULTS[key]
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, str)
    if isinstance(default, (int, float)):
        if not _is_number(value):
            return False
        if isinstance(value, float) and not math.isfinite(value):
            return False
        rule = _NUMERIC_RULES.get(key)
        if rule is None:
            return True
        kind, (lo, lo_inc, hi, hi_inc) = rule
        if kind == "int" and not isinstance(value, int):
            return False
        if lo is not None and (value < lo or (value == lo and not lo_inc)):
            return False
        if hi is not None and (value > hi or (value == hi and not hi_inc)):
            return False
        if key == "chunk_overlap":
            limit = DEFAULTS["chunk_threshold"] if chunk_threshold is None else chunk_threshold
            if value >= limit:
                return False
        return True
    return True


def _type_name(key: str, chunk_threshold: Any = None) -> str:
    default = DEFAULTS[key]
    if isinstance(default, bool):
        return "true/false"
    if isinstance(default, str):
        return "string"
    if isinstance(default, (int, float)):
        rule = _NUMERIC_RULES.get(key)
        if rule is None:
            return "number"
        kind, (lo, lo_inc, hi, hi_inc) = rule
        name = "integer" if kind == "int" else "number"
        parts = []
        if lo is not None:
            parts.append(f"{'>=' if lo_inc else '>'} {lo}")
        if hi is not None:
            parts.append(f"{'<=' if hi_inc else '<'} {hi}")
        if key == "chunk_overlap":
            limit = DEFAULTS["chunk_threshold"] if chunk_threshold is None else chunk_threshold
            parts.append(f"< chunk_threshold ({limit})")
        return name + (" " + " and ".join(parts) if parts else "")
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


def _tmp_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")


def _create_exclusive(path: Path, data: bytes) -> bool:
    """Create *path* with *data* only if it does not exist (spec §6, v1.7.1).

    Writes a temporary file in the same folder, then `os.link(tmp, path)`
    (fails if the target exists); where hard links are not available,
    `open(path, "x")`. Returns True when this process created the file,
    False when another process had created it first. Other errors raise.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
            return True
        except FileExistsError:
            return False
        except (OSError, NotImplementedError, AttributeError):
            # No hard links on this file system → exclusive open.
            try:
                with open(path, "xb") as f:
                    f.write(data)
                return True
            except FileExistsError:
                return False
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _replace_atomic(path: Path, data: bytes) -> None:
    """Write *data* to a temp file in the same folder, then os.replace it
    onto *path* (spec §6 / §7, v1.7.1): an interrupted write never leaves a
    truncated file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _config_bytes(cfg: dict[str, Any]) -> bytes:
    data = {k: v for k, v in cfg.items() if not str(k).startswith("_")}
    return json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")


def _migrate_legacy_config(path) -> None:
    """Spec §7 (v1.7.0): copy the v1.6.x config once, byte-for-byte.

    Only when the new file is missing and N3MC_DATA_DIR is not set. The
    legacy file is left in place. Failures are logged and ignored (a new
    config is then created as usual). v1.7.1: the copy uses the
    exclusive-create rule (temp file + os.link), so two processes migrating
    at once end up with one file.
    """
    if path.exists() or os.environ.get("N3MC_DATA_DIR"):
        return
    legacy = get_legacy_config_path()
    try:
        if not legacy.is_file():
            return
        if _create_exclusive(path, legacy.read_bytes()):
            print(f"[n3mc] config migrated from {legacy} to {path} "
                  "(the old file was left in place)", file=sys.stderr)
    except Exception as e:
        print(f"[n3mc] config migration from {legacy} failed: {e}", file=sys.stderr)


# Spec §6 (v1.7.1): after losing the create race, re-read the winner's file
# every 0.1 s for up to 2 s (a writer that does not use the rule may still be
# writing it).
_RACE_RETRY_SECONDS = 2.0
_RACE_RETRY_STEP = 0.1


def _read_config(path: Path) -> tuple[Any, str]:
    """(data, error) — error is "" when the file is a JSON object."""
    try:
        # utf-8-sig: tolerate the BOM some Windows editors add.
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    if not isinstance(data, dict):
        return data, "the top level is not a JSON object"
    return data, ""


def load_config() -> dict[str, Any]:
    path = get_config_path()
    _migrate_legacy_config(path)
    notices: list[str] = []
    file_data: dict[str, Any] | None = None   # file content to write back
    persist = False
    lost_race = False

    if not path.exists():
        # Case: file missing → create it EXCLUSIVELY with fresh UUIDs
        # (v1.7.1). If another process created it first, use that file.
        cfg = _defaults_with_fresh_ids()
        try:
            if _create_exclusive(path, _config_bytes(cfg)):
                return _finish(cfg, notices)
            lost_race = True
        except Exception as e:
            print(f"[n3mc] could not write {path}: {e}", file=sys.stderr)
            return _finish(cfg, notices)

    data, error = _read_config(path)
    if error and lost_race:
        deadline = time.monotonic() + _RACE_RETRY_SECONDS
        while error and time.monotonic() < deadline:
            time.sleep(_RACE_RETRY_STEP)
            data, error = _read_config(path)

    if error:
        # Case: unreadable → do NOT modify the file; defaults + per-process
        # UUIDs (not saved).
        cfg = _defaults_with_fresh_ids()
        cfg["_config_unreadable"] = str(path)
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
        # chunk_threshold first: chunk_overlap is checked against the value
        # in effect after this check (spec §6).
        ct = data.get("chunk_threshold", DEFAULTS["chunk_threshold"])
        if not config_value_ok("chunk_threshold", ct):
            ct = DEFAULTS["chunk_threshold"]
        ordered = sorted(data.items(), key=lambda kv: kv[0] != "chunk_threshold")
        for k, v in ordered:
            if not config_value_ok(k, v, chunk_threshold=ct):
                bad.append(
                    f"{k}={json.dumps(v, ensure_ascii=False)} "
                    f"(expected {_type_name(k, ct)}; "
                    f"using default {json.dumps(DEFAULTS[k])})"
                )
                cfg[k] = DEFAULTS[k]
        if cfg.get("chunk_overlap", DEFAULTS["chunk_overlap"]) >= ct:
            # The DEFAULT overlap (100) is itself >= a lowered chunk_threshold
            # (50..100): use a quarter of the threshold instead, so every
            # window advances by a useful amount (not stated in the spec).
            cfg["chunk_overlap"] = max(0, ct // 4)

        missing = [k for k in (*_ID_KEYS, *DEFAULTS) if k not in data]
        for k in missing:
            if k in _ID_KEYS:
                cfg[k] = str(uuid.uuid4())
            elif k not in cfg:
                cfg[k] = DEFAULTS[k]

        if bad:
            # Case: wrongly typed / out-of-range item(s) → ignore only those
            # items and do NOT modify the file.
            _notice(
                notices,
                "config_type: wrong type or value in " + str(path) + ": "
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

    return _finish(cfg, notices)


def _finish(cfg: dict[str, Any], notices: list[str]) -> dict[str, Any]:
    # N3MC_REDIS_URL env var overrides config file (spec §3.3). Applied after
    # persisting so the override never leaks into config.json.
    redis_env = os.environ.get("N3MC_REDIS_URL")
    if redis_env:
        cfg["redis_url"] = redis_env
    cfg["_notices"] = notices
    return cfg


def config_unreadable_detail(path: str) -> str:
    """save_memory `detail` while config.json is unreadable (spec §6, LL9)."""
    return (
        "config.json could not be read, so this entry was saved under a "
        "temporary owner_id and will not be visible after the server restarts. "
        f"Fix {path} and restart the MCP client."
    )


def _save_config(cfg: dict[str, Any]) -> None:
    """Update config.json atomically: temp file + os.replace (v1.7.1)."""
    path = get_config_path()
    try:
        _replace_atomic(path, _config_bytes(cfg))
    except Exception as e:
        print(f"[n3mc] could not write {path}: {e}", file=sys.stderr)
