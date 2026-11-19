# Modifications from N3MC-Free

This document records, as asked by section 4(b) of the Apache License 2.0,
how **N3MemoryCore MCP Lite** (`n3mcmcp-lite`, PyPI
`n3memorycore-mcp-lite`) uses and changes parts of **N3MC-Free**
(`n3memorycore-free`). It applies from version 2.0.0 (see
[CHANGELOG.md](./CHANGELOG.md); 2.0.0 is not released yet, and the code is
generated from the specification
[`N3MemoryCore_MCP_Spec_EN.md`](./N3MemoryCore_MCP_Spec_EN.md)).

It is written at module / feature granularity, not line by line.

## Lineage

```
N3MC-Free  (Apache 2.0, public)
    ↓ hooks, resident server and CLI taken over and adapted
N3MemoryCore MCP Lite 2.0.0  (Apache 2.0, public — this product)
    = N3MC-Free + MCP, with Redis as the database
```

Up to 1.12.2, MCP Lite was an MCP server of its own (Redis Stack storage,
Lite's search and save rules); its encoding-safety guards (UTF-8
reconfigure of the standard streams, lone-surrogate removal) followed
N3MC-Free's. From 2.0.0 it contains N3MC-Free's hooks, resident server and CLI, adapted
as described below. Free and Lite keep separate search and save rules.

| Upstream | License | Copyright | URL |
|---|---|---|---|
| N3MC-Free (`n3memorycore-free`) | Apache License 2.0 | Copyright 2026 NeuralNexusNote / ArnolfJp019 | https://github.com/NeuralNexusNote/n3mc-free |

Both products have the same author. This product is licensed under the
Apache License 2.0 (see [LICENSE](./LICENSE)); N3MC-Free's attribution is
kept in [NOTICE](./NOTICE).

---

## Taken from N3MC-Free

| Part | In N3MC-Free | In MCP Lite (specification section) |
|---|---|---|
| Claude Code hooks: `UserPromptSubmit` (audit record, repair, previous reply from the transcript, search, save the message) and `Stop` (audit record, save the reply, housekeeping) | Free §4, §4.5, §5 | §12 (`n3mc_mcp/lite_hook.py`, `lite_stop_hook.py`, `cli.py`) |
| `audit.log` written before anything else, rotated at 50 MB | Free hooks | §12.2 |
| Reading the previous reply from the transcript (decision D3) | Free | §12.5 |
| Hook registration `--init` with exact matching of its own hook entries (`is_own_hook`, decision D1), scripts-folder-first command resolution | Free §4 ① | §12.8 |
| Resident FastAPI server: `/health` identity (`root`, `code_version`, `pid`), `ensure_server`, stopping a stale server, PID file handling, start with back-off, `/shutdown`, Host-header check, no proxy | Free §3 "Resident FastAPI Server" | §3.15, §4.5 |
| Notices that need human action (`_notify`, `notices.json`, once a day) | Free | §3.15 |
| CLI | Free's CLI | §13 (`n3mc-lite`) |
| Rules file in `.claude/rules/` kept up to date by `--stop` | Free (`n3mc-behavior.md`) | §12.9 (`n3mc-lite-behavior.md`) |
| Role prefixes `[user]` / `[claude]` / `[<role> i/N]` on hook records (`add_chunk_prefixes` format) | Free | §3.16 |
| Exact-duplicate check of hook saves within one exchange (`turn_id`) | Free | §3.16 |

## Changed from N3MC-Free

- **Storage: Redis Stack instead of SQLite.** Every hook save goes through
  Lite's `save_memory` (Redis, RediSearch, 7-day TTL on every key, `SET NX`
  duplicate guards, one pipeline). No database file on disk.
- **Lite's search and save rules instead of Free's.** Search is Lite's hybrid
  search and result shaping (vector floor, newest version first, relative
  cut, weak-match fill, `saved=` times); saving is Lite's (`[code omitted]`,
  `chunk_threshold` 400 / `chunk_overlap` 100, parent document, guards). Lite
  has no separate Q-A exchanges block.
- **No endpoints beyond Lite's needs.** `/health`, `/shutdown`, `/buffer`,
  `/search`, `/repair` and `/tools/{name}`; Free's `/list` is replaced by the
  `list_memories` tool, and the `/delete`, `/gc` and `/import` endpoints do
  not exist.
- **Direct save without the server.** When the resident server is not
  available, a hook saves straight to Redis without a vector (found by
  keywords, vector added later by repair); nothing is queued for resending.
- **Hooks always exit 0** (Free exits with the status of `--hook-submit`).
- **Lite's names**, so both products can be installed on one PC: port
  18524 (Free 18520), data folder `~/.n3mc-workingmemory` with the files flat
  in it (Free `~/.n3mc` with a `.memory/` subfolder), commands `n3mc-lite`,
  `n3mc-lite-hook`, `n3mc-lite-stop-hook`, module `n3mc_mcp.cli`, rules file
  `n3mc-lite-behavior.md`, PID file `n3mc-lite.pid`.
- **Plugin installation through `uvx`**: the hook entries take `--uvx`, and
  the rules file and the permission rules then name the
  `uvx --from n3memorycore-mcp-lite==<version> n3mc-lite` form; the plugin's
  `uvx` commands are pinned to the package version. `--init` adds no hooks
  while the plugin is enabled.
- **Endpoints are plain `def`** and Lite's core runs under locks, because
  Lite's core ran in one event loop up to 1.12.2.

## Added (not in N3MC-Free)

- **MCP entry for applications without hooks** (`n3mc-workingmemory`, alias
  `n3mc-mcp-lite`; `n3mc_mcp/server.py`, `tooldefs.py`): a thin stdio client
  that answers `initialize` and `tools/list` itself, loads no model and
  forwards every tool call to the resident server (`/tools/{name}`). Its
  seven tools, their arguments and output texts, and the behavioral
  instructions are those of MCP Lite 1.12.2.
- **Tool handlers in the resident server** (`n3mc_mcp/tools.py`), used by the
  MCP entry and by the CLI commands `--search`, `--buffer`, `--list`,
  `--recall-thread`, `--delete`, `--delete-session`, `--repair`.
- **Context size limit** (`context_max_chars`, default 9500) on the hook's
  `# Recalled Memory Context` block and the MCP `search_memory` result.
- **Coexistence** with N3MC-Free, N3MC-Pro and the other editions on one PC
  (§14).

The specification also follows two designs of the same author's other
N3MC editions: the context size limit (§3.17, from N3MC-Pro §5) and the
mechanics of the thin MCP entry (§4.6, from N3MC MCP Pro v1.0.0 §4.6).

## Kept from MCP Lite 1.12.2

- Redis layout, index and settings (`redis_url`, `N3MC_REDIS_URL`), the
  7-day TTL and the ephemerality contract.
- Every search and save rule of the specification §3, the seven MCP tools,
  the behavioral instructions (with two added paragraphs for clients that
  have the hooks' block), `config.json` and its location.
- The PyPI name, the package `n3mc_mcp`, the plugin name
  `n3mc-workingmemory`. Records saved by 1.12.2 stay readable.

## Removed (relative to MCP Lite 1.12.2)

- The plugin no longer registers the MCP server in Claude Code
  (`plugin.json` has no `mcpServers`), and the specification's package
  layout has no `examples/claude_code_mcp.json`: Claude Code uses the hooks.
