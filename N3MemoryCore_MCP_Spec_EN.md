# N3MemoryCore MCP v1.1.0 [Volatile Memory over MCP]
> A NeuralNexusNote™ product — **Lite (ephemeral) build**

> **What is this variant?** The Lite build is the free, marketplace-targeted edition of N3MemoryCore MCP. Storage is **Redis Stack (RediSearch)**, every entry carries a **7-day TTL**, and nothing persists beyond that window. Think of it as a public test drive — the paid build uses SQLite and stores memories permanently.
>
> **Who is this for?** Users of any MCP-compatible LLM client (Claude Desktop, Claude Code, and others) who want searchable, short-lived memory shared across sessions within a 7-day window.
>
> **Transport**: Model Context Protocol over stdio (JSON-RPC). Tested on Windows 11 with Python 3.12 and Redis Stack 7.x.

## ⚠️ Disclaimer & Distribution Terms

This software and specification are provided **"AS-IS"** without warranty of any kind.

- **No Support**: The author provides no bug fixes, answers to questions, or guarantees of operation.
- **No Warranty / No Liability**: The author shall not be liable for any damages arising from use of this software, including but not limited to data loss, business interruption, or third-party claims.
- **Use at Your Own Risk**: You assume full responsibility for your use of this software.
- **Right to Change**: The author may modify or discontinue this software at any time without notice.

By using this software, you agree to the terms above.

- **License**: Apache License 2.0. See the `LICENSE` file for details.

> **Removal (Uninstall)**: `pip uninstall n3memorycore-mcp-lite` removes the package. Stop and delete the Redis container (`docker rm -f redis-stack`) to erase all stored memories instantly. Delete `${N3MC_DATA_DIR}` (or the platform default data dir) to remove `config.json`. Also remove the `n3memorycore-lite` entry from your MCP client's config file.
>
> **Backup?** The Lite build is **not designed to be backed up**. Entries vanish on a 7d rolling window; if you need durable memory, use the paid build.

> **For implementation questions**: While the author cannot be contacted for support, you can load this specification into Claude and ask questions directly — Claude can assist with implementation and customization.

---

## Lite: Volatile Memory

This section captures the tradeoffs unique to the Lite build; the rest of the specification deliberately reuses the paid-build structure so AI-driven regeneration stays simple.

| Property               | Lite (this spec)                         | Paid (separate spec)                   |
| ---------------------- | ----------------------------------------- | -------------------------------------- |
| Storage engine         | Redis Stack (RediSearch module)           | SQLite + sqlite-vec (local file)       |
| Durability             | **7 d TTL per entry**, ephemeral         | Permanent, disk-persistent             |
| Disk footprint         | Only `config.json` (< 1 KB)               | `n3memory.db` grows with history       |
| External dependency    | User-run Redis Stack container            | None (self-contained)                  |
| `time_decay` relevance | Meaningful (3-day half-life; fresh=1.0, 7d≈0.20) | Meaningful (90-day half-life) |
| Re-indexing / repair   | `FT.CREATE` is idempotent; no migrations  | Schema + model migration markers       |
| Intended use           | Short-term projects, evaluation, marketplace | Ongoing projects                    |

**Volatility contract:**
- Every write to Redis sets a TTL equal to `ttl_seconds` (default 604 800 = 7 d).
- Both the primary record (`mem:<uuid>`) and its exact-duplicate guard key (`mem:sha:<sha1>`) share the same TTL and expire together.
- Expiration is delegated to Redis; no background cleanup job runs.
- Restarting the Redis container with its volume removed wipes all memory immediately.

**No cross-session guarantee beyond 7 d.** Unlike the paid build, the Lite spec deliberately forbids any "persistence hack" — do not add RDB snapshots, AOF rewrite, or external dumps to circumvent the TTL. If durability is needed, use the paid variant.

---

## Setup

### Prerequisites

| Item                          | Requirement                                                              |
| :---------------------------- | :----------------------------------------------------------------------- |
| Python                        | 3.10 or higher                                                           |
| MCP-compatible client         | Claude Desktop, Claude Code, or any other MCP client                     |
| Redis Stack                   | Running on `localhost:6379` (or any URL set via `N3MC_REDIS_URL`)        |
| pip packages (auto-installed) | `mcp` `redis` `sentence-transformers` `uuid-utils` `platformdirs` `numpy` |

### Quick Start

