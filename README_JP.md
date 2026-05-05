# N3MemoryCore MCP — Lite（揮発型）

> **N3MC-MCP-Lite は、Claude Code / Cursor / Windsurf などの
> MCP 対応エディタが利用する "外部メモリサーバー" です。**
> MCP Server として動作し、AI が会話やコード文脈を保存・検索できます。

> NeuralNexusNote™ プロダクト — **無償 Lite** 版：Redis Stack を使った
> 揮発性ハイブリッド（ベクトル + BM25）メモリを Model Context Protocol
> サーバーとして提供します。各エントリは 7 日で自動失効します。

> 💬 **MCP サーバの制限により、保存する会話は基本的に LLM 任せになります。
> ただし Claude Code に依頼すれば、hook を使った全会話の自動記録もセットアップ可能です。**
> 「毎ターン終わったら、Claude Code の会話全文を Lite に自動保存して」と頼めば、
> Claude Code が `~/.claude/hooks/` にスクリプトを置き、`~/.claude/settings.json`
> に `Stop` hook を追加します。これは LLM の判断を介さず harness が決定論的に
> 実行するため、Claude が `save_memory` を呼び忘れる事故が構造的に発生しません。
> 詳細は本 README の [hook による全会話保存](#hook-による全会話保存) 節を参照。

> 🇺🇸 **[English README](./README.md)**
> 🛡️ **[開発ポリシー](./PHILOSOPHY.md)**

---

## 🚀 クイックスタート — Claude Code に 3 ステップで接続

> 「何もインストールしていない」状態から「Claude Code が N3MC メモリを
> 使っている」までの最短経路。インストール手段（PyPI / フォーク / uvx）
> を選んで、クライアント設定に登録するだけ。Claude Code CLI と
> Claude Desktop の両方を扱います。

### Step 1 — Redis Stack を起動

```bash
docker run -d --name redis-stack -p 6379:6379 redis/redis-stack-server:latest
# 2 回目以降は: `docker start redis-stack`
```

### Step 2 — パッケージをインストール（いずれか）

**(a) PyPI から** — 大半の利用者：

```bash
pip install n3memorycore-mcp-lite
```

**(b) フォークから**（リポジトリをクローン済み）— 寄稿者・改造目的：

```bash
git clone https://github.com/<YOU>/n3mcmcp-lite
cd n3mcmcp-lite
pip install -e ".[dev]"
```

**(c) uvx でゼロインストール** — グローバル install を使わず隔離環境：

```bash
# 動作確認のみ。実際の起動は MCP クライアント設定が担う：
uvx --from n3memorycore-mcp-lite n3mc-workingmemory --help
```

Step 2 終了時点で `n3mc-workingmemory` コマンドが `PATH` に通ります。
`where n3mc-workingmemory`（Windows）または `which n3mc-workingmemory`
（macOS/Linux）で確認できます。

### Step 3 — MCP クライアントに登録

| クライアント | やること |
|---|---|
| **Claude Code（CLI）・本リポジトリの作業ツリー** | `.mcp.json` がコミット済み。`cd` して `claude` を起動するだけで次のプロンプトから自動接続 |
| **Claude Code（CLI）・別プロジェクトのディレクトリ** | そのプロジェクトの `.mcp.json` に同じ `n3mc-workingmemory` ブロックを追加。詳細は [Claude Code（スタンドアロン CLI）](#claude-codeスタンドアロン-cli) |
| **Claude Desktop**（内蔵の「Code」タブ含む） | OS ごとの `claude_desktop_config.json` を編集。詳細は [Claude Desktop](#claude-desktopおよび-claude-desktop-内の「code」タブ) |
| **Claude Code でツール自動許可** | `~/.claude/settings.json` に許可ブロックを 1 つ足すだけ。AI が承認待ちで停止しなくなる。詳細は [ツール自動許可](#ツール自動許可claude-code-固有) |
| **uvx 経由で起動**（グローバル install 不要） | クライアント設定の `command`/`args` を uvx 形式にする。詳細は [Claude Code（スタンドアロン CLI）](#claude-codeスタンドアロン-cli) |

これだけ。接続すると、サーバの振る舞い指示が引き継ぎ、`search_memory`
が各ターン先頭で、`save_memory` が応答後に自動で呼ばれます。

> 初回呼び出しのみ 30〜60 秒かかります — `intfloat/e5-base-v2`
> （~400 MB）が `~/.cache/huggingface/` にダウンロードされるため。
> 2 回目以降は数秒で起動。

---

## ⚠️ 事前準備（インストール前に必須）

このサーバーは **そのままでは起動しません**。以下 2 点を事前に用意してください：

1. **`localhost:6379` で動く Redis Stack** — Lite 版は Redis + RediSearch にメモリを保存します。Docker が最も簡単です：
   ```bash
   # 初回のみ（コンテナを作成）：
   docker run -d --name redis-stack -p 6379:6379 redis/redis-stack-server:latest

   # 2 回目以降（コンテナは既存なので start するだけ）：
   docker start redis-stack
   ```
   コンテナ作成後に再度 `docker run` を実行すると `Conflict. The container name "/redis-stack" is already in use` エラーになります。2 回目以降は `docker start redis-stack` を使ってください。

   > **docker コマンドに永続化フラグが無い理由**：Lite 版は**意図的に
   > 揮発性**です。揮発性は Lite と有償の永続 N3MemoryCore 版を分ける
   > 製品境界です。`--save ""` のような空文字列引数は Windows PowerShell
   > や cmd.exe のクォート処理で壊れやすい（コンテナのエントリポイント
   > が起動不能になる事例あり）ため、docker 側での指定はやめ、MCP
   > サーバーが起動時に `CONFIG SET appendonly no` および `CONFIG SET
   > save ""` を毎回発行して**強制的に永続化を無効化**します。セッショ
   > ン間に手動で永続化を有効にしても次回 Lite 起動時に無効に戻され
   > ます。上の素の `docker run` で十分 — 揮発性の保証の真の源は
   > サーバー側です。
2. **[`uv`](https://docs.astral.sh/uv/) を `PATH` に通す** — Claude Code プラグイン / `uvx` 経由インストールの場合のみ必要。ソースからインストールする場合は不要です。

Redis に接続できない場合はサーバーが起動を拒否し、`uv` が無いと Claude Code プラグインは立ち上がりません。`/plugin install` やクライアント設定の前に必ず揃えてください。

---

## 特徴

- 💾 **完全ローカル** — 会話データは自分の PC の Redis に保存。クラウドに送りません
- 🔍 **意味で検索** — キーワードが違っても、関連する過去の会話を引き出します
- 🌐 **多言語対応・標準装備** — CPU のみ動作、LLM/GPU 不要。NFKC フォールド（`ｱﾙﾌｧ`↔`アルファ`、`１２３`↔`123`、合字）／日本語・中国語・韓国語・タイ語・ラオ語・ミャンマー語・クメール語のバイグラム被覆／Latin 系の発音区別フォールド（`café`↔`cafe`）
- 🛡️ **エンコーディング安全策** — Windows での stdio UTF-8 再設定（cp932 → UTF-8）、すべての入力に対する孤立サロゲート除去。Free 版と同等の防御
- 🔄 **会話の文脈を保持** — **7 日間**のワーキングメモリ（Redis TTL により自動失効。長期記憶が必要なら Pro 版）
- ⚡ **自動で動く** — 保存も検索もすべて自動。MCP の `initialize` 応答で配信される振る舞い指示により、ユーザーの指示は不要です
- 🤖 **マルチエージェント対応** — 複数の AI エージェントが 1 つの Redis を共有。`b_local` と `b_session` のバイアスでプロジェクトの記憶を優先しつつ、他のエージェントの知識も検索できます
- 🏢 **チーム・組織にも対応** — Redis を共有サーバーに配置し、`N3MC_REDIS_URL` をチーム全員で同じ URL に向ければ、記憶を共有できます（⚠️ Redis 自体のアクセス制御・認証が必要）
- 🧹 **揮発性は設計上の特長** — 7 日で自動蒸発するため、失敗した試行や破棄された設計案が次タスクに流出しません。`docker restart redis-stack` で即座に一掃可能
- 💰 **トークン消費を削減** — 過去の文脈を再説明する必要がなくなります。記憶検索はローカル embedding（`intfloat/e5-base-v2`）で行うため Claude のトークンを消費せず、的確な文脈注入により修正のやり取りも減少します

## 仕組み

```
ユーザーの発言
    │
    ▼
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  1. 自動保存  │────▶│  2. 意味検索  │────▶│ 3. コンテキスト│
│  前回の回答を │     │  関連する過去 │     │    注入       │
│  Redisに保存 │     │  の記憶を取得 │     │  Claudeに渡す │
└──────────────┘     └──────────────┘     └──────────────┘
                                                 │
                                                 ▼
                                          Claudeが過去の
                                          文脈を踏まえて回答
```

MCP の `initialize` 応答で配信される**振る舞い指示**により、すべて自動で
行われます。Claude Code のフックは使わないため、クライアント側の追加設定は
不要です（ツール自動許可の `permissions.allow` 設定のみ）。ユーザーが意識する
必要はありません。

### Claude 標準の auto-memory との違い

Claude Code には標準の auto-memory 機能（`~/.claude/projects/.../memory/`）が
あります。N3MemoryCore はこれと**競合せず、補完し合います**。

|                | Claude auto-memory                     | N3MemoryCore RAG                   |
| -------------- | -------------------------------------- | ---------------------------------- |
| **得意なこと** | 確実・毎回ロード・固定情報             | 過去の会話の文脈・詳細な経緯       |
| **苦手なこと** | 会話の流れや文脈は持てない             | 検索精度に依存・確実性がない       |
| **用途**       | ユーザープロフィール、フォルダパス等   | 会話の詳細、議論の経緯             |

**推奨の使い分け：**

- **毎回必要な固定情報**（開発フォルダのパス、ユーザーの好みなど）→ auto-memory に保存
- **会話の文脈・経緯**（議論の流れ、過去の決定理由など）→ N3MemoryCore が自動で蓄積（Lite は 7 日、Pro は永続）

---

## Lite 版と Pro 版（公開予定）

| 版                        | ストレージ                          | 耐久性           | 配布先              |
| ------------------------- | ----------------------------------- | ---------------- | ------------------- |
| **Lite（本リポジトリ）**  | Redis Stack（RediSearch）            | 7d TTL・揮発   | Claude Marketplace  |
| **Pro（公開予定）**       | SQLite + sqlite-vec（ローカルファイル） | 永続            | 別途配布             |

MCP の外向き仕様は同じ（6 ツール・同じランキング式。`delete_memories_by_session` のみ Lite 専用）。7 日 TTL と
揮発性 Redis ストレージは**設計上の特長であり制約ではありません** —
以下のワークフローでは Lite 版の方が適しています：

- **エージェント的コード生成ループ** — 失敗した試行や破棄された設計案
  が次タスクに流出しません。`docker restart redis-stack` で一掃可能。
- **マルチエージェント協調** — あるタスクでの決定事項が、無関係な後続
  タスクを汚染しません。
- **試作・実験用途** — 放置すれば 7 日で自動蒸発、削除判断は不要です。

**Pro 版（公開予定）** は真逆のユースケースを想定：数ヶ月〜数年に
わたる長期的な知識蓄積、永続性こそが価値となる場面。**プロジェクト
境界内のワーキングメモリ**が欲しければ Lite、**継続的なメモリ**が
必要であれば **Pro 版（公開予定）** をお待ちください。

## 概要

`n3memorycore-mcp-lite` は、Claude をはじめとする任意の MCP 対応
クライアントに **短時間の** 会話メモリを与えるローカル専用 MCP サーバー
です。Redis Stack 上に BM25 全文検索インデックスと 768 次元ベクトル
インデックス（[`intfloat/e5-base-v2`](https://huggingface.co/intfloat/e5-base-v2)）
の両方を持ち、ハイブリッドランキングで結果を返します。

全ての処理はユーザー端末上で完結します。API コールもクラウド保存も
ありません。

## 提供ツール

| ツール                          | 用途                                                                                |
| ------------------------------- | ----------------------------------------------------------------------------------- |
| `search_memory`                 | ハイブリッド検索（ベクトル + BM25、時間減衰ランキング、`session_id` ブースト）       |
| `save_memory`                   | 短いエントリを保存（7d TTL、完全一致・近似重複は自動拒否）                          |
| `list_memories`                 | 直近のエントリを新しい順に一覧                                                      |
| `delete_memory`                 | 特定のエントリを id で削除（id が親ドキュメントの場合は子チャンクごとカスケード削除）|
| `delete_memories_by_session`    | `session_id` に紐づく記憶を一括削除 — 終了したプロジェクトを TTL 待ちなしで撤収       |
| `repair_memory`                 | 欠損時に RediSearch インデックスを作り直す                                          |

さらに、MCP の `initialize` 応答で**振る舞いの指示**をクライアントへ
配信します。これにより「各ターン先頭で `search_memory`、応答後に
`save_memory`」という自動保存運用が、Claude Code のフック無しでも
成立します。

## ID 階層構造

N3MemoryCore は 5 つの ID フィールドで各レコードの出所と文脈を識別
します。実利用者が触るのは `session_id`（と稀に `agent_name`）だけで、
他は自動で埋まります。

| ID | 保存場所 | 生成タイミング | 粒度 | 用途 |
|---|---|---|---|---|
| `id` (PK) | Redis ハッシュ | レコード作成時 (UUIDv7、時系列順) | **1 レコード** | 各記憶の一意識別子 — `delete_memory` と重複検出に使用 |
| `owner_id` | `config.json` | 初回起動時 (UUIDv4) | **所有者 / インストール** | データの所有者識別。`save_memory` 呼び出しごとに検証され、不一致のペイロードは `owner_id mismatch` で拒否される。TAG フィールドとして保存され、フィルタリングは Python 側で行う（仕様書 §3.12） |
| `local_id` (agent_id) | `config.json` | 初回起動時 (UUIDv4) | **エージェント / インストール** | このインストールの UUIDv4 識別子。Pro 版との前方互換のため全レコードに保存されるが、**Lite の `b_local` ランキングには使われない** — `b_local` は `stored_importance + access_count` のみから算出される（後述のランキング式参照） |
| `session_id` | メモリ内またはクライアントから供給 | タスク／プロジェクト／会話ごと（任意文字列） | **タスク／プロジェクト／会話** | 同じタスク・プロジェクト・会話に属する記憶をまとめて浮上させる。**`b_session` ランキングバイアス**（`b_session_match=1.0`、`b_session_mismatch=0.6`）を駆動し、進行中の会話の記憶を、同じ Redis 内の無関係なクロスプロジェクト行より優先する。`delete_memories_by_session` のフィルタキーでもある。解決順序：呼び出し引数 → `N3MC_SESSION_ID` 環境変数 → プロセス起動時の UUIDv4 フォールバック |
| `agent_name` | Redis ハッシュ | `save_memory` 呼び出し時（任意文字列） | **エージェント表示名** | エージェントの人間可読なラベル（例：`"claude-code"`、`"claude-desktop"`）。ランキングには使用されず、表示・監査用 |

```
owner_id  (1 つの N3MC サーバ／データ所有者)
  └── session_id  (1 つのタスク／プロジェクト／会話)
        └── local_id  (そのセッション内で発話するエージェント)
              ├── agent_name  (その表示名: "claude-code" 等)
              └── id  (1 件の記憶レコード)
```

**実用上のガイダンス：**

- **名前のあるプロジェクトやタスクに従事中は `session_id` をピン留め
  すること**。`save_memory` と `search_memory` の両方に同じ文字列
  （例：`"proj-alpha"`、`"task-refactor-auth"`）を渡す。これにより
  プロジェクト固有の記憶がランキング上位に押し上げられ、終了時には
  `delete_memories_by_session` で一括撤収できる。
- **単一エージェント運用では `agent_name` は空でよい**。複数エージェントが
  同じ Redis を共有する場合のみ設定（`"claude-code"`、`"cursor"` 等）
  すれば、`list_memories` の出力が読みやすくなる。
- **`owner_id` は通常渡さない**。サーバが `config.json` の値と照合し、
  不一致なら拒否する仕組みなので、空のままにすれば「自分の所有」として
  扱われる。所有権を明示的に主張する必要がある特殊ケースのみ指定。

## 前提条件

### 1. Redis Stack の起動

Lite 版は Redis Stack（Redis + RediSearch モジュール）を必要とします。
Docker を使うのが最も簡単です：

```bash
# 初回のみ（コンテナを作成）：
docker run -d --name redis-stack -p 6379:6379 redis/redis-stack-server:latest

# 2 回目以降（コンテナは既存なので start するだけ）：
docker start redis-stack
```

コンテナが `localhost:6379` で Redis を公開し、サーバーは自動でこれを
見つけます。初回インストール後に `docker run` を再実行すると `Conflict.
The container name "/redis-stack" is already in use` エラーになります
ので、以後は `docker start redis-stack` を使ってください。

### 2. パッケージのインストール

**PyPI から（推奨）**：

```bash
pip install n3memorycore-mcp-lite
```

または `uvx` でゼロインストール実行（Claude Code プラグインはこの経路を使用）：

```bash
uvx --from n3memorycore-mcp-lite n3mc-workingmemory
```

**ソースから**（コードを編集する場合）：

```bash
git clone https://github.com/NeuralNexusNote/n3mcmcp-lite
cd n3mcmcp-lite
pip install -e .
```

初回起動時に ~400MB の埋め込みモデルが Hugging Face から
`~/.cache/huggingface/` にダウンロードされます。

## クライアント設定

### Claude Desktop（および Claude Desktop 内の「Code」タブ）

**Claude Desktop アプリ**（内蔵の **Code** タブを含む）を使っている場合
は、`.mcp.json` ではなく Desktop 用の設定ファイルを編集してください。
`.mcp.json` はターミナルから起動する `claude` CLI 専用で、Claude
Desktop アプリからは読まれません。

`%APPDATA%\Claude\claude_desktop_config.json`（Windows）または
`~/Library/Application Support/Claude/claude_desktop_config.json`（macOS）：

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

**Windows の注意点：** 上記のコマンド名指定で Claude Desktop がサーバー
を起動できない（ハンマー／ツールアイコンが出ない）場合は、`"command"`
をインストール済み `.exe` への絶対パスに置き換えてください。例：

```json
"command": "C:\\Users\\<ユーザー名>\\AppData\\Local\\Programs\\Python\\Python312\\Scripts\\n3mc-workingmemory.exe"
```

正確なパスはターミナルで `where n3mc-workingmemory` を実行して確認
できます。

**設定ファイル編集後は Claude Desktop を完全に終了してください。**
ウィンドウを閉じるだけでは不十分です。タスクトレイの Claude アイコン
を右クリックして Quit、もしくはタスクマネージャーで Claude 関連
プロセスをすべて終了させてから再起動してください。

### Claude Code（スタンドアロン CLI）

この節は `claude` コマンドラインツール専用で、Claude Desktop 内の
「Code」タブではありません（そちらは上の節を参照）。

**`.mcp.json` はこのリポジトリに同梱されています。** リポジトリを
クローンしてパッケージをインストールするだけで、Claude Code CLI が
自動的に接続されます — 手動設定は不要です。

他のプロジェクトから利用する場合は、そのプロジェクトの `.mcp.json`
に以下を追加してください：

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

### ツール自動許可（Claude Code 固有）

Claude Code は既定で各 MCP ツール呼び出しに対してユーザー承認プロンプトを
出します。**「AI が意識せず保存・検索する」自動ループを成立させるには**、
`n3mc-workingmemory` のツールを Claude Code 設定の `permissions.allow` に
事前登録しておく必要があります。

**プラグイン経由インストールは自動設定** — `/plugin install n3mc-workingmemory@neuralnexusnote`
でインストールすると、`SessionStart` フック [`hooks/install_permissions.py`](plugins/n3mc-workingmemory/hooks/install_permissions.py)
が `~/.claude/settings.json` の `permissions.allow` に 6 つの
`mcp__n3mc-workingmemory__*` ツールを冪等追加します。手動編集不要。
1 件でも欠けていれば追記、すべて揃っていれば無書き込み。既存フィールドは
温存します。`hooks.json` は `python` → `py`（Windows Python Launcher） →
`python3` の順にフォールバックチェイン（`||`）で試行するため、いずれか
1 つが `PATH` 上にあれば動作します。3 つすべて不在の場合のみ exit 非ゼロ
で失敗し、Claude Code の `/plugins` Errors タブに表示されます（silent fail
を回避）。

**プラグイン未経由のインストール**（`claude mcp add` / 手動 `.mcp.json` /
Python 完全不在）の場合は、下記ブロックを `~/.claude/settings.json`（ユーザー
グローバル — 推奨）または `.claude/settings.json`（プロジェクトスコープ）
に手動追記してください：

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

これがないと、`save_memory` / `search_memory` のたびに承認ダイアログが出て
AI が停止します（ユーザーが席を外していれば動作不能）。Claude Desktop には
ツール単位のパーミッションゲートが無いため、この設定は不要です。

## データ保存先

Lite 版はディスク上に DB を持ちません。メモリは Redis に保存され、
自動で失効します。ディスクには小さな `config.json` だけがプラット
フォーム標準のユーザーデータディレクトリに置かれます：

| OS      | パス                                                        |
| ------- | ----------------------------------------------------------- |
| Windows | `%LOCALAPPDATA%\n3mc-workingmemory\`                        |
| macOS   | `~/Library/Application Support/n3mc-workingmemory/`         |
| Linux   | `~/.local/share/n3mc-workingmemory/`                        |

環境変数 `N3MC_DATA_DIR` で上書き可能です。

## 設定

初回起動時に `config.json` が自動生成され、`owner_id` と `local_id`
にランダム UUID が割り当てられます。編集可能な既定値：

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
  "rerank_phrase_weight":     0.2,
  "b_session_match":          1.0,
  "b_session_mismatch":       0.6,
  "skip_code_blocks":         false
}
```

- `redis_url` — 接続 URL。環境変数 `N3MC_REDIS_URL` が優先される。
- `ttl_seconds` — 保存ごとに付与される TTL（既定 7 日）。
- `chunk_threshold` / `chunk_overlap` — スライディングウィンドウのサイズと重複（文字数）。閾値超の本文は親ドキュメント＋チャンク経路に入り、verbatim 復元される。
- `access_count_*` — アクセス頻度ベース自動 importance。検索の上位 K 件にヒットすると次回以降の検索でブースト（上限あり）。
- `ttl_refresh_on_search` / `ttl_refresh_top_k` — 検索ごとに上位 K 件の TTL をリセット（リセットのみ。新規保存以上には伸びない）。
- `lexical_rerank_*` / `rerank_weight` / `rerank_phrase_weight` — 軽量な後段リランカー（CPU のみ）。
- `b_session_match` / `b_session_mismatch` — 行の `session_id` が一致したときのスコア倍率（既定 `1.0`）と不一致のときの倍率（`0.6`）。`save_memory` と `search_memory` に同じ `session_id` を渡すことで、同じ Redis 内の他プロジェクトのノイズより現在プロジェクトの記憶を上位に押し上げる。両方を `1.0` にすればこのバイアスを完全に無効化できる。
- `skip_code_blocks` — `true` にすると、`save_memory` はトリプルバッククォートフェンス（```` ``` ````）を含むペイロードを拒否して `status: "skipped_code"` を返す。既定は `false`。FastAPI 版 N3MemoryCore 時代の「コードはメモリに入れない」運用を再現したい場合に有効化（git / IDE 履歴に任せ、Redis には散文の決定事項や計画だけを置きたい場合に有用）。

全フィールドの詳細は仕様書 §6 を参照。

## 多言語対応

標準装備、CPU のみ動作、LLM/GPU 不要。同じ単語をどう書こうと、検索と
重複判定の挙動が一致します：

| 層 | 動作 | 実例 |
|---|---|---|
| **NFKC 正規化** | SHA / 埋め込み / BM25 の前に互換形を畳み込む | `ｱﾙﾌｧ` ↔ `アルファ`、`１２３` ↔ `123`、`ﬁ` ↔ `fi` |
| **バイグラム BM25 サイドチャネル** | 区切り文字を持たない文字体系で重複バイグラムを発行 | `記憶装置` → `記憶 憶装 装置`。韓国語 (`안녕하세요`)、タイ語 (`สวัสดี`)、ラオ語、ミャンマー語、クメール語にも適用 |
| **発音区別フォールド** | Latin/Greek/Cyrillic の語は結合マークなしの形でも索引化 | `café` が `cafe` にヒット、`Ångström` が `Angstrom` にヒット |
| **e5-base-v2 埋め込み** | 100 言語以上にまたがる多言語意味空間 | 言語横断パラフレーズ検索 |

これらは `save_memory` と `search_memory` の呼び出しごとに自動で走り
ます。生の `content` フィールドは書き換えません — verbatim 復元
（仕様書 §3.11）は元のバイトを完全一致で返します。

## エンコーディング安全策

ツール本体が動く前に、エンコーディング安全策が 2 層走ります（仕様書 §3.13）。
Free 版の防御を一対一で移植：

1. **stdio の UTF-8 再設定** — モジュール import 時点で `sys.stdin` /
   `sys.stdout` / `sys.stderr` を `encoding="utf-8"` に切り替える。
   Windows 日本語環境では既定の console code page が cp932 のため、
   この措置がないと MCP JSON-RPC チャネル上で非 ASCII バイトが軒並み
   化ける。POSIX 系は既定で UTF-8 のため安全な no-op。
2. **孤立サロゲートのサニタイズ** — `save_memory.content` および
   `search_memory.query` は、`.encode("utf-8")` を伴うあらゆる経路の
   手前で `sanitize_surrogates()` を通過する。孤立 UTF-16 サロゲート
   ハーフ（`U+D800`–`U+DFFF`）は、Windows サブプロセスのパイプが渡し
   てくる UTF-8 バイトを Python のデコーダが `errors="surrogateescape"`
   でマップした場合に発生する。これは `json.loads` を素通りするが、
   SHA1 計算・Redis HSET・埋め込み生成のいずれかの段階で
   `UnicodeEncodeError` を投げ、本来書き込めるはずだったレコードが
   黙って失われる。本関数は `dict` / `list` を再帰処理するため、JSON
   ペイロードに埋まったサロゲートも 1 回でクリーンアップされる。

保存対象が全てサロゲートだった場合、サニタイズ後は空文字列に縮退し、
通常の empty-content 拒否経路に合流する —
`{"status":"error","saved":false,"reason":"empty content"}` を返す。

## ランキング式

```
final_score = (0.7 × cosine_similarity + 0.3 × keyword_relevance) × time_decay × b_local × b_session

time_decay = 2 ^ (-経過日数 / half_life_days)             (既定の半減期: 3 日)
b_local   = clamp(0.5, 2.0, stored_importance + access_boost)
access_boost = min(0.5, access_count × 0.02)
b_session = b_session_match (既定 1.0)   if 行の session_id == 実効 session
          = b_session_mismatch (既定 0.6) それ以外
```

既定の半減期 3 日は TTL（7 日）より短く設定されており、Lite 版でも
`time_decay` は実際に効きます。新鮮なメモリは 1.0、3 日経過で 0.5、
7 日経過（失効直前）で ≈ 0.20 となり、直近の文脈がランキング上位に
押し出されます。

**自動重要度ブースト（アクセス頻度型）**：`search_memory` が
ある記憶を上位 5 件で返すたび、その記憶の `access_count` が +1 され、
次回以降の検索で `b_local` が 0.02 ずつ押し上げられます（上限 +0.5）。
LLM による重要度判定は不要で、「よく使う記憶ほど自然に上位に来る」
自己調整ループが CPU 計算のみで成立します。

## 開発

```bash
# 先に Redis Stack を起動（前提条件の節を参照）してから：
pip install -e ".[dev]"
pytest tests/ -q
```

テストは Redis DB インデックス `0` を対象に動作し（環境変数
`N3MC_REDIS_TEST_URL` で変更可能）、各テスト前後に `FLUSHDB` を行います。
RediSearch は DB 0 以外でインデックスを作成できない
（`Cannot create index on db != 0`）ため、別 DB への分離はできません。
**残したいデータが入っている Redis をテストに使わないでください** —
テスト専用の Redis コンテナを用意してください。Redis に接続できない
場合、テストは実行されずスキップされます。

## Lite 版の拡張・改造

振る舞いを改造したい場合（ランキング式の変更、クロスエンコーダ・リランカーの差し込み、日本語形態素解析の追加など）は、本リポジトリに同梱された設計仕様書を参照してください：

- [`N3MemoryCore_MCP_Spec_JP.md`](https://github.com/NeuralNexusNote/n3mcmcp-lite/blob/main/N3MemoryCore_MCP_Spec_JP.md) — 完全な設計ドキュメント（日本語）
- [`N3MemoryCore_MCP_Spec_EN.md`](https://github.com/NeuralNexusNote/n3mcmcp-lite/blob/main/N3MemoryCore_MCP_Spec_EN.md) — English version

仕様書の付録 A にオプション拡張（クロスエンコーダ・リランカー、保存時チャンキング、HyDE、日本語形態素解析）の差し込み位置と候補ライブラリを記載しています。TTL・重複判定・RediSearch インデックス契約を壊さずに改造したいときの参考資料としてお使いください。

## なぜ N3MemoryCore？（組込みメモリとの違い）

auto-save の **信頼性** という観点では、N3MemoryCore は最近の LLM 製品の
組込みメモリ機能（例：Claude の組込みメモリ）**と本質的に変わりません** ──
どちらも「LLM が自発的に save ツールを呼ぶ」ことに依存し、後述の
*コンプライアンスについて* に書いた非決定性は両方に当てはまります。
差別化は別のところにあります：

| 観点 | 組込みメモリ | N3MemoryCore（Lite） |
|---|---|---|
| **データ所有権** | ベンダ管理サーバ | **自分のマシンの Redis Stack** |
| **クライアントの広さ** | ベンダ製品内のみ | **任意の MCP 準拠クライアント**（Claude Code / Cursor / Cline / Goose / 自前アプリ） |
| **複数 AI の協調** | 単一 AI の記憶 | **`session_id` で複数エージェントが同じ記憶名前空間を共有；タスク終了時は `delete_memories_by_session` で一括掃除** |
| **Verbatim 復元** | 不明（要約される可能性あり） | **親ドキュメント契約 — バイト一致の全文返却** |
| **検索内部** | ブラックボックス | **ハイブリッド BM25 + e5 ベクトル + CJK バイグラム + 時間減衰 + 軽量リランカー、全パラメータ可視・可変** |
| **可視性／制御** | UI 経由のみ | **`list_memories` / `delete_memory` / `delete_memories_by_session` で生レコード操作可** |
| **永続性** | ベンダのサービス継続期間に依存 | **インメモリ Redis、7 日 TTL** — 短命設計だがコンテナを自分で持つ。長期保存が必要なら Pro 版（SQLite・永続）と組み合わせる |
| **チューニング** | 固定 | `half_life_days` / `chunk_threshold` / `dedup_threshold` / リランク重み などすべて編集可能 |

つまり N3MemoryCore Lite を動かす価値は **「より確実な auto-save」ではなく、
「複数 AI が共有 `session_id` の下で協調できる透明・改造可能なワーキング
メモリ層を自分で所有すること」** にあります ── 検索の挙動は編集可能、
verbatim 復元は契約レベルで保証される。（ユーザ投入成果物の長期保存が
必要なら Pro 版と併用する。）

これらの特性が運用にとって重要なら、Lite は元を取ります。「**ある一社の
製品の中で LLM がセッション跨ぎで何かを覚えていてくれればいい**」だけなら、
組込みメモリの方がシンプルです。

## コンプライアンスについて — MCP は「促す」ことしかできない

このサーバから LLM にツール呼び出しを強制することはできません。MCP プロトコル
がサーバ側に与える働きかけ手段は次の 3 つだけです：

1. **`tools/list` の各ツール `description`** — 毎ターン LLM の視野に入る
2. **`instructions` フィールド** — セッション開始時に 1 回送られ、システムレベルの
   ヒントとして渡される
3. **ツール応答のテキスト** — LLM がツールを呼んだときに読む

本サーバはこの 3 つすべてを利用しています：tool description には明示的な指示、
`instructions` にはルール集、`search_memory` / `save_memory` の応答末尾には
auto-save を促す短い reminder を埋め込んでいます。それでも、**LLM がそれに従うかは
非決定的**です。コンプライアンスはモデルのツール呼び出しバイアス、MCP クライアントの
プロンプト構築（`instructions` を要約・破棄するクライアントもある）、ユーザの
プロンプトや `CLAUDE.md` など競合する別の指示に依存します。

実際には **大半のターンでは正しく auto-save されますが、一部のターンでは飛びます**
── 特に短い返答、事実訂正のターン、LLM がユーザの質問に強く集中しているターン。
保存していてほしかった事実が次のセッションで失われていたら、「保存して」と
明示的に言えば取り戻せます。

### 確実な保存が必要なときの 3 つの経路

MCP の建付けの中で、この非決定性を回避する経路は **3 通り**あります：

**経路 1 — ユーザがプロンプトで明示する**（運用回避・即効）
- 「**N3MemoryCore に保存して**」「**メモリに記録して**」をプロンプトに書く
- LLM はだいたいユーザの明示要求には応じる
- 利点: 何のインフラも要らない／今すぐ効く／すべての MCP クライアントで動く
- 欠点: ユーザの認知負荷（毎回明示する必要、自動化されない）

### hook による全会話保存

**経路 2 — Claude Code hook で全会話保存**（Claude Code 専用・決定論的）
- Claude Code には `Stop` などの harness レベル hook があり、これは LLM の判断を**一切介さず**
  harness が決定論的に実行する
- セットアップは Claude Code に依頼するだけ：
  > 「毎ターン終わったら、Claude Code の会話全文を Lite に自動保存して」
- Claude Code が以下を自動構築します：
  - `~/.claude/hooks/save_transcript.py` — `transcript_path` を読んで `n3mc_mcp.database.Database`
    を直接 import し、Lite DB に `save_memory` を呼び出すスクリプト
  - `~/.claude/settings.json` の `hooks.Stop` セクション — 上記スクリプトを毎ターン末に
    `async: true` で実行する設定
- 動作仕様：
  - **Claude が `save_memory` を呼び忘れる事故が構造的に発生しない**（harness が直接呼ぶ）
  - MCP の往復を経由しないため、ツール呼び出しコストゼロ
  - 同一セッションは turn ごとに transcript が成長 → 重複判定（`dedup_threshold`）が
    効くため近似一致は自動却下、DB は **1 セッション 1 エントリ近傍**に収束
  - 200 文字未満の短い transcript は noise として skip
- 利点: 決定論的／LLM の傾向に依存しない／ノイズや忘却の心配なし
- 欠点: Claude Code 専用（Cursor / Windsurf など他クライアントでは別の仕組みが必要）／
  hook プロセスが毎ターン埋め込みモデルをロード（async なので UI ブロックなしだが CPU/IO コストはあり）／
  **Lite は 7 日 TTL なので保存した transcript もその窓内で失効する** — 長期保存が必要なら
  Pro 版（公開予定・SQLite 永続）へ同じ hook で繋ぎ替えるのが筋

**経路 3 — MCP を抜けて first-party API（Anthropic Messages API）に自分で繋ぐ**（アーキテクチャ変更）
- MCP クライアント（Claude Code 等）から外れて、`messages.create` の `tool_use` を自前のアプリで直接制御する
- 「LLM が呼ぼうが呼ぶまいが、コード側で毎ターン `save_memory` を確実に発火する」決定論的動作を組める
- 利点: コードが書いた通り動く／保存保証／どのモデル・どのクライアントとも独立
- 欠点: そのオーケストレーションアプリを書く労力

「**MCP 経由で LLM に丸投げする利便性**」と「**毎ターン確実に保存される保証**」は
トレードオフの両端で、片方を取ったらもう片方は捨てる構造です。本サーバは MCP プロトコル
が許す限りの説得材料を盛り込んでいますが、それ以上の保証はユーザ／クライアント実装者
の選択になります（Claude Code を使っているなら経路 2 が最小コスト）。

## フォークと寄稿

本リポジトリは **public・Apache-2.0 ライセンス** です — 自由に
フォーク・改変・実行してください。フォークから動かすまでの最短経路：

```bash
git clone https://github.com/<YOU>/n3mcmcp-lite
cd n3mcmcp-lite
docker run -d --name redis-stack -p 6379:6379 redis/redis-stack-server:latest
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest tests/ -q                                     # 全 105 テスト・温暖時 ~30 秒
```

CI は push と PR ごとに同じマトリクス（Python 3.10–3.13 × Redis Stack）
を走らせます — [`.github/workflows/test.yml`](./.github/workflows/test.yml)
を参照。コーディング規約・仕様書を契約とする運用方針・PR チェックリスト
の詳細は [`CONTRIBUTING.md`](./CONTRIBUTING.md)（日本語併記）に整理しています。

**フォークを Claude Code から実際に使うには**、上記の
`pip install -e ".[dev]"` 以外に追加設定は不要です：

1. `n3mc-workingmemory` コマンドが `PATH` に通っている
   （`which n3mc-workingmemory` で確認）
2. リポジトリの [`.mcp.json`](./.mcp.json) が既にサーバを宣言している
   ため、`cd n3mcmcp-lite && claude` で次のプロンプトから自動接続
3. 他のクライアント（Claude Desktop、別プロジェクトの `.mcp.json`、
   ツール自動許可）に登録する手順は [クイックスタート Step 3 表](#-クイックスタート--claude-code-に-3-ステップで接続)
   に整理してあります

フォークを別パッケージ名で PyPI に再公開する場合は、
[`pyproject.toml`](./pyproject.toml) の `name`、`[project.urls]`、
console-script 名も忘れずに編集してください。

## トラブルシューティング

### Windows: `pip install --upgrade` が `WinError 32`（ファイル使用中）で失敗する

症状：
```
ERROR: Could not install packages due to an OSError: [WinError 32]
プロセスはファイルにアクセスできません。別のプロセスが使用中です。:
'...\Scripts\n3mc-workingmemory.exe' -> '...\Scripts\n3mc-workingmemory.exe.deleteme'
```

原因：MCP クライアント（Claude Code / Claude Desktop）が
`n3mc-workingmemory.exe` を子プロセスとして握っているため、pip がバイナリ
を置き換えられない。

対処（いずれか）：

1. **MCP クライアントを完全終了する。** Windows ではウィンドウを閉じる
   だけでは不十分。タスクマネージャーで `claude` /
   `n3mc-workingmemory.exe` / コマンドラインに `n3mc-workingmemory` を
   含む `python.exe` をすべて終了させてから `pip install --upgrade` を
   再実行する。
2. **グローバルインストールを使わず `uvx` 経由で動かす** —
   `uvx --from n3memorycore-mcp-lite n3mc-workingmemory` はセッションごと
   に隔離された一時環境で実行されるため、システムレベルの `.exe`
   ロック問題が原理的に発生しない。

これは Windows のファイルロック仕様であり、パッケージング側の不備では
ない — 別の fresh venv に対するインストール（`python -m venv .venv &&
.venv/Scripts/pip install n3memorycore-mcp-lite`）はそのまま通る。

### `~3memorycore-mcp-lite` 警告が pip install 時に出る

```
WARNING: Ignoring invalid distribution ~3memorycore-mcp-lite
```
これは過去のインストールが（典型的には上記のファイルロック問題で）
途中で中断したことを pip が検出している警告。リーディング `~` で残った
ディレクトリは無害だがノイズなので、手動で削除して構わない：

```bash
# Windows
rmdir /s "%LOCALAPPDATA%\Programs\Python\Python312\Lib\site-packages\~3memorycore_mcp_lite-1.5.0.dist-info"
```

（Python のインストール先に応じてパスを調整）

## ライセンス

Apache License 2.0 — [LICENSE](./LICENSE) を参照。
