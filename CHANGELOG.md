# Changelog

All notable changes to N3MemoryCore MCP Lite are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Changed
- The specification's "What changed in v2.0.0" table also lists three
  changes that v1.12.2 users see: a search no longer extends a TTL, the
  full-text index field is `content_fts` (the index is created again once),
  and Lite checks tool arguments itself instead of the MCP SDK.
- The specification asks the AI that implements it to write the code and the
  tests itself in the main session, and to use subagents only for read-only
  work.

### Fixed
- The MCP entry answers `initialize` within 2 seconds again: only the compiled
  modules of the direct save are imported before stdio starts; `redis` and
  `database.py` are imported at the direct save.
- Four errors in the specification (the regular expression of §12.9, the SCAN
  description of §3.12, the test list of §2, the import order of §4.6).
- The specification states the success answer of `delete_memory`
  (`{"status": "deleted", "id", "chunks_deleted"}` for a parent document,
  `{"status": "deleted", "id"}` otherwise, as in 1.x), and no longer says that
  any body up to 19 characters over `chunk_threshold` gives one chunk: that
  holds only when the rest after the cut at a soft break is shorter than 20
  characters.
- The code, the plugin and the tests are generated again from the
  specification: the plugin `n3mc-workingmemory` registers the hooks only (no
  MCP server in Claude Code), and the plugin `n3mc-workingmemory-mcp` is
  removed from the marketplace.
- Lowering only `chunk_threshold` to 100 or less no longer reports a
  `config_type` notice about `chunk_overlap` on every start; the missing
  `chunk_overlap` is written as the value in effect.
- `n3mc-lite --stop-server` reports the stop when the stop request failed but
  the server went down anyway (exit 0), as the specification says.
- An MCP `save_memory` whose `agent_name` is blank no longer stores the spaces;
  the name is empty unless `N3MC_AGENT_NAME` or the client name gives one.
- An unexpected error in a tool is answered `Error: <message>` again, as in
  1.x, without the exception type name.
- The specification's test list restores or strengthens the tests of
  `delete_memories_by_session`, `owner_id` mismatch, the final-score product,
  the `--stop-server` failure paths, the exact texts of the MCP entry when the
  server is not available, the stdout of the MCP entry, and the
  unreadable-date messages (`1969-12-31` is checked on Windows only).

## [2.0.2] — 2026-11-25

Documentation only; the package code is the same as 2.0.1.

### Changed
- **Generation from the specification is the primary way to install.** The
  README starts with the setup steps: make an empty folder, put the
  specification in it, open the folder in Claude Code (desktop app: **Code**
  tab → **+ New** → **Local** → **Open folder…**; or `claude` in a terminal) and
  ask it to implement n3mcmcp-lite. `pip` and the plugin remain as the second way.
- **Desktop app registration is automatic in that setup.** The AI backs up
  `claude_desktop_config.json`, replaces only the `n3mc-workingmemory` entry and
  adds `mcp__n3mc-workingmemory` to `permissions.deny` in
  `~/.claude/settings.json`, so the desktop app's Code tab keeps using the hooks.
- **Restart guidance.** The final setup message starts with a restart heading
  and explains how to quit Claude completely (the taskbar icon, or sign out of
  Windows); after the restart you open the same session and send "I restarted".
- **README is shorter.** The disclaimer (no support, use at your own risk, back
  up first) is right after the setup steps. Detailed sections (configuration,
  ranking formula, multilingual handling, encoding, development, extending, and
  what changed in 2.0.0) moved out of the README; they are in the specification
  and in this changelog.
- The specification no longer carries development history.
- The plugin and the marketplace entry are version 2.0.2, and the plugin's
  `uvx` commands and pre-approved rules are pinned to
  `n3memorycore-mcp-lite==2.0.2`.

## [2.0.1] — 2026-11-22

### Fixed
- **The model size in the notices is ~1.1 GB** (was ~440 MB): the "still
  starting" reason of the MCP entry and the "still loading" / "could not be
  loaded" notices name the size of `intfloat/multilingual-e5-base` that the
  first start downloads (`model.safetensors` is 1,112 MB). Display change only;
  the specification (EN / JP) says the same.
- **Tests**: `TestEnforceEphemeral` leaves no key in another DB of the test
  Redis (the key is set with `EX 60` and removed in a `finally` block; the
  empty-DB test removes it first). A local test Redis that had written a
  snapshot while the key existed failed the first run after a restart.

