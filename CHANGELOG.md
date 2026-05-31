# Changelog

All notable changes to N3MemoryCore MCP Lite are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

_Nothing yet._

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
