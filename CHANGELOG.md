# Changelog

All notable changes to N3MemoryCore MCP Lite are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

_Nothing yet._

## [1.10.0] — 2026-11-14

Spec v1.6.0 → v1.7.0 (JP/EN). Fixes found while running the Pro edition
(2026-11-12 memo), applied to Lite through the specification; code
regenerated from the EN spec.

### Fixed
- **Updated facts were silently dropped**: `save_memory` rejected a new
  entry as `near_duplicate` when an existing one had cosine ≥ 0.95. With
  `multilingual-e5` that also hits a fact re-saved with a new value or a
  setting sheet re-made with a few changes, so the user's latest decision
  never reached memory. Only exact duplicates are rejected now (short
  entries and whole documents); `dedup_threshold` is removed.
- **BM25 leg lost on Japanese questions**: CJK punctuation (`「」`, `？`, …)
  was not stripped from the query, so bigrams such as `「記` were ANDed into
  the RediSearch query and `「記憶装置」は？` returned 0 keyword hits.
  Control characters made `FT.SEARCH` fail with a syntax error. Both are
  stripped now, and a BM25 failure never fails the search.
- **`since` / `until` missed in-range entries**: the time window was applied
  after ranking the global top-N. It is now a pre-filter inside the KNN and
  BM25 queries (`@timestamp_epoch:[…]`).
- **Mistyped `config.json` was overwritten**: an unreadable file was reset to
  defaults. It is now left untouched; wrongly typed items are ignored one by
  one, and a notice is shown on the first `search_memory`.