### Changed
- The plugin and the marketplace entry are version 2.0.1, and the plugin's
  `uvx` commands and pre-approved rules are pinned to
  `n3memorycore-mcp-lite==2.0.1`.
- `MODIFICATIONS.md` no longer says that 2.0.0 is not released yet.

## [2.0.0] — 2026-11-22

Specification v2.0.0 (EN / JP, 2026-11-19). A MAJOR version: the way Lite
connects to Claude Code changed. MCP-Lite is now "N3MC-Free + MCP, with Redis
as the database": the search and save rules of v1.12.2, plus Free's hooks,
resident server and CLI, plus a thin MCP entry for applications without
hooks. The code in this repository was regenerated from the specification
(2026-11-20) and passes its tests.

### Changed
- **A search no longer extends a memory's life (decided 2026-11-22).** Every
  entry is deleted 7 days after it was saved, as the instructions and the
  rules file tell the user. Until now a search hit reset the TTL of the top
  results (with their parent documents, chunks and duplicate guards), so a
  record that kept coming up lived longer than that. `ttl_refresh_on_search`
  is removed (a value left in `config.json` is ignored); `ttl_refresh_top_k`
  now only sets how many top hits have their `access_count` increased.
- **Claude Code uses the hooks only.** The plugin registers the
  `UserPromptSubmit` and `Stop` hooks (and `SessionStart` for permissions)
  and no longer registers the MCP server in Claude Code; `plugin.json` has no
  `mcpServers` key. pip users run `n3mc-lite --init` once. Claude Code no
  longer shows the seven MCP tools. Do not register the MCP server in Claude
  Code; `--init` reports an old registration in `~/.claude.json` or
  `.mcp.json` but does not edit those files.
- **Every non-empty message and reply is saved in Claude Code** by the hooks,
  as `[user]` / `[claude]` records (chunks `[user i/N]`), with Lite's save
  rules (code blocks as `[code omitted]`, chunking, parent document, TTL).
  Hook saves reject exact duplicates only within the same exchange
  (`turn_id`); the MCP `save_memory` keeps Lite's rule across all records.
- **The `UserPromptSubmit` hook searches with every message** and puts the
  result into Claude's context under `# Recalled Memory Context`; it also
  adds missing vectors and saves Claude's previous reply from the transcript
  when the Stop hook missed it.
- **One resident server** (`127.0.0.1:18524`, `python -m n3mc_mcp.cli
  --run-server`) loads the embedding model once for the hooks, the CLI and
  every MCP client, and keeps running in the background. It is replaced by
  itself when the code or `config.json` changes (`code_version`).
- **The MCP entry `n3mc-workingmemory` is a thin client** for the Claude
  desktop app and other applications without hooks: it answers `initialize`
  and `tools/list` itself, loads no model and forwards every tool call to the
  resident server. When the server is not available, `save_memory` saves
  directly to Redis without a vector.
- The MCP instructions tell a client that already has the Lite hooks' block
  (the line `Source: N3MC Lite (7-day working memory)`, which the hook's
  `# Recalled Memory Context` block carries) not to save every turn. The
  blocks of the N3MC Free and Pro hooks do not count.
- **Display change**: the Redis command in the hints and the README is
  `docker run … -p 127.0.0.1:6379:6379 …` (was `-p 6379:6379`, all
  interfaces). An existing container keeps its own setting.
- **Display change**: rule 0 of the MCP instructions and the search
  reminder name `n3mc-pro`, `n3mc-pg` and `n3mc-longtermmemory` as the
  persistent N3MC memory (was `n3mc-longtermmemory` only).
- Setup (Method A) shows the Claude desktop app entry and no longer edits
  `claude_desktop_config.json`.
- The hooks never wait for a server start: without a running server they
  save directly and search later. Hook entries carry `"timeout": 60`.
- The hooks never wait for the embedding model either: while it loads (the
  first start downloads ~440 MB), the hook's search uses keywords only with a
  notice and its saves go in without vectors, which repair adds later. The
  MCP tools and the CLI still wait up to 20 seconds.
- Changing `server_port` while the server runs now restarts it: the old
  server is stopped on the port it still listens on.
- A server that cannot start (for example a port held by another program)
  records the failure itself, so the hooks show the `server_start_failed`
  notice and wait 5 minutes before trying again, instead of starting it on
  every message.
