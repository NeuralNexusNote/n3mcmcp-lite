# N3MemoryCore MCP — Lite（揮発型）

> NeuralNexusNote™ プロダクト — **無償 Lite** 版：Redis Stack を使った
> 揮発性ハイブリッド（ベクトル + BM25）メモリを Model Context Protocol
> サーバーとして提供します。各エントリは 7 日で自動失効します。

> 🇺🇸 **[English README](./README.md)**
> 🛡️ **[開発ポリシー](./PHILOSOPHY.md)**

---

## Lite 版と有償版

| 版                      | ストレージ                          | 耐久性           | 配布先              |
| ----------------------- | ----------------------------------- | ---------------- | ------------------- |
| **Lite（本リポジトリ）** | Redis Stack（RediSearch）            | 7d TTL・揮発   | Claude Marketplace  |
| 有償版                  | SQLite + sqlite-vec（ローカルファイル） | 永続            | 別途配布             |

MCP としての外向き仕様（5 つのツール、ランキング式）は同じです。
Lite 版は 7 日で中身を捨てる＆ディスクにはごく小さな `config.json`
以外を残さない、試乗版の位置付けです。

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
docker run -d --name redis-stack -p 6379:6379 redis/redis-stack-server:latest
```

コンテナが `localhost:6379` で Redis を公開し、サーバーは自動でこれを
見つけます。

### 2. パッケージのインストール

```bash
pip install n3memorycore-mcp-lite
```

ソースから：

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

プロジェクトの `.mcp.json`：

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
  "half_life_days": 90,
  "bm25_min_threshold": 0.1,
  "search_result_limit": 20,
  "min_score": 0.2,
  "search_query_max_chars": 2000
}
```

`redis_url` は環境変数 `N3MC_REDIS_URL` でも指定可能（こちらが優先）。

## ランキング式

```
final_score = (0.7 × cosine_similarity + 0.3 × keyword_relevance) × time_decay

time_decay = 2 ^ (-経過日数 / half_life_days)   (既定の半減期: 90日)
```

Lite 版はエントリが 7 日で消えるため、実際の `time_decay` は常に
1.0 に近い値になります。

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

## ライセンス

Apache License 2.0 — [LICENSE](./LICENSE) を参照。
