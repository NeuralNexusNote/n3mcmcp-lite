# N3MemoryCore MCP Lite (n3mcmcp-lite) v2.0.2

**Give Claude Code a working memory that clears itself after 7 days — and the same memory in the Claude desktop app.**

In Claude Code, hooks save every message and reply and bring the related ones
back on every turn; no tool call is needed. The Claude desktop app and other
MCP applications reach the same memory through MCP. Everything is kept in a
Redis Stack that you run, and every entry is deleted 7 days after it was
saved.

1. Start Redis Stack

   ```bash
   docker run -d --name redis-stack -p 127.0.0.1:6379:6379 redis/redis-stack-server:latest
   ```

2. Make a folder: in File Explorer, create one empty folder (for example `C:\Users\<you>\n3mcmcp-lite`)
3. Put the specification `N3MemoryCore_MCP_Spec_EN.md` in that folder, and nothing else
4. Open Claude Code in that folder: in the Claude desktop app, open the **Code** tab, start a new session with **+ New**, choose **Local**, then **Open folder…** and pick the folder (in a terminal, change to that folder and run `claude`)
5. Ask: "Please implement n3mcmcp-lite according to the specification `N3MemoryCore_MCP_Spec_EN.md`."
6. When it is done, restart Claude Code (closing the window may not be enough: quit Claude from its icon under the "^" at the right end of the taskbar, or sign out of Windows once)

After the restart, open the same session and send "I restarted". Claude Code checks that memory works and tells you what to do next.

