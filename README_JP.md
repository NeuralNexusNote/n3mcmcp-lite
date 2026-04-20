# N3MemoryCore MCP — Lite（揮発型）

> **N3MC-MCP-Lite は、Claude Code / Cursor / Windsurf などの
> MCP 対応エディタが利用する "外部メモリサーバー" です。**
> MCP Server として動作し、AI が会話やコード文脈を保存・検索できます。

> NeuralNexusNote™ プロダクト — **無償 Lite** 版：Redis Stack を使った
> 揮発性ハイブリッド（ベクトル + BM25）メモリを Model Context Protocol
> サーバーとして提供します。各エントリは 7 日で自動失効します。

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

## Lite 版と有償版

| 版                      | ストレージ                          | 耐久性           | 配布先              |
| ----------------------- | ----------------------------------- | ---------------- | ------------------- |
| **Lite（本リポジトリ）** | Redis Stack（RediSearch）            | 7d TTL・揮発   | Claude Marketplace  |
| 有償版                  | SQLite + sqlite-vec（ローカルファイル） | 永続            | 別途配布             |

MCP の外向き仕様は同じ（5 ツール・同じランキング式）。7 日 TTL と
揮発性 Redis ストレージは**設計上の特長であり制約ではありません** —
以下のワークフローでは Lite 版の方が適しています：

- **エージェント的コード生成ループ** — 失敗した試行や破棄された設計案
  が次タスクに流出しません。`docker restart redis-stack` で一掃可能。
- **マルチエージェント協調** — あるタスクでの決定事項が、無関係な後続
  タスクを汚染しません。
- **試作・実験用途** — 放置すれば 7 日で自動蒸発、削除判断は不要です。

有償版は真逆のユースケースを想定：数ヶ月〜数年にわたる長期的な知識
蓄積、永続性こそが価値となる場面。**プロジェクト境界内のメモリ**が
欲しければ Lite、**継続的なメモリ**が欲しければ有償版をお選びください。

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

ソースからインストールしてください（PyPI 配布は未公開です）：

```bash
git clone https://github.com/NeuralNexusNote/n3mcmcp-lite
cd n3mcmcp-lite
pip install -e .
```

初回起動時に ~400MB の埋め込みモデルが Hugging Face から
`~/.cache/huggingface/` にダウンロードされます。

## クライアント設定

### Claude Desktop

`%APPDATA%\Claude\claude_desktop_config.json`（Windows）または
`~/Library/Application Support/Claude/claude_desktop_config.json`（macOS）：

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

**`.mcp.json` はこのリポジトリに同梱されています。** リポジトリを
クローンしてパッケージをインストールするだけで、Claude Code が
自動的に接続されます — 手動設定は不要です。

他のプロジェクトから利用する場合は、そのプロジェクトの `.mcp.json`
に以下を追加してください：

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

## データ保存先

Lite 版はディスク上に DB を持ちません。メモリは Redis に保存され、
自動で失効します。ディスクには小さな `config.json` だけがプラット
フォーム標準のユーザーデータディレクトリに置かれます：

| OS      | パス                                                        |
| ------- | ----------------------------------------------------------- |
| Windows | `%LOCALAPPDATA%\n3memorycore-lite\`                        |
| macOS   | `~/Library/Application Support/n3memorycore-lite/`         |
| Linux   | `~/.local/share/n3memorycore-lite/`                        |

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
  "search_query_max_chars": 2000
}
```

`redis_url` は環境変数 `N3MC_REDIS_URL` でも指定可能（こちらが優先）。

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

仕様書の付録 B にオプション拡張（クロスエンコーダ・リランカー、保存時チャンキング、HyDE、日本語形態素解析）の差し込み位置と候補ライブラリを記載しています。仕様書があれば AI（あるいは人間）が TTL・重複判定・RediSearch 契約を壊さずにコードを編集できます — 設計意図の一次情報源です。

## ライセンス

Apache License 2.0 — [LICENSE](./LICENSE) を参照。
