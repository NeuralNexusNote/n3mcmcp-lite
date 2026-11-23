# N3MemoryCore MCP-Lite v2.0.1 [Volatile Memory — hooks for Claude Code, MCP for other apps]
> A NeuralNexusNote™ product — **Lite (ephemeral) build**

> **What is this variant?** The Lite build is the free, marketplace-targeted edition of N3MemoryCore MCP. Storage is **Redis Stack (RediSearch)**, every entry carries a **7-day TTL**, and nothing persists beyond that window. Think of it as working memory — the **Pro build** uses SQLite + sqlite-vec to store memories permanently.
>
> **What it is made of (v2.0.0)**: MCP-Lite = N3MC-Free + MCP, with Redis as the database. Concretely: the search and save rules of Lite v1.12.2 (this document, §3), plus Free's hooks, resident server and CLI (§3.15, §12, §13), plus a thin MCP entry for applications without hooks (§4.6). Free and Lite keep separate search and save rules.
>
> **Who is this for?** Claude Code users (through **hooks** — every message and reply is saved and searched automatically) and users of other MCP-compatible applications such as the Claude desktop app (through **MCP**), who want searchable, short-lived memory shared across sessions within a 7-day window. (v2.0.0)
>
> **Transport**: Claude Code: hooks and a CLI that talk to a resident server on `127.0.0.1` over HTTP. Other applications: Model Context Protocol over stdio (JSON-RPC) to a thin MCP entry, which forwards to the same resident server (v2.0.0). v1.12.2 was tested on Windows 11 and Ubuntu with Python 3.12 and Redis Stack 7.x; v2.0.0 has not been generated and run yet. macOS is not a target.

## ⚠️ Disclaimer & Distribution Terms

This software and specification are provided **"AS-IS"** without warranty of any kind.

- **No Support**: The author provides no bug fixes, answers to questions, or guarantees of operation.
- **No Warranty / No Liability**: The author shall not be liable for any damages arising from use of this software, including but not limited to data loss, business interruption, or third-party claims.
- **Use at Your Own Risk**: You assume full responsibility for your use of this software.
- **Right to Change**: The author may modify or discontinue this software at any time without notice.

By using this software, you agree to the terms above.

- **License**: Apache License 2.0. See the `LICENSE` file for details.

> **Removal (Uninstall)**: Ask the AI "Please remove N3MC Lite" so that the hook entries are removed too, or do it yourself: (1) plugin users: `/plugin uninstall n3mc-workingmemory@neuralnexusnote`; pip users: remove the two entries of `~/.claude/settings.json` whose command runs `n3mc-lite-hook` / `n3mc-lite-stop-hook` (v2.0.0); (2) stop the resident server: `n3mc-lite --stop-server` (v2.0.0; or end the process whose command line contains `n3mc_mcp.cli` and `--run-server`); (3) `pip uninstall n3memorycore-mcp-lite` removes the package; (4) stop and delete the Redis container (`docker rm -f redis-stack`) to erase all stored memories instantly; (5) delete `${N3MC_DATA_DIR}` (default `~/.n3mc-workingmemory/`) to remove `config.json`, `audit.log` and the other files of §7; (6) remove the `n3mc-workingmemory` entry from the Claude desktop app's config file. `.claude/rules/n3mc-lite-behavior.md` in your project folders can be deleted by hand.
>
> **Backup?** The Lite build is **not designed to be backed up**. Entries vanish on a 7d rolling window; if you need durable memory, the **Pro build** offers persistent storage.

> **For implementation questions**: While the author cannot be contacted for support, you can load this specification into Claude and ask questions directly — Claude can assist with implementation and customization.

---

## Lite: Volatile Memory

This section captures the tradeoffs unique to the Lite build; the rest of the specification deliberately reuses the Pro-build structure so AI-driven regeneration stays simple.

| Property               | Lite (this spec)                         | Pro (separate spec)                    |
| ---------------------- | ----------------------------------------- | -------------------------------------- |
| Storage engine         | Redis Stack (RediSearch module)           | SQLite + sqlite-vec (local file)       |
| Durability             | **7 d TTL per entry**, ephemeral         | Permanent, disk-persistent             |
| Disk footprint         | `config.json` (< 1 KB); since v2.0.0 also the hook's `audit.log` (raw prompts and replies, rotated at 50 MB, up to ~100 MB) and small process files (§7) | `n3memory.db` grows with history       |
| External dependency    | User-run Redis Stack container            | None (self-contained)                  |
| `time_decay` relevance | Meaningful (3-day half-life; fresh=1.0, 7d≈0.20) | Meaningful (90-day half-life) |
| Re-indexing / repair   | `FT.CREATE` is idempotent; no migrations  | Schema + model migration markers       |
| Intended use           | Short-term projects, working memory, marketplace | Ongoing projects                 |

**Volatility contract:**
- Every write to Redis sets a TTL equal to `ttl_seconds` (default 604 800 = 7 d).
- Both the primary record (`mem:<uuid>`) and its exact-duplicate guard key (`mem:sha:<sha1>`) share the same TTL and expire together, 7 days after the save. A search never changes a TTL (decided 2026-11-22, §3.3).
- No write ever creates a key without a TTL. A write to a record that already exists (`access_count`, a re-embedded vector, a guard reset) happens only while the record still exists, checked and written in one atomic step (v1.7.3, §3.3).
- Expiration is delegated to Redis; no background cleanup job runs.
- Restarting the Redis container with its volume removed wipes all memory immediately.
- (v2.0.0) The contract covers Redis. The hook's `audit.log` (§7, §12) is a separate append-only file of the raw hook input with no TTL; it is the last-resort copy for a message that could not reach Redis, not a memory: nothing reads it back into search. **Decided 2026-11-19: `audit.log` is kept as in Free** (rotated at 50 MB, at most about 100 MB), although it outlives the 7-day window.

**No cross-session guarantee beyond 7 d.** Unlike the Pro build, the Lite spec deliberately forbids any "persistence hack" — do not add RDB snapshots, AOF rewrite, or external dumps to circumvent the TTL. If durability is needed, use the Pro build.

---

## What changed in v2.0.0

v2.0.0 is a MAJOR version (Semantic Versioning): the way Lite connects to Claude Code changed. Up to v1.12.2 (whose specification was titled v1.9.1) Lite was an MCP server only; the model decided what to save. From v2.0.0 Lite is "N3MC-Free + MCP, with Redis as the database".

**Shape**

```
Claude Code ──hooks / CLI (HTTP)─────────────────────────┐
                                                         ├─▶ resident server (127.0.0.1:18524, model loaded once) ─▶ Redis Stack
Claude desktop app ─(MCP, stdio)─▶ n3mc-workingmemory ───┘
                                   (thin client, no model)
```

**Which application uses what**

| Application | Connects through | Registered by |
|---|---|---|
| Claude Code | **the hooks only** (`UserPromptSubmit`, `Stop`) and the CLI `n3mc-lite` (§12, §13) | the plugin, or `n3mc-lite --init` |
| Claude desktop app and other applications without hooks | **MCP only** (the thin client `n3mc-workingmemory`, §4.6) | the user, in the application's configuration file (§8) |

- **Claude Code no longer uses MCP.** The plugin registers the hooks and no longer registers the MCP server in Claude Code; `n3mc-lite --init` never registers it either. Do not register the MCP server in Claude Code.
- Both paths reach **the same resident server and the same Redis**: what is said in Claude Code is found from the desktop app, and what the desktop app saves is found in Claude Code.

**What existing users see after the update**

| Before (v1.12.2) | After (v2.0.0) |
|---|---|
| Claude Code showed the seven `mcp__…n3mc-workingmemory__*` tools | **Claude Code no longer shows the MCP tools.** The plugin does not register the MCP server any more |
| Only what the model chose to pass to `save_memory` was saved | **Every non-empty message and reply is saved by the hooks** (fenced code blocks as `[code omitted]`), as `[user]` / `[claude]` records (§3.16) |
| The model called `search_memory` at the start of a turn | The `UserPromptSubmit` hook searches with every message and puts the result into Claude's context under `# Recalled Memory Context` (§12) |
| Search, list, recall and delete were MCP tools | In Claude Code the CLI gives the same: `n3mc-lite --search`, `--list`, `--recall-thread`, `--delete`, `--delete-session`, `--buffer`, `--repair` (§13). The text they print is the tool's text |
| Each application started its own MCP server and loaded the model (~1.1 GB) | One resident server loads the model once (§3.15); the MCP entry is a thin client that never loads it (§4.6) |
| No process ran between sessions | The resident server keeps running in the background after it was started |
| pip users: register the MCP server | pip users: run `n3mc-lite --init` once to register the hooks (§12). An old `n3mc-workingmemory` registration in Claude Code is no longer used: Method A removes it, `--init` reports it and leaves it (§8) |
| An old plugin or `.mcp.json` started `uvx --from n3memorycore-mcp-lite n3mc-workingmemory` (no version) | **Update the plugin** (`/plugin update`). Until then, once `uvx` fetches 2.0.0, that registration starts the 2.0.0 thin client as an MCP server in Claude Code, with no hooks: the model searches and saves through the tools as in v1.12.2, now through the resident server (§4.6). `n3mc-lite --init` reports such a registration (§8) |
| `N3MC_SESSION_ID` grouped every save of a Claude Code session into one project | Hook records carry Claude Code's own `session_id` (§3.16); `N3MC_SESSION_ID` still applies to MCP saves and searches (§4.6). Use `delete_memories_by_session` / `--delete-session` with the session id shown in a record (`--list`) |
| `N3MC_REDIS_URL` in an MCP client's `env` chose the Redis | The resident server uses the environment of the process that started it (§3.15). Put `redis_url` into `config.json` so every process uses the same one; a client whose own setting differs from the running server's gets the notice `redis_url_mismatch` |
| `skip_code_blocks: true` rejected a `save_memory` with a fence | The same setting also skips every hook message or reply that contains a fence (§3.16); the default `false` stores it with `[code omitted]` |
| The first `uvx` fetch and the model download happened when the MCP server started | They happen in the background after the first hook call: the hooks do not wait for the server (§12.3), so the first messages are saved without vectors and searched with keywords only until the model is ready |
| `save_memory` without `agent_name` stored an empty name | MCP: `N3MC_AGENT_NAME`, else the client's `clientInfo.name`, else empty (§4.6); hooks: `claude-code` |
| — | `--stop` writes `.claude/rules/n3mc-lite-behavior.md` in the root folder of each project where Claude Code ends a turn (§12.4, §12.9) |
| `examples/claude_code_mcp.json` showed the MCP registration for Claude Code | Removed: Claude Code uses the hooks |
| The Redis hint and the README said `docker run … -p 6379:6379 …` (all interfaces) | **(display change, D9)** They say `-p 127.0.0.1:6379:6379`, so the memory is not reachable from the network. A container you already created keeps its own setting; to change it, recreate it (`docker rm -f redis-stack`, then the new `docker run`) — its memories are lost then, as on any restart (§3.4) |
| Rule 0 and the search reminder named `n3mc-longtermmemory` as the persistent memory | **(display change, D10)** They name `n3mc-pro`, `n3mc-pg` and `n3mc-longtermmemory` (the MCP registrations of N3MC MCP Pro, its PostgreSQL edition and the earlier MCP Pro) (§5 (1), (2)) |
| Method A edited `claude_desktop_config.json` | **(D11)** The AI never edits the desktop app's file: it shows the entry with the absolute path filled in, and the user adds it (§8) |

**What did not change**: the PyPI name `n3memorycore-mcp-lite`, the Python package `n3mc_mcp`, the MCP command and server name `n3mc-workingmemory` (alias `n3mc-mcp-lite`), the seven tools with the same names, arguments and output texts (except that the `search_memory` text is now kept within `context_max_chars`, §3.17), the Redis layout and settings (`redis_url`, `N3MC_REDIS_URL`), the 7-day TTL, the data folder `~/.n3mc-workingmemory` (`N3MC_DATA_DIR`), the plugin name `n3mc-workingmemory`, and every search and save rule of §3. Records saved by v1.12.2 stay readable (same index, same fields).

---

## Setup

### Prerequisites

| Item                          | Requirement                                                              |
| :---------------------------- | :----------------------------------------------------------------------- |
| Python                        | 3.10 or higher                                                           |
| Client                        | Claude Code (hooks, v2.0.0), and/or the Claude desktop app or any other MCP client (MCP) |
| Redis Stack                   | Running on `localhost:6379` (or any URL set via `N3MC_REDIS_URL`)        |
| pip packages (auto-installed) | `mcp` (**`>=1.0.0,<2.0.0`**) `redis` (`<8.0.0`) `sentence-transformers` `uuid-utils` `platformdirs` `numpy`, and since v2.0.0 `fastapi` `uvicorn` `pydantic` `psutil` (the resident server and its process checks, §3.15) |
| OS                            | Windows 11 or Linux. macOS is not a target                               |

### Quick Start

> **The primary way to use the N3MC series is to have an AI generate the implementation FROM the spec (this document).** The spec is written in detail specifically to make AI regeneration easy ("described with the same structure as the Pro build to ease AI regeneration"). Hand it to Claude (or another AI) to implement, customize, or regenerate the server. **Installing the published package via pip / marketplace is a secondary, "bonus" convenience** for trying it quickly.

Either way, start **Redis Stack** first:

```bash
# First time only (creates the container):
docker run -d --name redis-stack -p 127.0.0.1:6379:6379 redis/redis-stack-server:latest

# Every subsequent session (container already exists):
docker start redis-stack
```
Re-running the `docker run` command after the container exists fails with `Conflict. The container name "/redis-stack" is already in use`. Use `docker start redis-stack` thereafter.

> **Persistence is turned off by the server, not via docker args.**
> On every connect the MCP server issues `CONFIG SET appendonly no` and
> `CONFIG SET save ""` (§3.4 `enforce_ephemeral()`), so any manual
> re-enable between sessions is reverted on the next Lite run —
> **but only when that Redis holds nothing but N3MC keys** (v1.7.1).
> A Redis that also stores other data keeps its persistence settings,
> and the server shows a `shared_redis` notice recommending a dedicated
> container (§3.4). Earlier iterations of this spec put `--appendonly no
> --save ""` on the `docker run` line as a belt-and-suspenders, but
> the empty-string argument for `--save` is mangled by Windows
> PowerShell and cmd.exe quoting (it has left containers with a
> broken entrypoint in practice), so the docker args have been
> removed and server-side enforcement is the sole source of truth.
> Rationale for the ban itself: ephemerality is the product boundary
> that separates the free Lite build from the paid persistent
> N3MemoryCore — Lite is "a rolling 7-day scratchpad that truly
> forgets on restart", not "a durable store with a TTL". If the user
> wants continuous memory, they upgrade. On a dedicated container,
> `enforce_ephemeral()` makes it *mechanically impossible* to turn Lite
> into a persistent store by accident, regardless of the user's shell or
> docker flags.

#### Method A: Generate from the spec (primary, recommended)

1. **Start Redis Stack** (above).
2. **Hand the spec to an AI and ask it to implement**: open Claude Code in an **empty folder** (for example `~/n3mcmcp-lite`) with only this spec in it — the AI first checks the folder, Python and Redis ([§1](#1-vision), "Setup checks") and stops with instructions if something is missing. Then give `N3MemoryCore_MCP_Spec_EN.md` (or `_JP.md`) to Claude and ask "implement n3mcmcp-lite per this spec." The AI generates the package layout, RediSearch index, the resident server, the hooks, the CLI and the MCP thin client. The staged **implement → debug → self-evaluate → quality-review** four-phase workflow is in [Appendix B](#appendix-b-recommended-ai-assisted-workflow).
3. **Install and register (done by the AI)**: the AI runs `pip install -e .` in the project root, registers the **hooks** in Claude Code with `n3mc-lite --init` (v2.0.0), shows the MCP entry (with absolute paths) to add to the Claude desktop app if it is installed (the AI does not edit that app's config file; you add the entry, decision D11), and checks that everything starts ([§8 "Automatic registration after code generation"](#automatic-registration-after-code-generation-method-a--required)). You do not edit any Claude Code config file.
4. **Restart the client.** The first memory search after the restart may take 30–60 seconds as the ~1.1 GB embedding model is downloaded and loaded by the resident server.

#### Method B: Install the package (bonus)

If you just want to run it quickly from the published package:

1. **Start Redis Stack** (above).
2. **Install the package** (choose one):
   - **pip** (global or venv):
     ```bash
     pip install n3memorycore-mcp-lite
     ```
   - **uvx** (zero-install, isolated env — requires [`uv`](https://docs.astral.sh/uv/)), for the Claude desktop app:
     ```bash
     uvx --from n3memorycore-mcp-lite==2.0.1 n3mc-workingmemory
     ```
   - **Claude Code plugin marketplace** (no pip/uvx command required — the plugin runs the hooks through `uvx` for you, but `uv` must still be on PATH):
     ```
     /plugin marketplace add NeuralNexusNote/n3mcmcp-lite
     /plugin install n3mc-workingmemory@neuralnexusnote
     ```
3. **Connect the applications** (v2.0.0):
   - **Claude Code**: plugin users do nothing — the plugin registers the hooks. pip users run `n3mc-lite --init` once (§12). Do not register the MCP server in Claude Code.
   - **Claude desktop app** (and other MCP clients): register the MCP entry in the application's config (see [§8](#8-client-configuration)).
4. **Restart the client.** The first memory search may take 30–60 seconds as the ~1.1 GB embedding model is downloaded and loaded by the resident server.

### Data Backup

Not applicable. See the [Lite: Volatile Memory](#lite-volatile-memory) section — the build is explicitly ephemeral. `config.json` (containing `owner_id` / `local_id` UUIDs) is the only file that matters for identity and can be copied if the user wants to keep the same owner identity across re-installs. Since v2.0.0 the data folder also holds `audit.log` (the raw hook input, §12) and process files (§7); they are not memories and need no backup.

---

## 1. Vision

Provide a no-commitment memory endpoint for MCP clients: hybrid search (vector + RediSearch BM25), mathematically sound ranking, 7-day automatic garbage collection. For applications without hooks, the MCP server delivers behavioral instructions so the connected LLM auto-searches at the start of each turn and auto-saves after each meaningful exchange; in Claude Code, hooks do both (v2.0.0, below).

The Lite exists to demonstrate the N3MemoryCore MCP surface on the Claude Marketplace with zero risk to the user's disk; the **Pro build** keeps the memory permanently in SQLite + sqlite-vec (N3MC Pro, with an MCP entry in N3MC MCP Pro, whose tool set differs: §14).

**(v2.0.0) Hooks for Claude Code.** Behavioral instructions cannot make the model save (§11). For Claude Code, which has hooks, v2.0.0 adds Free's hooks: the `UserPromptSubmit` hook saves the user's message and searches with it, the `Stop` hook saves the reply, so every exchange is saved and every message gets the matching memories without any tool call. The MCP instructions stay for applications without hooks. The search and save rules are Lite's own (§3) in both paths; only the way they are called differs.

> **⚠️ Setup checks (Method A — do these first, in this order, before writing any file)**: checks 1–3 stop the setup when they fail; check 4 fixes the problem itself. These checks exist because a setup on a second PC (2026-11-16) started in a folder that already held a copy of an earlier setup from another PC, with Docker stopped, only Python 3.15 installed and `.mcp.json` pointing at the other PC's Python; the implementing session could neither build nor start the server. Stop at the first of checks 1–3 that fails and show the user what to do.

> **1. Install folder check**: Method A writes the code into the folder the AI is open in (the current working directory): `pyproject.toml`, `README.md`, `tests/`, `n3mc_mcp/` and the other files of §2. That folder stays in use after setup: with `pip install -e .` the hooks, the resident server and the MCP entry run the code from it, so deleting or moving it later stops the memory.
> 1. Show the user the absolute path of the current working directory and this message: "N3MC Lite will be installed in this folder: <absolute path>. The code will stay here; deleting or moving this folder later stops the memory hooks and the memory server. Continue? (yes/no)". **Wait for the user's answer.** Continue only on a clear yes.
> 2. Before asking, look at the folder. If it contains anything other than this specification file (`N3MemoryCore_MCP_Spec_EN.md` / `_JP.md`), `.claude/`, `.git/` and the files of an earlier setup of this product (`n3mc_mcp/`, `tests/`, `.mcp.json`, `pyproject.toml` whose `name` is `n3memorycore-mcp-lite`), **do not ask: abort** and display: "This folder already contains other files (for example a project of yours). Setting up here could overwrite them. Create an empty folder such as `~/n3mcmcp-lite`, open the AI there, and run this setup again." Never overwrite or mix into an existing project.
> 3. If the folder holds an earlier setup, say so in the message (with the version in its `pyproject.toml`) and ask whether to regenerate the code from this specification or keep it. An earlier setup may have been copied from another PC: any absolute path in `.mcp.json` or `.claude/settings*.json` that does not exist on this PC must be replaced with this PC's path when the server is registered (§8); never keep a path that does not exist.
> 4. Do not install into the home folder itself (`~`) or a system folder.

> **2. Python check**: run `python --version` or `python3 --version` to verify Python 3.10+ is available. Python **3.12** is the tested version (see the header). If not found, display: "Python 3.10 or higher is required. Install Python 3.12 from https://www.python.org/" and **abort setup**.
>
> **Windows note**: On Windows, `python` / `python3` may exist only as Microsoft Store shortcuts (App Execution Aliases under `%LOCALAPPDATA%\Microsoft\WindowsApps`). They open the Store or print a "Python was not found" message instead of a version. Treat this as "Python not found". Also try `py -0p` (the python.org launcher lists every installed version) before concluding. When aborting on Windows, add these steps to the message:
> 1. Install Python 3.12 from https://www.python.org/ and check "Add python.exe to PATH" in the installer, or run `winget install Python.Python.3.12`.
> 2. If `python` still opens the Microsoft Store, turn off the `python.exe` and `python3.exe` entries in Settings → Apps → Advanced app settings → App execution aliases.
> 3. **Restart required.** The AI client (Claude Code, Claude Desktop) keeps the `PATH` it started with, so it cannot see a Python installed afterwards. Closing the window is not enough: the app can keep running in the background (for example in the system tray). Sign out of Windows once and sign in again: Start menu → your account icon (bottom left) → **Sign out**. Restarting the PC also works.
> 4. After signing in, open a new terminal and confirm that `python --version` prints 3.10 or later.
> 5. Start the AI client, open the same folder, and ask it to run this setup again.
>
> Write this message for a beginner: one action per step, plain words, and say clearly that the sign-out (or restart) is required.
>
> **Newer Python than tested**: when only a Python newer than 3.12 is installed, some dependencies may not yet publish ready-built packages for it, and `pip install -e .` then tries to compile them from source and fails (for example with a message asking for Microsoft C++ Build Tools). If that happens, do not install build tools: tell the user to install Python 3.12 as above, then create the environment with that version (`py -3.12 -m venv .venv` on Windows) and install into it.
>
> Do not install Python yourself; installing Python is left to the user.

> **3. Redis check**: run `docker ps`. If Docker cannot be reached (Docker Desktop is not running), ask the user to start Docker Desktop and wait until it is running. Then check `docker ps -a --filter name=redis-stack`: if the container does not exist **on this PC**, create it with the `docker run` line in [Quick Start](#quick-start) (a container made on another PC does not exist here); if it exists but is stopped, run `docker start redis-stack`. Continue only when `redis-stack` is running. If the user set `N3MC_REDIS_URL` to another Redis, check that it answers instead and skip Docker.
>
> **4. Windows: Visual C++ runtime (install automatically, do not ask)**: `sentence-transformers` uses PyTorch, which on Windows needs the Microsoft Visual C++ Redistributable (x64). Without it, importing `torch` fails (for example `OSError: [WinError 126]` or `[WinError 1114]` naming `c10.dll`). Before `pip install -e .`, check `reg query "HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" /v Installed`. If the key is missing or not `0x1`, install it without asking: `winget install --id Microsoft.VCRedist.2015+.x64 --silent --accept-package-agreements --accept-source-agreements`. Windows shows its administrator prompt (UAC); tell the user in one line to approve it. Do the same if `import torch` later fails with one of the errors above. If `winget` is not available, show the user the official installer https://aka.ms/vs/17/release/vc_redist.x64.exe and wait. This is a runtime, not a compiler: it does not replace Python 3.12 when a dependency fails to build (check 2).
>
> Git is not required. If the code was received as a copy instead of a `git clone`, continue without git.

> **⚠️ First-run download**: `sentence-transformers` downloads the `multilingual-e5-base` model (~1.1 GB) on first tool use. The server will appear unresponsive during this time — this is expected. Subsequent starts complete in seconds once cached.

> **Important: Character Limits (Design Constraints)**
> - Hook saves (Claude Code, v2.0.0): every non-empty message and reply is saved whole, with no length or skip-pattern filter; long ones take the parent-document path below (§3.16).
> - Auto-save per entry (MCP `save_memory`): **50–200 characters recommended** (one fact per entry).
> - Search query: **2,000 characters** (configurable via `search_query_max_chars`).
> - Vector search: Only the first **~2,000 characters** of any record are semantically searchable (embedding model limit: 512 tokens). Beyond this, content is stored and BM25-searchable but invisible to vector similarity.
> - **Large text handling (two modes)**:
>   - **Fact extraction (preferred for fact-based memory)**: read and understand the content, extract each key fact as a separate short sentence (~50–200 chars), and call `save_memory` once per fact. Maximises search precision, access-frequency boost, and per-fact importance tuning.
>   - **Verbatim recall (whole-document save)**: when the user wants the exact original text back later ("save this setting doc, I want to see the same thing again"), pass the long body in a single `save_memory` call. The server auto-splits content longer than `chunk_threshold` (default 400 chars) into overlapping chunks while also persisting the original full text as a parent document (`doc:<uuid>`). A later `search_memory` that hits any chunk reconstructs the full verbatim content of the parent (see [§3.11](#311-verbatim-recall-parent-document--chunks-pattern)).

---

## 2. Package Structure

```
n3memorycore-mcp-lite/
├── pyproject.toml                  # Package metadata and the five console scripts (below)
├── README.md / README_JP.md        # User guide (generated from this spec)
├── LICENSE / NOTICE / CHANGELOG.md / PHILOSOPHY.md
├── N3MemoryCore_MCP_Spec_EN.md / _JP.md   # this specification
├── n3mc_mcp/                       # Python package
│   ├── __init__.py                 # Version marker (__version__ = "2.0.1")
│   ├── __main__.py                 # Entry point: python -m n3mc_mcp (= the MCP thin client, run())
│   ├── server.py                   # (v2.0.0) MCP thin client: stdio, tools/list, forwards tools/call to the resident server (§4.6); run()
│   ├── tooldefs.py                 # (v2.0.0) The seven Tool definitions of §5 (3), shared by the thin client and the resident server
│   ├── tools.py                    # (v2.0.0) The tool handlers, nudges, _render_search, notices (§4.3, §5 (2)–(4)); run inside the resident server
│   ├── instructions.py             # Behavioral instructions delivered at initialize (§5 (1))
│   ├── app.py                      # (v2.0.0) Resident FastAPI server: /health /shutdown /buffer /search /repair /tools/{name} (§4.5)
│   ├── cli.py                      # (v2.0.0) CLI `n3mc-lite`: --init --stop --hook-submit --save-claude-turn --run-server --search --buffer --list --recall-thread --delete --delete-session --repair; ensure_server, _start_server, _notify (§3.15, §12, §13)
│   ├── lite_hook.py                # (v2.0.0) UserPromptSubmit hook entry: audit.log → `cli --hook-submit`
│   ├── lite_stop_hook.py           # (v2.0.0) Stop hook entry: audit.log → `cli --save-claude-turn` → `cli --stop`
│   ├── context.py                  # (v2.0.0) `# Recalled Memory Context` block, context_size, size limit (§3.17)
│   ├── database.py                 # Redis layer: index, CRUD, TTL, dedup (+ role prefixes §3.16, direct save without vectors)
│   ├── processor.py                # Embedding, ranking, CJK tokenization, reranker
│   ├── config.py                   # config.json load/save + UUID generation
│   └── paths.py                    # data folder (~/.n3mc-workingmemory/) and its files (§7) + legacy platformdirs path for migration
├── plugins/n3mc-workingmemory/     # Claude Code plugin (§8, §12)
│   ├── .claude-plugin/plugin.json  # (v2.0.0) no "mcpServers" any more
│   └── hooks/
│       ├── hooks.json              # (v2.0.0) SessionStart + UserPromptSubmit + Stop
│       └── install_permissions.py  # SessionStart: permission rules for the CLI commands (§8)
├── .claude-plugin/marketplace.json # Marketplace entry; metadata.version = package version
├── tests/                          # pytest suite (§9)
│   ├── conftest.py
│   ├── test_database.py  test_processor.py  test_server.py  test_regressions.py
│   └── test_hooks.py  test_api.py  test_cli.py  test_mcp_client.py  test_context_limit.py  test_coexistence.py   # (v2.0.0)
└── examples/
    └── claude_desktop_config.json  # (v2.0.0) the desktop registration of §8 (claude_code_mcp.json is removed: Claude Code uses hooks)
```

`config.json` lives in **`~/.n3mc-workingmemory/`** (see [§7](#7-data-location)). No database file exists on disk — memory state lives entirely in Redis.

**Console scripts (v2.0.0)**

| Script | Maps to | Purpose |
|---|---|---|
| `n3mc-workingmemory` | `n3mc_mcp.__main__:run` | MCP thin client (the command the Claude desktop app starts) |
| `n3mc-mcp-lite` | `n3mc_mcp.__main__:run` | Deprecated alias of `n3mc-workingmemory` (pre-1.5 configs) |
| `n3mc-lite` | `n3mc_mcp.cli:main` | CLI for Claude Code and humans (§13) |
| `n3mc-lite-hook` | `n3mc_mcp.lite_hook:main` | UserPromptSubmit hook (§12) |
| `n3mc-lite-stop-hook` | `n3mc_mcp.lite_stop_hook:main` | Stop hook (§12) |

The resident server is started as `python -m n3mc_mcp.cli --run-server` (§3.15); `python -m n3mc_mcp.cli` is also the command form the rules file gives Claude (§12).

**`pyproject.toml` (v2.0.0)**: in the published repository, keep the current file (hatchling build backend, metadata, keywords, classifiers, `[tool.hatch…]` sections) and change only the items below. **When Method A generates into an empty folder (L14, 2026-11-20)**, write this file and then apply the same items: `[build-system]` `requires = ["hatchling>=1.21.0"]`, `build-backend = "hatchling.build"`; `[project]` `name = "n3memorycore-mcp-lite"`, `description = "N3MemoryCore MCP Lite - ephemeral 7-day hybrid-search memory over Redis Stack"`, `readme = { file = "README.md", content-type = "text/markdown" }`, `license = { file = "LICENSE" }`, `requires-python = ">=3.10"`, `authors = [{ name = "NeuralNexusNote" }]`, the dependencies below; `[tool.hatch.build.targets.wheel]` `packages = ["n3mc_mcp"]`. Without the files that `readme` and `license` point to, hatchling cannot build the metadata and `pip install -e .` fails (checked 2026-11-20), so Method A also writes `README.md` (a short user guide written from this spec) and `LICENSE` (the full text of the Apache License 2.0). Keywords, classifiers and URLs are optional for a local installation. The plugin files (`plugins/…/plugin.json`, `.claude-plugin/marketplace.json`) belong to the published repository only; Method A does not create them.

- `version = "2.0.1"`.
- `dependencies`: the current six (`mcp>=1.0.0,<2.0.0`, `redis>=5.0.0,<8.0.0`, `sentence-transformers>=2.7.0`, `uuid-utils>=0.6.0`, `platformdirs>=4.0.0`, `numpy>=1.26.0`) plus `fastapi>=0.110`, `uvicorn>=0.27`, `pydantic>=2.0`, `psutil>=5.9` (Free's lower bounds).
- `[project.optional-dependencies] dev`: add `httpx>=0.27` (FastAPI `TestClient`).
- `[project.scripts]`: the five scripts of the table above.
- The classifier `Operating System :: OS Independent` becomes `Operating System :: Microsoft :: Windows` and `Operating System :: POSIX :: Linux` (macOS is not a target).

`.claude-plugin/marketplace.json` `metadata.version` and `plugins/n3mc-workingmemory/.claude-plugin/plugin.json` `version` are the package version (`2.0.1`). **The `uvx` commands are pinned to the same version (decided 2026-11-19)**: every `uvx --from n3memorycore-mcp-lite==<version>` in this document (the plugin's hooks, the permission rules, the rules file's `{cli}`, the desktop registration) names the package version; a release changes all of them together with the two `version` fields. Without the pin, the plugin and a pip installation could run different versions, whose servers have different `code_version`s and replace each other (§4.6).

**Files kept as they are (not generated from this specification)**: `SECURITY.md`, `CONTRIBUTING.md`, `.github/` (issue and PR templates, `workflows/test.yml`, `workflows/pypi-publish.yml`), `server.json` (MCP registry entry), `.gitignore`, the evaluation scripts `tests/eval_*.py`, and `tests/test_cross_session.py` (decided 2026-11-19: kept). Regenerating the code must not delete or rewrite them. (v2.0.0: until now this list existed only in the repository.) When the CI matrix in `.github/workflows/test.yml` is edited by hand, it lists Windows and Linux only.

---

## 3. Technical Specifications (No Modifications Allowed)

> **⚠️ AI must not auto-modify**: AI must not autonomously change any of the following specifications for speed improvement or optimization. Changes to the embedding model, vector dimensions, or TTL are only permitted by a human manually editing `config.json`.

> ### 📌 Known implementation pitfalls (found during implementation, debugging & review — required reading)
>
> When re-implementing from this spec, the following are the classic traps that "look like they work but break a core feature." See the "⚠️ Implementation note" in each section for details.
>
> | # | Trap | Symptom | Fix | Detail |
> |---|------|---------|-----|--------|
> | 1 | redis-py 8.0 defaults to **RESP3** | `FT.SEARCH` always returns 0 results | Handle the RESP3 dict response (current impl) + pin `redis<8.0.0`, or `from_url(..., protocol=2)` | [§3.3](#33-redis-connection--ttl) |
> | 2 | BM25 score not fetched | Keyword search disabled (degrades to vector-only) | `.with_scores().scorer("BM25")` | [§3.6](#36-ranking-formula) |
> | 3 | NFKC normalization not applied | Full-/half-width dupes & search drift | NFKC on embedding, dedup hash and the BM25 text (not on stored `content`) | [§3.7](#37-text-tokenization--punctuation-handling) |
> | 4 | Model load **blocks** startup | MCP connection timeout (~40s) | Import the library before stdio; construct the model in the background after stdio + lock (importing in the background deadlocks on Windows) | [§3.9](#39-startup-sequence--self-recovery) |
> | 5 | `doc.id` is the **full key** (`mem:<uuid>`) | `mem:mem:...` ghost keys (TTL-less orphans) / `[doc×0]` / delete fails | Normalize to bare UUID with `_doc_bare_id()` | [§3.12](#312-uuid-tag-query-constraint--python-side-owner-filtering) |
> | 6 | `SCAN mem:*` also matches `mem:sha:*` | `HGET` fails with `WRONGTYPE` | Exclude by prefix or catch the exception | [§3.12](#312-uuid-tag-query-constraint--python-side-owner-filtering) |
> | 7 | `serverInfo.version` / `instructions` unset | Empty version / instructions not advertised | `Server(name, version=, instructions=)` | [§4.2](#42-initialize-response) |
> | 8 | No `turn_id` in `search_memory` output | `recall_thread` can't be called (workflow breaks) | Add `turn_id` to RETURN and to the markdown | [§4.3.1](#431-retrieval-extension-tools) |
> | 9 | Running pytest during the Evidence Report | `FLUSHDB` wipes live data | Stop pytest during §10 / use a separate Redis | [§9](#9-testing-pytest) |
> | 10 | Measuring "0.5s steady state" over the MCP round-trip | Looks like 5–10s, false negative | 0.5s is a pure server-side target | [§10](#10-self-evaluation-evidence-report) |
> | 11 | Tests pointed at the live Redis | `FLUSHDB` wipes the user's working memory | Tests run only against `N3MC_REDIS_TEST_URL`; there is no default | [§9](#9-testing-pytest) |
> | 12 | Control characters / CJK punctuation left in the BM25 query | `Syntax error` (BM25 silently skipped) / 0 BM25 hits for `「記憶装置」は？` | Control chars → space; strip CJK and full-width punctuation too | [§3.7](#37-text-tokenization--punctuation-handling) |
> | 13 | Redis checked only at startup / connection errors swallowed | After `docker start redis-stack` the server still says Redis is down; a Redis that stops later yields `_No memories found._` | `ensure_connected()` (PING / reconnect) before every tool; re-raise `ConnectionError` / `TimeoutError` | [§3.9](#39-startup-sequence--self-recovery) |
> | 14 | Config in `%LOCALAPPDATA%` | The Store (MSIX) Claude desktop app redirects its MCP servers' `%LOCALAPPDATA%` → two `config.json`, two `owner_id`s; memories saved from the desktop app are invisible to terminal `claude` and vice versa | Keep the config in `~/.n3mc-workingmemory/` (not redirected); migrate once | [§7](#7-data-location) |
> | 15 | Plugin auto-approve rules use the plain server name / hook failure only exits non-zero | Plugin users are prompted on every memory call; with no Python the only sign is `SessionStart hook error … python: command not found` | Allow `mcp__plugin_n3mc-workingmemory_n3mc-workingmemory__*` (+ `mcp__n3mc-workingmemory__*`); Python-free fallback prints `systemMessage` JSON and exits 0 | [§8](#auto-approve-claude-code-only) |
> | 16 | `mcp` without an upper bound | A fresh install (`uvx`, `pip`, plugin) gets `mcp` 2.x and the server dies on import: `AttributeError: 'Server' object has no attribute 'list_tools'` — `/mcp` shows only `Connection closed` | Pin `mcp>=1.0.0,<2.0.0` in `pyproject.toml` until the server is ported to the 2.x API | [Setup](#prerequisites) |
> | 17 | Model download retried on every call / degraded search not reported | Offline first run: each `save_memory` ~24 s, each `search_memory` ~12 s; keyword-only results look normal | Retry at most once a minute; notice on every degraded `search_memory`, `detail` on `save_memory` | [§3.9](#39-startup-sequence--self-recovery) |
> | 18 | Only `strip()` decides "empty" | `\x00`, a lone `U+200B` or `U+FEFF` is saved as a memory that shows as nothing | Reject content with no visible character (`has_visible_text`); store everything else verbatim | [§3.13](#313-encoding-safety) |
> | 19 | `min_score` / relative cut judged on the final score | Records older than ~2 days and every record saved before the MCP server restarted (another `session_id`) fall under 0.6 × the top score and disappear | Judge both floors on `filter_score` (no `time_decay`, no `b_session`); order by the final score | [§3.14](#314-search-result-shaping-v170-weak-match-fill-since-v172) |
> | 20 | Only `socket_connect_timeout` set | A Redis that accepts the connection but never answers (`docker pause`, sleep/resume) blocks `PING` forever; the event loop and every later tool call hang | `socket_timeout=5`; a read timeout is a lost connection | [§3.9](#39-startup-sequence--self-recovery) |
> | 21 | `config.json` created by every starting process | Two servers starting together get different `owner_id`s; one of them saves records nobody sees after the next start | Exclusive create, re-read if it exists; temp file + `os.replace` for every write | [§6](#6-configuration) |
> | 22 | Parent document written before the chunks | A failure between the two writes leaves `doc:` + `docsha:` without chunks: unsearchable, and re-saving is rejected as `duplicate` for 7 days | Embed every chunk first, then take the guard (`SET NX`) and write parent and chunks in one Lua script (`SAVE_WITH_GUARD`, §3.8) | [§3.8](#38-duplicate-rejection-v170-exact-only) |
> | 23 | No range check on numeric config | `chunk_overlap >= chunk_threshold` loops forever; `ttl_seconds: 604800.5` or `0` makes every save fail; `embedding_dim: 768.0` stops the server at start | Per-key type and range table; `chunk_text` always advances | [§6](#6-configuration) |
> | 24 | `FT.CREATE` failure re-raised at start | A plain Redis without RediSearch (or a connection lost during start) ends the process; the client only sees a dead server | Stay up without an index, retry per call, return a "Redis Stack required" error | [§3.9](#39-startup-sequence--self-recovery) |
> | 25 | Persistence turned off on any Redis | Someone else's Redis on `localhost:6379` silently loses its RDB/AOF and its data at the next restart | Only when the connected DB is empty or holds only N3MC keys **and no other DB of the server holds keys** (v1.8.1); otherwise notice `shared_redis` | [§3.4](#34-ephemerality-no-modifications-or-optimizations-allowed) |
> | 26 | Tool calls wait for the model lock without limit | During the first-run download (minutes) every tool call blocks and may hit the client's tool timeout | Wait at most 20 s, then continue keyword-only with a notice | [§3.9](#39-startup-sequence--self-recovery) |
> | 27 | TTL refresh skips the dedup guards; BM25 terms ANDed; unreadable `since` ignored | Frequently used records can be saved twice after 7 days; sentence-like queries get no keyword hits; `since="yesterday"` searches everything | Refresh guards with the record (removed 2026-11-22: a search no longer resets any TTL, §3.3); join terms with OR; reject unreadable time bounds | [§3.3](#33-redis-connection--ttl), [§3.7](#37-text-tokenization--punctuation-handling), [§4.3.1](#431-retrieval-extension-tools) |
> | 28 | "Does the key exist?" and the write sent as separate commands | A record deleted (`delete_memory` from another process) or expired between the check and the write comes back as a hash holding only `access_count` or `embedding`, **with no TTL** — it never expires and nothing deletes it; a guard reset after the record was deleted blocks saving the same content for 7 days | Check and write in **one Lua script** (`EXISTS` then `HINCRBY` / `HSET` / `SET … EX`) | [§3.3](#33-redis-connection--ttl) |
> | 29 | (v2.0.0) The MCP entry imports the model or prints to stdout | Each application loads ~1.1 GB again; a stray `print()` corrupts the JSON-RPC stream | The thin client imports neither `sentence_transformers` nor `torch`; nothing but MCP messages on stdout | [§4.6](#46-mcp-thin-client-v200) |
> | 30 | (v2.0.0) `urllib.request.urlopen` for `127.0.0.1` | With `HTTP_PROXY` set (company PCs) every request goes to the proxy; the server looks down and every hook waits | Open requests with `build_opener(ProxyHandler({}))` | [§3.15](#315-resident-server-v200) |
> | 31 | (v2.0.0) Endpoints written as `async def` | A long `/repair` or search blocks `/health`; the CLI decides there is no server and starts another | Plain `def` for every endpoint that touches Redis or the model | [§3.15](#315-resident-server-v200) |
> | 32 | (v2.0.0) Hooks of the plugin and of `--init` both registered | Two searches injected per message; the `[user]` and `[claude]` records of one exchange get different `turn_id`s | `--init` registers nothing while the plugin is enabled; the plugin's SessionStart hook reports `--init` hooks | [§14](#14-coexistence-with-other-n3mc-products-v200) |
> | 33 | (v2.0.0) Hook output longer than 10,000 characters | Claude Code moves it to a file and Claude sees a 2,000-character preview | `context_max_chars` (default 9,500) on the hook's and the MCP `search_memory`'s block | [§3.17](#317-context-size-limit-v200) |
> | 34 | (v2.0.0) A hook that raises or exits non-zero when Redis or the server is down | Claude Code shows a hook error on every message; the prompt is lost | `audit.log` first; direct save without a vector; a notice; exit 0 | [§12](#12-claude-code-hooks-v200) |

### 3.1 ID Hierarchy

N3MemoryCore uses 5 ID fields to identify the origin and context of each record:

| ID           | Stored in       | Generated                          | Granularity          | Purpose                                                                                            |
| ------------ | --------------- | ---------------------------------- | -------------------- | -------------------------------------------------------------------------------------------------- |
| `id` (PK)    | Redis hash      | Per record (UUIDv7, time-ordered)  | **One record**       | Unique identifier for each memory — used for deletion and dedup                                    |
| `owner_id`   | `config.json`   | First startup (UUIDv4)             | **Owner**            | Identifies whose data this is — stored as a TAG field and returned in results; filtering is done in Python (see §3.12) |
| `local_id` (agent_id)   | `config.json`   | First startup (UUIDv4)             | **Agent / install**  | UUIDv4 identifier for the install. Stored for compatibility; not used in Lite ranking.            |
| `session_id` | In-memory       | Per server process startup (UUIDv4) | **Server process**   | Identifies which process wrote the record. Override per call via the `save_memory` / `search_memory` argument or the `N3MC_SESSION_ID` env var. **Lite applies the same `b_session` ranking as Pro** (match=1.0, mismatch=0.6 by default). Pinning a consistent `session_id` per project surfaces that project's memories above unrelated cross-project rows in the same Redis instance. Also serves as the filter key for `delete_memories_by_session`. **(v2.0.0)** "Server process" means the MCP entry process (the thin client resolves the default — argument → its `N3MC_SESSION_ID` → its own startup UUIDv4 — and always sends it, §4.6), so the meaning for MCP clients is unchanged. Hook records carry Claude Code's `session_id` from the hook input (§12). |
| `agent_name`   | Redis hash      | Per `save_memory` call (free-form) | **Agent display**    | Human-readable label (e.g. `"claude-desktop"`, `"claude-code"`). **(v2.0.0)** Hook records: always `"claude-code"`. MCP: the `agent_name` argument, else `N3MC_AGENT_NAME`, else the client's `clientInfo.name`, else empty (§4.6). |

### 3.2 Embeddings

- Model: `intfloat/multilingual-e5-base` (default) / Vector: `float[768]` (default)
- **The model is configurable**: set `embedding_model` (default `intfloat/multilingual-e5-base`) and `embedding_dim` (default `768`) in `config.json` so each deployment picks its own quality / resource / language tradeoff (e.g. `intfloat/multilingual-e5-large` at 1024 dims, or a domain-specific multilingual model). The product itself stays language-neutral — the choice belongs to the operator, not baked into the code. `embedding_dim` MUST match the chosen model's output dimension — the RediSearch vector index is built with this value. Changing either the model or the dimension requires a process restart and an index flush (a different dimension is incompatible with existing vectors). **AI MUST NOT auto-change these (see the §3 opening warning) — change them only via a human `config.json` edit.**
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

> **⚠️ Implementation note (redis-py 8.0+ / RESP3)**: As of redis-py 8.0 the default protocol of `from_url()` became **RESP3**. Combining `decode_responses=False` with RESP3 makes `FT.SEARCH` responses arrive as a **dict keyed by bytes**, `Result.from_resp3` fails to parse, and **search always returns 0 results**. **The current implementation absorbs this by (a) pinning `redis<8.0.0` in `pyproject.toml` and (b) also handling the case where `FT.SEARCH` returns a dict response.** If you deliberately use a newer redis-py, **explicitly pass `protocol=2` (RESP2)** on connect (`redis.from_url(url, decode_responses=False, protocol=2)`). Apply this to **both** the live client and the test fixtures.

**TTL**: every `HSET` of `mem:<uuid>` is followed, in the same pipeline, by `EXPIRE mem:<uuid> <ttl_seconds>`. The sibling `mem:sha:<sha1>` guard is taken **before** that pipeline, with `SET … NX EX <ttl_seconds>` (§3.8), so it has the same TTL. Default TTL is 604 800 s (7 d).

**Atomicity and the guard (second review of 2026-11-20)**: the guard and the record(s) are written by **one Lua script**, `SAVE_WITH_GUARD` (§3.8): it runs `SET <guard> <id> NX EX <ttl>` and, only when that succeeded, `HSET` + `EXPIRE` for every record (a standalone record, or the parent document and **all** its chunks), all in one atomic step. `SET NX` still decides which of two simultaneous saves of the same content wins, a record without a TTL is not possible, and **a guard without its record is not possible either**. Until this rule the guard was taken first with its own `SET NX` and the records followed in a `MULTI`/`EXEC` pipeline; the failure handling deleted the guard again, but a process that ended in between (the server stopped, the PC shut down) left the guard alone, and every retry of the same text was answered `duplicate` for up to `ttl_seconds` while nothing was stored. (Before the final review of 2026-11-18, L-04, this paragraph wrongly said that the guard went into the record's pipeline.)

**A search never resets a TTL (decided 2026-11-22)**: every record, chunk, parent document and guard is deleted `ttl_seconds` (7 days) after the save that wrote it. No search, recall, list or other read changes a TTL. Until this decision a search hit reset the TTL of the top `ttl_refresh_top_k` ordinary rows, together with their parent documents, sibling chunks and guards (v1.7.1), so a record that kept coming up in searches outlived the "deleted 7 days after it was saved" that the INSTRUCTIONS (§5 (1)), the tool descriptions (§5 (3)), the rules file (§12.9) and the README tell the user. That promise is the specification; the reset was removed. `ttl_refresh_on_search` is no longer a setting (a value left in an existing `config.json` is ignored, §6). A record and its guard are written with the same TTL by one script (`SAVE_WITH_GUARD`, §3.8) and neither is ever reset, so they expire together without any guard refresh.

> **⚠️ History (v1.7.1, Lite review LM7)**: v1.7.0 reset only `mem:<id>` / `doc:<id>` on a search hit. A record that kept coming up in searches lived on, but its guard expired 7 days after the save, after which the same content could be saved again and the record existed twice. v1.7.1 reset the guard with the record. The decision of 2026-11-22 removed both resets (above); "the record and its guard expire together" now holds without any refresh.

**Conditional writes to an existing record (v1.7.3)**: `HINCRBY` and `HSET` create their key when it does not exist. Every write that `save_memory` does not make itself — the `access_count` increment (§3.14 step 7) and the re-embedded vector of `repair_memory` (§3.10) — therefore runs as **one Lua script that checks the record and writes in the same atomic step**. The scripts (`KEYS` / `ARGV` as in `EVAL`):

```lua
-- INCR_IF_EXISTS   KEYS[1] = mem:<id>        ARGV[1] = field, ARGV[2] = increment
if redis.call('EXISTS', KEYS[1]) == 1 then
  return redis.call('HINCRBY', KEYS[1], ARGV[1], ARGV[2])
end
return false

-- HSET_IF_EXISTS   KEYS[1] = mem:<id>        ARGV[1] = field, ARGV[2] = value
if redis.call('EXISTS', KEYS[1]) == 1 then
  redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
  return 1
end
return 0
```

- `HINCRBY` and `HSET` on an existing key keep its TTL; the scripts never call `EXPIRE` and never create the record.
- Register the scripts once (redis-py `register_script`) and run them through the pipeline (`script(keys=…, args=…, client=pipe)`); redis-py loads a script the server does not know yet. A script is atomic on its own — a pipeline of scripts needs no `MULTI`.
- Plain `EXPIRE` needs no script: it never creates a key.

> **⚠️ Why (v1.7.3, data-flow trace DF1 / DF2)**: v1.7.1 checked first and wrote later — `EXPIRE` in one pipeline and `HINCRBY` + the guard `SET` in a second (§3.14 step 7), `EXISTS` then `HSET` in `repair_memory`. Reproduced on a test Redis: (DF1) a record deleted by `delete_memory` between the two pipelines came back as `mem:<id>` holding only `access_count`, with no TTL, and its `mem:sha:` guard was written again, so the same content was rejected as `duplicate` for 7 days although no record existed; (DF2) a record that expired between `EXISTS` and `HSET` came back holding only `embedding`, with no TTL. Such a hash has no `owner_id`, so no search shows it and no delete removes it — it stays in Redis forever, which breaks "every write sets a TTL" (§3.4). The gap is milliseconds wide, but the Desktop app and the terminal `claude` run separate server processes against the same Redis.

### 3.4 Ephemerality (No Modifications or Optimizations Allowed)

On `save_memory` calls, take the sha1 guard (`SET NX EX`) and write HSET + EXPIRE in one Lua script (`SAVE_WITH_GUARD`), inside the same call (§3.3, §3.8) — no batching, no queuing. The key must have a finite TTL when the `save_memory` response returns.

**The following are absolutely prohibited (even for "performance" or "persistence" reasons):**
- Writing records without an `EXPIRE` (i.e. infinite TTL).
- Enabling Redis RDB / AOF persistence policies that would survive container removal for the sole purpose of extending Lite memory lifespan. (The user may, of course, choose any Redis configuration; the spec simply doesn't rely on it.)
- TTL extension on read (`TOUCH`, `EXPIRE` or any other TTL reset in `search_memory`, `recall_thread`, `list_memories` or the hook's search). **There is no exception (decided 2026-11-22)**: until then `ttl_refresh_on_search` (default `true`) reset the TTL of the top-K search hits; it was removed (§3.3), so every record is deleted 7 days after it was saved.
- Write buffering / deferred pipelines beyond the single save call.

**Reason**: the Lite build's differentiation is explicit volatility; circumventing it erodes the product distinction.

**`enforce_ephemeral()` — turning off Redis persistence (v1.7.1)**: called after every successful connect (startup and every reconnect, §3.9).

1. **Other databases first (v1.8.1)**: run `INFO keyspace`. If any database other than the connected one (the `db` of `redis_url`, default 0) holds keys, go to step 4. If `INFO` is refused, also go to step 4: the other databases cannot be checked.
2. Look at the keys of the connected DB with `SCAN` (`COUNT 1000`), stopping at the first key that does not start with `mem:`, `doc:` or `docsha:` (`mem:sha:` keys start with `mem:`). The RediSearch index itself is not a key.
3. If the connected DB is empty or holds only those keys: `CONFIG SET appendonly no` and `CONFIG SET save ""`.
4. Otherwise (a key that is not N3MC's in the connected DB, keys in another DB, or a refused `INFO`) **do not change the configuration**. Add the notice `shared_redis` (shown like the config notices of §6, on the first successful `search_memory`): `shared_redis: the Redis at <host>:<port> also holds keys that are not N3MC's, so its persistence settings were left unchanged. N3MC memories in it may survive a Redis restart. Use a dedicated container (docker run -d --name redis-stack -p 127.0.0.1:6379:6379 redis/redis-stack-server:latest) for Lite.`
5. If `CONFIG` is refused (managed Redis, `rename-command`, ACL), print one line to stderr and add the notice `config_refused` (same wording rules: what happened, that persistence may still be on, the dedicated-container hint). Never fail the connection because of it.

> **⚠️ Why (v1.7.1, Lite review LM4)**: v1.7.0 changed the persistence of whatever Redis it was pointed at, and the default URL is `localhost:6379`. A user who already ran Redis for something else lost that data at the next Redis restart, without any message. Lite's 7-day promise is kept by the TTL on every key; turning persistence off only keeps N3MC data off the disk, which does not justify touching another program's data. The behaviour was also described only in the Quick Start note.

> **⚠️ Why the other databases are checked (v1.8.1, code review of 2026-11-17)**: `SCAN` sees only the connected database, while `CONFIG SET` changes the whole server. Up to v1.8.0 a Lite pointed at DB 0 of a Redis whose other databases held another program's data turned persistence off for all of them, and that data was lost at the next Redis restart.

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
    turn_id         string      TAG — the turn_id of save_memory (empty string when none)
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
    turn_id         string      the turn_id of save_memory (empty string when none)
    chunk_count     number      number of chunks emitted for this doc
    chunk_ids       string      space-separated list of child chunk ids (for the cascade delete, §3.5)
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
        turn_id         TAG
        importance      NUMERIC
        access_count    NUMERIC
        parent_id       TAG
        embedding       VECTOR FLAT 6 TYPE FLOAT32 DIM 768 DISTANCE_METRIC COSINE
```

- **Primary Key**: UUIDv7 (time-sortable; generated at insert time). The reference implementation uses `uuid_utils.uuid7`.
- **Parent docs are NOT indexed**: `doc:` / `docsha:` keys live outside the `PREFIX 1 mem:` index prefix. Search always hits chunks (`mem:*`) first; the parent is looked up in a post-retrieval step (see [§3.11](#311-verbatim-recall-parent-document--chunks-pattern)).
- **Delete semantics**: `delete_memory` branches on the id:
  - Only records of the configured `owner_id` are deleted (v1.7.1; another owner's id → `not_found`).
  - If a `doc:<uuid>` key exists with that id → delete the parent, `docsha:<sha1>`, and every chunk listed in its `chunk_ids` (older parents without `chunk_ids`: chunks whose `parent_id` matches, §3.11), in a single pipeline (cascade).
  - Otherwise → delete `mem:<uuid>` and its sibling `mem:sha:<sha1>` as before — **only for a standalone record, and the guard only when it points at this record** (2026-11-22, review #6): a child chunk (non-empty `parent_id`) owns no guard, so deleting it deletes only `mem:<uuid>`. For a standalone record, one Lua script deletes the record and deletes `mem:sha:<turn_id>:<sha1>` only when `GET` of the guard equals this id. `delete_memories_by_session` uses the same check for each standalone record. (Until then deleting a chunk deleted the guard of a standalone record with the same text, so the next save of that text was `saved` again and the record was stored twice: reproduced.)

### 3.6 Ranking Formula

Identical to the Pro build:

```
Final Score = (cos_sim × 0.7 + keyword_relevance × 0.3) × time_decay × b_local × b_session
```

Where `b_local` is the **importance coefficient**:

```
b_local = clamp(0.5, 2.0, stored_importance + access_boost)
access_boost = min(access_count_max_boost, access_count × access_count_weight)
```

- `stored_importance`: provided at `save_memory` time (default `1.0`, range `0.5–2.0`).
- `access_boost`: **automatic CPU-only frequency boost**. Every `search_memory` call increments `access_count` by 1 for each memory returned in the top `ttl_refresh_top_k` ordinary hits (weak matches excluded, §3.14 step 6; the setting keeps its old name, but no TTL is reset, §3.3). On subsequent queries that memory receives an additive boost of `access_count × access_count_weight` (default `0.02`), capped at `access_count_max_boost` (default `0.5`). This creates a self-adjusting "frequently-used memories rank higher" loop with zero LLM involvement.

**The boost is off by default since 2026-11-17** (`access_count_enabled: false`; the formula uses `stored_importance` only). Set it to `true` to turn it on. `access_count` is still incremented on every search (§3.14 step 7), so turning the boost on later starts from the counts kept meanwhile.

> **⚠️ Why off by default (2026-11-17)**: measured in N3MC-Pro (2026-11-16, 138 labeled questions, with every search counted as here), turning the access-count boost back on lowered recall@3 by 0.051 (95% interval −0.101 to +0.000): 20 questions got worse and 8 better. Often-returned records rose above more relevant ones for loosely related questions. It was not measured on Lite's own data. An existing `config.json` keeps the value it has; set `"access_count_enabled": false` to follow the new default.

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

(RediSearch BM25 scores are non-negative, but the `abs()` keeps the algorithm identical to the Pro build where FTS5 produces negative scores.)

> **⚠️ Implementation note (fetching the BM25 score — the most frequent bug)**: RediSearch `FT.SEARCH` **does not return scores by default**. Without **`.with_scores()` on the redis-py `Query`, `doc.score` is `None`**, the implementation falls back to `bm25 = 0.0`, and **keyword_relevance is always 0 → the BM25 leg of the hybrid search (weight 0.3) dies completely** (it degrades to vector-only; the lexical rerank compensates, which is what makes it deceptively appear to work). Furthermore, even with `.with_scores()`, the **default scorer is TFIDF**, which on a small corpus returns a flat `1.0` for every doc and does not reflect term frequency. **Explicitly set the scorer to `BM25`**:
> ```python
> Query(raw_q).with_scores().scorer("BM25").paging(0, k).dialect(2)
> ```
> To survive Redis version differences (`BM25` was renamed to `BM25STD`), fall back in the order `BM25` → `BM25STD` → default for robustness. Measured (two docs containing the same term 1× vs 5×), `BM25` differentiates them at `0.20` / `0.56` respectively.

**time_decay**:

$$time\_decay = 2^{-\frac{days\_elapsed}{half\_life\_days}}$$

Default `half_life_days = 3` — deliberately shorter than the 7-day TTL so that `time_decay` is actually informative in the Lite build: a fresh entry scores 1.0, a 3-day-old one exactly 0.5, and a 7-day-old (near-expiry) one ≈ 0.20. This pushes recent context ahead in ranking. This is a Lite-specific tuning; the Pro build keeps a 90-day half-life to match its permanent horizon.

**Lightweight lexical rerank** (post-fusion, before the `access_count` increment):

After the hybrid score above is computed, an optional CPU-only rerank pass boosts each candidate's score by:

- `coverage × rerank_weight` (default weight `0.3`), where `coverage` is the fraction of query tokens that appear in the content. **Both the query and the content are tokenized with `prepare_query` (§3.7)** — NFKC, control characters and ASCII / CJK / full-width punctuation to spaces, whitespace split **augmented with CJK bigram tokens**, lower-cased — so `note,` in the content counts as `note` (the tokens are the lower-cased `prepare_query(text).split()`: a pure-CJK run contributes only its two-character bigrams, never the whole run as one token), and Japanese/Chinese queries contribute a real coverage signal (without bigrams, `.split()` collapses a pure-CJK query into a single token and coverage degenerates to a binary whole-query-substring match). (v1.7.0 split on whitespace only, so a word followed by punctuation never counted — Lite review LM8.)
- `rerank_phrase_weight` (default `0.2`) added when the entire query string appears as a substring of the content (case-insensitive).

Parent-document resolution happens **before** lexical rerank: chunk hits are expanded to their full `doc:<parent_id>` body first, then rerank operates on the full verbatim content. This ensures that a query phrase appearing in a non-matching chunk of the same document still boosts the parent's rank correctly (see [§3.11](#311-verbatim-recall-parent-document--chunks-pattern)).

Set `lexical_rerank_enabled: false` in config to skip this pass (candidates are then sorted by the hybrid score only).

**The `min_score` filter is applied AFTER lexical rerank, on `filter_score` (v1.7.1, below).** A candidate whose fused score sits just below `min_score` can still be kept if the phrase/coverage rerank boost lifts its final score over the threshold, so the score floor is enforced *after* rerank rather than at fusion time. Pruning at fusion time would discard a record that contains the exact query phrase but has a weak embedding before the reranker could rescue it — hence this ordering matters (see [§6](#6-configuration) `min_score`).

**`filter_score` — what the floors judge (v1.7.1, decision LD1)**: every candidate carries two values.

| Value | Formula | Used for |
|---|---|---|
| `score` | `(cos_sim × 0.7 + keyword_relevance × 0.3) × time_decay × b_local × b_session` + rerank boost | The order of the results and the `score=` shown |
| `filter_score` | `(cos_sim × 0.7 + keyword_relevance × 0.3) × b_local` + rerank boost | `min_score` (§3.6) and the relative cut (§3.14 step 5), and the order of the weak matches (§3.14 step 6, v1.7.2) |

`time_decay` and `b_session` decide **which records come first**, not whether a record is shown: a 3-day-old record or one saved before the server restarted (another `session_id`) still appears when it is as relevant as the top hit, just lower in the list. The rerank boost is added to both values.

The v1.7.0 shaping steps that run around this formula (vector floor, newest version first, relative cut) are in [§3.14](#314-search-result-shaping-v170-weak-match-fill-since-v172).

### 3.7 Text Tokenization & Punctuation Handling

**Tokenizer**: RediSearch's built-in tokenizer (whitespace + punctuation split, case-folded). The Porter stemmer used by the Pro build is **not** available here.

**CJK bigram expansion**: Japanese and Chinese text lacks inter-word spaces, so the raw RediSearch tokenizer collapses whole sentences into a single BM25 token and keyword relevance degenerates. To compensate, at save time the server expands every contiguous CJK run in `content` into **overlapping bigrams** (e.g. "記憶装置" → "記憶 憶装 装置") and stores the result in a parallel `content_ngram` TEXT field. BM25 queries apply the same expansion and run `@content:(...) | @content_ngram:(...)`, giving working Japanese partial-match retrieval without touching vector search (the e5 embedding model handles Japanese natively).

**Query cleaning** (`prepare_query`) — before submitting the user's query to RediSearch: NFKC-normalize, replace control characters with spaces, apply `strip_fts_punctuation` (ASCII **and** CJK / full-width punctuation), run CJK bigram expansion, and collapse whitespace. Every RediSearch special character is stripped, so no escaping is needed. Store raw (verbatim) `content` in the hash (RediSearch tokenizes on the fly for the `content` field; `content_ngram` holds the pre-expanded form, built with the same punctuation stripping).

```python
_CTRL_RE = re.compile(r'[\x00-\x1f\x7f]')
_PUNCT_STRIP_RE = re.compile(
    r'[,.<>{}\[\]"\':;!@#$%^&*()\-+=~|\\/?`'
    r'「」『』【】（）〈〉《》〔〕［］｛｝・、。，．！？；：―─…“”‘’]'
)

def prepare_query(text: str) -> str:
    text = normalize_text(text)              # NFKC
    text = _CTRL_RE.sub(' ', text)
    text = _PUNCT_STRIP_RE.sub(' ', text)
    tokens = cjk_bigram_expand(text).split()
    # Tokens with no letter or digit (e.g. a lone '_') are not indexed by
    # RediSearch and leave an empty '@content:()' → syntax error.
    return ' '.join(t for t in tokens if any(ch.isalnum() for ch in t))

def drop_lone_cjk_tokens(q: str) -> str:
    # BM25 query only: drop single-character CJK tokens (particles such as
    # は / の / を left over by bigram expansion) when other tokens remain.
    tokens = q.split()
    kept = [t for t in tokens if not (len(t) == 1 and _is_cjk(t))] or tokens
    # 2026-11-17: hiragana-only tokens (して / ます / どう) too, when
    # another token remains.
    content = [t for t in kept if not all(
        0x3040 <= ord(c) <= 0x309F or c == 'ー' for c in t)]
    return ' '.join(content or kept)
```

`_bm25_search` uses `drop_lone_cjk_tokens(prepare_query(query))` and **joins the remaining terms with OR** (v1.7.1, decision LD3):

```python
terms = drop_lone_cjk_tokens(prepare_query(query)).split()
q = "|".join(terms)
fts_query = f"(@content:({q}) | @content_ngram:({q}))"
```

A record matches when it contains **any** term; BM25 scores records with more and rarer terms higher, and the lexical rerank adds the coverage of all terms, so the ranking does what the AND used to do. A single CJK character left after bigram expansion (`「記憶装置」は？` → `記憶 憶装 装置 は`) is usually a particle and would match almost every Japanese record under OR, so it is still dropped. A query that consists only of single CJK characters is kept as is.

**Hiragana-only tokens (2026-11-17)**: bigrams made only of hiragana (`して`, `ます`, `どう`) are dropped the same way when another token remains. They appear in most Japanese records, so under OR an unrelated question matched through them (`来週の株価はどうなる` matched `今日の天気はどうですか` through `はど` and `どう`). The same rule was measured in N3MC-Pro's bigram index (2026-11-16). A query made only of hiragana-only tokens is kept as is. Lite already indexes CJK bigrams, so the other 2026-11-17 FTS changes of the SQLite editions (3-character grams, a second index) do not apply.

> **⚠️ Why OR (v1.7.1, Lite review LM8)**: RediSearch joins the terms inside `@field:(a b c)` with AND. `quick fox` found a record, but `quick fox elephant`, `what did the quick fox do` and `記憶装置 大阪` found nothing, because no record contained every term; the keyword part (30 %) of the score was then 0. Free made the same change (decision D4).

`prepare_query` also drops tokens that contain no letter or digit (`str.isalnum`), such as a lone `_`: RediSearch does not index them, so a query made only of them became `@content:()` and failed with a syntax error.

> **⚠️ Why control characters and CJK punctuation must go (v1.7.0)**: a control character such as `\x00` left in the query makes `FT.SEARCH` fail with `Syntax error`; the error is caught, so the BM25 leg silently disappears for that query. CJK punctuation is not in the ASCII set, so `「記憶装置」は？` was expanded into bigrams such as `「記` and `は？`. With the terms joined by AND (before v1.7.1), one bigram that never occurs in any document turned the whole BM25 leg into **0 hits** — and Japanese questions almost always end with `？` or contain `「」`. Stripping them on both sides restores keyword matching. A BM25 failure must never fail `search_memory`: catch it and continue with vector results.

**Empty-query rule**: if the cleaned query is empty after stripping, skip keyword search and rank using vector search only.

> **⚠️ Implementation note (don't forget to apply NFKC normalization)**: As defined in [§3.13](#313-encoding-safety), **NFKC normalization is applied to embedding generation, dedup-hash computation, and the BM25 text (`prepare_query` and the stored `content_ngram`)** (`unicodedata.normalize("NFKC", text)`). This makes full-/half-width (`ＡＩ`↔`AI`, `２０２６`↔`2026`) and composed/decomposed forms equivalent for search and dedup. It is **easy to forget**, so to be explicit: normalize **both** the text fed to `embed()` and the SHA1 input used to build the `mem:sha:` / `docsha:` guard keys with NFKC. **Do not apply it to the stored `content` HASH field** (preserve the verbatim contract). Run the same normalization consistently across all of save / dedup-check / delete (otherwise the hash at save time won't match the hash at delete time).

### 3.8 Duplicate Rejection (v1.7.0: exact only)

`save_memory` branches on whether the input body exceeds `chunk_threshold` (default 400 chars).

**(A) Single-chunk path** (body ≤ `chunk_threshold`):

1. **Exact dedup (O(1))** — `EXISTS mem:sha:<sha1(content)>`. If the key exists, return `{"status": "duplicate", "saved": false}`.
2. Compute the embedding.
3. Build the record's fields.
4. **Take the guard and write the record in one Lua script (second review of 2026-11-20)** — `SAVE_WITH_GUARD` with `KEYS = [mem:sha:<sha1>, mem:<id>]` and `ARGV = [<id>, <ttl_seconds>, <number of field/value pairs>, <field>, <value>, …]`. The script runs `SET <guard> <id> NX EX <ttl>`; when that returns nothing (another save of the same content got there first) it writes nothing and returns 0, and `save_memory` returns `duplicate`; otherwise it runs `HSET` + `EXPIRE` for the record and returns 1. (Step 1 only avoids computing an embedding for an obvious duplicate; step 4 decides. v1.7.0's `EXISTS` → write let two simultaneous saves of the same content both through.) If building the fields or sending the script fails, nothing of this save is in Redis, except when the script failed half-way: then delete the guard only if it still holds this save's `<id>` and `mem:<id>` does not exist, and report the error. Never delete a guard that holds another id (it belongs to another save). (Until the final review of 2026-11-18, L-01, only a failed pipeline released it: a failure while the fields were built — a lone surrogate in `agent_name` / `session_id` / `turn_id`, or a value that is not a string, such as `turn_id: 12` from an older MCP SDK that does not check the input types — left the guard, and the content was answered `duplicate` for `ttl_seconds` although nothing was stored.)

**(B) Multi-chunk path** (body > `chunk_threshold`): dedup runs at the **parent-document level** against the full body.

1. **Parent-level exact dedup (O(1))** — `EXISTS docsha:<sha1(full_text)>`. If the key exists, return `{"status": "duplicate", "saved": false, "parent_id": "<existing>"}`.
2. Chunks themselves are **not** given per-chunk sha guards. Reason: sliding-window chunks are overlapping by design and would otherwise reject each other.

If the parent-level check passes, a single `save_memory` call (v1.7.1):
1. Splits the body and **computes the embedding of every chunk first** (no Redis write yet).
2. Builds the fields of `doc:<parent_id>` and of **all** chunks (no per-chunk sha guard; each chunk's `parent_id` field is set to the parent id).
3. Takes the guard and writes them in **one** Lua script, `SAVE_WITH_GUARD` (as in (A) step 4) with `KEYS = [docsha:<sha1(full_text)>, doc:<parent_id>, mem:<chunk_id>, …]`: `SET <guard> <parent_id> NX EX <ttl_seconds>`, then `HSET` + `EXPIRE` for the parent and every chunk. `ARGV` is `[<parent_id>, <ttl_seconds>]` followed, for each key of `KEYS` after the first and in that order, by `<number of field/value pairs>, <field>, <value>, …` (the `ARGV` of (A) step 4 is this layout with one record). When the guard exists already, nothing is written: return `duplicate` with the existing `parent_id`.
4. On a failure, the same rule as (A) step 4: nothing is left half-written, the guard is deleted only when it holds this `parent_id` and `doc:<parent_id>` does not exist, and the same body can be saved again.

> **⚠️ Why (v1.7.1, Lite review LM1)**: v1.7.0 wrote the parent and `docsha:` in a first pipeline and computed the chunk embeddings and wrote the chunks afterwards. A failure in between (connection lost, embedding exception) left a parent and a guard without chunks: the document could not be found (search hits chunks only), and re-saving it was rejected as `duplicate` for 7 days (reproduced).

> **⚠️ No near-duplicate (similarity) rejection (removed in v1.7.0)**: v1.6.x also rejected a new entry when an existing one had `cos_sim >= dedup_threshold` (0.95), for short entries and for whole documents. With `multilingual-e5` that threshold cannot tell "the same content again" from "an updated version" or "a different item with the same shape": a fact saved again with a new value ("the budget is 120" after "the budget is 100"), or a setting sheet re-made with a few values changed, scores ≥ 0.95 and **the new version was silently discarded** (`near_duplicate`) — the user's latest decision never reached memory. Only exact duplicates are rejected now. When both versions come back, `search_memory` puts the newest first and shows `saved=` times ([§3.14](#314-search-result-shaping-v170-weak-match-fill-since-v172)). The `dedup_threshold` config key is ignored.

> **⚠️ One Redis = one installation (second review of 2026-11-17, L01)**: the guards `mem:sha:<sha1>` and `docsha:<sha1>` are made from the body alone — they do not contain `owner_id`. On a Redis used by one installation (the setup this document describes: one PC, a dedicated container, one `config.json`) this is the "exact duplicate" rule above. If two `owner_id`s share one Redis — several PCs or people pointing at the same Redis, or a `config.json` that was re-created with a new `owner_id` while the old records are still there — a body that one owner saved is answered `duplicate` for the other, and nothing is saved for the other owner until the guard expires with the record (`ttl_seconds`, 7 days by default). Nothing is deleted. The Lite build keeps this design; use one Redis per installation.

### 3.9 Startup Sequence & Self-Recovery

> **(v2.0.0) Where these steps run.** Since v2.0.0 the Redis connection, the index and the embedding model live in the **resident server** (`python -m n3mc_mcp.cli --run-server`, §3.15), not in the MCP entry. Read this section with these substitutions:
> - "the server" / "the MCP server" / "the process" = the resident server process. `_main()` = `run_server()` in `cli.py`.
> - "before the stdio loop begins accepting requests" = before uvicorn starts listening. Steps 1–3 run there; step 4 (model) is started in a background thread by the FastAPI lifespan, so `/health` answers within seconds and a tool call that arrives during the load follows the 20-second rule below (keyword-only with the "still loading" notice).
> - The library import of the "Split the load in two" note still runs synchronously before uvicorn starts (it is harmless there and keeps one code path); the model construction runs in the background thread.
> - "the MCP client shows the server as connected" (§4.4): the thin client always answers `initialize` and `tools/list` itself (§4.6).
> - "restart the MCP client" (to apply a setting): the resident server restarts by itself on the next hook, CLI or MCP call after `config.json` changed (`code_version`, §3.15).
> - The MCP entry (thin client) runs none of these steps: it loads no model and never connects to Redis, except for the direct save of §4.6 "When the server is not available".

The server's `_main()` runs these steps in order, **before** the stdio loop begins accepting requests:

1. **Load config** (`load_config()`):
   - Read `config.json` from the data directory.
   - The config lives in `~/.n3mc-workingmemory/config.json`; on first start after upgrading from v1.6.x it is migrated from the legacy platform directory ([§7](#7-data-location)).
   - **Never overwrite a `config.json` the user mistyped** (v1.6.x replaced an unreadable file with defaults, losing the user's settings). See the table in [§6](#6-configuration): an unreadable file is left untouched and the server runs on defaults with in-memory UUIDs for this process; a wrongly typed item is ignored individually. Unlike the Pro build, the Lite does **not** attempt DB-based ID recovery — Redis may already be empty (TTL-expired).
   - Apply `N3MC_REDIS_URL` env-var override (takes precedence over the file).
   - If the file is readable and a field is missing, fill with defaults and persist.

2. **Redis connect & ping**:
   - Build a client from `redis_url`.
   - `PING`. **If it fails**: log a warning to `stderr` with the recovery hint (below) and continue without a working connection. The server stays up — the user can start Redis without restarting the MCP server (see "Redis availability on every tool call").
   - Build the client with `socket_connect_timeout=2` **and `socket_timeout=5`** (v1.7.1). The connect timeout makes a reconnect attempt against a stopped Redis fail within seconds; the read/write timeout makes a Redis that accepted the connection but stopped answering (`docker pause`, a host that has just resumed from sleep, a congested network) fail within 5 seconds instead of blocking forever. A `redis.TimeoutError` is handled exactly like a lost connection (item 3 below).
   - **Why 5 seconds**: the largest single Redis round trip Lite makes is the one-pipeline write of a long document (§3.8 (B)); with the default `chunk_threshold` a 100,000-character body is about 260 chunks. Measured on the test Redis (Windows 11, Docker Desktop, 2026-11-13): that pipeline (parent, guard and 260 chunks with 768-dimension vectors) took 0.031 s (median of 5; max 0.044 s), and one KNN plus one BM25 `FT.SEARCH` took 0.007 s (max 0.008 s) — two orders of magnitude below 5 s.
   - When the client is rebuilt (reconnect), `close()` the old client first so its connection pool is released.

   > **⚠️ Why (v1.7.1, Lite review LH2)**: v1.7.0 set only `socket_connect_timeout`. With the Redis container paused, `ensure_connected()` was still waiting after 20 seconds (reproduced); the tools run synchronously inside the event loop, so every later tool call hung as well and the server never recovered by itself.

   **Redis availability on every tool call (v1.7.0)** — the connection is checked per call, not only at startup:

   1. Before running any tool, the dispatcher calls `Database.ensure_connected()`: if the server is connected it sends `PING`; if it is not connected (or the `PING` fails) it rebuilds the client from `redis_url` and tries `PING` again. When a connection comes back, it re-runs `enforce_ephemeral()` and `ensure_index()` (the index may be missing on a fresh container). `stderr` gets one line when the state changes (lost / restored), not one per call.
   2. If Redis is unreachable, the tool is not run and the response is the **backend error with the recovery hint**: `search_memory` / `list_memories` / `recall_thread` → a `TextContent` starting with `Error: memory backend unreachable. <hint>`; `save_memory` → `{"status": "error", "saved": false, "reason": "<hint>"}`; `delete_memory` / `delete_memories_by_session` → `{"status": "error", "reason": "<hint>"}`; `repair_memory` → `{"status": "error", "message": "<hint>"}`.
   3. If the connection drops **during** a tool (`redis.ConnectionError` / `redis.TimeoutError`), the server marks itself disconnected and returns the same backend error for that tool. The vector / BM25 helpers must **re-raise** these two exceptions instead of swallowing them — swallowing turned a stopped Redis into `_No memories found._`, which the LLM reads as "nothing was saved". Other `FT.SEARCH` errors (e.g. a query syntax error) are still caught and only that leg is skipped.
   4. **Hint text** (`backend_hint(redis_url)`, English, because this server is used worldwide): `Redis Stack is not reachable at <host>:<port>. If Docker itself is not running, start Docker (Docker Desktop) first. Then run: docker start redis-stack (first time: docker run -d --name redis-stack -p 127.0.0.1:6379:6379 redis/redis-stack-server:latest). The memory server reconnects automatically — no restart needed; just retry.` `<host>:<port>` comes from `redis_url` **without the password or user name**.

   > **⚠️ Why (v1.7.0)**: v1.6.x checked Redis only at startup. A server started while Redis was stopped kept answering "Redis Stack is not running" after the user ran `docker start redis-stack` — even `repair_memory` did not reconnect — until the MCP client was restarted. A server whose Redis stopped later returned `_No memories found._` from `search_memory` and a raw `Error 10061 connecting to localhost:6379` from `save_memory`, with no hint. The hint was also Japanese only. (v1.7.0 A4 test: when Docker Desktop itself is stopped, `docker start redis-stack` fails with a Docker API error, so the hint names starting Docker first.)

3. **Ensure RediSearch index** (`ensure_index()`):
   - `FT.CREATE n3mc_idx ON HASH PREFIX 1 mem: SCHEMA ...` as per [§3.5](#35-data-layout).
   - `ResponseError` containing `"already exists"` is success. Then compare the index's vector dimension (`FT.INFO n3mc_idx`, the `DIM` of the `embedding` attribute) with `embedding_dim`; if they differ, add the notice `index_dim_mismatch` (`index_dim_mismatch: the existing index n3mc_idx was built for <old> dimensions but embedding_dim is <new>; new vectors are not indexed. Drop the index (FT.DROPINDEX n3mc_idx) or flush the test DB, then restart.`). (v1.7.1; v1.7.0 accepted the old index silently and new vectors never reached it — Lite review LL8.)
   - **Any other failure does not stop the process (v1.7.1)**. The server keeps running in an "index missing" state and `ensure_connected()` tries `ensure_index()` again before every tool call. While the index is missing, every tool returns the backend error of item 2 below with the hint `The Redis at <host>:<port> does not provide RediSearch (FT.CREATE failed: <reason>). N3MC Lite needs Redis Stack, not plain Redis: docker run -d --name redis-stack -p 127.0.0.1:6379:6379 redis/redis-stack-server:latest`. A connection error during `ensure_index()` is handled like any lost connection.
   - Idempotent: safe to call on every boot.

   > **⚠️ Why (v1.7.1, Lite review LM3)**: v1.7.0 re-raised every other `FT.CREATE` error out of `_main()`. Pointed at a plain Redis (`unknown command 'FT.CREATE'`) — or losing the connection during start — the process ended, and the client showed only a server that would not start, with no hint. §4.4 promises the opposite: the server stays connected and the LLM tells the user what to do.

4. **Preload embedding model** (`get_model()`):
   - Load `intfloat/multilingual-e5-base` into memory so the first tool call is not slowed by the one-time model load.
   - **Build from the local cache first**: construct the model with `SentenceTransformer(<model>, local_files_only=True)` while `huggingface_hub.constants.HF_HUB_OFFLINE` is set to `True` (restore the previous value afterwards). Only if that fails (first start, partial cache) construct it again without these settings, which downloads it. **Why**: with the files already cached, the online construction still asks the Hub whether each file changed, and transformers 5.x also asks for the model's info while loading the tokenizer even with `local_files_only`. On Windows 11 (2026-11-16) the construction took 5.5 s online against 2.0 s cache-only, and the first `search_memory` after `initialize`, which waits for this construction, took 5.7–5.9 s against 1.8–2.0 s — over and under the 3.0 s target of §10-1.
   - **Non-fatal**: if the model fails to load (e.g. offline, HF cache absent), log a warning and continue. The model is retried lazily by later `save_memory` / `search_memory` calls, **but at most once every 60 seconds** (`MODEL_RETRY_SECONDS`): within that window `embed()` returns `None` immediately instead of attempting the download again.
   - **Tool calls during the load wait at most 20 seconds (v1.7.1, decision LD4)**: a tool call that needs the model while the background load holds the lock waits for the lock for at most `MODEL_WAIT_SECONDS = 20`. If the load has not finished by then, `embed()` returns `None` for this call: `search_memory` runs keyword-only and prepends `> ⚠️ N3MC notice: The embedding model (<embedding_model>) is still loading (the first start downloads ~1.1 GB), so this search used keywords only and new entries are saved without vectors. Retry in a minute.`; `save_memory` saves without a vector and adds `"warning": "embedding_unavailable", "detail": "<the same text>"`. Entries saved without a vector get one later from `repair_memory` (§3.10). **Why 20 seconds**: with the Hugging Face cache present, constructing the model takes about 5 s after the import (measured, note below), so a call that arrives during a normal start still gets vectors with a 4× margin; a first-run download takes minutes and would otherwise block every tool call (and the event loop) past typical client tool timeouts. **The hooks do not wait at all (M3, 2026-11-20)**: the saves and the search the hooks send through the server carry `"no_model_wait": true` (§4.5, §12.3 step 4); within those requests `get_model()` never takes or waits for the lock and never loads the model itself — while the model is not ready it returns at once with the notice text above (the search runs keyword-only with that notice, the save goes on without a vector and `/repair` adds it later), and when the model is not loaded and no load is running (and the 60-s retry pause of a failed load is over) it starts the load in a background thread. **Why**: a hook makes three such calls per message (the previous reply, the search, the prompt), so with the 20-s wait every message waited about 55 s (the hook's child timeout) during the first-run download; this is the same decision as L8 (the hook never waits for a server start). The MCP tools and the CLI commands keep the 20-s wait.
   - **Degraded-mode notice (v1.7.0)**: while the model is unavailable, the server says so instead of degrading silently. `processor.model_status()` returns the last load error. `search_memory` prepends `> ⚠️ N3MC notice: <model_unavailable_message>` to its output (every call while degraded, not only the first), and `save_memory` adds `"detail": "<model_unavailable_message>"` next to `"warning": "embedding_unavailable"`. The message (English): `The embedding model (<embedding_model>) could not be loaded (<error>), so search is keyword-only (BM25) and new entries are saved without vectors. The first start downloads the model (~1.1 GB) from Hugging Face: check the network or proxy, or pre-download it. The server retries automatically.` `stderr` gets one line per failed attempt (at most once a minute).

     > **⚠️ Why (v1.7.0, A6 offline test)**: with no network and no cached model, v1.6.x retried the download inside every tool call — `save_memory` took ~24 s and `search_memory` ~12 s each — and `search_memory` gave no sign that the vector leg was off; the only log line was `Cannot send a request, as the client has been closed.`

> **⚠️ Implementation note (load the model in the background, *after* the stdio loop starts)**: Initializing `SentenceTransformer` takes **~30–40 seconds** even with the HF cache present, because of PyTorch init (measured 39s). Running it synchronously *before* the stdio loop starts makes the **MCP `initialize` handshake time out, so the client (e.g. Claude Code) fails to connect**. Start step 4 in the background (e.g. via `ThreadPoolExecutor`) **after `stdio_server()` begins listening**. That lets you return the initialize response right after steps 1–3 (measured ~2s), while the model becomes ready before the first tool call arrives.
>
> **⚠️ Split the load in two (v1.7.0, Windows deadlock)**: importing `sentence_transformers` (torch, sklearn, …) can spawn child processes. On Windows, once the MCP stdio reader has a synchronous read pending on the stdin pipe, creating a child process blocks on that handle — the background load hangs forever inside the import, and the first tool call waits on the `get_model()` lock, so the whole server freezes (observed with v1.7.0's first implementation; v1.6.x avoided it only because it loaded everything synchronously before stdio). Therefore:
>
> 1. **Before `stdio_server()` starts**, import the library synchronously (`import_backend()`), with fd 1 temporarily pointed at stderr so stray output cannot reach the JSON-RPC channel. Measured ~10 s warm.
> 2. **After stdio starts**, construct the model (`SentenceTransformer(...)`, ~5 s warm; the first-run download also happens here) in the background, guarded by the lock below.
>
> Measured on Windows 11: `initialize` answered at 9.5 s, first `save_memory` at 15 s. Any code that spawns a process after stdio has started has the same hazard.
>
> **Thread safety**: the background load above can race with the "first tool call," so `get_model()` may be invoked from two threads at once. Protect the singleton with **double-checked locking** (`threading.Lock`).

Step 1 must complete before the server accepts tool calls. Steps 2, 3 and 4 are best-effort — an unreachable Redis, or one without RediSearch (step 3, v1.7.1), does not stop the process but disables the tools until it is usable again (each tool call retries). **Step 4 must be backgrounded as noted above.**

### 3.10 Repair

The `repair_memory` tool in the Lite build is a **thin idempotent operation**: it calls `ensure_index()` again and, since v1.7.1, **adds missing vectors**. There are no migration markers and no FTS rebuild — Redis records that exist are already indexed by the RediSearch side-channel, and expired records are simply gone.

**Re-embedding (v1.7.1, Lite review LL1)**: entries saved while the model was unavailable (§3.9 step 4) have no `embedding` field and are found by BM25 only. When the model is available, `repair_memory` `SCAN`s `mem:*` (skipping `mem:sha:*`), and for up to 200 hashes of the configured `owner_id` without `embedding` computes `embed(content)` (passage prefix) and writes the field with the conditional script `HSET_IF_EXISTS` (§3.3, v1.7.3) — without changing the TTL. A record that expired between the `SCAN` and the write is skipped (the script returns 0) and is not counted in `reembedded`; a plain `HSET` would recreate it as a hash holding only `embedding`, with no TTL. If the model is unavailable it re-embeds nothing; the response keeps `"message": "index ensured"` and adds `"warning": "embedding_unavailable"` with the model's `detail` (§3.9 step 4).

Return shape: `{"status": "ok", "message": "index ensured", "reembedded": N, "remaining": M}` (`remaining` = entries still without a vector after this call, counted while scanning; call again if > 0), or `{"status": "error", "message": "<detail>"}` on failure.

This is a deliberate simplification versus the Pro build (which runs FTS punctuation migration, vec model-version migration, and an unindexed-row repair loop). The Lite has nothing to migrate because the oldest record is at most 7 d old.

### 3.11 Verbatim Recall (Parent-Document + Chunks Pattern)

When a `save_memory` body exceeds `chunk_threshold` (default 400 chars), the server automatically:

1. Splits the body into sliding windows of `chunk_threshold` chars with `chunk_overlap` overlap (default 100). **Every window advances by at least one character** (v1.7.1): the next window starts after the soft break, or at `end - overlap` after a hard cut, but never at or before the current start (`start = max(start + 1, …)`). §6 also rejects `chunk_overlap >= chunk_threshold`; this rule makes `chunk_text` safe even if called with such values. (v1.7.0 looped forever on a body without breaks when `chunk_overlap >= chunk_threshold` — Lite review LM2.) **Every character of the body is in at least one chunk**: when the last window would be shorter than 20 characters (`_MIN_CHUNK_CHARS`), it is not made a chunk of its own (so short a text gives a poor embedding) — its text is **appended to the previous chunk**, which may then be up to 19 characters longer than `chunk_threshold`. (Up to v1.7.3 the code dropped such a tail: after a soft break, the last few characters of a long body — e.g. `PIN: 4471-ZX.` on its own last line — were in no chunk, so neither BM25 nor the vector search could find the document by them. The parent still held them.)
2. Computes the embedding of every chunk, then (§3.8 (B)) takes the `docsha:<sha1(full_text)>` guard and writes `doc:<parent_id>` — the **full verbatim body** (no truncation; byte-for-byte after the two input rules of §3.13 — the outer `strip()` and, since v1.9.0, code blocks → `[code omitted]`) — together with every chunk in **one Lua script** (`SAVE_WITH_GUARD`, together with the guard), setting each `mem:<chunk_id>`'s `parent_id` TAG field to the parent id. Per-chunk sha guards are skipped (the parent-level exact guard `docsha:` handles whole-document duplicates — see [§3.8 (B)](#38-duplicate-rejection-v170-exact-only)).

`search_memory` integration:

- `hybrid_search` scores and ranks against the chunk index as usual and includes each chunk's `parent_id` in its result dicts.
- The dispatcher post-processes hits **before** lexical rerank: for each result whose `parent_id` is non-empty,
  - If the same `parent_id` has already been emitted → drop as duplicate (keep the highest-scoring hit).
  - First occurrence → `HGET doc:<parent_id>` to fetch the full body and substitute it into the result, replacing the id with the parent id.
- The subsequent lexical rerank (token-coverage + phrase bonus) therefore sees the **full verbatim body** for parent hits, not just the matched chunk — a query phrase that appears in a non-matching chunk of the same document still boosts the parent's rank correctly.
- **After** rerank, the top `ttl_refresh_top_k` ordinary rows (weak matches are skipped, §3.14 step 6) have `access_count` incremented on the underlying `mem:<chunk_id>` (or standalone `mem:<id>`) key — only on the chunk that actually matched, not on its siblings. **No TTL is reset** — not the chunk's, not the parent document's, not the sibling chunks' (decided 2026-11-22, §3.3). The parent and all its chunks are written with the same TTL by one script, so they expire together, 7 days after the save.
- Rendered output tags parent hits with `[doc×N]` (N = `chunk_count`) in the markdown.

`list_memories` integration:

- Issues a `*` (match-all) FT.SEARCH query; `owner_id` and `parent_id` are included in the RETURN fields. Python filters the result set to records whose `owner_id` matches and whose `parent_id` is an empty string (standalone memories only). The `-@parent_id:{*}` TAG filter is not used because UUID values in TAG queries cause parse errors in RediSearch (see §3.12).
- These are merged with parent docs fetched by `SCAN doc:*` (owner-filtered in Python) and sorted by timestamp desc.
- Parent rows render with a `[doc×N]` tag where N is `chunk_count`.

`delete_memory` integration:

- **Owner check (v1.7.1)**: if the record (or parent) belongs to another `owner_id`, return `{"status": "not_found", "id": ...}` and delete nothing — the same scope as `delete_memories_by_session`. (v1.7.0 deleted any id it was given — Lite review LL3.)
- If the id resolves to a `doc:<uuid>` key, the chunks to delete are the ids in its `chunk_ids` field. Only for a parent saved before `chunk_ids` existed, fall back to `FT.SEARCH @parent_id:{<id>}` and then to `SCAN mem:*` with a Python-side `parent_id` check (UUID hyphen issue — §3.12). The parent, `docsha:`, and all chunks are then deleted in a single pipeline.
- Otherwise the usual single-memory delete applies.

**Design invariants**:
- Parent rows are intentionally excluded from the RediSearch index (kept outside `PREFIX 1 mem:`). Ranking therefore always operates on chunk bodies, so a long parent body never distorts time-decay or BM25 norms.
- `stored_importance` and `access_count` live on chunks, not parents. A parent is the "verbatim box" and carries no ranking state.
- As long as the parent is alive, a single chunk hit reconstructs the full body. The parent and all its chunks are written by one `SAVE_WITH_GUARD` script with the same TTL and are never refreshed (§3.3), so they expire together. Should the parent disappear before its chunks (for example, deleted by hand in Redis), orphaned chunks surface as their own short-text hits (a graceful degrade, not a regression).

**Use cases**: when the user wants to retrieve an exact original body later ("save this setting/spec/article so I can pull it verbatim"). For the split between this mode and fact-extraction, see [§1 "Large text handling (two modes)"](#1-vision).

### 3.12 UUID TAG Query Constraint & Python-side Owner Filtering

**Background**: RediSearch TAG field queries (`@field:{value}`) treat the hyphen character (`-`) as a special operator inside the `{...}` delimiters. Because every UUID (e.g. `041500aa-4b54-4f49-ab4c-82045865072c`) contains hyphens in every segment, injecting a UUID into a TAG query causes a parse error regardless of whether the hyphen is backslash-escaped (`\-`) or left bare. This behavior was confirmed on Redis Stack 7.x and affects both KNN hybrid queries and BM25 FT.SEARCH queries.

**Design decision**: Remove `owner_id` (and, where relevant, `parent_id`) from all FT.SEARCH query strings. Instead, include them as `RETURN` fields and filter in Python after the query returns.

**Affected methods and how each is handled**:

| Method | FT.SEARCH query | Python filtering |
|---|---|---|
| `_vector_search` | `*=>[KNN N @embedding $vec AS __dist]` — no owner filter; `owner_id` added to RETURN | Keep only records where `owner_id` matches |
| `_bm25_search` | `(@content:(...) \| @content_ngram:(...))` — no owner filter; `owner_id` added to RETURN | Keep only records where `owner_id` matches |
| `list_memories` | `*` (match-all); `owner_id` and `parent_id` added to RETURN | Keep records where `owner_id` matches **and** `parent_id` is empty string |
| `delete_memory` (cascade) | None when the parent has `chunk_ids` (v1.7.1). Only for an older parent without it: attempt `@parent_id:{<id>}` via FT.SEARCH; on parse error fall back to `SCAN mem:*` | Owner check on the record first; in the fallback path, compare `parent_id` field value in Python |

**Performance note**: Fetching globally and filtering in Python incurs extra network transfer when multiple owner IDs share the same Redis instance. The Lite build assumes a single-user, single-install deployment, so this is not a practical concern. **It is not only a cost when owners do share a Redis (second review of 2026-11-17, L02)**: the vector and BM25 queries take the top candidates of **all** owners (`min(limit × 5, 100)` per query) before the owner filter, so when other owners' records fill those candidates, this owner's matching records are not returned at all. A larger limit does not remove this. Use one Redis per installation (§3.8). Should multi-tenant use arise, consider storing a separate hyphen-free derived field (e.g. `owner_id_tag = owner_id.replace("-", "")`) alongside the canonical `owner_id` and using that field for TAG queries.

**The TAG index schema is preserved**: The `owner_id` and `parent_id` TAG field declarations in §3.5 remain intact. If a future Redis Stack release resolves the UUID hyphen parse error, the Python-side filtering can be moved back into the FT.SEARCH queries without any schema migration.

> **⚠️ Implementation note (`doc.id` returns the full Redis key — double-prefix bugs are common)**: Under RESP2 (`protocol=2`), the **`doc.id` in `FT.SEARCH` results is always the full Redis key (`"mem:<uuid>"`)**, not the bare UUID stored in the HASH `id` field — even when `id` is listed in `RETURN_FIELDS`. Unaware of this, doing `MEM_PREFIX + doc.id` produces a **double-prefixed ghost key `"mem:mem:<uuid>"`**; `EXPIRE`/`HINCRBY` then act on it and create a **TTL-less orphan key** (breaking the "TTL on every write" contract of §3.4).
>
> Fix: use a `_doc_bare_id(doc)` helper that strips the leading `mem:` exactly once to **normalize to the bare UUID**, and store the bare UUID in the row dict's `id`. Always build `MEM_PREFIX`-prefixed operations (`EXPIRE`, `HINCRBY`, `DELETE`) from this bare UUID. The **impact is broad**, so audit every FT.SEARCH consumer:
> - `hybrid_search` **`access_count` increment** (after parent resolution `row["id"]` is the bare parent UUID; stash the original chunk UUID in a separate field before adding `mem:` — the increment goes to that chunk.)
> - `delete_by_session` **single-memory deletion** (`MEM_PREFIX + bare_id`)
> - `list_memories` **display ID** (unify the user-facing ID to the bare UUID)
> - `delete_memory` cascade (also normalize to bare UUID in the `@parent_id` SCAN fallback)
>
> Also note that **`SCAN mem:*` matches `mem:sha:*` (STRING type) and `SCAN doc:*` matches `docsha:*` (STRING type)**. Exclude them by prefix before reading HASH fields, or catch the `WRONGTYPE` exception (otherwise `HGET` fails with `WRONGTYPE`).
>
> **Fetching `chunk_count` during parent resolution**: when folding a chunk hit into its parent, fetch not only `content` but also **`chunk_count` (and `chunk_ids` / `turn_id`)** from `doc:<pid>`, otherwise the `[doc×N]` tag in the results is always `[doc×0]` (fetching them in a single pipeline is efficient).

### 3.13 Encoding Safety

Two encoding-safety layers run before any tool body executes. They mirror the
Free build's defenses (`n3mc_hook.py` stream reconfigure + `core/processor.py`
`sanitize_surrogates`) so a Lite deployment offers the same baseline reliability
on Windows-Japanese hosts.

**(1) stdio UTF-8 reconfigure** — at module import time of `n3mc_mcp.server`
(v2.0.0: and at module top of `n3mc_mcp.cli`, `n3mc_mcp.lite_hook` and
`n3mc_mcp.lite_stop_hook`, before the first `sys.stdin.read()` — Free's rule)
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

**(2) Lone-surrogate sanitization** — (v2.0.0: also the whole hook JSON
right after parsing, the previous reply read from the transcript, and the
`--search` argument, as in Free §4.5) every `save_memory.content`,
`save_memory.agent_name`, `save_memory.session_id`, `save_memory.turn_id`
(these three since the final review of 2026-11-18, L-01; a value without
surrogates is unchanged) and `search_memory.query` is passed through
`sanitize_surrogates()` before any `.encode("utf-8")` call. Lone UTF-16 surrogate halves (`U+D800`–`U+DFFF`)
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

**Invisible-only input contract (v1.7.0)**: `save_memory` also rejects
content that has **no visible character** — every character is whitespace,
a control or format character (Unicode category `Cc` / `Cf`, e.g. `U+0000`,
`U+001B`, `U+200B`, `U+FEFF`), a separator (`Zs` / `Zl` / `Zp`), a
variation selector (`U+FE00`–`U+FE0F`, `U+E0100`–`U+E01EF`), or (v1.7.1) one
of the blank-looking letters `U+115F`, `U+1160`, `U+3164`, `U+FFA0` (Hangul
fillers) and `U+2800` (braille blank) — they have category `Lo` / `So` but
render as nothing (Lite review LL5). Combining marks on their own stay
"visible". The reply is
`{"status":"error","saved":false,"reason":"empty content (no visible
characters)"}`. The check is `processor.has_visible_text()`; it only decides
*whether* to save and never changes what is saved — content with at least one
visible character (e.g. `A\x00B`, a lone combining mark `U+309A`) is stored
verbatim as below. Whitespace-only input keeps the plain `empty content`
reason.

> **⚠️ Why (v1.7.0, A10 encoding test)**: `str.strip()` removes whitespace but
> not `U+0000` or `U+200B`, so a payload such as `\x00` or a lone zero-width
> space was saved as a memory that displays as nothing, cannot be told apart
> in `list_memories`, and only adds noise to search.

**What is NOT sanitized (intentionally)** — the following code points
**round-trip verbatim** through save and recall (consistent with the §3.11
parent-document verbatim contract). They are preserved for display fidelity,
graphical reversibility, variation-selector information, and backward
compatibility:

- **C0 control characters including `U+0000` (NUL)** — Redis HSET / RediSearch
  HASH are binary-safe, so strings containing NUL round-trip unchanged. Input
  like `A\x00B` is stored as 3 characters (it does not collapse into the
  empty-content rejection path). Content made **only** of such characters is
  rejected (invisible-only contract above).
- **Zero-width joiners and spacers** (`U+200B`, `U+200C`, `U+200D`, `U+FEFF`,
  …) — they may affect search-time tokenization (RediSearch behavior) but
  are kept in the stored body.
- **Variation selectors** (`U+E0100`–`U+E01EF`, `U+FE00`–`U+FE0F`) — glyph
  hints like `神󠄀` survive **verbatim** (including combinations with CJK
  Extension code points).
- **Combining diacritics** (`U+0300`–`U+036F`, `U+3099`–`U+309A`, …) — when
  input arrives in decomposed form, the stored body stays decomposed. NFKC
  normalization is applied **only** to the embedding input and the dedup
  hash key (§3.7 / §3.8), so search and dedup treat decomposed and
  precomposed forms as equivalent, while `list_memories` / `search_memory`
  responses respect the caller's input form.

In short: "the bytes you pass to `save_memory` come back exactly as written,
with only lone surrogates removed" — apart from one outer `strip()`: like
MCP Pro, `save_memory` strips leading and trailing whitespace (spaces, tabs,
newlines) once at the input boundary, so `' \nAAA\n '` is saved and returned
as `AAA`, and a body that differs from a saved one only in surrounding
whitespace is an exact duplicate of it. Everything inside the body is kept.
(v1.8.1: this sentence states what every release has done; the code review of
2026-11-17 found that the text above did not say so.) The second exception is
code blocks (v1.9.0, below). This minimal-sanitization contract is a
deliberate design choice that prevents silent transformations from breaking
the verbatim guarantee.

**Code blocks are replaced when saved (v1.9.0)**: right after the
`skip_code_blocks` check (§6), `save_memory` replaces every fenced code block
with `[code omitted]` — `purify(text)` = `re.sub(r"```[\s\S]*?(?:```|\Z)",
"[code omitted]", text)` (an unclosed fence runs to the end; inline backtick
spans stay). The replaced text is what is hashed for dedup, chunked, embedded,
indexed and stored. `search_memory`, `list_memories` and `recall_thread` still
apply `purify` to what they show, so records saved by 1.11.x or earlier (which
kept the code in Redis until their TTL ran out) look the same.

> **⚠️ Why (v1.9.0, 2026-11-17)**: since 2026-04 Lite stored code blocks but
> always showed them as `[code omitted]`, so no tool could return the code; it
> only took space, influenced search, and made the "verbatim" promise read
> wrongly. N3MemoryCore remembers what was said and decided, not source code
> (the code belongs in the project's files). What users see does not change;
> a word that appears only inside a code block no longer finds the record, and
> two bodies that differ only inside their code blocks are exact duplicates.

**Pre-1.2.0 mojibake recovery is intentionally NOT ported** from Free. That
routine retroactively rewrote rows that earlier Free builds had decoded
through cp932; Lite has no historical data to retrofit because every entry
ages out within `ttl_seconds` (default 7 days). Adding a recovery routine
would only run on data that the user already accepted as ephemeral.

### 3.14 Search Result Shaping (v1.7.0; weak-match fill since v1.7.2)

v1.6.x ranked every KNN candidate. With `multilingual-e5`, even unrelated records sit at cosine 0.80–0.85 to almost any query, so `search_memory` returned long lists of loosely related rows, and an updated version of a fact or document could rank below the old one (the old one has accumulated `access_count`). `search_memory` therefore shapes its candidates in this order:

1. **Time window (pre-filter)** — `since` / `until` are applied inside the KNN and BM25 queries (see [§4.3.1](#431-retrieval-extension-tools)), not after ranking.
2. **Vector floor** — drop vector candidates whose `cos_sim < vector_min_cos` (default `0.85`). A dropped row can still enter through BM25 (with `cos_sim = 0.0`). **0.85 was measured with `multilingual-e5-base`; re-measure it when `embedding_model` is changed.**
3. **Fusion score** — §3.6 formula, then parent-document resolution (§3.11), then lexical rerank (§3.6), then the `min_score` floor (§3.6) on `filter_score` (v1.7.1).
4. **Newest version first** (`version_order_enabled`, default `true`) — when the user re-makes a setting or a fact, both the old and the new version are stored (near-duplicates are no longer rejected, §3.8) and both come back.
   - For each result take a **representative embedding**: a standalone row uses its own `embedding`; a parent document uses the `embedding` of its first chunk (`chunk_ids[0]`). Skip rows whose representative text (the standalone `content`, or the first chunk's `content`) is shorter than `version_min_chars` (default `100`) — short rows are too similar to each other to tell versions from different facts.
   - Two results whose representative cosine is `>= version_sim_threshold` (default `0.98`) are versions of the same content; group them transitively.
   - Inside each group, re-order the rows newest first by `(timestamp_epoch, id)` (`id` is a UUIDv7, so rows saved in the same second are still ordered). **Every position keeps its score** — only the rows move, so the newest version inherits the group's best score. Mark **every row of a group of two or more** with `_version_group` (the group number, starting at 1) — also when the newest version was already on top and nothing moved, so the old versions survive the relative cut below. Nothing is deleted. Parent documents saved before `chunk_ids` existed have no representative and are not grouped.
   - 0.98 was measured with `multilingual-e5-base`: the same setting re-made → 1.0; settings with the same shape but different values → 0.88–0.91; different items inside one long reply → at most 0.95.
5. **Relative cut** — drop results whose **`filter_score`** is below `relative_min_score_ratio` (default `0.6`) × the highest `filter_score` (v1.7.1), except rows marked `_version_group` (old versions stay visible next to the new one). `0.0` disables it. Step 4 moves rows while each position keeps its `score`; `filter_score` moves with the position in the same way.
6. **Minimum-results fill with weak matches (v1.7.2, Lite review LA1)** — when fewer than `min(min_results, limit)` results remain after step 5 (`limit` is the `search_memory` argument, or `search_result_limit`), add the shortfall from the candidates that **this search fetched but steps 2, 3 or 5 dropped**:
   - Weak candidates are: vector candidates dropped by `vector_min_cos` in step 2, **with their actual `cos_sim`** (not the `0.0` they get as BM25-only rows), and results dropped by `min_score` (step 3) or the relative cut (step 5). A row dropped in step 2 that also hit BM25 is one candidate with its actual `cos_sim` and its `keyword_relevance`. Their `filter_score` and `score` use the §3.6 formula with the same parent-document resolution (§3.11) and lexical rerank as normal rows. Candidates whose `filter_score` is below `1e-6` (no similarity and no keyword signal; the cosine distance of float32 vectors leaves about `1e-7` for orthogonal vectors, so do not compare with exactly `0`) are never added. The `since` / `until` window applies as for normal rows (weak candidates come from the same pre-filtered queries).
   - Take them in **descending `filter_score`** (ties: newer `(timestamp_epoch, id)` first). `score` is the `score=` shown, but it does not decide the order of weak matches.
   - **One row per parent document**, counted together with the rows already in the results: a weak candidate that resolves to a parent document already in the results (or already added as a weak row), or to the same standalone `id`, is skipped and the next candidate is tried.
   - Stop when the total reaches `min(min_results, limit)` or the candidates run out. Mark every added row `_weak`. Weak rows always come after every normal row.
   - **Weak rows are not counted** (step 7): no `access_count` increment. (A search changes no row's TTL, §3.3 / §3.4.)
   - `min_results: 0` disables this step. **An empty or whitespace-only query skips it** (no weak rows): the vector search still returns rows for an empty query (it embeds the empty string), and filling a list for a query with no content would only add noise.
   - Weak rows take no part in steps 4 and 5 (they are not moved by version ordering and do not count toward the relative cut's top score).
7. **`access_count`** for the top `ttl_refresh_top_k` **normal** rows (§3.11; rows marked `_weak` are skipped), then truncate to `limit`. **Increment `access_count` only on a key that still exists (v1.7.1, Lite review LL4)**: run the conditional script `INCR_IF_EXISTS` of §3.3 on the chunk that matched (one pipeline of scripts). A key that expired or was deleted after the search would otherwise be recreated by `HINCRBY` as a hash holding only `access_count` and no TTL. **No `EXPIRE` and no guard write (decided 2026-11-22)**: a search changes no TTL (§3.3). (Until then this step ran `EXPIRE` in pipeline 1 and `INCR_IF_EXISTS` plus `SET_GUARD_IF_EXISTS` in pipeline 2.)

> **⚠️ Why (v1.7.1, Lite review LH1; decision LD1)**: v1.7.0 compared the final score, which already contains `time_decay` (3-day half-life) and `b_session` (0.6 for another session). With equal relevance, a record about 2.2 days older than the top hit (0.5^(2.2/3) ≈ 0.6) and every record from another session fell under the cut — and because the default `session_id` is a new UUID per server process, every record saved before the MCP server last restarted counts as another session. Reproduced: with a fresh current-session record at cos 0.86 (score 0.602, cut 0.361), a 3-day-old same-session record at cos 0.95 (0.332) and 1-day-old other-session records at cos 0.99 (0.330) and 0.95 (0.317) were all dropped. Lite is a 7-day working memory; one recent hit hid everything older than two days.

> **Note: Why weak matches (v1.7.2, Lite review LA1)**: in a measurement with the real model (50 records, 26 questions, searched from another session) every question got **at most one** result (0.9 on average): recall@1 and recall@3 were both 22/26, so when the first hit was wrong the right record was never shown, and 2 of 26 questions got nothing. Filling up to 3 rows gives the LLM the next candidates under their own heading. **Cost**: every search can carry up to 3 more rows, mostly weakly related, and even an unrelated question always gets them. In Free (same corpus) the fill raised recall@3 from 23/26 to 24/26 and added about 2 weak rows per question; Lite has not been measured yet — re-run the measurement after implementing. The heading, the `min_results` key and the instruction wording are shared with N3MC Free.

**Output format** (`search_memory` markdown, v1.7.0; weak matches added in v1.7.2):

```markdown
> ⚠️ N3MC notice: <only if a notice is pending, §6, or the embedding model is unavailable, §3.9>

_If several versions of the same content appear, the one with the newest `saved` time is the current one._

### [doc×3] [<id>] turn=<turn_id> score=0.812 saved=2026-11-13 10:34 (UTC+0900)
<content>

#### Possibly related (weak matches — may be unrelated)

### [<id>] turn=<turn_id> score=0.412 saved=2026-11-12 21:15 (UTC+0900)
<content>
```

- `saved=` is the stored UTC timestamp converted to the **server's local time** with its offset (`_fmt_saved`).
- The version sentence is printed once, above the first hit, whenever there is at least one hit.
- `turn=` is omitted when the row has no `turn_id`; `[doc×N]` only for parent documents.
- **Weak matches (v1.7.2)**: rows marked `_weak` are printed after every normal row, under `#### Possibly related (weak matches — may be unrelated)`, in the same `### …` form. The heading is printed only when there is at least one weak row.
- **No normal rows but weak rows (v1.7.2)**: print `_No memories found._` (the text of an empty result), then the weak-matches heading and rows. The version sentence is not printed in this case. With neither normal nor weak rows, the output stays `_No memories found._`.
- **(v2.0.0)** The same rendering is used inside the hook's `# Recalled Memory Context` block (§3.17, §12). Lite has **no** separate Q-A exchanges block (Free's `## Previous matching Q-A exchanges`): a hit is a record or a whole parent document, and the conversation around it is read with `recall_thread` / `--recall-thread`.

### 3.15 Resident Server (v2.0.0)

Everything in this section is new in v2.0.0 and is taken from N3MC-Free §3 "Resident FastAPI Server", with Lite's names. The server runs Lite's core — the `Database` of §3.3–§3.14, the embedding model of §3.2 / §3.9, and the tool handlers of §4.3 — and nothing else holds the model.

**Why a resident server**: the hooks run as a new process on every message; loading the model there would cost 13–40 s per message (§3.9). Up to v1.12.2 each MCP client started its own server process and each loaded the model; one resident server loads it once for the hooks, the CLI and every MCP client.

- **Port**: `server_port` in `config.json`, default **`18524`**. The other N3MC products use 18520 (Free), 18521 (Pro), 18522 (PostgreSQL edition) and 18523 (Ollama edition), so Lite can run on the same PC as any of them (§14).
- **Bind address**: `bind_host` (default `127.0.0.1`). **Keep the default.** The server has no authentication; any other value exposes the memory to the network. If another value is configured, print a warning to stderr at startup.
- **Start**: `python -m n3mc_mcp.cli --run-server` (`run_server()`), started by `ensure_server` (below) from the CLI commands that use the server, the hooks and the MCP thin client. The PID file is created with an exclusive create (`open(path, "x")`) so that two processes checking at the same time do not both start a server.
- **Communication**: HTTP over TCP (`http://127.0.0.1:<server_port>`), JSON bodies, UTF-8. **Never through a proxy**: open every request with `urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=…)`, not `urllib.request.urlopen`. (Free F16: with `HTTP_PROXY` set and no `NO_PROXY` for `127.0.0.1`, every request went to the proxy and the server looked down.)
- **Host header check**: every endpoint returns 403 unless the `Host` header is `127.0.0.1:<port>` or `localhost:<port>` (DNS rebinding from a web page could otherwise read the memory). The CLI and the thin client always send `Host: 127.0.0.1:<port>`; tests set the header explicitly (FastAPI's `TestClient` sends `testserver`).
- **Requests with an `Origin` header are refused (from Free v1.9.1, decision D8)**: every endpoint returns 403 when the request carries an `Origin` header, whatever its value (`null` included). A browser adds `Origin` to the requests a web page sends to another site (`fetch`, a form POST); the server listens on `127.0.0.1` without authentication, so a page the user opens could send requests to it, and the `Host` check does not stop them (their `Host` is `127.0.0.1:<port>`). The hooks, the CLI and the thin client never send `Origin`, so nothing they do changes. Check it in the same place as the `Host` header, before any endpoint runs.
- **Endpoints do not block each other**: every endpoint that touches Redis or the model is a plain `def` (FastAPI runs it in its thread pool), so `/health` keeps answering during a long save or `/repair`. Consequences for Lite's core, which ran in one event loop up to v1.12.2: one `Database` per server process (redis-py's client and pool are thread-safe); `ensure_connected()` and the reconnect of §3.9 run under one `threading.Lock`; the notice list (`cfg["_notices"]` and the "already shown" set) is changed under a lock; `get_model()` keeps its double-checked lock (§3.9).
- **Timeouts (client side)**: `/health` and `/shutdown` 5 s; `/buffer`, `/search`, `/repair` 30 s (Free); `/tools/<name>` 300 s (a `save_memory` of a long document embeds hundreds of chunks on the CPU, and an MCP client waits for the tool result anyway). After asking a server to stop, wait until its port is free for at most 15 s, polling every 0.3 s; a process that must be ended is `terminate()`d, waited for 5 s, then `kill()`ed. `/shutdown` answers first and ends the process 0.3 s later. uvicorn runs with `log_level='error'`.

#### Server identity (`/health`)

`GET /health` returns `{"status": "ok", "root": "<absolute path of the data folder>", "code_version": "<16 hex chars>", "pid": 12345, "redis": "<host>:<port>/<db>"}`.

- `root`: the absolute data folder (`N3MC_DATA_DIR` or `~/.n3mc-workingmemory`), compared with `os.path.normcase(os.path.abspath(...))`.
- `code_version`: the first 16 hex characters of a SHA-256 over the contents of `app.py`, `tools.py`, `tooldefs.py`, `instructions.py`, `context.py`, `database.py`, `processor.py`, `config.py`, `paths.py` and `cli.py`, followed by the contents of `config.json` (the key-sorted `json.dumps(..., sort_keys=True)` when it parses as a JSON object, otherwise the raw bytes; nothing when the file is missing). Whitespace or key order alone does not change it. **Editing `config.json` therefore restarts the server on the next call**, which is how a changed setting takes effect. The server computes this value **once, when it starts, from the same `config.json` content that `load_config()` used — the bytes it parsed, or after its fill-in write the bytes it wrote — never from a second read of the file; this happens before it connects to Redis or loads the model, and `run_server` passes it to `create_app(..., code_version=...)`** (2026-11-22, review #9: reading the file a second time left a window in which the server ran with the first contents and reported the hash of the second) — and `/health` keeps returning that value (L4, 2026-11-22: computed later, a change of `config.json` while the server was starting gave a server that ran with the first contents but reported the hash of the second, so it was not replaced until the next change); recomputing it on every `/health` request would make a running server always match the files on disk, so it would never be replaced.
- `pid`: the server's process id.
- `redis` (2026-11-20, L7): the Redis the server uses (`redis_url` after the `N3MC_REDIS_URL` override of the **server's** environment, §3.3), without user name or password. **When `redis_url` cannot be parsed** (for example a port that is not a number), `redis_display` returns the fixed text `<unparseable redis_url>` and never any part of the input (2026-11-22, review #1: it returned the whole input, so a password could reach `/health`, stderr, `notices.json` and `audit.log` through the `redis_url_mismatch` notice). Every message that names the Redis address uses `redis_display` or `_hostport`. The server inherits the environment of the process that started it (a hook, the CLI or an MCP entry), so `ensure_server` compares it with its own effective `redis_url`: when they differ, it uses the server anyway (the data folder is the same) and emits the notice `redis_url_mismatch` (`The N3MC Lite server uses Redis at <server's>, but this process is set to <own>. Put redis_url into <config.json path> instead of setting N3MC_REDIS_URL per application, then restart the server (it restarts after config.json changes).`). It never replaces the server for this alone: two applications with different settings would stop each other's server on every call. **Clearing it (2026-11-22)**: when our own server answers and its `redis` equals this process's effective `redis_url`, `ensure_server` clears `redis_url_mismatch` (`_clear_notice`). (Until then nothing cleared it: after the setting was fixed, the notice was still shown once a day for good.)

#### Server check and replacement (`ensure_server`)

`_health_decision(health)` returns one of `'ok'`, `'stale'`, `'foreign'`, `'legacy'`, `'none'`:

| Response | Verdict | Action |
|---|---|---|
| Same `root`, same `code_version` | Healthy | Use it |
| Same `root`, different `code_version` | Our own stale server | Stop it and start a new one |
| Different `root` | Another folder's server | **Do not stop it. Do not use it.** See "When the port belongs to another folder's server" |
| No `root` | Needs verification | Stop it only if psutil confirms it is our own server; otherwise treat it as another folder's server (`root` shown as `(unknown: the server did not report its folder)`) |
| No response | No server | See "PID file handling" |

- Run by: `--hook-submit`, `--save-claude-turn`, `--search`, `--buffer`, `--list`, `--recall-thread`, `--delete`, `--delete-session`, `--repair`, and the MCP thin client (§4.6). Not by `--init`, `--stop` or `--run-server`.
- **How to stop (`_stop_stale_server`)**: `POST /shutdown` with `{"root": "<our data folder>"}`. Only a server that does not know `/shutdown` is terminated, after psutil verification. **Never stop a process by the number in the PID file** (the number may have been reused).
- **psutil verification (`_is_own_server_process`)**: take the PID from `/health`'s `pid`, or from the process listening on the port (`psutil.net_connections`). It is "our own server" only if its command line contains both `n3mc_mcp.cli` and `--run-server`, and the data folder resolved from its `N3MC_DATA_DIR` environment variable (default `~/.n3mc-workingmemory`) equals ours. psutil missing, permission denied or anything not matching → "cannot verify": do not stop it; emit the notice `stale_server` ("Cannot stop the old N3MC Lite server. Another program may be using server_port <port>. Check server_port in config.json and restart manually."). If our own stale server cannot be stopped, keep using it (it serves the same Redis).
- **PID file handling**: when `/health` does not answer and the process of the PID-file number is verified to be this folder's server (it is starting), keep the PID file and poll `/health` for up to 60 s (every 0.5 s). Still starting after 60 s (the first start downloads the model): do not start another server, do not delete the PID file; this call goes on without the server (below). Otherwise (dead process, reused number) delete the PID file and start the server. If `import psutil` fails anyway, treat a PID-file process as "still starting" for 60 s after the file's modification time and as dead afterwards.
  - **The server moved to another port (M1, 2026-11-20)**: before waiting for a verified PID-file process, check whether it is listening on a port other than the current `server_port` (psutil, `LISTEN` connections of that PID). This happens when the user changed `server_port` while the server ran: `/health` on the new port never answers, so without this check the server never restarted (the CLI waited 60 s on every call, the hooks saved without vectors for good). For each such port whose `/health` reports this folder's `root`, send `POST /shutdown` (`{"root": "<own data folder>"}`) to that port, wait until that port is free (15 s, as above), delete the PID file if it still holds that PID, and go on to "Starting the server" without waiting. A port whose `/health` does not answer or reports another `root` is left alone; the process is never ended by its PID-file number. A server that is still starting listens on no port, so the wait above applies as before. The check needs the server's own PID in the PID file, which the server writes itself (§13 "PID file", L1). The server's `Host` check uses the port it was started with (§4.5), so the old server still answers on its own port.
- Once a server we started answers, clear the notices `stale_server`, `port_conflict` and `server_start_failed`.

#### Starting the server (`_start_server`)

1. `subprocess.Popen([*python_cmd(), "-m", "n3mc_mcp.cli", "--run-server"], stdin=DEVNULL, stdout=DEVNULL, stderr=<server.log opened for append>, …)`; truncate `server.log` first when it is larger than 1 MB. Detached: on Windows `creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`, elsewhere `start_new_session=True`, so the server outlives the hook or MCP entry that started it. Except with an MCP client that puts its child in a Job Object that ends the whole process tree when closed (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` without breakaway): there the server may end together with the entry. The Python `mcp` stdio client on Windows ended it when checked on 2026-11-20; with `mcp` 1.27.0 on Windows 11 (checked 2026-11-22) the server kept running after the entry closed. Both outcomes are correct. No memory is lost: the next call starts the server again, and saves in between are direct saves without vectors (§4.6). The `-m` form is required (package-relative imports); never start it by file path. Its stdout never reaches a hook's or the MCP entry's stdout.
2. Poll `/health` every 0.5 s for up to 60 s. If the child exits meanwhile (`proc.poll() is not None`), stop waiting at once.
3. On that early exit: emit `server_start_failed` ("The N3MC Lite memory server could not start (exit code <n>). See <server.log path> for the reason."), write the time to the marker file `server_start_failed` in the data folder, delete the PID file if it holds the child's PID, and go on without the server. If `Popen` itself raises, go on without the server (one stderr line).
   - **The server process records its own start failure too (M2, 2026-11-20)**: when `run_server` ends with an exception, or with `SystemExit` whose code is not 0 / `None` (uvicorn exits this way when it cannot bind the port), it emits `server_start_failed` ("The N3MC Lite memory server could not start (<exit code n \| ErrorType: message>). See <server.log path> for the reason.") and writes the marker itself before exiting; the exception is not swallowed. **Why**: the hooks call `ensure_server(cfg, max_wait=0)` and never see the child exit, so without this a server that cannot start was started again on every hook, with no notice and no 5-minute pause, and the hook's answer stayed "still starting".
4. While the marker is younger than 5 minutes, do not try to start the server again; go straight to the no-server path. Delete the marker and clear the notice once a server answers.
5. A child still alive after 60 s is still starting: leave it running ("still starting" rule above).

#### Safe import path (2026-11-21)

`python -m <module>` puts the current folder first on `sys.path`. The hooks and the commands the rules file gives to Claude run in the folder the user has open in Claude Code. Without the two rules below, a file in that folder with the name of any module N3MC imports (for example `shlex.py` or `fastapi.py` in a repository the user has just cloned) is imported, and runs, inside N3MC.

**Rule 1 — entry modules drop the current folder.** Every module of the package that can be the program's entry — the ones that end with `if __name__ == "__main__":`; here `cli.py`, `lite_hook.py`, `lite_stop_hook.py`, `server.py` and `__main__.py` — has the following block as its **first statements**, right after the docstring and before any other import (in the hook modules, the UTF-8 block comes after it):

```python
import os as _os
import sys as _sys

if __name__ == "__main__":
    # Safe import path. Started as `python -m <package>.<module>`, Python has put the current folder
    # first on sys.path, so a file there named like a module imported below would run inside this
    # program. Drop that entry, unless this package itself lives in the current folder (a source
    # checkout run in place). Nothing else may be imported before this block.
    try:
        _cwd = _os.path.normcase(_os.path.abspath(_os.getcwd()))
        _pkg_parent = _os.path.normcase(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
        if _sys.path and _os.path.normcase(_os.path.abspath(_sys.path[0])) == _cwd and _pkg_parent != _cwd:
            del _sys.path[0]
    except Exception:  # noqa: BLE001
        pass
```

- It acts only when the module is the program (`python -m …`). Imported by a console script or by a test, it does nothing: a library must not change the `sys.path` of the program that imports it.
- `os` and `sys` are already loaded when the block runs, so importing them reads no file.
- The entry is kept when the package itself lives in the current folder (a source checkout run in place, not installed): without it the package could not be found.

**Rule 2 — children are started with `python_cmd()`.** `paths.py` has:

```python
def python_cmd():
    """The command that starts a child Python: [sys.executable], plus -P on Python 3.11 and later so
    that the child does not put the current folder on sys.path (see the safe-path block at the top of
    the entry modules). -P is left out when this package itself lives in the current folder (a source
    checkout run in place), because the child could then not find the package."""
    import sys
    cmd = [sys.executable]
    try:
        cwd = os.path.normcase(os.path.abspath(os.getcwd()))
        pkg_parent = os.path.normcase(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if sys.version_info >= (3, 11) and pkg_parent != cwd:
            cmd.append("-P")
    except Exception:  # noqa: BLE001
        pass
    return cmd
```

Every child Python process — the server of "Starting the server" above and the calls of the hook modules — is started as `[*python_cmd(), "-m", …]`, never as `[sys.executable, "-m", …]`. `-P` (Python 3.11) keeps Python from adding the current folder at all, which also covers a folder there that has the name of this package.

**What the rules do not cover**: a folder or file in the current folder with the name of this package itself (`n3mc_mcp`), when the program is entered by `python -m …` typed in that folder (the commands the rules file gives to Claude, and a hook registered in the `python -m` form). The block is never reached, because the other package is the one that is imported. On Python 3.10 the same holds for the children (there is no `-P`), and a `runpy.py` in that folder runs as well. Entering through the console scripts, or with `python -P -m …` on Python 3.11 and later, avoids it. (Checked 2026-11-21: with `shlex.py` in the current folder, `python -m n3mc_mcp.cli` ran that file before these rules and does not run it with them; a folder `n3mc_mcp/` there still runs.)

#### `/shutdown`

`POST /shutdown` with `{"root": str}`: only when `root` equals the server's own data folder does it answer `{"status": "ok"}` and exit 0.3 s later (deleting the PID file if it holds its own PID: read it and close it first, then delete it, because Windows refuses to delete a file that is still open); otherwise 403. Tests replace `_request_shutdown` and the HTTP senders so nothing reaches a real server.

#### When the port belongs to another folder's server

`ensure_server` returns False, keeps that server's `root` in `_FOREIGN_SERVER_ROOT` and emits `port_conflict` ("Another N3MC server (<root>) is using port <port>. Give N3MC Lite its own server_port in <config.json path>."). Then, so that no record goes into another product's memory:

- Hook saves go to Redis directly without a vector (§3.16 "Direct save"); the MCP `save_memory` does the same (§4.6).
- Searches are not made: the hook and `--search` print `# Recalled Memory Context\nSource: N3MC Lite (7-day working memory)\n\n_(memory search unavailable: another N3MC folder's server is using port <port>)_\n`.
- `--repair`, `--list`, `--recall-thread`, `--delete`, `--delete-session` and the other MCP tools print or return the backend error of §4.6 "When the server is not available" with that reason.

#### Notices (`_notify`)

Notices that need human action and come from outside the server — `stale_server`, `port_conflict`, `server_start_failed`, `redis_unreachable` (§12), `save_failed` (§12), `rules_md_edited:<hash>` (§12), `redis_url_mismatch` ("Server identity" above) — go through Free's `_notify(key, message)`:

1. Print to stderr.
2. Append `{"ts", "hook": "notice", "key", "message"}` to `audit.log` (only once for the same key and message).
3. Keep it in `notices.json` in the data folder — one JSON object `{<key>: {"message": <text>, "last_shown": <epoch seconds or null>}}`, written through a temporary file and `os.replace` — and show it **once a day** per key (when `last_shown` is null or at least 86,400 s old; showing sets `last_shown`).
4. `_clear_notice(key)` removes it when the cause is gone.

**Where notices are shown** (`_take_notices()` of the server): the first successful search of either kind — the hook's or CLI's `/search`, or the MCP `search_memory` — shows Lite's in-process notices of §6 (`config_unreadable`, `config_type`, `shared_redis`, `config_refused`, `index_dim_mismatch`; once per server process, as in §6) followed by the `notices.json` notices that are due (once a day), as `> ⚠️ N3MC notice: <message>` lines. The degraded-model notice of §3.9 is added on every degraded search as before. The degraded-state placeholders of the hook and the CLI (§12) also print the due `notices.json` notices, right after the heading, because the server may be the thing that is broken.

In tests, `notices.json`, `audit.log`, the PID file, `server.log` and the marker always live in a temporary data folder.

### 3.16 Hook records: role prefixes and direct save (v2.0.0)

Hook saves go through `POST /buffer` (§4.5), which calls Lite's `save_memory` of §3.8 / §3.11 / §3.13 with one extra argument, `role` (`"user"` or `"claude"`). **Every rule of `save_memory` applies unchanged** — lone-surrogate removal, the outer `strip()`, the empty and invisible-content checks, `skip_code_blocks`, `[code omitted]`, the chunk threshold, Lite's chunking (`chunk_threshold` 400 / `chunk_overlap` 100, §5 (7)), the parent document, exact-duplicate rejection with `mem:sha:` / `docsha:`, the guard taken with `SET NX`, one pipeline, and the TTL. `importance` is 1.0. The role prefix is added **after** `purify` and before the length decision, as follows (Free's `add_chunk_prefixes` format):

| Body after `strip()` and `purify` (`B`) | What is stored |
|---|---|
| `len(B) <= chunk_threshold` | One standalone record `mem:<id>` whose `content` is `[<role>] ` + `B`. |
| `len(B) > chunk_threshold` | `chunks = chunk_text(B, chunk_threshold, chunk_overlap)` (Lite's chunking on `B`, without the prefix). The parent document `doc:<id>` has `content` = `[<role>] ` + `B`. Chunk *i* of *N* has `content` = `[<role> i/N] ` + chunk *i*; when `chunk_text` returns one chunk (a body up to 19 characters longer than the threshold, §3.11), it is `[<role>] ` + that chunk. |

- The length decision uses `B`, not the prefixed text, so a text takes the same path whether it comes from a hook or from MCP.
- The guards hash what is stored, **within the exchange** (decided 2026-11-19, below): `mem:sha:<turn_id>:<sha1(NFKC([<role>] B))>` for a standalone record, `docsha:<turn_id>:<sha1(NFKC([<role>] B))>` for a parent. They are taken with `SET NX` and carry the record's TTL, like Lite's guards. A hook save without a `turn_id` does not happen (§12 always has one). **Deleting these guards (L6, 2026-11-20)**: the deletes of §3.5 / `delete_memories_by_session` recompute a record's guard key from the stored record. A record that has a `turn_id` **and** whose `content` starts with a role prefix (`[user` or `[claude`, i.e. a hook record) uses `mem:sha:<turn_id>:<sha1(NFKC(content))>` (a parent: `docsha:<turn_id>:<sha1(NFKC(content))>`) — the stored `content` is exactly `[<role>] B`. Every other record uses the v1.12.2 key. (An MCP save whose own text starts with `[user` and that passes a `turn_id` is read as a hook record here; a delete then misses its guard, which expires with the save's TTL, so the same text can be saved again only after 7 days.) The chunk embeddings are made from the prefixed chunk text (`"passage: " + content`), like any stored `content`.
- One space follows the closing bracket. The parent document is "verbatim" apart from this prefix.
- `agent_name` = `"claude-code"`, `session_id` = Claude Code's `session_id` from the hook input (empty → the server's default: `N3MC_SESSION_ID` of the server process, else a UUIDv4 the server makes when it starts — L7, 2026-11-20), `turn_id` = the exchange's `turn_id` (§12).
- **The MCP `save_memory` has no `role`** and stores exactly what it stored in v1.12.2 (no prefix).
- **Hook saves: exact duplicates only within the same exchange (decided 2026-11-19).** For a hook save, the exact-duplicate check (§3.8) compares only with records of the same `turn_id`, as Free does, so **every message and reply is stored**: `ok` sent in two exchanges is stored twice, once in each. Inside one exchange the check still works: when the Stop hook and the transcript recovery of §12 both save the same reply under the same `turn_id`, the second save is answered `duplicate`. **The MCP `save_memory` keeps Lite's rule** (§3.8: exact duplicates across all records), because it has no `role` and stores exactly what v1.12.2 stored.
- **`skip_code_blocks: true` also applies to hook saves (decided 2026-11-19: kept as it is)**: a message or reply containing a fence is answered `skipped_code` and not stored. The default is `false`, which stores it with `[code omitted]`.

**`/buffer` response**: the `save_memory` result dict of §4.3 as JSON (`saved`, `duplicate`, `skipped_code`, or `error` with its `reason`), HTTP 200; when Redis is unreachable, HTTP 503 with `{"status": "error", "saved": false, "reason": "<backend_hint>"}`.

**Direct save (without the server)**: when the server is not available (no answer, still starting, failed to start, another folder's server on the port, a timeout, or an HTTP error other than 503), the hook — and the MCP `save_memory` (§4.6) — saves directly to Redis from its own process: `Database(cfg).save_memory(..., role=…, embed=False)`. This is the same function with `embed=False`: it never imports `sentence_transformers` and never computes an embedding, so every record (standalone record or chunk) is written **without the `embedding` field**, exactly like a save while the model is unavailable (§3.9 step 4). Such records are found by BM25 at once and get their vector from Lite's repair (`repair_memory` / `/repair`, §3.10), which the hook calls on every message (§12). Before the direct save the process connects and runs `ensure_index()` as in §3.9 (it does not run `enforce_ephemeral()`; the server does that when it connects). When Redis is unreachable too, nothing is saved: `audit.log` already holds the text (§12), and the notice `redis_unreachable` is emitted. **Nothing is queued for resending.**

### 3.17 Context size limit (v2.0.0)

Lite's own rule (decision D4, 2026-11-20: kept in Lite; N3MC Free has no such limit). It has the same shape as the context size limit of N3MC Pro, fitted to Lite's result shape (Lite has no Q-A exchanges block, §3.14).

**Why**: Claude Code caps a hook's plain stdout at 10,000 characters; longer output is moved to a file and Claude sees only a 2,000-character preview (measured with N3MC Pro, 2026-11-18: with 20 results the block was over 10,000 characters for 45 % of 138 questions). Lite's hits can be whole parent documents, so a single hit can exceed the cap.

- **Where it applies**: the hook's `# Recalled Memory Context` block (`--hook-submit`) and the MCP `search_memory` result (it goes into the model's context too, and the limit of a tool result differs between clients). Both use `max_chars = context_max_chars` (§6, default 9,500, allowed 2,000–10,000). A manual `--search` is **not** limited. The degraded-state placeholders (§12) are not limited.
- **Rendering only**: the search itself and its `access_count` increments (§3.14 step 7) run on the full result before rendering; the limit removes text from the output only.
- **Size** (`context_size(text)`): the number of UTF-16 code units of the text (a character outside the BMP counts as 2), plus the number of line breaks (each line break counts as 2: a Windows pipe may deliver it as CR LF). The output fits when `context_size(output) + 2 <= max_chars`.
- **Parts** of the output, in order: the **head** (hook: `# Recalled Memory Context`, `Source: N3MC Lite (7-day working memory)`, `Search query: <query>` and an empty line; MCP: nothing), the **notices**, the **version sentence**, the **normal rows** (joined with `\n\n---\n\n`), the **weak part** (heading and weak rows), and the **trail** (MCP: `_NUDGE_AFTER_SEARCH`; hook: nothing). The parts other than the head and the trail are joined with `\n\n` exactly as in §3.14 / §5 (4).
- **Shortening**: render the full output first; when it fits, use it unchanged. Otherwise, in order, stopping as soon as it fits:
  1. Remove normal rows from the lowest-ranked one, one at a time, while more than 3 remain. After the last kept normal row add the part `_(N lower-ranked match(es) omitted to keep this block within the context limit.)_` (N = removed rows), joined with `\n\n` (not with `---`).
  2. Cut the text: take the output without the trail, and keep its longest prefix (trailing whitespace removed) for which prefix + `\n\n` + `_(cut here to keep this block within the context limit.)_` + trail fits. This is also how a long parent document among the kept rows is shortened.
- Notices and weak rows are never removed by step 1 (weak rows exist only when fewer than `min_results` rows passed, so the output is short then). The head and the trail are never removed.
- The rules file (§12) tells Claude that a manual `--search` with the same keywords shows what was left out; the MCP instructions are not changed for this.

---

## 4. MCP Protocol Surface

### 4.1 Transport

stdio. The server reads JSON-RPC lines from `stdin` and writes responses to `stdout`. Logs go to `stderr`. On Windows, `stdin`/`stdout`/`stderr` are reconfigured to UTF-8 at startup (see [§3.13](#313-encoding-safety) for the full encoding-safety contract, including lone-surrogate sanitization on every tool input).

**(v2.0.0)** "The server" in §4.1 and §4.2 is the MCP thin client `n3mc-workingmemory` (§4.6). It answers `initialize` and `tools/list` itself and forwards every `tools/call` to the resident server (§3.15, §4.5), which runs the tool handlers of §4.3. No new network port is opened: the MCP side is stdio only, and the resident server listens on `127.0.0.1` only.

### 4.2 `initialize` response

The server advertises:
- `protocolVersion` — **negotiated by the MCP SDK (v1.7.3)**: the version the client sent in `initialize` when the SDK supports it (`mcp` 1.27: `2024-11-05`, `2025-03-26`, `2025-06-18`, `2025-11-25`), otherwise the SDK's latest. Do not pin it in code. (Up to v1.7.2 this line read `"2024-11-05"`; a client that asks for `2025-06-18` gets `2025-06-18`.)
- `serverInfo: { name: "n3mc-workingmemory", version: <package __version__> }`
- `capabilities.tools` with `listChanged: false`
- `instructions:` — a multi-line string delivering behavioral guidance (see [§5](#5-behavioral-instructions-auto-save-strategy)). **The Lite instruction text explicitly tells the LLM that memory expires after 7 days.** (v2.0.0: rules 1 and 2 also say what a client that has the hooks' `# Recalled Memory Context` block does, §5 (1).)
- (v2.0.0) The thin client answers `initialize` within 2 seconds: it loads no model and does not wait for the resident server (§4.6).

> **⚠️ Implementation note (don't forget to set version / instructions)**: `serverInfo.version` and `instructions` can be **passed as `Server()` constructor arguments** (`Server(name, version=__version__, instructions=INSTRUCTIONS)`). Forget this and the version is easily empty and the instructions never advertised. Passing them to the constructor lets `create_initialization_options()` pick them up automatically. Depending on the MCP SDK version, also assigning `init_options.instructions` afterward makes it reliable.

### 4.3 Tools

Seven tools are exposed via `tools/list` (6 base tools + `recall_thread` from §4.3.1). N3MC MCP Pro 1.0.0 has five tools with partly the same names (`search_memory`, `save_memory`, `list_memories`, `delete_memory`, and `show_turn` instead of `recall_thread`); `delete_memories_by_session` and `repair_memory` are Lite-only (Pro keeps only the per-record `delete_memory` to minimize accidental-deletion risk on a persistent store):

| Name            | Inputs                                    | Behavior                                                              |
| --------------- | ----------------------------------------- | --------------------------------------------------------------------- |
| `search_memory` | `query: string, limit?: int, session_id?: string, since?: string, until?: string` (§4.3.1) | Hybrid (vector + BM25) search, time-decayed ranking with frequency boost and `b_session` match boost, lexical rerank. Chunk hits collapse to their parent document and render verbatim (see [§3.11](#311-verbatim-recall-parent-document--chunks-pattern)). Results are shaped and rendered as in [§3.14](#314-search-result-shaping-v170-weak-match-fill-since-v172) (vector floor, newest version first, relative cut, `saved=` times). **When fewer than `min_results` (default 3) rows pass the floors, the closest dropped rows are added under `#### Possibly related (weak matches — may be unrelated)` (v1.7.2); the tool description tells the LLM: weak matches did not pass the relevance floors and are often unrelated — use one only when it is actually relevant to the question; if it is not, ignore it and do not mention it.** The `session_id` argument feeds the **same `b_session` ranking as Pro** (match=1.0 / mismatch=0.6 by default), surfacing memories saved with the same `session_id` above unrelated rows. When omitted, the server default (`N3MC_SESSION_ID` env var → per-process UUIDv4) is used as the effective session. Returns markdown. |
| `save_memory`   | `content: string, agent_name?: string, owner_id?: string, importance?: number, session_id?: string, turn_id?: string` (§4.3.1) | Body ≤ `chunk_threshold`: exact dedup only (§3.8), then HSET + EXPIRE. Returns `{"status": "saved", "saved": true, "id": "...", "ttl_seconds": ...}`. Body > `chunk_threshold`: persists a **parent document** (`doc:<id>`) verbatim and writes sliding-window chunks to `mem:<id>`; returns `{"status": "saved", "saved": true, "parent_id": "...", "chunks": N, "saved_count": N, "ids": [...], "ttl_seconds": ...}`. If `owner_id` is provided and does not match the server config, returns `{"status":"error","saved":false,"reason":"owner_id mismatch"}`. `importance` is clamped to 0.5–2.0 and feeds `stored_importance` in ranking; a value that is not a number — including `NaN` (e.g. the string `"nan"`) — returns `{"saved": false, "status": "error", "reason": "importance must be numeric (got <value>)"}` and nothing is saved (a `NaN` used to pass the clamp and was stored as `2.0`, the highest importance). When `session_id` is omitted, the server default (`N3MC_SESSION_ID` env var, or a per-process UUIDv4) is stored as a write-time tag — used as the filter key for `delete_memories_by_session` AND as the match key for subsequent `search_memory` `b_session` boosting. |
| `list_memories` | `limit?: int (default 20)`                | Markdown listing that interleaves parent documents and standalone memories, newest first. A `limit` below 1 is treated as 1 (a negative value used to cut rows from the end of the list instead). Parents are tagged `[doc×N]`; chunks are hidden (FT.SEARCH `*` fetch then Python filter on empty `parent_id` — see §3.12). **Pages through the index (v1.7.1)**: fetch newest first in pages of 200 (`LIMIT <offset> 200`, sorted by `timestamp_epoch` desc) and keep paging until `limit` standalone memories of this owner are collected or the index is exhausted. (v1.7.0 read one block of `max(limit × 5, 200)` rows; after a 260-chunk document the block held only chunks, and a standalone memory saved just before it was not listed — Lite review LM6.) |
| `delete_memory` | `id: string`                              | Only records of the configured `owner_id` (another owner's id → `{"status":"not_found","id":...}`, v1.7.1). **The id may be given bare (`<uuid>`, as `search_memory` / `list_memories` print it) or with one leading `doc:` or `mem:` prefix; one such prefix is removed first (v1.7.3, §3.12 bare-UUID rule), and the response's `id` is the bare UUID.** If the id is a parent (`doc:<uuid>` exists), cascade-deletes the parent, `docsha:`, and every chunk listed in its `chunk_ids` (§3.11). Otherwise `DEL mem:<uuid>`, and for a standalone record (no `parent_id`) also `DEL mem:sha:<sha1>` when the guard's value is this id, atomically (§3.5 "Delete semantics"). |
| `delete_memories_by_session` | `session_id: string`         | Bulk-delete every standalone memory, parent document, child chunk, and sha guard tied to the given `session_id`, scoped to the configured `owner_id`. Response: `{"status":"deleted", "session_id": ..., "documents_deleted": D, "chunks_deleted": C, "singles_deleted": S, "deleted": D+C+S}`. When nothing matches: `{"status":"not_found", "session_id": ..., "deleted": 0}` (a re-call is a safe no-op). **Irreversible — confirm `session_id` with the user before calling.** Lite-only (see [§10 Test 6](#10-self-evaluation-evidence-report)). |
| `repair_memory` | —                                         | `ensure_index()` and adding missing vectors (up to 200 per call); see [§3.10](#310-repair). |

**(v2.0.0) Where the tools run**: the handlers below run inside the resident server (`tools.py`, called by `POST /tools/<name>`, §4.5); the thin client returns their text unchanged (§4.6). Their arguments, behaviour and output texts are those of v1.12.2, with one addition: the `search_memory` text is kept within `context_max_chars` (§3.17). The CLI of §13 calls the same handlers and prints the same text without the reminder.

All tool responses are a single `TextContent` element. `save_memory` / `delete_memory` / `delete_memories_by_session` / `repair_memory` return JSON strings; `search_memory` / `list_memories` return human-readable markdown. **Every response also ends with a short auto-save reminder** (separated by `\n\n---\n`) — the [§11](#11-save-guarantees-and-the-limit-of-the-mcp-protocol) nudge channel. **This includes every error response (v1.7.3)**: the `Error: …` text of §4.4 (an unreadable `since` / `until`, an unexpected exception, the backend error of `search_memory` / `list_memories` / `recall_thread`) and the JSON with `"status": "error"` (e.g. a non-numeric `importance`). The error text stays first, so the `Error: ` prefix of §4.4 is unchanged. (v1.7.2 sent these without the reminder.) Callers that machine-parse the JSON should use a streaming decoder (e.g. `json.JSONDecoder().raw_decode()`) that returns the first JSON document and ignores the trailing markdown nudge.

### 4.3.1 Retrieval Extension Tools

> Behavior of existing tools is unchanged. The following are **additive tools and additive arguments**. The Pro build adds further tools for entity indexing and lazy summarization (NOT shipped in this Lite build).

#### Additional arguments on `search_memory`

| Argument | Type | Behavior |
| --- | --- | --- |
| `since` | `string` (ISO 8601 date or datetime) | Restrict to entries saved at or after this timestamp. Date-only inputs are interpreted as `00:00:00`. |
| `until` | `string` (ISO 8601 date or datetime) | Restrict to entries saved at or before this timestamp. Date-only inputs are interpreted as `23:59:59.999999`. |

**Time zone (v1.7.1)**: a date-only value or a datetime without an offset is read in the **server's local time zone** — the same clock as `saved=` in the output ([§3.14](#314-search-result-shaping-v170-weak-match-fill-since-v172)) — and converted to epoch seconds. A value with an offset (`2026-11-13T09:00:00+09:00`, `…Z`) is used as given. `T` or a space may separate date and time.

**Unreadable values (v1.7.1)**: if `since` or `until` is given but cannot be parsed (`yesterday`, `2026/11/13`, `2026-13-01`) or cannot be converted to epoch seconds on this platform (`9999-12-31`, `1970-01-01`, `1969-12-31` in the local time zone on Windows: the conversion raises `OSError` or `OverflowError`, which count as unreadable — 2026-11-22, L3; before, the CLI ended with a traceback, the tool returned `Error: [Errno 22] Invalid argument` and `/search` answered 500), the search is not run and the response is a `TextContent` `Error: since must be an ISO 8601 date or datetime, e.g. 2026-11-13 or 2026-11-13T09:00:00+09:00 (got '<value>')` (`until` likewise). If `since` is later than `until`: `Error: since is later than until`. Since v1.7.3 the §4.3 reminder follows either text. An empty string means "no bound".

> **⚠️ Why (v1.7.1, Lite review LM9)**: v1.7.0 treated an unparsable value as "no filter", so `since="yesterday"` returned results from the whole store and the LLM answered as if they were filtered. It also read offset-less values as UTC while `saved=` shows local time, so on a UTC+9 host `since="2026-11-13"` dropped what `saved=` showed as saved between 00:00 and 08:59 that day. Free made the same change (v1.5.0).

Either bound may be specified alone. These do not alter the ranking formula. They are applied as **pre-filters inside both queries** — KNN: `@timestamp_epoch:[<since> <until>]=>[KNN N @embedding $vec AS __dist]`; BM25: `(<text query>) @timestamp_epoch:[<since> <until>]` (use `-inf` / `+inf` for an open bound). NUMERIC filters do not hit the UUID TAG problem of §3.12.

> **⚠️ Why a pre-filter (v1.7.0)**: KNN returns the globally nearest N rows first. v1.6.x filtered by time *after* ranking, so when many rows outside the window were closer to the query, the in-range rows never made it into the candidate set and `search_memory(since=...)` returned nothing.

#### New tool `recall_thread`

| Name | Inputs | Behavior |
| --- | --- | --- |
| `recall_thread` | `turn_id: string, before?: int (default 2), after?: int (default 2)` | Returns markdown that interleaves the entry with the given `turn_id` plus the `before` preceding and `after` following entries, sorted ascending by save timestamp. The Lite implementation scans all stored entries (parent documents, standalone memories, and chunks) sharing the same `turn_id` and returns them in chronological order. **A chunk whose parent document still exists is skipped** (the parent carries the full verbatim body, tagged `[doc×N]`); **a chunk whose parent is gone (an orphan, §3.11) is listed as its own entry with its chunk text** — the same graceful degrade as `search_memory` (v1.7.3; v1.7.2 skipped every chunk, so a turn whose parent had expired came back `not_found` while `search_memory` still showed its chunks). When nothing matches, returns the JSON string `{"status":"not_found","turn_id":...}`. |

**Operational rule**: The connected LLM SHOULD pass a hit's `turn_id` to `recall_thread` when the snippet returned by `search_memory` is insufficient to answer. End users do not need to invoke this tool directly.

> **⚠️ Implementation note (emit `turn_id` in the `search_memory` response)**: For the operational rule above to work, the LLM must be able to read the `turn_id` to pass to `recall_thread` from the `search_memory` results. Therefore **include each hit's `turn_id` in the `search_memory` markdown output** (omit when empty). In practice, add `turn_id` to the `RETURN_FIELDS` of `_vector_search` / `_bm25_search`; on parent resolution, fetch `turn_id` from `doc:<pid>` and put it on the row dict; and emit it in the final markdown as `turn=<turn_id>` in each hit's heading line ([§3.14](#314-search-result-shaping-v170-weak-match-fill-since-v172)). Without this, `recall_thread` is effectively uncallable (the spec's operational workflow does not close).

### 4.4 Error Handling

Tool exceptions are caught in the dispatch layer and returned as `TextContent` with a leading `"Error: "` prefix. The server never crashes the stdio loop due to a tool-level exception. Redis availability is checked on every call and lost connections are re-established automatically; while Redis is unreachable the dispatcher returns the backend error with the recovery hint instead of invoking the tool (see [§3.9](#39-startup-sequence--self-recovery) "Redis availability on every tool call"). An MCP client therefore shows the server as **connected** even while Redis is down — by design: the instructions and tools stay available, so the LLM can tell the user what to do (§5 rule 4).

**(v2.0.0)** The same holds while the resident server is not available: the thin client stays connected and answers each tool with the texts of §4.6 "When the server is not available". `isError` is never set; every error is the text of a normal result, as in v1.12.2.

### 4.5 Resident Server Endpoints (v2.0.0)

All on `http://127.0.0.1:<server_port>`. Every endpoint returns 403 when the `Host` header is not local or the request carries an `Origin` header (§3.15). The `Host` check uses the `server_port` the server was started with, not the value `config.json` holds later (§3.15 "PID file handling"). Every endpoint except `/health` and `/shutdown` is a plain `def`.

| Method | Path | Used by | Body | Answer |
|---|---|---|---|---|
| `GET` | `/health` | `ensure_server` | — | `{"status": "ok", "root", "code_version", "pid", "redis"}` (§3.15) |
| `POST` | `/shutdown` | `_stop_stale_server` | `{"root": str}` | `{"status": "ok"}`, or 403 when `root` is not the server's (§3.15) |
| `POST` | `/buffer` | the hooks (`--hook-submit`, `--save-claude-turn`) | `{"content": str, "role": "user" \| "claude", "agent_name": str?, "session_id": str?, "turn_id": str?, "no_model_wait": bool?}` | The `save_memory` result dict (§3.16), HTTP 200. `no_model_wait: true` (the hooks always send it): do not wait for the model (§3.9 item 26, M3); Redis unreachable → 503 with `{"status": "error", "saved": false, "reason": "<hint>"}` |
| `POST` | `/search` | `--hook-submit`, `--search` | `{"query": str, "session_id": str?, "since": str?, "until": str?, "limit": int?, "max_chars": int?, "no_model_wait": bool?}` | `{"text": "<# Recalled Memory Context block>"}` (§12 "Search output"). `no_model_wait: true` (sent by `--hook-submit` only, never by `--search`): do not wait for the model (§3.9, M3). The server tells the hook from `--search` by this field only, not by `max_chars`. `max_chars` given → the limit of §3.17; absent → no limit. Unreadable `since` / `until`, or `since` later than `until` → 400 with `{"detail": "<the Error: text of §4.3.1>"}`. Redis unreachable → 503 with `{"detail": "<hint>"}` |
| `POST` | `/repair` | `--hook-submit` | `{}` | The `repair_memory` result dict of §3.10 (one call: `ensure_index()` and up to 200 vectors). One repair at a time per server process: while one runs, another call answers at once with `{"status": "ok", "message": "repair already running", "reembedded": 0, "remaining": 0}` (L5). Redis unreachable → 503 with `{"status": "error", "message": "<hint>"}` |
| `POST` | `/tools/{name}` | the MCP thin client (`nudge=1`), the CLI (`nudge=0`) | the tool's arguments as one JSON object (anything else is read as `{}`) | `{"text": "<the tool's result text>"}`, always HTTP 200 (errors are in the text, as in §4.3 / §4.4). The handler is the v1.12.2 handler of §4.3, including the per-call `ensure_connected()` and the backend errors of §3.9, plus the size limit of §3.17 for `search_memory`. Query parameter `nudge` (default `1`): `0` leaves out the reminder of §5 (2) |

- **No other endpoints.** Free's `/list` is not needed (listing is the `list_memories` tool), and Free's Pro-only `/delete`, `/gc` and `/import` do not exist (deleting is the `delete_memory` / `delete_memories_by_session` tools; Redis expires records itself).
- An unknown `{name}` returns `{"text": "Error: unknown tool '<name>'" + _NUDGE_GENERIC}` (without the reminder when `nudge=0`).
- `nudge=0` exists because the reminders tell the model to call `save_memory`; in Claude Code the hooks save, so the CLI prints the tool's text without them.
- `/search` and `/tools/search_memory` both run Lite's `search_memory` (§3.14). `/search` wraps the rendered text in the hook's heading (§12); `/tools/search_memory` returns the v1.12.2 text.

### 4.6 MCP Thin Client (v2.0.0)

Everything in this section is new in v2.0.0. Its mechanics are those of the MCP entry of N3MC MCP Pro v1.0.0 §4.6; its tools, outputs and `isError` behaviour are Lite's own (§4.3, §4.4).

| Item | Value |
|---|---|
| Module | `n3mc_mcp/server.py` (`run()`; `__main__.py` calls it) |
| Commands | `n3mc-workingmemory`, alias `n3mc-mcp-lite` |
| Transport | stdio only |
| Library | `mcp` 1.x (`>=1.0.0,<2.0.0`), the low-level `mcp.server.lowlevel.Server` and `mcp.server.stdio.stdio_server` |
| `serverInfo` | `name` = `n3mc-workingmemory`, `version` = `__version__` |
| Configuration | the same `config.json` and data folder as the server (`load_config()`, `N3MC_DATA_DIR`; §6, §7) — it needs `server_port`. **Read again at the start of every tool call** (second review of 2026-11-20), and that reading is used for both `ensure_server` and the HTTP request: the entry runs for a whole client session, and with the settings read once at start a changed `server_port` sent every call to the old port. When the file cannot be read at that moment, the last readable settings stay in use (the settings the entry started with, if none was readable since): an unreadable read would bring a new temporary `owner_id` on every call |

1. **stdout belongs to MCP.** `run()` first reconfigures stdin / stdout / stderr to UTF-8 (§3.13 (1)). Nothing but MCP messages is written to stdout; warnings, `_notify` output and library messages go to stderr.
2. **No model, no Redis.** The module imports neither `sentence_transformers` nor `torch` (directly or through `processor.py`'s model code), and opens no Redis connection, except for the direct save below. `import_backend()` and the background model load of §3.9 are not run here.
3. **Starting the server.** Right after start, `run()` calls `ensure_server(cfg)` once **in a background thread**, so `initialize` is answered at once (a server start can take up to 60 s, §3.15). `_start_server` sends the child's stdout to `DEVNULL` and stderr to `server.log`, so the child never writes into the MCP stream.
4. **`initialize` / `tools/list`** are answered locally: the instructions of §5 (1) and the seven Tool definitions of §5 (3) (`tooldefs.py`, shared with the server). They work without the server.
5. **Input check off**: register the handler with `server.call_tool(validate_input=False)`; when the installed `mcp` refuses that argument (`TypeError`), register it with `server.call_tool()`. Newer `mcp` 1.x versions check the arguments against the schema before the handler runs, which would replace Lite's own texts (for example the `importance must be numeric` JSON of §4.3) with the SDK's.
6. **Each `tools/call`**:
   1. Arguments that are not a JSON object are read as `{}`. An unknown tool name is answered locally with `Error: unknown tool '<name>'` + `_NUDGE_GENERIC` (no request).
   2. `search_memory` and `save_memory`: when `session_id` is missing or blank, set it to the entry's session — `N3MC_SESSION_ID` of this process, else one UUIDv4 made when this process started. **Why**: in v1.12.2 the default session was per MCP server process; the resident server is shared by every client and outlives them, so the thin client sends its own default to keep the v1.12.2 meaning (`b_session`, `delete_memories_by_session`).
   3. `save_memory`: when `agent_name` is missing or blank, set it to (1) the environment variable `N3MC_AGENT_NAME`, (2) otherwise the `clientInfo.name` the client sent in `initialize`, (3) otherwise leave it empty (v1.12.2 stored an empty string). Each value after `strip()`, used only when not empty.
   4. Run `ensure_server(cfg)` in a worker thread (`asyncio.to_thread`), so the stdio loop keeps reading. A server that stopped while the entry was running is started again here.
   5. `POST /tools/<name>?nudge=1` with the arguments; return `TextContent(type="text", text=<the answer's "text">)` **unchanged**.
   6. Any exception inside the handler gives `Error: <message>` + `_NUDGE_GENERIC` (the v1.12.2 rule of §4.4) and one stderr line; the entry never exits because of a tool call.
7. **Ending.** When the application closes stdin, the entry exits. It never sends `/shutdown`: the server was started detached and the hooks use it too.

#### When the server is not available

"Not available" = `ensure_server` returned without a usable server (still starting after 60 s, the start failed or was skipped within 5 minutes of a failure, another folder's server holds the port), or the `/tools` request failed (connection error, timeout, non-2xx answer). `<reason>` is one of `the memory server is still starting (the first start downloads the embedding model, ~1.1 GB)`, `the memory server could not start; see <server.log path>`, `another N3MC folder's server is using port <port>`, or the request error.

| Tool | Answer |
|---|---|
| `save_memory` | **Direct save** of §3.16 (`role` none, `embed=False`), with the v1.12.2 argument checks of §4.3 (`importance`, `owner_id`) done locally first. The result JSON is the `save_memory` result with `"warning": "embedding_unavailable"` and `"detail": "The N3MC Lite memory server was not available (<reason>), so this entry was saved without a vector. It is found by keywords now and gets its vector from repair_memory once the server runs."`, then `_NUDGE_AFTER_SAVE`. When Redis is unreachable too: the backend error JSON of §3.9 item 2 (`{"status": "error", "saved": false, "reason": "<backend_hint>"}`) + `_NUDGE_GENERIC` |
| `search_memory`, `list_memories`, `recall_thread` | `Error: memory backend unreachable. The N3MC Lite memory server is not available (<reason>). It is started automatically; retry in a minute.` + `_NUDGE_GENERIC` (the prefix of §3.9 item 2, so rule 4 of the instructions applies) |
| `delete_memory`, `delete_memories_by_session` | `{"status": "error", "reason": "The N3MC Lite memory server is not available (<reason>). Nothing was deleted; retry in a minute."}` + `_NUDGE_GENERIC` — **only when no request was sent** (no usable server after `ensure_server`, or the connection was refused). When the request was sent and no answer came back (timeout, connection reset after sending, non-2xx): `{"status": "unknown", "reason": "The request reached the N3MC Lite memory server but no answer came back (<reason>). The delete may have completed; check with list_memories or search_memory before deleting again."}` + `_NUDGE_GENERIC` (2026-11-22, review #7: "Nothing was deleted" was returned although the delete had completed, and retrying `delete_memories_by_session` would also delete records added to that session meanwhile) |
| `repair_memory` | `{"status": "error", "message": "The N3MC Lite memory server is not available (<reason>). Retry in a minute."}` + `_NUDGE_GENERIC` |

**Why save directly**: a fact the model decided to save would otherwise be lost; the hooks do the same (§3.16), and Lite has saved without vectors since v1.7.0 whenever the model was unavailable. A direct save that races with a server save of the same text after a timeout is resolved by the `SET NX` guard (§3.8): one record.

#### Registering in other applications

See §8 "Claude Desktop". Never register the thin client in Claude Code (§8 "Claude Code").

#### Limits of this design

- **In applications without hooks, saving is up to the model** (§11). The instructions and reminders ask it to save; full conversations are kept only in Claude Code.
- **The resident server must be able to run** (Python with the dependencies of §2). When it cannot start, searches return the error above and saves go in without vectors.
- **One installation for all clients.** The hooks, the CLI and the thin client must run the same code: a copy of another version has another `code_version`, and each would replace the other's server on every call (§3.15). Register the desktop app with the absolute path of the same scripts folder the hooks use, or — for plugin users — with the same `uvx --from n3memorycore-mcp-lite==2.0.1 …` form (§8). The plugin's `uvx` commands are pinned to the package version (§2), so the plugin and the desktop app run the same version as long as both use this release.

---

## 5. Behavioral Instructions (Auto-Save Strategy)

Because MCP has no equivalent of Claude Code's `UserPromptSubmit` / `Stop` hooks, the auto-save behavior is expressed as **natural-language instructions** returned in the `initialize` response. The connected LLM reads them as system guidance.

**(v2.0.0)** These instructions are for applications that use MCP (the Claude desktop app and others). Claude Code uses the hooks and gets its guidance from the rules file `.claude/rules/n3mc-lite-behavior.md` (§12). One text is sent to every client; it does not know whether the client has hooks, so rules 1 and 2 say what to do when the context already holds the block with the line `Source: N3MC Lite (7-day working memory)` (the Lite hooks put it there; the blocks of the N3MC Free and Pro hooks have no such line, L9): search only when the block is not enough, and save only when the user asks. A client without the block keeps the v1.12.2 behaviour (search every turn, save automatically). This matters only when an old MCP registration is still present in Claude Code next to the hooks (§8).

The instructions require the LLM to:

0. **Pro coexistence policy** — When `n3mc-longtermmemory` (N3MemoryCore Pro, persistent SQLite+sqlite-vec) is also registered in the same session, treat Lite as working memory (in-session progress notes, temporary debug state, disposable intermediate results) and Pro as the canonical long-term store (user profile / preferences, project specs, architecture decisions, world-building, API contracts, research summaries — anything worth keeping across sessions). When in doubt, prefer Pro. With Pro connected, the TTL warning (rule 7) is generally unnecessary — simply route long-lived content to Pro instead. Without Pro, this server is the sole store: accept all content and activate rule 7 when the user signals permanence. Do NOT double-save the same fact to both servers.

1. **Search first, then acknowledge when you recall** — (v2.0.0: when the context already holds the block with the line `Source: N3MC Lite (7-day working memory)`, call `search_memory` only when that block is not enough; otherwise as follows) call `search_memory` at the start of every user turn with a concise query reflecting the user's intent. When the retrieved snippets actually shape the reply (i.e. you are recalling information saved in an earlier turn), open the reply with a short acknowledgment **in the user's language**, e.g. Japanese 「前回の回答がメモリに保存されています。」 or English "Pulling this from earlier memory in this session." **If no relevant memory was found, or if retrieval did not influence the answer, do not announce anything.** Never announce the mere act of searching — only the act of recalling.
2. **Save every substantive exchange — automatic, no permission asked** — (v2.0.0: when the context holds the block with the line `Source: N3MC Lite (7-day working memory)`, every message and reply is already saved by the hooks: do not call `save_memory` on every turn, only when the user asks to remember something; otherwise as follows) saving is silent and automatic. The user should NEVER have to say "save this" or "remember that"; the LLM saves by default and does NOT ask for confirmation. After each meaningful turn, call `save_memory` to persist (a) the user's paraphrased intent, (b) **the LLM's own substantive output** — decisions, plans, and especially any creative or generated content the user may refer back to later: world-building, character settings, design sketches, code architecture, research summaries, outlines (if more than a sentence or two of work went into producing it, save it), and (c) key facts, preferences, and unresolved questions. One `save_memory` call per distinct fact, 50–200 chars each (long content → rule 3). Exact duplicates are auto-rejected server-side, so err on the side of saving more. Near-duplicates are **not** rejected: an updated fact or a re-made document is saved as a new entry, and `search_memory` shows the newest version first. **Note**: the Lite text explicitly reminds the LLM that entries vanish after 7 d — it is a rolling scratchpad, not a permanent archive.
3. **Long content — save the full body in one call (verbatim recall)** — when the turn produces OR receives a long body the user may want back verbatim — a user-pasted spec / article / log (code blocks are stored as `[code omitted]`, §3.13), **OR a long creative setting / world / character sheet / design doc the LLM just generated** — pass the FULL text to a single `save_memory` call. The server automatically creates a parent document + chunks, indexes chunks for search, and returns the full parent body on recall (see [§3.11](#311-verbatim-recall-parent-document--chunks-pattern)). Rough threshold: **> ~400 chars → save as one full-body call**. Shorter summarizable content → rule 2 (multiple short facts). Do NOT split a long verbatim-worthy body into many short summaries — that destroys recall fidelity.
4. **Handle tool errors visibly — never generate long content blind** — if `search_memory` / `save_memory` returns a server error (Redis unreachable, connection refused, timeout, "start Redis Stack" hint, etc.), **STOP and surface the failure to the user in their language BEFORE generating any long creative or spec content**. Rationale: with the backend down, every subsequent `save_memory` also fails silently, so a long setting / design doc / code architecture produced during the outage is lost the moment the session closes — and the user will not realize the memory layer was broken until recall fails. Required behavior: (a) announce the failure succinctly (e.g. "Memory backend is unreachable — save/search are offline right now"), (b) relay the recovery hint the tool returned (e.g. `docker run -p 127.0.0.1:6379:6379 redis/redis-stack-server:latest`), (c) ask whether to proceed without memory (so the user knows this session will not persist) or to pause until the backend is restored. Short factual replies may proceed, but must still mention the error once so the user knows saves are failing.
5. **Skip noise** — do not save greetings, clarifying questions, or mechanical acknowledgements.
6. **Respect explicit requests** — honor "don't save this" and "forget that" (use `delete_memory`).
7. **Inform the user about the 7-day TTL when they expect permanence** — the TTL is visible to the LLM (this INSTRUCTIONS block, the `search_memory` tool description, and the `ttl_seconds` field on every `save_memory` response) but is **invisible to the human user**. Default behavior remains silent auto-save (rule 2) — do NOT mention TTL on every save. However, when the user's message signals an expectation of long-term retention — explicit permanence phrases ("remember this forever", "don't forget", "save permanently", 「ずっと覚えておいて」「永続的に保存して」), a long setting / spec / code dump / world-building / character sheet the user obviously invested in, or a reference back to content saved more than ~5 days ago — add ONE short sentence in the user's language reminding them that Lite memories auto-expire 7 days after save, noting that the **Pro build (sqlite-vec backed)** offers persistence, and suggesting external backup otherwise. Save anyway; do not ask permission. Emit the reminder ONCE per distinct long-term signal, not once per turn and not once per save. Rationale: without this rule, the LLM silently persists content that the user believes is permanent, and the user only discovers the loss when recall fails mid-project.
8. **Respect the `skip_code_blocks` server policy** — when `save_memory` returns `{"status": "skipped_code", "saved": false}`, the server is configured to reject any payload containing a triple-backtick fence (see [§6](#6-configuration)). This mirrors the FastAPI-era N3MemoryCore's code-exclusion behavior for users who intentionally keep code out of memory. With the default `false` the body is saved with its code blocks as `[code omitted]` (§3.13), and the INSTRUCTIONS text says so (v1.9.1). Do NOT retry the same payload; either save a prose description of what the code does, or skip saving for that turn. Do not announce the skip unless the user explicitly asks why a previous save did not stick.

9. **Bulk cleanup via `delete_memories_by_session`** — Use this to wind down a finished project or remove test-run noise before TTL expiry, keeping search results clean. The operation is **irreversible** — confirm the `session_id` with the user before calling.

10. **Thread context retrieval — `recall_thread` (§4.3.1)** — When a `search_memory` hit snippet lacks enough context to be actionable, pass the hit's `turn_id` to `recall_thread` to retrieve the surrounding conversation thread in chronological order. When saving memories, tag all `save_memory` calls within a turn with the same `turn_id` argument to enable future thread recall (any stable string works: short UUID, timestamp, `"turn-N"`, etc.). End users do not need to invoke this tool directly — it is an internal enrichment tool the LLM uses proactively.

11. **Versions and notices** — when several versions of the same content appear in `search_memory` results, the one with the newest `saved=` time is the current one; answer from it (mention the change if it matters). If a result starts with `> ⚠️ N3MC notice:`, tell the user what it says in their language (e.g. an unreadable `config.json`).

12. **Weak matches (v1.7.2)** — records under `#### Possibly related (weak matches — may be unrelated)` did not pass the relevance floors and are added only so that a search is never empty. Use one only when it is actually relevant to the question; if it is not, ignore it and do not mention it (do not say that unrelated memories were found).

The full text is in [`n3mc_mcp/instructions.py`](./n3mc_mcp/instructions.py).

### Texts and rules written down from the code (2026-11-19)

Up to here this document pointed at the code for some texts and left some rules out. The code is regenerated from this document, so everything below is part of the specification. **Where a summary above differs from a text below, the text below is the one to implement** (for example, rule 8 of the delivered instructions is the `session_id` rule; `skip_code_blocks` is a note inside rule 2).

**(1) The instructions returned at `initialize` — `INSTRUCTIONS` in `n3mc_mcp/instructions.py`.** Exactly this text, followed by one line feed. Do not rephrase, reorder or translate it. (v2.0.0: the two paragraphs that start with `HOOK CLIENTS:` in rules 1 and 2 are new, and the next paragraph of each now starts with `Otherwise,` / `Without that block,` and is wrapped again; every other paragraph is the v1.12.2 text.)

`````text
N3MemoryCore — Lite (Ephemeral Memory)

This MCP server gives you hybrid-search memory (vector + BM25) backed by
Redis Stack. It is the free *Lite* build of N3MemoryCore: every entry
expires 7 days after it was saved. Treat it as a short-lived scratchpad,
not a long-term store.

BEHAVIORAL RULES
================

0. ROLE WHEN PRO IS ALSO CONNECTED (coexistence policy)
   If a persistent N3MemoryCore memory is also registered as an MCP server
   in the same session — `n3mc-pro` (N3MC MCP Pro), `n3mc-pg` (its
   PostgreSQL edition) or `n3mc-longtermmemory` (the earlier MCP Pro) —
   treat the two servers as complementary, not redundant:

   - THIS SERVER (Lite, ephemeral) is the working-memory / scratchpad.
     Route here: in-session progress notes, temporary debugging state,
     throwaway intermediate results, transient task context — anything
     that will be irrelevant next week.

   - PRO is the canonical long-term store. Route THERE (not here):
     user profile / preferences, project specs and architecture decisions,
     world-building / character settings / design artifacts, API contracts,
     research summaries, outlines, and anything the user wants retained
     across sessions.

   - When in doubt, prefer Pro. "Painful if lost" > "fine if it vanishes".
   - Search order at the start of a turn: Pro first, then Lite.
   - Do NOT double-save the same fact to both servers.
   - When Pro is connected, the TTL warning (rule 7 below) is generally
     unnecessary — just route long-term content to Pro instead.

   If Pro is NOT connected, this server is the sole memory store. Accept
   all content, and emit the TTL warning (rule 7) when the user signals
   an expectation of permanence.

1. SEARCH FIRST, THEN ACKNOWLEDGE WHEN YOU USE MEMORY
   HOOK CLIENTS: if your context already contains a block with the line
   "Source: N3MC Lite (7-day working memory)" (the N3MC Lite hooks of
   Claude Code put it there), the memory was already searched for this
   message. Call `search_memory` only when that block is not enough. A
   "# Recalled Memory Context" block without that line comes from
   another product and does not count.

   Otherwise, at the start of every user turn, call `search_memory` with a
   concise representation of the user's intent (key nouns + verbs). Use the
   returned snippets as context when formulating your answer.

   When your answer actually draws on retrieved memories (i.e. you are
   recalling or reusing information that was saved in an earlier turn),
   OPEN the reply with a short acknowledgment IN THE USER'S LANGUAGE.
   Examples:
   - Japanese: 「前回の回答がメモリに保存されています。」
                「以前の会話から該当情報を取り出しました。」
   - English:  "Pulling this from earlier memory in this session."
                "Recalling the setting you saved earlier."

   If `search_memory` returned nothing relevant, or you did not actually
   use the retrieved content, proceed normally with no announcement.
   Do not announce the mere act of searching — only the act of recalling.

2. SAVE AFTER EVERY SUBSTANTIVE EXCHANGE — AUTOMATIC, NO PERMISSION ASKED
   HOOK CLIENTS: if your context contains the block with the line
   "Source: N3MC Lite (7-day working memory)", every message and reply
   is already saved by the hooks. Do NOT call `save_memory` on every
   turn; call it only when the user asks you to remember something.

   Without that block, saving is silent and automatic. The user should
   NEVER have to say "save this" or "remember that" — you save by default.
   Do NOT ask the user whether to save. Do NOT wait for the user to
   confirm. Just save.

   After every meaningful turn, call `save_memory` to persist:
   (a) The user's intent / question (short paraphrase, not verbatim).
   (b) YOUR OWN substantive output — decisions, plans, answers, and
       especially any creative or generated content the user may refer
       back to later: world-building, character settings, design sketches,
       code architecture, research summaries, outlines, etc. If you
       invested more than a sentence or two producing it, save it.
   (c) Key facts, preferences, and unresolved questions.
   Use one `save_memory` call per distinct fact (~50-200 chars each).
   Long content goes to rule 3.
   Exact duplicates are auto-rejected server-side, so err on the side of
   saving more. Near-duplicates are NOT rejected: an updated fact or a
   re-made document is saved as a new entry, and `search_memory` shows the
   newest version first.
   NOTE: Lite memories expire 7 days after they are saved — this is a
   rolling scratchpad, not a permanent archive.

   Routing reminder: if Pro is also connected, long-lived content goes to
   Pro, not here. See rule 0.

   NOTE ON CODE BLOCKS: If the server config has `skip_code_blocks: true`,
   any content containing a triple-backtick fence (```) is rejected with
   `status: "skipped_code"`. The default is `false`: the content is saved
   with each code block replaced by [code omitted] — the code itself is
   never kept or returned (it belongs in the project files). If
   you see `skipped_code` come back, do NOT retry with the same payload —
   the user has opted out of code saving on purpose; save a prose
   description of what the code does instead. Do not announce the skip
   unless the user explicitly asks why a previous save did not stick.

3. LONG CONTENT — SAVE VERBATIM IN ONE CALL
   When the turn produces OR receives a long body (> ~400 chars) the user
   may want back verbatim — a pasted spec / article / log, or a long
   creative setting you just generated — pass the FULL text to a SINGLE
   `save_memory` call. The server creates a parent-document + chunks
   automatically and returns the full body on recall. Do NOT split long
   verbatim-worthy content into many short summaries.

   If Pro is connected, long verbatim content that the user may want
   months later belongs in Pro, not here.

4. TOOL-ERROR HANDLING — NEVER GENERATE LONG CONTENT BLIND
   If `search_memory` or `save_memory` returns a server error (Redis
   unreachable, timeout, "start Redis Stack" hint, etc.), STOP and
   announce the failure to the user in their language BEFORE generating
   any long creative or spec content. Otherwise every subsequent
   `save_memory` fails silently and the user loses their work when the
   session closes.

   Required on error:
   - Surface the error succinctly.
   - Relay the recovery hint the tool returned (e.g. the docker run command).
   - Ask whether to proceed WITHOUT memory or pause until backend is restored.

   Short factual replies may proceed, but still mention the error once.

5. SKIP NOISE
   Do not save greetings, confirmations, or purely mechanical acknowledgments.

6. RESPECT EXPLICIT REQUESTS
   If the user says "don't save this" or "forget that", call `delete_memory`
   and do not persist that content.

7. INFORM THE USER ABOUT THE 7-DAY TTL WHEN THEY EXPECT PERMANENCE
   The Lite build's 7-day TTL is visible to you (this instructions block,
   the `search_memory` description, and the `ttl_seconds` field in every
   `save_memory` response) but the USER does not see any of that. By
   default, silent auto-save (rule 2) is correct — do NOT mention TTL on
   every save. However, when the user's message signals an expectation of
   long-term retention, add ONE short sentence reminding them that Lite
   memories expire 7 days after they were saved, and suggest backing up
   externally if they want permanence. Do NOT ask permission to save;
   save anyway, then mention the TTL.

   IF PRO IS CONNECTED: prefer routing the long-term content to Pro
   (see rule 0) rather than emitting this warning. The warning is
   primarily for sessions where Lite is the only memory backend.

   Signals that warrant the reminder (examples, non-exhaustive):
   - Explicit permanence words: "remember this forever", "don't forget",
     "save this permanently", "keep this for next time", "次回も覚えていて",
     「ずっと覚えておいて」「永続的に保存して」「絶対忘れないで」
   - Pasting or generating a long setting / spec / world-
     building / character sheet that the user obviously invested time in.
   - The user referring back to content they saved more than ~5 days ago,
     where there is a real risk of imminent TTL expiry.

   Phrase the reminder in the USER'S LANGUAGE. Examples:
   - Japanese: 「なお、Lite 版のメモリは保存から7日で自動削除されます。
               永続保存が必要な場合は Pro 版（sqlite-vec バックエンド）を、
               もしくは外部バックアップをご検討ください。」
   - English:  "Note: Lite memories auto-expire 7 days after they're saved.
                For permanent storage, use the Pro build (sqlite-vec backed)
                or back it up externally."

   Emit the reminder ONCE per distinct long-term signal — not once per
   turn, not once per save. Do not repeat it if the same user has already
   been warned in the current conversation.

8. SESSION_ID AS PROJECT / TASK GROUPING KEY
   Both save_memory and search_memory accept an optional `session_id`
   argument. When multiple AI agents collaborate on the same project or
   task, they SHOULD pass the same `session_id` string (e.g.
   `"proj-alpha"`, `"task-refactor-auth"`) on every call.

   Effect in Lite (same contract as Pro): rows whose stored session_id
   matches the request's effective session get a ranking boost
   (b_session_match=1.0); non-matching rows are dampened
   (b_session_mismatch=0.6). Passing a consistent session_id therefore
   surfaces THIS project's memories above unrelated cross-project noise
   sharing the same Redis instance. The boost is multiplicative on top
   of the existing (cos·0.7 + bm25·0.3) · time_decay · b_local score.

   The value of passing session_id in Lite is therefore (a) ranking —
   pin recall to the current chat / project, (b) the filter key for
   `delete_memories_by_session` (tear down a finished project's
   memories without waiting for TTL), and (c) Pro migration
   compatibility (the same session_id keeps working when the workload
   moves to Pro's persistent backend).

   Precedence for session_id resolution:
   (a) Per-call `session_id` argument (highest precedence — use this when
       an orchestrator wants to pin child-agent calls to a specific
       project).
   (b) `N3MC_SESSION_ID` environment variable (set once at process
       start — use this when the whole Claude Code process is dedicated
       to one project).
   (c) Per-process UUIDv4 (fallback — fresh on each server start). Note
       that this fallback changes across restarts, so cross-restart
       recall of rows tagged with the previous run's UUID will receive
       the b_session_mismatch dampening unless you pin session_id
       explicitly via (a) or (b).

   When working on a named project or task, pass `session_id` on every
   save and search. For ad-hoc work, leave it blank.

9. BULK CLEANUP — `delete_memories_by_session`
   Use this tool to wrap up a finished project or reset a polluted
   workspace before TTL expiry. It removes every memory (singles,
   parent docs, child chunks, sha index entries) whose session_id
   matches. Scoped to the configured owner.

   Use cases:
   - Project closed and you want to keep search results clean.
   - A test/debug session generated noise that's hurting later recalls.
   - Reclaiming Redis memory early instead of waiting 7 days.

   Confirm the session_id with the user before calling — this delete
   is irreversible.

10. THREAD CONTEXT RETRIEVAL — `recall_thread` (§4.3.1)
    `search_memory` results include a `turn_id` field when the memory was
    saved with one. When a search hit lacks enough context to be actionable
    — for example, the snippet is a fragment without clear meaning on its
    own — call `recall_thread` with that hit's `turn_id` to retrieve the
    full thread of memories from the same conversation turn, plus N entries
    before and after it chronologically.

    Usage pattern:
      1. `search_memory` returns a hit with `turn=<some-id>` in the header.
      2. You judge the snippet insufficient for answering the user.
      3. Call `recall_thread(turn_id="<some-id>", before=2, after=2)`.
      4. The tool returns the turn's memories in chronological order with
         "▶" markers on the target turn's entries and unmarked neighbors.

    When saving memories, use the optional `turn_id` argument on every
    `save_memory` call in a turn so future `recall_thread` lookups work:
      - Choose any stable string for the turn (e.g. a short UUID, or
        "turn-1", "turn-2", …). Use the SAME value for all saves within
        one turn.
      - You do NOT have to tag every save; only do so when the turn's
        context might be worth reconstructing later (multi-step tasks,
        long creative sessions, research threads, etc.).

    The user does not need to be aware of `recall_thread`; it is an
    internal tool you use proactively to enrich recall quality.

11. VERSIONS AND NOTICES
    Every `search_memory` hit shows a `saved=` time. When several versions
    of the same content appear in the results, the one with the newest
    `saved=` time is the current one — answer from it (mention the change
    if it matters).

    If a `search_memory` result starts with `> ⚠️ N3MC notice:`, tell the
    user what it says, in their language (e.g. their `config.json` could
    not be read, or a setting has the wrong type and was ignored).

12. WEAK MATCHES
    Records under `#### Possibly related (weak matches — may be unrelated)` did not
    pass the relevance floors and are added only so that a search is never
    empty. Use one only when it is actually relevant to the question; if it
    is not, ignore it and do not mention it (do not say that unrelated
    memories were found).
`````

**(2) The reminders appended to every tool result — `n3mc_mcp/tools.py` (v1.12.2: `server.py`).** (v2.0.0: added by the resident server unless `/tools/<name>` is called with `nudge=0`, §4.5; the texts are unchanged.) `search_memory` results end with `_NUDGE_AFTER_SEARCH`; `save_memory` results end with `_NUDGE_AFTER_SAVE`; every other result — including every error result and the answer to an unknown tool name (`Error: unknown tool '<name>'`) — ends with `_NUDGE_GENERIC`.

`````python
_NUDGE_AFTER_SEARCH = (
    "\n\n---\n_Reminder: before closing this turn, call `save_memory` "
    "to persist the user's intent (paraphrased) and any substantive "
    "output you generate. If a persistent N3MC memory (`n3mc-pro`, "
    "`n3mc-pg` or `n3mc-longtermmemory`) is also connected, route "
    "long-lived artifacts there; this server is for "
    "transient task scratch (7-day TTL). Do not skip the save just "
    "because no memory hit was returned above._"
)
_NUDGE_AFTER_SAVE = (
    "\n\n---\n_Reminder: if this turn produced multiple distinct facts or a "
    "long verbatim artifact, make sure each was saved — one "
    "`save_memory` call per fact, or one full-text call for verbatim "
    "long content (>400 chars). Exact duplicates are auto-rejected, so "
    "err on the side of saving._"
)
_NUDGE_GENERIC = (
    "\n\n---\n_Reminder: before closing this turn, call `save_memory` "
    "to persist the user's intent (paraphrased) and any substantive "
    "output you produced. Lite entries expire 7 days after save, so "
    "saving more is safer than saving less._"
)
`````

**(3) The tool definitions returned by `tools/list` — `list_tools()` in `n3mc_mcp/server.py`.** (v2.0.0: the `Tool` list lives in `n3mc_mcp/tooldefs.py`; the thin client's `list_tools()` returns it. The definitions are unchanged.) Seven tools. The descriptions, the argument descriptions, the `minimum` / `maximum` / `default` values and the `required` lists are what the connected model reads; write them exactly as below.

`````python
@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search_memory",
            description=(
                "Hybrid (vector + BM25) search over stored memories. "
                "Call this at the start of every user turn. "
                "Each hit shows its saved= time; when several versions of the "
                "same content appear, the newest is listed first and is the "
                "current one. "
                "When fewer than min_results (default 3) rows pass the "
                "relevance floors, the closest dropped rows are added under "
                "'Possibly related (weak matches — may be unrelated)': they "
                "are often unrelated — use one only when it is actually "
                "relevant to the question; if it is not, ignore it and do not "
                "mention it. "
                "NOTE: Lite memories expire 7d after they were saved."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (natural language or keywords).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default: config search_result_limit).",
                        "minimum": 1,
                        "maximum": 100,
                    },
                    "session_id": {
                        "type": "string",
                        "description": (
                            "Optional project/task grouping key. Rows whose stored "
                            "session_id matches this value are boosted in ranking "
                            "(b_session_match=1.0); non-matching rows are dampened "
                            "(b_session_mismatch=0.6). Pass the same session_id used "
                            "at save time to surface that project's memories above "
                            "unrelated rows. Leave blank to use the server default "
                            "(N3MC_SESSION_ID env var, or per-process UUIDv4)."
                        ),
                    },
                    "since": {
                        "type": "string",
                        "description": (
                            "§4.3.1 — ISO 8601 date or datetime (e.g. '2026-05-01' or "
                            "'2026-05-01T09:00:00+09:00'). Only entries saved ON OR "
                            "AFTER this timestamp are returned. Date-only values and "
                            "values without an offset are read in the server's local "
                            "time (the clock of saved=); date-only means 00:00:00. "
                            "An unreadable value is an error."
                        ),
                    },
                    "until": {
                        "type": "string",
                        "description": (
                            "§4.3.1 — ISO 8601 date or datetime. Only entries saved "
                            "ON OR BEFORE this timestamp are returned. Read like "
                            "since; date-only means 23:59:59.999999 local time."
                        ),
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="save_memory",
            description=(
                "Save a memory entry (Lite: 7-day TTL). "
                "Rejects exact duplicates only; an updated fact or a re-made "
                "document is saved as a new entry (search_memory lists the "
                "newest version first). "
                "Long content (>chunk_threshold chars) is chunked with a "
                "parent-document for verbatim recall."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Memory content to save.",
                    },
                    "agent_name": {
                        "type": "string",
                        "description": "Agent display name (e.g. 'claude-code').",
                    },
                    "owner_id": {
                        "type": "string",
                        "description": "Owner UUID override (must match server config).",
                    },
                    "importance": {
                        "type": "number",
                        "description": "Importance weight 0.5–2.0 (default 1.0).",
                    },
                    "session_id": {
                        "type": "string",
                        "description": (
                            "Optional project/task grouping key. Stored on the row "
                            "and used as: (a) the ranking key for search_memory's "
                            "b_session boost (match=1.0 / mismatch=0.6), and "
                            "(b) the filter for delete_memories_by_session. "
                            "Pass the same value across all calls for one project. "
                            "Leave blank to use the server default "
                            "(N3MC_SESSION_ID env var, or per-process UUIDv4)."
                        ),
                    },
                    "turn_id": {
                        "type": "string",
                        "description": (
                            "§4.3.1 — Optional turn grouping label. "
                            "Tag all memories saved within one conversation turn with "
                            "the same turn_id. Enables recall_thread to surface the "
                            "surrounding context later. Any non-empty string works; "
                            "a short UUID, timestamp, or 'turn-N' format is recommended."
                        ),
                    },
                },
                "required": ["content"],
            },
        ),
        Tool(
            name="list_memories",
            description=(
                "List stored memories newest first. "
                "Parent documents shown with [doc×N] tag."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20).",
                    },
                },
            },
        ),
        Tool(
            name="delete_memory",
            description=(
                "Delete a memory by ID (only memories of the configured owner). "
                "If the ID is a parent document (doc:<uuid>), cascades to all child chunks."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Memory ID or parent document ID to delete.",
                    },
                },
                "required": ["id"],
            },
        ),
        Tool(
            name="delete_memories_by_session",
            description=(
                "Delete every memory (singles, parent docs, child chunks, sha guards) "
                "whose session_id matches. Scoped to the configured owner. "
                "Use this to wrap up a finished project or reset a polluted session "
                "before TTL expiry. IRREVERSIBLE — confirm session_id with the user first."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "session_id whose memories should be removed.",
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="repair_memory",
            description=(
                "Re-ensure the RediSearch index (idempotent) and add missing "
                "vectors (up to 200 per call) to entries saved while the "
                "embedding model was unavailable. "
                "Returns {status, message, reembedded, remaining}."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="recall_thread",
            description=(
                "§4.3.1 — Retrieve the memories sharing a turn_id plus N entries "
                "before and after in chronological order. "
                "Use this when a search_memory hit lacks enough context: pass the "
                "hit's turn_id here to surface the surrounding conversation thread. "
                "Returns {status:'not_found'} if turn_id is unknown."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "turn_id": {
                        "type": "string",
                        "description": "The turn_id from a search_memory result.",
                    },
                    "before": {
                        "type": "integer",
                        "description": "Number of earlier entries to include (default 2).",
                        "minimum": 0,
                        "maximum": 20,
                    },
                    "after": {
                        "type": "integer",
                        "description": "Number of later entries to include (default 2).",
                        "minimum": 0,
                        "maximum": 20,
                    },
                },
                "required": ["turn_id"],
            },
        ),
    ]
`````

**(4) Text form of the listing tools.**

- `list_memories`: when nothing is stored, `_No memories stored._`. Otherwise one block per entry, `**<tag> [<id>]** <first 19 characters of timestamp>` + line feed + the purified content, where `<tag> ` is present only when the entry has a tag (`[doc×N]` for a parent document); blocks are joined with `\n\n---\n\n`. The content of a parent document is its first 200 characters, followed by `…` only when it was longer than 200. Standalone memories (at most `limit`) and all parent documents are merged, sorted by the timestamp string descending, and cut to `limit`. A `limit` that is missing, empty or 0 is 20; a value below 1 is 1.
- `recall_thread`: an empty `turn_id` returns `{"status": "error", "reason": "turn_id required"}`; no rows returns `{"status": "not_found", "turn_id": "<turn_id>"}`. Otherwise one block per row, `<marker>**<tag> [<id>]** <first 19 characters of timestamp>` + line feed + the purified content, where `<marker>` is `▶ ` for rows of the requested turn and two spaces for the others; blocks are joined with `\n\n---\n\n`. The range runs from the first row of the turn minus `before` to its last row plus `after`, and includes rows of other turns that lie in between. A parent document is shown in full here (not cut to 200 characters).
- `search_memory`: hits are joined with `\n\n---\n\n`; the notices, the version sentence, the hits, the weak-match heading and the weak hits are joined with one empty line (`\n\n`). (v2.0.0: then the size limit of §3.17.)

**(5) Which characters are expanded to bigrams (§3.7) — `n3mc_mcp/processor.py`.** `_is_cjk(ch)` is true when the code point lies in one of these ranges; "CJK run" in §3.7 means a run of such characters. A run of a single such character is kept as that one character.

`````python
_CJK_RANGES = [
    (0x0E00, 0x0E7F),    # Thai
    (0x0E80, 0x0EFF),    # Lao
    (0x1000, 0x109F),    # Myanmar
    (0x1780, 0x17FF),    # Khmer
    (0x3000, 0x9FFF),    # CJK punct + Hiragana + Katakana + CJK Unified
    (0xAC00, 0xD7AF),    # Hangul Syllables (Korean)
    (0xF900, 0xFAFF),    # CJK Compatibility Ideographs
    (0x20000, 0x2A6DF),  # CJK Unified Ideographs Ext B
]
`````

For runs of other letters, `content_ngram` also receives the form with the combining marks removed (`fold_diacritics`: NFD, then drop every character of Unicode category `Mn`), so `café` is also indexed as `cafe`. The stored content is not changed.

**(6) Lexical re-rank (§3.6): dampening for short content.** The boost of a row is `(coverage × weight + phrase_bonus) × length_factor`, where `length_factor = len(content) / 50` when the normalized, lower-cased content is shorter than 50 characters and 1.0 otherwise. The boost is added to `score` and, when present, to `filter_score`. Without the factor, a tiny snippet that happens to contain every query token is promoted above real answers.

**(7) Chunking (§3.11): the soft breaks.** In each window, look for a break only inside its last `overlap` characters (from `max(start + 1, end − overlap)` to `end`), trying the separators in this order and taking the **last** occurrence of the first separator that is found; cut right after it. After a soft break the next window starts at the cut (no overlap). When none is found, cut at `end` and start the next window `overlap` characters earlier (always at least one character forward).

`````python
_SOFT_BREAKS = (
    "\n\n",                      # paragraph break  (highest priority)
    "\n",                        # line break
    ". ", "! ", "? ",            # ASCII sentence ends
    "。", "！", "？", "…",      # CJK / universal sentence ends
    " ", "\t",                   # word boundary    (lowest priority)
)
`````

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

The last three keys are new in v2.0.0. An existing `config.json` gets them filled in with their defaults on the next start (the "missing fields" rule below).

- `redis_url` — connection URL. `N3MC_REDIS_URL` env var overrides this field.
- `ttl_seconds` — TTL applied to every new memory and its sha-guard (default 7 d). Lowering it is fine; raising it far beyond a week defeats the purpose of the Lite and will be flagged during review.
- `search_result_limit` — max results returned by `search_memory`.
- `vector_min_cos` / `relative_min_score_ratio` / `version_order_enabled` / `version_sim_threshold` / `version_min_chars` — search result shaping ([§3.14](#314-search-result-shaping-v170-weak-match-fill-since-v172)). `vector_min_cos` (0.85) and `version_sim_threshold` (0.98) were measured with `multilingual-e5-base`; re-measure them for another `embedding_model`. A sensible `vector_min_cos` range is 0.80–0.88 (lower: more recall, more loosely related rows; higher: shorter results, related rows dropped more easily). Changes take effect when the MCP server process restarts (restart the MCP client).
- `min_results` (v1.7.2) — when fewer results than this pass the floors, the shortfall is filled with **weak matches** — candidates the floors dropped, shown under a separate heading and not counted in `access_count` (default `3`; [§3.14](#314-search-result-shaping-v170-weak-match-fill-since-v172) step 6). Set to `0` to disable the fill. Changes take effect when the MCP server process restarts.
- `dedup_threshold` and `context_char_limit` (v1.6.x) are **removed** — ignored if present in an existing `config.json` (no need to delete them).
- `min_score` — excludes results whose `filter_score` (the score without `time_decay` and `b_session`, [§3.6](#36-ranking-formula)) is below this value (default `0.2`). Set to `0.0` to disable. **The filter is applied after lexical rerank ([§3.6](#36-ranking-formula))** — it judges the score including phrase/coverage boosts, so a candidate just below the threshold at fusion time can be rescued by the rerank boost.
- `search_query_max_chars` — max characters used from a query (default `2000`; embedding model caps at ~512 tokens).
- `chunk_threshold` / `chunk_overlap` — sliding-window size and overlap (defaults 400 / 100 chars). Bodies longer than the threshold trigger the parent-document + chunks path (see [§3.11](#311-verbatim-recall-parent-document--chunks-pattern)).
- `access_count_enabled` / `access_count_weight` / `access_count_max_boost` — enable flag, per-hit weight, and cap for the frequency boost (see [§3.6](#36-ranking-formula)). **Default `false` since 2026-11-17**: the formula uses `stored_importance` only; `true` turns the boost on.
- `ttl_refresh_top_k` — the number of top ordinary rows whose `access_count` is incremented after each search (§3.14 step 7; `0` counts nothing). The name is kept from earlier versions: **no TTL is reset** (decided 2026-11-22, §3.3). `ttl_refresh_on_search` was removed the same day; a value left in an existing `config.json` is ignored.
- `lexical_rerank_enabled` / `rerank_weight` / `rerank_phrase_weight` — lightweight post-fusion lexical reranker (see [§3.6](#36-ranking-formula)). Setting `enabled` to `false` passes the fused score through unchanged.
- `b_session_match` / `b_session_mismatch` — the `b_session` factor in the ranking formula (see [§3.6](#36-ranking-formula)). For each row, the search compares the request's `effective_session` (per-call argument → `N3MC_SESSION_ID` env var → per-process startup UUID) against the row's stored `session_id`: matches multiply the score by `b_session_match` (default `1.0`), mismatches by `b_session_mismatch` (default `0.6`). Set both to `1.0` to disable the bias entirely (all rows symmetric).
- `skip_code_blocks` — when `true`, `save_memory` rejects any content containing a triple-backtick fence (```` ``` ````) and returns `{"status": "skipped_code", "saved": false}`. Default `false` (inherit the FastAPI-era N3MemoryCore behavior where users who did not want code in memory could opt out). Heuristic only — the fence marker is the signal; mixed prose+code is rejected wholesale. With `false`, the body is saved with its code blocks replaced by `[code omitted]` (v1.9.0, §3.13). The LLM is instructed (§5) to avoid retrying the same payload on `skipped_code` and to save a prose description instead.
- `server_port` (v2.0.0) — the port of the resident server (default `18524`, §3.15). Free uses 18520, Pro 18521, the PostgreSQL edition 18522 and the Ollama edition 18523; keep a port of its own for each N3MC product on one PC (§14).
- `bind_host` (v2.0.0) — the address the resident server listens on. Keep the default `127.0.0.1` (§3.15).
- `context_max_chars` (v2.0.0) — the largest size of the hook's `# Recalled Memory Context` block and of the MCP `search_memory` result, counted as in §3.17 (default `9500`). Claude Code moves hook output over 10,000 characters to a file and leaves a 2,000-character preview, so the allowed range is 2000–10000. A manual `--search` is not limited.
- **When a change takes effect (v2.0.0)**: editing `config.json` changes the resident server's `code_version`, so the next hook, CLI or MCP call stops the server and starts a new one that reads the file (§3.15). Where this section says "when the MCP server process restarts (restart the MCP client)", read "on the next call after the edit".
- `embedding_model` / `embedding_dim` — the embedding model name (default `intfloat/multilingual-e5-base`) and its output vector dimension (default `768`). See [§3.2](#32-embeddings). Made configurable so the operator can pick the quality / resource / language tradeoff while the product stays language-neutral. `embedding_dim` MUST match the actual output dimension of `embedding_model` — the RediSearch vector index (`DIM`) is built from this value, so a mismatch breaks save and search. Changing either requires a **process restart + index flush** (Lite's 7-day TTL means stale data expires on its own if left). With the defaults unchanged, behavior is identical to prior versions.

**Unreadable or mistyped `config.json` (v1.7.0)** — users edit this file by hand (e.g. to tune `vector_min_cos`), so typos are expected. **Never overwrite it.**

| Case | Behavior |
|---|---|
| File missing | Create it **exclusively** with the defaults and fresh `owner_id` / `local_id` (v1.7.1, below). If another process created it first, use that file instead |
| Not valid JSON, or the top level is not an object | **Do not modify the file.** Run on defaults; use fresh UUIDs for this process only (not saved). Emit notice `config_unreadable` (text: `config_unreadable: <path> could not be read (<reason>) …`) |
| A value has the wrong type (e.g. `"vector_min_cos": "0.83"`, a quoted number) or is outside its allowed range (table below) | Ignore **only that item** (use its default) and use the others. Do not modify the file. Emit notice `config_type` naming the items |
| Readable | Fill missing fields with defaults and save (as before; a missing `owner_id` / `local_id` is filled under the lock of "Writing `config.json`" below) |

- Type check (`config_value_ok(key, value, chunk_threshold=None)`; `chunk_threshold` is the value in effect, needed for `chunk_overlap`): if the default is a bool, accept only bools; if a string, only strings; numbers follow the table below (a bool is never a number). Fields without a default (`owner_id`, `local_id`) and unknown fields are not checked.

**Allowed values (v1.7.1, Lite review LM2)**:

| Keys | Type | Allowed range |
|---|---|---|
| `ttl_seconds` | integer | ≥ 1 |
| `search_result_limit`, `search_query_max_chars`, `embedding_dim` | integer | ≥ 1 |
| `chunk_threshold` | integer | ≥ 50 |
| `chunk_overlap` | integer | ≥ 0 and < `chunk_threshold` (the value in effect after checking `chunk_threshold`). When the configured value — or the default 100, if the key is absent or rejected — is not below `chunk_threshold`, use `chunk_threshold // 4` (lowering only `chunk_threshold` to 100 or less must not break saving) |
| `ttl_refresh_top_k`, `version_min_chars`, `min_results` (v1.7.2) | integer | ≥ 0 |
| `half_life_days` | number | > 0 |
| `bm25_min_threshold`, `access_count_weight`, `access_count_max_boost`, `rerank_weight`, `rerank_phrase_weight` | number | ≥ 0 |
| `min_score`, `vector_min_cos`, `relative_min_score_ratio`, `version_sim_threshold`, `b_session_match`, `b_session_mismatch` | number | 0–1 |
| `server_port` (v2.0.0) | integer | 1–65535 |
| `context_max_chars` (v2.0.0) | integer | 2000–10000 |
| `bind_host` (v2.0.0) | string | any (a value other than `127.0.0.1` prints a warning, §3.15) |

- "Integer" means a JSON integer: `604800.5` and also `768.0` are rejected. v1.7.0 accepted them; `ttl_seconds: 604800.5` (or `0`, or a negative value) made every `save_memory` fail with a Redis `DataError`, `embedding_dim: 768.0` made `FT.CREATE` fail at start, and `chunk_overlap >= chunk_threshold` (e.g. only `chunk_threshold` lowered to 100) made `chunk_text` loop forever on a body without breaks — none with a notice.
- An out-of-range value is handled like a wrong type: only that key uses its default, and `config_type` names it.

**Writing `config.json` (v1.7.1, Lite review LH3)**:

- **Creating** a missing file: write the content to a temporary file in the same folder, then create the target exclusively — `os.link(tmp, path)` (fails if the target exists), or `open(path, "x")` where hard links are not available — and remove the temporary file. If the target already exists, another process won: stop creating, read that file and use its `owner_id` / `local_id`. If it cannot be parsed yet (a writer that does not use this rule), wait 0.1 s and read again, up to 2 seconds.
- **Updating** (filling missing fields) and the legacy migration (§7): write a temporary file in the same folder and `os.replace` it onto the target, so an interrupted write never leaves a truncated file.
- **Filling in a missing `owner_id` / `local_id` of an existing file (2026-11-22, review #2)**: take the lock file `config.json.lock` in the same folder (`open(..., "x")`; retry every 0.05 s for up to 10 s; a lock file older than 10 s is stale and is removed), **read `config.json` again under the lock**, generate an ID only when it is still missing, write with `os.replace`, remove the lock, and use the IDs of the file as read under the lock. A process never runs with an ID that is not in the file, unless the write itself fails (the existing `could not update` path). When the lock cannot be taken within 10 s (or cannot be created at all), fill in without it, as before. **On Windows**, reading `config.json` and its `os.replace` are retried every 0.05 s for up to 1 s on `PermissionError` (another process or a virus scanner may have the file open for a moment; without the retry a concurrent start once ended with an ID that was not in the file). (Until then two processes that read the same file without IDs each wrote their own `owner_id`; the loser kept running with an ID that was not in the file, and the records it saved were filtered out by `owner_id` from the next start on — the LH3 failure below, for an existing file: reproduced.)

> **⚠️ Why (v1.7.1, Lite review LH3)**: v1.7.0 let every starting process generate its own `owner_id` and write the file. Starting 8 processes at once, 5 times out of 5 all 8 ended up with different `owner_id`s and 7 kept running with an ID that was not in the file; their saves returned `saved: true`, but search and list filter by `owner_id`, so those memories were invisible from the next start on. The Claude desktop app on the development PC starts two registrations at the same moment.

- **While `config.json` is unreadable** (the run uses fresh in-memory IDs, table above), every `save_memory` response adds `"warning": "config_unreadable", "detail": "config.json could not be read, so this entry was saved under a temporary owner_id and will not be visible after the server restarts. Fix <path> and restart the MCP client."` (v1.7.1; v1.7.0 warned only on the first search — Lite review LL9). The warning is added to responses that saved something. If the entry was also saved without a vector, the two are combined: `"warning": "embedding_unavailable; config_unreadable"` and the two `detail` texts joined with a space.
- **Notices**: `load_config()` collects these messages in `cfg["_notices"]` (a list of strings, each starting with its key, e.g. `config_type: …`) and prints each to `stderr`. The connection layer adds `shared_redis` / `config_refused` (§3.4) and `index_dim_mismatch` (§3.9) to the same list. The server prepends each notice **once**, to the output of the first successful `search_memory` call after the notice was added (an error response must still start with `Error:`, so the notices wait for the next successful search), as `> ⚠️ N3MC notice: <message>` lines, so the connected LLM can tell the user (§5 rule 11). A notice added later in the process — `shared_redis` after a reconnect, for example — is therefore shown on the next successful search (v1.7.1; v1.7.0 showed notices only on the first search of the process, so later ones were never shown). Fixing the cause and restarting the client clears them. **(v2.0.0)** "The process" is the resident server; "the first successful `search_memory`" is the first successful search of either kind (the MCP `search_memory`, or the hook's or CLI's `/search`, §3.15 "Where notices are shown"); fixing `config.json` restarts the server by itself (`code_version`). Every process that reads `config.json` (the CLI, the hooks, the thin client) still prints these messages to its own stderr.

> **Multi-account on a single PC**: each OS user runs the server under their own `config.json`, so each has its own `owner_id`. Lite does not share memories between owners: search and list show only the records of the configured `owner_id` (§3.12). Do not point two accounts at one Redis — the duplicate guards do not contain `owner_id`, so a body saved by one account is answered `duplicate` for the other (§3.8), and the other account's records can fill the search candidates (§3.12). Give each account its own Redis (another container on another port, set in its `redis_url`). Memories are shared only between servers that use the same `config.json` (the same `owner_id`), for example the Claude desktop app and the terminal `claude` of one account. (Until the final review of 2026-11-18, L-02, this note said that two accounts could share a Redis by setting the same `redis_url`.)

---

## 7. Data Location

By default, only `config.json` lives on disk, in **`~/.n3mc-workingmemory/`** (the user's home directory; on Windows `%USERPROFILE%\.n3mc-workingmemory\`). Override via the environment variable `N3MC_DATA_DIR` (absolute path).

Files inside the data directory:
- `config.json` — configuration (up to v1.12.2 the only on-disk artifact)
- (v2.0.0) `n3mc-lite.pid` — PID file of the resident server (exists only while it runs, §3.15)
- (v2.0.0) `server.log` — stderr of the resident server (truncated when over 1 MB at a start, §3.15)
- (v2.0.0) `server_start_failed` — marker of a failed server start (5-minute back-off, §3.15)
- (v2.0.0) `notices.json` — notices that need human action (§3.15)
- (v2.0.0) `audit.log`, `audit.log.1` — JSONL audit trail of every hook call, written before anything else; it holds the raw prompts and replies, **including code blocks and pasted text, with no TTL** (§12). Treat it as private data. Rotated at 50 MB by `--stop`, so at most about 100 MB is kept.
- (v2.0.0) `turn_id_<session>.txt` (`turn_id.txt` without a session) — the `turn_id` handed from the UserPromptSubmit hook to the Stop hook (§12); files older than 30 days are removed by `--stop`

These files are flat in the data folder (Free keeps them in a `.memory/` subfolder; Lite has no database file to separate them from). `audit.log` is the only file that keeps conversation text beyond Redis's TTL; it exists because the hook must not lose a message when Redis is down (§12).

> **⚠️ Why the home directory, not the platform app-data directory (v1.7.0)**: up to v1.6.x the config lived in the platform user-data directory (`%LOCALAPPDATA%\n3mc-workingmemory\n3mc-workingmemory\` on Windows, via `platformdirs`). The Microsoft Store (MSIX) build of the Claude desktop app runs the MCP servers it launches inside its package container, and Windows **redirects their reads and writes under `%LOCALAPPDATA%`** to `%LOCALAPPDATA%\Packages\<package>\LocalCache\Local\…`. The same PC therefore had two `config.json` files with two different `owner_id`s — one for servers launched by the desktop app, one for servers launched from a terminal (`claude`, scripts) — and because search and list filter by `owner_id` (§3.12), memories saved from one were invisible to the other, on the same Redis. The home directory is not redirected, so every launcher reads the same file.

**Migration (once)**: when `~/.n3mc-workingmemory/config.json` does not exist (and `N3MC_DATA_DIR` is not set), `load_config()` copies the legacy file from the platform user-data directory (`platformdirs.user_data_dir("n3mc-workingmemory")` + `config.json`, as seen by the starting process) byte-for-byte to the new location, prints one line to `stderr`, and leaves the legacy file in place. The copy uses the exclusive-create rule of §6 (temporary file + `os.link`), so two processes migrating at once end up with one file. The first process to start after the upgrade decides which `owner_id` is kept; entries saved under the other `owner_id` (at most `ttl_seconds`, 7 days, old) stop appearing in results and expire on their own. If no legacy file exists, a new config is created as usual. Redis state lives wherever the Redis container stores it (by default, an anonymous Docker volume that vanishes with `docker rm -f redis-stack`).

---

## 8. Client Configuration

> **(v2.0.0) Which application uses what.** Claude Code uses **the hooks only**; the Claude desktop app and other applications without hooks use **MCP only** (see "What changed in v2.0.0"). This section was titled "MCP Client Configuration" up to v1.12.2.

### Claude Desktop

The desktop app starts the MCP servers listed in `claude_desktop_config.json`. Open it from the app (Settings → Developer → Edit Config); the usual place on Windows is `%APPDATA%\Claude\claude_desktop_config.json`, and the Microsoft Store version of the app may keep it in another folder, so prefer opening it from the app. Add the entry under `mcpServers` (keep the entries already there), save, and **quit the app completely and start it again**.

pip installation (v2.0.0: absolute path and `env`):

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

Plugin users (no pip installation; the hooks run through `uvx`):

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

- `command`: **the absolute path** of `n3mc-workingmemory` from the same scripts folder the hooks use (Windows: `where n3mc-workingmemory`; Linux: `which n3mc-workingmemory`). The app does not use the `PATH` of your terminal. Hooks and MCP must run the same code (§4.6 "Limits of this design").
- `env.N3MC_AGENT_NAME` (v2.0.0): the name stored as `agent_name` when the model does not pass one (§4.6). Use one name per application.
- When the hooks use another data folder (`N3MC_DATA_DIR`), add the same `N3MC_DATA_DIR` to `env`; otherwise the desktop app would use `~/.n3mc-workingmemory`, another `config.json` and another `server_port`.
- `N3MC_SESSION_ID` may be added to pin the default `session_id` of this application (§3.1).
- Other MCP clients on Windows or Linux are registered the same way (command, args, env).

### Claude Code (hooks — v2.0.0)

**Do not register the MCP server in Claude Code** (`claude mcp add`, `~/.claude.json`, `.mcp.json`). Claude Code gets its memory from the hooks (every message and reply saved, the matching memories put into every turn) and from the CLI `n3mc-lite` (§12, §13). Two ways to install:

**(a) Plugin marketplace (recommended — no manual config file)**

```
/plugin marketplace add NeuralNexusNote/n3mcmcp-lite
/plugin install n3mc-workingmemory@neuralnexusnote
```

Requires `uv` on PATH. `plugins/n3mc-workingmemory/.claude-plugin/plugin.json` keeps its metadata and has **no `mcpServers` key** any more (v2.0.0). `plugins/n3mc-workingmemory/hooks/hooks.json` registers three hooks:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "<COMMAND A, below, as a JSON string>"
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          { "type": "command", "command": "uvx --from n3memorycore-mcp-lite==2.0.1 n3mc-lite-hook --uvx", "timeout": 60 }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          { "type": "command", "command": "uvx --from n3memorycore-mcp-lite==2.0.1 n3mc-lite-stop-hook --uvx", "timeout": 60 }
        ]
      }
    ]
  }
}
```

COMMAND A (the shell command; in `hooks.json` it is written as a JSON string, with `"` and `\` escaped):

```text
python "${CLAUDE_PLUGIN_ROOT}/hooks/install_permissions.py" || py "${CLAUDE_PLUGIN_ROOT}/hooks/install_permissions.py" || python3 "${CLAUDE_PLUGIN_ROOT}/hooks/install_permissions.py" || echo '{"systemMessage": "<FALLBACK>", "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "<FALLBACK>"}}'
```

`<FALLBACK>` is the text of the fallback JSON in "Auto-approve" below (with its `\"` escapes). Only the text changed from v1.12.2.

- `--uvx` tells the hook entries that this installation runs through `uvx`, so the rules file and the plugin's permission rules name the `uvx --from n3memorycore-mcp-lite==2.0.1 n3mc-lite` command form (§12). Without it the `python -m n3mc_mcp.cli` form is used (pip installations).
- The resident server is started by the hook process with its own `sys.executable` (`-m n3mc_mcp.cli --run-server`, §3.15), i.e. inside the `uvx` environment.
- **Start-up time of `uvx` (measured 2026-11-19, Windows 11, uv 0.11.7, package already in the uv cache; median of 10 runs after one warm-up)**: `python -c pass` with the system Python 125 ms; `uvx --from n3memorycore-mcp-lite==2.0.1 python -c pass` 316 ms (pinned `==1.12.2`: 317 ms); `uvx --from n3memorycore-mcp-lite==1.12.2 python -c "import n3mc_mcp.config"` 503 ms. So `uvx` adds about 0.19 s to each hook start, and each message starts two hooks (`UserPromptSubmit` and `Stop`). Not measured: the v2.0.0 hook code itself (it does not exist yet), a run that must download or refresh the package, and Linux. **Decided 2026-11-19: the plugin's hooks run through `uvx`; the added time is accepted.** The `uvx` command is pinned to the package version (decided 2026-11-19, §2).

**(b) pip**

```bash
pip install n3memorycore-mcp-lite
n3mc-lite --init
```

`n3mc-lite --init` registers the two hooks in `~/.claude/settings.json` with the absolute paths of `n3mc-lite-hook` / `n3mc-lite-stop-hook` and adds the permission rules below (§12 "`--init`"). Restart Claude Code; memory starts with the next session.

**Old MCP registrations (v1.12.2 and earlier)**: an `n3mc-workingmemory` entry in Claude Code (user scope in `~/.claude.json`, or a project's `.mcp.json`) is no longer used. `n3mc-lite --init` reads `~/.claude.json` — its top-level `mcpServers` object and the `mcpServers` object of each entry under `projects` (L15) — and the current folder's `.mcp.json` (read only) and, when it finds one, prints `An MCP registration of n3mc-workingmemory was found in Claude Code (<file>). Claude Code now uses the hooks; remove it with: claude mcp remove n3mc-workingmemory -s user` (for `.mcp.json`: `remove the "n3mc-workingmemory" entry from <file>`; an entry under `projects` is a local-scope registration that `-s user` does not remove, so for it: `An MCP registration of n3mc-workingmemory was found in Claude Code (<file>, project <project path>). Claude Code now uses the hooks; remove it with: claude mcp remove n3mc-workingmemory -s local (run it in that project)`). It does not edit these files. If it stays, Claude Code shows the tools again next to the hooks; the instructions then tell the model not to save every turn (§5).

Restart the client after editing any config. Ensure Redis Stack is running before the first message — otherwise the hooks keep the text only in `audit.log` and show the "start Redis Stack" notice (§12).

### Automatic registration after code generation (Method A — required)

**Method A ends with the hooks registered and verified by the AI. The user's only remaining step is a full restart.** Do not ask the user to edit any config file, and do not finish Phase 1 with a working package that no client uses. Do this right after `pip install -e .` succeeds:

1. **Find the entry points on this PC.** Take the scripts folder of the Python that ran `pip install -e .` (`python -c "import sysconfig; print(sysconfig.get_path('scripts'))"`, or the venv's `Scripts\` / `bin/`) and check that `n3mc-lite`, `n3mc-lite-hook`, `n3mc-lite-stop-hook` and `n3mc-workingmemory` (`.exe` on Windows) exist there. Use **absolute paths**, not bare command names: Claude Code may run commands with a `PATH` that does not contain the scripts folder.
2. **Remove stale MCP registrations from Claude Code (v2.0.0).** Run `claude mcp remove n3mc-workingmemory -s user` (a "not found" error is fine). In the install folder, if `.mcp.json` has an `n3mc-workingmemory` entry, remove that entry and keep the other servers. If the `claude` command is not on `PATH`, remove the `n3mc-workingmemory` entry from the top-level `mcpServers` object of `~/.claude.json` instead — after copying the file to a backup, keeping every other key, and writing it atomically (temporary file in the same folder, then replace). **Why**: Claude Code uses the hooks; a registration left from v1.12.2 would show the tools again and make the model save on its own next to the hooks.
3. **Register the hooks**: run `<absolute path of n3mc-lite> --init` (§12). It writes the hooks and the permission rules into `~/.claude/settings.json`, keeping every other entry (other N3MC products' hooks included, §14). **If it reports that the plugin is enabled, stop and let the user choose (L2, 2026-11-20)**: the plugin runs the PyPI code through `uvx`, and the generated code has another `code_version` (§3.15), so the plugin's hooks and this installation's CLI and MCP entry would stop and restart each other's server on every call. Show: `The n3mc-workingmemory plugin is enabled. Choose one: (a) keep the plugin and remove this generated installation (pip uninstall n3memorycore-mcp-lite), or (b) disable the plugin (/plugin disable n3mc-workingmemory@neuralnexusnote), then run this setup step again.` Wait for the answer; on (b), run step 3 again after the user disabled the plugin.
4. **Claude Desktop (decision D11)**: show the user the pip entry of "Claude Desktop" above with the absolute path from step 1 filled in, and where to add it (Settings → Developer → Edit Config, then quit the app completely and start it again). **Do not edit `claude_desktop_config.json`** yourself: it belongs to another application, the Microsoft Store version keeps it in another folder, and the app may rewrite it while it runs.
5. **Verify before telling the user to restart.**
   - The hook gate of §10 item 7.1 prints `OK`.
   - `<absolute path of n3mc-lite> --search "setup check"` prints a block that starts with `# Recalled Memory Context` (this starts the resident server; the first start may take minutes while the model downloads — a "still starting" placeholder is acceptable, then repeat until a real result or `_No memories found._` appears).
   - Start `n3mc-workingmemory` once yourself, send an MCP `initialize` followed by `tools/list` over stdio, check that the seven tools of §4.3 are listed (the answer comes within 2 seconds, §4.6), then close its stdin.
   - If `claude` is available, `claude mcp get n3mc-workingmemory` must report that no such server exists.
   - If any check fails, fix it and repeat; do not hand over a setup that does not work.
6. **Finish with one message**: "Setup is complete. Fully exit Claude Code (and Claude Desktop) and open it again — memory starts with the next session." Add the Windows full-exit steps from Appendix B ("Fully restart Claude Code between Phase 1 and Phase 2").

> **⚠️ Why (2026-11-16)**: a code-generation setup on a second PC ended with a package installed but no working registration: the folder's `.mcp.json` pointed at `C:\Users\<other PC user>\…\Python312\Scripts\n3mc-workingmemory.exe`, which did not exist there. Up to that version, Method A said only "register the server (§8)", and §8 showed the bare command name, which depends on `PATH`. Code generation is the primary install path for Lite, so it must end in a working setup without manual configuration. (v2.0.0: the registration is now the hooks; the absolute-path rule is kept for the hook commands and the desktop entry.)

### Auto-approve (Claude Code only)

**(v2.0.0)** Claude Code no longer has MCP tools for this server. The hooks are run by Claude Code itself and need no permission; what needs approval is the CLI commands Claude runs when it searches on its own (§13). Without a rule, every `--search` asks the user.

**pip (`PERMISSION_RULES`, written by `n3mc-lite --init`)** — added to `permissions.allow` in `~/.claude/settings.json` (only the missing ones; nothing is removed):

```json
{
  "permissions": {
    "allow": [
      "Bash(python -m n3mc_mcp.cli --search *)",
      "Bash(python -m n3mc_mcp.cli --buffer *)",
      "Bash(python -m n3mc_mcp.cli --list*)",
      "Bash(python -m n3mc_mcp.cli --recall-thread *)",
      "Bash(python -m n3mc_mcp.cli --repair*)",
      "Bash(python -m n3mc_mcp.cli --stop*)",
      "Bash(n3mc-lite --search *)",
      "Bash(n3mc-lite --buffer *)",
      "Bash(n3mc-lite --list*)",
      "Bash(n3mc-lite --recall-thread *)",
      "Bash(n3mc-lite --repair*)",
      "Bash(n3mc-lite --stop*)"
    ]
  }
}
```

**Plugin (`hooks/install_permissions.py`, SessionStart)** — the same six commands in the `uvx` form, `Bash(uvx --from n3memorycore-mcp-lite==2.0.1 n3mc-lite --search *)` … `Bash(uvx --from n3memorycore-mcp-lite==2.0.1 n3mc-lite --stop*)`. The v1.12.2 rules `mcp__plugin_n3mc-workingmemory_n3mc-workingmemory__*` and `mcp__n3mc-workingmemory__*` are no longer added; existing ones are left alone (they match nothing now).

- **`--delete` and `--delete-session` are never pre-approved**: they are irreversible (§4.3), so Claude Code asks the user each time — the same confirmation the tools' descriptions ask the model to get.
- The `<python> -m n3mc_mcp.cli` form is the one the rules file gives Claude (Free §4 ②: the bash subshell Claude Code spawns does not reliably have the scripts folder on `PATH`). **`--init` writes the rules above with `<python>` = `shell_path(sys.executable)`** (L11, 2026-11-20), the same absolute path as in the rules file, so they match what Claude runs also when the package is in a venv; the JSON above shows the form for a `python` on `PATH`. The `n3mc-lite` form is for interactive use.

The plugin hook keeps the v1.12.2 mechanics, with texts about the CLI commands instead of the MCP tools:

- **Writing**: re-read `settings.json` immediately before writing, merge only the missing rules, write a temporary file in the same folder and `os.replace` it; never remove entries; leave unrelated fields untouched (v1.7.1, LL2).
- **When `settings.json` cannot be updated**: change nothing, print one stderr line and the `systemMessage` + `additionalContext` JSON, exit 0: `[n3mc-workingmemory] Could not update ~/.claude/settings.json (<ErrorType>: …), so the memory commands were not pre-approved and Claude Code will ask for permission when Claude searches the memory. Make the file writable, or add "Bash(uvx --from n3memorycore-mcp-lite==2.0.1 n3mc-lite --search *)" to permissions.allow yourself.` Nothing is reported when the rules are already present.
- **Python fallback and visible failure**: the `hooks.json` command tries `python` → `py` → `python3`; if none works (or the script exits non-zero), a Python-free step prints one JSON object and exits 0:

```json
{"systemMessage": "[n3mc-workingmemory] Python was not found on PATH (tried python, py, python3), or the permission script failed, so the memory commands may not be pre-approved and Claude Code may ask for permission when Claude searches the memory. Install Python 3, or add \"Bash(uvx --from n3memorycore-mcp-lite==2.0.1 n3mc-lite --search *)\" to permissions.allow in ~/.claude/settings.json.", "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "<the same text>"}}
```

- **`uvx` pre-flight**: `shutil.which("uvx")`; if missing, the same kind of JSON with this bilingual install hint (L14, written down 2026-11-20 from v1.12.2 and adapted to the hooks): `[n3mc-workingmemory] uvx was not found on PATH. The plugin's memory hooks run through "uvx --from n3memorycore-mcp-lite==<version> n3mc-lite", so they cannot run until uv is installed. Install (Windows): pipx install uv or powershell -c "irm https://astral.sh/uv/install.ps1 | iex"; (Linux): curl -LsSf https://astral.sh/uv/install.sh | sh. Docs: https://docs.astral.sh/uv/ / [n3mc-workingmemory] uvx が PATH 上に見つかりません。uv を入れるまで、記憶のフックは動きません。上のいずれかのコマンドで uv を入れてください。` Combined into one JSON object with any other message; written with `json.dumps(..., ensure_ascii=True)` (L-03).
- **(v2.0.0) Double registration check**: if `~/.claude/settings.json` also holds hooks of a pip installation, add to the same JSON: `[n3mc-workingmemory] The N3MC Lite hooks are registered twice: by this plugin and by "n3mc-lite --init" in ~/.claude/settings.json. Every message is then searched twice. Remove the two n3mc-lite-hook / n3mc-lite-stop-hook entries from ~/.claude/settings.json, or uninstall the plugin.` (§14). The script never removes hooks itself. **How it recognizes them (L14, 2026-11-20)**: the script runs with a Python that may not have the package, so it carries its own copy of rules (a) and (b) of `is_own_hook` (§12.8) — the executable name `n3mc-lite-hook` / `n3mc-lite-stop-hook` (with or without `.exe`, any folder) or `-m n3mc_mcp.lite_hook` / `-m n3mc_mcp.lite_stop_hook` — and not rule (c), which needs the package folder.

> Claude Desktop has no per-tool permission gate, so none of this is needed there. Claude Code does not (as of 2026-04) accept a `permissions` field in `plugin.json`, so the plugin ships a `SessionStart` hook that patches the user's `settings.json`.

---

## 9. Testing (pytest)

> **Purpose**: Repeatable automated regression tests that complement the manual Evidence Report in §10. Layers are split by responsibility; the MCP tool E2E is the last line of defence.

### How to run

```bash
# 1. Start a DEDICATED Redis Stack for tests (never the one that holds your memories).
#    RediSearch can only index DB 0, and the suite FLUSHDBs it.
#    First time: docker run -d --name redis-stack-test --restart=no -p 127.0.0.1:6390:6379 redis/redis-stack-server:latest
#    Subsequent: docker start redis-stack-test
docker start redis-stack-test 2>/dev/null || docker run -d --name redis-stack-test --restart=no -p 127.0.0.1:6390:6379 redis/redis-stack-server:latest

# 2. Point the tests at it explicitly and run pytest
pip install -e ".[dev]"
N3MC_REDIS_TEST_URL=redis://localhost:6390/0 pytest tests/ -q
```

Windows PowerShell (L25; development is done on Windows 11):

```powershell
docker start redis-stack-test 2>$null; if ($LASTEXITCODE -ne 0) { docker run -d --name redis-stack-test --restart=no -p 127.0.0.1:6390:6379 redis/redis-stack-server:latest }
pip install -e ".[dev]"
$env:N3MC_REDIS_TEST_URL = "redis://localhost:6390/0"; pytest tests/ -q
```

**There is no default test URL (v1.7.0).** Tests that need Redis are skipped unless `N3MC_REDIS_TEST_URL` is set, and the suite refuses to run (skips with a reason) when that URL equals the `redis_url` in the user's real `config.json` or `N3MC_REDIS_URL`. The real `config.json` is read with `encoding="utf-8-sig"`, like the server reads it (a BOM must not hide its `redis_url`).

**Second guard: the DB itself (v1.7.1, Lite review LL6)**: comparing URLs misses aliases of the same server (the PC's host name, its LAN address, `127.0.0.2`). Before **every** `FLUSHDB`, the fixtures check the DB: flush only if `DBSIZE` is 0 or the marker key `__n3mc_test_db__` exists; right after flushing, `SET __n3mc_test_db__ 1`. A DB with data and no marker is never flushed — the Redis tests skip with the reason `N3MC_REDIS_TEST_URL points at a Redis that holds data and is not marked as a test DB`. To reuse a test container that holds leftovers from an older suite, flush it yourself once. (Because the marker is not an N3MC key, `enforce_ephemeral()` treats a marked test DB as shared and leaves its persistence settings alone — the safe side; `TestEnforceEphemeral` uses its own empty / N3MC-only states.) Every test file uses the same `REDIS_TEST_URL` from `conftest.py`; no test may hard-code a Redis URL. v1.6.x defaulted to `redis://localhost:6379/0` — the same Redis the MCP server uses — so a plain `pytest` wiped the user's working memory.

Tests auto-skip (not fail) if `N3MC_REDIS_TEST_URL` is unset or Redis Stack is not reachable there. Tests that do not need Redis (ranking, shaping, config) run without it.

**Protect real data (v2.0.0, mandatory)**: an autouse fixture in `conftest.py`, for every test:

- points `N3MC_DATA_DIR` at a new temporary folder and every path of `paths.py` derived from it (`config.json`, the PID file, `server.log`, the start-failure marker, `notices.json`, `audit.log`, `turn_id_*.txt`) — also the module-level copies in `cli.py`, `lite_hook.py`, `lite_stop_hook.py` and `server.py` — so no test reads or writes `~/.n3mc-workingmemory`;
- writes into that temporary `config.json` the test Redis URL and a **`server_port` of its own**: a free port taken by binding a socket to `127.0.0.1:0` once per test session. Tests never use 18524 (the real Lite server), 18520 (Free) or 18521 (Pro);
- sets `HOME` and `USERPROFILE` to a temporary folder, so `--init`, the plugin's permission script and the `~/.claude.json` check read and write temporary files; **the real `~/.claude/settings.json` is never read or written**;
- replaces `_request_shutdown` and the HTTP senders (`_post`, `_get`) with fakes unless the test starts its own server on its own port (the HTTP layer tests use FastAPI's `TestClient` with `Host: 127.0.0.1:<port>`);
- stops every server a test started (by `/shutdown` to its own port) before the test ends.

Use `monkeypatch.setattr` / `monkeypatch.setenv` and let pytest restore them at the end of the test. **Never call `monkeypatch.undo()`** inside a test: it restores the real paths in the middle of the test, and later writes go to the real data folder.

> **⚠️ Destructive test DB**: RediSearch can only create indexes on DB 0 (`Cannot create index on db != 0`), so the test suite FLUSHDBs DB 0 before and after every test. Do **not** point `N3MC_REDIS_TEST_URL` at a Redis instance that holds data you care about — run a dedicated container for testing.

> **⚠️ Do not run pytest during the Evidence Report (§10)**: §10 saves and searches real memories against a live MCP server (the same DB 0). Running `pytest` in the middle of it means **`FLUSHDB` wipes the §10 data along with everything else**, so verification suddenly turns up "no results" and gets confusing. While running §10, stop pytest or point the tests at a separate (dedicated) Redis on another container/host.

### Directory layout

```
n3mcmcp-lite/
└── tests/
    ├── conftest.py          # Shared fixtures: isolated data dir, Redis URL override, dummy vectors
    ├── test_safe_path.py    # Safe import path: files in the current folder do not run inside N3MC (no model needed)
    ├── test_database.py     # Layer 1: Redis / RediSearch unit tests (CRUD, schema, TTL, dedup, BM25, KNN)
    ├── test_processor.py    # Layer 2: ranking math, embeddings, purify, chunking, reranker
    ├── test_server.py       # Layer 3: MCP tool dispatch E2E (isolated `config.json` + flushed DB 0)
    ├── test_regressions.py  # Layer 4: v1.7.0 fixes (query cleaning, exact-only dedup, pre-filter, shaping, config)
    ├── test_hooks.py        # Layer 5 (v2.0.0): hook flow, role prefixes, direct save
    ├── test_api.py          # Layer 6 (v2.0.0): resident server endpoints and process management
    ├── test_cli.py          # Layer 7 (v2.0.0): CLI commands
    ├── test_mcp_client.py   # Layer 8 (v2.0.0): MCP thin client
    ├── test_context_limit.py # Layer 9 (v2.0.0): context size limit
    └── test_coexistence.py  # Layer 10 (v2.0.0): --init, plugin files, coexistence with Free / Pro
```

**(v2.0.0) Layer 3** keeps its tests; the tool handlers are now in `tools.py` (called as `/tools/<name>` handlers inside the resident server), and the assertions on the texts are unchanged.

**Internal shape the kept `tests/test_cross_session.py` relies on (2026-11-22, a gap filled in):** that file is not generated from this specification, but it calls these names, so the generated code and `conftest.py` must provide them. `conftest.py`: a fixture `rconn` (the flushed test Redis connection, with the deterministic fake embeddings already applied) next to `cfg`. `Database(cfg, default_session=<str>)`: the session used by `save_memory` and `search` when none is passed; `Database.ensure_connected()` returns `"ok"` on success; `Database.client` is the redis-py client (it has `close()`); `Database.search(query)` returns a 3-tuple `(ordinary rows, weak rows, message)` whose rows are dicts with `content`, `score` and `session_id`, and `_doc` true for a parent-document hit. `tools.ToolRunner(cfg, db).call(name, args, nudge=False)` runs a tool handler on that `Database` and returns its text.

### Layer 1: `tests/test_database.py` (34 tests)

| Test class | What it covers | Area |
|---|---|---|
| `TestIndexSetup` | RediSearch index creation, idempotent re-run, adding missing fields | Schema management |
| `TestInsertAndRetrieve` | INSERT→COUNT, hash integrity, insertion without embedding, `parent_id` storage | CRUD |
| `TestDedup` | Exact SHA collisions, parent-doc SHA guard (no similarity rejection since v1.7.0) | De-duplication |
| `TestDelete` | Single delete, parent→chunk cascade, `docsha:` guard cleanup; (2026-11-22) after a long text and a standalone record equal to its first chunk are saved, deleting the chunk leaves the standalone record's guard, so saving that text again is `duplicate`; after the standalone record itself is deleted, saving it again is `saved` | Transactional safety |
| `TestTTL` | 7-day TTL set on save, **a search hit does not change the TTL** (2026-11-22), expiry-driven disappearance | TTL |
| `TestFTS` | Punctuation stripping, BM25 scores, short queries, CJK-bigram hits | FTS / Japanese |
| `TestVectorSearch` | KNN results, empty-DB search, `owner_id` filter, standalone records only (empty `parent_id`, filtered in Python — the `-@parent_id:{*}` TAG filter is not used, §3.12) | KNN search |
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
| `TestChunkText` | Below-threshold→single, long→split+overlap, boundary alignment; a tail shorter than 20 characters is appended to the previous chunk (every character of the body is in a chunk) | Parent-chunk |
| `TestEmbedding` | `passage:` / `query:` prefixes, vector dimensionality, same-text similarity | Embedding |
| `TestEncodingSafety` | Lone-surrogate strip on `str` / `list` / `dict` / `None`, all-surrogate input collapses to empty, post-strip `.encode("utf-8")` succeeds | Encoding safety (§3.13) |

### Layer 3: `tests/test_server.py` (19 tests)

| Test class | What it covers | Area |
|---|---|---|
| `TestToolRegistration` | Seven tools registered (incl. `recall_thread`), schema types, description non-empty | MCP registration |
| `TestSaveAndSearch` | Save→search round-trip, exact-duplicate rejection, empty-content rejection, code-block purification (§10.3) | Single chunk |
| `TestListAndDelete` | Three recent entries listed, delete non-existent id | List / delete |
| `TestRepair` | `repair_memory` on empty DB → ok | Repair |
| `TestUnknownTool` | Unknown tool name → error string | Dispatch |
| `TestParentDocRecall` | Verbatim recall of fictional "Shiranui" city doc, chunk→parent collapse, `[doc×N]` rendering, parent→chunk cascade delete | Parent document (§3.11) |
| `TestEncodingSafetyE2E` | `save_memory` with surrogate-laced content does not raise, surrogate-only payload returns empty-content error, `search_memory` with surrogate-laced query returns cleanly | Encoding safety end-to-end (§3.13) |

### Layer 4: `tests/test_regressions.py` (v1.7.0 – v1.7.3)

Embeddings are deterministic fakes (no model). Classes marked *Redis* need `N3MC_REDIS_TEST_URL`.

| Test class | What it covers |
|---|---|
| `TestQueryCleaning` | Control characters and CJK / full-width punctuation are stripped; `「記憶装置」は？` hits a record containing 記憶装置 (*Redis*); no query in a symbol / control-character list makes `FT.SEARCH` raise (*Redis*) |
| `TestExactOnlyDedup` | A near-identical short fact with a new value is saved; a re-made long document with a few changes is saved; exact duplicates are still rejected (*Redis*) |
| `TestTimeWindow` | `since` / `until` return in-range rows even when more than `limit × 5` newer rows are closer to the query (*Redis*) |
| `TestShaping` | `vector_min_cos` drops low-cosine vector candidates; relative cut; the newest version moves to the top and inherits the slot score; `_version_group` rows survive the relative cut; short rows (< `version_min_chars`) are not grouped |
| `TestRender` | `saved=` in local time, the version sentence, each notice printed once; **v1.7.1**: a notice added after the first search is printed on the next search; **v1.7.2**: weak rows appear under `#### Possibly related (weak matches — may be unrelated)` after every normal row; with no normal rows, `_No memories found._` is followed by the weak-matches heading and no version sentence; no weak heading when there are no weak rows |
| `TestConfig` | An unreadable `config.json` is not modified; a wrongly typed item is ignored and reported; bool is not accepted as a number |
| `TestTestSafety` | `conftest` refuses a test URL equal to the live `redis_url`. **v1.7.1**: the live `config.json` is read with a BOM; a DB with data and no marker is not flushed |
| `TestFilterScore` (v1.7.1) | With a fresh current-session record at cos 0.86 on top, a 3-day-old same-session record and 1-day-old other-session records at cos 0.95–0.99 survive the relative cut and `min_score`; the order is still by the final score |
| `TestRedisTimeouts` (v1.7.1, *Redis*) | The client is built with `socket_timeout=5`; a server socket that accepts and never answers makes `ensure_connected()` return within ~5 s with the backend error; the old client is closed on reconnect |
| `TestConfigCreate` (v1.7.1) | Several processes creating `config.json` at once end with one `owner_id` (every process uses the file's ID); (2026-11-22) two processes started together on an existing `config.json` without IDs both end with the `owner_id` / `local_id` that is on disk, the user's own keys are kept, and records saved by both are listed after a restart; updates go through a temporary file and `os.replace`; the legacy migration uses the exclusive-create rule (temporary file + `os.link`) |
| `TestConfigRanges` (v1.7.1) | `ttl_seconds` `604800.5` / `0`, `embedding_dim` `768.0`, `chunk_overlap >= chunk_threshold`, `chunk_threshold` 10, `half_life_days` 0, `min_score` 1.5 are ignored with `config_type` (**v1.7.2**: so are `min_results` `-1` and `1.5`, while `0` is accepted); `chunk_text(text, 100, 100)` on a body without breaks terminates |
| `TestLongSaveAtomic` (v1.7.1, *Redis*) | An embedding failure in the middle of a long save leaves no `doc:` / `docsha:` key, and the same body can then be saved; the guard, the parent and all chunks are written by one `SAVE_WITH_GUARD` script |
| `TestReview2Fixes` (second review of 2026-11-20, *Redis*) | A save interrupted when the script is sent (short and long) leaves no guard, the retry is `saved` (one record, or the parent and every chunk) and a third save is `duplicate`; the guard, the parent and the chunks carry the TTL; after a failure only a guard holding this save's id is deleted. An MCP entry that stays open sends its second call to a changed `server_port`; an unreadable `config.json` keeps the last readable settings (same `owner_id`) |
| `TestIndexFailure` (v1.7.1) | `FT.CREATE` failing with `unknown command` does not end `_main()`; tools return the "Redis Stack required" hint; the index is retried on the next call; an existing index with another `DIM` adds `index_dim_mismatch` |
| `TestEnforceEphemeral` (v1.7.1, *Redis*) | An empty DB or one with only `mem:` / `doc:` / `docsha:` keys gets `CONFIG SET`; a DB with another key does not, and `shared_redis` is added; keys in another DB of the server (v1.8.1) or a refused `INFO` also leave the configuration unchanged with `shared_redis`; a refused `CONFIG` adds `config_refused` without failing the connection. **No leftovers in another DB (2026-11-22)**: the key that the other-DB test puts into DB 1 is set with a 60-second expiry (`EX 60`) and deleted in a `finally` block, and the empty-DB test deletes that key in DB 1 before it connects. **Why**: a test Redis with the image's default save rules writes a snapshot while the key exists; after a restart it loads the key again, the empty-DB test sees keys in another DB and fails with `shared_redis` on the first run (the CI starts a fresh Redis each time and never saw it) |
| `TestCodeOmittedOnSave` (v1.9.0, *Redis*) | A short body and a parent document are stored with `[code omitted]` in place of each fenced block (raw `HGET`), inline backticks kept; an unclosed fence is replaced to the end; two bodies that differ only inside a code block are exact duplicates; `skip_code_blocks: true` still rejects the body; a record stored with a raw fence (as by 1.11.x) is still shown as `[code omitted]` |
| `TestSurroundingWhitespace` (v1.8.1, *Redis*) | The outer `strip()`: `' \nAAA\n '` is stored as `AAA`; a parent document keeps its interior newlines and loses only the outer whitespace; a body that differs only in surrounding whitespace is an exact duplicate; whitespace-only content is rejected with `empty content` |
| `TestModelWait` (v1.7.1) | While the load holds the lock longer than `MODEL_WAIT_SECONDS` (patched short), `search_memory` answers keyword-only with the "still loading" notice and `save_memory` saves without a vector with `detail` |
| `TestListPaging` (v1.7.1, *Redis*) | After a standalone memory and a 260-chunk document, `list_memories(20)` lists both |
| `TestNoTtlRefresh` (2026-11-22, *Redis*) | A search hit changes the TTL of no `mem:` / `doc:` / `mem:sha:` / `docsha:` key: a standalone record, a parent document, its chunks and their guards keep the TTL of the save |
| `TestOrBm25` (v1.7.1, *Redis*) | `quick fox elephant` and `what did the quick fox do` hit a record containing `quick fox`; `記憶装置 大阪` hits a record containing 記憶装置; rerank coverage counts `note,` as `note` |
| `TestTimeBoundsStrict` (v1.7.1) | `since="yesterday"` / `"2026/11/13"` return the `Error:` text; `since > until` is an error; a date-only value is local midnight (fixed non-UTC zone) |
| `TestRepairReembed` (v1.7.1, *Redis*) | Entries saved without `embedding` get one from `repair_memory` (at most 200 per call, TTL unchanged, `reembedded` / `remaining` reported) |
| `TestDeleteOwner` (v1.7.1, *Redis*) | `delete_memory` on another owner's id returns `not_found` and deletes nothing; a parent's chunks are deleted from `chunk_ids` |
| `TestGuardNx` (v1.7.1, *Redis*) | Two simultaneous saves of the same content store one record; a failed write after taking the guard releases it |
| `TestAccessCountNoGhost` (v1.7.1, *Redis*) | A record that expires or is deleted between the search and the `access_count` increment is not recreated by `HINCRBY`: no key is left at all (no `access_count`-only hash, no `mem:sha:` / `docsha:` guard) |
| `TestRecallOrphanChunks` (v1.7.3, *Redis*) | With the parent alive, `recall_thread` lists the parent once (`[doc×N]`, full body) and no chunk; after `doc:<id>` is deleted, each chunk of that turn is listed with its chunk text instead of `not_found` |
| `TestDeleteIdPrefix` (v1.7.3, *Redis*) | `delete_memory` with `doc:<uuid>` cascades like the bare parent id; `mem:<uuid>` deletes a standalone record and its guard; the response `id` is the bare UUID; `doc:doc:<uuid>` (two prefixes) is `not_found` |
| `TestNudgeOnErrors` (v1.7.3) | The non-numeric `importance` JSON, `Error:` for an unreadable `since`, an unexpected exception and the backend error of `search_memory` each start with their error text and end with the reminder |
| `TestConditionalWrites` (v1.7.3, *Redis*) | `INCR_IF_EXISTS` and `HSET_IF_EXISTS` write and keep the TTL when the record exists, and create nothing when it does not; `repair_memory` on a record that disappears after the `SCAN` creates no key and does not count it in `reembedded`; after save, search and repair every key in the test DB (except the test marker) has a TTL |
| `TestHangulFiller` (v1.7.1) | `U+3164`, `U+115F`, `U+1160`, `U+FFA0`, `U+2800` alone are rejected as no visible characters; with a visible character they are stored verbatim |
| `TestPluginHook` (additions, v1.7.1) | `settings.json` is re-read just before writing and replaced atomically; the fallback text names "or the permission script failed"; with `uvx` missing, the hint text of §8 appears in full (L14) |
| `TestWeakMatches` (v1.7.2, *Redis*) | With one row passing the floors and `min_results: 3`, two weak rows are added after it, ordered by `filter_score` and marked `_weak`; a candidate below `vector_min_cos` is added with its actual cos; `min_results: 0` adds nothing; nothing is added when enough rows pass; a `limit` smaller than `min_results` caps the total; two chunk hits of one parent document give one weak row, and a parent already in the results is not added again; a candidate with `filter_score` below `1e-6` is never added; **a weak row's `access_count` does not change** while a normal row's is incremented; an empty or whitespace-only query adds no weak rows; the default config has `min_results: 3` |
| `TestGuardReleasedOnAnyFailure` (final review of 2026-11-18, L-01, *Redis*) | For a short body and a long body: a save with `turn_id: 12` fails and leaves no `mem:sha:` / `docsha:` key, and the same body then saves; lone surrogates in `agent_name` / `session_id` / `turn_id` are stripped and the record is saved; fields without surrogates are stored unchanged |
| `TestPluginHookAsciiStdout` (final review of 2026-11-18, L-03) | With `uvx` missing, the hook's stdout is one ASCII-only line whose `systemMessage` still contains the Japanese hint after `json.loads` |
| `TestPublicRepoHygiene` (final review of 2026-11-18, L-05) | `tests/eval_uvx_missing.py` contains no absolute user path (the folders to strip are the `PATH` entries that hold `uvx`); `metadata.version` of `.claude-plugin/marketplace.json` equals the package version |

(v2.0.0) `TestPluginHook` and `TestPluginHookAsciiStdout` now check the CLI permission rules (`Bash(uvx --from n3memorycore-mcp-lite==2.0.1 n3mc-lite --search *)` …) instead of the MCP rules, and that `--delete` / `--delete-session` are not among them.

### Layer 5: `tests/test_hooks.py` (v2.0.0)

Fake embeddings (no model). Classes marked *Redis* need `N3MC_REDIS_TEST_URL`. The server is either a `TestClient` of `app.py` behind the faked `_post`, or absent.

| Test class | What it covers |
|---|---|
| `TestAuditFirst` | Both hook entries append `{"ts", "hook", "raw", "payload"}` to `audit.log` before anything else, also when the server and Redis are down and when stdin is not JSON (`payload` = `{"raw": …}`); `raw` is cut to 4,096 characters; a failing `audit.log` write does not stop the hook |
| `TestTurnIdHandoff` (*Redis*) | `--hook-submit` makes a new `turn_id`, writes `turn_id_<session>.txt` (name sanitized, 64 characters) and saves `[user]` with it; `--save-claude-turn` reads it and saves `[claude]` with the same `turn_id`; the file is not cleared; without a file a new UUID is used; an image-only prompt still writes a new `turn_id` and saves no `[user]` |
| `TestTranscriptRecovery` (*Redis*) | Free's transcript cases (last text block only; `tool_result`-only, `isMeta`, interruption marker, slash-command, `thinking`, `tool_use`, `isSidechain` lines skipped; the trailing current prompt skipped; an interrupted reply found) save the previous reply under the previous `turn_id`; when Stop already saved it the exact-duplicate rule stores nothing new; a missing or broken transcript logs `transcript_unreadable` and the hook still searches and saves |
| `TestSystemPrefixes` | `<task-notification>`, `<agent-message from=`, `<system-reminder>` prompts are neither saved nor searched, keep the `turn_id`, write `stage: skipped` and print `# Recalled Memory Context\nSource: N3MC Lite (7-day working memory)\n\n_No memories found._`; a prompt starting with `<div>` is not skipped |
| `TestRolePrefix` (*Redis*) | A short message is stored as `[user] <text>` with `agent_name` `claude-code` and the hook's `session_id`; a body of 1,000 characters gives a parent `[claude] <body>` and chunks `[claude 1/N] …`; a body up to 19 characters over `chunk_threshold` (one chunk) gives a parent and a chunk `[claude] …`; the length decision ignores the prefix; `[code omitted]` replaces fences before the prefix; `mem:sha:<turn_id>:` / `docsha:<turn_id>:` hash the prefixed text within the exchange, so `ok` saved by MCP and `[user] ok` by the hook are both stored, the same `[user] ok` in a later exchange is stored again, and the same reply saved twice under one `turn_id` (Stop hook and transcript recovery) is `duplicate`; with `skip_code_blocks: true` a message with a fence is `skipped_code` and stays in `audit.log` |
| `TestDirectSave` (*Redis*) | With no server (and with another folder's server on the port, and with the server still starting) the hook saves directly: same records and prefixes, no `embedding` field, TTL set; `sentence_transformers` is not imported (`sys.modules` checked); a later `/repair` adds the vectors; nothing is queued |
| `TestRedisDown` | With Redis unreachable (server up → `/buffer` 503; server down → direct save fails) the hook exits 0, `audit.log` holds the text, the notice `redis_unreachable` contains the `docker start redis-stack` hint and appears in the next search output; it is cleared after the next successful save |
| `TestHookSearchOutput` | The hook prints `# Recalled Memory Context`, `Source: N3MC Lite (7-day working memory)` (also in the empty block and every placeholder), `Search query: …` and Lite's rows (`### [<id>] turn=… score=… saved=…`); `_No memories found._` when empty; `_(memory search unavailable: <reason>)_` with no server; due notices right after the heading |
| `TestHookExitStatus` | `n3mc-lite-hook` and `n3mc-lite-stop-hook` exit 0 on every path above |

### Layer 6: `tests/test_api.py` (v2.0.0)

`TestClient` of `app.py` with fake embeddings; temporary data folder; *Redis* where marked.

| Test class | What it covers |
|---|---|
| `TestHealth` | `/health` returns `status`, `root` (the temporary data folder), a 16-hex `code_version` and `pid`; `code_version` changes when `config.json`'s content changes and not when only whitespace or key order changes |
| `TestHostHeader` | `Host: evil.example` gets 403 on every endpoint; `127.0.0.1:<port>` and `localhost:<port>` pass; (D8) `Origin: http://evil.example` and `Origin: null` get 403 on every endpoint even with `Host: 127.0.0.1:<port>`, the same request without `Origin` passes, and the requests of the CLI and the thin client carry no `Origin` |
| `TestPlainDef` | `/buffer`, `/search`, `/repair` and `/tools/{name}` are plain `def` functions |
| `TestReviewFixes` (2026-11-20) | M1: a verified PID-file server listening on another port whose `/health` reports this folder gets `/shutdown` on that port and a new server starts; another folder's server there is left alone. M2: `run_server` ending with `SystemExit(1)` or an exception writes the marker and `server_start_failed`, a clean exit writes nothing, and the next `ensure_server(max_wait=0)` does not start again. M3: with the model lock held, `no_model_wait` returns at once while a normal call waits; `/buffer` and `/search` with `no_model_wait` answer within about a second and save without `embedding`; the hook's requests carry the field and `--search`'s do not. L4: a later save clears `save_failed`. L5: a second `repair` while one runs answers "repair already running". L6: `--search` gets the sanitized text. L7: the Stop entry's second step gets what is left of `CHILD_TIMEOUT` |
| `TestBufferEndpoint` (*Redis*) | `role` user / claude store the prefixes of §3.16; the answer is the `save_memory` dict; Redis down → 503 with the hint |
| `TestSearchEndpoint` (*Redis*) | `{"text"}` starts with `# Recalled Memory Context`; `max_chars` limits it, its absence does not; an unreadable `since` → 400 with the `Error:` text; Redis down → 503 |
| `TestToolsEndpoint` (*Redis*) | Each of the seven tools returns the same text as the v1.12.2 handler (Layer 3 assertions) with `nudge=1` and the same text without the reminder with `nudge=0`; an unknown name → `Error: unknown tool '<name>'`; a body that is not an object is read as `{}`; `search_memory` follows `context_max_chars` |
| `TestShutdown` | `/shutdown` with another `root` → 403; with its own `root` → `ok` (exit replaced by a fake) |
| `TestConcurrency` (*Redis*) | Saves and searches from 8 threads at once all succeed; two saves of the same text store one record |

### Layer 7: `tests/test_cli.py` (v2.0.0)

| Test class | What it covers |
|---|---|
| `TestServerDecision` | Healthy / other `code_version` / other `root` / no `root` and unverifiable; `/shutdown` first; never stops a process by the PID-file number; does not wait on a dead PID file |
| `TestServerStart` | Detach flags per platform, stderr to `server.log`, `-m n3mc_mcp.cli --run-server`; a child that exits early ends the wait, emits `server_start_failed` and writes the marker; no new start within 5 minutes; a later success clears both; a PID-file process still starting after 60 s is neither replaced nor duplicated (process creation faked) |
| `TestNoProxy` | With `HTTP_PROXY` pointing at another local server, `_get` reaches the N3MC Lite server and nothing reaches the proxy |
| `TestCommands` | `--search` → `/search` without `max_chars`, `--search --since/--until` passed through; an unreadable `--since` prints the `Error:` text to stderr and exits 2; `--buffer` → `/tools/save_memory?nudge=0`; `--list` → `list_memories`; `--recall-thread` → `recall_thread` with `--before` / `--after`; `--delete` → `delete_memory`; `--delete-session` → `delete_memories_by_session`; `--repair` → `repair_memory`; each prints the answer's text unchanged; (2026-11-22, L3) `--until 9999-12-31` and `--since 1969-12-31` print one `Error:` line to stderr and exit 2, and `search_memory` / `/search` give the `Error:` text of §4.3.1 (400 for `/search`) |
| `TestReview20261122` | L1: `_write_own_pid` overwrites a PID file holding another number and creates a missing one; `run_server` calls it twice, the last time before listening; `_handle_other_port` with a PID-file server listening on another port whose `/health` reports this folder sends `/shutdown` to that port. L2: after `redis_url_mismatch` was emitted, an answer of our own server whose `redis` matches clears it. L4: `run_server` computes `code_version` before connecting to Redis and loading the model, and passes that value to `create_app(..., code_version=...)` |
| `TestForeignPort` | With another folder's server on the port: searches print the `port_conflict` placeholder, `--list` / `--delete` print the backend error, hook saves go in directly |
| `TestStop` | `--stop` writes the rules file (§12), removes `turn_id_*.txt` older than 30 days, rotates `audit.log` over 50 MB (threshold patched), prints nothing and does not stop the server. **2026-11-22**: with the hook's `cwd` (or, without one, the working folder) = the home folder, `--stop` creates nothing in `.claude/rules/` (§12.4 step 1). **Project folder (2026-11-22)**: with `CLAUDE_PROJECT_DIR` naming another temporary folder, `--stop` creates the rules file there and nothing in the hook's `cwd`; without the variable it creates it in the hook's `cwd`; with the variable naming the home folder it creates nothing |

### Layer 8: `tests/test_mcp_client.py` (v2.0.0)

No model. The thin client's handlers are called as functions with the HTTP senders faked, plus one subprocess test.

| Test class | What it covers |
|---|---|
| `TestThinNames` | `n3mc-workingmemory` and `n3mc-mcp-lite` map to `n3mc_mcp.__main__:run`; `serverInfo.name` is `n3mc-workingmemory`; `pyproject.toml` declares version `2.0.1` and `mcp>=1.0.0,<2.0.0` |
| `TestThinTools` | `tools/list` gives the seven definitions of §5 (3) verbatim without a server; the instructions are the text of §5 (1) verbatim; the handler is registered with `validate_input=False` (or without it when refused) |
| `TestForwarding` | Each tool is sent as `POST /tools/<name>?nudge=1` with its arguments, and the answer's text is returned unchanged (byte for byte); `session_id` is filled for `search_memory` / `save_memory` only when missing (argument → `N3MC_SESSION_ID` → the process UUID, the same for every call); `agent_name` is filled (argument → `N3MC_AGENT_NAME` → `clientInfo.name` → empty); no result has `isError` set; `importance: "nan"` reaches the server and gets Lite's JSON (no SDK validation error) |
| `TestThinNoServer` | With no server / still starting / start failed / another folder's server: `save_memory` saves directly (no `embedding` field, `warning` and `detail` of §4.6); the other tools return the texts of §4.6; Redis down too → the backend error JSON; (2026-11-22) `delete_memory` / `delete_memories_by_session` and `--delete` / `--delete-session`: not sent → "Nothing was deleted", completed → the normal answer, answer lost after the delete (timeout patched in) → `"status": "unknown"` |
| `TestReview20261122b` | `redis_display` of a valid URL with user and password shows only host, port and db; of an unparseable URL (`redis://u:secret@host:abc/0`) gives `<unparseable redis_url>`, and neither `/health` nor the `redis_url_mismatch` notice (stderr, `notices.json`, `audit.log`) contains `secret`; `code_version` of the server equals the client's when nothing changed (also after a fill-in write) and is `stale` after `config.json` changes between `load_config()` and the start, then `ok` after the restart; `--stop-server`: stopped, none running, another folder's server, stop failed (exit 1) |
| `TestThinStart` | `ensure_server` runs once in a background thread at start (`initialize` is answered while it is blocked, patched) and before every tool call; the entry never calls `/shutdown` |
| `TestThinNoModel` | Importing and running the thin client does not import `sentence_transformers` or `torch` (`sys.modules`); nothing is written to stdout outside MCP messages (captured) |
| `TestThinSubprocess` (*Redis*) | `n3mc-workingmemory` started as a child with the temporary `N3MC_DATA_DIR`, and with `HF_HOME` set to the real Hugging Face cache folder (resolved before the fixture replaces `HOME`; skipped when the model is not in that cache, so the test never downloads ~1.1 GB — L16), and talked to with `mcp.client.stdio.stdio_client`: `initialize` within 2 s, seven tools, a `save_memory` then `search_memory` round trip through a real server on the test port; the server is still running after the client is closed, and the test stops it. The job of the client may also end the server (§3.15), so after the close the test stops the server if it still runs, and checks that the next CLI call starts it again if it is gone (either outcome passes; 2026-11-22) |

### Layer 9: `tests/test_context_limit.py` (v2.0.0)

No model, no Redis (rendering from prepared result rows).

| Test class | What it covers |
|---|---|
| `TestContextSize` | `context_size` counts UTF-16 code units (`😀` is 2) and each line break twice |
| `TestRenderLimit` | Without `max_chars` the output is unchanged (20 rows, over 10,000); an output that fits is identical to the unlimited one; over the limit the kept rows are the first rows in order, one more row would not fit, and the omission line with the right N follows the last kept normal row and comes before the weak heading; at 3 rows the text is cut and ends with the cut line; a 30,000-character parent document as the first hit is cut; notices and weak rows are kept by step 1; rows of Japanese and emoji stay within the limit; the MCP output keeps `_NUDGE_AFTER_SEARCH` at the end and counts it |
| `TestHookUsesLimit` | `--hook-submit` prints at most `context_max_chars` (default 9,500); a manual `--search` prints all rows |
| `TestConfigContextMaxChars` | Default 9500; `1999`, `10001`, `9500.0`, `"9500"` are ignored with `config_type`; `2000` and `10000` are accepted |

### Layer 10: `tests/test_coexistence.py` (v2.0.0)

Temporary `HOME` only.

| Test class | What it covers |
|---|---|
| `TestInit` | `--init` writes the two hooks with absolute forward-slash paths (quoted only with a space) and the `PERMISSION_RULES`; re-running writes nothing; a backup is made before a change; an invalid `settings.json` is left unchanged with exit status 1; our entry-point, `-m` and package-script forms are replaced, not duplicated (also written with backslashes) |
| `TestKeepsOtherProducts` | Free's `n3mc-hook` / `n3mc-stop-hook` entries, Pro's `python ".../n3mc_hook.py"` entry and any other hook are kept and reported; Free's own matcher — its rules, written down here (L14): the executable `n3mc-hook` / `n3mc-stop-hook` (with or without `.exe`), `-m n3memorycore.n3mc_hook` / `-m n3memorycore.n3mc_stop_hook`, or the script `n3mc_hook.py` / `n3mc_stop_hook.py` inside Free's package folder — does not match Lite's entries |
| `TestPluginEnabled` | With `enabledPlugins["n3mc-workingmemory@neuralnexusnote"]` true, `--init` registers no hooks, adds the permission rules and prints the message of §12; the plugin's permission script reports `--init` hooks found in `settings.json` |
| `TestNoMcpInClaudeCode` | `plugin.json` has no `mcpServers`; `hooks.json` has `SessionStart`, `UserPromptSubmit` and `Stop` with the commands of §8; `--init` never adds an MCP server to `~/.claude.json` or `.mcp.json`; an old `n3mc-workingmemory` entry there is reported, not edited |
| `TestSeparateNames` | Default port 18524, data folder `~/.n3mc-workingmemory`, rules file `n3mc-lite-behavior.md`, PID file `n3mc-lite.pid`, commands `n3mc-lite*` — none equal to Free's (`18520`, `~/.n3mc`, `n3mc-behavior.md`, `n3mc*`) or Pro's (`18521`) |
| `TestRulesFile` | The rules file is written from the template with the `python -m n3mc_mcp.cli` form, or the `uvx` form with `--uvx`; a file equal to the other form's template is replaced (with backup); a hand-edited file is not replaced and gives `rules_md_edited:<hash>`; the first line is `<!-- n3mc-lite-behavior v1 -->` |

### Safe import path: `test_safe_path.py` (2026-11-21; no model needed)

Every test that starts a process gives it a temporary data folder and a temporary home folder.

| Test | What it checks |
|---|---|
| `test_there_are_entry_modules` | The main module is one of the modules that end with `if __name__ == "__main__":`, and there are at least 3 of them |
| `test_entry_module_starts_with_the_safe_path_block` (once per entry module) | After the docstring, the first statements are `import os as _os`, `import sys as _sys` and the `if __name__ == "__main__":` block that deletes `sys.path[0]` |
| `test_files_in_the_current_folder_do_not_run` | In a folder that holds `argparse.py`, `shlex.py`, `queue.py`, `fastapi.py`, `pydantic.py`, `redis.py`, `numpy.py` and `uvicorn.py` (each writes a marker file and raises `ImportError`), `python -m n3mc_mcp.cli --help` prints the usage, exits 0 and leaves no marker |
| `test_without_the_block_they_would_run` | In the same folder, `python -c "import shlex"` writes the marker (so the test above is real) |
| `test_a_source_checkout_run_in_place_still_works` | From the repository root, without `PYTHONPATH`, `python -m n3mc_mcp.cli --help` exits 0 |
| `test_children_are_started_with_dash_P_outside_a_checkout` | `python_cmd()` is `[sys.executable, "-P"]` in another folder on Python 3.11 and later (`[sys.executable]` on 3.10), and `[sys.executable]` in the repository root |
| `test_no_child_is_started_without_python_cmd` | No module of the package contains `[sys.executable,` |

### Example runs

```bash
# Full suite
pytest tests/ -q

# Single layer
pytest tests/test_database.py -v

# Skip slow embedding tests
pytest tests/ -v -k "not TestEmbedding"
```

> **⚠️ The `slow` marker (optional)**: Heavy tests that load the embedding model can be excluded with `-k "not TestEmbedding"` as above. Alternatively, tag them with `@pytest.mark.slow` and register `slow` under `[tool.pytest.ini_options].markers` in `pyproject.toml`, then exclude them with `pytest -m "not slow"` (an unregistered marker raises `PytestUnknownMarkWarning`).

> **⚠️ Relationship to the Evidence Report**: A failing automated test does **not** block the §10 sign-off. The Evidence Report is the sole pass/fail gate for implementation completeness. The automated suite is an optional regression aid and must not trigger an endless fix-and-retry loop during the first build.

---

## 10. Self-Evaluation (Evidence Report)

> Once implementation is complete, the AI runs the following checks autonomously and reports each item at ⭐⭐⭐⭐⭐. This is not a post-generation one-shot scorecard: run the §9 pytest suite first and report its result. Per §9, a failing test does not block this sign-off (the Evidence Report is the pass/fail gate), but every failing test is named in the report. MCP Lite is judged on *best-in-class behavior given the Redis-Stack + 7-day-TTL constraint set*.

> **(v2.0.0) How to call the tools in items 1–6**: Claude Code has no MCP tools for this server any more. Run each tool through the CLI, which calls the same handler and prints the same text without the reminder (§13): `search_memory` → `python -m n3mc_mcp.cli --search` (it runs the same search of §3.14 and prints the rows inside the hook's heading); `save_memory` → `--buffer`; `list_memories` → `--list`; `delete_memories_by_session` → `--delete-session` (Claude Code asks for permission: approve it); `repair_memory` → `--repair`. Item 10 checks the MCP path itself.

1. **Latency & process health**: measure separately (L1, 2026-11-20): (a) the MCP entry's answer to `initialize` (recorded); (b) the time from the server start until `/health` answers (recorded; the library import takes about 10 s, §3.9); (c) the first search after the model has loaded (recorded); (d) the median of 10 later searches, as server-side processing (embedding + Redis KNN/BM25 + ranking, measured with a microbenchmark that calls the `processor` / `database` functions directly). **Pass: (d) ≤0.5 s on CPU.** (a)–(c) depend on the PC and are reported, not judged. Also confirm that calling any tool while Redis Stack is down does **not** crash the server — it must return the "start Redis Stack" hint. (v2.0.0: also confirm the PID file is created while the server runs, and that after `config.json` is edited the next call starts a new server.)

   > **⚠️ Interpreting the measurement**: "≤0.5 s steady state" is a target for **pure server-side processing** (embedding generation + Redis KNN/BM25 + ranking). Measured *through* an MCP client (e.g. Claude Code), the **JSON-RPC round-trip plus client-side LLM processing time is added on top**, and the end-to-end median can be **5–10 s** (this is not a spec violation). To measure the pure server side, use a microbenchmark that calls the `processor`/`database` functions directly. Also note, per §3.9, that the model load is backgrounded, so **a call that arrives before the background load finishes waits at most 20 s** (`MODEL_WAIT_SECONDS`, §3.9) and then runs keyword-only with the "still loading" notice; the first call after the model is loaded takes a few seconds.

2. **Real-person recall test (historical data)**: Save a passage about a real historical figure via `save_memory`, then search for that figure's name with `search_memory`. Pass criterion: the saved record appears in the **top-3 results**.
   - Japanese example: "坂本龍馬" (Sakamoto Ryoma)
   - English example: "Abraham Lincoln"

3. **Fictional-setting test (creative world-building / parent-document contract)**: Save a passage **≥400 characters** containing made-up names/places/terms via `save_memory`, then retrieve it with `search_memory`. Pass criterion: **every section of the saved text is restored byte-for-byte**. Per §3.11's parent-chunk design, even though search hits land on chunks, the response must substitute the verbatim full text fetched from `doc:<parent_id>`. Code blocks inside a Claude response are replaced with `[code omitted]` per the purification contract.
   - Japanese example: "Floating city Shiranui (fictional setting sheet)"
   - English example: any fictional character, city, or proper noun

4. **FTS punctuation + CJK tolerance test**: Save Japanese text containing brackets or punctuation (e.g. `架空の惑星「アルファ9」の気温設定`) via `save_memory`, then search with a query that drops the brackets (e.g. `アルファ9 気温`) **and with a query that keeps CJK punctuation** (e.g. `「アルファ9」の気温は？`). Pass criterion: the record shows up in the **top-3 results** for both. This verifies that §3.7's punctuation stripping and CJK bigram expansion are applied consistently on both the save side and the query side.

5. **Complete-recording test**: Confirm that any non-empty input is recorded — the legacy "skip if under N chars", "skip boilerplate", and "noise pattern" filters must **not** exist.
   - A 2-character string (e.g. `はい` / `ok`) passed to `save_memory` **is saved**.
   - Stock replies (`ok`, `yes`, `thanks`) passed to `save_memory` **are saved** (exact duplicates may be rejected server-side — if so, retry with a different string).
   - An updated fact (e.g. `The project budget is 100 dollars.` then `The project budget is 120 dollars.`) **is saved as a new entry** (no `near_duplicate`), and `search_memory` for it lists both with `saved=` times.
   - Empty or whitespace-only input is rejected with `empty content`; input with no visible character (e.g. `\x00`, a lone `U+200B`) is rejected with `empty content (no visible characters)` (§3.13).

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

7. **Hook integration (v2.0.0, adapted from Free §6 test 7)**: verify within Claude Code:
   1. **Global hook registration is in place** (mandatory gate). Run:
      ```bash
      python -c "import json,os; s=json.load(open(os.path.expanduser('~/.claude/settings.json'),encoding='utf-8')); h=s.get('hooks',{}); p=s.get('enabledPlugins',{}).get('n3mc-workingmemory@neuralnexusnote'); ok=lambda ev,names: any(any(n in c.get('command','').lower() for n in names) for e in h.get(ev,[]) for c in e.get('hooks',[])); assert not p, 'the plugin is enabled: its hooks run the PyPI code, not this installation (Method A step 3)'; assert ok('UserPromptSubmit',('n3mc-lite-hook','n3mc_mcp.lite_hook')), 'UserPromptSubmit hook missing'; assert ok('Stop',('n3mc-lite-stop-hook','n3mc_mcp.lite_stop_hook')), 'Stop hook missing'; assert any(r.endswith('-m n3mc_mcp.cli --search *)') for r in s.get('permissions',{}).get('allow',[])), 'permission rules missing'; print('OK')"
      ```
      Pass: prints `OK`. It requires the hooks written by this installation's `--init`; an enabled plugin fails it, because the plugin's hooks run the PyPI code instead of the generated one (L2, 2026-11-20). If it fails, the implementation is not complete: run `n3mc-lite --init` (§12).
   2. **UserPromptSubmit**: from a Claude Code session opened **outside the install folder** (e.g. `~`), send a message and confirm that `# Recalled Memory Context` is in that turn's context, and that the previous reply and the message are saved (`python -m n3mc_mcp.cli --list` shows `[claude] …` and `[user] …` records). Outside the install folder is essential: inside it, a missing global registration could be hidden.
   3. **Stop**: after the reply of that session, confirm that the reply is saved (`[claude] …`, same `turn_id` as the `[user]` record — check with `--recall-thread <turn_id>`), and that the project's `.claude/rules/n3mc-lite-behavior.md` starts with `<!-- n3mc-lite-behavior v1 -->`.
   4. `claude mcp list` (when available) does not list `n3mc-workingmemory`.

8. **Complete recording through the hooks (v2.0.0, adapted from Free §6 test 9)**:
   - Pass a 2-character reply as `last_assistant_message` to `--save-claude-turn` and a 5-character prompt to `--hook-submit` (hook JSON on stdin): both are saved, as `[claude] …` and `[user] …`.
   - A routine prompt (`ok`, `yes`, `thanks`) is saved every time it is sent, once in each exchange: hook saves reject exact duplicates only within the same `turn_id` (§3.16).
   - Every hook call writes an `audit.log` record, also when the save fails. **Check the failing save in a temporary data folder whose `redis_url` is the test Redis of §9** (L3, 2026-11-20): `docker pause redis-stack-test` for one hook call, then `docker unpause redis-stack-test`. **Never stop or pause the user's Redis**: its persistence is off (§3.4), so a restart deletes every memory.
   - A long, uniformly formatted reply (a settings text of 12 items, about 2,000 characters) is saved as one parent document `[claude] …` with chunks `[claude i/N] …`, and `--search` returns it in full as one `[doc×N]` hit. An almost identical settings text (a few values changed) in another exchange is saved too, and both versions are shown with the newer first.

9. **Fully automatic saving (v2.0.0, adapted from Free §6 test 10)**:
   1. Note the newest records with `python -m n3mc_mcp.cli --list`.
   2. Have **3 or more turns** of conversation in Claude Code. Claude must **not** call `--buffer` at any point.
   3. `--list` again: every turn has its `[user]` and its `[claude]` record, also when a message repeats an earlier one, with one `turn_id` per turn.

10. **MCP thin client (v2.0.0, like N3MC MCP Pro §6 test 20)**: in a temporary data folder (`N3MC_DATA_DIR`) whose `config.json` has a `server_port` of its own and the **test** Redis as `redis_url` (`redis://localhost:6390/0`, §9 — a temporary `config.json` has another `owner_id`, and its records in the real Redis would block the same texts for the real owner, §3.8), start `n3mc-workingmemory` as a child process with `N3MC_DATA_DIR` set and `N3MC_AGENT_NAME=test-client`, and talk to it over stdio with the `mcp` client (`mcp.client.stdio.stdio_client`, `ClientSession`):
    1. `initialize` and `tools/list`: the seven tools of §4.3 are listed with their v1.12.2 definitions, and the instructions are the text of §5 (1). Everything the child wrote to stdout was MCP messages (no parse error).
    2. With the temporary server stopped, call `search_memory`: the server is started and a result is returned. Then start a second MCP entry while the server runs and record how long `initialize` takes (not judged; Layer 8 `TestThinNoModel` checks that the entry loads no model). Both entries share one server (one PID file, one process).
    3. Save with `save_memory` and find it with `python -m n3mc_mcp.cli --search` (same `N3MC_DATA_DIR`); save with `--buffer` and find it with `search_memory`. The `save_memory` record has `agent_name` `test-client` (when the call passes none) and no role prefix.
    4. `list_memories`, `recall_thread`, `delete_memory`, `delete_memories_by_session` and `repair_memory` return the v1.12.2 texts (Layer 3).
    5. Stop the server (`/shutdown`), block its start (an occupied port), and call `save_memory`: the record is saved without a vector with the `warning` / `detail` of §4.6; `search_memory` returns `Error: memory backend unreachable. …`; the entry keeps running.
    6. Close the MCP entry: the server (when one runs) is still running (`/health`). Stop it afterwards with `/shutdown` and delete the temporary folder. On Windows the Python `mcp` stdio client used by this test may end its process tree through a Job Object when it closes, so the server may end too (§3.15; it ended on 2026-11-20, and kept running with `mcp` 1.27.0 on 2026-11-22). If it is gone, check that the next CLI call starts it again; if it still runs, stop it with `/shutdown` as above. Either outcome passes.
    Do **not** register the MCP entry in Claude Code or in the real desktop app configuration for this test.

11. **Context size limit (v2.0.0, like N3MC-Pro §6 test 19)**: in a temporary data folder whose `config.json` has `context_max_chars: 4000` and its own `server_port`, against the **test** Redis, save 30 records of 300–400 characters about the same subject (the same text with a few words changed in each) with `--buffer`. Run `--hook-submit` with a `UserPromptSubmit` JSON whose `prompt` asks about that subject, a manual `--search` with the same question, and the MCP `search_memory` with it. The stdout of `--hook-submit` and the `search_memory` text are each at most 4,000 as `context_size` counts it (§3.17), have fewer rows than the manual search and contain `lower-ranked match(es) omitted to keep this block within the context limit`; the manual `--search` prints every row and no such line. Then save one parent document of 20,000 characters on the same subject and repeat the hook search: the output still fits, and ends with `_(cut here to keep this block within the context limit.)_` when that document is among the first three hits.

> Items 7–9 run in the real installation with the user's Redis, like items 1–6, except the failing-save check of item 8 (test Redis). Items 10 and 11 use a temporary data folder and their own port.

---

## 11. Save guarantees and the limit of the MCP protocol

> **Stated up front as a design premise**: this server can persuade the LLM to save and search via MCP, but **whether the LLM actually calls those tools cannot be enforced at the MCP-protocol level**. This is not an implementation defect — it is a limit of what an MCP server is allowed to do.

> **(v2.0.0) Claude Code is no longer limited by this.** In Claude Code the hooks save every message and reply and search with every message (§12), whatever the model decides — the guaranteed save this section describes as out of reach for MCP. This section now applies to applications without hooks (the Claude desktop app and other MCP clients), which use the thin client of §4.6.

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

## 12. Claude Code Hooks (v2.0.0)

Everything in this section is new in v2.0.0. The flow is N3MC-Free's (§4, §4.5, §5 of the Free specification), with Lite's names, Lite's save rules (§3.16) and Lite's search (§3.14). It is written out here in full, because the code is generated from this document only.

> **🛑 Hook registration is a mandatory automated step.** The most common reason cross-session memory "does not work" after a successful-looking install is that the implementing AI tested the CLI and then left the `~/.claude/settings.json` hook block to the user. Method A ends with `n3mc-lite --init` (§8); the gate of §10 item 7.1 must print `OK` before the setup is reported as complete. (Plugin users get the hooks from the plugin.)

### 12.1 What the hooks do

| Hook | Entry point | Steps (each step is a synchronous `subprocess.run`, never `Popen`) |
|---|---|---|
| `UserPromptSubmit` | `n3mc-lite-hook` (`n3mc_mcp/lite_hook.py`) | Step 0: append the audit record. Then `python -m n3mc_mcp.cli --hook-submit` with the same stdin; its stdout is the hook's stdout (Claude Code puts it into the turn's context) |
| `Stop` | `n3mc-lite-stop-hook` (`n3mc_mcp/lite_stop_hook.py`) | Step 0: append the audit record. Then `python -m n3mc_mcp.cli --save-claude-turn` with the same stdin, then `python -m n3mc_mcp.cli --stop` with the same stdin (for `cwd`, §12.4) |

- `python` here is the hook's own `sys.executable`. The hook entries pass `--uvx` on to both `cli` calls when they were started with `--uvx` (§8).
- **Why synchronous**: `--repair` must finish before the search, and the search must have printed before control returns to Claude Code, or Claude does not see it (Free §5.2).
- **Time limit (L8)**: the hook entries are registered with `"timeout": 60` (seconds) in their `settings.json` / `hooks.json` entries, so Claude Code ends a hook that hangs; with the rules above a hook normally takes a few seconds. Not measured: Claude Code's own default when no `timeout` is given. Each step runs with a child timeout, and **the steps of one entry share `CHILD_TIMEOUT = 55` seconds** (each step gets what is left, at least 1 s), so the Stop entry's two steps end within the 60 s it is registered with (L7, 2026-11-20). On a timeout the stdout already printed is kept, as before.
- **Exit status**: both entries always exit 0. An error is one line on stderr. A hook that fails is shown to the user by Claude Code on every message, and a non-zero `UserPromptSubmit` can block the prompt; the text is already safe in `audit.log`. (Free exits with the status of `--hook-submit`; Lite does not, because "the hook never fails" is a decision of v2.0.0.)
- Total processes per turn: `n3mc-lite-hook` + 1 (`--hook-submit`), `n3mc-lite-stop-hook` + 2 (`--save-claude-turn`, `--stop`).
- The UTF-8 reconfigure block of §3.13 (1) runs at module top of both entries and of `cli.py`, before the first `sys.stdin.read()`.

### 12.2 `audit.log` (Step 0)

- One JSON line per hook call: `{"ts": <UTC ISO 8601>, "hook": "UserPromptSubmit" | "Stop", "raw": <the first 4,096 characters of stdin>, "payload": <the parsed JSON in full>}`; when stdin is not JSON, `payload` is `{"raw": <the whole stdin>}`. Written with `ensure_ascii=False`, UTF-8, append mode, **before anything else**, by the entry itself (not by a subprocess).
- Other records in the same file: `{"ts", "hook": "UserPromptSubmit", "stage": "skipped", "reason": "system_prompt"}`, `{"ts", "hook": "UserPromptSubmit", "stage": "transcript_unreadable", "reason": "<short reason>"}`, and the notices of §3.15.
- A failing write goes to stderr and never stops the hook.
- `--stop` renames `audit.log` to `audit.log.1` (replacing an older one) when it is larger than 50 MB.
- `audit.log` holds every prompt and reply in full, including code blocks and pasted text. It is the last-resort copy for a message that could not reach Redis; nothing reads it back into the memory (§7).

### 12.3 `--hook-submit`

Input: the UserPromptSubmit JSON on stdin (`session_id`, `transcript_path`, `cwd`, `permission_mode`, `hook_event_name`, `prompt`, and keys added by newer Claude Code versions, which are ignored). `message` is accepted as an alias of `prompt`. When stdin is not JSON, the whole stdin is the prompt.

1. **Clean**: pass the whole parsed JSON through `sanitize_surrogates` (§3.13 (2)).
2. **Text**: `_extract_text(prompt)` — a string `prompt` is a multimodal array **only when it parses as a non-empty JSON array whose elements are all objects with a string `type` key**; then the text is the `text` of its `type == "text"` parts joined with `\n`. Any other string is the user's own text, unchanged (`[1, 2, 3]` stays `[1, 2, 3]`). A list `prompt` is read the same way.
3. **System inputs**: when the text starts with one of `_SYSTEM_PROMPT_PREFIXES = ("<task-notification>", "<agent-message from=", "<system-reminder>")`, write the `skipped` audit record, print `# Recalled Memory Context\nSource: N3MC Lite (7-day working memory)\n\n_No memories found._\n`, and stop: no repair, no save, no search, and **no new `turn_id`** (Claude's reply to that input belongs to the current exchange).
4. **Server**: `ensure_server(cfg, max_wait=0)` (§3.15). **The hook never waits for a server start** (L8, 2026-11-20): when no healthy server answers, it starts one in the background (unless one is starting or the 5-minute back-off applies) and goes on at once without a server (direct save, placeholder search). v1.12.2 never made the user wait to send; waiting up to 60 s for a start, plus the first `uvx` fetch of the plugin, would. `ensure_server` takes `max_wait` (default 60, which the CLI keeps): it limits the poll of "PID file handling" and of "Starting the server" step 2. The Stop hook (`--save-claude-turn`) does the same. **Nor does the hook wait for the model** (M3, 2026-11-20): its `/buffer` and `/search` requests carry `"no_model_wait": true` (§3.9 item 26, §4.5).
5. **Repair**: when a server is in use, `POST /repair` `{}` (one batch: `ensure_index()` and up to 200 missing vectors, §3.10) with a timeout of 10 s (L8). A failure or a timeout is one stderr line. Without a server, skip.
6. **Previous reply**: read Claude's previous reply from `transcript_path` (12.5). When there is one, save it with `role = "claude"` and the `turn_id` in `turn_id_<session>.txt` (the previous exchange; when the file is missing, a new UUIDv4 that is written to the file) — §12.6. When the Stop hook already saved it, the save is answered `duplicate` and nothing is stored twice.
7. **New exchange**: make a new `turn_id` (UUIDv4) and write it to `turn_id_<session>.txt` — also for an image-only prompt.
8. **Search**: when the text is not empty, `POST /search` with `query` = the text, `session_id` = the hook's `session_id`, `max_chars` = `context_max_chars`, and print the answer's `text`. Empty text (image-only): print `# Recalled Memory Context\nSource: N3MC Lite (7-day working memory)\n\n_No memories found._\n`. Without a server, or on an error: print the placeholder of 12.7.
9. **Save the message**: when the text is not empty, save it with `role = "user"` and the new `turn_id` (§12.6).

- Every record of this hook has `agent_name = "claude-code"` and `session_id` = the hook's `session_id`.
- The search runs **before** the message is saved, so the message never finds itself.
- Image-only prompts: steps 5–7 still run; steps 8 and 9 print the empty block and save nothing.

### 12.4 `--save-claude-turn` and `--stop`

**`--save-claude-turn`** reads the Stop JSON (`session_id`, `stop_hook_active`, `last_assistant_message`), applies `sanitize_surrogates`, and, when `last_assistant_message` is not empty, saves it with `role = "claude"` and the `turn_id` from `turn_id_<session>.txt` (when the file is missing, a new UUIDv4 that is written to the file) — §12.6. The file is not cleared (the next `--hook-submit` overwrites it). **Why the new UUID is written (data-flow trace, 2026-11-20)**: the hook's exact-duplicate check works only within one `turn_id` (§3.16). When Stop saved a reply under a new UUID without a file and did not write it, the next `--hook-submit` recovered the same reply from the transcript (step 6) under another new UUID, and the reply was saved twice (reproduced on real data: the first reply right after `--init`). If Stop fires twice in one exchange, the second save is a `duplicate`. Prints nothing to stdout.

**`--stop`** (session-end housekeeping; it does **not** stop the server — `--stop-server` does, §13):

1. Prepare `<project>/.claude/rules/n3mc-lite-behavior.md` (§12.9). `<project>` is the value of the environment variable `CLAUDE_PROJECT_DIR` when it is set and names an existing folder (Claude Code sets it for the hook commands it starts, plugin hooks included: the root of the project the session was started in; on Windows it is written with forward slashes, e.g. `C:/Users/you/work`; the hook entry's children inherit it). Otherwise it is `<cwd>`: the `cwd` of the Stop hook's JSON, which `n3mc-lite-stop-hook` passes on to `--stop` on stdin as it does to `--save-claude-turn` (L21, 2026-11-20); without it, the process's working folder. **When `<project>` is the home folder**, skip this step: `~/.claude/rules/` would apply to every project. **Why the project folder (F3, 2026-11-22)**: the hook's `cwd` is the session's current working folder, a subfolder after Claude has `cd`ed into it, so `--stop` also created rules files in subfolders (found on a development PC). A rules file in a subfolder is loaded whenever Claude reads files in that folder, on top of the project's own file, and it stays after this product is uninstalled. Files already created in subfolders are left alone: `--stop` does not search the user's folders. This is the only behavior of Lite that this change alters.
2. Delete `turn_id_*.txt` files in the data folder whose modification time is more than 30 days old.
3. Rotate `audit.log` when it exceeds 50 MB.
4. Exit 0 and print nothing. Errors in steps 1–3 are one stderr line each.

`turn_id_<session>.txt`: `<session>` is the hook's `session_id` with every character outside `[a-zA-Z0-9_-]` replaced by `_`, cut to 64 characters; `turn_id.txt` when there is no `session_id`. The value comes from the hook's JSON and must never reach the file system unchanged.

### 12.5 Previous reply from the transcript

Free's decision D3, unchanged. `transcript_path` is the session's JSONL transcript. Read at most its last 5 MB (seek from the end) and walk the lines backwards:

1. Skip lines that are not JSON objects, lines with `isSidechain: true`, and lines whose `type` is neither `user` nor `assistant`.
2. Skip `user` lines that are not a real prompt: `isMeta: true`; `message.content` with no string and no `type: "text"` part (`tool_result` only); text starting with `[Request interrupted by user`, `<command-`, `<local-command-`, or one of `_SYSTEM_PROMPT_PREFIXES`.
3. Skip the current prompt itself — **the trailing line only**: the newest `user` or `assistant` line of the walk is skipped when it is a real prompt whose text is the new prompt. An older real-prompt line ends the walk even when its text is the same.
4. The first `assistant` line with a non-empty `type: "text"` part gives the answer: its **last** non-empty text part (`thinking` and `tool_use` parts are ignored). If another real prompt is reached first, there is no previous reply.

Any failure (missing path, unreadable file, unexpected structure) means "no previous reply": write the `transcript_unreadable` audit record and go on. The transcript is a recovery source only and never makes the hook fail. **Why the last text block**: Claude Code's Stop input carries only the final text block of a reply, so the same block makes the duplicate rule skip what Stop already saved.

### 12.6 Saving from a hook

1. **Through the server**: `POST /buffer` with `content`, `role`, `agent_name = "claude-code"`, `session_id`, `turn_id` (§4.5). `saved`, `duplicate` and `skipped_code` are final. An `error` answer is one stderr line (for example `empty content`).
2. **Redis unreachable** (`/buffer` answers 503): do not try a direct save. Emit `redis_unreachable`: `N3MC Lite could not reach Redis, so a message was kept only in <audit.log path>. ` + `backend_hint(redis_url)` (§3.9 item 4).
3. **No server or another failure** (no usable server after `ensure_server`, connection error, timeout, another HTTP status): **direct save** of §3.16 (`embed=False`), same `role`, `agent_name`, `session_id`, `turn_id`. When it fails because Redis is unreachable (`redis.ConnectionError` / `redis.TimeoutError`, or the connect check of §3.9 fails), emit `redis_unreachable` as in step 2. Any other exception: emit `save_failed` (`A record could not be saved (<ErrorType>: <message>). Current memories may be lost.`).
4. A save that ends `saved` or `duplicate` clears `redis_unreachable` and `save_failed` (L4, 2026-11-20: without the second, one failed save left `save_failed` shown once a day for good).
5. Nothing is queued for resending; `audit.log` already holds the text.

### 12.7 Search output (`/search`, `--hook-submit`, `--search`)

The server renders (`context.py`):

```markdown
# Recalled Memory Context
Source: N3MC Lite (7-day working memory)
Search query: <query>

> ⚠️ N3MC notice: <message>

_If several versions of the same content appear, the one with the newest `saved` time is the current one._

### [doc×3] [<id>] turn=<turn_id> score=0.812 saved=2026-11-13 10:34 (UTC+0900)
[claude] <content>

---

### [<id>] turn=<turn_id> score=0.640 saved=2026-11-13 10:33 (UTC+0900)
[user] <content>

#### Possibly related (weak matches — may be unrelated)

### [<id>] turn=<turn_id> score=0.412 saved=2026-11-12 21:15 (UTC+0900)
<content>
```

- The first line is `# Recalled Memory Context`, the second `Source: N3MC Lite (7-day working memory)` (L9, 2026-11-20: N3MC Free and Pro hooks print the same heading; this line is what the MCP instructions look for, §5 (1)), the third `Search query: <query>` (the query as searched: cut to `search_query_max_chars`, line breaks replaced by spaces), then an empty line, then **exactly the `search_memory` text of §3.14** — notices (§3.15 "Where notices are shown"), version sentence, rows, weak part, or `_No memories found._` — without the reminder. The CLI prints the block followed by one line break.
- With `max_chars` (the hook), the limit of §3.17 applies; without it (manual `--search`), the block is complete.
- **Degraded-state placeholders** (printed by the CLI itself, not limited): `# Recalled Memory Context` + `Source: N3MC Lite (7-day working memory)` + empty line + the due `notices.json` notices (if any) + one of:
  - no usable server: `_(memory search unavailable: <reason>)_` with the reasons of §4.6 ("the memory server is still starting (…)", "the memory server could not start; see <server.log path>");
  - another folder's server on the port: `_(memory search unavailable: another N3MC folder's server is using port <port>)_`;
  - `/search` answered 503 (Redis unreachable): `_(memory search unavailable: <hint>)_`, and the notice `redis_unreachable` is emitted;
  - any other error: `_(memory search unavailable: <ErrorType>: <message>)_`.
- The CLI prints something on **every** call, so Claude always sees the state of the memory.

### 12.8 `--init`

`n3mc-lite --init` (pip installations; plugin users do not need it):

1. Load (create) `config.json` in the data folder (§6).
2. Read `~/.claude/settings.json`. **A missing file is read as `{}`** (L13, 2026-11-20): it is created in step 6, without a backup (there is nothing to back up), and `~/.claude/` is created if needed. An unreadable file, or `hooks` / `permissions` / `permissions.allow` of the wrong type: change nothing, print the reason, exit 1.
3. **Plugin enabled?** When `settings["enabledPlugins"]["n3mc-workingmemory@neuralnexusnote"]` is `true`, the plugin registers the hooks: remove our own entries (if any) and add none, print `The n3mc-workingmemory plugin is enabled and registers the N3MC Lite hooks, so n3mc-lite --init added no hooks.`, and continue with step 5.
4. **Merge the hooks**: for `UserPromptSubmit` and `Stop`, keep every entry that is not ours (report each kept command that contains `n3mc` with `Another N3MC hook is registered for <event> and was left in place: <command>`), and append one entry `{"hooks": [{"type": "command", "command": resolve_cmd(<entry point>), "timeout": 60}]}` (L8).
5. **Permission rules**: append the missing `PERMISSION_RULES` of §8 (the `python -m n3mc_mcp.cli` and `n3mc-lite` forms); never remove anything.
6. **Write only when something changed**: copy the file to `settings.json.bak-n3mc-lite-<YYYYmmddHHMMSS>`, write a temporary file in the same folder, `os.replace` it.
7. **Old MCP registration**: report an `n3mc-workingmemory` entry in `~/.claude.json` or `./.mcp.json` as §8 says (read only).
8. Print `N3MC Lite hooks registered in <settings path>. Saving starts with the next Claude Code session.` (or the step 3 message).

```python
OURS = {  # event -> (entry-point name, module name)
    "UserPromptSubmit": ("n3mc-lite-hook", "n3mc_mcp.lite_hook"),
    "Stop":             ("n3mc-lite-stop-hook", "n3mc_mcp.lite_stop_hook"),
}
PKG_DIR = os.path.normcase(os.path.abspath(os.path.dirname(n3mc_mcp.__file__)))

def _tokens(command: str) -> list:
    command = command.replace("\\", "/")      # posix shlex reads "\" as an escape
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()

def is_own_hook(command: str, event: str) -> bool:
    exe_name, module = OURS[event]
    toks = _tokens(command)
    if not toks:
        return False
    first = os.path.basename(toks[0]).lower()          # (a) our entry point, any folder
    if first.endswith(".exe"):
        first = first[:-4]
    if first == exe_name:
        return True
    for i, t in enumerate(toks[:-1]):                  # (b) python -m n3mc_mcp.lite_hook
        if t == "-m" and toks[i + 1] == module:
            return True
    script = module.split(".")[-1] + ".py"             # (c) a script in this package's folder
    for t in toks[1:]:
        if os.path.basename(t).lower() == script:
            d = os.path.normcase(os.path.abspath(os.path.dirname(os.path.expanduser(t))))
            if d == PKG_DIR:
                return True
    return False

def shell_path(p) -> str:
    s = pathlib.Path(p).as_posix()                     # forward slashes: bash reads "\" as an escape
    if " " in s and os.name == "nt":                   # (L28) a command that starts with a quote
        short = _short_path(p)                         #       did not run as a hook on a Windows PC:
        if short and " " not in short:               #       use the 8.3 short name, which has no space
            s = pathlib.Path(short).as_posix()
    return f'"{s}"' if " " in s else s                 # quote only when a space is left

def _short_path(p) -> str:
    """Windows 8.3 short path of an existing file, or "" (8.3 names may be disabled on the volume)."""
    import ctypes
    buf = ctypes.create_unicode_buffer(32768)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(p), buf, len(buf))
    return buf.value if 0 < n < len(buf) else ""

def resolve_cmd(script_name: str) -> str:
    folders = [sysconfig.get_path("scripts")]
    try:
        folders.append(sysconfig.get_path("scripts", sysconfig.get_preferred_scheme("user")))
    except (KeyError, ValueError):
        pass
    for folder in folders:
        for name in (script_name + ".exe", script_name):
            cand = pathlib.Path(folder) / name
            if cand.is_file():
                return shell_path(cand)
    exe = shutil.which(script_name)
    if exe:
        return shell_path(exe)
    module = {"n3mc-lite-hook": "n3mc_mcp.lite_hook",
              "n3mc-lite-stop-hook": "n3mc_mcp.lite_stop_hook"}[script_name]
    return f"{shell_path(sys.executable)} -m {module}"
```

- `lite_hook.py` and `lite_stop_hook.py` have a `main()` and also run under `python -m` (the fallback form).
- **Why this exact matching** (Free D1, v1.5.0): matching every command that merely *contains* a hook name deleted other N3MC products' hooks; matching only one form left duplicates. Lite's names (`n3mc-lite-hook`, `n3mc_mcp.lite_hook`) differ from Free's (`n3mc-hook`, `n3memorycore.n3mc_hook`) and Pro's, so neither `--init` removes the other's entries (§14).
- **Why the scripts folder first and no quotes** (Free v1.6.0): right after installing Python, Claude Code's `PATH` lacks the scripts folder, and a hook command that starts with a quote did not run on one Windows PC. **(L28, 2026-11-20)** A path with a space would still need the quote, so on Windows `shell_path` uses the 8.3 short name of the file (`GetShortPathNameW`), which has no space. Only when the volume has 8.3 names turned off is the path quoted; `--init` then prints `The hook command for <event> starts with a quote because <path> contains a space; if the hook does not run, install Python into a folder without spaces.` Linux paths are quoted as before (the quote is not known to fail there).

### 12.9 The rules file `.claude/rules/n3mc-lite-behavior.md`

Claude Code loads `.claude/rules/*.md` of the project at session start. `--stop` keeps `<project>/.claude/rules/n3mc-lite-behavior.md` up to date (`_ensure_rules_md`; `<project>` is the project folder of §12.4 step 1, never the home folder). Free writes `n3mc-behavior.md`; the names differ so both products can be installed (§14).

**Template** (`_RULES_MD_TEMPLATE`, exactly this text with a line feed after the last line; do not rephrase it). `{cli}` is replaced by the command form of this installation: `<python> -m n3mc_mcp.cli` (pip), where `<python>` is `shell_path(sys.executable)` of the process that runs `--stop` — the absolute path of the Python that has the package, with forward slashes, quoted only when it contains a space (§12.8) — or `uvx --from n3memorycore-mcp-lite==<version> n3mc-lite` when `--stop` was called with `--uvx` (plugin). (L11, 2026-11-20: a bare `python` is another Python when the package is installed in a venv.)

````markdown
<!-- n3mc-lite-behavior v1 -->
# N3MemoryCore Lite Behavioral Guidelines

These guidelines apply to every Claude Code session in this project. They are
loaded automatically from `.claude/rules/` at session start. N3MC Lite is a
working memory: every record is deleted 7 days after it was saved.

## Fully Automatic Saving
All conversations are saved by hooks (UserPromptSubmit + Stop). Do NOT call
`--buffer` for them. Every non-empty user message and Claude response is
saved (fenced code blocks as `[code omitted]`); there is no length filter and
no skip-pattern filter. Make NO acknowledgement when a save succeeds —
silence is correct. Run `{cli} --buffer "<text>"` only when the user asks
you to remember something specific.

## Recalled Memory Context
Every turn, the UserPromptSubmit hook adds a `# Recalled Memory Context` block
to your context. Each hit starts with `### ` and shows its `saved=` time and,
when it has one, `turn=<turn_id>`; a `[doc×N]` hit is a long text shown in
full. When several versions of the same content appear, the one with the
newest `saved` time is the current one. If the block contains a
`> ⚠️ N3MC notice:` line, tell the user what it says.

## Weak Matches
Records under `#### Possibly related (weak matches — may be unrelated)` did
not pass the relevance floors and are often unrelated. Use one only when it
is actually relevant to the question; if it is not, ignore it and do not
mention it.

## Active RAG
When prior context would help, run a search proactively. **Always invoke via
`{cli} --search "<keywords>"`** (optional `--since 2026-11-13`,
`--until 2026-11-14`). To read the conversation around a hit, run
`{cli} --recall-thread <turn_id>`. `{cli} --list` shows the newest records.
These commands are auto-allowed via `permissions.allow`.

## Cut Context
The block is kept within a size limit. If it says that lower-ranked matches
were omitted, or ends with `_(cut here to keep this block within the context
limit.)_`, and the missing part is needed, run `{cli} --search` with the same
keywords: a manual search is not limited.

## Deleting
Delete only when the user explicitly asks: `{cli} --delete <id>` or
`{cli} --delete-session <session_id>`. Both are irreversible and are not
auto-allowed; confirm the id or session_id with the user first. They
remove the memory from Redis only: the raw text stays in `audit.log` in
the data folder (no TTL); tell the user so when they ask to forget
something.

## Seven-Day Lifetime
When the user expects something to be kept permanently ("remember this
forever", 「ずっと覚えておいて」), the hooks save it as usual; add ONE short
sentence in the user's language: Lite memories are deleted 7 days after they
were saved; for permanent storage use the Pro build or keep an external copy.

## Recall Acknowledgment
When recalled memory actually shapes your reply (you are recalling something
saved earlier), open the reply with a short acknowledgment in the user's
language — e.g. "Pulling this from earlier memory in this session." or
「前回の回答がメモリに保存されています。」 If no relevant memory was found,
or it did not influence your answer, do NOT announce anything.

## Save Failures
If a `> ⚠️ N3MC notice:` line says that Redis could not be reached or that a
record could not be saved, tell the user prominently and relay the command it
gives (for example `docker start redis-stack`): until then, new messages are
kept only in `audit.log`.
````

**File handling**:

| Existing file | Behaviour |
|---|---|
| None | Write the rendered template (create `.claude/rules/` if needed) |
| The content equals the template rendered for this installation | Nothing; clear the notice `rules_md_edited:<hash>` for this path |
| The content **with its command forms taken out** (below) equals the current template or a previous one (`_KNOWN_RULES_MD_HASHES`) | Back it up (`n3mc-lite-behavior.md.bak-n3mc-lite-<YYYYmmddHHMMSS>`), then write the rendered template. This covers another Python path, the other installation form and an older pinned version |
| Anything else (edited by the user) | **Do not replace it.** Emit `rules_md_edited:<hash>`: `The rules file <absolute path> has been edited by hand, so it was not updated. Merge the new template manually if needed.` `<hash>` = the first 12 hex characters of the SHA-256 of the file's absolute path |

- Content is compared with line endings normalized to LF. **Taking the command forms out (L12, 2026-11-20)**: replace every match of `(?:"[^"\n]*"|\S+) -m n3mc_mcp\.cli` and of `uvx --from n3memorycore-mcp-lite==\S+ n3mc-lite` with `{cli}`; the result is compared with `_RULES_MD_TEMPLATE` itself and, for a previous template, by the first 16 hex characters of its SHA-256 with `_KNOWN_RULES_MD_HASHES`. v1 is the first template, so the set starts empty. Because the pinned version of the `uvx` form is taken out, a new release does not make every plugin user's file look edited.
- **Whenever the template changes, bump the marker version and add the hash of the previous unrendered template to `_KNOWN_RULES_MD_HASHES`.**
- Write through a temporary file in the same folder and `os.replace`.
- **Why the command form is filled in**: a plugin installation has no package that `python -m` could find (the code lives in `uvx`'s environment), and a pip installation has no reason to start `uvx`. The form that wrote the file is the one that works in that project.

---

## 13. CLI `n3mc-lite` (v2.0.0)

`n3mc-lite` (`n3mc_mcp.cli:main`, also `python -m n3mc_mcp.cli`) is Free's CLI with Lite's functions. In Claude Code, which has no MCP tools for this server, it gives the same as the tools: each tool command calls the tool's handler in the resident server (`POST /tools/<name>?nudge=0`) and prints the answer's text unchanged, followed by one line break. Exactly one command per call (argparse, mutually exclusive group). Every string argument is passed through `sanitize_surrogates` (§3.13) before any command uses it, `--search` included (L6, 2026-11-20).

| Command | Calls | Prints |
|---|---|---|
| `--search "<keywords>" [--since S] [--until U] [--limit N]` | `POST /search` with `query`, `since`, `until`, `limit`, and `session_id` = `N3MC_SESSION_ID` of this process when set; no `max_chars` | The block of §12.7 (not limited) |
| `--buffer "<text>"` or `--buffer -` (stdin) `[--agent-name NAME] [--session-id S] [--turn-id T] [--importance X]` | `/tools/save_memory` with `content`, `agent_name` (default `claude-code`), `session_id`, `turn_id`, `importance` (each only when given) | The `save_memory` JSON of §4.3 |
| `--list [--limit N]` | `/tools/list_memories` | The `list_memories` text |
| `--recall-thread <turn_id> [--before N] [--after N]` | `/tools/recall_thread` | The `recall_thread` text |
| `--delete <id>` | `/tools/delete_memory` | The `delete_memory` JSON |
| `--delete-session <session_id>` | `/tools/delete_memories_by_session` | The JSON of §4.3 |
| `--repair` | `/tools/repair_memory` (one call: up to 200 vectors; run again while `remaining` > 0) | The `repair_memory` JSON |
| `--stop-server` (L26, 2026-11-20) | `ensure_server`'s identity check without starting anything: when `/health` on `server_port` reports our `root`, `POST /shutdown` with it and wait until the port is free (§3.15) | `Stopped the N3MC Lite server (pid <pid>).` only when the port became free within the 15 s wait; `No N3MC Lite server of this data folder is running.` (also when another folder's server holds the port, which is never stopped); exit 0. **When the stop is not confirmed** (the shutdown request failed and the port is still taken, or the wait ended with the port in use): `Could not stop the N3MC Lite server (pid <pid>): <reason>. End the process whose command line contains n3mc_mcp.cli and --run-server.` on stderr, exit 1 (2026-11-22, review #8: the failure was discarded and success was printed, so the uninstall steps went on with the server running) |
| `--hook-submit`, `--save-claude-turn`, `--stop` | §12 | §12 |
| `--init` | §12.8 | §12.8 |
| `--run-server` | Runs the resident server in this process (§3.15): load `config.json`, warn about a `bind_host` other than `127.0.0.1`, steps 1–3 of §3.9 and the library import, then uvicorn on `bind_host:server_port` with the lifespan that starts the model load; removes the PID file on exit when it holds its own PID | nothing on stdout |
| `--uvx` | Modifier for `--hook-submit`, `--save-claude-turn` and `--stop`: this installation runs through `uvx` (§8, §12.9) | — |

- `--search`, `--list`, `--recall-thread`: when no server is usable, `--search` prints the placeholder of §12.7 and the tool commands print the texts of the §4.6 table without the reminder. `--buffer` without a server saves directly (§3.16, no `role`) and prints the §4.6 `save_memory` JSON without the reminder.
- **Time bounds**: `--since` / `--until` are read with the rules of §4.3.1 before any request. An unreadable value, or `since` later than `until`, prints the `Error: …` text of §4.3.1 to stderr and exits with status 2.
- **Exit status**: 0, except 2 for argument errors and 1 for `--init` on an unreadable `settings.json`. A tool's own error text (`Error: …`, `"status": "error"`) is printed with status 0, as the tool returns it.
- **PID file**: the process that starts the server (`_start_server`) creates the PID file with `open(path, "x")` before `Popen` and writes the child's PID into it. When the exclusive create fails, another process is starting a server: follow "PID file handling" (§3.15). **The server writes its own PID (L1, 2026-11-22)**: the server writes its own `os.getpid()` into the PID file (`_write_own_pid`: exclusive create when the file is missing, otherwise overwrite; an `OSError` is ignored) twice: at the start of `_run_server`, before Redis and the model, and again right before `uvicorn.run`, because the starter writes `Popen.pid` just after `Popen` returns and the last write must be the server's. Free does the same once. Why: on Windows the `python.exe` of a venv (the standard venv and uv's, so also `uvx`) is a launcher that starts the real Python as its child, so `Popen.pid` is the launcher's number (observed: 37620 for the launcher, 32424 for the server). The launcher listens on no port, so after a change of `server_port` the moved-port check of §3.15 (M1) found no port, the old server was never stopped and every CLI call waited 60 s; `remove_pid_file_if_own()` never matched either. **An empty or unreadable PID file** (L19, 2026-11-20: the moment between the create and the write) is treated as "still starting" for 60 s after the file's modification time, never as a dead process: it is not deleted and no second server is started.
- **Which commands Claude may run without asking**: §8 "Auto-approve". `--delete`, `--delete-session` and `--init` always need the user's approval.
- Do not print hints such as "run n3mc-lite --search …" in normal output; the rules file tells Claude which commands exist.

---

## 14. Coexistence with other N3MC products (v2.0.0)

Lite can be installed on the same PC as N3MC-Free, N3MC-Pro and the MCP Pro / PostgreSQL / Ollama editions. Nothing is shared:

| | N3MC-Free | N3MC-Pro | MCP-Lite (this) |
|---|---|---|---|
| Data folder | `~/.n3mc` (`N3MC_HOME`) | `~/.n3mc-pro` (`N3MC_PRO_HOME`) | `~/.n3mc-workingmemory` (`N3MC_DATA_DIR`) |
| Database | SQLite in the data folder | SQLite in the data folder | Redis Stack (`redis_url`) |
| Server port | 18520 | 18521 | **18524** (PostgreSQL edition 18522, Ollama edition 18523) |
| Hook commands | `n3mc-hook`, `n3mc-stop-hook` | `n3mc-pro-hook`, `n3mc-pro-stop-hook` | `n3mc-lite-hook`, `n3mc-lite-stop-hook` |
| CLI | `n3mc` / `python -m n3memorycore.n3memory` | `n3mc-pro` / `python -m n3memorycore_pro.…` | `n3mc-lite` / `python -m n3mc_mcp.cli` |
| Rules file | `.claude/rules/n3mc-behavior.md` | `.claude/rules/n3mc-pro-behavior.md` | `.claude/rules/n3mc-lite-behavior.md` |
| MCP server name | — | `n3mc-pro` (N3MC MCP Pro; `n3mc-pg` for its PostgreSQL edition) | `n3mc-workingmemory` |

- **`--init` keeps the other products' hooks** (`is_own_hook`, §12.8) and reports them; the other products' `--init` keep Lite's (their matching does not match `n3mc-lite-hook` or `n3mc_mcp.lite_hook`).
- **Server identity**: each server reports its own data folder as `root`; a CLI that finds another product's server on its port never uses or stops it (§3.15 "When the port belongs to another folder's server").
- **Two products' hooks together**: Claude gets one `# Recalled Memory Context` block from each product every turn, each from its own memory, and each rules file names its own commands. This is the user's choice; neither product removes the other.
- **Plugin and pip on one PC**: the plugin's hooks and hooks written by `n3mc-lite --init` would both run (two searches per message, and the `[user]` and `[claude]` records of an exchange could get different `turn_id`s because both write `turn_id_<session>.txt`). `--init` registers no hooks while the plugin is enabled (§12.8), and the plugin's SessionStart script reports hooks written by `--init` (§8). Not verified yet: how Claude Code behaves when the plugin's hooks and `settings.json` hooks of the same program both exist (check it in the first generation test).
- **MCP coexistence**: rule 0 of the instructions (§5 (1)): when a persistent N3MC memory is connected over MCP in the same application — `n3mc-pro` (N3MC MCP Pro), `n3mc-pg` (its PostgreSQL edition) or `n3mc-longtermmemory` (the earlier MCP Pro) — Lite is the working memory (display change of v2.0.0, decision D10).

---

## Appendix A: Optional Extensions (not shipped)

The Lite build intentionally stops at the hybrid + time-decay ranker described in §3.6. The following extensions are **not part of the shipped spec** — they are sketched here so a future AI or contributor has a clean starting map when the user decides to try them. None of them are required for the Lite build to behave correctly; each is a precision-vs-latency trade.

- **Cross-encoder reranker** — after `hybrid_search` returns the top-N candidates, rerank them with a small cross-encoder (e.g. `cross-encoder/ms-marco-MiniLM-L-12-v2`, ~130 MB, or `BAAI/bge-reranker-base`, ~278 MB). Expect **+100–300 ms CPU latency** per `search_memory` call on a modern laptop (top-50 rerank), in exchange for roughly **+1 precision point** on paraphrase-heavy queries. Drop-in point: between the fused-score sort and the `min_score` filter in `processor.hybrid_search`. Keep the existing score as a fallback when the reranker is disabled. Note that the shipping build already enables a lightweight lexical reranker by default (`lexical_rerank_enabled`, see [§6](#6-configuration)); a cross-encoder would be a stronger upgrade slotting into the same hook.
- **HyDE (Hypothetical Document Embeddings)** — before embedding the user's query, ask a small LLM to synthesize a hypothetical *answer* to the query, then embed that answer instead of (or in addition to) the raw query. Helps when queries are short/vague and memories are long/specific. Needs an LLM hop per search, so it is a poor fit for the Lite build's "no external API calls" promise unless a local model is already available.
- **Japanese morphological analysis** — the shipping build already supplements RediSearch's default tokenizer with CJK bigram expansion (see [§3.7](#37-text-tokenization--punctuation-handling)), which covers the basic case. For further precision, pre-tokenize the body at save time with a morphological analyzer — candidates: `fugashi` + `unidic-lite` (MeCab-based, ~50 MB), `SudachiPy` + `sudachidict-core` (~70 MB, multi-granularity A/B/C modes), or pure-Python `Janome` when binary dependencies are a problem — store the space-joined surface forms in a parallel `text_tokens` TEXT field and point BM25 search at that field. Vector search is unaffected (e5 handles Japanese natively) and the raw body stays untouched for display. Expected cost: +5–20 ms per `save_memory` call; the delta versus bigram expansion shows up most on compound words and inflected forms.

All three extensions are additive — none of them require changes to the Redis schema's existing fields or the TTL/dedup contracts (the morphological tokenizer only **adds** a parallel field). A future implementer should treat them as separate feature flags, default-off, and benchmark each independently against the baseline ranker.

---

## Appendix B: Recommended AI-Assisted Workflow

> **This appendix is a human operator guide.** At each phase, copy the prompt inside ``` and paste it into Claude Code. The AI does not advance to the next phase on its own. (v2.0.0) In Claude Code the memory is driven by the **hooks** and the CLI `n3mc-lite` (§12, §13); the MCP tools are exercised through the thin client in a test folder (§10 item 10).

| Phase | What you do | Model |
|---|---|---|
| 1. Implementation | Paste the prompt to kick off the build | **Sonnet** (fast) |
| 2. Debugging | Paste three prompts **in order** to verify | **Sonnet** |
| 3. Evidence Report | **Restart Claude Code first**, then paste the prompt to run §10 | **Sonnet** or **Opus** |
| 4. Quality review | **Start a new session**, then paste the review prompt | **Opus** (deep reasoning) |

> **⚠️ A full Claude Code restart is required before Phase 3**
>
> (v2.0.0) The hooks and the rules file are read by Claude Code at startup; a session that started before `n3mc-lite --init` ran has no hooks. The resident server replaces itself when its code changes (`code_version`, §3.15), so it needs no manual restart; §10-1 measures the first search after a server start, which is easiest to observe after a fresh start.
>
> Pre-restart checklist:
> 1. Confirm the latest code is on the import path (`pip install -e .` or reinstall).
> 2. Confirm the hooks are registered: the gate of §10 item 7.1 prints `OK` (see [§8](#8-client-configuration), §12).
> 3. Confirm `~/.claude/settings.json` has the CLI permission rules (`Bash(python -m n3mc_mcp.cli --search *)` …; see [§8 Auto-approve](#auto-approve-claude-code-only)), and that `claude mcp get n3mc-workingmemory` reports no such server.
> 4. Confirm Redis Stack is up (`docker ps`; if not, `docker start redis-stack` or `docker run -d --name redis-stack -p 127.0.0.1:6379:6379 redis/redis-stack-server:latest`).
> 5. **Fully exit and relaunch Claude Code.** On **Windows**: closing the window with × or running `/exit` can leave background processes alive — open **Task Manager** and end every `Claude` / `claude` process. To measure a fresh server start, also end the `python.exe` whose command line contains `n3mc_mcp.cli --run-server`.
> 6. The first search after a server start takes 2–10 minutes if the multilingual-e5-base (~1.1 GB) HF cache is missing; meanwhile the hook prints a "still starting" placeholder or keyword-only results with a notice (§3.9).

---

### Phase 1: Implementation (Sonnet)

Set the model to **Sonnet** and paste:

```
Please implement n3mcmcp-lite according to this specification.
```

Sonnet will automatically handle code generation, hook setup, and server startup. Once complete, proceed to Phase 2 ("done" ≠ "spec-compliant", so do not stop here).

> **⚠️ Fully restart Claude Code between Phase 1 and Phase 2**
>
> When Claude Code launched at the start of Phase 1, the hooks did not exist yet, so this session saves nothing and gets no `# Recalled Memory Context`. Before pasting the Phase 2 debugging prompts, **fully exit Claude Code and reopen it** so the hooks registered by `n3mc-lite --init` and the rules file take effect (v2.0.0).
>
> **How to fully exit on Windows**: closing the window with × or running `/exit` can leave background processes alive. Open **Task Manager**, end every `Claude` / `claude` process, and then relaunch Claude Code. After relaunch, send one message and check that it was saved (`python -m n3mc_mcp.cli --list` shows its `[user]` record).

---

### Phase 2: Debugging (Sonnet)

Stay on **Sonnet** and paste the following three prompts **one at a time, in order**.

**① Data-flow trace** (check that nothing is silently dropped)
```
For n3mcmcp-lite:
Read the code and trace the end-to-end data flow from a save_memory tool
call (through the MCP thin client and /tools/save_memory) and from a hook
save (--hook-submit / --save-claude-turn and /buffer, and the direct save
without the server) all the way to the Redis pipeline's EXECUTE, and from
a search_memory call and a hook search back to the output. Verify that
TTL is set on every write, that the role prefixes of §3.16 are on the
parent document and on every chunk, and that the parent-document fallback
(§3.11) is not lost mid-flight. Fix any issues you find.
```

**② Spec-to-code comparison** (find behaviors documented but not implemented)
```
For n3mcmcp-lite:
Walk the input schema and behavior of each MCP tool in §4.3 one by one,
then the endpoints of §4.5, the thin client of §4.6, the hooks of §12 and
the CLI of §13, and compare them against the actual implementation. Also walk the parent-
chunk contract in §3.11 line by line (verbatim restoration, parent→chunk
cascade delete, [doc×N] rendering). Flag anything documented but not
implemented and fix it.
```

**③ Cross-session test** (confirm data survives a server restart)
```
For n3mcmcp-lite:
Check that a message of this session was saved by the hooks
(python -m n3mc_mcp.cli --list). Stop the resident server with python -m n3mc_mcp.cli --stop-server
(keep Redis running), then run python -m n3mc_mcp.cli --search for that
message: the server is started again and the record is found. Do the
same with save_memory through the MCP thin client in a temporary data
folder (§10 item 10). Fix anything that prevents this.
```

When all three are done, proceed to phase 3.

---

### Phase 3: Evidence Report

> **Fully exit and relaunch Claude Code before running this phase** (see the "⚠️ A full Claude Code restart is required before Phase 3" box above). Without the restart, the hooks of a changed installation may not be in effect, and §10-1's first-call timing after a server start cannot be captured.

The model can be **Sonnet** or **Opus** — Sonnet is sufficient to verify the items; Opus tends to grade harder. After the restart, paste:

```
For n3mcmcp-lite:
Run the §10 Evidence Report from the spec. Use the CLI commands of §13
(python -m n3mc_mcp.cli ...) for items 1-9 and the MCP client test of
item 10, and verify items 1 through 11 in order. Score each item on a
1–5 ⭐ scale and write up a short Evidence Report.

Be explicit about:
- §10-1: first search latency after a server start, and the median of 5
         steady-state search latencies; verify a hint (no crash) when
         Redis is down
- §10-3: byte-for-byte verbatim recall of a >400-char body (parent-doc
         contract and [doc×N] tag)
- §10-4: bracketed save text retrieved via a bracket-free query within
         the top 3 (CJK bigram coverage)
- §10-6: mixed inserts (short / medium / long-exceeding-chunk_threshold)
         under one session_id → delete_memories_by_session → matches
         the inserted count, leaves other sessions untouched, second
         call returns not_found
- §10-7..9: hook gate, [user]/[claude] records with one turn_id per turn
- §10-10: the MCP thin client in a temporary folder (no model, same texts)
- §10-11: the context size limit with context_max_chars 4000

If anything fails, identify the root cause and fix the implementation,
then rerun. When all items are ⭐⭐⭐⭐⭐, advance to Phase 4.
```

Once the Evidence Report is green, proceed to Phase 4 (quality review).

---

### Phase 4: Quality review (Opus)

> **⚠️ Review in a new session**: Do not review in the session that implemented and debugged the code. Exit Claude Code, start it again in the same folder, and begin a new session with the model set to **Opus**.
> - The implementing session's context holds its own decisions and explanations, so a review there tends to confirm them instead of checking the code against this specification afresh.
> - (v2.0.0) The resident server replaces itself when the code changes (`code_version`), so the review sees the server as a user will.
> - In the new session, memory searches may return notes saved during the implementation; that is expected. Base the review on the code, this specification and the results of commands actually run.

Switch the model to **Opus** and paste:

```
Please review n3mcmcp-lite and fix anything that needs fixing.

On a scale of 1–10, what score does n3mcmcp-lite earn as (a) a memory
device (hooks for Claude Code, MCP for other apps) and (b) a RAG system? Split the evaluation:
memory device (save / 7-day TTL / dedup / parent-document verbatim
contract) vs. RAG (hybrid search / CJK bigram / lightweight reranker /
time decay / auto importance). Generate a scorecard.
```

Opus will actually run the CLI commands (and the MCP thin client in a test folder) and produce a two-axis scorecard: **memory device** (persistence, TTL, dedup, parent doc) vs. **RAG** (retrieval precision, ranking, noise resistance).

> **Note**: the MCP Lite build ships a lightweight reranker and CJK bigram expansion by default, so the RAG ceiling sits slightly higher than in the Free build. Even so, the following are not implemented, and **the RAG score is unlikely to exceed 8:**
> - Morphological analysis (MeCab / SudachiPy, etc.) for stricter word boundaries (see Appendix A)
> - A full cross-encoder reranker (see Appendix A)
> - Language-specialized embedding models (e.g. `multilingual-e5-large`)
> - Query-expansion techniques such as HyDE (see Appendix A)
>
> Soft grading robs you of improvement opportunities — grade strictly. **Regardless of the score, have Opus explicitly list what is missing and discuss concrete fixes with it.**

---
