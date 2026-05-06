# Changelog

All notable changes to N3MemoryCore MCP Lite are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

_Nothing yet._

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
