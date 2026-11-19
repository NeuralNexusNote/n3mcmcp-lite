# N3MemoryCore MCP — Development Philosophy

> A NeuralNexusNote™ product

## 🚀 The Core Function

**N3MemoryCore MCP** is a **Memory and local RAG system**. Claude Code
reaches it through **hooks**; the Claude desktop app and other tools that
speak the **Model Context Protocol (MCP)** reach it through an MCP entry.
Both gain memory across sessions, from the same store.

- **Cross-Session Memory** — bridges conversations so the assistant
  remembers project context, decisions, and user preferences.
- **Local Context Injection** — a seamless RAG layer that stays entirely
  on the user's machine. No cloud. No API calls.
- **Client-agnostic** — the same store is reachable from Claude Code and
  from every MCP client the user connects to it.

## 🎁 Lite vs. Pro

This repository is the **Lite** build. It is free, intended for the
Claude Marketplace distribution, and shares the same MCP surface as
the Pro build — with one deliberate tradeoff:

| Aspect          | Lite (this repo)               | Pro                        |
| --------------- | ------------------------------- | -------------------------- |
| Storage engine  | Redis Stack (RediSearch)        | SQLite + sqlite-vec        |
| Durability      | 7 d TTL, volatile              | Permanent, disk-persistent |
| External deps   | User runs Redis Stack container | None (self-contained)      |
| Target audience | Agentic loops, multi-agent collab, short-term projects, working memory | Long-term knowledge accumulation |

Why two builds? **They target different workflows, not different price
points.**

- **Lite — project-scoped working memory.** The 7-day TTL and volatile
  Redis storage are active features. Agentic loops, multi-agent
  sessions, and short-lived experiments benefit from memory that
  resets cleanly (`docker restart`) or evaporates on its own. No
  pruning, no contamination from abandoned context.
- **Pro — continuous memory.** SQLite-backed persistence
  is the feature, not the luxury. For long-term projects where the
  assistant needs to remember decisions across months or years.

The Pro build is a separate edition; the Lite is the free build — and
it is a different tool, not a crippled preview of Pro.

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

### Why hooks and MCP?

Up to v1.12.2 the Lite was an MCP server only. MCP reaches every client,
but an MCP server can only ask the model to save: whether it does is up
to the model. Claude Code's hooks (`UserPromptSubmit` / `Stop`) run
whatever the model decides, so they can save every message and reply.

From v2.0.0 the Lite uses each where it works best — it is N3MC-Free +
MCP, with Redis as the database:

| Aspect           | Claude Code                                   | Claude desktop app and other MCP clients            |
| ---------------- | --------------------------------------------- | --------------------------------------------------- |
| Connects through | Hooks and the CLI `n3mc-lite`                 | The MCP entry `n3mc-workingmemory` (stdio JSON-RPC) |
| Save trigger     | Hooks save every message and reply            | LLM calls `save_memory` (guided by instructions)    |
| Search           | The hook searches with every message          | LLM calls `search_memory` (guided by instructions)  |
| Process model    | One resident server on `127.0.0.1`, shared by both; it loads the embedding model once | (same server; the MCP entry is a thin client) |
| Storage          | The same Redis Stack, the same 7-day TTL      | (same)                                              |

The "Instruction is the light" principle holds on both paths: in Claude
Code the rules file `.claude/rules/n3mc-lite-behavior.md` tells Claude how
to use the memory, and the MCP entry delivers behavioral `instructions`
during the `initialize` handshake. The behavior is specified in natural
language, not compiled into the client.

## Project Details

- **Architecture** — Python package: hooks, a CLI and a resident server
  for Claude Code, and an MCP entry for other applications. Storage layer
  is swappable; the Lite uses Redis Stack, the Pro build uses SQLite +
  sqlite-vec.
- **Focus** — Hands-free memory continuity across Claude Code and every
  MCP-capable tool.
- **License** — Apache License 2.0 — free to use, modify, and redistribute
  (including commercial use).

---

## 🚀 コア機能（日本語）

**N3MemoryCore MCP** は、**メモリおよびローカル RAG システム** です。
Claude Code は**フック**でつながります。Claude デスクトップアプリと、
その他の **Model Context Protocol (MCP)** 対応ツールは、MCP の入口で
つながります。どちらも、同じストアから、セッションをまたいだ記憶を
獲得できます。

- **セッション間メモリ** — 会話をまたいでプロジェクトの文脈・決定事項・
  ユーザー選好をアシスタントに記憶させます。
