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
   docker run -d --name redis-stack -p 6379:6379 redis/redis-stack-server:latest --appendonly yes

   # Every subsequent session (container already exists):
   docker start redis-stack
   ```
   Re-running the `docker run` command after the container exists fails with `Conflict. The container name "/redis-stack" is already in use`. Use `docker start` from the second session onward.

   > **Why `--appendonly yes`**: enables Redis AOF persistence so writes survive
   > container restart / crash. Without it, anything saved between the periodic
   > RDB snapshots (default: every 1–60 minutes depending on write volume) is
   > lost on restart. Memory is still ephemeral via the 7-day TTL — AOF just
   > closes the "saved but not snapshotted yet" data-loss window.
2. **[`uv`](https://docs.astral.sh/uv/) on your `PATH`** — required only for the Claude Code plugin / `uvx` install path. Not needed if you install from source.

The server refuses to start if Redis is unreachable, and the Claude Code plugin will fail to launch without `uv`. Install both before running `/plugin install` or any client-side config.

---

## Lite vs. Paid

| Build                   | Storage                           | Durability        | Where                |
| ----------------------- | --------------------------------- | ----------------- | -------------------- |
| **Lite (this repo)**   | Redis Stack (RediSearch)          | 7d TTL, volatile | Claude Marketplace   |
| Paid                    | SQLite + sqlite-vec (local file)  | Permanent         | Separate distribution |

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

The Paid build targets the opposite use case: long-term knowledge
accumulation where persistence is the feature. Pick the Lite for
**project-scoped memory**; pick the Paid for **continuous memory**.

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
docker run -d --name redis-stack -p 6379:6379 redis/redis-stack-server:latest --appendonly yes

# Every subsequent session (container already exists):
docker start redis-stack
```

That's it — the container exposes Redis on `localhost:6379` and the
server will find it automatically. Re-running the `docker run` command
after the first install produces `Conflict. The container name
"/redis-stack" is already in use`; use `docker start redis-stack`
thereafter.

### 2. Install the package

Install from source (PyPI distribution is not yet available):

```bash
git clone https://github.com/NeuralNexusNote/n3mcmcp-lite
cd n3mcmcp-lite
pip install -e .
```

The first run downloads the ~400 MB embedding model from Hugging Face
into the standard `~/.cache/huggingface/` directory.

## Configure a client

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "n3memorycore-lite": {
      "command": "n3mc-mcp-lite",
      "args": []
    }
  }
}
```

### Claude Code

**`.mcp.json` is already included in this repository.** Clone the repo,
install the package, and Claude Code connects automatically — no manual
configuration needed.

For other projects, add the following to that project's `.mcp.json`:

```json
{
  "mcpServers": {
    "n3memorycore-lite": {
      "type": "stdio",
      "command": "n3mc-mcp-lite",
      "args": []
    }
  }
}
```

### Auto-approve tool calls (Claude Code only)

By default, Claude Code prompts the user for each MCP tool call. For a
fully automatic memory loop — so the connected AI never blocks on an
"Allow?" prompt — the `n3memorycore-lite` tools must be listed under
`permissions.allow` in Claude Code settings.

**Plugin install auto-configures this** — when you install via
`/plugin install n3memorycore-lite@neuralnexusnote`, a `SessionStart`
hook runs [`hooks/install_permissions.py`](plugins/n3memorycore-lite/hooks/install_permissions.py)
which idempotently adds the five `mcp__n3memorycore-lite__*` tools to
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
      "mcp__n3memorycore-lite__search_memory",
      "mcp__n3memorycore-lite__save_memory",
      "mcp__n3memorycore-lite__list_memories",
      "mcp__n3memorycore-lite__delete_memory",
      "mcp__n3memorycore-lite__repair_memory"
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
| Windows | `%LOCALAPPDATA%\n3memorycore-lite\`                       |
| macOS   | `~/Library/Application Support/n3memorycore-lite/`        |
| Linux   | `~/.local/share/n3memorycore-lite/`                       |

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
  "rerank_phrase_weight":     0.2
}
```

- `redis_url` — connection URL; `N3MC_REDIS_URL` env var takes precedence.
- `ttl_seconds` — TTL on every new memory and sha-guard (default 7 d).
- `chunk_threshold` / `chunk_overlap` — sliding-window size and overlap (chars). Bodies longer than the threshold trigger the parent-document + chunks path for verbatim recall.
- `access_count_*` — access-frequency auto-importance; top-K search hits receive a capped boost on future queries.
- `ttl_refresh_on_search` / `ttl_refresh_top_k` — TTL reset for the top-K hits on each search (reset-only; no extension past a fresh save).
- `lexical_rerank_*` / `rerank_weight` / `rerank_phrase_weight` — lightweight post-fusion lexical reranker (CPU-only).

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

Appendix B of the spec lists optional extensions (cross-encoder reranker, save-time chunking, HyDE, Japanese morphological analysis) with drop-in points and library candidates. The spec gives an AI (or human) enough context to edit the code without breaking the TTL, dedup, or RediSearch contracts — it is the source of truth for design intent.

## License

Apache License 2.0 — see [LICENSE](./LICENSE).
