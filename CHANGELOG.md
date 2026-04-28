# Changelog

All notable changes to N3MemoryCore MCP Lite are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

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

## [1.5.0] — 2026-04-26

### Added
- `delete_memories_by_session` tool — bulk-delete every memory tied to
  a `session_id` (singles, parent docs, child chunks, sha guards).
  Lite-only; Pro will keep only per-record `delete_memory` for safety.
- `session_id` argument on `save_memory` and `search_memory` for
  ChatLink-style "one chat = one project" workflows.
- `b_session_match` / `b_session_mismatch` config fields.
- §10 Test 6 (bulk delete by session) in the Evidence Report.
- Hook-based full-transcript saving recipe in README.

### Changed
- §11 explicitly documents the limit of MCP persuasion — what an MCP
  server can and cannot enforce on the LLM's behavior.
- Tool response text now ends with a short auto-save reminder
  (three variants: after-search, after-save, generic).

---

## [1.4.0] and earlier

See git history. Earlier versions did not maintain a changelog file.
