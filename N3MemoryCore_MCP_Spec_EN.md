# N3MemoryCore MCP v1.6.0 [Volatile Memory over MCP]
> A NeuralNexusNote™ product — **Lite (ephemeral) build**

> **What is this variant?** The Lite build is the free, marketplace-targeted edition of N3MemoryCore MCP. Storage is **Redis Stack (RediSearch)**, every entry carries a **7-day TTL**, and nothing persists beyond that window. Think of it as working memory — the **Pro build (coming soon)** will use SQLite + sqlite-vec to store memories permanently.
>
> **Who is this for?** Users of any MCP-compatible LLM client (Claude Desktop, Claude Code, and others) who want searchable, short-lived memory shared across sessions within a 7-day window.
>
> **Transport**: Model Context Protocol over stdio (JSON-RPC). Tested on Windows 11 and Ubuntu with Python 3.12 and Redis Stack 7.x.

## ⚠️ Disclaimer & Distribution Terms

This software and specification are provided **"AS-IS"** without warranty of any kind.

- **No Support**: The author provides no bug fixes, answers to questions, or guarantees of operation.
- **No Warranty / No Liability**: The author shall not be liable for any damages arising from use of this software, including but not limited to data loss, business interruption, or third-party claims.
- **Use at Your Own Risk**: You assume full responsibility for your use of this software.
- **Right to Change**: The author may modify or discontinue this software at any time without notice.

By using this software, you agree to the terms above.

- **License**: Apache License 2.0. See the `LICENSE` file for details.

> **Removal (Uninstall)**: `pip uninstall n3memorycore-mcp-lite` removes the package. Stop and delete the Redis container (`docker rm -f redis-stack`) to erase all stored memories instantly. Delete `${N3MC_DATA_DIR}` (or the platform default data dir) to remove `config.json`. Also remove the `n3mc-workingmemory` entry from your MCP client's config file.
>
> **Backup?** The Lite build is **not designed to be backed up**. Entries vanish on a 7d rolling window; if you need durable memory, the **Pro build (coming soon)** will offer persistent storage.

> **For implementation questions**: While the author cannot be contacted for support, you can load this specification into Claude and ask questions directly — Claude can assist with implementation and customization.

---

## Lite: Volatile Memory

This section captures the tradeoffs unique to the Lite build; the rest of the specification deliberately reuses the Pro-build structure so AI-driven regeneration stays simple.

| Property               | Lite (this spec)                         | Pro (coming soon — separate spec)      |
| ---------------------- | ----------------------------------------- | -------------------------------------- |
| Storage engine         | Redis Stack (RediSearch module)           | SQLite + sqlite-vec (local file)       |
| Durability             | **7 d TTL per entry**, ephemeral         | Permanent, disk-persistent             |
| Disk footprint         | Only `config.json` (< 1 KB)               | `n3memory.db` grows with history       |
| External dependency    | User-run Redis Stack container            | None (self-contained)                  |
| `time_decay` relevance | Meaningful (3-day half-life; fresh=1.0, 7d≈0.20) | Meaningful (90-day half-life) |
| Re-indexing / repair   | `FT.CREATE` is idempotent; no migrations  | Schema + model migration markers       |
| Intended use           | Short-term projects, working memory, marketplace | Ongoing projects (coming soon)   |

**Volatility contract:**
- Every write to Redis sets a TTL equal to `ttl_seconds` (default 604 800 = 7 d).
- Both the primary record (`mem:<uuid>`) and its exact-duplicate guard key (`mem:sha:<sha1>`) share the same TTL and expire together.
- Expiration is delegated to Redis; no background cleanup job runs.
- Restarting the Redis container with its volume removed wipes all memory immediately.

**No cross-session guarantee beyond 7 d.** Unlike the forthcoming Pro build, the Lite spec deliberately forbids any "persistence hack" — do not add RDB snapshots, AOF rewrite, or external dumps to circumvent the TTL. If durability is needed, wait for the Pro build (coming soon).

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

1. Start Redis Stack:
   ```bash
   # First time only (creates the container):
   docker run -d --name redis-stack -p 6379:6379 redis/redis-stack-server:latest

   # Every subsequent session (container already exists):
   docker start redis-stack
   ```
   Re-running the `docker run` command after the container exists fails with `Conflict. The container name "/redis-stack" is already in use`. Use `docker start redis-stack` thereafter.

   **Persistence is forbidden — enforced at server startup, not via
   docker args.** The MCP server issues `CONFIG SET appendonly no` and
   `CONFIG SET save ""` on every connect (§3.4 `_enforce_ephemeral`),
   so any manual re-enable between sessions is reverted on the next
   Lite run. Earlier iterations of this spec put `--appendonly no
   --save ""` on the `docker run` line as a belt-and-suspenders, but
   the empty-string argument for `--save` is mangled by Windows
   PowerShell and cmd.exe quoting (it has left containers with a
   broken entrypoint in practice), so the docker args have been
   removed and server-side enforcement is the sole source of truth.
   Rationale for the ban itself: ephemerality is the product boundary
   that separates the free Lite build from the paid persistent
   N3MemoryCore — Lite is "a rolling 7-day scratchpad that truly
   forgets on restart", not "a durable store with a TTL". If the user
   wants continuous memory, they upgrade. `_enforce_ephemeral` makes
   it *mechanically impossible* to turn Lite into a persistent store
   by accident, regardless of the user's shell or docker flags.