- A save writes its duplicate guard and its record(s) in one atomic step, so
  a save cut off midway no longer leaves a guard that answers `duplicate` for
  7 days while nothing was stored.
- The MCP entry reads `config.json` again on every tool call, so an open
  client follows a changed `server_port`.
- Hook records carry Claude Code's session id; `N3MC_SESSION_ID` applies to
  MCP only. `N3MC_REDIS_URL` set for one application only gives the notice
  `redis_url_mismatch`; put `redis_url` into `config.json`.
- The resident server refuses requests with an `Origin` header (from
  N3MC Free v1.9.1); the hooks, the CLI and the MCP entry never send it.
- New CLI command `n3mc-lite --stop-server` stops this data folder's
  resident server (never another folder's).
- On Windows, a hook path that contains a space is written with its 8.3
  short name, so the hook command does not start with a quote.
- The Claude desktop app is registered with the absolute path of the command
  and `env.N3MC_AGENT_NAME`; plugin users use
  `uvx --from n3memorycore-mcp-lite==2.0.0 n3mc-workingmemory`. The plugin's
  `uvx` commands are pinned to the package version.
- Auto-approve in Claude Code covers the CLI commands (`--search`,
  `--buffer`, `--list`, `--recall-thread`, `--repair`, `--stop`) instead of
  the MCP tools. `--delete` and `--delete-session` are never pre-approved.
- Classifiers: Windows and Linux (macOS is not a target).

### Added
- CLI `n3mc-lite`: `--search`, `--buffer`, `--list`, `--recall-thread`,
  `--delete`, `--delete-session`, `--repair` (the tools' handlers and texts),
  and `--init`, `--hook-submit`, `--save-claude-turn`, `--stop`,
  `--run-server`, `--uvx`.
- Console scripts `n3mc-lite`, `n3mc-lite-hook`, `n3mc-lite-stop-hook`.
- Rules file `.claude/rules/n3mc-lite-behavior.md`, kept up to date by the
  Stop hook (a file edited by hand is not replaced).
- Config keys `server_port` (18524), `bind_host` (`127.0.0.1`) and
  `context_max_chars` (9500): the hook's block and the MCP `search_memory`
  result are kept within this size.
- Data-folder files: `audit.log` (the raw input of every hook call, no TTL,
  rotated at 50 MB), `n3mc-lite.pid`, `server.log`, `server_start_failed`,
  `notices.json`, `turn_id_<session>.txt`.
- Dependencies `fastapi`, `uvicorn`, `pydantic`, `psutil`; dev `httpx`.
- Coexistence with N3MC-Free, N3MC-Pro and the other editions on one PC (own
  port, folder, hook commands, CLI and rules file).
- `MODIFICATIONS.md`; `NOTICE` names the portions derived from N3MC-Free.

### Unchanged
- The PyPI name, the package `n3mc_mcp`, the MCP command and server name
  `n3mc-workingmemory` (alias `n3mc-mcp-lite`), the seven tools with the same
  names, arguments and output texts (the `search_memory` text is now kept
  within `context_max_chars`), the Redis layout and settings, the
  7-day TTL, the data folder `~/.n3mc-workingmemory`, the plugin name, and
  every search and save rule. Records saved by v1.12.2 stay readable.

### Docs
- README (EN / JP), PHILOSOPHY, NOTICE and this file describe v2.0.0;
  `MODIFICATIONS.md` is new.

## [1.12.2] — 2026-11-18

### Fixed
- **A failed save no longer blocks the same content for 7 days** (§3.8 (A) /
  (B) step 4, §3.13 (2); final review of 2026-11-18, L-01): any failure after
  the `mem:sha:` / `docsha:` guard is taken — also while the record is being
  built, not only a failed pipeline — releases the guard, and `agent_name`,
  `session_id` and `turn_id` have lone surrogates removed like `content`.
  Until then a lone surrogate in those fields, or a non-string `turn_id` from
  an older MCP SDK, left the guard behind and the content was answered
  `duplicate` although nothing was stored. Normal saves and output are
  unchanged.
- **The plugin's SessionStart hook writes ASCII-only JSON** (§8,
  `ensure_ascii=True`; L-03): its stdout is cp932 on a Japanese Windows, and
  the `uvx` hint contains Japanese. The text after JSON decoding is the same.

### Docs
- The specification (EN / JP, §3.8 and §3.12) and the README say that one
  Redis is for one installation (one `owner_id`): if several PCs or people
  share a Redis, or `owner_id` is re-created while old records remain, a body
  one owner saved is `duplicate` for the other until it expires, and one
  owner's records can fill the search candidates so the other's are not
  found (second review of 2026-11-17, L01 / L02). No change in behavior.
- Spec (EN / JP): §3.3 / §3.4 describe the guard as it works (taken with
  `SET NX` before the record's pipeline, released on failure) instead of "one
  pipeline, so a guard without a record cannot exist"; §3.5 lists `turn_id`
  in `mem:`, `doc:` and the index; §5 rule 7 no longer calls the Pro build
  "coming soon"; the §6 note on several accounts no longer says they can share
  a Redis through the same `redis_url` (L-02, L-04). README (EN / JP): the
  "Team & organization support" line now says that memories are not shared
  between people.
- Spec (EN / JP) and PHILOSOPHY: the Pro build is no longer called "coming
  soon" / 「公開予定」 anywhere (introduction, Backup note, Lite / Pro table,
  §1 and the rest); it is described as the separate edition that exists. No
  change in behavior.

### Chore
- `tests/eval_uvx_missing.py` finds the folders to remove from `PATH` (the
  ones that hold `uvx`) instead of naming a developer's folders;
  `.claude/settings.json` (local permissions) is no longer tracked and is
  ignored; `.claude-plugin/marketplace.json` `metadata.version` is the package
  version (1.12.1, was 1.6.2) (L-05).

### Tests
- New: `TestGuardReleasedOnAnyFailure`, `TestPluginHookAsciiStdout`,
  `TestPublicRepoHygiene` (final review of 2026-11-18). Each fails on 1.12.1
  except the check that plain fields are stored unchanged.
- `test_foreign_key_leaves_config_and_notices` expects the host:port of the
  Redis under test (`N3MC_REDIS_TEST_URL`) instead of a fixed `:6390`, so it
  passes against a dedicated Redis on any port (second review of 2026-11-17,
  T01). No change in the package. Tests: 329 passed (Windows 11, Python 3.12,
  Redis Stack in a throw-away container on port 56390).

## [1.12.1] — 2026-11-17

Spec v1.9.0 → v1.9.1 (JP/EN). The instructions the server sends to the model
now match how code blocks are saved: they said "the default is `false` (code
is saved)" and listed a pasted code dump among the long bodies to save
verbatim. They now say that content is saved with each code block replaced
by `[code omitted]` and that the code itself is never kept or returned.
Saving and display are unchanged.

