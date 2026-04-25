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
- 🔄 **会話の文脈を保持** — **7 日間**のワーキングメモリ（Redis TTL により自動失効。長期記憶が必要なら Pro 版）
- ⚡ **自動で動く** — 保存も検索もすべて自動。MCP の `initialize` 応答で配信される振る舞い指示により、ユーザーの指示は不要です
- 🤖 **マルチエージェント対応** — 複数の AI エージェントが 1 つの Redis を共有。`b_local` バイアスで自分の記憶を優先しつつ、他のエージェントの知識も検索できます
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

| ツール           | 用途                                                           |
| ---------------- | -------------------------------------------------------------- |
| `search_memory`  | ハイブリッド検索（ベクトル + BM25、時間減衰ランキング）        |
| `save_memory`    | 短いエントリを保存（7d TTL、完全一致・近似重複は自動拒否）    |
| `list_memories`  | 直近のエントリを新しい順に一覧                                 |
| `delete_memory`  | 特定のエントリを id で削除                                     |
| `repair_memory`  | 欠損時に RediSearch インデックスを作り直す                     |

さらに、MCP の `initialize` 応答で**振る舞いの指示**をクライアントへ
配信します。これにより「各ターン先頭で `search_memory`、応答後に
`save_memory`」という自動保存運用が、Claude Code のフック無しでも
成立します。

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
温存します。`python` が `PATH` 上にあることが前提。

**プラグイン未経由のインストール**（`claude mcp add` / 手動 `.mcp.json` /
Python 不在）の場合は、下記ブロックを `~/.claude/settings.json`（ユーザー
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
  "owner_id": "<uuid>",
  "local_id": "<uuid>",
  "redis_url": "redis://localhost:6379/0",
  "ttl_seconds": 604800,
  "dedup_threshold": 0.95,
  "half_life_days": 3,
  "bm25_min_threshold": 0.1,
  "search_result_limit": 20,
  "min_score": 0.2,
  "search_query_max_chars": 2000,
  "skip_code_blocks": false
}
```

`redis_url` は環境変数 `N3MC_REDIS_URL` でも指定可能（こちらが優先）。

`skip_code_blocks` を `true` にすると、`save_memory` はトリプルバック
クォートフェンス（```` ``` ````）を含むペイロードを拒否して
`status: "skipped_code"` を返します。既定は `false`。FastAPI 版
N3MemoryCore 時代の「コードはメモリに入れない」運用を再現したい場合に
有効化してください（git / IDE 履歴に任せ、Redis には散文の決定事項や
計画だけを置きたい場合に有用）。全フィールドの詳細は仕様書 §6 を参照。

## ランキング式

```
final_score = (0.7 × cosine_similarity + 0.3 × keyword_relevance) × time_decay × b_local

time_decay = 2 ^ (-経過日数 / half_life_days)             (既定の半減期: 3 日)
b_local   = clamp(0.5, 2.0, stored_importance + access_boost)
access_boost = min(0.5, access_count × 0.02)
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

## ライセンス

Apache License 2.0 — [LICENSE](./LICENSE) を参照。