2. Install the package (choose one):
   - **pip** (global or venv):
     ```bash
     pip install n3memorycore-mcp-lite
     ```
   - **uvx** (zero-install, isolated env — requires [`uv`](https://docs.astral.sh/uv/)):
     ```bash
     uvx --from n3memorycore-mcp-lite n3mc-workingmemory
     ```
   - **Claude Code plugin marketplace** (no pip/uvx command required — the plugin configures `uvx` launch for you, but `uv` must still be on PATH):
     ```
     /plugin marketplace add NeuralNexusNote/n3mcmcp-lite
     /plugin install n3mc-workingmemory@neuralnexusnote
     ```
3. Register the server in your MCP client's config (see [§8](#8-mcp-client-configuration)). Skip this step when installing via the plugin marketplace — the plugin registers the server automatically.
4. Restart the client. The first tool call may take 30–60 seconds as the ~400 MB embedding model is downloaded and loaded.

### Data Backup

Not applicable. See the [Lite: Volatile Memory](#lite-volatile-memory) section — the build is explicitly ephemeral. `config.json` (containing `owner_id` / `local_id` UUIDs) is the only on-disk artifact and can be copied if the user wants to keep the same owner identity across re-installs.

---

## 1. Vision

Provide a no-commitment memory endpoint for MCP clients: hybrid search (vector + RediSearch BM25), mathematically sound ranking, 7-day automatic garbage collection. The MCP server delivers behavioral instructions so the connected LLM auto-searches at the start of each turn and auto-saves after each meaningful exchange — without requiring client-side hooks.

The Lite exists to demonstrate the N3MemoryCore MCP surface on the Claude Marketplace with zero risk to the user's disk; the forthcoming **Pro build (coming soon)** will swap the storage layer from Redis to SQLite + sqlite-vec while preserving the MCP surface.

> **⚠️ Python check**: Before installing, run `python --version` to verify Python 3.10+ is available.

> **⚠️ First-run download**: `sentence-transformers` downloads the `e5-base-v2` model (~440 MB) on first tool use. The server will appear unresponsive during this time — this is expected. Subsequent starts complete in seconds once cached.

> **Important: Character Limits (Design Constraints)**
> - Auto-save per entry: **50–200 characters recommended** (one fact per entry).
> - Search query: **2,000 characters** (configurable via `search_query_max_chars`).
> - Vector search: Only the first **~2,000 characters** of any record are semantically searchable (embedding model limit: 512 tokens). Beyond this, content is stored and BM25-searchable but invisible to vector similarity.
> - **Large text handling (two modes)**:
>   - **Fact extraction (preferred for fact-based memory)**: read and understand the content, extract each key fact as a separate short sentence (~50–200 chars), and call `save_memory` once per fact. Maximises search precision, access-frequency boost, and per-fact importance tuning.
>   - **Verbatim recall (whole-document save)**: when the user wants the exact original text back later ("save this setting doc, I want to see the same thing again"), pass the long body in a single `save_memory` call. The server auto-splits content longer than `chunk_threshold` (default 400 chars) into overlapping chunks while also persisting the original full text as a parent document (`doc:<uuid>`). A later `search_memory` that hits any chunk reconstructs the full verbatim content of the parent (see [§3.11](#311-verbatim-recall-parent-document--chunks-pattern)).

---

## 2. Package Structure

```
n3memorycore-mcp-lite/
├── pyproject.toml                  # Package metadata, entry point 'n3mc-workingmemory' (+ deprecated alias 'n3mc-mcp-lite')
├── n3mc_mcp/                       # Python package
│   ├── __init__.py                 # Version marker
│   ├── __main__.py                 # Entry point: python -m n3mc_mcp
│   ├── server.py                   # MCP server definition + 6 tools
│   ├── instructions.py             # Behavioral instructions delivered at initialize
│   ├── database.py                 # Redis layer: index, CRUD, TTL, dedup
│   ├── processor.py                # Embedding, ranking, CJK tokenization, reranker
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
| `owner_id`   | `config.json`   | First startup (UUIDv4)             | **Owner**            | Identifies whose data this is — stored as a TAG field and returned in results; filtering is done in Python (see §3.12) |
| `local_id` (agent_id)   | `config.json`   | First startup (UUIDv4)             | **Agent / install**  | UUIDv4 identifier for the install. Stored for compatibility; not used in Lite ranking.            |
| `session_id` | In-memory       | Per server process startup (UUIDv4) | **Server process**   | Identifies which process wrote the record. Override per call via the `save_memory` / `search_memory` argument or the `N3MC_SESSION_ID` env var. **Lite applies the same `b_session` ranking as Pro** (match=1.0, mismatch=0.6 by default). Pinning a consistent `session_id` per project surfaces that project's memories above unrelated cross-project rows in the same Redis instance. Also serves as the filter key for `delete_memories_by_session`. |
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
- Unconditional TTL extension on read (`TOUCH` / `EXPIRE` applied indiscriminately on `search_memory`). Note: the `ttl_refresh_on_search` setting (§6, default `true`) is an **explicit design exception** to this rule — it resets TTL only for the top-K search hits and only up to `ttl_seconds`, never extending memory beyond the configured maximum lifetime.
- Write buffering / deferred pipelines beyond the single save call.

**Reason**: the Lite build's differentiation is explicit volatility; circumventing it erodes the product distinction.

### 3.5 Data Layout

```
mem:<uuid>                  HASH (memory record OR chunk)
    id              string      UUIDv7 (same as the key suffix)
    content         string      original text verbatim (chunk text if chunked)
    content_ngram   string      CJK bigram expansion (BM25 side channel)
    timestamp       string      ISO 8601 UTC
    timestamp_epoch number      unix seconds (SORTABLE)
    owner_id        string      TAG
    local_id        string      TAG
    agent_name        string      TAG
    session_id      string      TAG
    importance      number      0.5–2.0 (from save_memory; default 1.0)
    access_count    number      search-hit counter (feeds frequency boost)
    parent_id       string      TAG — parent doc id when this row is a chunk
                                (empty string for standalone memories)
    embedding       bytes       FLOAT32 * 768 little-endian
    TTL                         ttl_seconds (default 604 800)

mem:sha:<sha1>              STRING
    value = the associated mem id (standalone rows only;
            chunks do NOT get a per-chunk sha guard)
    TTL = same as mem:<uuid>

doc:<uuid>                  HASH (parent document — verbatim whole-body store;
                                   NOT in the RediSearch index)
    id              string      UUIDv7
    content         string      full verbatim body
    timestamp       string      ISO 8601 UTC
    timestamp_epoch number      unix seconds
    owner_id        string
    local_id        string
    agent_name        string
    session_id      string
    chunk_count     number      number of chunks emitted for this doc
    TTL                         ttl_seconds

docsha:<sha1>               STRING
    value = associated doc id (parent-level exact-duplicate guard)
    TTL = same as doc:<uuid>

n3mc_idx                    RediSearch index, ON HASH PREFIX 1 mem:
    SCHEMA:
        content         TEXT weight 1.0
        content_ngram   TEXT weight 0.8
        timestamp_epoch NUMERIC SORTABLE
        owner_id        TAG
        local_id        TAG
        agent_name        TAG
        session_id      TAG
        importance      NUMERIC
        access_count    NUMERIC
        parent_id       TAG
        embedding       VECTOR FLAT 6 TYPE FLOAT32 DIM 768 DISTANCE_METRIC COSINE
```

- **Primary Key**: UUIDv7 (time-sortable; generated at insert time). The reference implementation uses `uuid_utils.uuid7`.
- **Parent docs are NOT indexed**: `doc:` / `docsha:` keys live outside the `PREFIX 1 mem:` index prefix. Search always hits chunks (`mem:*`) first; the parent is looked up in a post-retrieval step (see [§3.11](#311-verbatim-recall-parent-document--chunks-pattern)).
- **Delete semantics**: `delete_memory` branches on the id:
  - If a `doc:<uuid>` key exists with that id → delete the parent, `docsha:<sha1>`, and every chunk whose `parent_id` matches, in a single pipeline (cascade).
  - Otherwise → delete `mem:<uuid>` and its sibling `mem:sha:<sha1>` as before.

### 3.6 Ranking Formula

Identical to the forthcoming Pro build:

```
Final Score = (cos_sim × 0.7 + keyword_relevance × 0.3) × time_decay × b_local × b_session
```

Where `b_local` is the **importance coefficient**:

```
b_local = clamp(0.5, 2.0, stored_importance + access_boost)
access_boost = min(access_count_max_boost, access_count × access_count_weight)
```

- `stored_importance`: provided at `save_memory` time (default `1.0`, range `0.5–2.0`).
- `access_boost`: **automatic CPU-only frequency boost**. Every `search_memory` call increments `access_count` by 1 for each memory returned in the top `ttl_refresh_top_k` hits. On subsequent queries that memory receives an additive boost of `access_count × access_count_weight` (default `0.02`), capped at `access_count_max_boost` (default `0.5`). This creates a self-adjusting "frequently-used memories rank higher" loop with zero LLM involvement.

Set `access_count_enabled: false` in config to disable the boost (the formula falls back to `stored_importance` only).

`b_session` is the **session-match coefficient** (same contract as Pro):

```
b_session = b_session_match     if  row.session_id == effective_session
          = b_session_mismatch  otherwise
```

- `b_session_match`: default `1.0`. Multiplied into rows whose stored `session_id` matches the request's `effective_session` (resolved via per-call argument → `N3MC_SESSION_ID` env var → per-process startup UUID).
- `b_session_mismatch`: default `0.6`. Pushes rows from other projects sharing the same Redis instance below the current session's results.
- This is the primary signal for ChatLink-style "one chat = one session_id" workflows: surfacing the current chat's memories above unrelated cross-project noise. Pass the same `session_id` to both `save_memory` and `search_memory` to make this work.

When `effective_session` is empty, the match check always fails and every row receives `b_session_mismatch` — symmetrically, so no row gains an advantage. To explicitly disable the bias, set both `b_session_match` and `b_session_mismatch` to `1.0`.

**cos_sim** — **derived directly from RediSearch's cosine distance**:

$$cos\_sim = \max(0,\ \min(1,\ 1.0 - cosine\_distance))$$

RediSearch returns `cosine_distance ∈ [0, 2]` for normalized vectors. Clamping to `[0, 1]` discards the "opposite direction" half-space, which we treat as irrelevant for memory retrieval.

**keyword_relevance** — normalize RediSearch BM25 scores to `[0.0, 1.0]`:

1. If `|bm25_score| < bm25_min_threshold` (default `0.1`), set to `0.0`.
2. Otherwise: `|bm25_score| / max(1.0, max_|bm25_score| in result set)`.

(RediSearch BM25 scores are non-negative, but the `abs()` keeps the algorithm identical to the forthcoming Pro build where FTS5 produces negative scores.)

**time_decay**:

$$time\_decay = 2^{-\frac{days\_elapsed}{half\_life\_days}}$$

Default `half_life_days = 3` — deliberately shorter than the 7-day TTL so that `time_decay` is actually informative in the Lite build: a fresh entry scores 1.0, a 3-day-old one exactly 0.5, and a 7-day-old (near-expiry) one ≈ 0.20. This pushes recent context ahead in ranking. This is a Lite-specific tuning; the forthcoming Pro build will keep a 90-day half-life to match its permanent horizon.

**Lightweight lexical rerank** (post-fusion, pre-TTL-refresh):

After the hybrid score above is computed, an optional CPU-only rerank pass boosts each candidate's score by:

- `coverage × rerank_weight` (default weight `0.3`), where `coverage` is the fraction of query tokens that appear in the content. Tokenization is whitespace-split **augmented with CJK bigram tokens** so that Japanese/Chinese queries contribute a real coverage signal (without bigrams, `.split()` collapses a pure-CJK query into a single token and coverage degenerates to a binary whole-query-substring match).
- `rerank_phrase_weight` (default `0.2`) added when the entire query string appears as a substring of the content (case-insensitive).

Parent-document resolution happens **before** lexical rerank: chunk hits are expanded to their full `doc:<parent_id>` body first, then rerank operates on the full verbatim content. This ensures that a query phrase appearing in a non-matching chunk of the same document still boosts the parent's rank correctly (see [§3.11](#311-verbatim-recall-parent-document--chunks-pattern)).

Set `lexical_rerank_enabled: false` in config to skip this pass (candidates are then sorted by the hybrid score only).

### 3.7 Text Tokenization & Punctuation Handling

**Tokenizer**: RediSearch's built-in tokenizer (whitespace + punctuation split, case-folded). The Porter stemmer used by the forthcoming Pro build is **not** available here.

**CJK bigram expansion**: Japanese and Chinese text lacks inter-word spaces, so the raw RediSearch tokenizer collapses whole sentences into a single BM25 token and keyword relevance degenerates. To compensate, at save time the server expands every contiguous CJK run in `content` into **overlapping bigrams** (e.g. "記憶装置" → "記憶 憶装 装置") and stores the result in a parallel `content_ngram` TEXT field. BM25 queries apply the same expansion and run `@content:(...) | @content_ngram:(...)`, giving working Japanese partial-match retrieval without touching vector search (the e5 embedding model handles Japanese natively).

**Query cleaning** — apply `strip_fts_punctuation` to the user's query string *before* submitting it to RediSearch, run CJK bigram expansion, then backslash-escape remaining RediSearch special characters. Store raw (verbatim) `content` in the hash (RediSearch tokenizes on the fly for the `content` field; `content_ngram` holds the pre-expanded form).

```python
_PUNCT_STRIP_RE = re.compile(r'[,.<>\{\}\[\]"\':;!@#\$%\^&\*\(\)\-\+=~\|\\/?`]')
_FTS_SPECIAL_RE = re.compile(r'([,.<>\{\}\[\]"\':;!@#\$%\^&\*\(\)\-\+=~\|\\/?])')
```

**Empty-query rule**: if the cleaned query is empty after stripping, skip keyword search and rank using vector search only.

### 3.8 Duplicate Rejection

`save_memory` branches on whether the input body exceeds `chunk_threshold` (default 400 chars).

**(A) Single-chunk path** (body ≤ `chunk_threshold`): reject duplicates in this order.

1. **Exact dedup (O(1))** — `EXISTS mem:sha:<sha1(content)>`. If the key exists, return `{"status": "duplicate", "saved": false}`.
2. **Near-duplicate (semantic) dedup** — compute the embedding, run KNN=5 against `@embedding` (no `owner_id` TAG filter in the FT.SEARCH query — see §3.12), check `owner_id` in Python on returned results, convert `cosine_distance` → `cos_sim`. If `cos_sim >= dedup_threshold` (default `0.95`), return `{"status": "near_duplicate", "saved": false, "similarity": <value>}`.

Only if both checks pass, proceed with the HSET + EXPIRE + sha1-guard pipeline.

**(B) Multi-chunk path** (body > `chunk_threshold`): dedup runs at the **parent-document level** against the full body.

1. **Parent-level exact dedup (O(1))** — `EXISTS docsha:<sha1(full_text)>`. If the key exists, return `{"status": "duplicate", "saved": false, "parent_id": "<existing>"}`.
2. **Parent-level near-duplicate (semantic) dedup** — embed the full body (e5-base-v2 truncates to ~512 tokens, which is enough to fingerprint the document's opening), run the same KNN=5 near-dedup used by (A) against the indexed chunk space. If a prior chunk's `cos_sim >= dedup_threshold` (default `0.95`) for the same `owner_id`, return `{"status": "near_duplicate", "saved": false, "similarity": <value>}`. This makes long-content dedup semantics symmetric with short-content (A).
3. Chunks themselves are **not** given per-chunk sha guards and bypass per-chunk near-duplicate checks. Reason: sliding-window chunks are overlapping by design and would otherwise reject each other.

If both parent-level checks pass, a single `save_memory` call writes, in order:
- `doc:<parent_id>` via HSET + EXPIRE + `docsha:<sha1(full_text)>` guard (one pipeline)
- All chunk HSET + EXPIRE commands batched in a **single pipeline** (no per-chunk sha guard; each chunk's `parent_id` field is set to the parent id)

### 3.9 Startup Sequence & Self-Recovery

The server's `_startup()` runs these steps in order, **before** the stdio loop begins accepting requests:

1. **Load config** (`load_config()`):
   - Read `config.json` from the data directory.
   - **If the file is corrupt (JSON parse error)**: log a warning to `stderr` and fall back to defaults. Unlike the forthcoming Pro build, the Lite does **not** attempt DB-based recovery — Redis may already be empty (TTL-expired). A fresh UUIDv4 pair is generated and written.
   - Apply `N3MC_REDIS_URL` env-var override (takes precedence over the file).
   - If any field is missing, fill with defaults and persist.

2. **Redis connect & ping**:
   - Build a client from `redis_url`.
   - `PING`. **If it fails**: log a warning to `stderr` with both startup hints (first-time: `docker run -d --name redis-stack -p 6379:6379 redis/redis-stack-server:latest`; restart: `docker start redis-stack`) and continue with a non-functional client. Every subsequent tool call returns an **explicit error** with the same hints — `save_memory`, `delete_memory`, and `repair_memory` return `{"status": "error", ...}` JSON; `search_memory` and `list_memories` surface a `TextContent` starting with `Error:` that includes the recovery hint. This matches the [§5](#5-behavioral-instructions-auto-save-strategy) contract that tells the AI client to announce backend failures instead of silently falling back to "no memory found". The server stays up — the client can hot-fix Redis without restarting the MCP.

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

This is a deliberate simplification versus the forthcoming Pro build (which will run FTS punctuation migration, vec model-version migration, and an unindexed-row repair loop). The Lite has nothing to migrate because the oldest record is at most 7 d old.

### 3.11 Verbatim Recall (Parent-Document + Chunks Pattern)

When a `save_memory` body exceeds `chunk_threshold` (default 400 chars), the server automatically:

1. Splits the body into sliding windows of `chunk_threshold` chars with `chunk_overlap` overlap (default 100).
2. Allocates a fresh `parent_id` (UUIDv7) and writes `doc:<parent_id>` with the **full verbatim body** (no truncation, no code-block stripping — the input is stored byte-for-byte). Sets `docsha:<sha1(full_text)>` as the parent-level exact-duplicate guard.
3. Batches all chunk HSET + EXPIRE commands in a **single pipeline**, setting each `mem:<chunk_id>`'s `parent_id` TAG field to the parent id. Per-chunk sha guards and per-chunk near-duplicate checks are skipped (parent-level near-dedup handles this — see [§3.8 (B)](#38-duplicate-rejection)).

`search_memory` integration:

- `hybrid_search` scores and ranks against the chunk index as usual and includes each chunk's `parent_id` in its result dicts.
- The dispatcher post-processes hits **before** lexical rerank: for each result whose `parent_id` is non-empty,
  - If the same `parent_id` has already been emitted → drop as duplicate (keep the highest-scoring hit).
  - First occurrence → `HGET doc:<parent_id>` to fetch the full body and substitute it into the result, replacing the id with the parent id.
- The subsequent lexical rerank (token-coverage + phrase bonus) therefore sees the **full verbatim body** for parent hits, not just the matched chunk — a query phrase that appears in a non-matching chunk of the same document still boosts the parent's rank correctly.
- When `ttl_refresh_on_search` is enabled, TTL refresh is applied to the top-K **after** rerank: for each hit the underlying `mem:<chunk_id>` (or standalone `mem:<id>`) key is refreshed and its `access_count` is incremented; if the hit resolved to a parent document, the `doc:<parent_id>` key's TTL is also refreshed so verbatim recall stays alive alongside its chunks.
- Rendered output tags parent hits with `[doc×N]` (N = `chunk_count`) in the markdown.

`list_memories` integration:

- Issues a `*` (match-all) FT.SEARCH query; `owner_id` and `parent_id` are included in the RETURN fields. Python filters the result set to records whose `owner_id` matches and whose `parent_id` is an empty string (standalone memories only). The `-@parent_id:{*}` TAG filter is not used because UUID values in TAG queries cause parse errors in RediSearch (see §3.12).
- These are merged with parent docs fetched by `SCAN doc:*` (owner-filtered in Python) and sorted by timestamp desc.
- Parent rows render with a `[doc×N]` tag where N is `chunk_count`.

`delete_memory` integration:

- If the id resolves to a `doc:<uuid>` key, the server first attempts `FT.SEARCH @parent_id:{<id>}` to collect chunks. If that TAG query fails (UUID hyphen issue — §3.12), it falls back to `SCAN mem:*` and checks the `parent_id` field in Python. The parent, `docsha:`, and all collected chunks are then deleted in a single pipeline.
- Otherwise the usual single-memory delete applies.

**Design invariants**:
- Parent rows are intentionally excluded from the RediSearch index (kept outside `PREFIX 1 mem:`). Ranking therefore always operates on chunk bodies, so a long parent body never distorts time-decay or BM25 norms.
- `stored_importance` and `access_count` live on chunks, not parents. A parent is the "verbatim box" and carries no ranking state.
- As long as the parent is alive, a single chunk hit reconstructs the full body. When `ttl_refresh_on_search` is enabled (default `true`), every chunk hit that fetches the parent doc also refreshes the `doc:` key's TTL, so the parent and its chunks age together under normal use. Should the parent expire — e.g. with `ttl_refresh_on_search: false`, or if it was never searched during its initial 7-day window — orphaned chunks surface as their own short-text hits (a graceful degrade, not a regression).

**Use cases**: when the user wants to retrieve an exact original body later ("save this setting/spec/article so I can pull it verbatim"). For the split between this mode and fact-extraction, see [§1 "Large text handling (two modes)"](#1-vision).

### 3.12 UUID TAG Query Constraint & Python-side Owner Filtering

**Background**: RediSearch TAG field queries (`@field:{value}`) treat the hyphen character (`-`) as a special operator inside the `{...}` delimiters. Because every UUID (e.g. `041500aa-4b54-4f49-ab4c-82045865072c`) contains hyphens in every segment, injecting a UUID into a TAG query causes a parse error regardless of whether the hyphen is backslash-escaped (`\-`) or left bare. This behavior was confirmed on Redis Stack 7.x and affects both KNN hybrid queries and BM25 FT.SEARCH queries.

**Design decision**: Remove `owner_id` (and, where relevant, `parent_id`) from all FT.SEARCH query strings. Instead, include them as `RETURN` fields and filter in Python after the query returns.

**Affected methods and how each is handled**:

| Method | FT.SEARCH query | Python filtering |
|---|---|---|
| `_vector_search` | `*=>[KNN N @embedding $vec AS __dist]` — no owner filter; `owner_id` added to RETURN | Keep only records where `owner_id` matches |
| `_bm25_search` | `(@content:(...) \| @content_ngram:(...))` — no owner filter; `owner_id` added to RETURN | Keep only records where `owner_id` matches |
| `_near_dedup` | `*=>[KNN 5 @embedding $vec AS __dist]` — global fetch of 5 candidates; `owner_id` added to RETURN | Check `owner_id` match before applying cosine threshold |
| `list_memories` | `*` (match-all); `owner_id` and `parent_id` added to RETURN | Keep records where `owner_id` matches **and** `parent_id` is empty string |
| `delete_memory` (cascade) | Attempt `@parent_id:{<id>}` via FT.SEARCH first; on parse error fall back to `SCAN mem:*` | In fallback path, compare `parent_id` field value in Python |

**Performance note**: Fetching globally and filtering in Python incurs extra network transfer when multiple owner IDs share the same Redis instance. The Lite build assumes a single-user, single-install deployment, so this is not a practical concern. Should multi-tenant use arise, consider storing a separate hyphen-free derived field (e.g. `owner_id_tag = owner_id.replace("-", "")`) alongside the canonical `owner_id` and using that field for TAG queries.

**The TAG index schema is preserved**: The `owner_id` and `parent_id` TAG field declarations in §3.5 remain intact. If a future Redis Stack release resolves the UUID hyphen parse error, the Python-side filtering can be moved back into the FT.SEARCH queries without any schema migration.

### 3.13 Encoding Safety

Two encoding-safety layers run before any tool body executes. They mirror the
Free build's defenses (`n3mc_hook.py` stream reconfigure + `core/processor.py`
`sanitize_surrogates`) so a Lite deployment offers the same baseline reliability
on Windows-Japanese hosts.

**(1) stdio UTF-8 reconfigure** — at module import time of `n3mc_mcp.server`
(before any other import that might touch stdout/stderr), each of `sys.stdin`,
`sys.stdout`, `sys.stderr` is switched to `encoding="utf-8"` if the stream
implements `reconfigure()` (Python 3.7+). On Windows-Japanese hosts the default
console code page is cp932, which would otherwise mangle every non-ASCII byte
on the MCP JSON-RPC channel. POSIX systems are already UTF-8 by default, so the
call is a safe no-op there. The `hasattr(stream, "reconfigure")` guard
additionally protects against environments that have replaced the streams
with bare file objects (test harnesses, embedded interpreters).

```python
for _stream_name in ("stdin", "stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
```

**(2) Lone-surrogate sanitization** — every `save_memory.content` and
`search_memory.query` is passed through `sanitize_surrogates()` before any
`.encode("utf-8")` call. Lone UTF-16 surrogate halves (`U+D800`–`U+DFFF`)
appear when Windows subprocess pipes deliver UTF-8 bytes that Python's decoder
maps with `errors="surrogateescape"`. They round-trip through `json.loads` but
raise `UnicodeEncodeError` at SHA1 / Redis HSET / embedding time. Without the
guard the entire write is silently lost (the dispatcher catches the
exception, returns a generic `Error: ...` response, and the caller's content
never reaches Redis).

```python
_LONE_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")

def sanitize_surrogates(text):
    if isinstance(text, str):
        return _LONE_SURROGATE_RE.sub("", text)
    if isinstance(text, list):
        return [sanitize_surrogates(x) for x in text]
    if isinstance(text, dict):
        return {k: sanitize_surrogates(v) for k, v in text.items()}
    return text
```

The function is recursive over `dict` / `list` so JSON payloads with surrogates
buried inside (e.g. multimodal tool-call audit blobs) are cleaned in one pass.
Non-string scalars (`None`, `int`, `bytes`) pass through unchanged.

**Degenerate input contract**: if `save_memory.content` consists *entirely* of
surrogates, sanitization collapses it to the empty string and the regular
empty-content rejection path applies — `{"status":"error","saved":false,
"reason":"empty content"}`. This is a deterministic failure mode; the caller
sees an explicit refusal rather than a silent encoding crash.

**Pre-1.2.0 mojibake recovery is intentionally NOT ported** from Free. That
routine retroactively rewrote rows that earlier Free builds had decoded
through cp932; Lite has no historical data to retrofit because every entry
ages out within `ttl_seconds` (default 7 days). Adding a recovery routine
would only run on data that the user already accepted as ephemeral.

---

## 4. MCP Protocol Surface

### 4.1 Transport

stdio. The server reads JSON-RPC lines from `stdin` and writes responses to `stdout`. Logs go to `stderr`. On Windows, `stdin`/`stdout`/`stderr` are reconfigured to UTF-8 at startup (see [§3.13](#313-encoding-safety) for the full encoding-safety contract, including lone-surrogate sanitization on every tool input).

### 4.2 `initialize` response

The server advertises:
- `protocolVersion: "2024-11-05"`
- `serverInfo: { name: "n3mc-workingmemory", version: "1.6.0" }`
- `capabilities.tools` with `listChanged: false`
- `instructions:` — a multi-line string delivering behavioral guidance (see [§5](#5-behavioral-instructions-auto-save-strategy)). **The Lite instruction text explicitly tells the LLM that memory expires after 7 days.**

### 4.3 Tools

Six tools are exposed via `tools/list` (same names as the forthcoming Pro build, except `delete_memories_by_session` which is Lite-only — Pro will keep only the per-record `delete_memory` to minimize accidental-deletion risk on a persistent store):

| Name            | Inputs                                    | Behavior                                                              |
| --------------- | ----------------------------------------- | --------------------------------------------------------------------- |
| `search_memory` | `query: string, limit?: int, session_id?: string` | Hybrid (vector + BM25) search, time-decayed ranking with frequency boost and `b_session` match boost, lexical rerank. Chunk hits collapse to their parent document and render verbatim (see [§3.11](#311-verbatim-recall-parent-document--chunks-pattern)). The `session_id` argument feeds the **same `b_session` ranking as Pro** (match=1.0 / mismatch=0.6 by default), surfacing memories saved with the same `session_id` above unrelated rows. When omitted, the server default (`N3MC_SESSION_ID` env var → per-process UUIDv4) is used as the effective session. Returns markdown. |
| `save_memory`   | `content: string, agent_name?: string, owner_id?: string, importance?: number, session_id?: string` | Body ≤ `chunk_threshold`: exact + near-duplicate dedup, then HSET + EXPIRE. Returns JSON status including `ttl_seconds`. Body > `chunk_threshold`: persists a **parent document** (`doc:<id>`) verbatim and writes sliding-window chunks to `mem:<id>`; returns `{"saved": true, "parent_id": "...", "chunks": N, "saved_count": N, "ids": [...], "ttl_seconds": ...}`. If `owner_id` is provided and does not match the server config, returns `{"status":"error","saved":false,"reason":"owner_id mismatch"}`. `importance` is clamped to 0.5–2.0 and feeds `stored_importance` in ranking. When `session_id` is omitted, the server default (`N3MC_SESSION_ID` env var, or a per-process UUIDv4) is stored as a write-time tag — used as the filter key for `delete_memories_by_session` AND as the match key for subsequent `search_memory` `b_session` boosting. |
| `list_memories` | `limit?: int (default 20)`                | Markdown listing that interleaves parent documents and standalone memories, newest first. Parents are tagged `[doc×N]`; chunks are hidden (FT.SEARCH `*` fetch then Python filter on empty `parent_id` — see §3.12). |
| `delete_memory` | `id: string`                              | If the id is a parent (`doc:<uuid>`), cascade-deletes the parent, `docsha:`, and every chunk with matching `parent_id`. Otherwise `DEL mem:<uuid>` + `DEL mem:sha:<sha1>` atomically. |
| `delete_memories_by_session` | `session_id: string`         | Bulk-delete every standalone memory, parent document, child chunk, and sha guard tied to the given `session_id`, scoped to the configured `owner_id`. Response: `{"status":"deleted", "session_id": ..., "documents_deleted": D, "chunks_deleted": C, "singles_deleted": S, "deleted": D+C+S}`. When nothing matches: `{"status":"not_found", "session_id": ..., "deleted": 0}` (a re-call is a safe no-op). **Irreversible — confirm `session_id` with the user before calling.** Lite-only (see [§10 Test 6](#10-self-evaluation-evidence-report)). |
| `repair_memory` | —                                         | `ensure_index()`; see [§3.10](#310-repair).                            |

All tool responses are a single `TextContent` element. `save_memory` / `delete_memory` / `delete_memories_by_session` / `repair_memory` return JSON strings; `search_memory` / `list_memories` return human-readable markdown. **Every response also ends with a short auto-save reminder** (separated by `\n---\n`) — the [§11](#11-save-guarantees-and-the-limit-of-the-mcp-protocol) nudge channel. Callers that machine-parse the JSON should use a streaming decoder (e.g. `json.JSONDecoder().raw_decode()`) that returns the first JSON document and ignores the trailing markdown nudge.

### 4.4 Error Handling

Tool exceptions are caught in the dispatch layer and returned as `TextContent` with a leading `"Error: "` prefix. The server never crashes the stdio loop due to a tool-level exception. If Redis is unreachable when a tool is called, the dispatcher returns a "start Redis Stack" hint instead of invoking the tool.

---

## 5. Behavioral Instructions (Auto-Save Strategy)

Because MCP has no equivalent of Claude Code's `UserPromptSubmit` / `Stop` hooks, the auto-save behavior is expressed as **natural-language instructions** returned in the `initialize` response. The connected LLM reads them as system guidance.

The instructions require the LLM to:

1. **Search first, then acknowledge when you recall** — call `search_memory` at the start of every user turn with a concise query reflecting the user's intent. When the retrieved snippets actually shape the reply (i.e. you are recalling information saved in an earlier turn), open the reply with a short acknowledgment **in the user's language**, e.g. Japanese 「前回の回答がメモリに保存されています。」 or English "Pulling this from earlier memory in this session." **If no relevant memory was found, or if retrieval did not influence the answer, do not announce anything.** Never announce the mere act of searching — only the act of recalling.
2. **Save every substantive exchange — automatic, no permission asked** — saving is silent and automatic. The user should NEVER have to say "save this" or "remember that"; the LLM saves by default and does NOT ask for confirmation. After each meaningful turn, call `save_memory` to persist (a) the user's paraphrased intent, (b) **the LLM's own substantive output** — decisions, plans, and especially any creative or generated content the user may refer back to later: world-building, character settings, design sketches, code architecture, research summaries, outlines (if more than a sentence or two of work went into producing it, save it), and (c) key facts, preferences, and unresolved questions. One `save_memory` call per distinct fact, 50–200 chars each (long content → rule 3). Duplicates are auto-rejected server-side, so err on the side of saving more. **Note**: the Lite text explicitly reminds the LLM that entries vanish after 7 d — it is a rolling scratchpad, not a permanent archive.
3. **Long content — save the full body in one call (verbatim recall)** — when the turn produces OR receives a long body the user may want back verbatim — a user-pasted spec / article / log / code dump, **OR a long creative setting / world / character sheet / design doc the LLM just generated** — pass the FULL text to a single `save_memory` call. The server automatically creates a parent document + chunks, indexes chunks for search, and returns the full parent body on recall (see [§3.11](#311-verbatim-recall-parent-document--chunks-pattern)). Rough threshold: **> ~400 chars → save as one full-body call**. Shorter summarizable content → rule 2 (multiple short facts). Do NOT split a long verbatim-worthy body into many short summaries — that destroys recall fidelity.
4. **Handle tool errors visibly — never generate long content blind** — if `search_memory` / `save_memory` returns a server error (Redis unreachable, connection refused, timeout, "start Redis Stack" hint, etc.), **STOP and surface the failure to the user in their language BEFORE generating any long creative or spec content**. Rationale: with the backend down, every subsequent `save_memory` also fails silently, so a long setting / design doc / code architecture produced during the outage is lost the moment the session closes — and the user will not realize the memory layer was broken until recall fails. Required behavior: (a) announce the failure succinctly (e.g. "Memory backend is unreachable — save/search are offline right now"), (b) relay the recovery hint the tool returned (e.g. `docker run -p 6379:6379 redis/redis-stack-server:latest`), (c) ask whether to proceed without memory (so the user knows this session will not persist) or to pause until the backend is restored. Short factual replies may proceed, but must still mention the error once so the user knows saves are failing.
5. **Skip noise** — do not save greetings, clarifying questions, or mechanical acknowledgements.
6. **Respect explicit requests** — honor "don't save this" and "forget that" (use `delete_memory`).
7. **Inform the user about the 7-day TTL when they expect permanence** — the TTL is visible to the LLM (this INSTRUCTIONS block, the `search_memory` tool description, and the `ttl_seconds` field on every `save_memory` response) but is **invisible to the human user**. Default behavior remains silent auto-save (rule 2) — do NOT mention TTL on every save. However, when the user's message signals an expectation of long-term retention — explicit permanence phrases ("remember this forever", "don't forget", "save permanently", 「ずっと覚えておいて」「永続的に保存して」), a long setting / spec / code dump / world-building / character sheet the user obviously invested in, or a reference back to content saved more than ~5 days ago — add ONE short sentence in the user's language reminding them that Lite memories auto-expire 7 days after save, noting that the **Pro build (sqlite-vec backed, coming soon)** will offer persistence, and suggesting external backup in the meantime. Save anyway; do not ask permission. Emit the reminder ONCE per distinct long-term signal, not once per turn and not once per save. Rationale: without this rule, the LLM silently persists content that the user believes is permanent, and the user only discovers the loss when recall fails mid-project.
8. **Respect the `skip_code_blocks` server policy** — when `save_memory` returns `{"status": "skipped_code", "saved": false}`, the server is configured to reject any payload containing a triple-backtick fence (see [§6](#6-configuration)). This mirrors the FastAPI-era N3MemoryCore's code-exclusion behavior for users who intentionally keep code out of memory. Do NOT retry the same payload; either save a prose description of what the code does, or skip saving for that turn. Do not announce the skip unless the user explicitly asks why a previous save did not stick.

The full text is in [`n3mc_mcp/instructions.py`](./n3mc_mcp/instructions.py).

---

## 6. Configuration

On first run, `config.json` is auto-generated in the data directory with random UUIDv4 values for `owner_id` and `local_id`.

Complete schema (missing fields auto-filled with defaults below):

```json
{
  "owner_id":                 "<UUIDv4 auto-generated>",
  "local_id":                 "<UUIDv4 auto-generated>",
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

- `redis_url` — connection URL. `N3MC_REDIS_URL` env var overrides this field.
- `ttl_seconds` — TTL applied to every new memory and its sha-guard (default 7 d). Lowering it is fine; raising it far beyond a week defeats the purpose of the Lite and will be flagged during review.
- `search_result_limit` — max results returned by `search_memory`.
- `context_char_limit` — reserved for client-side truncation by downstream tools; not used internally.
- `min_score` — excludes results with score below this value (default `0.2`). Set to `0.0` to disable.
- `search_query_max_chars` — max characters used from a query (default `2000`; embedding model caps at ~512 tokens).
- `chunk_threshold` / `chunk_overlap` — sliding-window size and overlap (defaults 400 / 100 chars). Bodies longer than the threshold trigger the parent-document + chunks path (see [§3.11](#311-verbatim-recall-parent-document--chunks-pattern)).
- `access_count_enabled` / `access_count_weight` / `access_count_max_boost` — enable flag, per-hit weight, and cap for the frequency boost (see [§3.6](#36-ranking-formula)). Setting `enabled` to `false` disables the feature entirely and the formula falls back to `stored_importance` only.
- `ttl_refresh_on_search` / `ttl_refresh_top_k` — TTL-reset and `access_count` increment for the top-K hits after each search. Reset-only (no lifetime extension beyond a fresh save). When a chunk hit expands to its parent document, the `doc:<parent_id>` key's TTL is also refreshed alongside the chunk's `mem:` key, keeping verbatim recall ability alive in sync with the chunks.
- `lexical_rerank_enabled` / `rerank_weight` / `rerank_phrase_weight` — lightweight post-fusion lexical reranker (see [§3.6](#36-ranking-formula)). Setting `enabled` to `false` passes the fused score through unchanged.
- `b_session_match` / `b_session_mismatch` — the `b_session` factor in the ranking formula (see [§3.6](#36-ranking-formula)). For each row, the search compares the request's `effective_session` (per-call argument → `N3MC_SESSION_ID` env var → per-process startup UUID) against the row's stored `session_id`: matches multiply the score by `b_session_match` (default `1.0`), mismatches by `b_session_mismatch` (default `0.6`). Set both to `1.0` to disable the bias entirely (all rows symmetric).
- `skip_code_blocks` — when `true`, `save_memory` rejects any content containing a triple-backtick fence (```` ``` ````) and returns `{"status": "skipped_code", "saved": false}`. Default `false` (inherit the FastAPI-era N3MemoryCore behavior where users who did not want code in memory could opt out). Heuristic only — the fence marker is the signal; mixed prose+code is rejected wholesale, not stripped. The LLM is instructed (§5) to avoid retrying the same payload on `skipped_code` and to save a prose description instead.

> **Multi-account on a single PC**: each OS user runs the server under their own `config.json` by default. To share a Redis across accounts, set the same `redis_url` in both configs — entries are segregated via the `owner_id` TAG filter.

---

## 7. Data Location

By default, only `config.json` lives on disk:

| OS      | Path                                                        |
| ------- | ----------------------------------------------------------- |
| Windows | `%LOCALAPPDATA%\n3mc-workingmemory\`                        |
| macOS   | `~/Library/Application Support/n3mc-workingmemory/`         |
| Linux   | `~/.local/share/n3mc-workingmemory/`                        |

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
    "n3mc-workingmemory": {
      "command": "n3mc-workingmemory",
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
/plugin install n3mc-workingmemory@neuralnexusnote
```

The plugin ships a `plugin.json` that launches the server via `uvx --from n3memorycore-mcp-lite n3mc-workingmemory`. Requires `uv` on PATH.

**(b) Project-local `.mcp.json` (manual, when cloning the repo or pip-installing)**

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

**(c) Project-local `.mcp.json` via uvx (no prior install needed)**

```json
{
  "mcpServers": {
    "n3mc-workingmemory": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "n3memorycore-mcp-lite", "n3mc-workingmemory"]
    }
  }
}
```

Restart the client after editing the config. Ensure Redis Stack is running *before* the client starts the server — otherwise the first tool call returns the "start Redis Stack" hint.

### Auto-approve tool calls (Claude Code only)

By default, Claude Code prompts the user for each MCP tool call. **For the auto-save loop to work without the LLM blocking mid-turn**, pre-approve the `n3mc-workingmemory` tools — otherwise every `save_memory` / `search_memory` call pops a Yes/No dialog and stalls the connected AI when the user is away from the keyboard.

**Plugin install auto-configures this** — installing via `/plugin install n3mc-workingmemory@neuralnexusnote` ships a `SessionStart` hook ([`hooks/install_permissions.py`](./plugins/n3mc-workingmemory/hooks/install_permissions.py)) that idempotently adds the six `mcp__n3mc-workingmemory__*` tools to `~/.claude/settings.json`. It only writes when at least one entry is missing, leaves unrelated fields untouched, and requires `python` on `PATH`.

**If you installed without the plugin** (`claude mcp add`, manual `.mcp.json`, or Python is not available), add the block below manually to `~/.claude/settings.json` (user-global — recommended) or `.claude/settings.json` (per-project):

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

> Claude Desktop has no per-tool permission gate, so this step is unnecessary there. Claude Code does not (as of 2026-04) accept a `permissions` field in `plugin.json`, so the plugin ships a `SessionStart` hook that idempotently patches the user's `settings.json` (see "Plugin install auto-configures this" above).

---

## 9. Testing (pytest)

> **Purpose**: Repeatable automated regression tests that complement the manual Evidence Report in §10. Layers are split by responsibility; the MCP tool E2E is the last line of defence.

### How to run

```bash
# 1. Start Redis Stack (RediSearch can only index DB 0)
#    First time: docker run -d --name redis-stack -p 6379:6379 redis/redis-stack-server:latest
#    Subsequent: docker start redis-stack
docker start redis-stack 2>/dev/null || docker run -d --name redis-stack -p 6379:6379 redis/redis-stack-server:latest

# 2. Install with dev deps and run pytest
pip install -e ".[dev]"
pytest tests/ -q
```

Tests auto-skip (not fail) if Redis Stack is not reachable at `N3MC_REDIS_TEST_URL` (default `redis://localhost:6379/0`).

> **⚠️ Destructive test DB**: RediSearch can only create indexes on DB 0 (`Cannot create index on db != 0`), so the test suite FLUSHDBs DB 0 before and after every test. Do **not** point `N3MC_REDIS_TEST_URL` at a Redis instance that holds data you care about — run a dedicated container for testing.

### Directory layout

```
n3mcmcp-lite/
└── tests/
    ├── conftest.py          # Shared fixtures: isolated data dir, Redis URL override, dummy vectors
    ├── test_database.py     # Layer 1: Redis / RediSearch unit tests (CRUD, schema, TTL, dedup, BM25, KNN)
    ├── test_processor.py    # Layer 2: ranking math, embeddings, purify, chunking, reranker
    └── test_server.py       # Layer 3: MCP tool dispatch E2E (isolated `config.json` + flushed DB 0)
```

### Layer 1: `tests/test_database.py` (34 tests)

| Test class | What it covers | Area |
|---|---|---|
| `TestIndexSetup` | RediSearch index creation, idempotent re-run, adding missing fields | Schema management |
| `TestInsertAndRetrieve` | INSERT→COUNT, hash integrity, insertion without embedding, `parent_id` storage | CRUD |
| `TestDedup` | Exact SHA collisions, near-similarity threshold, parent-doc SHA guard | De-duplication |
| `TestDelete` | Single delete, parent→chunk cascade, `docsha:` guard cleanup | Transactional safety |
| `TestTTL` | 7-day TTL set on save, `EXPIRE` refresh on search hits, expiry-driven disappearance | TTL |
| `TestFTS` | Punctuation stripping, BM25 scores, short queries, CJK-bigram hits | FTS / Japanese |
| `TestVectorSearch` | KNN results, empty-DB search, `owner_id` filter, `-@parent_id:{*}` exclusion | KNN search |
| `TestCjkBigramExpand` | Bigram expansion for hiragana / katakana / kanji mixes, boundary handling | Tokenization |
| `TestAccessCount` | `access_count` HINCRBY, only top-N increments, cap enforcement | Auto importance |
| `TestSerialization` | Vector binary round-trip, f32 LE packing | Serialization |
| `TestSha1` | SHA1 hex, empty string, non-ASCII UTF-8, long input | Digest |

### Layer 2: `tests/test_processor.py` (52 tests)

| Test class | What it covers | Area |
|---|---|---|
| `TestCosineSim` | Identical→1.0, orthogonal→0.0, negative clamp, RediSearch distance→similarity | Distance conversion |
| `TestTimeDecay` | Now→1.0, half-life, floor, malformed timestamp→1.0 | Half-life |
| `TestKeywordRelevance` | Below-threshold cutoff, normalization, zero max value | BM25 normalization |
| `TestFinalScore` | `(cos·0.7 + bm25·0.3)·decay·b_local`, `b_local` clamp | Ranking formula |
| `TestAccessCountBoost` | `stored_importance + access_boost`, 0.5–2.0 clamp, disabled flag | Auto importance |
| `TestLexicalRerank` | Term coverage, phrase boost, short-content preference, zero-overlap non-penalty | Lightweight reranker |
| `TestPurification` | Multi-line code block → `[code omitted]`, inline code preserved, multiple blocks | Purification |
| `TestChunkText` | Below-threshold→single, long→split+overlap, boundary alignment | Parent-chunk |
| `TestEmbedding` | `passage:` / `query:` prefixes, vector dimensionality, same-text similarity | Embedding |
| `TestEncodingSafety` | Lone-surrogate strip on `str` / `list` / `dict` / `None`, all-surrogate input collapses to empty, post-strip `.encode("utf-8")` succeeds | Encoding safety (§3.13) |

### Layer 3: `tests/test_server.py` (18 tests)

| Test class | What it covers | Area |
|---|---|---|
| `TestToolRegistration` | Six tools registered, schema types, description non-empty | MCP registration |
| `TestSaveAndSearch` | Save→search round-trip, exact-duplicate rejection, empty-content rejection | Single chunk |
| `TestListAndDelete` | Three recent entries listed, delete non-existent id | List / delete |
| `TestRepair` | `repair_memory` on empty DB → ok | Repair |
| `TestUnknownTool` | Unknown tool name → error string | Dispatch |
| `TestParentDocRecall` | Verbatim recall of fictional "Shiranui" city doc, chunk→parent collapse, `[doc×N]` rendering, parent→chunk cascade delete | Parent document (§3.11) |
| `TestEncodingSafetyE2E` | `save_memory` with surrogate-laced content does not raise, surrogate-only payload returns empty-content error, `search_memory` with surrogate-laced query returns cleanly | Encoding safety end-to-end (§3.13) |

### Example runs

```bash
# Full suite
pytest tests/ -q

# Single layer
pytest tests/test_database.py -v

# Skip slow embedding tests
pytest tests/ -v -k "not TestEmbedding"
```

> **⚠️ Relationship to the Evidence Report**: A failing automated test does **not** block the §10 sign-off. The Evidence Report is the sole pass/fail gate for implementation completeness. The automated suite is an optional regression aid and must not trigger an endless fix-and-retry loop during the first build.

---

## 10. Self-Evaluation (Evidence Report)

> Once implementation is complete, the AI runs the following checks autonomously and reports each item at ⭐⭐⭐⭐⭐. This is not a post-generation one-shot scorecard: it assumes the §9 pytest suite is green. MCP Lite is judged on *best-in-class behavior given the Redis-Stack + 7-day-TTL constraint set*.

1. **Latency & process health**: Call `search_memory` from an MCP client and measure both the first call (which includes model load) and subsequent calls. Target: **≤3.0 s first call / ≤0.5 s steady state on CPU**. Also confirm that calling any tool while Redis Stack is down does **not** crash the server — it must return the "start Redis Stack" hint.

2. **Real-person recall test (historical data)**: Save a passage about a real historical figure via `save_memory`, then search for that figure's name with `search_memory`. Pass criterion: the saved record appears in the **top-3 results**.
   - Japanese example: "坂本龍馬" (Sakamoto Ryoma)
   - English example: "Abraham Lincoln"

3. **Fictional-setting test (creative world-building / parent-document contract)**: Save a passage **≥400 characters** containing made-up names/places/terms via `save_memory`, then retrieve it with `search_memory`. Pass criterion: **every section of the saved text is restored byte-for-byte**. Per §3.11's parent-chunk design, even though search hits land on chunks, the response must substitute the verbatim full text fetched from `doc:<parent_id>`. Code blocks inside a Claude response are replaced with `[code omitted]` per the purification contract.
   - Japanese example: "Floating city Shiranui (fictional setting sheet)"
   - English example: any fictional character, city, or proper noun

4. **FTS punctuation + CJK tolerance test**: Save Japanese text containing brackets or punctuation (e.g. `架空の惑星「アルファ9」の気温設定`) via `save_memory`, then search with a query that drops the brackets (e.g. `アルファ9 気温`). Pass criterion: the record shows up in the **top-3 results**. This verifies that §3.7's punctuation stripping and CJK bigram expansion are applied consistently on both the save side and the query side.

5. **Complete-recording test**: Confirm that any non-empty input is recorded — the legacy "skip if under N chars", "skip boilerplate", and "noise pattern" filters must **not** exist.
   - A 2-character string (e.g. `はい` / `ok`) passed to `save_memory` **is saved**.
   - Stock replies (`ok`, `yes`, `thanks`) passed to `save_memory` **are saved** (exact/near duplicates may be rejected server-side — if so, retry with a different string).
   - Empty or whitespace-only input is rejected with `empty content`.

6. **Bulk-delete-by-session test (`delete_memories_by_session`)**: Confirm that working memory belonging to a finished project/task can be wiped without waiting for the 7-day TTL.
   1. Pick a unique `session_id` (e.g. `eval-cleanup-YYYY-MM-DD`) and call `save_memory` several times under that session_id, covering **multiple shapes**:
      - a short single record (a 2–10-char string such as `はい` / `ok`)
      - a medium single record (a few dozen to a few hundred chars carrying one fact)
      - **a long body that exceeds `chunk_threshold` (default 400)** — the server auto-splits it into a parent document (`doc:<uuid>`) plus child chunks.
   2. Before deletion, confirm via `search_memory(query=..., session_id=<the test session>)` that the records are retrievable, and via `list_memories` that the parent shows the `[doc×N]` tag.
   3. Call `delete_memories_by_session(session_id=<the test session>)` **once**. The response must take the form `{"status": "deleted", "session_id": ..., "documents_deleted": D, "chunks_deleted": C, "singles_deleted": S, "deleted": D+C+S}`. Verify the numbers reconcile with what was inserted (parent + every child chunk must cascade out together).
   4. Immediately re-run `search_memory` against the same session_id and confirm **zero results** for the previously inserted content. Records under other session_ids must be unaffected — the operation is a hard, session-scoped delete, not a soft demotion.
   5. **Idempotency check**: call `delete_memories_by_session` again with the same session_id; the server must respond `{"status": "not_found", ..., "deleted": 0}` cleanly — no errors, no crashes.
   6. **Pass criterion**: insertion count matches the server's `deleted` total, parent→chunk cascade is complete, no collateral effect on other sessions, and the second call is a safe no-op. **This is an irreversible operation. Run this test only against a dedicated test session_id; never against a production session_id.**

---

## 11. Save guarantees and the limit of the MCP protocol

> **Stated up front as a design premise**: this server can persuade the LLM to save and search via MCP, but **whether the LLM actually calls those tools cannot be enforced at the MCP-protocol level**. This is not an implementation defect — it is a limit of what an MCP server is allowed to do.

### The three persuasion levers an MCP server has

1. **The `description` field of each tool in `tools/list`** — visible to the LLM on every turn.
2. **The `instructions` field** — sent once to the client at session start.
3. **`tools/call` response text** — read by the LLM whenever it does call a tool. (See §4.3: each tool's response ends with a short reminder that re-anchors the auto-save discipline mid-turn.)

This spec uses all three. Even so, **whether the LLM follows through is non-deterministic**. Compliance depends on:

- the model's own training and tool-calling bias,
- the MCP client's prompt construction (some clients summarize or drop the `instructions` field),
- competing instructions from the user prompt, the project's `CLAUDE.md`, etc.

### What this looks like in practice

Most turns auto-save correctly. But **short answers, fact-correction turns, and turns where the LLM is heavily focused on the user's question** sometimes skip the save. The behavior is hard to evaluate automatically, so it is not part of the §10 Evidence Report. "If it skipped, it stays skipped" — you don't notice until the next session, when you find the fact missing.

### Two paths when guaranteed save matters

When you cannot rely on the LLM's voluntary discipline, **only these two paths exist within the MCP framing**:

**Path 1: ask the LLM explicitly in your prompt** (operational workaround, immediate)
- Write `"save this to N3MemoryCore"` / `"record this in memory"` into the prompt.
- LLMs almost always honour explicit user requests.
- **Pros**: zero infrastructure, works today, works with every MCP client.
- **Cons**: cognitive load on the user (you must remember to say it; not auto).

**Path 2: bypass MCP and orchestrate against the first-party Anthropic Messages API** (architecture change)
- Drop the MCP path entirely and drive `messages.create` `tool_use` directly from your own application code.
- You can then make `save_memory` fire deterministically every turn regardless of what the LLM "decided" to do.
- **Pros**: deterministic — code does what code is written to do; saves are guaranteed.
- **Cons**: you have to write that orchestration application; you step outside MCP clients (Claude Code, etc.).

In short, **the convenience of "let MCP + the LLM handle it" and the guarantee of "every turn saves" sit at opposite ends of a tradeoff** — picking either side means giving up the other. The most this server can do is "pack the response with persuasion to make the LLM want to comply"; any stronger guarantee is, by spec, the user's or the client implementer's choice.

---

## Appendix A: Optional Extensions (not shipped)

The Lite build intentionally stops at the hybrid + time-decay ranker described in §3.6. The following extensions are **not part of the shipped spec** — they are sketched here so a future AI or contributor has a clean starting map when the user decides to try them. None of them are required for the Lite build to behave correctly; each is a precision-vs-latency trade.

- **Cross-encoder reranker** — after `hybrid_search` returns the top-N candidates, rerank them with a small cross-encoder (e.g. `cross-encoder/ms-marco-MiniLM-L-12-v2`, ~130 MB, or `BAAI/bge-reranker-base`, ~278 MB). Expect **+100–300 ms CPU latency** per `search_memory` call on a modern laptop (top-50 rerank), in exchange for roughly **+1 precision point** on paraphrase-heavy queries. Drop-in point: between the fused-score sort and the `min_score` filter in `processor.hybrid_search`. Keep the existing score as a fallback when the reranker is disabled. Note that the shipping build already enables a lightweight lexical reranker by default (`lexical_rerank_enabled`, see [§6](#6-configuration)); a cross-encoder would be a stronger upgrade slotting into the same hook.
- **HyDE (Hypothetical Document Embeddings)** — before embedding the user's query, ask a small LLM to synthesize a hypothetical *answer* to the query, then embed that answer instead of (or in addition to) the raw query. Helps when queries are short/vague and memories are long/specific. Needs an LLM hop per search, so it is a poor fit for the Lite build's "no external API calls" promise unless a local model is already available.
- **Japanese morphological analysis** — the shipping build already supplements RediSearch's default tokenizer with CJK bigram expansion (see [§3.7](#37-text-tokenization--punctuation-handling)), which covers the basic case. For further precision, pre-tokenize the body at save time with a morphological analyzer — candidates: `fugashi` + `unidic-lite` (MeCab-based, ~50 MB), `SudachiPy` + `sudachidict-core` (~70 MB, multi-granularity A/B/C modes), or pure-Python `Janome` when binary dependencies are a problem — store the space-joined surface forms in a parallel `text_tokens` TEXT field and point BM25 search at that field. Vector search is unaffected (e5 handles Japanese natively) and the raw body stays untouched for display. Expected cost: +5–20 ms per `save_memory` call; the delta versus bigram expansion shows up most on compound words and inflected forms.

All three extensions are additive — none of them require changes to the Redis schema's existing fields or the TTL/dedup contracts (the morphological tokenizer only **adds** a parallel field). A future implementer should treat them as separate feature flags, default-off, and benchmark each independently against the baseline ranker.

---

## Appendix B: Recommended AI-Assisted Workflow

> **This appendix is a human operator guide.** At each phase, copy the prompt inside ``` and paste it into Claude Code (or your preferred MCP client). The AI does not advance to the next phase on its own. Unlike the Free build, which is driven by slash commands, the MCP Lite build is driven by **MCP tool calls** (`save_memory` / `search_memory` / `list_memories` / `delete_memory` / `repair_memory`).

| Phase | What you do | Model |
|---|---|---|
| 1. Implementation | Paste the prompt to kick off the build | **Sonnet** (fast) |
| 2. Debugging | Paste three prompts **in order** to verify | **Sonnet** |
| 3. Evidence Report | **Restart Claude Code first**, then paste the prompt to run §10 | **Sonnet** or **Opus** |
| 4. Quality review | Paste the review prompt | **Opus** (deep reasoning) |

> **⚠️ A full Claude Code restart is required before Phase 3**
>
> The MCP server runs as a stdio child process spawned by Claude Code at startup, and the same process is reused for the rest of the session. Right after Phase 1 / 2 generated or modified code, the running server still holds the **old bytecode**, so the Evidence Report would not reflect the current implementation. §10-1 also measures the `initialize` response time and the very first `search_memory` latency, **which can only be observed at server startup**.
>
> Pre-restart checklist:
> 1. Confirm the latest code is on the import path (`pip install -e .` or reinstall).
> 2. Confirm `n3mc-workingmemory` is registered — user scope: `~/.claude.json` (recommended for Claude Code), project scope: `.mcp.json` at the project root, Claude Desktop: `claude_desktop_config.json` (see [§8](#8-mcp-client-configuration)).
> 3. Confirm `~/.claude/settings.json` already has the `mcp__n3mc-workingmemory__*` allow block (see [§8 tool auto-allow](#tool-auto-allow-claude-code-specific)).
> 4. Confirm Redis Stack is up (`docker ps`; if not, `docker start redis-stack` or `docker run -d --name redis-stack -p 6379:6379 redis/redis-stack-server:latest`).
> 5. **Fully exit and relaunch Claude Code.** On **Windows**: closing the window with × or running `/exit` can leave background processes alive — open **Task Manager** and end every `Claude` / `claude` process (including the child `python.exe` whose command line is `n3mc-workingmemory.exe`).
> 6. The first tool call after relaunch takes 2–10 minutes if the e5-base-v2 (~440 MB) HF cache is missing. With the cache warm, `initialize` finishes in ~17 s (see [§3.9 step 4](#39-startup-sequence-and-self-recovery)).

---

### Phase 1: Implementation (Sonnet)

Set the model to **Sonnet** and paste:

```
Please build n3mcmcp-lite from this spec (N3MemoryCore_MCP_Spec_EN.md).
Redis Stack is already running in Docker. Run it as an MCP stdio server
and register the six tools (save_memory / search_memory / list_memories
/ delete_memory / delete_memories_by_session / repair_memory).
```

Sonnet handles package scaffolding, RediSearch index creation, MCP tool registration, and stdio launch automatically. When it finishes, move on to phase 2 ("it runs" ≠ "it matches the spec" — do not stop here).

> **⚠️ Fully restart Claude Code between Phase 1 and Phase 2**
>
> When Claude Code launched at the start of Phase 1, `n3mc-workingmemory` did not yet exist, so Claude Code is not connected to this MCP server. Before pasting the Phase 2 debugging prompts, **fully exit Claude Code and reopen it** so it picks up the freshly-registered MCP server and can actually exercise `save_memory` / `search_memory` while debugging.
>
> **How to fully exit on Windows**: closing the window with × or running `/exit` can leave background processes alive. Open **Task Manager**, end every `Claude` / `claude` process (including child `python.exe` processes whose command line is `n3mc-workingmemory.exe`), and then relaunch Claude Code. After relaunch, verify in the settings panel that `n3mc-workingmemory` is listed as **connected** before proceeding (the first `initialize` response takes ~17 s — see §3.9 step 4).

---

### Phase 2: Debugging (Sonnet)

Stay on **Sonnet** and paste the following three prompts **one at a time, in order**.

**① Data-flow trace** (check that nothing is silently dropped)
```
For n3mcmcp-lite:
Read the code and trace the end-to-end data flow from a save_memory tool
call all the way to the Redis pipeline's EXECUTE, and from a search_memory
call back to the tool response. Verify that TTL is set on every write and
that the parent-document fallback (§3.11) is not lost mid-flight. Fix any
issues you find.
```

**② Spec-to-code comparison** (find behaviors documented but not implemented)
```
For n3mcmcp-lite:
Walk the input schema and behavior of each MCP tool in §4.3 one by one,
and compare it against the actual implementation. Also walk the parent-
chunk contract in §3.11 line by line (verbatim restoration, parent→chunk
cascade delete, [doc×N] rendering). Flag anything documented but not
implemented and fix it.
```

**③ Cross-session test** (confirm data survives an MCP restart)
```
For n3mcmcp-lite:
From an MCP client, save an entry with save_memory in session 1, restart
the MCP server (keep Redis running), then run search_memory in session 2
and confirm the entry is retrievable. Fix anything that prevents this.
```

When all three are done, proceed to phase 3.

---

### Phase 3: Evidence Report

> **Fully exit and relaunch Claude Code before running this phase** (see the "⚠️ A full Claude Code restart is required before Phase 3" box above). Without the restart, (a) the MCP server still runs old bytecode and the Evidence Report won't match the current implementation, and (b) §10-1's `initialize` and first-call timings cannot be captured.

The model can be **Sonnet** or **Opus** — Sonnet is sufficient to verify the items; Opus tends to grade harder. After the restart, paste:

```
For n3mcmcp-lite:
Run the §10 Evidence Report from the spec. Actually call the MCP tools
(mcp__n3mc-workingmemory__*) and verify items 1 through 6 in order.
Score each item on a 1–5 ⭐ scale and write up a short Evidence Report.

Be explicit about:
- §10-1: initialize response time, first search_memory latency, and the
         median of 5 steady-state search_memory latencies; verify the
         server returns a hint (no crash) when Redis is down
- §10-3: byte-for-byte verbatim recall of a >400-char body (parent-doc
         contract and [doc×N] tag)
- §10-4: bracketed save text retrieved via a bracket-free query within
         the top 3 (CJK bigram coverage)
- §10-6: mixed inserts (short / medium / long-exceeding-chunk_threshold)
         under one session_id → delete_memories_by_session → matches
         the inserted count, leaves other sessions untouched, second
         call returns not_found

If anything fails, identify the root cause and fix the implementation,
then rerun. When all items are ⭐⭐⭐⭐⭐, advance to Phase 4.
```

Once the Evidence Report is green, proceed to Phase 4 (quality review).

---

### Phase 4: Quality review (Opus)

Switch the model to **Opus** and paste:

```
Please review n3mcmcp-lite and fix anything that needs fixing.

On a scale of 1–10, what score does n3mcmcp-lite earn as (a) a memory
device exposed over MCP and (b) a RAG system? Split the evaluation:
memory device (save / 7-day TTL / dedup / parent-document verbatim
contract) vs. RAG (hybrid search / CJK bigram / lightweight reranker /
time decay / auto importance). Generate a scorecard.
```

Opus will actually call the MCP tools and produce a two-axis scorecard: **memory device** (persistence, TTL, dedup, parent doc) vs. **RAG** (retrieval precision, ranking, noise resistance).

> **Note**: the MCP Lite build ships a lightweight reranker and CJK bigram expansion by default, so the RAG ceiling sits slightly higher than in the Free build. Even so, the following are not implemented, and **the RAG score is unlikely to exceed 8:**
> - Morphological analysis (MeCab / SudachiPy, etc.) for stricter word boundaries (see Appendix A)
> - A full cross-encoder reranker (see Appendix A)
> - Language-specialized embedding models (e.g. `multilingual-e5-large`)
> - Query-expansion techniques such as HyDE (see Appendix A)
>
> Soft grading robs you of improvement opportunities — grade strictly. **Regardless of the score, have Opus explicitly list what is missing and discuss concrete fixes with it.**

---
