# N3MemoryCore MCP — Lite (Ephemeral)

**A working memory for Claude that forgets by itself after 7 days.**

In Claude Code, hooks save every message and reply and bring the related ones
back on every turn; no tool call is needed. The Claude desktop app and other
MCP applications reach the same memory through MCP. Everything is kept in a
Redis Stack that you run, and every entry is deleted 7 days after it was
saved.

1. Start Redis Stack

   ```bash
   docker run -d --name redis-stack -p 127.0.0.1:6379:6379 redis/redis-stack-server:latest
   ```

2. Put only the specification `N3MemoryCore_MCP_Spec_EN.md` into an empty folder and open Claude Code in that folder
3. Ask: "implement n3mcmcp-lite per this spec."
4. When it is done, restart Claude Code

Memory works from the next session. The steps are in
[Quickstart](#step-1--start-redis-stack). To install with pip or the plugin
instead (bonus), and for the Claude desktop app, see
[Method B: Install the package (bonus)](#method-b-install-the-package-bonus).

> Upgrading from v1.12.2? What changes for existing users is in
> [What changed in v2.0.0](#what-changed-in-v200).

> A NeuralNexusNote™ product — **free Lite** build: ephemeral hybrid
> (vector + BM25) memory backed by Redis Stack, with a 7-day TTL per entry.
>
> 🇯🇵 **[日本語版はこちら](./README_JP.md)**
> 🛡️ **[Development Philosophy](./PHILOSOPHY.md)**

**Where to go**

| You are… | Read |
|---|---|
| new here | [Quickstart](#step-1--start-redis-stack), then [What Claude receives](#what-claude-receives) |
| coming from v1.x | [What changed in v2.0.0](#what-changed-in-v200) |
| choosing between the two free builds | [Lite or Free?](#lite-or-free-which-one-to-use) |
| asking where your data is | [Your Memory Stays on Machines You Control](#your-memory-stays-on-machines-you-control), [Data location](#data-location) |
| changing settings | [Configuration](#configuration), [Ranking formula](#ranking-formula) |
| stuck | [Troubleshooting](#troubleshooting), [Limits](#limits) |

## What is this?

`n3memorycore-mcp-lite` gives Claude short-lived memory across
conversations. It stores text in a Redis Stack that you run, with a BM25
full-text index and a 768-dimension vector index
([`intfloat/multilingual-e5-base`](https://huggingface.co/intfloat/multilingual-e5-base)),
and returns hybrid-ranked results. Every entry is deleted by Redis 7 days
after it was saved.

From v2.0.0, MCP-Lite is "N3MC-Free + MCP, with Redis as the database": the
search and save rules of Lite v1.12.2, plus the hooks, the resident server
and the CLI of [N3MC Free](https://github.com/NeuralNexusNote/n3mc-free),
plus a thin MCP entry for applications without hooks. Free and Lite keep
separate search and save rules. See [MODIFICATIONS.md](./MODIFICATIONS.md).

```
Claude Code ──hooks / CLI (HTTP)─────────────────────────┐
                                                         ├─▶ resident server (127.0.0.1:18524, model loaded once) ─▶ Redis Stack
Claude desktop app ─(MCP, stdio)─▶ n3mc-workingmemory ───┘
                                   (thin client, no model)
```

| Application | Connects through | Registered by |
|---|---|---|
| Claude Code | **the hooks only** (`UserPromptSubmit`, `Stop`) and the CLI `n3mc-lite` | the plugin, or `n3mc-lite --init` |
| Claude desktop app and other applications without hooks | **MCP only** (the thin client `n3mc-workingmemory`) | you, in the application's configuration file |

- **Do not register the MCP server in Claude Code.** Claude Code gets its
  memory from the hooks and the CLI.
- Both paths reach **the same resident server and the same Redis**: what is
  said in Claude Code is found from the desktop app, and what the desktop
  app saves is found in Claude Code.

## Lite or Free? Which one to use

There are two free N3MemoryCore builds. **If you only use Claude Code and
want memory that stays, [N3MC Free](https://github.com/NeuralNexusNote/n3mc-free)
is easier: it needs no Docker.**

| | **N3MC MCP Lite** (this repository) | **[N3MC Free](https://github.com/NeuralNexusNote/n3mc-free)** |
|---|---|---|
| Works with | Claude Code (hooks), and the Claude desktop app or other MCP applications (MCP) | Claude Code only |
| What gets saved | Claude Code: every message and reply, automatically (hooks). Other applications: what the AI decides to save (`save_memory`) | Every message, automatically (hooks) |
| How long it is kept | 7 days, then deleted automatically | Until you delete it |
| What you need | Python 3.10+ and Docker (Redis Stack) | Python 3.10+ |
| Setup | Start Redis Stack, install the plugin (or `pip install` and `n3mc-lite --init`); for the desktop app, add the MCP entry to its config | `pip install n3memorycore-free`, then `n3mc --init` |
| Good for | Short-lived working memory; one memory shared by Claude Code and the desktop app | Long-term memory for Claude Code |

---

## What changed in v2.0.0

v2.0.0 is a major version: the way Lite connects to Claude Code changed. Up
to v1.12.2 Lite was an MCP server only, and the model decided what to save.

| Before (v1.12.2) | After (v2.0.0) |
|---|---|
| Claude Code showed the seven `mcp__…n3mc-workingmemory__*` tools | **Claude Code no longer shows the MCP tools.** The plugin does not register the MCP server any more |
| Only what the model chose to pass to `save_memory` was saved | **Every non-empty message and reply is saved by the hooks** (fenced code blocks as `[code omitted]`), as `[user]` / `[claude]` records |
| The model called `search_memory` at the start of a turn | The `UserPromptSubmit` hook searches with every message and puts the result into Claude's context under `# Recalled Memory Context` |
| Search, list, recall and delete were MCP tools | In Claude Code the CLI gives the same: `n3mc-lite --search`, `--list`, `--recall-thread`, `--delete`, `--delete-session`, `--buffer`, `--repair`. The text they print is the tool's text |
| Each application started its own MCP server and loaded the model (about 1.1 GB) | One resident server loads the model once; the MCP entry is a thin client that never loads it |
| No process ran between sessions | The resident server keeps running in the background after it was started |
| pip users: register the MCP server | pip users: run `n3mc-lite --init` once to register the hooks. An old `n3mc-workingmemory` registration in Claude Code is no longer used and should be removed |
| The Redis command was `docker run … -p 6379:6379 …` | It is `docker run … -p 127.0.0.1:6379:6379 …`, so the memory is not reachable from the network. A container you already created keeps its setting; recreating it (`docker rm -f redis-stack`, then the new command) loses its memories, like any Redis restart |
| The MCP instructions named `n3mc-longtermmemory` as the persistent memory | They name `n3mc-pro`, `n3mc-pg` and `n3mc-longtermmemory` (N3MC MCP Pro, its PostgreSQL edition and the earlier MCP Pro) |
| An old plugin or `.mcp.json` started `uvx --from n3memorycore-mcp-lite n3mc-workingmemory` without a version | Update the plugin. Until then such a registration starts the v2.0.0 MCP entry in Claude Code without hooks |
| `N3MC_SESSION_ID` grouped a Claude Code session's saves | Hook records carry Claude Code's own session id; `N3MC_SESSION_ID` still applies to MCP |
| `N3MC_REDIS_URL` in an MCP client's `env` chose the Redis | Put `redis_url` into `config.json`: the shared server uses the environment of the process that started it |
| `skip_code_blocks: true` rejected saves with code | It also skips every hook message or reply that contains a code block |
| — | The search result of `search_memory` is kept within `context_max_chars` (9,500 characters by default) |

**What did not change**: the PyPI name `n3memorycore-mcp-lite`, the Python
package `n3mc_mcp`, the MCP command and server name `n3mc-workingmemory`
(alias `n3mc-mcp-lite`), the seven tools with the same names, arguments and
output texts (the `search_memory` text is now kept within `context_max_chars`),
the Redis layout and settings (`redis_url`, `N3MC_REDIS_URL`), the 7-day TTL,
the data folder `~/.n3mc-workingmemory` (`N3MC_DATA_DIR`), the
plugin name `n3mc-workingmemory`, and every search and save rule. Records
saved by v1.12.2 stay readable (same index, same fields).

**What existing users do after updating to v2.0.0**

- **Plugin users**: the plugin registers the hooks instead of the MCP
  server. `uv` must still be on `PATH`.
- **pip users**: run `n3mc-lite --init` once, then restart Claude Code.
  Remove the old MCP registration from Claude Code:
  `claude mcp remove n3mc-workingmemory -s user`, or remove the
  `n3mc-workingmemory` entry from a project's `.mcp.json`. `--init` reports
  such a registration but does not edit those files. If it stays, Claude
  Code shows the tools again next to the hooks.
- **Claude desktop app users**: the entry stays `n3mc-workingmemory`. v2.0.0
  asks for the absolute path of the command and an `env` block (see
  [Claude desktop app](#step-3--claude-desktop-app-optional)). Setup shows
  the entry; you add it yourself (setup does not edit the app's file).
- **New files in the data folder**: `audit.log` (the raw text of every hook
  call, with no TTL), `server.log`, `notices.json` and a few small process
  files. See [Data location](#data-location).

---

## 🚀 Quickstart

There are two ways to set it up. Method A, generating the code from the specification, is the primary one; Method B, installing the package, is a bonus. Both start with Step 1, starting Redis Stack.

### Step 1 — Start Redis Stack

```bash
# First time only (creates the container):
docker run -d --name redis-stack -p 127.0.0.1:6379:6379 redis/redis-stack-server:latest

# Every subsequent session (container already exists):
docker start redis-stack
```

Re-running the `docker run` command after the container exists fails with
`Conflict. The container name "/redis-stack" is already in use`. Use
`docker start redis-stack` from the second session onward.

### Method A: Generate from the spec (primary, recommended)

The N3MC series is specification first: the specification is written so that
an AI can generate the implementation from it. To do that, open Claude Code
in an **empty folder** (for example `~/n3mcmcp-lite`) that holds only
`N3MemoryCore_MCP_Spec_EN.md` (or `_JP.md`), start Redis Stack, and ask
"implement n3mcmcp-lite per this spec." The AI first checks the folder,
Python and Redis, then generates the package, runs `pip install -e .`,
registers the hooks with `n3mc-lite --init`, shows the MCP entry for the
Claude desktop app if it is installed (you add it to the app's file
yourself), and checks that everything starts.
The folder stays in use afterwards: deleting or moving it stops the memory.
The four-phase workflow is in Appendix B of the specification.

Then restart Claude Code, as in [Step 4 — Restart](#step-4--restart).

### Method B: Install the package (bonus)

#### Step 2 — Claude Code (hooks)

Choose one. **Do not register the MCP server in Claude Code**
(`claude mcp add`, `~/.claude.json`, `.mcp.json`).

**(a) Plugin marketplace (recommended — no config file to edit).** Requires
[`uv`](https://docs.astral.sh/uv/) on `PATH`. Run inside Claude Code:

```
/plugin marketplace add NeuralNexusNote/n3mcmcp-lite
/plugin install n3mc-workingmemory@neuralnexusnote
```

The plugin registers three hooks: `SessionStart` (pre-approves the CLI
commands), and `UserPromptSubmit` and `Stop`, which run through
`uvx --from n3memorycore-mcp-lite==2.0.1 …` (the version is pinned to the
package version).

**(b) pip.**

```bash
pip install n3memorycore-mcp-lite
n3mc-lite --init
```

`n3mc-lite --init` registers the two hooks in `~/.claude/settings.json` with
the absolute paths of `n3mc-lite-hook` / `n3mc-lite-stop-hook`, keeps every
other entry (including other N3MC products' hooks), backs the file up before
writing, and adds the permission rules for the CLI. When the plugin is enabled, it
adds no hooks (the plugin's hooks are used).

Use only one of (a) and (b): with both, every message is searched twice.

#### Step 3 — Claude desktop app (optional)

Open the config file from the app (Settings → Developer → Edit Config; the
usual place on Windows is `%APPDATA%\Claude\claude_desktop_config.json`, and
the Microsoft Store version of the app may keep it in another folder). Add
the entry under `mcpServers`, keep the entries already there, save, and
**quit the app completely and start it again**.

pip installation:

```json
{
  "mcpServers": {
    "n3mc-workingmemory": {
      "command": "C:\\Users\\<you>\\AppData\\Local\\Programs\\Python\\Python312\\Scripts\\n3mc-workingmemory.exe",
      "args": [],
      "env": {
        "N3MC_AGENT_NAME": "claude-desktop"
      }
    }
  }
}
```

Plugin users (no pip installation):

```json
{
  "mcpServers": {
    "n3mc-workingmemory": {
      "command": "uvx",
      "args": ["--from", "n3memorycore-mcp-lite==2.0.1", "n3mc-workingmemory"],
      "env": {
        "N3MC_AGENT_NAME": "claude-desktop"
      }
    }
  }
}
```

- `command`: **the absolute path** of `n3mc-workingmemory` from the same
  scripts folder the hooks use (Windows: `where n3mc-workingmemory`; Linux:
  `which n3mc-workingmemory`). The app does not use the `PATH` of your
  terminal. The hooks and the MCP entry must run the same code (see
  [Limits](#limits)).
- `env.N3MC_AGENT_NAME`: the name stored as `agent_name` when the model does
  not pass one. Use one name per application.
- When the hooks use another data folder (`N3MC_DATA_DIR`), add the same
  `N3MC_DATA_DIR` to `env`.
- `N3MC_SESSION_ID` may be added to pin the default `session_id` of this
  application.
- Other MCP clients on Windows or Linux are registered the same way
  (command, args, env).

#### Step 4 — Restart

Restart Claude Code (and the desktop app). Memory starts with the next
session. On the first start the resident server downloads and loads the
embedding model, about 1.1 GB (later starts use the cache). The hooks do not wait
for it: until the model is ready, the memory search uses keywords only and
says so in a notice, and messages are saved without vectors, which are added
later automatically. The MCP tools and the CLI wait up to 20 seconds for the
model.

---

## ⚠️ Prerequisites

| Item | Requirement |
|---|---|
| Python | 3.10 or higher |
| Client | Claude Code (hooks), and/or the Claude desktop app or another MCP client (MCP) |
| Redis Stack | Running on `localhost:6379` (or any URL set with `N3MC_REDIS_URL`) |
| pip packages (installed automatically) | `mcp` (`>=1.0.0,<2.0.0`), `redis` (`<8.0.0`), `sentence-transformers`, `uuid-utils`, `platformdirs`, `numpy`, and from v2.0.0 `fastapi`, `uvicorn`, `pydantic`, `psutil` |
| `uv` | Only for the plugin and the `uvx` form |
| OS | Windows 11 or Linux. macOS is not a target |

> **Why no persistence flags on the docker line**: this build is
> *deliberately volatile*. The resident server turns Redis persistence off
> itself (`CONFIG SET appendonly no` and `CONFIG SET save ""`) when it
> connects, so a manual re-enable is reverted on the next Lite run. The
> `--save ""` argument is not put on the `docker run` line because its empty
> string breaks in Windows PowerShell and cmd.exe quoting.
>
> **Use a dedicated Redis.** The server changes these settings only when the
> Redis holds nothing but N3MC keys. If it finds other data, it leaves the
> persistence settings alone and shows a `shared_redis` notice. N3MC Lite
> also needs **Redis Stack** (RediSearch); with plain Redis every tool
> answers with a hint to start Redis Stack.

Without Redis, the hooks do not fail: they keep the text in `audit.log`
only and show a notice telling you to start Redis Stack. Nothing is queued
for resending.

---

## Features

- 💾 **Your own Redis** — Your memories are kept in the Redis you run (on your PC by default), not in any cloud service. Only the memories recalled for a turn are sent to the AI service, as part of the conversation ([details](#your-memory-stays-on-machines-you-control)).
- ⚡ **Automatic in Claude Code** — The hooks save every non-empty message and reply and search with every message, whatever the model decides. No tool call is needed.
- 🔗 **One memory for Claude Code and the desktop app** — Both reach the same resident server and the same Redis.
- 🔍 **Semantic search** — Finds relevant past conversations even when the exact words differ.
- 🌐 **Multilingual out of the box** — CPU-only, no LLM/GPU required. NFKC fold (`ｱﾙﾌｧ`↔`アルファ`, `１２３`↔`123`, ligatures), bigram coverage for Japanese / Chinese / Korean / Thai / Lao / Myanmar / Khmer, diacritic cross-match for Latin scripts (`café`↔`cafe`).
- 🛡️ **Encoding safety** — UTF-8 reconfigure of stdin/stdout/stderr on Windows (cp932 → UTF-8), lone-surrogate sanitization on every input.
- 🔄 **Context across sessions** — Working memory that lasts **7 days** (auto-expires via Redis TTL).
- 🧠 **Model loaded once** — One resident server loads the embedding model for the hooks, the CLI and every MCP client.
- 🏢 **One person, one memory** — Memories are not shared between people: each `config.json` has its own `owner_id`, and search shows only that owner's records. Use one Redis per installation.
- 🧹 **Ephemerality is a design feature** — 7-day auto-expiry means failed attempts and abandoned designs don't bleed into the next task.
- 💰 **Reduces token waste** — Memory search uses local embeddings (`intfloat/multilingual-e5-base`) and costs zero Claude tokens.

## How It Works

**Claude Code (hooks)**

```
User's message
    │
    ▼  UserPromptSubmit hook
    ├─ 1. append the raw input to audit.log
    ├─ 2. add missing vectors (repair, one batch)
    ├─ 3. save Claude's previous reply from the transcript, if the Stop hook missed it
    ├─ 4. search with the message → "# Recalled Memory Context" into Claude's context
    └─ 5. save the message as a [user] record
    │
    ▼
Claude responds
    │
    ▼  Stop hook
    ├─ 1. append the raw input to audit.log
    ├─ 2. save the reply as a [claude] record
    └─ 3. housekeeping: keep .claude/rules/n3mc-lite-behavior.md up to date, rotate audit.log
```

The search runs before the message is saved, so a message never finds
itself. The hooks always exit 0: when the server or Redis is down, the text
is safe in `audit.log` and a notice tells you what to do. Claude Code gets its
guidance from the rules file `.claude/rules/n3mc-lite-behavior.md`, which the
Stop hook writes into the project folder (a file you edited by hand is not
replaced). Input that Claude Code itself injects (task notifications, agent
messages, system reminders) is neither saved nor searched.

**Claude desktop app and other MCP applications**

The thin MCP entry answers `initialize` with **behavioral instructions**
(search at the start of each turn, save after each meaningful exchange) and
forwards every tool call to the resident server. In these applications,
saving is up to the model — see
[On compliance](#on-compliance--mcp-can-persuade-not-force).

### Three scenes

> **Monday** — In Claude Code you decide to use PostgreSQL for the website, and say why. The hooks save the exchange.
>
> **Tuesday** — In a new session you ask which database you chose. Monday's decision arrives in Claude's context together with your question.
>
> **Eight days later** — Monday's records are gone: every entry is deleted 7 days after it was saved. That is the design. For memory that stays, use [N3MC Free](https://github.com/NeuralNexusNote/n3mc-free) or a Pro build.

### What Claude receives

This is the block the `UserPromptSubmit` hook added for the Tuesday question. It was captured from a real run of the hooks (a temporary data folder and a throwaway Redis Stack container; the second session was started in a different folder from the first).

```text
# Recalled Memory Context
Source: N3MC Lite (7-day working memory)
Search query: Which database did we decide to use for the website, and why?

_If several versions of the same content appear, the one with the newest `saved` time is the current one._

### [01a2020d-c67a-7373-93aa-39a0db176d4f] turn=8f3d1d47-3d9d-4b02-a9ae-a3236124c27b score=0.199 saved=2026-11-22 15:59 (UTC+0900)
[user] Let's use PostgreSQL for the website's database. Several people will edit at the same time, and SQLite locks the whole file on every write.

#### Possibly related (weak matches — may be unrelated)

### [01a2020d-e3a3-7a63-a2f9-1a273e8847f0] turn=8f3d1d47-3d9d-4b02-a9ae-a3236124c27b score=0.409 saved=2026-11-22 15:59 (UTC+0900)
[claude] Agreed, PostgreSQL it is. The reason, for the record: several editors at the same time, and SQLite allows one writer at a time. Next step: write the schema for the articles table.
```

Each hit starts with `### [<id>]` and shows its `turn=`, `score=` and `saved=` time. Hits under `Possibly related` did not pass the relevance floors (see `min_results` in [Configuration](#configuration)).

### Relationship with Claude's built-in auto-memory

Claude Code has a built-in auto-memory system
(`~/.claude/projects/.../memory/`). N3MemoryCore **complements it rather
than competing with it**.

|                 | Claude auto-memory                                      | N3MemoryCore RAG                                     |
| --------------- | ------------------------------------------------------- | ---------------------------------------------------- |
| **Strengths**   | Reliable, loads every session, great for fixed facts    | Conversation context, detailed history               |
| **Weaknesses**  | Cannot capture conversation flow or context             | Depends on search quality; not guaranteed to surface |
| **Best for**    | User profile, folder paths, stable settings             | Conversation threads, past decisions, reasoning      |

- **Fixed information needed every session** (folder paths, user preferences) → auto-memory
- **Conversation context and history** (discussion threads, past decisions) → N3MemoryCore (7-day window)

---

## Your Memory Stays on Machines You Control

N3MC Lite keeps your memories in the Redis Stack you run: by default the one
on your own PC, or a Redis server you choose.

- **Your Redis, not an N3MC cloud** — Memories are stored in the Redis set by `redis_url` (default `redis://localhost:6379/0`; the `N3MC_REDIS_URL` environment variable takes precedence). There is no N3MC account and no N3MC cloud service. Each entry expires 7 days after it was saved (`ttl_seconds`).
- **The resident server listens on your PC only** — It listens on `127.0.0.1:18524` (`bind_host`, `server_port`). It has no authentication, so **keep `bind_host` at `127.0.0.1`**: any other value exposes the memory to the network (the server prints a warning). Requests whose `Host` header is not `127.0.0.1` / `localhost` are refused. The MCP entry talks to your application over stdio and opens no port. The CLI and the hooks never go through a proxy.
- **What is on disk** — Besides Redis, the data folder holds `config.json`, and since v2.0.0 **`audit.log`: the raw text of every hook call — every prompt and reply in full, including code blocks and pasted text — with no TTL** (rotated at 50 MB, at most about 100 MB). Treat it as private data. It is the last-resort copy for a message that could not reach Redis; nothing reads it back into the memory. See [Data location](#data-location).
- **Embeddings are made on your PC** — The embedding model runs locally on the CPU, and the search runs inside your Redis. Searching calls no external API.
- **No telemetry** — N3MC sends no usage data anywhere. The code is open, so you can check this yourself.
- **You can see and remove your memories** — `n3mc-lite --list` / `list_memories` show the newest entries; `--delete` / `delete_memory` and `--delete-session` / `delete_memories_by_session` remove them from Redis. The raw text of every hook call also stays in `audit.log` in the data folder (no TTL, rotated at 50 MB): delete that file yourself to remove it.

What does go over the network:

- **Installing** — `pip` and `uvx` download the package and its dependencies from PyPI; the Claude Code marketplace path fetches the plugin from the `NeuralNexusNote/n3mcmcp-lite` GitHub repository.
- **The Redis Stack image** — The first `docker run` downloads `redis/redis-stack-server` from Docker Hub.
- **The embedding model download** — On the first start, `intfloat/multilingual-e5-base` (about 1.1 GB) is downloaded from Hugging Face.
- **A Redis on another machine** — If you point `redis_url` / `N3MC_REDIS_URL` at another machine, every memory you save and every search goes over the network to that machine.
- **What the AI reads** — The `# Recalled Memory Context` block and the results of `search_memory` become part of the conversation, so they are sent, together with your message, to the AI service behind your application (Anthropic, for Claude). N3MC does not upload your Redis contents anywhere.

## Use cases — when working memory is the right tool

The 7-day TTL and volatile Redis storage are **design features, not
limitations**. They make Lite the right fit for:

- **Agentic code-generation loops** — failed attempts and abandoned
  designs don't bleed into the next task.
- **Experimental / throwaway prototyping** — leave it alone and memory
  evaporates in 7 days, no pruning needed.
- **Project-scoped working memory** — pin a `session_id` per task /
  project to keep contexts separated.
- **Switching between Claude Code and the desktop app** during one piece of
  work.

If you need **long-term, persistent memory**, working memory is not the
right layer: use the Pro build (persistent storage) or keep an external
copy. Lite forbids any "persistence hack" (RDB snapshots, AOF, external
dumps) to get around the TTL.

## Tools and CLI commands

The seven tools (MCP) and the CLI commands (Claude Code) call the same
handlers in the resident server and give the same text. The CLI prints it
without the auto-save reminder, because in Claude Code the hooks save;
`--search` prints the same block as the hook, with the
`# Recalled Memory Context` heading.

| Tool (MCP) | CLI (`n3mc-lite`) | Purpose |
|---|---|---|
| `search_memory` | `--search "<keywords>" [--since S] [--until U] [--limit N]` | Hybrid (vector + BM25) search, ranked & time-decayed, `session_id` boost |
| `save_memory` | `--buffer "<text>"` or `--buffer -` (stdin) | Save an entry (7-day TTL, exact duplicates rejected) |
| `list_memories` | `--list [--limit N]` | Most-recent entries, newest first |
| `recall_thread` | `--recall-thread <turn_id> [--before N] [--after N]` | The entries around a hit's `turn_id` |
| `delete_memory` | `--delete <id>` | Remove one entry (cascades to chunks if the id is a parent document) |
| `delete_memories_by_session` | `--delete-session <session_id>` | Bulk-delete every memory tied to a `session_id` |
| `repair_memory` | `--repair` | Re-create the index if missing and add missing vectors (up to 200 per call) |

- In Claude Code, `--search`, `--buffer`, `--list`, `--recall-thread`,
  `--repair` and `--stop` are pre-approved (`permissions.allow`), in the
  `python -m n3mc_mcp.cli` and `n3mc-lite` forms (pip) or the
  `uvx --from n3memorycore-mcp-lite==2.0.1 n3mc-lite` form (plugin).
- **`--delete` and `--delete-session` are never pre-approved**: they are
  irreversible, so Claude Code asks you each time.
- A manual `--search` is not limited in size; the hook's block and the MCP
  `search_memory` result are kept within `context_max_chars`.

## ID hierarchy

N3MemoryCore identifies the origin and context of every record with
five ID fields. Most users only ever touch `session_id` (and rarely
`agent_name`); the rest are filled in automatically.

| ID                    | Stored in                       | Generated                                  | Granularity                            | Purpose |
|-----------------------|---------------------------------|--------------------------------------------|----------------------------------------|---------|
| `id` (PK)             | Redis hash                      | Per record (UUIDv7, time-ordered)          | **One record**                         | Unique identifier for each memory — used for `delete_memory` and dedup. |
| `owner_id`            | `config.json`                   | First startup (UUIDv4)                     | **Owner / installation**               | Identifies whose data this is. Validated on every `save_memory`; mismatched payloads are rejected with `owner_id mismatch`. |
| `local_id` (agent_id) | `config.json`                   | First startup (UUIDv4)                     | **Agent / install**                    | Stored on every row, but **does not feed Lite's `b_local` ranking** — `b_local` is computed from `stored_importance + access_count` only. |
| `session_id`          | Redis hash                      | Per task / project / conversation (string) | **Task / project / conversation**      | Drives the **`b_session` ranking bias** (`b_session_match=1.0`, `b_session_mismatch=0.6`) and is the filter key for `delete_memories_by_session`. Hook records: Claude Code's `session_id`. MCP: per-call argument → `N3MC_SESSION_ID` → a UUIDv4 made when the MCP entry started. |
| `agent_name`          | Redis hash                      | Per save (free-form string)                | **Agent display label**                | Hook records: always `"claude-code"`. MCP: the argument, else `N3MC_AGENT_NAME`, else the client's name from `initialize`. Not used in ranking. |

Hook records also carry a `turn_id` that ties a message to its reply; pass
it to `--recall-thread` / `recall_thread` to read the conversation around a
hit.

**Practical guidance:**

- **Pin `session_id`** when you work on a named project through MCP: pass
  the same string (e.g. `"proj-alpha"`) to `save_memory` and
  `search_memory`, or set `N3MC_SESSION_ID`.
- **You should not pass `owner_id`** unless you need to prove ownership.
- **Use one Redis per installation.** If several PCs or people point at
  the same Redis, or `config.json` is re-created with a new `owner_id` while
  old records remain: a body one owner saved is rejected as `duplicate` for
  the other until it expires (7 days), and one owner's records can fill the
  search candidates so the other's are not found. Give each installation its
  own Redis.

## Data location

Memories live in Redis and expire automatically. The data folder is
`~/.n3mc-workingmemory/` (Windows: `%USERPROFILE%\.n3mc-workingmemory\`);
override it with the `N3MC_DATA_DIR` environment variable (absolute path).

| File | What it is |
|---|---|
| `config.json` | Configuration (up to v1.12.2 the only file) |
| `audit.log`, `audit.log.1` | (v2.0.0) JSONL record of every hook call, written before anything else: raw prompts and replies, **with no TTL**. Rotated at 50 MB, so at most about 100 MB is kept |
| `n3mc-lite.pid` | (v2.0.0) PID file of the resident server (exists only while it runs) |
| `server.log` | (v2.0.0) stderr of the resident server (truncated when over 1 MB at a start) |
| `server_start_failed` | (v2.0.0) marker of a failed server start (5-minute back-off) |
| `notices.json` | (v2.0.0) notices that need your action, shown once a day |
| `turn_id_<session>.txt` | (v2.0.0) the `turn_id` handed from the UserPromptSubmit hook to the Stop hook; files older than 30 days are removed |

Since v1.10.0 the folder is in the home directory instead of the platform
app-data directory: the Microsoft Store build of the Claude desktop app
redirects `%LOCALAPPDATA%` for the processes it starts, which gave one PC
two configs and two `owner_id`s. On first start the old config is copied
over once (the old file is left in place).

Not designed to be backed up: entries vanish on a 7-day rolling window.
`config.json` (the `owner_id` / `local_id`) is the only file that matters
for identity.

## Configuration

On first run, `config.json` is auto-generated with random UUIDs for
`owner_id` and `local_id`. Editable defaults:

```json
{
  "owner_id":                 "<uuid>",
  "local_id":                 "<uuid>",
  "redis_url":                "redis://localhost:6379/0",
  "ttl_seconds":              604800,
  "half_life_days":           3,
  "bm25_min_threshold":       0.1,
  "search_result_limit":      20,
  "min_score":                0.2,
  "vector_min_cos":           0.85,
  "relative_min_score_ratio": 0.6,
  "min_results":              3,
  "version_order_enabled":    true,
  "version_sim_threshold":    0.98,
  "version_min_chars":        100,
  "search_query_max_chars":   2000,
  "chunk_threshold":          400,
  "chunk_overlap":            100,
  "access_count_enabled":     false,
  "access_count_weight":      0.02,
  "access_count_max_boost":   0.5,
  "ttl_refresh_top_k":        5,
  "lexical_rerank_enabled":   true,
  "rerank_weight":            0.3,
  "rerank_phrase_weight":     0.2,
  "b_session_match":          1.0,
  "b_session_mismatch":       0.6,
  "skip_code_blocks":         false,
  "embedding_model":          "intfloat/multilingual-e5-base",
  "embedding_dim":            768,
  "server_port":              18524,
  "bind_host":                "127.0.0.1",
  "context_max_chars":        9500
}
```

- `redis_url` — connection URL; `N3MC_REDIS_URL` env var takes precedence.
- `ttl_seconds` — TTL on every new memory and sha-guard (default 7 d).
- `vector_min_cos` — how close (cosine) a record must be to the query to become a vector candidate (default `0.85`). A sensible range is 0.80–0.88 (lower: more recall, more loosely related rows; higher: shorter results). 0.85 was measured with `multilingual-e5-base`, so re-measure it if you change `embedding_model`.
- `relative_min_score_ratio` — drop results scoring below this ratio × the top score (default `0.6`; `0.0` disables).
- `min_results` — when fewer results than this pass the floors, the search adds the closest of the rows it dropped under a separate heading `#### Possibly related (weak matches — may be unrelated)` (default `3`; `0` turns it off). Weak rows are not counted in `access_count`.
- `version_order_enabled` / `version_sim_threshold` / `version_min_chars` — results whose content is ≥ 0.98 similar are treated as versions and listed newest first. Each hit shows its `saved=` time.
- `chunk_threshold` / `chunk_overlap` — sliding-window size and overlap (chars). Bodies longer than the threshold are stored as a parent document plus chunks, and a hit returns the whole text.
- `access_count_*` — access-frequency boost; **off by default since 2026-11-17** (`access_count_enabled: false`); the hits are still counted.
- `ttl_refresh_top_k` — the number of top hits whose `access_count` goes up on each search. The name is kept from earlier versions: a search never resets a TTL. (`ttl_refresh_on_search` was removed on 2026-11-22; a value left in your `config.json` is ignored.)
- `lexical_rerank_*` / `rerank_weight` / `rerank_phrase_weight` — lightweight post-fusion lexical reranker (CPU-only).
- `b_session_match` / `b_session_mismatch` — ranking factor for rows whose stored `session_id` matches (default `1.0`) or not (`0.6`). Set both to `1.0` to disable the bias.
- `skip_code_blocks` — when `true`, content containing a triple-backtick fence is not saved (`status: "skipped_code"`), also for hook saves. Default `false`: the body is saved with each code block replaced by `[code omitted]`.
- `embedding_model` / `embedding_dim` — the embedding model and its vector dimension; they must match. Changing either requires a restart and an index flush.
- `server_port` (v2.0.0) — the port of the resident server (default `18524`). N3MC-Free uses 18520, Pro 18521, the PostgreSQL edition 18522 and the Ollama edition 18523; keep a port of its own for each N3MC product on one PC.
- `bind_host` (v2.0.0) — keep the default `127.0.0.1`.
- `context_max_chars` (v2.0.0) — the largest size of the hook's `# Recalled Memory Context` block and of the MCP `search_memory` result (default `9500`, allowed 2000–10000). Claude Code moves hook output over 10,000 characters to a file and shows only a 2,000-character preview. When the block is too long, lower-ranked rows are left out first, then the text is cut; a manual `--search` shows everything.
- **When a change takes effect**: editing `config.json` restarts the resident server on the next hook, CLI or MCP call. If `config.json` has a typo and cannot be read, it is left untouched and defaults are used; the first search result says so. Write numbers without quotes.

See §6 of the specification for the complete reference.

## Multilingual support

Built-in, CPU-only, no LLM and no GPU required. Search and dedup behave
the same regardless of how the user types the same word:

| Layer | What it does | Real-world example |
|---|---|---|
| **NFKC normalization** | Folds compatibility forms before SHA / embedding / BM25 | `ｱﾙﾌｧ` ↔ `アルファ`, `１２３` ↔ `123`, `ﬁ` ↔ `fi` |
| **Bigram BM25 side channel** | Overlapping bigrams emitted for space-less scripts | `記憶装置` → `記憶 憶装 装置`; same for Korean (`안녕하세요`), Thai (`สวัสดี`), Lao, Myanmar, Khmer |
| **Diacritic fold** | Latin/Greek/Cyrillic words also indexed without combining marks | `café` matches `cafe`, `Ångström` matches `Angstrom` |
| **multilingual-e5-base embedding** | Multilingual semantic space across 100+ languages | Cross-language paraphrase retrieval |

These run on every save and search. None of them rewrites the stored
`content`: a long body comes back whole, as it was saved.

## Encoding safety

1. **UTF-8 reconfigure** — the MCP entry, the hook entries and the CLI
   switch `sys.stdin` / `sys.stdout` / `sys.stderr` to UTF-8 at start. On
   Windows-Japanese hosts the default console code page is cp932, which
   would otherwise mangle every non-ASCII byte. POSIX systems are already
   UTF-8, so the call is a safe no-op.
2. **Lone-surrogate sanitization** — every saved text, every query and the
   whole hook input are passed through `sanitize_surrogates()`. Lone UTF-16
   surrogate halves (`U+D800`–`U+DFFF`) can appear when Windows pipes
   deliver bytes Python cannot decode; without the guard the write would
   fail.

Content that becomes empty, or has no visible character, is rejected with
`empty content` / `empty content (no visible characters)`.

Two rules apply to everything that is saved:

- **Outer whitespace is trimmed once** — `' \nAAA\n '` is saved as `AAA`.
- **Code blocks are stored as `[code omitted]`** — each fenced block
  (```` ``` ```` … ```` ``` ````, or an unclosed fence to the end) is
  replaced before saving; inline backticks stay. N3MemoryCore remembers what
  was said and decided; the code belongs in your project's files.

Hook records start with a role prefix: `[user] ` or `[claude] ` (chunks of a
long text: `[user 2/5] ` and so on).

## Ranking formula

```
final_score = (0.7 * cosine_similarity + 0.3 * keyword_relevance) * time_decay * b_local * b_session

time_decay   = 2 ^ (-days_elapsed / half_life_days)       (default half-life: 3 days)
b_local      = clamp(0.5, 2.0, stored_importance + access_boost)
access_boost = min(0.5, access_count * 0.02)               (only when access_count_enabled)
b_session    = b_session_match (default 1.0)   if row.session_id == effective_session
             = b_session_mismatch (default 0.6) otherwise
```

With a 3-day half-life (shorter than the 7-day TTL), `time_decay` matters
in Lite: a fresh memory scores 1.0, a 3-day-old one 0.5, and a 7-day-old
one ≈ 0.20.

The access-frequency boost is off by default since 2026-11-17: measured in
N3MC-Pro, it lowered recall@3 (often-returned memories rose above more
relevant ones).

## Coexistence with other N3MC products

Lite can be installed on the same PC as N3MC-Free, N3MC-Pro and the MCP Pro
/ PostgreSQL / Ollama editions. Nothing is shared: Lite has its own data
folder (`~/.n3mc-workingmemory`), its own database (Redis), its own server
port (18524), its own hook commands (`n3mc-lite-hook`,
`n3mc-lite-stop-hook`), its own CLI (`n3mc-lite`) and its own rules file
(`.claude/rules/n3mc-lite-behavior.md`).

- `n3mc-lite --init` keeps the other products' hooks and reports them.
- When two products' hooks are installed, Claude gets one
  `# Recalled Memory Context` block from each, each from its own memory.
- When another N3MC server holds Lite's port, Lite never uses or stops it:
  it saves without vectors, does not search, and shows a `port_conflict`
  notice asking you to give Lite its own `server_port`.
- When a persistent N3MC memory is connected over MCP in the same
  application — `n3mc-pro` (N3MC MCP Pro), `n3mc-pg` (its PostgreSQL edition)
  or `n3mc-longtermmemory` (the earlier MCP Pro) — the MCP instructions treat
  Lite as the working memory.

## Limits

- **7 days.** Every entry is deleted 7 days after it was saved. A search does
  not extend it. `audit.log` is the exception: it has no TTL.
- **In applications without hooks, saving is up to the model** (see below).
  Full conversations are kept only in Claude Code.
- **The resident server must be able to run.** When it cannot start,
  searches answer with an error or a placeholder, and saves go into Redis
  without a vector (found by keywords at once, vector added later by repair).
- **One installation for all clients.** The hooks, the CLI and the MCP entry
  must run the same version; otherwise each replaces the other's server on
  every call. Register the desktop app with the same scripts folder the
  hooks use, or with the same pinned `uvx` form.
- **Plugin and pip together**: do not use both. `--init` registers no hooks
  while the plugin is enabled, and the plugin reports hooks written by
  `--init`.

## Why N3MemoryCore? (vs. built-in memory)

| Aspect | Built-in memory | N3MemoryCore (Lite) |
|---|---|---|
| **Data ownership** | Vendor-hosted | **Your own Redis Stack on your machine** |
| **Saving in Claude Code** | Up to the model | **Every message and reply, by hooks** |
| **Client surface** | The vendor's product only | **Claude Code (hooks) and any MCP client** |
| **Verbatim recall** | Opaque (may be summarized) | **Parent-document contract — the full text returned as saved (code blocks as `[code omitted]`)** |
| **Search internals** | Black box | **Hybrid BM25 + e5 vectors + CJK bigram + time decay + lightweight reranker, all parameters visible and tunable** |
| **Inspect / control** | UI only | **List, recall, delete and bulk-delete by `session_id` on raw records** |
| **Persistence** | Tied to the vendor's service | **Redis with a 7-day TTL** — short-lived by design, but you own the container |

If you only need "the AI remembers something across sessions" inside one
vendor's product, the built-in memory is simpler.

## On compliance — MCP can persuade, not force

**This section applies to applications without hooks** (the Claude desktop
app and other MCP clients). In Claude Code the hooks save every message and
reply and search with every message, whatever the model decides.

An MCP server cannot make the LLM call its tools. The protocol gives it
three persuasion levers: tool descriptions in `tools/list`, the
`instructions` field sent at session start, and the text of tool responses.
Lite uses all three: every tool response ends with a short reminder. Even
so, **whether the LLM follows through is non-deterministic**: it depends on
the model, on how the client builds its prompt (some clients summarize or
drop `instructions`), and on competing instructions. Most turns save
correctly; short answers and fact corrections are sometimes skipped.

When a save must happen:

- **Ask explicitly** — write "save this to N3MemoryCore" in your prompt.
  LLMs almost always honour explicit requests.
- **Use Claude Code** — its hooks save every exchange.
- **Call the Anthropic Messages API from your own application** — drive
  `tool_use` yourself so that `save_memory` runs every turn. You then have to
  write that application.

## Uninstall

Ask the AI "Please remove N3MC Lite" so that the hook entries are removed
too, or do it yourself:

1. Plugin users: `/plugin uninstall n3mc-workingmemory@neuralnexusnote`.
   pip users: remove the two entries of `~/.claude/settings.json` whose
   command runs `n3mc-lite-hook` / `n3mc-lite-stop-hook`.
2. Stop the resident server: `n3mc-lite --stop-server` (or end the process
   whose command line contains `n3mc_mcp.cli` and `--run-server`).
3. `pip uninstall n3memorycore-mcp-lite` removes the package.
4. `docker rm -f redis-stack` stops and deletes the Redis container and
   erases all stored memories.
5. Delete `~/.n3mc-workingmemory/` (or `N3MC_DATA_DIR`) to remove
   `config.json`, `audit.log` and the other files.
6. Remove the `n3mc-workingmemory` entry from the Claude desktop app's
   config file.

`.claude/rules/n3mc-lite-behavior.md` in your project folders can be deleted
by hand.

## Development

```bash
# Tests FLUSHDB their Redis: use a DEDICATED test container, never your memory Redis
docker run -d --name redis-stack-test --restart=no -p 127.0.0.1:6390:6379 redis/redis-stack-server:latest
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
N3MC_REDIS_TEST_URL=redis://localhost:6390/0 pytest tests/ -q
```

Windows PowerShell:

```powershell
docker run -d --name redis-stack-test --restart=no -p 127.0.0.1:6390:6379 redis/redis-stack-server:latest
python -m venv .venv; .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
$env:N3MC_REDIS_TEST_URL = "redis://localhost:6390/0"; pytest tests/ -q
```

RediSearch refuses to create indexes outside DB 0, so a separate test DB is
not an option — run the tests against a **dedicated** Redis container. The
v2.0.0 test layers (hooks, server API, CLI, MCP client, context limit,
coexistence) are described in §9 of the specification; they are in
`tests/`.

CI runs the tests on every push to `main` and every PR to `main` — see
[`.github/workflows/test.yml`](./.github/workflows/test.yml). Read
[`CONTRIBUTING.md`](./CONTRIBUTING.md) for the developer guide (EN + JP).

## Extending the Lite build

This repository is **public and Apache-2.0 licensed** — fork, modify and
run it freely. If you want to change behavior (the ranking formula, a
cross-encoder reranker, a Japanese morphological tokenizer, …), start from
the specification:

- [`N3MemoryCore_MCP_Spec_EN.md`](./N3MemoryCore_MCP_Spec_EN.md) — full design document (English)
- [`N3MemoryCore_MCP_Spec_JP.md`](./N3MemoryCore_MCP_Spec_JP.md) — 日本語版

Appendix A of the specification lists optional extensions with drop-in
points. If you publish a fork under a new package name, also change the
`name`, `[project.urls]` and console-script names in `pyproject.toml`.

## Troubleshooting

### Windows: `pip install --upgrade` fails with `WinError 32` (file in use)

Symptom:
```
ERROR: Could not install packages due to an OSError: [WinError 32]
The process cannot access the file because it is being used by another process:
'...\Scripts\n3mc-workingmemory.exe' -> '...\Scripts\n3mc-workingmemory.exe.deleteme'
```

Cause: a running process holds a file of the package open — for example
the Claude desktop app running `n3mc-workingmemory.exe`.

Fix: fully quit the Claude desktop app and Claude Code (closing the window
is not enough on Windows; end the processes in Task Manager or sign out
once), stop the resident server (`n3mc-lite --stop-server`), then run
`pip install --upgrade` again.

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
rmdir /s "%LOCALAPPDATA%\Programs\Python\Python312\Lib\site-packages\~3memorycore_mcp_lite-<version>.dist-info"
```

(Adjust the path to match your Python installation.)

### The `# Recalled Memory Context` block says the search is unavailable

The block always shows the state of the memory. `the memory server is still
starting` appears while the resident server starts and does not answer yet —
send the next message a little later. While the first start downloads the
embedding model, the server already answers: the search uses keywords only
and a notice says the model `is still loading`; the vectors are added later
automatically. Other reasons name the cause (`see <server.log path>`,
another N3MC server on the port, Redis unreachable). A `> ⚠️ N3MC notice:`
line tells you what to do, for example `docker start redis-stack`.

## License

Apache License 2.0 — see [LICENSE](./LICENSE). Portions are derived from
N3MC-Free (Apache License 2.0, same author) — see [NOTICE](./NOTICE) and
[MODIFICATIONS.md](./MODIFICATIONS.md).

---

<sub>MCP Registry: `mcp-name: io.github.NeuralNexusNote/n3mc-workingmemory`</sub>
