# N3MemoryCore MCP — Lite (Ephemeral)

> **N3MC-MCP-Lite is an "external memory server" used by MCP-compatible
> editors such as Claude Code, Cursor, and Windsurf.**
> It runs as an MCP Server so AI can save and search conversation and
> code context across sessions.

> A NeuralNexusNote™ product — **free Lite** build: ephemeral hybrid
> (vector + BM25) memory exposed as a Model Context Protocol server, backed
> by Redis Stack with a 7-day TTL per entry.

> 💬 **The MCP protocol can only nudge the LLM to call `save_memory`, so
> which conversations actually get saved is ultimately up to the LLM. But
> if you ask Claude Code, it can also wire up hook-based auto-saving of
> every conversation.**
> Just say "after every turn, automatically save the full Claude Code
> transcript to Lite" and Claude Code will drop a script under
> `~/.claude/hooks/` and add a `Stop` hook to `~/.claude/settings.json`.
> The harness runs the hook deterministically — it does not depend on the
> LLM remembering to call `save_memory`, so Claude can never accidentally
> skip a save. See the
> [Hook-based full-transcript saving](#hook-based-full-transcript-saving)
> section below for details.

> 🇯🇵 **[日本語版はこちら](./README_JP.md)**
> 🛡️ **[Development Philosophy](./PHILOSOPHY.md)**

---

## 🚀 Quickstart — connect to Claude Code in 3 steps

> The fastest path from "nothing installed" to "Claude Code is using
> N3MC memory". Pick the install path that matches you (PyPI / fork /
> uvx), then add the server to your client config. Both Claude Code
> CLI and Claude Desktop are covered.

### Step 1 — Start Redis Stack

```bash
docker run -d --name redis-stack -p 6379:6379 redis/redis-stack-server:latest
# (Subsequent sessions: `docker start redis-stack`)
```

### Step 2 — Install the package (choose one)

**Quickest path — Claude Code marketplace.** Bundles install + MCP wiring in two
commands. Run inside Claude Code:

```
/plugin marketplace add NeuralNexusNote/n3mcmcp-lite
/plugin install n3mc-workingmemory@neuralnexusnote
```

Then `/reload-plugins` and skip Step 3 — the plugin manifest handles MCP wiring.
The manual options below remain available for forks, custom configs, and
Claude Desktop.

---

**(a) From PyPI** — most users:

```bash
pip install n3memorycore-mcp-lite
```

**(b) From a fork (you cloned this repo)** — contributors / customizers:

```bash
git clone https://github.com/<YOU>/n3mcmcp-lite
cd n3mcmcp-lite
pip install -e ".[dev]"
```

**(c) Zero-install via uvx** — no global install, isolated env:

```bash
# Just verify it runs; the actual launch is handled by your MCP client config:
uvx --from n3memorycore-mcp-lite n3mc-workingmemory --help
```

After step 2, the `n3mc-workingmemory` command is on your `PATH`. Run
`where n3mc-workingmemory` (Windows) or `which n3mc-workingmemory`
(macOS/Linux) to confirm.

### Step 3 — Wire it into your MCP client

