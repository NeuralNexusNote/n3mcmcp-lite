# N3MemoryCore MCP — Lite (Ephemeral)

> A NeuralNexusNote™ product — **free Lite** build: ephemeral hybrid
> (vector + BM25) memory exposed as a Model Context Protocol server, backed
> by Redis Stack with a 7-day TTL per entry.

> 🇯🇵 **[日本語版はこちら](./README_JP.md)**
> 🛡️ **[Development Philosophy](./PHILOSOPHY.md)**

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
docker run -d --name redis-stack -p 6379:6379 redis/redis-stack-server:latest
```

That's it — the container exposes Redis on `localhost:6379` and the
server will find it automatically.

### 2. Install the package

```bash
pip install n3memorycore-mcp-lite
```

Or from source:

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

Add to your project's `.mcp.json`:

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
  "owner_id": "<uuid>",
  "local_id": "<uuid>",
  "redis_url": "redis://localhost:6379/0",
  "ttl_seconds": 604800,
  "dedup_threshold": 0.95,
  "half_life_days": 3,
  "bm25_min_threshold": 0.1,
  "search_result_limit": 20,
  "min_score": 0.2,
  "search_query_max_chars": 2000
}
```

`redis_url` can also be supplied via the `N3MC_REDIS_URL` environment
variable (takes precedence over the config file).

## Ranking formula

```
final_score = (0.7 * cosine_similarity + 0.3 * keyword_relevance) * time_decay

time_decay = 2 ^ (-days_elapsed / half_life_days)       (default half-life: 3 days)
```

With a default 3-day half-life (shorter than the 7-day TTL), `time_decay`
is meaningful in the Lite build: a fresh memory scores 1.0, a 3-day-old
one exactly 0.5, and a 7-day-old (near-expiry) entry ≈ 0.20 — pushing
recent context ahead in the ranking.

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

`pip install n3memorycore-mcp-lite` installs the runtime only — the design spec is **not** shipped with the package. If you want to modify behavior (change the ranking formula, drop in a cross-encoder reranker, plug in a Japanese morphological tokenizer, etc.), download the spec from GitHub first:

- [`N3MemoryCore_MCP_Spec_EN.md`](https://github.com/NeuralNexusNote/n3mcmcp-lite/blob/main/N3MemoryCore_MCP_Spec_EN.md) — full design document (English)
- [`N3MemoryCore_MCP_Spec_JP.md`](https://github.com/NeuralNexusNote/n3mcmcp-lite/blob/main/N3MemoryCore_MCP_Spec_JP.md) — 日本語版

Appendix B of the spec lists optional extensions (cross-encoder reranker, save-time chunking, HyDE, Japanese morphological analysis) with drop-in points and library candidates. The spec gives an AI (or human) enough context to edit the code without breaking the TTL, dedup, or RediSearch contracts. Extending the package by reading only the installed source is possible but much harder — the spec is the source of truth for design intent.

## License

Apache License 2.0 — see [LICENSE](./LICENSE).