Memory works from the next session. The steps are in
[Quickstart](#step-1--start-redis-stack). To install with pip or the plugin
instead (bonus), and for the Claude desktop app, see
[Method B: Install the package (bonus)](#method-b-install-the-package-bonus).

> **⚠️ Disclaimer**
>
> - **No support, no claims**: This project is provided as-is with no support. Questions, bug reports, and feature requests are not guaranteed a response. Use entirely at your own discretion.
> - **Use at your own risk**: This system runs on AI-generated code. The author is not responsible for any data changes or issues caused by its use.
> - **Backup first**: Always back up your current environment before running this on important projects.

> Upgrading from v1.12.2? What changes for existing users is in
> `CHANGELOG.md` (version 2.0.0).

> A NeuralNexusNote™ product — **free Lite** build: ephemeral hybrid
> (vector + BM25) memory backed by Redis Stack, with a 7-day TTL per entry.
>
> 🇯🇵 **[日本語版はこちら](./README_JP.md)**
> 🛡️ **[Development Philosophy](./PHILOSOPHY.md)**

**Where to go**

| You are… | Read |
|---|---|
| new here | [Quickstart](#step-1--start-redis-stack), then [What Claude receives](#what-claude-receives) |
| coming from v1.x | `CHANGELOG.md` (version 2.0.0) |
| choosing between the two free builds | [Lite or Free?](#lite-or-free-which-one-to-use) |
| asking where your data is | [Your Memory Stays on Machines You Control](#your-memory-stays-on-machines-you-control), [Data location](#data-location) |
| changing settings | [Details](#details) |
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
| Claude Code | **the hooks** (`UserPromptSubmit`, `Stop`) and the CLI `n3mc-lite`, **or MCP** (the thin client) — your choice; both together also work | hooks: `n3mc-lite --init`; MCP: the plugin (as in v1.12.2) or `claude mcp add` |
| Claude desktop app and other applications without hooks | **MCP only** (the thin client `n3mc-workingmemory`) | you, in the application's configuration file |

- **Claude Code can use the hooks, MCP, or both.** The hooks save every
  message and reply; with MCP the model saves through the tools, as in
  v1.12.2.
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
an AI can generate the implementation from it. To do that:

1. **Start Redis Stack** ([Step 1](#step-1--start-redis-stack) above).
2. **Make a folder**: in File Explorer, create one empty folder (for example
   `C:\Users\<you>\n3mcmcp-lite`). The code is generated in this folder and
   stays in use after setup; do not delete or move it later.
3. **Put the specification in it**: copy the downloaded specification
   `N3MemoryCore_MCP_Spec_EN.md` into that folder (put nothing else in it).
4. **Open Claude Code in that folder**: in the Claude desktop app, open the
   **Code** tab at the top of the sidebar and start a new session with
   **+ New**. In the row under the prompt box, choose **Local** as the
   environment, then click the folder control beside it, choose
   **Open folder…** and pick the folder from step 2. (In a terminal, change
   to that folder and run `claude`.)
5. **Ask**: in the prompt box, send "Please implement n3mcmcp-lite according
   to the specification `N3MemoryCore_MCP_Spec_EN.md`."

The AI first checks the folder, Python and Redis, then generates the package,
runs `pip install -e .`, registers the hooks (`n3mc-lite --init`) and the MCP
entry (`claude mcp add`) in Claude Code, writes
the MCP entry into the
Claude desktop app's configuration file if the app is installed (after a
backup, replacing only that entry), and checks that everything starts. The
desktop app reads the entry after it is quit completely and started again.
The folder stays in use afterwards: deleting or moving it stops the memory.
The four-phase workflow is in Appendix B of the specification.

Then restart Claude Code, as in [Step 4 — Restart](#step-4--restart).

### Method B: Install the package (bonus)

#### Step 2 — Claude Code (MCP or hooks)

**(a) Plugin marketplace — MCP, as in v1.12.2 (no config file to edit).** Requires
[`uv`](https://docs.astral.sh/uv/) on `PATH`. Run inside Claude Code:

```
/plugin marketplace add NeuralNexusNote/n3mcmcp-lite
/plugin install n3mc-workingmemory@neuralnexusnote
```

The plugin registers the MCP server `n3mc-workingmemory` in Claude Code,
as in v1.12.2. It runs through `uvx --from n3memorycore-mcp-lite==2.0.2 …`
(the version is pinned to the package version). A `SessionStart` hook
pre-approves the memory tools.

**(b) pip — hooks.**

```bash
pip install n3memorycore-mcp-lite
n3mc-lite --init
```

`n3mc-lite --init` registers the two hooks in `~/.claude/settings.json` with
the absolute paths of `n3mc-lite-hook` / `n3mc-lite-stop-hook`, keeps every
other entry (including other N3MC products' hooks), backs the file up before
writing, and adds the permission rules for the CLI. It never removes an MCP
registration.

(a) and (b) can be used together: the MCP tools and the hooks reach the
same memory, and the model then does not save on every turn.

#### Step 3 — Claude desktop app (optional)

With Method A, setup has already written this entry; skip this step.

Open the config file from the app (Settings → Developer → Edit Config; the
usual place on Windows is `%APPDATA%\Claude\claude_desktop_config.json`, and
the Microsoft Store version of the app may keep it in another folder). Add
the entry under `mcpServers`, keep the entries already there, save, and
**quit the app completely and start it again**. To quit completely, right-click the Claude icon under the "^" at the right end of the taskbar and quit; if unsure, sign out of Windows once.

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
      "args": ["--from", "n3memorycore-mcp-lite==2.0.2", "n3mc-workingmemory"],
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

Restart Claude Code (and the desktop app). Closing the window is not always enough: the app can keep running in the background. On Windows, open the "^" at the right end of the taskbar (near the clock); if a Claude icon is there, right-click it and quit. If unsure, sign out of Windows once and sign in again (Start menu → your account icon at the bottom left → **Sign out**), or restart the PC. Memory starts with the next
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

Each hit starts with `### [<id>]` and shows its `turn=`, `score=` and `saved=` time. Hits under `Possibly related` did not pass the relevance floors.

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
  `uvx --from n3memorycore-mcp-lite==2.0.2 n3mc-lite` form (plugin).
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
- **Plugin and pip together**: they work together when both run the same
  version (the plugin pins it). A version generated from the specification
  runs other code, so its setup stops while the plugin is enabled.

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

## Details

Settings (`config.json`), how search results are ranked, multilingual handling, encoding safety, and how to develop and extend Lite are described in the specification `N3MemoryCore_MCP_Spec_EN.md`. For the changes in each version, see `CHANGELOG.md`.

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
