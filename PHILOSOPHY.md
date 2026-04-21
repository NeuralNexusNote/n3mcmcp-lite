# N3MemoryCore MCP — Development Philosophy

> A NeuralNexusNote™ product

## 🚀 The Core Function

**N3MemoryCore MCP** is a **Memory and local RAG system** exposed through
the **Model Context Protocol (MCP)**.

Any MCP-compatible client — Claude Desktop, Claude Code, or other tools
that speak MCP — can attach to it and gain memory across sessions.

- **Cross-Session Memory** — bridges conversations so the assistant
  remembers project context, decisions, and user preferences.
- **Local Context Injection** — a seamless RAG layer that stays entirely
  on the user's machine. No cloud. No API calls.
- **Client-agnostic** — the same store is reachable from every MCP
  client the user connects to it.

## 🎁 Lite vs. Paid

This repository is the **Lite** build. It is free, intended for the
Claude Marketplace distribution, and behaves identically to the paid
build on the MCP surface — with one deliberate tradeoff:

| Aspect          | Lite (this repo)               | Paid                       |
| --------------- | ------------------------------- | -------------------------- |
| Storage engine  | Redis Stack (RediSearch)        | SQLite + sqlite-vec        |
| Durability      | 7 d TTL, volatile              | Permanent, disk-persistent |
| External deps   | User runs Redis Stack container | None (self-contained)      |
| Target audience | Agentic loops, multi-agent collab, short-term projects | Long-term knowledge accumulation |

Why two builds? **They target different workflows, not different price
points.**

- **Lite — project-scoped memory.** The 7-day TTL and volatile Redis
  storage are active features. Agentic loops, multi-agent sessions,
  and short-lived experiments benefit from memory that resets cleanly
  (`docker restart`) or evaporates on its own. No pruning, no
  contamination from abandoned context.
- **Paid — continuous memory.** SQLite-backed persistence is the
  feature, not the luxury. For long-term projects where the assistant
  needs to remember decisions across months or years.

Claude Marketplace currently has no payment mechanism, so the Lite is
the build you can get from there today — but it is not a crippled
version of the Paid build, it is a different tool.

## Extending and Modifying the Code

The Lite source in this repository is a **regular, installable Python
package**. `pip install n3memorycore-mcp-lite` or a plain `git clone` →
`pip install -e .` is enough — no regeneration step, no AI pipeline.
Fork it, edit it, vendor it into your own project; nothing here assumes
you will re-derive the code from a spec.

That said, a protocol specification ships alongside the code
([`N3MemoryCore_MCP_Spec_EN.md`](./N3MemoryCore_MCP_Spec_EN.md)) for
people who want to *modify* the server without breaking contracts — the
RediSearch index layout, the hybrid ranking formula, the dedup
thresholds, and the optional extension points (cross-encoder reranker,
chunk-on-save, HyDE, Japanese morphological analysis) are documented
there. Use it as reference when editing; ignore it if you just want to
run the thing.

### Why MCP?

The earlier generation of N3MemoryCore bound the runtime tightly to
Claude Code's hook system (`UserPromptSubmit` / `Stop`). That gave
effortless auto-save but locked users into one client.

Migrating to MCP inverts the coupling:

| Aspect             | Old (hook-based)                        | New (MCP server)                                 |
| ------------------ | --------------------------------------- | ------------------------------------------------ |
| Client reach       | Claude Code only                        | Any MCP client                                   |
| Transport          | CLI subprocess + FastAPI HTTP           | stdio JSON-RPC                                   |
| Save trigger       | Hook executes automatically             | LLM calls `save_memory` (guided by instructions) |
| Process model      | Two processes (CLI + resident server)   | Single process per client                        |
| Distribution       | Git clone + Claude Code setup           | `pip install n3memorycore-mcp-lite`             |

Auto-save is preserved: the MCP server delivers behavioral `instructions`
during the `initialize` handshake, telling the connected LLM when to
search and when to save. The "Instruction is the light" principle holds
— the behavior is specified in natural language, not compiled into the
client.

## Project Details

- **Architecture** — Python package implementing the N3 MCP protocol.
  Storage layer is swappable; the Lite uses Redis Stack, the Paid build
  uses SQLite.
- **Focus** — Hands-free memory continuity across every MCP-capable tool.
- **License** — Apache License 2.0 — free to use, modify, and redistribute
  (including commercial use).

---

## 🚀 コア機能（日本語）

**N3MemoryCore MCP** は、**Model Context Protocol (MCP)** を通じて公開
される **メモリおよびローカル RAG システム** です。

Claude Desktop、Claude Code、その他 MCP 対応ツールなど、あらゆる MCP
クライアントが接続し、セッションをまたいだ記憶を獲得できます。