1. Start Redis Stack (once):
   ```bash
   docker run -d --name redis-stack -p 6379:6379 redis/redis-stack-server:latest
   ```
2. Install the package (choose one):
   - **pip** (global or venv):
     ```bash
     pip install n3memorycore-mcp-lite
     ```
   - **uvx** (zero-install, isolated env — requires [`uv`](https://docs.astral.sh/uv/)):
     ```bash
     uvx --from n3memorycore-mcp-lite n3mc-mcp-lite
     ```
   - **Claude Code plugin marketplace** (no pip/uvx command required — the plugin configures `uvx` launch for you, but `uv` must still be on PATH):
     ```
     /plugin marketplace add NeuralNexusNote/n3mcmcp-lite
     /plugin install n3memorycore-lite@neuralnexusnote
     ```
3. Register the server in your MCP client's config (see [§8](#8-mcp-client-configuration)). Skip this step when installing via the plugin marketplace — the plugin registers the server automatically.
4. Restart the client. The first tool call may take 30–60 seconds as the ~400 MB embedding model is downloaded and loaded.

### Data Backup

Not applicable. See the [Lite: Volatile Memory](#lite-volatile-memory) section — the build is explicitly ephemeral. `config.json` (containing `owner_id` / `local_id` UUIDs) is the only on-disk artifact and can be copied if the user wants to keep the same owner identity across re-installs.

---

## 1. Vision

Provide a no-commitment memory endpoint for MCP clients: hybrid search (vector + RediSearch BM25), mathematically sound ranking, 7-day automatic garbage collection. The MCP server delivers behavioral instructions so the connected LLM auto-searches at the start of each turn and auto-saves after each meaningful exchange — without requiring client-side hooks.

The Lite exists to demonstrate the N3MemoryCore MCP surface on the Claude Marketplace with zero risk to the user's disk; upgrading to the paid build swaps the storage layer from Redis to SQLite while preserving the MCP surface.

> **⚠️ Python check**: Before installing, run `python --version` to verify Python 3.10+ is available.

> **⚠️ First-run download**: `sentence-transformers` downloads the `e5-base-v2` model (~440 MB) on first tool use. The server will appear unresponsive during this time — this is expected. Subsequent starts complete in seconds once cached.

> **Important: Character Limits (Design Constraints)**
> - Auto-save per entry: **50–200 characters recommended** (one fact per entry).
> - Search query: **2,000 characters** (configurable via `search_query_max_chars`).
> - Vector search: Only the first **~2,000 characters** of any record are semantically searchable (embedding model limit: 512 tokens). Beyond this, content is stored and BM25-searchable but invisible to vector similarity.
> - **Large text handling**: When the user pastes a long text (spec, article, log, etc.), the LLM must NOT save it as-is. Instead: read and understand the full content, extract each key fact as a separate short sentence (~50–200 chars), and call `save_memory` once per fact.

---

## 2. Package Structure

```
n3memorycore-mcp-lite/
├── pyproject.toml                  # Package metadata, entry point 'n3mc-mcp-lite'
├── n3mc_mcp/                       # Python package
│   ├── __init__.py                 # Version marker
│   ├── __main__.py                 # Entry point: python -m n3mc_mcp
│   ├── server.py                   # MCP server definition + 5 tools
│   ├── instructions.py             # Behavioral instructions delivered at initialize
│   ├── database.py                 # Redis layer: index, CRUD, TTL, dedup
│   ├── processor.py                # Embedding, ranking, text purification
│   ├── config.py                   # config.json load/save + UUID generation
│   └── paths.py                    # platformdirs-based config location
├── tests/
│   ├── conftest.py                 # Skips if Redis Stack not reachable
│   ├── test_database.py
│   ├── test_processor.py
│   └── test_server.py
└── examples/
    ├── claude_desktop_config.json
    └── claude_code_mcp.json
```

`config.json` lives in a **user data directory** (see [§7](#7-data-location)). No database file exists on disk — memory state lives entirely in Redis.

---

## 3. Technical Specifications (No Modifications Allowed)

> **⚠️ AI must not auto-modify**: AI must not autonomously change any of the following specifications for speed improvement or optimization. Changes to the embedding model, vector dimensions, or TTL are only permitted by a human manually editing `config.json`.

### 3.1 ID Hierarchy

N3MemoryCore uses 5 ID fields to identify the origin and context of each record:

| ID           | Stored in       | Generated                          | Granularity          | Purpose                                                                                            |
| ------------ | --------------- | ---------------------------------- | -------------------- | -------------------------------------------------------------------------------------------------- |
| `id` (PK)    | Redis hash      | Per record (UUIDv7, time-ordered)  | **One record**       | Unique identifier for each memory — used for deletion and dedup                                    |
| `owner_id`   | `config.json`   | First startup (UUIDv4)             | **Owner**            | Identifies whose data this is — used as a TAG filter in RediSearch                                 |
| `local_id` (agent_id)   | `config.json`   | First startup (UUIDv4)             | **Agent / install**  | UUIDv4 identifier for the install. Stored for compatibility; not used in Lite ranking.            |
| `session_id` | In-memory       | Per server process startup (UUIDv4) | **Server process**   | Identifies which server process wrote the record (stored for compatibility; not used in Lite ranking). |
| `agent_name`   | Redis hash      | Per `save_memory` call (free-form) | **Agent display**    | Human-readable label (e.g. `"claude-desktop"`, `"claude-code"`).                                   |

### 3.2 Embeddings

- Model: `intfloat/e5-base-v2` / Vector: `float[768]`
- Always specify `normalize_embeddings=True` at encoding time to guarantee L2-normalized vectors (norm=1). This matters even with cosine distance: an unnormalized input breaks the `(1 − cosine_distance)` ↔ similarity identity.
- **Input Prefixes (Required)**: Without prefixes, this model's accuracy degrades significantly:

  ```python
  # At save time (registering as a document)
  text_to_embed = "passage: " + content

  # At search time (matching as a query)
  text_to_embed = "query: " + keyword
  ```

### 3.3 Redis Connection & TTL

**Connection**: constructed from `redis_url` (config field) or the `N3MC_REDIS_URL` environment variable (env wins). Default: `redis://localhost:6379/0`. `decode_responses=False` — the client must handle binary embedding payloads.

**TTL**: every `HSET` of `mem:<uuid>` is followed (atomically via `PIPELINE`) by `EXPIRE mem:<uuid> <ttl_seconds>`. The sibling `mem:sha:<sha1>` guard is written with `SET ... EX <ttl_seconds>` in the same pipeline. Default TTL is 604 800 s (7 d).

**Pipeline atomicity**: the three commands (`HSET`, `EXPIRE`, `SET`) ship as one pipeline, so partial-failure interleavings that could produce a record without a TTL or a sha-guard without a record are not possible.

### 3.4 Ephemerality (No Modifications or Optimizations Allowed)

On `save_memory` calls, complete HSET + EXPIRE + sha1-guard in a single pipeline — no batching, no queuing. The key must have a finite TTL when the `save_memory` response returns.

**The following are absolutely prohibited (even for "performance" or "persistence" reasons):**
- Writing records without an `EXPIRE` (i.e. infinite TTL).
- Enabling Redis RDB / AOF persistence policies that would survive container removal for the sole purpose of extending Lite memory lifespan. (The user may, of course, choose any Redis configuration; the spec simply doesn't rely on it.)
- Re-extending TTL on read (`TOUCH`, `EXPIRE` on `search_memory`).
- Write buffering / deferred pipelines beyond the single save call.

**Reason**: the Lite build's differentiation is explicit volatility; circumventing it erodes the product distinction.

### 3.5 Data Layout

```
mem:<uuid>                  HASH
    id              string      UUIDv7 (same as the key suffix)
    content         string      original text (post-purify)
    timestamp       string      ISO 8601 UTC
    timestamp_epoch number      unix seconds (SORTABLE)
    owner_id        string      TAG
    local_id        string      TAG
    agent_name        string      TAG
    session_id      string      TAG
    embedding       bytes       FLOAT32 * 768 little-endian
    TTL                         ttl_seconds (default 604 800)

mem:sha:<sha1>              STRING
    value = the associated mem id
    TTL = same as mem:<uuid>

n3mc_idx                    RediSearch index, ON HASH PREFIX 1 mem:
    SCHEMA:
        content         TEXT
        timestamp_epoch NUMERIC SORTABLE
        owner_id        TAG
        local_id        TAG
        agent_name        TAG
        session_id      TAG
        embedding       VECTOR FLAT 6 TYPE FLOAT32 DIM 768 DISTANCE_METRIC COSINE
```

- **Primary Key**: UUIDv7 (time-sortable; generated at insert time). The reference implementation uses `uuid_utils.uuid7`.
- **Delete semantics**: `delete_memory` deletes `mem:<uuid>` and its sibling `mem:sha:<sha1>` (fetched via `HGET mem:<uuid> content` → sha1) in a single pipeline. Redis removes the index entry automatically because the hash no longer exists.

### 3.6 Ranking Formula

Identical to the paid build:

```
Final Score = (cos_sim × 0.7 + keyword_relevance × 0.3) × time_decay
```

**cos_sim** — **derived directly from RediSearch's cosine distance**:

$$cos\_sim = \max(0,\ \min(1,\ 1.0 - cosine\_distance))$$

RediSearch returns `cosine_distance ∈ [0, 2]` for normalized vectors. Clamping to `[0, 1]` discards the "opposite direction" half-space, which we treat as irrelevant for memory retrieval.

**keyword_relevance** — normalize RediSearch BM25 scores to `[0.0, 1.0]`:

1. If `|bm25_score| < bm25_min_threshold` (default `0.1`), set to `0.0`.
2. Otherwise: `|bm25_score| / max(1.0, max_|bm25_score| in result set)`.

(RediSearch BM25 scores are non-negative, but the `abs()` keeps the algorithm identical to the paid build where FTS5 produces negative scores.)

**time_decay**:

$$time\_decay = 2^{-\frac{days\_elapsed}{half\_life\_days}}$$

Default `half_life_days = 3` — deliberately shorter than the 7-day TTL so that `time_decay` is actually informative in the Lite build: a fresh entry scores 1.0, a 3-day-old one exactly 0.5, and a 7-day-old (near-expiry) one ≈ 0.20. This pushes recent context ahead in ranking. This is a Lite-specific tuning; the paid build keeps a 90-day half-life to match its permanent horizon.

### 3.7 Text Tokenization & Punctuation Handling

**Tokenizer**: RediSearch's built-in tokenizer (whitespace + punctuation split, case-folded). The Porter stemmer used by the paid build is **not** available here; the Lite accepts RediSearch's default behaviour as a documented tradeoff.

**Query cleaning** — apply `strip_fts_punctuation` to the user's query string *before* submitting it to RediSearch, and backslash-escape remaining RediSearch special characters. Store raw `content` in the hash (RediSearch will tokenize it on the fly).

```python
_PUNCT_STRIP_RE = re.compile(r'[,.<>\{\}\[\]"\':;!@#\$%\^&\*\(\)\-\+=~\|\\/?`]')
_FTS_SPECIAL_RE = re.compile(r'([,.<>\{\}\[\]"\':;!@#\$%\^&\*\(\)\-\+=~\|\\/?])')
```

**Empty-query rule**: if the cleaned query is empty after stripping, skip keyword search and rank using vector search only.

### 3.8 Duplicate Rejection

On every `save_memory` call, reject duplicates in this order:

1. **Exact dedup (O(1))** — `EXISTS mem:sha:<sha1(content)>`. If the key exists, return `{"status": "duplicate", "saved": false}`.
2. **Near-duplicate (semantic) dedup** — compute the embedding, run KNN=1 against `@embedding` filtered by the current `owner_id`, convert `cosine_distance` → `cos_sim`. If `cos_sim >= dedup_threshold` (default `0.95`), return `{"status": "near_duplicate", "saved": false, "similarity": <value>}`.

Only if both checks pass, proceed with the HSET + EXPIRE + sha1-guard pipeline.

### 3.9 Startup Sequence & Self-Recovery

The server's `_startup()` runs these steps in order, **before** the stdio loop begins accepting requests:

1. **Load config** (`load_config()`):
   - Read `config.json` from the data directory.
   - **If the file is corrupt (JSON parse error)**: log a warning to `stderr` and fall back to defaults. Unlike the paid build, the Lite does **not** attempt DB-based recovery — Redis may already be empty (TTL-expired). A fresh UUIDv4 pair is generated and written.
   - Apply `N3MC_REDIS_URL` env-var override (takes precedence over the file).
   - If any field is missing, fill with defaults and persist.

2. **Redis connect & ping**:
   - Build a client from `redis_url`.
   - `PING`. **If it fails**: log a warning pointing the user at `docker run -p 6379:6379 redis/redis-stack-server:latest` and continue with a non-functional client. Every subsequent tool call returns an error with the same hint. The server stays up — the client can hot-fix Redis without restarting the MCP.

3. **Ensure RediSearch index** (`ensure_index()`):
   - `FT.CREATE n3mc_idx ON HASH PREFIX 1 mem: SCHEMA ...` as per [§3.5](#35-data-layout).
   - Catch `ResponseError` whose message contains `"already exists"`; re-raise any other error.
   - Idempotent: safe to call on every boot.

4. **Preload embedding model** (`get_model()`):
   - Load `intfloat/e5-base-v2` into memory so the first tool call is not slowed by the one-time model load.
   - **Non-fatal**: if the model fails to load (e.g. offline, HF cache absent), log a warning and continue. The model will be retried lazily on first `save_memory` / `search_memory`.

Steps 1 and 3 must complete before the server accepts tool calls. Steps 2 and 4 are best-effort — an unreachable Redis does not stop the process but disables the tools until Redis becomes reachable again.

### 3.10 Repair

The `repair_memory` tool in the Lite build is a **thin idempotent operation**: it calls `ensure_index()` again. There are no migration markers, no FTS rebuild, no re-embedding loop — Redis records that exist are already indexed by the RediSearch side-channel, and expired records are simply gone.

Return shape: `{"status": "ok", "message": "index ensured"}`, or `{"status": "error", "message": "<detail>"}` on failure.

This is a deliberate simplification versus the paid build (which runs FTS punctuation migration, vec model-version migration, and an unindexed-row repair loop). The Lite has nothing to migrate because the oldest record is at most 7 d old.

---

## 4. MCP Protocol Surface

### 4.1 Transport

stdio. The server reads JSON-RPC lines from `stdin` and writes responses to `stdout`. Logs go to `stderr`. On Windows, `stdin`/`stdout`/`stderr` are reconfigured to UTF-8 at startup.

### 4.2 `initialize` response

The server advertises:
- `protocolVersion: "2024-11-05"`
- `serverInfo: { name: "n3memorycore-lite", version: "1.1.0" }`
- `capabilities.tools` with `listChanged: false`
- `instructions:` — a multi-line string delivering behavioral guidance (see [§5](#5-behavioral-instructions-auto-save-strategy)). **The Lite instruction text explicitly tells the LLM that memory expires after 7 days.**

### 4.3 Tools

Five tools are exposed via `tools/list` (same names as the paid build):

| Name            | Inputs                                    | Behavior                                                              |
| --------------- | ----------------------------------------- | --------------------------------------------------------------------- |
| `search_memory` | `query: string, limit?: int`              | Hybrid (vector + BM25) search, time-decayed ranking. Returns markdown. |
| `save_memory`   | `content: string, agent_name?: string`      | Exact + near-duplicate dedup, then HSET + EXPIRE. Returns JSON status including `ttl_seconds`. |
| `list_memories` | `limit?: int (default 20)`                | Most-recent entries, newest first. Returns markdown.                   |
| `delete_memory` | `id: string`                              | `DEL mem:<uuid>` + `DEL mem:sha:<sha1>` atomically.                    |
| `repair_memory` | —                                         | `ensure_index()`; see [§3.10](#310-repair).                            |

All tool responses are a single `TextContent` element. `save_memory` / `delete_memory` / `repair_memory` return JSON strings for easy parsing; `search_memory` / `list_memories` return human-readable markdown.

### 4.4 Error Handling

Tool exceptions are caught in the dispatch layer and returned as `TextContent` with a leading `"Error: "` prefix. The server never crashes the stdio loop due to a tool-level exception. If Redis is unreachable when a tool is called, the dispatcher returns a "start Redis Stack" hint instead of invoking the tool.

---

## 5. Behavioral Instructions (Auto-Save Strategy)

Because MCP has no equivalent of Claude Code's `UserPromptSubmit` / `Stop` hooks, the auto-save behavior is expressed as **natural-language instructions** returned in the `initialize` response. The connected LLM reads them as system guidance.

The instructions require the LLM to:

1. **Search first** — call `search_memory` at the start of every user turn with a concise query reflecting the user's intent.
2. **Save after each exchange** — call `save_memory` after a meaningful response, with paraphrased intent and key conclusions (50–200 chars each). **Note**: the Lite text explicitly reminds the LLM that entries vanish after 7 d.
3. **Extract from long pastes** — split user-pasted text into discrete facts, one `save_memory` per fact.
4. **Skip noise** — do not save greetings, clarifying questions, or mechanical acknowledgements.
5. **Respect explicit requests** — honor "don't save this" and "forget that" (use `delete_memory`).

The full text is in [`n3mc_mcp/instructions.py`](./n3mc_mcp/instructions.py).

---

## 6. Configuration

On first run, `config.json` is auto-generated in the data directory with random UUIDv4 values for `owner_id` and `local_id`.

Complete schema (missing fields auto-filled with defaults below):

```json
{
  "owner_id":               "<UUIDv4 auto-generated>",
  "local_id":               "<UUIDv4 auto-generated>",
  "redis_url":              "redis://localhost:6379/0",
  "ttl_seconds":            604800,
  "dedup_threshold":        0.95,
  "half_life_days":         3,
  "bm25_min_threshold":     0.1,
  "search_result_limit":    20,
  "context_char_limit":     3000,
  "min_score":              0.2,
  "search_query_max_chars": 2000
}
```

- `redis_url` — connection URL. `N3MC_REDIS_URL` env var overrides this field.
- `ttl_seconds` — TTL applied to every new memory and its sha-guard (default 7 d). Lowering it is fine; raising it far beyond a week defeats the purpose of the Lite and will be flagged during review.
- `search_result_limit` — max results returned by `search_memory`.
- `context_char_limit` — reserved for client-side truncation by downstream tools; not used internally.
- `min_score` — excludes results with score below this value (default `0.2`). Set to `0.0` to disable.
- `search_query_max_chars` — max characters used from a query (default `2000`; embedding model caps at ~512 tokens).

> **Multi-account on a single PC**: each OS user runs the server under their own `config.json` by default. To share a Redis across accounts, set the same `redis_url` in both configs — entries are segregated via the `owner_id` TAG filter.

---

## 7. Data Location

By default, only `config.json` lives on disk:

| OS      | Path                                                        |
| ------- | ----------------------------------------------------------- |
| Windows | `%LOCALAPPDATA%\n3memorycore-lite\`                        |
| macOS   | `~/Library/Application Support/n3memorycore-lite/`         |
| Linux   | `~/.local/share/n3memorycore-lite/`                        |

Files inside the data directory:
- `config.json` — configuration (the only on-disk artifact)

Override via the environment variable `N3MC_DATA_DIR` (absolute path). Redis state lives wherever the Redis container stores it (by default, an anonymous Docker volume that vanishes with `docker rm -f redis-stack`).

---

## 8. MCP Client Configuration

### Claude Desktop

`%APPDATA%\Claude\claude_desktop_config.json` (Windows) or
`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

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

Three equivalent paths are supported. Pick one; do not combine.

**(a) Plugin marketplace (recommended — no manual config file)**

```
/plugin marketplace add NeuralNexusNote/n3mcmcp-lite
/plugin install n3memorycore-lite@neuralnexusnote
```

The plugin ships a `plugin.json` that launches the server via `uvx --from n3memorycore-mcp-lite n3mc-mcp-lite`. Requires `uv` on PATH.

**(b) Project-local `.mcp.json` (manual, when cloning the repo or pip-installing)**

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

**(c) Project-local `.mcp.json` via uvx (no prior install needed)**

```json
{
  "mcpServers": {
    "n3memorycore-lite": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "n3memorycore-mcp-lite", "n3mc-mcp-lite"]
    }
  }
}
```

Restart the client after editing the config. Ensure Redis Stack is running *before* the client starts the server — otherwise the first tool call returns the "start Redis Stack" hint.

---

## 9. Testing

```bash
# 1. Start Redis Stack
docker run -d --name redis-stack -p 6379:6379 redis/redis-stack-server:latest

# 2. Install with dev deps and run pytest
pip install -e ".[dev]"
pytest tests/ -q
```

The test suite covers:
- `tests/test_database.py` — RediSearch index, CRUD, TTL, dedup, BM25, KNN, serialization.
- `tests/test_processor.py` — cosine sim (from cosine distance), time decay, BM25 normalization, purification, embeddings.
- `tests/test_server.py` — MCP tool dispatch end-to-end against an isolated `config.json` and a flushed Redis DB index 0.

Tests auto-skip (not fail) if Redis Stack is not reachable at `N3MC_REDIS_TEST_URL` (default `redis://localhost:6379/0`).

> **⚠️ Destructive test DB**: RediSearch can only create indexes on DB 0 (`Cannot create index on db != 0`), so the test suite FLUSHDBs DB 0 before and after every test. Do **not** point `N3MC_REDIS_TEST_URL` at a Redis instance that holds data you care about — run a dedicated container for testing.

---

## Appendix A: Recommended Review Workflow

After an AI regenerates the implementation from this spec, review it in this order:

1. **Data flow trace** — ask the AI to read the code and trace the end-to-end path from a `save_memory` tool call to the Redis pipeline's `EXECUTE`, and from a `search_memory` call back to the tool response. Confirm no silent data loss, and confirm TTL is set on every write.
2. **Spec ↔ code comparison** — walk each tool (§4.3) one-by-one, comparing the input schema and behavior in this document to the implementation.
3. **TTL test** — save an entry with a short `ttl_seconds` (e.g. 5) via a direct config override, wait, and confirm both `mem:<uuid>` and `mem:sha:<sha1>` are gone (proves §3.3 and §3.4 compliance).
4. **Cross-session test (within 7 d)** — save in session 1, restart the MCP server (not Redis), search in session 2. Confirm the saved entry is retrievable.
5. **Dedup test** — save the same content twice; confirm the second call returns `status: "duplicate"`. Save near-paraphrases; confirm near-duplicate rejection.
6. **Redis-down test** — stop Redis, call any tool, confirm the server returns the "start Redis Stack" hint without crashing. Restart Redis, confirm tools work again without restarting the MCP process.

These steps are operated by the human reviewer, not automated tests.

---

## Appendix B: Optional Extensions (not shipped)

The Lite build intentionally stops at the hybrid + time-decay ranker described in §3.6. The following extensions are **not part of the shipped spec** — they are sketched here so a future AI or contributor has a clean starting map when the user decides to try them. None of them are required for the Lite build to behave correctly; each is a precision-vs-latency trade.

- **Cross-encoder reranker** — after `hybrid_search` returns the top-N candidates, rerank them with a small cross-encoder (e.g. `cross-encoder/ms-marco-MiniLM-L-12-v2`, ~130 MB, or `BAAI/bge-reranker-base`, ~278 MB). Expect **+100–300 ms CPU latency** per `search_memory` call on a modern laptop (top-50 rerank), in exchange for roughly **+1 precision point** on paraphrase-heavy queries. Drop-in point: between the fused-score sort and the `min_score` filter in `processor.hybrid_search`. Keep the existing score as a fallback when the reranker is disabled.
- **Chunking on save** — when `save_memory` receives a body longer than ~2000 characters, split it into ~500-character sliding windows (with ~100-char overlap) and store each chunk as its own `mem:<uuid>` entry, all sharing a `source_id` field so `search_memory` can re-group hits. Adds write amplification but materially improves recall on long pastes (specs, articles, logs). Today the Lite build relies on the behavioral instruction *"extract each key fact as a separate short sentence"* instead — chunking would make that instruction optional.
- **HyDE (Hypothetical Document Embeddings)** — before embedding the user's query, ask a small LLM to synthesize a hypothetical *answer* to the query, then embed that answer instead of (or in addition to) the raw query. Helps when queries are short/vague and memories are long/specific. Needs an LLM hop per search, so it is a poor fit for the Lite build's "no external API calls" promise unless a local model is already available.
- **Japanese morphological analysis** — RediSearch's default tokenizer splits on whitespace and punctuation, so Japanese text (which has no inter-word spaces) collapses into roughly one BM25 token per sentence and keyword relevance degenerates to something close to "exact substring match." Pre-tokenize the `text` body at save time with a morphological analyzer — candidates: `fugashi` + `unidic-lite` (MeCab-based, ~50 MB), `SudachiPy` + `sudachidict-core` (~70 MB, multi-granularity A/B/C modes), or pure-Python `Janome` when binary dependencies are a problem — store the space-joined surface forms in a parallel `text_tokens` TEXT field, and point BM25 search at that field. Vector search is unaffected (the e5 embedding model handles Japanese natively) and the raw `text` field stays untouched for display. Expected cost: +5–20 ms per `save_memory` call; precision gain on Japanese queries is material, not marginal. For a mixed-language deployment this is closer to a requirement than a nice-to-have; English-only deployments can skip it safely.

All four extensions are additive — none of them require changes to the Redis schema's existing fields or the TTL/dedup contracts (the Japanese tokenizer only **adds** a parallel field). A future implementer should treat them as separate feature flags, default-off, and benchmark each independently against the baseline ranker.
