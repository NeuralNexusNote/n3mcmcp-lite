# N3MemoryCore MCP v1.1.0-lite [Volatile Memory over MCP]
> NeuralNexusNote™ プロダクト — **Lite（揮発型）版**

> **本版の位置付け**：N3MemoryCore MCP の無償 Lite 版。ストレージは **Redis Stack（RediSearch）**、各エントリに **7 日の TTL**、それ以上の永続性はありません。お試し用の公開テスト版 — ディスクに永続保存する有償版との差別化を明確化しています。
>
> **想定ユーザー**：Claude Desktop / Claude Code などあらゆる MCP 対応クライアントのユーザーで、7 日の窓内で会話をまたいで検索可能な短期メモリを欲しい人。
>
> **通信**：Model Context Protocol over stdio (JSON-RPC)。Windows 11 + Python 3.12 + Redis Stack 7.x で検証。

## ⚠️ 免責・配布条件

本ソフトウェア及び仕様書は、いかなる保証もなく **"AS-IS"**（現状のまま）で提供されます。

- **サポート無し**：作者はバグ修正・質問対応・動作保証を一切行いません。
- **無保証・免責**：作者は本ソフトウェアの使用に起因するあらゆる損害（データ損失・業務中断・第三者請求等を含むが限定されない）について一切責任を負いません。
- **自己責任**：本ソフトウェアの使用に伴うリスクは全てユーザーが負担します。
- **変更権**：作者は予告なく本ソフトウェアを修正または提供停止できるものとします。

本ソフトウェアを使用することで、上記条件に同意したものとみなします。

- **ライセンス**：Apache License 2.0。詳細は `LICENSE` を参照。

> **アンインストール**：`pip uninstall n3memorycore-mcp-lite` でパッケージを削除。`docker rm -f redis-stack` で Redis コンテナを停止・削除すれば保存済みメモリは即座に消えます。`${N3MC_DATA_DIR}`（またはプラットフォーム既定のデータディレクトリ）を削除すれば `config.json` も除去されます。MCP クライアント設定の `n3memorycore-lite` エントリも削除してください。
>
> **バックアップは？** Lite 版は **バックアップを想定していません**。7 日の滑り窓で消滅します。永続メモリが必要なら有償版を使ってください。

> **実装に関する質問**：作者への問い合わせはできませんが、本仕様書を Claude に読み込ませて直接質問することで、Claude が実装やカスタマイズを支援できます。

---

## Lite: Volatile Memory（揮発性メモリ）

本節は Lite 版固有のトレードオフをまとめたものです。以降は有償版と同じ構造で記述し、AI による再生成を容易にしています。

| 項目                   | Lite（本仕様）                              | 有償版（別仕様）                         |
| ---------------------- | -------------------------------------------- | ---------------------------------------- |
| ストレージエンジン     | Redis Stack（RediSearch モジュール）           | SQLite + sqlite-vec（ローカルファイル）    |
| 耐久性                 | **エントリごと 7d TTL**・揮発                 | 永続・ディスク保存                       |
| ディスク使用量         | `config.json` のみ（< 1 KB）                   | `n3memory.db` が履歴と共に増加             |
| 外部依存               | ユーザー実行の Redis Stack コンテナ           | なし（セルフコンテイン）                 |
| `time_decay` の実効値  | 有意に効く（既定半減期 3 日：新鮮=1.0、7 日経過 ≈ 0.20） | 有意に効く（既定半減期 90 日）         |
| 再インデックス / 修復  | `FT.CREATE` が冪等・マイグレーションなし       | スキーマ＋モデル移行マーカーあり         |
| 想定用途               | 評価・使い捨てタスク・マーケットプレース       | 継続プロジェクト                         |

**揮発性の契約：**
- Redis への全書き込みで `ttl_seconds`（既定 604 800 = 7 日）の TTL を設定する。
- 主レコード（`mem:<uuid>`）と完全一致ガードキー（`mem:sha:<sha1>`）は同じ TTL を共有し、同時に失効する。
- 失効は Redis に委任 — バックグラウンド掃除ジョブは動かない。
- Redis コンテナをボリュームごと削除すれば全メモリが即座に消える。