## [1.12.0] — 2026-11-17

Spec v1.8.0 → v1.9.0 (JP/EN): two findings of the code review of
2026-11-17, and code blocks replaced on save. The specification was changed
first, then the code and tests.

### Changed
- **Code blocks are replaced with `[code omitted]` when saved** (§3.13).
  Since 2026-04 Lite stored them but always showed them as `[code omitted]`,
  so no tool could return the code. What you see does not change. A word that
  appears only inside a code block no longer finds the record, and two bodies
  that differ only inside their code blocks are exact duplicates. Records
  saved by 1.11.x keep their code in Redis until their 7-day TTL ends, and are
  still shown as `[code omitted]`. `skip_code_blocks: true` still rejects the
  whole body.

### Fixed
- **Persistence was turned off although another database held data** (§3.4).
  `enforce_ephemeral()` looked only at the connected database with `SCAN`, but
  `CONFIG SET appendonly no` / `save ""` change the whole Redis server. A Lite
  pointed at DB 0 of a Redis whose DB 1 held another program's data turned
  persistence off for that data too, which was then lost at the next Redis
  restart. `INFO keyspace` is now checked first: keys in another database, or
  a refused `INFO`, leave the configuration unchanged with the `shared_redis`
  notice.

### Changed (specification text only)
- **§3.13 now states the outer `strip()`** that `save_memory` has always
  applied: leading and trailing whitespace is removed once, so `' \nAAA\n '`
  is saved as `AAA`. The text said only lone surrogates were removed. The
  behavior is unchanged, to keep existing installations working as before.

## [1.11.0] — 2026-11-17

Spec v1.7.3 → v1.8.0 (JP/EN). The access-frequency boost is now off by
default and hiragana-only query tokens are dropped, so search results can
rank differently than in 1.10.2; that is why this is a minor release.