- **ローカルコンテキスト注入** — 完全にユーザー端末内に留まる RAG
  レイヤー。クラウド送信も API 呼び出しもありません。
- **クライアント非依存** — Claude Code と、ユーザーが接続するあらゆる
  MCP クライアントから、同一のストアに到達可能。

## 🎁 Lite 版と Pro 版

本リポジトリは **Lite 版** です。無償で、Claude Marketplace 配布を
想定しており、MCP 的な外向き仕様は **Pro 版** と同一 —
ただし以下の意図的なトレードオフがあります：

| 観点               | Lite（本リポジトリ）               | Pro                          |
| ------------------ | ----------------------------------- | ----------------------------- |
| ストレージエンジン | Redis Stack（RediSearch）            | SQLite + sqlite-vec           |
| 耐久性             | 7d TTL・揮発                        | 永続（ディスク保存）          |
| 外部依存           | ユーザーが Redis Stack コンテナを実行 | なし（セルフコンテイン）      |
| 対象ユース        | エージェントループ・マルチエージェント・短期プロジェクト・ワーキングメモリ | 長期的な知識蓄積             |

なぜ 2 版あるのか？**価格差ではなく、対象ワークフローが異なります。**

- **Lite — プロジェクト境界内のワーキングメモリ**。7 日 TTL と揮発性
  Redis は積極的な特長です。エージェント的ループ、マルチエージェント
  会話、短期的な実験では、メモリがクリーンにリセットされる
  （`docker restart`）または自動蒸発することが有利に働きます。剪定
  不要、破棄済みコンテキストからの汚染なし。
- **Pro — 継続的なメモリ**。SQLite による永続化は「付加
  価値」ではなく「本質」。数ヶ月〜数年にわたってアシスタントに決定
  事項を記憶させたい長期プロジェクト向け。

Pro 版は別の版で、Marketplace から取得できるのは
Lite 版です。Lite は Pro の機能制限版ではなく、**別の用途を持つ
別のツール**です。

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

### なぜフックと MCP か

v1.12.2 までの Lite は MCP サーバーだけでした。MCP はあらゆる
クライアントに届きます。しかし MCP サーバーは、モデルに保存を頼む
ことしかできません。保存するかどうかはモデル次第です。Claude Code の
フック（`UserPromptSubmit` / `Stop`）は、モデルの判断にかかわらず
動くので、すべてのメッセージと応答を保存できます。

v2.0.0 からの Lite は、それぞれを向いている場所で使います。
「N3MC-Free + MCP で、データベースは Redis」です：

| 観点         | Claude Code                                  | Claude デスクトップアプリとその他の MCP クライアント |
| ------------ | -------------------------------------------- | ---------------------------------------------------- |
| つながる経路 | フックと CLI `n3mc-lite`                     | MCP の入口 `n3mc-workingmemory`（stdio JSON-RPC）     |
| 保存の契機   | フックがすべてのメッセージと応答を保存       | LLM が `save_memory` を呼ぶ（instructions で誘導）   |
| 検索         | フックがメッセージのたびに検索               | LLM が `search_memory` を呼ぶ（instructions で誘導） |
| プロセス構成 | `127.0.0.1` の常駐サーバー 1 つを両方で共有。埋め込みモデルは 1 回だけ読み込む | （同じサーバー。MCP の入口は薄いクライアント） |
| 保存先       | 同じ Redis Stack、同じ 7 日の TTL            | （同じ）                                             |

「指示は光」原則は、どちらの経路でも保たれます。Claude Code では、
ルールファイル `.claude/rules/n3mc-lite-behavior.md` が Claude に記憶の
使い方を伝えます。MCP の入口は、`initialize` ハンドシェイク時に
**振る舞いの指示**を配信します。振る舞いは自然言語で指定され、
クライアントにコンパイルされません。

## プロジェクト詳細

- **アーキテクチャ** — Python パッケージ。Claude Code 向けのフック・
  CLI・常駐サーバーと、ほかのアプリ向けの MCP の入口。ストレージ層は
  差し替え可能で、Lite は Redis Stack、Pro 版は SQLite + sqlite-vec を
  使用。
- **重点** — Claude Code と、あらゆる MCP 対応ツールをまたいだ、
  ハンズフリーなメモリ継続性。
- **ライセンス** — Apache License 2.0 — 使用・改変・再配布（商用含む）
  を自由に許可。
