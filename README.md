# N3MemoryCore MCP — Lite (Ephemeral)

> **N3MC-MCP-Lite is an "external memory server" used by MCP-compatible
> editors such as Claude Code, Cursor, and Windsurf.**
> It runs as an MCP Server so AI can save and search conversation and
> code context across sessions.

> A NeuralNexusNote™ product — **free Lite** build: ephemeral hybrid
> (vector + BM25) memory exposed as a Model Context Protocol server, backed
> by Redis Stack with a 7-day TTL per entry.

> 🇯🇵 **[日本語版はこちら](./README_JP.md)**
> 🛡️ **[Development Philosophy](./PHILOSOPHY.md)**

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

   > **Why no persistence flags on the docker line**: the Lite build is
   > *deliberately volatile*. Ephemerality is the product boundary that
   > separates Lite from the paid, persistent N3MemoryCore build. Rather
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
- 🔄 **Context across sessions** — Working memory that lasts **7 days** (auto-expires via Redis TTL; use Pro for long-term memory).
- ⚡ **Works automatically** — Saving and searching happen automatically. The MCP `initialize` response ships behavioral instructions, so no user action is required.
- 🤖 **Multi-agent ready** — Multiple AI agents share one Redis. The `b_local` bias prioritizes each agent's own memories while still surfacing the team's collective knowledge.
- 🏢 **Team & organization support** — Deploy Redis on a shared server and point `N3MC_REDIS_URL` to it for team-wide memory sharing (⚠️ authentication must be handled at the Redis layer).
- 🧹 **Ephemerality is a design feature** — 7-day auto-expiry means failed attempts and abandoned designs don't bleed into the next task. `docker restart redis-stack` wipes everything instantly.
- 💰 **Reduces token waste** — No more re-explaining past context. Memory search uses local embeddings (`intfloat/e5-base-v2`) and costs zero Claude tokens, and accurate context injection means fewer corrections and back-and-forth.

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
- **Conversation context and history** (discussion threads, past decisions) → N3MemoryCore accumulates automatically (7 days in Lite, permanent in Pro)

---

## Lite vs. Pro (coming soon)

| Build                      | Storage                           | Durability        | Where                 |
| -------------------------- | --------------------------------- | ----------------- | --------------------- |
| **Lite (this repo)**       | Redis Stack (RediSearch)          | 7d TTL, volatile  | Claude Marketplace    |
| **Pro (coming soon)**      | SQLite + sqlite-vec (local file)  | Permanent         | Separate distribution |

Same MCP surface (five tools, same ranking formula). The 7-day TTL and
volatile Redis storage are **design features, not limitations** —
they make the Lite build the better fit for:

- **Agentic code-generation loops** — failed attempts and abandoned
  designs don't bleed into the next task; `docker restart redis-stack`
  wipes the slate clean.
- **Multi-agent collaboration** — decisions made during one task don't
  contaminate unrelated follow-ups.
- **Experimental / throwaway prototyping** — leave it alone and memory
  evaporates in 7 days, no pruning needed.

The **Pro build (coming soon)** will target the opposite use case:
long-term knowledge accumulation where persistence is the feature.
Pick Lite for **project-scoped working memory**; the Pro build will
offer **continuous memory** when released.

## What is this?

`n3memorycore-mcp-lite` is a local-only MCP server that gives Claude (and
any other MCP-compatible client) short-lived memory across conversations.
It stores text entries in a local Redis Stack instance with both a BM25
full-text index and a 768-dimension vector index
([`intfloat/e5-base-v2`](https://huggingface.co/intfloat/e5-base-v2)), and
returns hybrid-ranked results.

Every operation runs on the user's machine. No API calls, no cloud
storage.

## Tools exposed

| Tool             | Purpose                                                       |
| ---------------- | ------------------------------------------------------------- |
| `search_memory`  | Hybrid (vector + BM25) search, ranked & time-decayed          |
| `save_memory`    | Persist a short entry (7d TTL, dedup: exact + near-duplicate) |
| `list_memories`  | Most-recent entries, newest first                             |
| `delete_memory`  | Remove a specific entry by id                                 |
| `repair_memory`  | Re-create the RediSearch index if missing                     |

The server also ships **behavioral instructions** via MCP's `initialize`
response, asking the client to `search_memory` at the start of each turn
and `save_memory` after each meaningful exchange — so "auto-save" is
preserved without any Claude Code hooks.

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
which idempotently adds the five `mcp__n3mc-workingmemory__*` tools to
`~/.claude/settings.json`. No manual editing needed. The hook only
writes if at least one entry is missing and never touches unrelated
fields. Requires `python` on `PATH`.

**If you installed without the plugin** (e.g. `claude mcp add` or a
manual `.mcp.json`), or the hook could not find Python, add the block
below manually to `~/.claude/settings.json` (user-global, recommended)
or `.claude/settings.json` (per-project):

```json
{
  "permissions": {
    "allow": [
      "mcp__n3mc-workingmemory__search_memory",
      "mcp__n3mc-workingmemory__save_memory",
      "mcp__n3mc-workingmemory__list_memories",
      "mcp__n3mc-workingmemory__delete_memory",
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
  "skip_code_blocks":         false
}
```

- `redis_url` — connection URL; `N3MC_REDIS_URL` env var takes precedence.
- `ttl_seconds` — TTL on every new memory and sha-guard (default 7 d).
- `chunk_threshold` / `chunk_overlap` — sliding-window size and overlap (chars). Bodies longer than the threshold trigger the parent-document + chunks path for verbatim recall.
- `access_count_*` — access-frequency auto-importance; top-K search hits receive a capped boost on future queries.
- `ttl_refresh_on_search` / `ttl_refresh_top_k` — TTL reset for the top-K hits on each search (reset-only; no extension past a fresh save).
- `lexical_rerank_*` / `rerank_weight` / `rerank_phrase_weight` — lightweight post-fusion lexical reranker (CPU-only).
- `skip_code_blocks` — when `true`, `save_memory` rejects any payload containing a triple-backtick fence (```` ``` ````) and returns `status: "skipped_code"`. Default `false`. Set to `true` if you want FastAPI-era N3MemoryCore-style code exclusion (keep code out of the memory index entirely — useful when your workflow already has git/IDE history for code and you only want prose decisions/plans in Redis).

See the spec §6 for the complete field-by-field reference.

## Ranking formula

```
final_score = (0.7 * cosine_similarity + 0.3 * keyword_relevance) * time_decay * b_local

time_decay   = 2 ^ (-days_elapsed / half_life_days)       (default half-life: 3 days)
b_local      = clamp(0.5, 2.0, stored_importance + access_boost)
access_boost = min(0.5, access_count * 0.02)
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

## License

Apache License 2.0 — see [LICENSE](./LICENSE).