- **Tests could wipe the live working memory**: the suite FLUSHes DB 0 and
  defaulted to `redis://localhost:6379/0` (the MCP server's own Redis);
  `test_cross_session.py` hard-coded it and its subprocess servers used the
  user's real config. Tests now need `N3MC_REDIS_TEST_URL`, refuse the live
  `redis_url`, and run the subprocess servers against the test Redis and a
  temp data dir. CI uses a sidecar on port 6390.

- **Redis outages were not recoverable / were silent** (spec §3.9, found by
  the A1 abnormal-case test): Redis was checked only at startup, so a server
  started while Redis was stopped kept saying "Redis Stack is not running"
  after `docker start redis-stack` until the MCP client was restarted; and a
  Redis that stopped later made `search_memory` return `_No memories found._`
  and `save_memory` a raw `Error 10061` without a hint. Every tool call now
  PINGs / reconnects first (`ensure_connected()`), connection errors inside a
  tool are reported as the backend error, and the hint is English and shows
  the host:port from `redis_url` (no password). The hint also says to start
  Docker (Docker Desktop) first: with Docker itself stopped (A4 test),
  `docker start redis-stack` alone fails with a Docker API error.
- **Plugin hook failed silently when `settings.json` could not be updated**
  (spec §8, found by the A8 test): with a read-only or policy-locked
  `~/.claude/settings.json`, or invalid JSON in it, the hook exited 0 with no
  output, so the user never learned that the tools were not pre-approved. It
  now changes nothing and reports the reason (stderr + `systemMessage`) with
  the rule to add by hand; a read-only file that already has the rules stays
  quiet.
- **Offline without a cached model: every call retried the download, and
  keyword-only search was silent** (spec §3.9, found by the A6 test): each
  `save_memory` took ~24 s and each `search_memory` ~12 s, with no sign in
  the search output that the vector leg was off. A failed model load is now
  retried at most once every 60 s, `search_memory` carries a notice on every
  degraded call, and `save_memory` adds a `detail` explaining the
  `embedding_unavailable` warning.
- **Fresh installs crashed on start with `mcp` 2.x** (found while running the
  A3 abnormal-case test): `pyproject.toml` required `mcp>=1.0.0` with no upper
  bound, so `uvx` / `pip` / the plugin now install `mcp` 2.3.0, which removed
  the low-level `Server.list_tools()` decorator API — the server died on import
  (`AttributeError: 'Server' object has no attribute 'list_tools'`) and `/mcp`
  showed only `Connection closed`. This also affects the published 1.9.1.
  Pinned to `mcp>=1.0.0,<2.0.0` until the server is ported to the 2.x API.
- **Plugin auto-approve never matched, and failed silently without Python**
  (spec §8, found by the A2 abnormal-case test): the SessionStart hook added
  plain `mcp__n3mc-workingmemory__<tool>` rules, but Claude Code names a
  plugin's MCP tools `mcp__plugin_<plugin>_<server>__<tool>`, so plugin users
  were prompted on every memory call; `recall_thread` was also missing. The
  hook now adds `mcp__plugin_n3mc-workingmemory_n3mc-workingmemory__*` and
  `mcp__n3mc-workingmemory__*`. When no Python is on PATH, a Python-free last
  step prints a `systemMessage` warning (what breaks, what to add) and exits
  0 — a non-zero exit only produced a one-line `python: command not found`
  transcript notice, not an entry in the `/plugin` Errors tab as the spec
  claimed. The missing-`uvx` hint uses the same `systemMessage` JSON.
- **Memories split by launcher on Windows** (spec §7): the config lived in
  `%LOCALAPPDATA%`, which the Microsoft Store (MSIX) Claude desktop app
  redirects for the MCP servers it starts. One PC had two `config.json`
  files and two `owner_id`s, so memories saved from the desktop app were
  invisible to `claude` in a terminal (and vice versa) on the same Redis.
  The config now lives in `~/.n3mc-workingmemory/` and the old file is
  copied over once on first start.
- **Startup model load split in two** (spec §3.9): the embedding library is
  imported synchronously before stdio starts, and only the model is built in
  the background. Importing it in the background after stdio starts (as the
  spec's v1.6 note suggested) deadlocked on Windows — child processes spawned
  during the import block on the stdin pipe that the MCP reader is reading.
  `initialize` answers in ~10 s warm.

### Changed
- **Search result shaping**: vector candidates below `vector_min_cos`
  (0.85) are dropped; results below 0.6 × the top score are cut; versions of
  the same content (≥ 0.98 similar) are listed newest first. Each hit shows
  `saved=` in local time, with a note that the newest version is current.
- `serverInfo.version` follows the package version.
- New config keys: `vector_min_cos`, `relative_min_score_ratio`,
  `version_order_enabled`, `version_sim_threshold`, `version_min_chars`.
  Removed: `dedup_threshold`, `context_char_limit` (ignored if present).

## [1.9.1] — 2026-05-31

### Docs
- **Quickstart reordered to lead with spec-based generation** (§Quick Start,
  JP/EN + release copies): the N3MC series' primary workflow is having an AI
  generate the implementation FROM the spec; pip / marketplace install is a
  secondary "bonus." Quickstart now presents **Method A: Generate from the
  spec (primary)** before **Method B: Install the package (bonus)**, with
  Redis Stack startup and the ephemerality note shared by both. No code or
  behavior changes.

## [1.9.0] — 2026-05-31

### Added
- **Configurable embedding model** (`config.py`, `processor.py`, `database.py`,
  `server.py`): the embedding model is no longer hardcoded. Two new config
  fields — `embedding_model` (default `intfloat/multilingual-e5-base`) and
  `embedding_dim` (default `768`) — let each deployment pick its own quality /
  resource / language tradeoff (e.g. `multilingual-e5-large` at 1024 dims, or a
  domain-specific multilingual model) without editing code. The product stays
  language-neutral; the choice is the operator's. The RediSearch vector index
  is now built with `embedding_dim`, so it follows the chosen model. With the
  defaults unchanged, behavior is identical to 1.8.0.
  - `processor.set_model_name()` injects the configured model name before the
    first `get_model()` call. A model swap requires a process restart and an
    index flush (a different dimension is incompatible with existing vectors).

### Fixed
- **redis-py 8.x FT.SEARCH dict response** (`database.py`): redis-py 8.0
  registers a response callback that converts the raw RESP2 list into a dict,
  which broke every search path with `KeyError: 0` at `res[0]`. Added
  `_unwrap_search_response()` to normalize list / dict / object responses back
  to the canonical RESP2 flat-list form, applied to all FT.SEARCH calls.
  `redis` is also pinned to `>=5.0.0,<8.0.0` until full 8.x support is verified.

## [1.8.0] — 2026-05-31

### Added
- **Sibling-chunk TTL refresh** (`database.py`): `doc:<uuid>` parent hash now
  stores a `chunk_ids` field (space-joined list of all child chunk IDs).
  `search_memory` uses this to refresh **every sibling chunk** in a single
  pipeline when the parent is hit — not just the winning chunk — keeping the
  whole document's chunk set alive together throughout the TTL window.
  `access_count` is still incremented only on the actually-matched chunk.
  Older parent docs without the field fall back gracefully to single-chunk
  + parent refresh.
- **Six standalone eval scripts** under `tests/eval_*.py`: offline HF model
  check, latency measurement, Redis-down startup, Unicode round-trip,
  uvx-missing startup, and verbatim recall verification.

### Changed
- **`min_score` applied after lexical rerank** (`database.py`): The score
  floor is now enforced after the phrase/coverage rerank pass. A candidate
  just below the threshold at fusion time can now be rescued by a rerank
  boost. Previously the cut ran at fusion time, discarding records before
  rerank could lift them.
- **Spec compliance — BM25 formula restored** (`processor.py`):
  `keyword_relevance()` denominator reverted to `max(1.0, max_bm25)` (spec
  §3.6). The 1.7.0 change to `max(max_bm25, 1e-9)` was spec-incompatible.
- **Spec compliance — `repair_memory` simplified** (`database.py`): Reverted
  to a thin `ensure_index()` only call (spec §3.10). The three orphan-cleanup
  passes introduced in 1.7.0 were beyond the spec's intent.
- **Spec compliance — chunk SHA guards removed** (`database.py`): Per-chunk
  `mem:sha:` guards are no longer written (spec §3.8 "チャンク側は個別の sha
  ガードを付けず"). `delete_memory` cascade and `delete_by_session` updated
  accordingly.

### Fixed
- **`delete_memory` cascade key parsing** (`database.py`): `FT.SEARCH …
  RETURN 0` returns a flat key list `[count, key1, key2, …]` with no
  interleaved field arrays. The old loop used `i += 2`, collecting only every
  other chunk key and leaking orphans on the TAG-query success path. Changed
  to `res[1:]` iteration.
- **TTL refresh round-trips** (`database.py`): Replaced up to 15 individual
  Redis commands per `search_memory` call with a single
  `refresh_pipe.execute()`, matching the spec's "チャンクと親が同時に
  リフレッシュされる" intent.

## [1.7.0] — 2026-05-10

Score-improvement release: soft chunking, BM25 fix, per-chunk SHA dedup
guards, and a three-pass `repair_memory`. No MCP API surface changes.

### Added
- **Soft chunking** (`processor.py`): `chunk_text()` now prefers
  paragraph / line / sentence / word break boundaries over hard character
  cuts; no overlap is emitted after a soft break (clean semantic start).
  `_SOFT_BREAKS` covers ASCII and CJK terminators.
- **Per-chunk SHA guards** (`database.py`): `_save_parent_chunks()` runs
  MGET before each write and skips already-indexed chunks; the guard key
  is written atomically in the same pipeline. `delete_memory()` and
  `delete_by_session()` now cascade-delete `mem:sha:` guards alongside
  chunk keys.
- **`repair_memory()` three new scan passes**:
  - (A) orphaned `mem:sha:` guards (target `mem:` key gone)
  - (B) orphaned chunk keys (`doc:` parent gone)
  - (C) orphaned `docsha:` guards (`doc:` key gone)
  Returns counts per pass; index rebuild retained.
- New test suite `tests/test_cross_session.py` (cross-session isolation,
  112 tests total).

### Fixed
- **BM25 channel weight suppression**: denominator was `max(1.0, max_bm25)`,
  which clipped the BM25 contribution to zero when all raw scores were < 1.0.
  Changed to `max(max_bm25, 1e-9)` so the full 0.3 weight is applied.
- **`save_memory` unhandled exception on invalid `importance`**: bare
  `float()` replaced with `try/except`; invalid values now return
  `{"saved": false, "status": "error", "reason": "…"}` instead of a 500.

### Docs
- MCP-Lite spec synced with Retrieval Extensions v1 addendum
  (`recall_thread`, §4.3.1, `status:saved`, §5 rules 0/9/10).

## [1.6.2] — 2026-05-06

### Documentation
- README.md: footer に `mcp-name: io.github.NeuralNexusNote/n3mc-workingmemory`
  行を追加。MCP Registry (`registry.modelcontextprotocol.io`) の PyPI
  パッケージオーナーシップ検証で要求される識別行。視覚的な邪魔を最小化
  するため `<sub>` タグで footer に格納。

## [1.6.1] — 2026-05-06

### Documentation
- README (EN/JP): Quickstart Step 2 に Claude Code marketplace 経由の install
  手順（`/plugin marketplace add` + `/plugin install`）を最短経路として追記。
  既存の PyPI / フォーク / uvx 手順は手動オプションとして据え置き。
- README (EN/JP): Pro 関連の言及を中立化。`## Lite vs. Pro (coming soon)` を
  `## Use cases — when working memory is the right tool` /
  `## ユースケース — ワーキングメモリが適している場面` に書き換え、長期保存の
  案内を「any persistent memory backend / 任意の永続メモリバックエンド」へ統一。
  Lite が単体で完結したワーキングメモリレイヤであることを明確化する目的。

## [1.6.0] — 2026-04-28

> First public release-tagged minor bump on top of `V1.1.0` (the only
> previously published GitHub Release). Internal commits had been using
> `1.5.0` / `1.5.1` markers, but no tag was ever pushed; the cumulative
> changes since `V1.1.0` are large enough to justify a single `1.6.0`
> release rather than one tag per internal milestone.

### Added
- **Multilingual RAG primitives (CPU-only, no LLM/GPU)**:
  - Unicode NFKC normalization on `content_ngram`, query, dedup SHA, and
    embed input. Folds half/full-width katakana, full-width digits,
    compat ligatures.
  - Bigram BM25 side-channel coverage extended from CJK-only to also
    Hangul (Korean), Thai, Lao, Myanmar, Khmer.
  - Diacritic-folded duplicate of each Latin-alphabet word emitted into
    `content_ngram` so `café` ↔ `cafe` cross-match.
- **Encoding safety layer ported one-to-one from the Free build (§3.13)**:
  - `sys.stdin` / `sys.stdout` / `sys.stderr` reconfigured to UTF-8 at
    server import time (Windows-Japanese cp932 mojibake fix).
  - `sanitize_surrogates()` applied to every `save_memory.content` and
    `search_memory.query` before any `.encode("utf-8")` call. Lone
    UTF-16 surrogate halves no longer crash saves silently.
- New unit tests: `TestEncodingSafety` (10 tests) and
  `TestEncodingSafetyE2E` (3 tests). Suite total now 105 (was 92).
- New spec section `§3.13 Encoding Safety` (EN + JP).
- Production-grade `pyproject.toml` metadata: classifiers (Dev Status,
  Audience, License, Natural Language EN/JP/CN-Simp/CN-Trad/KO, Topics,
  Python 3.10–3.13), keywords, project URLs, authors/maintainers,
  explicit sdist file list.
- `CONTRIBUTING.md` (EN + JP).
- GitHub Actions CI: pytest matrix on Python 3.10–3.13 × Redis Stack
  service container, plus `python -m build` + `twine check` job.
- README sections for Multilingual support, Encoding safety,
  Troubleshooting (Windows file-lock during pip upgrade, leftover
  `~3memorycore-mcp-lite` warnings).

### Changed
- `cjk_bigram_expand` is now language-aware: NFKC-normalizes input,
  emits diacritic-folded Latin word duplicates, covers more scripts.
  Backward compatible — existing CJK behavior unchanged.
- `_sha1` (dedup digest) hashes NFKC-normalized text. Half/full-width
  perceptually-identical content now deduplicates correctly. Old
  pre-1.5.0 hashes age out within the 7-day TTL window.
- Lite `b_session` ranking reinstated (match=1.0 / mismatch=0.6) —
  matches the Pro contract and surfaces project-scoped memories above
  cross-project noise in shared Redis instances.

### Fixed
- Spec §4.1 declared "stdin/stdout/stderr reconfigured to UTF-8 at
  startup" but the implementation never called `reconfigure()`. Now it
  does. (Spec violation closed.)
- `save_memory` no longer crashes silently when content contains lone
  surrogate halves (Windows subprocess pipe edge case).

---

## Internal milestones between V1.1.0 and 1.6.0 (no GitHub Release)

> The following work was committed during the `V1.1.0` → `1.6.0` window
> but was never tagged on its own. Folded into `1.6.0` for the release.
> Listed here for traceability against the git log.

### `1.5.0` (commit `ab1e958`, 2026-04-26)
- `delete_memories_by_session` tool — bulk-delete every memory tied to
  a `session_id` (singles, parent docs, child chunks, sha guards).
  Lite-only; Pro will keep only per-record `delete_memory` for safety.
- `session_id` argument on `save_memory` and `search_memory` for
  ChatLink-style "one chat = one project" workflows.
- `b_session_match` / `b_session_mismatch` config fields.
- §10 Test 6 (bulk delete by session) in the Evidence Report.
- Hook-based full-transcript saving recipe in README.
- §11 explicitly documents the limit of MCP persuasion — what an MCP
  server can and cannot enforce on the LLM's behavior.
- Tool response text now ends with a short auto-save reminder
  (three variants: after-search, after-save, generic).

---

## [V1.1.0] and earlier

The only previously published GitHub Release. See the GitHub Releases
page and git history for details. Earlier versions did not maintain a
changelog file.