**7 日を超えるセッション間保証はない。** 有償版と違い、Lite 仕様では「永続化ハック」を禁止する — TTL を回避するために RDB スナップショット・AOF リライト・外部ダンプを追加しないこと。永続性が必要なら有償版を使う。

---

## セットアップ

### 前提条件

| 項目                      | 要件                                                                        |
| :------------------------ | :-------------------------------------------------------------------------- |
| Python                    | 3.10 以上                                                                   |
| MCP 対応クライアント      | Claude Desktop、Claude Code、その他 MCP クライアント                        |
| Redis Stack               | `localhost:6379` で稼働（`N3MC_REDIS_URL` で変更可能）                      |
| pip パッケージ（自動導入） | `mcp` `redis` `sentence-transformers` `uuid-utils` `platformdirs` `numpy`   |

### クイックスタート

1. Redis Stack を起動（一度きり）：
   ```bash
   docker run -d --name redis-stack -p 6379:6379 redis/redis-stack-server:latest
   ```
2. パッケージをインストール：
   ```bash
   pip install n3memorycore-mcp-lite
   ```
3. MCP クライアント設定にサーバーを登録（[§8](#8-mcp-クライアント設定) 参照）。
4. クライアントを再起動。初回ツール呼び出しは ~400 MB の埋め込みモデルのダウンロードとロードで 30–60 秒かかります。

### データバックアップ

適用外。[Lite: Volatile Memory](#lite-volatile-memory揮発性メモリ) を参照 — 本版は意図的に揮発的です。`config.json`（`owner_id` / `local_id` UUID を含む）が唯一のディスク上の成果物で、再インストール時に同じオーナー ID を維持したい場合にのみコピーすれば十分です。

---

## 1. ビジョン

MCP クライアント向けに「気軽に試せる」メモリエンドポイントを提供する：ハイブリッド検索（ベクトル + RediSearch BM25）、数学的に正しいランキング、7 日での自動ガベージコレクション。MCP サーバーは振る舞いの指示を配信し、接続中の LLM が各ターンの先頭で自動検索、応答後に自動保存を行う — クライアント側フック不要。

Lite は Claude Marketplace で N3MemoryCore MCP の外向き仕様をゼロリスクでデモするために存在する。有償版にアップグレードすると、ストレージ層が Redis から SQLite に差し替わるだけで、MCP としての外向き仕様はそのまま維持される。

> **⚠️ Python 確認**：インストール前に `python --version` で 3.10+ を確認すること。

> **⚠️ 初回ダウンロード**：`sentence-transformers` が初回ツール使用時に `e5-base-v2` モデル（~440 MB）をダウンロードします。その間サーバーは無応答に見えますが、これは想定動作です。キャッシュ後は数秒で起動します。

> **重要：文字数上限（設計制約）**
> - 1 エントリの自動保存：**50–200 文字推奨**（1 エントリ 1 事実）。
> - 検索クエリ：**2,000 文字**（`search_query_max_chars` で調整可能）。
> - ベクトル検索：レコードの先頭 **~2,000 文字** のみがセマンティック検索対象（モデル上限 512 トークン）。それ以降は保存・BM25 検索可能だがベクトル類似性には見えない。
> - **長文貼り付けの扱い**：ユーザーが長文（仕様書・記事・ログ等）を貼った場合、LLM はそれをそのまま保存してはならない。内容を読解し、各要点を短文（~50–200 字）に抽出して、1 事実ごとに `save_memory` を呼ぶこと。

---

## 2. パッケージ構成

```
n3memorycore-mcp-lite/
├── pyproject.toml                  # パッケージメタデータ、エントリポイント 'n3mc-mcp-lite'
├── n3mc_mcp/                       # Python パッケージ
│   ├── __init__.py                 # バージョンマーカー
│   ├── __main__.py                 # エントリポイント: python -m n3mc_mcp
│   ├── server.py                   # MCP サーバー定義 + 5 ツール
│   ├── instructions.py             # initialize 時の振る舞い指示
│   ├── database.py                 # Redis 層：インデックス・CRUD・TTL・重複判定
│   ├── processor.py                # 埋め込み・ランキング・テキスト整形
│   ├── config.py                   # config.json 読み書き + UUID 生成
│   └── paths.py                    # platformdirs 基準の設定保存場所
├── tests/
│   ├── conftest.py                 # Redis Stack 未起動時は自動スキップ
│   ├── test_database.py
│   ├── test_processor.py
│   └── test_server.py
└── examples/
    ├── claude_desktop_config.json
    └── claude_code_mcp.json
```

`config.json` は **ユーザーデータディレクトリ** に置かれます（[§7](#7-データ保存先) 参照）。ディスク上に DB ファイルは存在しません — メモリ状態は Redis 内にのみ存在します。

---

## 3. 技術仕様（変更不可）

> **⚠️ AI は自動改変してはならない**：速度向上・最適化の名目で以下の仕様を自律的に変更してはなりません。埋め込みモデル・ベクトル次元・TTL の変更は人間による `config.json` 手編集でのみ許可されます。

### 3.1 ID 階層

N3MemoryCore は各レコードの出所と文脈を識別する 5 つの ID フィールドを使います：

| ID           | 保存場所         | 生成タイミング                          | 粒度                    | 用途                                                                                          |
| ------------ | ---------------- | --------------------------------------- | ----------------------- | --------------------------------------------------------------------------------------------- |
| `id` (PK)    | Redis ハッシュ    | レコード毎（UUIDv7、時刻順）             | **1 レコード**          | 各メモリの一意識別子 — 削除・重複判定に使用                                                    |
| `owner_id`   | `config.json`    | 初回起動時（UUIDv4）                     | **オーナー**            | 誰のデータか — RediSearch の TAG フィルタで使用                                                |
| `local_id` (agent_id)   | `config.json`    | 初回起動時（UUIDv4）                     | **エージェント / 導入** | インストールの UUIDv4 識別子。互換性のため保存（Lite のランキングでは未使用）。                |
| `session_id` | メモリ内          | サーバープロセス起動時（UUIDv4）         | **サーバープロセス**    | どのサーバープロセスが書いたか（互換性のため保存、Lite のランキングでは未使用）。              |
| `agent_name`   | Redis ハッシュ    | `save_memory` 呼び出し毎（自由文字列）   | **エージェント表示名**  | 人間向けラベル（例：`"claude-desktop"`、`"claude-code"`）。                                    |

### 3.2 埋め込み

- モデル：`intfloat/e5-base-v2` / ベクトル：`float[768]`
- エンコード時は必ず `normalize_embeddings=True` を指定し、L2 正規化ベクトル（ノルム=1）を保証する。コサイン距離を使う場合でも重要：正規化されていない入力は `(1 − cosine_distance)` ↔ 類似度 の等価を崩す。
- **入力プレフィックス（必須）**：プレフィックスなしでは本モデルの精度は著しく低下します：

  ```python
  # 保存時（文書として登録）
  text_to_embed = "passage: " + content

  # 検索時（クエリとして照合）
  text_to_embed = "query: " + keyword
  ```

### 3.3 Redis 接続と TTL

**接続**：`redis_url`（設定フィールド）または環境変数 `N3MC_REDIS_URL`（環境変数が優先）から構築。既定値：`redis://localhost:6379/0`。`decode_responses=False` — クライアントはバイナリ埋め込みペイロードを扱う必要がある。

**TTL**：`mem:<uuid>` への `HSET` は毎回 `EXPIRE mem:<uuid> <ttl_seconds>` と（`PIPELINE` でアトミックに）組み合わせる。兄弟である `mem:sha:<sha1>` ガードは同じパイプライン内で `SET ... EX <ttl_seconds>` にて書き込む。既定 TTL は 604 800 秒（7 日）。

**パイプラインのアトミック性**：3 コマンド（`HSET`・`EXPIRE`・`SET`）を 1 パイプラインで送信するため、「レコードはあるが TTL なし」「sha ガードはあるがレコードなし」のような部分失敗の交錯状態は発生しない。

### 3.4 揮発性の厳守（変更・最適化禁止）

`save_memory` 呼び出し時、HSET + EXPIRE + sha1 ガードを単一パイプラインで完結させる — バッチ化なし、キューイングなし。`save_memory` が応答を返す時点で、キーには有限の TTL が設定されていなければならない。

**以下は絶対に禁止（「性能」や「永続化」目的であっても）：**
- `EXPIRE` なしの書き込み（＝無限 TTL）。
- Lite メモリ寿命の延長のみを目的として、コンテナ削除後も生き残る Redis RDB / AOF 永続化設定を有効にすること。（ユーザーが自分の都合で Redis 設定を選ぶのは自由だが、仕様上はそれに依存しない。）
- 読み込み時の TTL 再延長（`search_memory` 時の `TOUCH` や `EXPIRE`）。
- 1 保存呼び出しを超える範囲でのライトバッファリング / 遅延パイプライン。

**理由**：Lite の差別化は明示的な揮発性である。回避するとプロダクト区分が崩れる。

### 3.5 データレイアウト

```
mem:<uuid>                  HASH
    id              string      UUIDv7（キーサフィックスと同じ）
    content         string      元テキスト（purify 済み）
    timestamp       string      ISO 8601 UTC
    timestamp_epoch number      unix 秒（SORTABLE）
    owner_id        string      TAG
    local_id        string      TAG
    agent_name        string      TAG
    session_id      string      TAG
    embedding       bytes       FLOAT32 × 768（リトルエンディアン）
    TTL                         ttl_seconds（既定 604 800）

mem:sha:<sha1>              STRING
    value = 対応する mem id
    TTL = mem:<uuid> と同値

n3mc_idx                    RediSearch インデックス、ON HASH PREFIX 1 mem:
    SCHEMA:
        content         TEXT
        timestamp_epoch NUMERIC SORTABLE
        owner_id        TAG
        local_id        TAG
        agent_name        TAG
        session_id      TAG
        embedding       VECTOR FLAT 6 TYPE FLOAT32 DIM 768 DISTANCE_METRIC COSINE
```

- **主キー**：UUIDv7（時刻順、挿入時に生成）。リファレンス実装は `uuid_utils.uuid7` を使用。
- **削除のセマンティクス**：`delete_memory` は単一パイプライン内で `mem:<uuid>` とその兄弟 `mem:sha:<sha1>`（`HGET mem:<uuid> content` → sha1 で取得）を削除する。ハッシュが消えれば RediSearch はインデックスエントリを自動的に削除する。

### 3.6 ランキング式

有償版と同一：

```
Final Score = (cos_sim × 0.7 + keyword_relevance × 0.3) × time_decay
```

**cos_sim** — **RediSearch のコサイン距離から直接導出**：

$$cos\_sim = \max(0,\ \min(1,\ 1.0 - cosine\_distance))$$

RediSearch は正規化ベクトルに対し `cosine_distance ∈ [0, 2]` を返す。`[0, 1]` にクランプすることで「正反対方向」の半空間を捨てる（メモリ検索では無関係と扱う）。

**keyword_relevance** — RediSearch の BM25 スコアを `[0.0, 1.0]` に正規化：

1. `|bm25_score| < bm25_min_threshold`（既定 `0.1`）なら `0.0`。
2. それ以外：`|bm25_score| / max(1.0, 結果集合内の max_|bm25_score|)`。

（RediSearch の BM25 は非負だが、FTS5 が負値を返す有償版と同じアルゴリズムに揃えるため `abs()` を保つ。）

**time_decay**：

$$time\_decay = 2^{-\frac{days\_elapsed}{half\_life\_days}}$$

既定 `half_life_days = 3` — 7 日の TTL より意図的に短く設定しており、Lite でも `time_decay` が実際に効く。新鮮なエントリは 1.0、3 日経過で 0.5、7 日経過（失効直前）で ≈ 0.20 となり、直近の文脈がランキング上位に押し出される。これは Lite 固有のチューニングで、永続化を前提とする有償版は 90 日の半減期を維持する。

### 3.7 トークナイズと句読点処理

**トークナイザ**：RediSearch 内蔵のトークナイザ（空白＋句読点区切り、大文字小文字統一）。有償版で使われる Porter ステマーは本版では利用不可 — Lite は RediSearch 既定動作を明示的なトレードオフとして受け入れる。

**クエリ整形** — ユーザー入力クエリは RediSearch へ送る前に `strip_fts_punctuation` を適用し、残った RediSearch 特殊文字をバックスラッシュでエスケープする。`content` はハッシュにそのまま保存する（RediSearch が動的にトークナイズする）。

```python
_PUNCT_STRIP_RE = re.compile(r'[,.<>\{\}\[\]"\':;!@#\$%\^&\*\(\)\-\+=~\|\\/?`]')
_FTS_SPECIAL_RE = re.compile(r'([,.<>\{\}\[\]"\':;!@#\$%\^&\*\(\)\-\+=~\|\\/?])')
```

**空クエリ規則**：整形後に空文字列になった場合、キーワード検索はスキップしベクトル検索のみでランキングする。

### 3.8 重複判定

`save_memory` 呼び出し毎に、以下の順で重複を拒否：

1. **完全一致（O(1)）** — `EXISTS mem:sha:<sha1(content)>`。キーが存在すれば `{"status": "duplicate", "saved": false}` を返す。
2. **近似（意味的）重複** — 埋め込みを計算し、現在の `owner_id` で絞った KNN=1 を `@embedding` に対して実行、`cosine_distance` → `cos_sim` に変換。`cos_sim >= dedup_threshold`（既定 `0.95`）なら `{"status": "near_duplicate", "saved": false, "similarity": <値>}`。

両チェックを通過した場合のみ、HSET + EXPIRE + sha1 ガードのパイプラインへ進む。

### 3.9 起動シーケンスと自己回復

サーバーの `_startup()` は stdio ループがリクエスト受付を始める**前**に、次の手順を順に実行する：

1. **設定読み込み**（`load_config()`）：
   - データディレクトリから `config.json` を読み込む。
   - **ファイルが壊れている（JSON パースエラー）場合**：`stderr` に警告を出し、既定値にフォールバック。有償版と違い、Lite では DB からの回復を**試みない** — Redis は TTL 切れで空かもしれないため。新しい UUIDv4 ペアを生成して書き込む。
   - `N3MC_REDIS_URL` 環境変数のオーバーライドを適用（ファイルより優先）。
   - 欠損フィールドは既定値で埋めて保存。

2. **Redis 接続と ping**：
   - `redis_url` からクライアントを構築。
   - `PING`。**失敗時**は `docker run -p 6379:6379 redis/redis-stack-server:latest` のヒントを `stderr` に出し、非機能クライアントのまま続行。以降のツール呼び出しはすべて同じヒントをエラーで返す。サーバーは生存する — ユーザーは MCP を再起動せずに Redis をホットフィックスできる。

3. **RediSearch インデックスの確保**（`ensure_index()`）：
   - [§3.5](#35-データレイアウト) の通り `FT.CREATE n3mc_idx ON HASH PREFIX 1 mem: SCHEMA ...`。
   - `ResponseError` のメッセージに `"already exists"` を含む場合はキャッチ。それ以外のエラーは再 raise。
   - 冪等：毎起動ごと呼んでも安全。

4. **埋め込みモデル事前ロード**（`get_model()`）：
   - `intfloat/e5-base-v2` をメモリにロードし、初回ツール呼び出しが一度だけのモデルロードで遅くならないようにする。
   - **非致命的**：ロード失敗（オフライン・HF キャッシュ未生成等）時は警告を出して続行。初回 `save_memory` / `search_memory` で遅延リトライする。

手順 1 と 3 は完了してからでないとツール呼び出しを受け付けない。手順 2 と 4 はベストエフォート — Redis 到達不可はプロセスを止めないが、Redis が戻るまでツールは無効。

### 3.10 修復

Lite 版の `repair_memory` ツールは **冪等な薄い操作**：再度 `ensure_index()` を呼ぶだけ。移行マーカー無し、FTS 再構築無し、再埋め込みループ無し — 存在している Redis レコードは RediSearch サイドチャネルですでにインデックス化されており、失効レコードは単に消えている。

返却形：成功時 `{"status": "ok", "message": "index ensured"}`、失敗時 `{"status": "error", "message": "<詳細>"}`。

これは有償版（FTS 句読点移行・vec モデル版移行・未インデックス行修復ループ）からの意図的な簡略化。Lite では最古レコードが高々 7 日なので、移行対象が存在しない。

---

## 4. MCP プロトコル表面

### 4.1 通信

stdio。サーバーは `stdin` から JSON-RPC 行を読み、`stdout` に応答を書く。ログは `stderr`。Windows では起動時に `stdin`/`stdout`/`stderr` を UTF-8 に再設定する。

### 4.2 `initialize` 応答

サーバーが広告する内容：
- `protocolVersion: "2024-11-05"`
- `serverInfo: { name: "n3memorycore-lite", version: "1.1.0-lite" }`
- `capabilities.tools` with `listChanged: false`
- `instructions:` — 振る舞い指示の複数行文字列（[§5](#5-振る舞い指示自動保存戦略) 参照）。**Lite 用文面には「メモリは 7 日で失効する」旨を明示する。**

### 4.3 ツール

`tools/list` で公開する 5 ツール（名前は有償版と同じ）：

| 名前            | 入力                                      | 振る舞い                                                                 |
| --------------- | ----------------------------------------- | ------------------------------------------------------------------------ |
| `search_memory` | `query: string, limit?: int`              | ハイブリッド（ベクトル + BM25）検索、時間減衰ランキング。markdown を返す。 |
| `save_memory`   | `content: string, agent_name?: string`      | 完全 + 近似の重複判定後、HSET + EXPIRE。`ttl_seconds` を含む JSON を返す。 |
| `list_memories` | `limit?: int (既定 20)`                   | 直近エントリを新しい順。markdown を返す。                                 |
| `delete_memory` | `id: string`                              | `DEL mem:<uuid>` + `DEL mem:sha:<sha1>` をアトミックに実行。               |
| `repair_memory` | —                                         | `ensure_index()` を実行。[§3.10](#310-修復) 参照。                         |

全ツールの応答は単一の `TextContent`。`save_memory` / `delete_memory` / `repair_memory` は JSON 文字列、`search_memory` / `list_memories` は人間可読 markdown。

### 4.4 エラー処理

ツール例外はディスパッチ層で捕捉し、先頭 `"Error: "` を付けた `TextContent` で返す。ツールレベル例外で stdio ループがクラッシュすることはない。ツール呼び出し時に Redis 到達不可な場合、ディスパッチャはツール実行を行わず「Redis Stack を起動してください」のヒントを返す。

---

## 5. 振る舞い指示（自動保存戦略）

MCP には Claude Code の `UserPromptSubmit` / `Stop` フック相当が無いため、自動保存の振る舞いは `initialize` 応答で **自然言語の指示** として返す。接続中の LLM がシステム指示として読む。

指示は LLM に以下を要求する：

1. **先に検索** — 各ユーザーターンの先頭で、意図を反映した簡潔なクエリで `search_memory` を呼ぶ。
2. **交互ごとに保存** — 意味のある応答後に `save_memory` を呼び、意図の言い換えと結論（各 50–200 字）を保存。**Lite 文面では「7 日で消える」旨を LLM に明示する。**
3. **長文貼り付けから抽出** — ユーザー貼り付けテキストを個別事実に分割、1 事実ごとに `save_memory`。
4. **ノイズをスキップ** — 挨拶・確認質問・機械的な了解は保存しない。
5. **明示要求を尊重** — 「これは保存しないで」「忘れて」に従う（`delete_memory` を使用）。

完全文面は [`n3mc_mcp/instructions.py`](./n3mc_mcp/instructions.py)。

---

## 6. 設定

初回起動時、データディレクトリ内に `config.json` が自動生成され、`owner_id` / `local_id` にランダムな UUIDv4 が割り当てられる。

完全スキーマ（欠損フィールドは以下の既定値で補完）：

```json
{
  "owner_id":               "<UUIDv4 自動生成>",
  "local_id":               "<UUIDv4 自動生成>",
  "redis_url":              "redis://localhost:6379/0",
  "ttl_seconds":            604800,
  "dedup_threshold":        0.95,
  "half_life_days":         3,
  "bm25_min_threshold":     0.1,
  "search_result_limit":    20,
  "context_char_limit":     3000,
  "min_score":              0.2,
  "search_query_max_chars": 2000
}
```

- `redis_url` — 接続 URL。環境変数 `N3MC_REDIS_URL` がこのフィールドより優先。
- `ttl_seconds` — 新規メモリと sha ガードに適用する TTL（既定 7 日）。下げるのは問題ないが、1 週間を大きく超える値に上げると Lite の目的が崩れるためレビュー時に指摘される。
- `search_result_limit` — `search_memory` が返す最大件数。
- `context_char_limit` — 下流ツールのクライアント側トリミング用に予約（内部では未使用）。
- `min_score` — このスコア未満の結果を除外（既定 `0.2`）。`0.0` で無効化。
- `search_query_max_chars` — クエリから使う最大文字数（既定 `2000`；埋め込みモデルが ~512 トークンで飽和）。

> **1 PC 内の複数アカウント**：OS ユーザーごとに各自の `config.json` で動く。Redis を共有したい場合は両方の設定の `redis_url` を揃える — エントリは `owner_id` TAG フィルタで分離される。

---

## 7. データ保存先

既定ではディスク上には `config.json` のみ：

| OS      | パス                                                         |
| ------- | ------------------------------------------------------------ |
| Windows | `%LOCALAPPDATA%\n3memorycore-lite\`                         |
| macOS   | `~/Library/Application Support/n3memorycore-lite/`          |
| Linux   | `~/.local/share/n3memorycore-lite/`                         |

データディレクトリ内のファイル：
- `config.json` — 設定（唯一のディスク上成果物）

環境変数 `N3MC_DATA_DIR`（絶対パス）で上書き可能。Redis 状態は Redis コンテナが置く場所（既定では無名 Docker ボリュームで、`docker rm -f redis-stack` で消える）に保存される。

---

## 8. MCP クライアント設定

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

設定編集後はクライアントを再起動。Redis Stack は **クライアントがサーバーを起動する前に** 稼働させておくこと — さもないと初回ツール呼び出しは「Redis Stack を起動してください」のヒントを返す。

---

## 9. テスト

```bash
# 1. Redis Stack を起動
docker run -d --name redis-stack -p 6379:6379 redis/redis-stack-server:latest

# 2. 開発依存込みでインストールして pytest
pip install -e ".[dev]"
pytest tests/ -q
```

テストスイートの対象：
- `tests/test_database.py` — RediSearch インデックス、CRUD、TTL、重複、BM25、KNN、シリアライズ。
- `tests/test_processor.py` — コサイン類似度（コサイン距離から）、時間減衰、BM25 正規化、整形、埋め込み。
- `tests/test_server.py` — 隔離された `config.json` とフラッシュ済み Redis DB インデックス 0 に対する MCP ツールディスパッチの E2E。

Redis Stack が `N3MC_REDIS_TEST_URL`（既定 `redis://localhost:6379/0`）で到達不可の場合、テストは失敗ではなく自動スキップされる。

> **⚠️ 破壊的なテスト DB**：RediSearch は DB 0 以外でインデックスを作成できません（`Cannot create index on db != 0`）。このためテストスイートは各テストの前後で DB 0 を `FLUSHDB` します。残したいデータが入っている Redis には `N3MC_REDIS_TEST_URL` を向けないでください — テスト専用コンテナを用意してください。

---

## 付録 A：推奨レビュー手順

AI が本仕様書から実装を再生成した後、以下の順でレビューすること：

1. **データフロー追跡** — AI にコードを読ませ、`save_memory` ツール呼び出しから Redis パイプラインの `EXECUTE` まで、`search_memory` 呼び出しからツール応答までのエンドツーエンド経路を追跡させる。無音のデータ損失が無いこと、全書き込みで TTL が設定されていることを確認。
2. **仕様 ↔ コード対照** — 各ツール（§4.3）について、本仕様書の入力スキーマ・振る舞いを実装と 1 ツールずつ比較。
3. **TTL テスト** — 直接設定オーバーライドで短い `ttl_seconds`（例 5）のエントリを保存し、待機後に `mem:<uuid>` と `mem:sha:<sha1>` が両方消えていることを確認（§3.3 と §3.4 の遵守を証明）。
4. **セッション間テスト（7 日以内）** — セッション 1 で保存、MCP サーバーを再起動（Redis は再起動しない）、セッション 2 で検索。保存エントリが取得できることを確認。
5. **重複判定テスト** — 同一内容を 2 回保存し、2 回目が `status: "duplicate"` を返すことを確認。近似言い換えを保存し、近似重複拒否を確認。
6. **Redis ダウンテスト** — Redis を停止、任意のツールを呼び、サーバーがクラッシュせず「Redis Stack を起動してください」ヒントを返すことを確認。Redis を再起動、MCP プロセスを再起動せずにツールが再び動くことを確認。

これらは人間のレビュアーが操作する手順であり、自動テストではない。

---

## 付録 B：オプション拡張（本 Lite 版には含めない）

Lite 版は §3.6 に記載したハイブリッド + 時間減衰ランカーで意図的に止めている。以下の拡張は **出荷仕様には含まれない** — 将来ユーザーや AI が「試してみたい」となった時に迷わないよう、拡張余地の見取り図として記す。いずれも Lite 版が正しく動作するために必須ではなく、各々が「精度 vs レイテンシ」のトレードである。

- **クロスエンコーダ・リランカー** — `hybrid_search` が返した上位 N 候補を、小型のクロスエンコーダ（例: `cross-encoder/ms-marco-MiniLM-L-12-v2`、~130 MB / `BAAI/bge-reranker-base`、~278 MB）で再ランキングする。現代的なノート PC で `search_memory` 1 回あたり **+100〜300 ms の CPU レイテンシ**（上位 50 件リランク）を加え、言い換えの多いクエリで概ね **精度 +1 ポイント** を得る見立て。差し込み位置は `processor.hybrid_search` の融合スコアソート後、`min_score` フィルタの前。リランカー無効時には従来スコアを素通しできるよう既定フォールバックを残すこと。
- **保存時チャンキング** — `save_memory` が 2000 文字超の本文を受け取った場合、~500 文字のスライディングウィンドウ（重なり ~100 文字）に分割し、各チャンクを独立した `mem:<uuid>` として保存、共通の `source_id` フィールドで `search_memory` 側が再グループ化できるようにする。書き込みは増えるが、長い貼り付け（仕様書・記事・ログ）での再現率は顕著に改善する。現状の Lite 版は振る舞い指示の *「各キー事実を短文として別個に抽出して保存する」* でこれを代替している — チャンキングを入れればその指示は任意化できる。
- **HyDE（Hypothetical Document Embeddings）** — ユーザーのクエリを埋め込む前に、小型 LLM で「仮の回答」を合成し、その回答をクエリの代わりに（または併用で）埋め込む。クエリが短く曖昧で、記憶側が長く具体的な場合に効く。検索ごとに LLM ホップが入るため、「外部 API コール無し」を謳う Lite 版とは相性が悪い — ローカル LLM が既に利用可能な場合に限り選択肢となる。
- **日本語形態素解析** — RediSearch の既定トークナイザは空白・句読点で分割するため、単語間にスペースを持たない日本語テキストは 1 文がほぼ 1 つの BM25 トークンに潰れ、キーワード関連度は実質「部分文字列一致」レベルまで退化する。保存時に形態素解析器で `text` 本文を事前分割する — 候補は `fugashi` + `unidic-lite`（MeCab ベース、~50 MB）、`SudachiPy` + `sudachidict-core`（~70 MB、A/B/C 三段の分割粒度）、バイナリ依存を避けたいなら純 Python の `Janome` — 表層形をスペース結合したものを並列 `text_tokens` TEXT フィールドに格納し、BM25 検索はこのフィールドを参照する。ベクトル検索側は影響なし（e5 埋め込みモデルは日本語を直接扱える）、表示用の生 `text` はそのまま保持する。見込みコストは `save_memory` 1 回あたり +5〜20 ms、日本語クエリでの精度改善は誤差ではなく明確な向上として現れる。日本語を含む運用ではこれは「あれば嬉しい」というより**ほぼ必須**に近く、英語のみの運用であれば省略しても安全である。

4 案はいずれも加算的で、Redis スキーマ既存フィールドや TTL / 重複判定契約の変更を要求しない（日本語トークナイザは並列フィールドを**追加**するのみ）。将来の実装者は機能フラグとして独立に扱い（既定 OFF）、各々ベースラインランカーに対して単独でベンチマークすべきである。