### Changed
- **Hiragana-only query tokens are dropped** (§3.7, JP/EN spec, 2026-11-17):
  bigrams made only of hiragana (`して`, `ます`, `どう`) are dropped from
  the BM25 query when another token remains, like single CJK characters.
  They appear in most Japanese records, so under OR an unrelated question
  matched through them (`来週の株価はどうなる` matched
  `今日の天気はどうですか` through `はど` and `どう`). Measured in
  N3MC-Pro's bigram index (2026-11-16).
- **The access-frequency boost is off by default** (§3.6, §6, JP/EN spec,
  README, 2026-11-17): `access_count_enabled` now defaults to `false`.
  Measured in N3MC-Pro with every search counted as here, turning the
  boost back on lowered recall@3 by 0.051 (20 questions worse, 8 better).
  `access_count` is still incremented with the TTL refresh. An existing
  `config.json` keeps the value it has.
- **The embedding model is built from the local Hugging Face cache first**
  (§3.9 step 4, JP/EN spec): `local_files_only=True` with the Hub marked
  offline, falling back to the normal (downloading) construction only when
  that fails. Found by the §10-1 Evidence Report: the first `search_memory`
  after `initialize` waited for a construction that asked the Hub about
  every cached file, and took 5.7–5.9 s against the 3.0 s target; it now
  takes 1.8–2.0 s (Windows 11, 2026-11-16). No network call at start when
  the model is cached.

### Fixed
- **The end of a long body is searchable** (§3.11, JP/EN spec): when the
  last window of a chunked body was shorter than 20 characters, the code
  dropped it, so after a soft break the last few characters (for example
  `PIN: 4471-ZX.` on its own last line) were in no chunk and neither BM25
  nor the vector search could find the document by them. The tail is now
  appended to the previous chunk (which may be up to 19 characters longer
  than `chunk_threshold`); every character of the body is in a chunk. The
  parent document always held the full body. Documents saved before this
  change keep their chunks until they expire.
- **`save_memory` rejects a `NaN` importance** (§4.3): `"nan"` passed the
  0.5–2.0 clamp and was stored as `2.0`, the highest importance. It now
  returns the same error as a non-numeric value and nothing is saved.
- **`list_memories` treats a `limit` below 1 as 1** (§4.3): a negative value
  cut rows from the end of the list instead.

## [1.10.2] — 2026-11-16

Spec v1.7.2 → v1.7.3 (JP/EN): writes to an existing record are checked and
made in one atomic step (findings DF1 / DF2 of a data-flow trace), and four
gaps between §4.3 and the code found by a clause-by-clause comparison are
closed; code regenerated from the EN spec. No change to the tool schemas or
`config.json`.

### Changed
- **Every error response ends with the auto-save reminder** (§4.3): the
  JSON for a non-numeric `importance`, the `Error:` text for an unreadable
  `since` / `until`, an unexpected exception, an unknown tool and the
  backend error of `search_memory` / `list_memories` / `recall_thread`.
  The error text stays first. A caller that compared the whole `Error:`
  text must now compare the part before `\n\n---\n`.
- **`delete_memory` accepts `doc:<uuid>` and `mem:<uuid>`** besides the
  bare UUID; one such prefix is removed. The response's `id` is the bare
  UUID.

### Fixed
- **`recall_thread` lists orphan chunks**: a chunk whose parent document is
  gone is listed with its own text (as `search_memory` does, §3.11). It
  skipped every chunk, so a turn whose parent had expired came back
  `not_found`. A chunk whose parent exists is still skipped.
- **Spec only — `protocolVersion`** (§4.2): the spec said the server
  advertises `2024-11-05`; the MCP SDK negotiates it (the client's version
  when supported). The spec now says so; the code was already right.
- **No key without a TTL after a delete during a search** (DF1): the TTL
  refresh after a search sent `EXPIRE` in one pipeline and the
  `access_count` increment and the guard reset in a second one. A record
  deleted by `delete_memory` (for example from another server process on
  the same Redis) between the two came back as a hash holding only
  `access_count`, with no TTL, and its dedup guard was written again — the
  same content was then rejected as `duplicate` for 7 days although no
  record existed. Both writes are now Lua scripts that write only while
  the record exists (`INCR_IF_EXISTS`, `SET_GUARD_IF_EXISTS`, spec §3.3).
- **No key without a TTL from `repair_memory`** (DF2): a record that
  expired between the existence check and the write of its new vector came
  back holding only `embedding`, with no TTL. The check and the write are
  now one script (`HSET_IF_EXISTS`); a vanished record is skipped and not
  counted in `reembedded`.