- **セッション間メモリ** — 会話をまたいでプロジェクトの文脈・決定事項・
  ユーザー選好をアシスタントに記憶させます。
- **ローカルコンテキスト注入** — 完全にユーザー端末内に留まる RAG
  レイヤー。クラウド送信も API 呼び出しもありません。
- **クライアント非依存** — ユーザーが接続するあらゆる MCP クライアント
  から同一のストアに到達可能。

## 🎁 Lite 版と有償版

本リポジトリは **Lite 版** です。無償で、Claude Marketplace 配布を
想定しており、MCP 的な外向き仕様は有償版と同一 — ただし以下の意図的な
トレードオフがあります：

| 観点               | Lite（本リポジトリ）               | 有償版                        |
| ------------------ | ----------------------------------- | ----------------------------- |
| ストレージエンジン | Redis Stack（RediSearch）            | SQLite + sqlite-vec           |
| 耐久性             | 7d TTL・揮発                        | 永続（ディスク保存）          |
| 外部依存           | ユーザーが Redis Stack コンテナを実行 | なし（セルフコンテイン）      |
| 対象ユース        | エージェントループ・マルチエージェント・短期プロジェクト | 長期的な知識蓄積             |

なぜ 2 版あるのか？**価格差ではなく、対象ワークフローが異なります。**

- **Lite — プロジェクト境界内のメモリ**。7 日 TTL と揮発性 Redis は
  積極的な特長です。エージェント的ループ、マルチエージェント会話、
  短期的な実験では、メモリがクリーンにリセットされる（`docker restart`）
  または自動蒸発することが有利に働きます。剪定不要、破棄済みコンテキスト
  からの汚染なし。
- **有償版 — 継続的なメモリ**。SQLite による永続化は「付加価値」では
  なく「本質」。数ヶ月〜数年にわたってアシスタントに決定事項を記憶
  させたい長期プロジェクト向け。

現在の Claude Marketplace には課金機構が無いため、Marketplace から
現時点で取得できるのは Lite 版ですが、これは有償版の機能制限版では
なく、**別の用途を持つ別のツール**です。

## コードの拡張・改造について

本リポジトリの Lite 版ソースは、**普通のインストール可能な Python
パッケージ**です。`pip install n3memorycore-mcp-lite` でも、素の
`git clone` → `pip install -e .` でも動きます。AI による再生成は
前提ではありません。**fork して編集、自分のプロジェクトに組み込む、
そのまま使う — いずれも問題ありません。**

なお、コードと一緒にプロトコル仕様書
（[`N3MemoryCore_MCP_Spec_JP.md`](./N3MemoryCore_MCP_Spec_JP.md)）を
同梱しています。これは「**契約を壊さずに改造したい人向けの参考資料**」
です。RediSearch インデックスの構造、ハイブリッドランキング式、重複
判定のしきい値、オプション拡張ポイント（クロスエンコーダ・リランカー、
保存時チャンキング、HyDE、日本語形態素解析）がそこに記載されています。
改造するなら参照してください — 普通に使うだけなら読まなくて結構です。

### なぜ MCP か

旧世代の N3MemoryCore は Claude Code のフック機構
（`UserPromptSubmit` / `Stop`）にランタイムが強く結合していました。
自動保存は楽でしたが、ユーザーは 1 つのクライアントに縛られていました。

MCP への移行は結合を逆転させます：

| 観点               | 旧（フック方式）                         | 新（MCP サーバー）                                  |
| ------------------ | ---------------------------------------- | --------------------------------------------------- |
| クライアント範囲   | Claude Code のみ                         | あらゆる MCP クライアント                           |
| 通信               | CLI サブプロセス + FastAPI HTTP          | stdio JSON-RPC                                      |
| 保存トリガー       | フックが自動実行                         | LLM が `save_memory` を呼ぶ（instructions で誘導）  |
| プロセス構成       | 2 プロセス（CLI + 常駐サーバー）         | クライアントごとに 1 プロセス                       |
| 配布               | Git clone + Claude Code セットアップ     | `pip install n3memorycore-mcp-lite`                |

自動保存は失われていません。MCP サーバーは `initialize` ハンドシェイク
時に**振る舞いの指示**を配信し、接続中の LLM に「いつ検索し、いつ
保存するか」を教えます。「指示は光」原則は保たれ、振る舞いは自然言語で
指定されクライアントにコンパイルされません。

## プロジェクト詳細

- **アーキテクチャ** — N3 MCP プロトコルを実装した Python パッケージ。
  ストレージ層は差し替え可能で、Lite は Redis Stack、有償版は SQLite
  を使用。
- **重点** — あらゆる MCP 対応ツールをまたいだ、ハンズフリーなメモリ
  継続性。
- **ライセンス** — Apache License 2.0 — 使用・改変・再配布（商用含む）
  を自由に許可。