| Client | What to do |
|---|---|
| **Claude Code (CLI), this repo's working tree** | `.mcp.json` is already committed — just `cd` into the repo and run `claude`. The CLI auto-connects on next prompt. |
| **Claude Code (CLI), a different project directory** | Copy [.mcp.json](./.mcp.json) into that project, or add the same `n3mc-workingmemory` block to its `.mcp.json`. See [Claude Code (standalone CLI)](#claude-code-standalone-cli). |
| **Claude Desktop** (incl. its built-in "Code" tab) | Edit `claude_desktop_config.json` (path differs per OS). See [Claude Desktop](#claude-desktop-and-the-code-tab-inside-claude-desktop). |
| **Claude Code with auto-tool-approval** | One extra block in `~/.claude/settings.json` so the AI never blocks on "Allow?" prompts. See [Auto-approve tool calls](#auto-approve-tool-calls-claude-code-only). |
| **uvx-launched** (no global install needed) | Use the uvx-form `command`/`args` in your client config. See [Claude Code (standalone CLI)](#claude-code-standalone-cli). |

That's it. Once Claude Code is connected, the server's behavioral
instructions take over — `search_memory` runs at the start of every
turn and `save_memory` runs after each meaningful exchange, all
automatically.

> First call may take 30–60 seconds the **first** time only — the
> ~400 MB `intfloat/multilingual-e5-base` embedding model downloads to
> `~/.cache/huggingface/`. Subsequent starts complete in seconds.

---

## ⚠️ Prerequisites (required before install)

This server does **not** run out of the box — you must prepare two things first:

1. **Redis Stack on `localhost:6379`** — the Lite build stores memory in Redis + RediSearch. The easiest way is Docker:
   ```bash
   # First time only (creates the container):
   docker run -d --name redis-stack -p 6379:6379 redis/redis-stack-server:latest

   # Every subsequent session (container already exists):
   docker start redis-stack
   ```
   Re-running the `docker run` command after the container exists fails with `Conflict. The container name "/redis-stack" is already in use`. Use `docker start` from the second session onward.

   > **Why no persistence flags on the docker line**: this build is
   > *deliberately volatile*. Ephemerality is a design feature, not a
   > missing capability — see the "Use cases" section below. Rather
   > than rely on fragile shell-quoting for `--save ""` (which breaks on
   > Windows PowerShell and cmd.exe), the MCP server **enforces** the
   > ephemeral state at startup by issuing `CONFIG SET appendonly no` and
   > `CONFIG SET save ""` on every connect. If you manually re-enable
   > persistence between sessions, it is reverted on the next Lite run.
   > The plain `docker run` above is sufficient — the server is the
   > source of truth for the ephemerality guarantee.
2. **[`uv`](https://docs.astral.sh/uv/) on your `PATH`** — required only for the Claude Code plugin / `uvx` install path. Not needed if you install from source.

The server refuses to start if Redis is unreachable, and the Claude Code plugin will fail to launch without `uv`. Install both before running `/plugin install` or any client-side config.

---

## Features

- 💾 **Fully local** — Your conversations stay in your own Redis instance. Nothing sent to the cloud.
- 🔍 **Semantic search** — Finds relevant past conversations even when the exact words differ.
- 🌐 **Multilingual out of the box** — CPU-only, no LLM/GPU required. NFKC fold (`ｱﾙﾌｧ`↔`アルファ`, `１２３`↔`123`, ligatures), bigram coverage for Japanese / Chinese / Korean / Thai / Lao / Myanmar / Khmer, diacritic cross-match for Latin scripts (`café`↔`cafe`).
- 🛡️ **Encoding safety** — stdio UTF-8 reconfigure on Windows (cp932 → UTF-8), lone-surrogate sanitization on every input. Same defenses as the Free build.
- 🔄 **Context across sessions** — Working memory that lasts **7 days** (auto-expires via Redis TTL; pair with any persistent memory backend if you need longer retention).
- ⚡ **Works automatically** — Saving and searching happen automatically. The MCP `initialize` response ships behavioral instructions, so no user action is required.
- 🤖 **Multi-agent ready** — Multiple AI agents share one Redis. The `b_local` and `b_session` biases prioritize each project's own memories while still surfacing the team's collective knowledge.
- 🏢 **Team & organization support** — Deploy Redis on a shared server and point `N3MC_REDIS_URL` to it for team-wide memory sharing (⚠️ authentication must be handled at the Redis layer).
- 🧹 **Ephemerality is a design feature** — 7-day auto-expiry means failed attempts and abandoned designs don't bleed into the next task. `docker restart redis-stack` wipes everything instantly.
- 💰 **Reduces token waste** — No more re-explaining past context. Memory search uses local embeddings (`intfloat/multilingual-e5-base`) and costs zero Claude tokens, and accurate context injection means fewer corrections and back-and-forth.

## How It Works

```
User's message
    │
    ▼
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  1. Auto-save │────▶│ 2. Semantic   │────▶│ 3. Context    │
│  Save last    │     │    search     │     │    injection   │
│  response to  │     │  Find related │     │  Feed to       │
│  Redis        │     │  memories     │     │  Claude        │
└──────────────┘     └──────────────┘     └──────────────┘
                                                 │
                                                 ▼
                                          Claude responds
                                          with full context
```

Everything runs automatically via the **behavioral instructions** shipped
in the MCP `initialize` response. No Claude Code hooks are involved — the
only client-side setup is adding the tools to `permissions.allow`. No user
action required.

### Relationship with Claude's built-in auto-memory

Claude Code has a built-in auto-memory system
(`~/.claude/projects/.../memory/`). N3MemoryCore **complements it rather
than competing with it**.

|                 | Claude auto-memory                                      | N3MemoryCore RAG                                     |
| --------------- | ------------------------------------------------------- | ---------------------------------------------------- |
| **Strengths**   | Reliable, loads every session, great for fixed facts    | Conversation context, detailed history               |
| **Weaknesses**  | Cannot capture conversation flow or context             | Depends on search quality; not guaranteed to surface |
| **Best for**    | User profile, folder paths, stable settings             | Conversation threads, past decisions, reasoning      |

**Recommended usage:**

- **Fixed information needed every session** (folder paths, user preferences) → save to auto-memory
- **Conversation context and history** (discussion threads, past decisions) → N3MemoryCore accumulates automatically (7-day window; pair with a persistent memory backend if you need longer retention)

---

## Use cases — when working memory is the right tool

The 7-day TTL and volatile Redis storage are **design features, not
limitations**. They make this server the right fit for:

- **Agentic code-generation loops** — failed attempts and abandoned
  designs don't bleed into the next task; `docker restart redis-stack`
  wipes the slate clean.
- **Multi-agent collaboration** — decisions made during one task don't
  contaminate unrelated follow-ups.
- **Experimental / throwaway prototyping** — leave it alone and memory
  evaporates in 7 days, no pruning needed.
- **Project-scoped working memory** — pin a `session_id` per task /
  project to keep contexts cleanly separated.

If you need **long-term, persistent knowledge accumulation across
months or years**, working memory is not the right layer. Pair this
server with any persistent memory MCP — the official knowledge-graph
server, your own SQLite-backed implementation, or an external
service — to cover the long-term side.

## What is this?

`n3memorycore-mcp-lite` is a local-only MCP server that gives Claude (and
any other MCP-compatible client) short-lived memory across conversations.
It stores text entries in a local Redis Stack instance with both a BM25
full-text index and a 768-dimension vector index
([`intfloat/multilingual-e5-base`](https://huggingface.co/intfloat/multilingual-e5-base)), and
returns hybrid-ranked results.

Every operation runs on the user's machine. No API calls, no cloud
storage.

## Tools exposed

| Tool                          | Purpose                                                                          |
| ----------------------------- | -------------------------------------------------------------------------------- |
| `search_memory`               | Hybrid (vector + BM25) search, ranked & time-decayed, `session_id` boost         |
| `save_memory`                 | Persist a short entry (7d TTL, dedup: exact + near-duplicate)                    |
| `list_memories`               | Most-recent entries, newest first                                                |
| `delete_memory`               | Remove a specific entry by id (cascades to chunks if id is a parent doc)         |
| `delete_memories_by_session`  | Bulk-delete every memory tied to a `session_id` — wraps up a finished project    |
| `repair_memory`               | Re-create the RediSearch index if missing                                        |

The server also ships **behavioral instructions** via MCP's `initialize`
response, asking the client to `search_memory` at the start of each turn
and `save_memory` after each meaningful exchange — so "auto-save" is
preserved without any Claude Code hooks.

## ID hierarchy

N3MemoryCore identifies the origin and context of every record with
five ID fields. Most users only ever touch `session_id` (and rarely
`agent_name`); the rest are filled in automatically.

| ID                    | Stored in                       | Generated                                  | Granularity                            | Purpose |
|-----------------------|---------------------------------|--------------------------------------------|----------------------------------------|---------|
| `id` (PK)             | Redis hash                      | Per record (UUIDv7, time-ordered)          | **One record**                         | Unique identifier for each memory — used for `delete_memory` and dedup. |
| `owner_id`            | `config.json`                   | First startup (UUIDv4)                     | **Owner / installation**               | Identifies whose data this is. Validated on every `save_memory`; mismatched payloads are rejected with `owner_id mismatch`. Stored as a TAG field; filtering happens in Python (see spec §3.12). |
| `local_id` (agent_id) | `config.json`                   | First startup (UUIDv4)                     | **Agent / install**                    | UUIDv4 identifier for this install. Stored on every row for forward-compatibility with future persistent variants, but **does NOT feed Lite's `b_local` ranking** — `b_local` is computed from `stored_importance + access_count` only (see Ranking formula). |
| `session_id`          | In-memory or supplied by client | Per task / project / conversation (string) | **Task / project / conversation**      | Surfaces memories from the same task / project together. Drives the **`b_session` ranking bias** (`b_session_match=1.0`, `b_session_mismatch=0.6`) so the current chat's memories outrank unrelated cross-project rows in the same Redis instance. Also the filter key for `delete_memories_by_session`. Resolution order: per-call argument → `N3MC_SESSION_ID` env var → per-process UUIDv4 fallback. |
| `agent_name`          | Redis hash                      | Per `save_memory` call (free-form string)  | **Agent display label**                | Human-readable label (e.g. `"claude-code"`, `"claude-desktop"`). Not used in ranking — display/audit only. |

```
owner_id  (one N3MC server / data owner)
  └── session_id  (one task / project / conversation)
        └── local_id  (the agent speaking inside that session)
              ├── agent_name  (its display name: "claude-code" etc.)
              └── id  (one memory record)
```

**Practical guidance:**

- **You should pin `session_id`** when working on a named project or
  task. Pass the same string (e.g. `"proj-alpha"`, `"task-refactor-auth"`)
  to both `save_memory` and `search_memory`. This both ranks-up the
  project's own memories and gives you a one-shot
  `delete_memories_by_session` for project teardown.
- **You can leave `agent_name` empty** for single-agent use. Set it
  (`"claude-code"`, `"cursor"`, …) when multiple agents share the same
  Redis so audit/list output stays readable.
- **You should not pass `owner_id`** unless you specifically need to
  prove ownership (the server validates it against `config.json` and
  rejects mismatches; an empty value means "use my own").

## Prerequisites

### 1. Start Redis Stack

The Lite build requires Redis Stack (Redis + RediSearch module). The
easiest way is Docker:

```bash
# First time only (creates the container):
docker run -d --name redis-stack -p 6379:6379 redis/redis-stack-server:latest

# Every subsequent session (container already exists):
docker start redis-stack
```

That's it — the container exposes Redis on `localhost:6379` and the
server will find it automatically. Re-running the `docker run` command
after the first install produces `Conflict. The container name
"/redis-stack" is already in use`; use `docker start redis-stack`
thereafter.

### 2. Install the package

**From PyPI (recommended):**

```bash
pip install n3memorycore-mcp-lite
```

Or zero-install via `uvx` (the Claude Code plugin uses this path):

```bash
uvx --from n3memorycore-mcp-lite n3mc-workingmemory
```

**From source** (if you want to edit the code):

```bash
git clone https://github.com/NeuralNexusNote/n3mcmcp-lite
cd n3mcmcp-lite
pip install -e .
```

The first run downloads the ~400 MB embedding model from Hugging Face
into the standard `~/.cache/huggingface/` directory.

> **First install requires internet access to three resources:**
> 1. **github.com** — when `/plugin marketplace add NeuralNexusNote/n3mcmcp-lite`
>    registers the plugin (skip this step if you install via `uvx` or from
>    source instead).
> 2. **pypi.org** — when `uvx --from n3memorycore-mcp-lite` (or `pip install`)
>    resolves the package.
> 3. **huggingface.co** — when the server first starts and downloads
>    `intfloat/multilingual-e5-base` (~400 MB) into `~/.cache/huggingface/`.
>
> All three fail with explicit, time-bounded errors when offline; none
> hang. Subsequent starts use only the local cache and require no
> internet.

## Configure a client

### Claude Desktop (and the "Code" tab inside Claude Desktop)

If you are using the **Claude Desktop application** — including its
built-in **Code** tab — configure MCP via the desktop config file, NOT
via `.mcp.json` (which is only read by the standalone `claude` CLI).

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "n3mc-workingmemory": {
      "command": "n3mc-workingmemory",
      "args": []
    }
  }
}
```

**Windows tip:** if Claude Desktop fails to spawn the server with the
bare command name above (the hammer/tool icon never appears), replace
`"command"` with the absolute path to the installed `.exe`, for example:

```json
"command": "C:\\Users\\<YOU>\\AppData\\Local\\Programs\\Python\\Python312\\Scripts\\n3mc-workingmemory.exe"
```

Run `where n3mc-workingmemory` in a terminal to find the exact path on
your machine.

**After editing the config, fully quit Claude Desktop** — closing the
window is not enough. Right-click the Claude icon in the system tray (or
use Task Manager) and terminate every Claude process, then relaunch.

### Claude Code (standalone CLI)

This section applies ONLY to the `claude` command-line tool, not to the
Claude Desktop "Code" tab (see above for that).

**`.mcp.json` is already included in this repository.** Clone the repo,
install the package, and the Claude Code CLI connects automatically — no
manual configuration needed.

For other projects, add the following to that project's `.mcp.json`:

```json
{
  "mcpServers": {
    "n3mc-workingmemory": {
      "type": "stdio",
      "command": "n3mc-workingmemory",
      "args": []
    }
  }
}
```

### Auto-approve tool calls (Claude Code only)

By default, Claude Code prompts the user for each MCP tool call. For a
fully automatic memory loop — so the connected AI never blocks on an
"Allow?" prompt — the `n3mc-workingmemory` tools must be listed under
`permissions.allow` in Claude Code settings.

**Plugin install auto-configures this** — when you install via
`/plugin install n3mc-workingmemory@neuralnexusnote`, a `SessionStart`
hook runs [`hooks/install_permissions.py`](plugins/n3mc-workingmemory/hooks/install_permissions.py)
which idempotently adds the six `mcp__n3mc-workingmemory__*` tools to
`~/.claude/settings.json`. No manual editing needed. The hook only
writes if at least one entry is missing and never touches unrelated
fields. The `hooks.json` command tries `python` → `py` (Windows Python
Launcher) → `python3` in a `||` fallback chain, so the hook works as
long as any one of these is on `PATH`. It only exits non-zero —
surfacing in Claude Code's `/plugins` Errors tab — when **all three**
are missing, avoiding silent failure.

The same hook also performs a **`uvx` pre-flight check** — the plugin
manifest launches the MCP server via
`uvx --from n3memorycore-mcp-lite n3mc-workingmemory`, so a missing
`uvx` would otherwise surface only as an opaque `ENOENT` in the MCP
launcher. The hook calls `shutil.which("uvx")` and, if not found,
writes a bilingual install hint to stderr (`pipx install uv`,
`curl -LsSf https://astral.sh/uv/install.sh | sh`, plus the docs URL)
so the user sees an actionable message in the `/plugins` Errors tab.
The hook still exits 0 because the permission install itself
succeeded.

**If you installed without the plugin** (e.g. `claude mcp add` or a
manual `.mcp.json`), or no Python interpreter is available at all, add
the block below manually to `~/.claude/settings.json` (user-global,
recommended) or `.claude/settings.json` (per-project):

```json
{
  "permissions": {
    "allow": [
      "mcp__n3mc-workingmemory__search_memory",
      "mcp__n3mc-workingmemory__save_memory",
      "mcp__n3mc-workingmemory__list_memories",
      "mcp__n3mc-workingmemory__delete_memory",
      "mcp__n3mc-workingmemory__delete_memories_by_session",
      "mcp__n3mc-workingmemory__repair_memory"
    ]
  }
}
```

Without this, every `save_memory` / `search_memory` call surfaces an
approval prompt and the AI blocks if the user is away. Claude Desktop
has no per-tool permission gate, so this step is not needed there.

## Data location

The Lite build does not store a database on disk — memories live in
Redis and expire automatically. Only a small `config.json` sits in the
platform-standard user data directory:

| OS      | Path                                                       |
| ------- | ---------------------------------------------------------- |
| Windows | `%LOCALAPPDATA%\n3mc-workingmemory\`                       |
| macOS   | `~/Library/Application Support/n3mc-workingmemory/`        |
| Linux   | `~/.local/share/n3mc-workingmemory/`                       |

Override with the `N3MC_DATA_DIR` environment variable.

## Configuration

On first run, `config.json` is auto-generated with random UUIDs for
`owner_id` and `local_id`. Editable defaults:

```json
{
  "owner_id":                 "<uuid>",
  "local_id":                 "<uuid>",
  "redis_url":                "redis://localhost:6379/0",
  "ttl_seconds":              604800,
  "dedup_threshold":          0.95,
  "half_life_days":           3,
  "bm25_min_threshold":       0.1,
  "search_result_limit":      20,
  "context_char_limit":       3000,
  "min_score":                0.2,
  "search_query_max_chars":   2000,
  "chunk_threshold":          400,
  "chunk_overlap":            100,
  "access_count_enabled":     true,
  "access_count_weight":      0.02,
  "access_count_max_boost":   0.5,
  "ttl_refresh_on_search":    true,
  "ttl_refresh_top_k":        5,
  "lexical_rerank_enabled":   true,
  "rerank_weight":            0.3,
  "rerank_phrase_weight":     0.2,
  "b_session_match":          1.0,
  "b_session_mismatch":       0.6,
  "skip_code_blocks":         false
}
```

- `redis_url` — connection URL; `N3MC_REDIS_URL` env var takes precedence.
- `ttl_seconds` — TTL on every new memory and sha-guard (default 7 d).
- `chunk_threshold` / `chunk_overlap` — sliding-window size and overlap (chars). Bodies longer than the threshold trigger the parent-document + chunks path for verbatim recall.
- `access_count_*` — access-frequency auto-importance; top-K search hits receive a capped boost on future queries.
- `ttl_refresh_on_search` / `ttl_refresh_top_k` — TTL reset for the top-K hits on each search (reset-only; no extension past a fresh save).
- `lexical_rerank_*` / `rerank_weight` / `rerank_phrase_weight` — lightweight post-fusion lexical reranker (CPU-only).
- `b_session_match` / `b_session_mismatch` — multiplicative ranking boost for rows whose stored `session_id` matches (default `1.0`) vs. rows from other projects (`0.6`). Pass the same `session_id` to `save_memory` and `search_memory` to surface a project's memories above unrelated cross-project rows in the same Redis instance. Set both to `1.0` to disable the bias.
- `skip_code_blocks` — when `true`, `save_memory` rejects any payload containing a triple-backtick fence (```` ``` ````) and returns `status: "skipped_code"`. Default `false`. Set to `true` if you want FastAPI-era N3MemoryCore-style code exclusion (keep code out of the memory index entirely — useful when your workflow already has git/IDE history for code and you only want prose decisions/plans in Redis).

See the spec §6 for the complete field-by-field reference.

## Multilingual support

Built-in, CPU-only, no LLM and no GPU required. Search and dedup behave
the same regardless of how the user types the same word:

| Layer | What it does | Real-world example |
|---|---|---|
| **NFKC normalization** | Folds compatibility forms before SHA / embedding / BM25 | `ｱﾙﾌｧ` ↔ `アルファ`, `１２３` ↔ `123`, `ﬁ` ↔ `fi` |
| **Bigram BM25 side channel** | Overlapping bigrams emitted for space-less scripts | `記憶装置` → `記憶 憶装 装置`; same for Korean (`안녕하세요`), Thai (`สวัสดี`), Lao, Myanmar, Khmer |
| **Diacritic fold** | Latin/Greek/Cyrillic words also indexed without combining marks | `café` matches `cafe`, `Ångström` matches `Angstrom` |
| **multilingual-e5-base embedding** | Multilingual semantic space across 100+ languages | Cross-language paraphrase retrieval |

These run automatically on every `save_memory` and `search_memory` call.
The raw `content` field is never rewritten — verbatim recall (spec §3.11)
still returns the original bytes byte-for-byte.

## Encoding safety

Two layers of defense run before any tool body executes (spec §3.13).
Same guards as the Free build, ported one-to-one:

1. **stdio UTF-8 reconfigure** — at module import, `sys.stdin` /
   `sys.stdout` / `sys.stderr` are switched to `encoding="utf-8"`. On
   Windows-Japanese hosts the default console code page is cp932, which
   would otherwise mangle every non-ASCII byte on the MCP JSON-RPC
   channel. POSIX systems are already UTF-8, so the call is a safe no-op.
2. **Lone-surrogate sanitization** — every `save_memory.content` and
   `search_memory.query` is passed through `sanitize_surrogates()` before
   any `.encode("utf-8")` call. Lone UTF-16 surrogate halves
   (`U+D800`–`U+DFFF`) appear when Windows subprocess pipes deliver UTF-8
   bytes that Python's decoder maps with `errors="surrogateescape"` —
   they round-trip through `json.loads` but raise `UnicodeEncodeError` at
   SHA1 / Redis HSET / embedding time. Without the guard the entire write
   is silently lost. The function is recursive so JSON payloads with
   surrogates buried inside are cleaned in one pass.

If a save payload consists entirely of surrogates, sanitization collapses
it to the empty string and the regular empty-content rejection path
applies — `{"status":"error","saved":false,"reason":"empty content"}`.

## Ranking formula

```
final_score = (0.7 * cosine_similarity + 0.3 * keyword_relevance) * time_decay * b_local * b_session

time_decay   = 2 ^ (-days_elapsed / half_life_days)       (default half-life: 3 days)
b_local      = clamp(0.5, 2.0, stored_importance + access_boost)
access_boost = min(0.5, access_count * 0.02)
b_session    = b_session_match (default 1.0)   if row.session_id == effective_session
             = b_session_mismatch (default 0.6) otherwise
```

With a default 3-day half-life (shorter than the 7-day TTL), `time_decay`
is meaningful in the Lite build: a fresh memory scores 1.0, a 3-day-old
one exactly 0.5, and a 7-day-old (near-expiry) entry ≈ 0.20 — pushing
recent context ahead in the ranking.

**Auto-importance (access-frequency boost)**: each time `search_memory`
returns a memory in its top 5 hits, that memory's `access_count` is
incremented by 1 and `b_local` rises by 0.02 on future queries (capped at
+0.5). No LLM judgement required — frequently-useful memories naturally
float to the top through CPU-only self-tuning.

## Development

```bash
# Start Redis Stack first (see Prerequisites), then:
pip install -e ".[dev]"
pytest tests/ -q
```

Tests target Redis DB index `0` (configurable via `N3MC_REDIS_TEST_URL`)
and `FLUSHDB` it before/after each test. RediSearch refuses to create
indexes outside DB 0 (`Cannot create index on db != 0`), so a separate
test DB isn't an option — run the test suite against a **dedicated**
Redis container, never one that holds data you care about. Tests refuse
to run if Redis isn't reachable.

## Extending the Lite build

If you want to modify behavior (change the ranking formula, drop in a cross-encoder reranker, plug in a Japanese morphological tokenizer, etc.), start from the design spec shipped in this repository:

- [`N3MemoryCore_MCP_Spec_EN.md`](https://github.com/NeuralNexusNote/n3mcmcp-lite/blob/main/N3MemoryCore_MCP_Spec_EN.md) — full design document (English)
- [`N3MemoryCore_MCP_Spec_JP.md`](https://github.com/NeuralNexusNote/n3mcmcp-lite/blob/main/N3MemoryCore_MCP_Spec_JP.md) — 日本語版

Appendix A of the spec lists optional extensions (cross-encoder reranker, save-time chunking, HyDE, Japanese morphological analysis) with drop-in points and library candidates. Use it as reference when you want to edit the code without breaking the TTL, dedup, or RediSearch contracts.

## Why N3MemoryCore? (vs. built-in memory)

The auto-save *reliability* of N3MemoryCore is no better than the memory
features built into modern LLM products (e.g. Claude's built-in memory)
— both depend on the LLM voluntarily calling a save tool, and both share
the non-determinism described in *On compliance* below. The differentiation
sits elsewhere:

| Aspect | Built-in memory | N3MemoryCore (Lite) |
|---|---|---|
| **Data ownership** | Vendor-hosted | **Your own Redis Stack on your machine** |
| **Client surface** | The vendor's product only | **Any MCP-compliant client** (Claude Code, Cursor, Cline, Goose, your own app) |
| **Multi-AI collaboration** | One AI's memory | **`session_id` lets multiple agents share the same memory namespace; `delete_memories_by_session` wraps up a finished task** |
| **Verbatim recall** | Opaque (may be summarized) | **Parent-document contract — byte-exact full text returned** |
| **Search internals** | Black box | **Hybrid BM25 + e5 vectors + CJK bigram + time decay + lightweight reranker, all parameters visible and tunable** |
| **Inspect / control** | UI only | **`list_memories` / `delete_memory` / `delete_memories_by_session` operate on raw records** |
| **Persistence** | Tied to the vendor's service lifetime | **In-memory Redis with 7-day TTL** — short-lived by design, but you own the container; pair with any persistent memory backend for long-term storage |
| **Tunability** | Fixed | `half_life_days`, `chunk_threshold`, `dedup_threshold`, rerank weights — all editable |

So the value of running N3MemoryCore Lite is **not** "more reliable
auto-save" — it is **owning a transparent, multi-client working-memory
layer** that several AIs can collaborate on under a shared `session_id`,
where search behaviour is editable and verbatim recall is contractually
guaranteed. (For long-term, persistent storage of user-invested artifacts,
pair it with any persistent memory backend.)

If those properties matter to your workflow, Lite earns its keep. If you
only need "the LLM remembers something across sessions" inside one
vendor's product, the built-in memory is simpler.

## On compliance — MCP can persuade, not force

This server cannot make the LLM call its tools. The MCP protocol gives a
server only three persuasion levers:

1. **Tool descriptions** in `tools/list` — visible to the LLM on every turn.
2. **The `instructions` field** sent at session start — usually surfaced to
   the LLM as a system-level hint.
3. **Tool response text** — read by the LLM when it does call a tool.

We use all three: tool descriptions are explicit, `instructions` lays out a
rule set, and `search_memory` / `save_memory` responses end with short
reminders that re-anchor the auto-save discipline mid-turn. Even with all
of that, **whether the LLM follows through is non-deterministic**.
Compliance depends on the model's tool-calling bias, the MCP client's
prompt construction (some clients summarize or drop the `instructions`
field), and competing instructions from the user prompt, `CLAUDE.md`, etc.

In practice: **most turns will auto-save correctly, but some won't** —
especially short answers, fact-correction turns, or turns where the LLM is
heavily focused on the user's question. If a fact you wanted saved is
missing next session, just say "save this" — the server is still ready to
take it.

### When you need a guaranteed save

Within the MCP framing, three paths bypass this non-determinism:

**Path 1 — ask the LLM explicitly in your prompt** (operational workaround,
immediate). Write *"save this to N3MemoryCore"* or *"record this in
memory"* into your prompt. LLMs almost always honour explicit user
requests. Pros: zero infrastructure, works today, works with every MCP
client. Cons: cognitive load — you must remember to say it; not automatic.

### Hook-based full-transcript saving

**Path 2 — Claude Code hook that saves the full transcript** (Claude Code
only, deterministic). Claude Code exposes harness-level hooks (`Stop`,
etc.) that the harness runs deterministically — they do not depend on the
LLM remembering anything. Setup is one prompt to Claude Code:

> *"After every turn, automatically save the full Claude Code transcript
> to Lite."*

Claude Code then provisions:

- A script at `~/.claude/hooks/save_transcript.py` that reads
  `transcript_path` from hook input, imports `n3mc_mcp.database.Database`
  directly, and calls `save_memory` on the Lite DB (no MCP round-trip).
- A `hooks.Stop` block in `~/.claude/settings.json` that runs the script
  after every assistant turn with `async: true` (so model load never
  blocks the UI).

Behavioral notes:

- **Claude can never accidentally skip a save** — the harness fires the
  hook regardless of what the LLM does.
- No MCP round-trip overhead; the hook talks to Redis directly.
- As a session grows, the per-turn transcripts collide via near-duplicate
  detection (`dedup_threshold`), so the DB stays close to **one entry per
  session** instead of one per turn.
- Transcripts shorter than ~200 chars are skipped as noise.
- Pros: deterministic / independent of model behavior / no save anxiety.
- Cons: Claude Code only (Cursor / Windsurf need a different approach) /
  the hook process loads the embedding model each turn (async, so no UI
  block, but there is CPU/IO cost) /
  **Lite's 7-day TTL still applies**, so transcripts saved this way still
  expire within a week — point the same hook at any persistent memory
  backend when long-term retention matters.

**Path 3 — bypass MCP and call the first-party Anthropic Messages API
yourself** (architecture change). Step outside MCP clients (Claude Code,
etc.) and drive `messages.create` `tool_use` directly from your own
application code; you can then fire `save_memory` deterministically every
turn regardless of what the LLM "decided" to do. Pros: deterministic /
works with any model and any client. Cons: you have to write the
orchestration application.

The convenience of "MCP + LLM handles it for me" and the guarantee of
"every turn saves" sit at opposite ends of a tradeoff. This server packs
its persuasion levers as hard as the protocol allows; any stronger
guarantee is your call as the user or client implementer (and if you're
on Claude Code, Path 2 is by far the lowest-cost option).

## Forking & contributing

This repository is **public and Apache-2.0 licensed** — fork, modify,
and run it freely. The fork-and-run path is:

```bash
git clone https://github.com/<YOU>/n3mcmcp-lite
cd n3mcmcp-lite
docker run -d --name redis-stack -p 6379:6379 redis/redis-stack-server:latest
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest tests/ -q                                      # 105 tests, ~30s warm
```

CI runs the same matrix on every push and PR — see
[`.github/workflows/test.yml`](./.github/workflows/test.yml). Read
[`CONTRIBUTING.md`](./CONTRIBUTING.md) for the full developer guide
(EN + JP) including coding conventions, the spec-as-contract policy,
and PR checklist.

**To actually use the fork from Claude Code**, you do NOT need any
additional setup beyond the `pip install -e ".[dev]"` above:

1. The `n3mc-workingmemory` command is now on your `PATH` (run
   `which n3mc-workingmemory` to confirm).
2. The repository's [`.mcp.json`](./.mcp.json) already declares the
   server, so the moment you `cd n3mcmcp-lite && claude`, the CLI
   auto-connects.
3. For other client surfaces (Claude Desktop, a different project's
   `.mcp.json`, auto-tool-approval), the [Quickstart Step 3
   table](#-quickstart--connect-to-claude-code-in-3-steps) lists the
   exact action.

If you intend to publish your fork under a new package name, also
edit the `name`, `[project.urls]`, and console-script names in
[`pyproject.toml`](./pyproject.toml) before re-uploading to PyPI.

## Troubleshooting

### Windows: `pip install --upgrade` fails with `WinError 32` (file in use)

Symptom:
```
ERROR: Could not install packages due to an OSError: [WinError 32]
The process cannot access the file because it is being used by another process:
'...\Scripts\n3mc-workingmemory.exe' -> '...\Scripts\n3mc-workingmemory.exe.deleteme'
```

Cause: an MCP client (Claude Code / Claude Desktop) is currently holding
`n3mc-workingmemory.exe` open as a child process, so pip cannot replace
the binary.

Fix — pick one:

1. **Fully quit the MCP client first.** Closing the window is not enough
   on Windows. Open Task Manager and end every `claude` /
   `n3mc-workingmemory.exe` / `python.exe` process whose command line
   includes `n3mc-workingmemory`, then re-run `pip install --upgrade`.
2. **Use `uvx` instead of a global install** — `uvx --from
   n3memorycore-mcp-lite n3mc-workingmemory` runs in an isolated
   ephemeral environment per session, so there is no system-level
   `.exe` to lock.

This is a Windows file-locking quirk, not a packaging defect — the wheel
itself installs cleanly into a fresh venv (`python -m venv .venv &&
.venv/Scripts/pip install n3memorycore-mcp-lite`).

### `~3memorycore-mcp-lite` warnings during pip install

If you see lines like:
```
WARNING: Ignoring invalid distribution ~3memorycore-mcp-lite
```
that is pip flagging a previous install that was interrupted mid-write
(typically by the file-lock issue above). The leftover directory is
named with a leading `~` and is harmless but noisy. Delete it manually:

```bash
# Windows
rmdir /s "%LOCALAPPDATA%\Programs\Python\Python312\Lib\site-packages\~3memorycore_mcp_lite-1.5.0.dist-info"
```

(Adjust the path to match your Python installation.)

## License

Apache License 2.0 — see [LICENSE](./LICENSE).

---

<sub>MCP Registry: `mcp-name: io.github.NeuralNexusNote/n3mc-workingmemory`</sub>