Such a hash has no `owner_id`, so no search showed it and no delete removed
it; it stayed in Redis until Redis was restarted. Both gaps were
milliseconds wide.

## [1.10.1] — 2026-11-15

Spec v1.7.1 → v1.7.2 (JP/EN): the weak-match fill from scoring v1.7.1
(decision LA1), applied through the specification; code regenerated from
the EN spec. This version adds a `config.json` key (`min_results`) and a
new heading in the `search_memory` output, so search results change.

### Added
- **Weak matches** (`min_results`, default 3): `search_memory` returned at
  most one row for most questions (the relevance floors cut the rest).
  When fewer rows pass the floors, the closest rows they dropped now fill
  the shortfall under `#### Possibly related (weak matches — may be
  unrelated)`, one row per parent document. Weak rows never get their TTL
  reset or `access_count` incremented, so being shown does not keep them
  alive. `0` turns the fill off; a blank query never fills. The server
  instructions (rule 12) and the `search_memory` description tell the LLM
  to use weak rows only when they are relevant.
- Measured with the real model (50 records, 26 questions, searched from
  another session): recall@3 22/26 → 25/26, about 2.1 weak rows per
  question; unrelated questions now always get 3 rows.

## [1.10.0] — 2026-11-14

Spec v1.6.0 → v1.7.0 (JP/EN). Fixes found while running the Pro edition
(2026-11-12 memo), applied to Lite through the specification; code
regenerated from the EN spec. Then spec v1.7.0 → v1.7.1: fixes from the
2026-11-13 code review of this release (decisions LD1–LD5), still
unreleased, so the package version stays 1.10.0.

### Fixed (spec v1.7.1, code review)
- **Older records disappeared from search**: `min_score` and the relative
  cut compared the final score, which includes `time_decay` (3-day
  half-life) and `b_session` (0.6). A record about two days older than the
  top hit, and every record saved before the MCP server restarted, was cut.
  Both floors now use the score without those two factors; the order is
  unchanged.
- **A paused Redis froze the server**: only a connect timeout was set, so a
  Redis that accepted the connection but never answered blocked every tool
  call. Reads and writes now time out after 5 s and are handled like a lost
  connection.
- **Two servers starting together got different `owner_id`s**, and the
  memories saved by one of them were invisible afterwards. `config.json` is
  created exclusively and every write is atomic.
- **A long save that failed half-way left an unsearchable document** that
  blocked re-saving for 7 days. All chunk embeddings are computed first and
  parent, guard and chunks are written in one pipeline.
- **Numeric config values were not range-checked**: `chunk_overlap >=
  chunk_threshold` looped forever, `ttl_seconds: 604800.5` broke every save,
  `embedding_dim: 768.0` stopped the server. Out-of-range values are now
  ignored with a `config_type` notice, and chunking always advances.
- **A Redis without RediSearch ended the process at start**. The server now
  stays up and every tool explains that Redis Stack is required.
- **Persistence was turned off on any Redis it connected to**. Only an empty
  or N3MC-only Redis is changed now; otherwise a `shared_redis` notice.
- **Tool calls waited for the first-run model download**. They wait at most
  20 s, then continue keyword-only with a notice.
- **`list_memories` missed standalone memories after a long document**,
  **dedup guards expired while their record lived on**, **sentence-like
  queries had no keyword hits** (terms are ORed now), **unreadable
  `since` / `until` were ignored** (they are errors now; offset-less values
  are local time).
- **Smaller fixes**: `repair_memory` adds missing vectors; `delete_memory`
  checks the owner; no ghost keys from `access_count`; Hangul fillers count
  as invisible; tests refuse to flush a DB with data and no test marker;
  plugin hook writes `settings.json` atomically and its fallback text also
  names a failed script; `index_dim_mismatch` notice; a warning on saves
  while `config.json` is unreadable; atomic dedup guard (`SET NX`); a notice
  added later in the process (e.g. `shared_redis` after a reconnect) is shown
  on the next search instead of never.

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
- **Blank memories from invisible-only input** (spec §3.13, found by the A10
  encoding test): `strip()` keeps `U+0000`, `U+200B` and `U+FEFF`, so a
  payload such as `\x00` or a lone zero-width space was saved as a memory
  that shows as nothing. `save_memory` now rejects content with no visible
  character (`empty content (no visible characters)`); anything with a
  visible character is still stored verbatim, NUL included.
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
