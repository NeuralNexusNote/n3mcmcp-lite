# N3MemoryCore MCP v1.1.0 [Volatile Memory over MCP]
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

1. Redis Stack を起動：
   ```bash
   # 初回のみ（コンテナを作成）：
   docker run -d --name redis-stack -p 6379:6379 redis/redis-stack-server:latest

   # 2 回目以降（コンテナ既存）：
   docker start redis-stack
   ```
   初回作成後は `docker run` を再実行すると `Conflict. The container name "/redis-stack" is already in use` になるので、以降は `docker start` を使用。

   **永続化は禁止 — docker 引数ではなく、サーバー起動時に強制**。
   MCP サーバーは接続のたびに `CONFIG SET appendonly no` と
   `CONFIG SET save ""` を発行します（§3.4 `_enforce_ephemeral`）。
   セッション間で手動で永続化を有効にしても次回 Lite 起動時に元に
   戻されます。以前のバージョンでは安全網として docker コマンドに
   `--appendonly no --save ""` を付与していましたが、`--save ""` の
   空文字列引数が Windows PowerShell および cmd.exe のクォート処理
   で壊れ、実際にコンテナのエントリポイントが起動不能になる事例が
   あったため、docker 引数からは削除し、サーバー側の強制のみを真の
   源としました。**禁止の理由**：揮発性こそが無償 Lite 版と有償永続版
   N3MemoryCore を分ける製品境界であり、Lite は「再起動で本当に
   忘れる 7 日ローリング・スクラッチパッド」であって「TTL 付きの
   永続ストア」ではありません。継続的なメモリが欲しければ有償版に
   移行してもらう設計です。`_enforce_ephemeral` により、**ユーザー
   のシェルや docker フラグに関わらず、Lite を誤って永続ストアに変え
   てしまうことが機構的に不可能**になります。
2. パッケージをインストール（いずれか一つ）：
   - **pip**（グローバルまたは venv）：
     ```bash
     pip install n3memorycore-mcp-lite
     ```
   - **uvx**（事前インストール不要・分離環境 — [`uv`](https://docs.astral.sh/uv/) が必要）：
     ```bash
     uvx --from n3memorycore-mcp-lite n3mc-workingmemory
     ```
   - **Claude Code プラグインマーケットプレイス**（pip/uvx コマンドを手動で打つ必要なし — プラグインが `uvx` 起動を設定するが、`uv` は PATH に必要）：
     ```
     /plugin marketplace add NeuralNexusNote/n3mcmcp-lite
     /plugin install n3memorycore-lite@neuralnexusnote
     ```
3. MCP クライアント設定にサーバーを登録（[§8](#8-mcp-クライアント設定) 参照）。プラグインマーケットプレイス経由でインストールした場合はこのステップ不要 — プラグインが自動登録します。
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
> - **長文貼り付けの扱い（2 モード）**：
>   - **要点抽出（推奨・事実ベースの記憶）**：内容を読解し、各要点を短文（~50–200 字）に抽出して 1 事実ごとに `save_memory` を呼ぶ。検索精度・アクセス頻度ブースト・重要度調整の効果が最大化される。
>   - **全文保存（verbatim recall）**：ユーザーが「この設定資料を後で同じものを表示してほしい」のように**原文をそのまま再現したい**場合は、長文を 1 回の `save_memory` に渡してよい。サーバーは `chunk_threshold`（既定 400 文字）を超える本文を自動的にスライディングウィンドウでチャンク化し、同時に親ドキュメント（`doc:<uuid>`）に全文を verbatim 保存する。後続の `search_memory` で任意のチャンクがヒットすると、親ドキュメントの全文が復元される（[§3.11](#311-全文再現親ドキュメントチャンクパターン) 参照）。

---

## 2. パッケージ構成

```
n3memorycore-mcp-lite/
├── pyproject.toml                  # パッケージメタデータ、エントリポイント 'n3mc-workingmemory'（`n3mc-mcp-lite` は deprecated alias として 1.1.4 のみ存続）
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
mem:<uuid>                  HASH（メモリレコード or チャンク）
    id              string      UUIDv7（キーサフィックスと同じ）
    content         string      元テキスト（purify 済み；チャンクなら部分文）
    content_ngram   string      CJK バイグラム展開（BM25 用のサイドチャネル）
    timestamp       string      ISO 8601 UTC
    timestamp_epoch number      unix 秒（SORTABLE）
    owner_id        string      TAG
    local_id        string      TAG
    agent_name        string      TAG
    session_id      string      TAG
    importance      number      0.5〜2.0（save_memory 時指定、既定 1.0）
    access_count    number      検索ヒット回数（頻度ブースト用）
    parent_id       string      TAG 親ドキュメント id（独立メモリなら空文字列）
    embedding       bytes       FLOAT32 × 768（リトルエンディアン）
    TTL                         ttl_seconds（既定 604 800）

mem:sha:<sha1>              STRING
    value = 対応する mem id（独立メモリのみ；チャンクは付けない）
    TTL = mem:<uuid> と同値

doc:<uuid>                  HASH（親ドキュメント — 長文 verbatim 保存用。RediSearch 非インデックス）
    id              string      UUIDv7
    content         string      元の全文 verbatim
    timestamp       string      ISO 8601 UTC
    timestamp_epoch number      unix 秒
    owner_id        string
    local_id        string
    agent_name        string
    session_id      string
    chunk_count     number      生成されたチャンク数
    TTL                         ttl_seconds

docsha:<sha1>               STRING
    value = 対応する doc id（親全文の完全一致ガード）
    TTL = doc:<uuid> と同値

n3mc_idx                    RediSearch インデックス、ON HASH PREFIX 1 mem:
    SCHEMA:
        content         TEXT weight 1.0
        content_ngram   TEXT weight 0.8
        timestamp_epoch NUMERIC SORTABLE
        owner_id        TAG
        local_id        TAG
        agent_name        TAG
        session_id      TAG
        importance      NUMERIC
        access_count    NUMERIC
        parent_id       TAG
        embedding       VECTOR FLAT 6 TYPE FLOAT32 DIM 768 DISTANCE_METRIC COSINE
```

- **主キー**：UUIDv7（時刻順、挿入時に生成）。リファレンス実装は `uuid_utils.uuid7` を使用。
- **親ドキュメントは非インデックス**：`doc:` / `docsha:` は RediSearch の `PREFIX 1 mem:` に該当しないため検索インデックス対象外。検索ヒットは常にチャンク（`mem:*`）経由で、親は post-lookup で引く（[§3.11](#311-全文再現親ドキュメントチャンクパターン)）。
- **削除のセマンティクス**：`delete_memory` は ID のキーを見て自動分岐する：
  - ID が `doc:<uuid>` に存在 → 親ドキュメント＋`docsha:<sha1>`＋`parent_id` に一致する全チャンクを単一パイプラインで削除（cascade）。
  - それ以外 → `mem:<uuid>` とその sha ガードを削除（従来挙動）。

### 3.6 ランキング式

有償版と同一：

```
Final Score = (cos_sim × 0.7 + keyword_relevance × 0.3) × time_decay × b_local
```

ここで `b_local` は **重要度係数**：

```
b_local = clamp(0.5, 2.0, stored_importance + access_boost)
access_boost = min(access_count_max_boost, access_count × access_count_weight)
```

- `stored_importance`：`save_memory` 時に指定（既定 `1.0`、範囲 `0.5〜2.0`）。
- `access_boost`：**CPU のみで自動算出される頻度ブースト**。`search_memory` がその記憶を上位 `ttl_refresh_top_k` 件に含めるたび `access_count` が +1 され、次回以降の検索で `access_count × access_count_weight`（既定 `0.02`）だけ `b_local` を押し上げる（上限 `access_count_max_boost = 0.5`）。LLM の介在なく「よく使われる記憶ほど上位に来る」自己調整ループが成立する。

設定で `access_count_enabled: false` にすればブーストを無効化できる（`stored_importance` のみで重み付け）。

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

**トークナイザ**：RediSearch 内蔵のトークナイザ（空白＋句読点区切り、大文字小文字統一）。有償版で使われる Porter ステマーは本版では利用不可。

**CJK バイグラム展開**：日本語・中国語は単語間スペースがないため、素の RediSearch トークナイザでは 1 文が 1 トークンに潰れ BM25 がほぼ効かない。これを補うため、保存時に `content` 中の連続 CJK ランを **オーバーラップバイグラム**（例：「記憶装置」→「記憶 憶装 装置」）に展開し、並列の `content_ngram` TEXT フィールドに格納する。BM25 検索はクエリ側にも同じ展開を適用した上で `@content:(...) | @content_ngram:(...)` の OR クエリを走らせるため、日本語の部分一致が RediSearch 内で機能する。ベクトル検索側には一切干渉しない（e5 埋め込みは日本語を直接扱える）。

**クエリ整形** — ユーザー入力クエリは RediSearch へ送る前に `strip_fts_punctuation` を適用し、CJK バイグラム展開ののち残った RediSearch 特殊文字をバックスラッシュでエスケープする。`content` はハッシュにそのまま（verbatim）保存する（RediSearch が動的にトークナイズする）。

```python
_PUNCT_STRIP_RE = re.compile(r'[,.<>\{\}\[\]"\':;!@#\$%\^&\*\(\)\-\+=~\|\\/?`]')
_FTS_SPECIAL_RE = re.compile(r'([,.<>\{\}\[\]"\':;!@#\$%\^&\*\(\)\-\+=~\|\\/?])')
```

**空クエリ規則**：整形後に空文字列になった場合、キーワード検索はスキップしベクトル検索のみでランキングする。

### 3.8 重複判定

`save_memory` は入力本文長が `chunk_threshold`（既定 400 文字）以下かどうかで分岐する。

**(A) 単一チャンクパス**（本文が `chunk_threshold` 以下）：以下の順で重複を拒否する。

1. **完全一致（O(1)）** — `EXISTS mem:sha:<sha1(content)>`。キーが存在すれば `{"status": "duplicate", "saved": false}` を返す。
2. **近似（意味的）重複** — 埋め込みを計算し、現在の `owner_id` で絞った KNN=1 を `@embedding` に対して実行、`cosine_distance` → `cos_sim` に変換。`cos_sim >= dedup_threshold`（既定 `0.95`）なら `{"status": "near_duplicate", "saved": false, "similarity": <値>}`。

両チェックを通過した場合のみ、HSET + EXPIRE + sha1 ガードのパイプラインへ進む。

**(B) マルチチャンクパス**（本文が `chunk_threshold` 超）：重複判定は**親ドキュメント全文**のレベルでのみ行う。

1. **親レベル完全一致（O(1)）** — `EXISTS docsha:<sha1(full_text)>`。キーが存在すれば `{"status": "duplicate", "saved": false, "parent_id": "<既存>"}` を返す。
2. チャンクは個別の sha ガードを付けず、近似重複チェックもバイパスする。理由：スライディングウィンドウで生成される隣接チャンクは設計上ほぼ重複しており、個別に dedup すると自分自身を却下してしまう。

親レベル全文一致を通過した場合、単一 `save_memory` 応答内で：
- `doc:<parent_id>` に HSET + EXPIRE + `docsha:<sha1>` ガードを書く
- `mem:<chunk_id>` を各チャンクにつき HSET + EXPIRE（sha ガード無し、`parent_id` フィールドに親 ID）

### 3.9 起動シーケンスと自己回復

サーバーの `_startup()` は stdio ループがリクエスト受付を始める**前**に、次の手順を順に実行する：

1. **設定読み込み**（`load_config()`）：
   - データディレクトリから `config.json` を読み込む。
   - **ファイルが壊れている（JSON パースエラー）場合**：`stderr` に警告を出し、既定値にフォールバック。有償版と違い、Lite では DB からの回復を**試みない** — Redis は TTL 切れで空かもしれないため。新しい UUIDv4 ペアを生成して書き込む。
   - `N3MC_REDIS_URL` 環境変数のオーバーライドを適用（ファイルより優先）。
   - 欠損フィールドは既定値で埋めて保存。

2. **Redis 接続と ping**：
   - `redis_url` からクライアントを構築。
   - `PING`。**失敗時**は `stderr` にヒント（初回：`docker run -d --name redis-stack -p 6379:6379 redis/redis-stack-server:latest` ／ 再起動：`docker start redis-stack`）を出し、非機能クライアントのまま続行。以降のツール呼び出しはすべて同じヒントをエラーで返す。サーバーは生存する — ユーザーは MCP を再起動せずに Redis をホットフィックスできる。

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

### 3.11 全文再現（親ドキュメント＋チャンクパターン）

`save_memory` に渡された本文が `chunk_threshold`（既定 400 文字）を超えた場合、サーバーは以下を自動実行する：

1. 本文全体を `chunk_threshold` 文字のスライディングウィンドウ（オーバーラップ `chunk_overlap`、既定 100 文字）に分割する。
2. 新規 `parent_id`（UUIDv7）を採番し、`doc:<parent_id>` に全文 verbatim（purify のみ適用・切り詰め無し）を HSET で書く。`docsha:<sha1(full_text)>` に親レベル完全一致ガードを設定する。
3. 各チャンクを個別の `mem:<chunk_id>` として HSET + EXPIRE で書き、`parent_id` TAG フィールドに親 ID を格納する（sha ガード・近似 dedup はスキップ）。

`search_memory` 側の整合：

- `hybrid_search` は通常どおりチャンクを対象に BM25 + ベクトル検索を行い、各候補の `parent_id` を結果辞書に含める。
- ディスパッチャは結果を走査し、`parent_id` が非空のヒットについて：
  - 同じ `parent_id` が既出 → 重複として破棄（最高スコアのヒットのみ残す）
  - 初出 → `doc:<parent_id>` を HGET し、`content` を親の全文に置換、ID も親 ID に差し替える
- 最終出力は markdown 先頭に `[doc]` タグ付きで表示される。

`list_memories` 側の整合：

- 標準のインデックス検索は `-@parent_id:{*}` フィルタでチャンクを除外する（独立メモリのみ拾う）。
- これに `SCAN doc:*` でオーナー一致の親ドキュメントを重ねて timestamp 降順でマージする。
- 親は `[doc×N]`（N=`chunk_count`）タグ付きで表示される。

`delete_memory` 側の整合：

- ID が `doc:<uuid>` キーを指していれば、親＋`docsha:`＋`@parent_id:{<id>}` にマッチする全チャンクを単一パイプラインで連鎖削除する。
- それ以外は従来どおり `mem:<uuid>` とその sha ガードを削除する。

**設計上の不変条件**：
- 親レコードは検索インデックスに含めない（`PREFIX 1 mem:` の外に置く）。これによりランキング式は常にチャンク本文に対して計算され、親の長大な本文が time_decay / BM25 を歪めない。
- `stored_importance` / `access_count` はチャンクにのみ付く。親ドキュメントは「verbatim の箱」であり、ランキング要素を持たない。
- 親が生存している限り、チャンクが 1 個でもヒットすれば全文が返る。親の TTL が切れた場合、孤児チャンクは個別メモリとして（短文ヒットの形で）表示される（退行ではなく graceful degrade）。

**用途**：ユーザーが「この設定資料 / 仕様書 / 記事をあとで verbatim に取り出したい」と要求する場面。要点抽出との使い分けは [§1 の「長文貼り付けの扱い（2 モード）」](#1-ビジョン) を参照。

---

## 4. MCP プロトコル表面

### 4.1 通信

stdio。サーバーは `stdin` から JSON-RPC 行を読み、`stdout` に応答を書く。ログは `stderr`。Windows では起動時に `stdin`/`stdout`/`stderr` を UTF-8 に再設定する。

### 4.2 `initialize` 応答

サーバーが広告する内容：
- `protocolVersion: "2024-11-05"`
- `serverInfo: { name: "n3memorycore-lite", version: "1.1.0" }`
- `capabilities.tools` with `listChanged: false`
- `instructions:` — 振る舞い指示の複数行文字列（[§5](#5-振る舞い指示自動保存戦略) 参照）。**Lite 用文面には「メモリは 7 日で失効する」旨を明示する。**

### 4.3 ツール

`tools/list` で公開する 5 ツール（名前は有償版と同じ）：

| 名前            | 入力                                      | 振る舞い                                                                 |
| --------------- | ----------------------------------------- | ------------------------------------------------------------------------ |
| `search_memory` | `query: string, limit?: int`              | ハイブリッド（ベクトル + BM25）検索、時間減衰＋頻度ブーストランキング、語彙リランク。チャンクヒットは親ドキュメントに折りたたまれ全文 verbatim で返る（[§3.11](#311-全文再現親ドキュメントチャンクパターン)）。markdown を返す。 |
| `save_memory`   | `content: string, agent_name?: string, owner_id?: string, importance?: number` | 本文長 ≤ `chunk_threshold` なら完全 + 近似重複判定後、HSET + EXPIRE し `ttl_seconds` を含む JSON を返す。超過なら**親ドキュメント**を `doc:<id>` に verbatim 保存し、スライディングウィンドウでチャンク化した `mem:<id>` を並列登録、`{"saved": true, "parent_id": "...", "chunks": N, "saved_count": N, "ids": [...], "ttl_seconds": ...}` を返す。`owner_id` を指定した場合、サーバー設定と不一致なら `{"status":"error","saved":false,"reason":"owner_id mismatch"}` を返す。`importance` は 0.5〜2.0 の範囲でクランプされ、保存時スコア重みに反映される。 |
| `list_memories` | `limit?: int (既定 20)`                   | 親ドキュメントと独立メモリを新しい順で並べた markdown。親は `[doc×N]` タグ付き。チャンクは隠蔽（`-@parent_id:{*}` フィルタ）。 |
| `delete_memory` | `id: string`                              | ID が親（`doc:<uuid>`）なら親＋`docsha:`＋該当 `parent_id` を持つ全チャンクを連鎖削除。それ以外は `mem:<uuid>` とその sha ガードをアトミック削除。 |
| `repair_memory` | —                                         | `ensure_index()` を実行。[§3.10](#310-修復) 参照。                         |

全ツールの応答は単一の `TextContent`。`save_memory` / `delete_memory` / `repair_memory` は JSON 文字列、`search_memory` / `list_memories` は人間可読 markdown。

### 4.4 エラー処理

ツール例外はディスパッチ層で捕捉し、先頭 `"Error: "` を付けた `TextContent` で返す。ツールレベル例外で stdio ループがクラッシュすることはない。ツール呼び出し時に Redis 到達不可な場合、ディスパッチャはツール実行を行わず「Redis Stack を起動してください」のヒントを返す。

---

## 5. 振る舞い指示（自動保存戦略）

MCP には Claude Code の `UserPromptSubmit` / `Stop` フック相当が無いため、自動保存の振る舞いは `initialize` 応答で **自然言語の指示** として返す。接続中の LLM がシステム指示として読む。

指示は LLM に以下を要求する：

1. **先に検索し、想起したら明示する** — 各ユーザーターンの先頭で、意図を反映した簡潔なクエリで `search_memory` を呼ぶ。検索結果を実際に応答に活用した（以前の記憶を想起して答えた）場合は、返答冒頭にユーザーの言語で一言そえる。例:「前回の回答がメモリに保存されています。」「以前の会話から該当情報を取り出しました。」／英語なら "Pulling this from earlier memory in this session."。**関連メモリが無かった / 想起内容を使わなかった場合は告知しない。** 単に「検索した」ことを述べるのは禁止 — 「想起した」ことだけを述べる。
2. **毎ターン自動保存（許可を求めない）** — 保存は静かに自動で行う。ユーザーが「保存して」「覚えておいて」と言う必要は一切なく、**既定で保存する**。保存してよいか確認する質問もしない。各意味のあるターン後に `save_memory` を呼び、(a) ユーザーの意図・質問の言い換え、(b) **自分が生成した実質的な出力** — 特にユーザーが後で参照しそうな創作・生成コンテンツ（世界観設定、キャラクター設定、設計スケッチ、コードアーキテクチャ、リサーチ要約、アウトライン等。1〜2 文を超えて作ったものは保存）、(c) 確立した事実・嗜好・未解決の問いを保存。1 事実 = 1 `save_memory` 呼び出し、各 50–200 字（長文は 3 番ルール）。重複はサーバー側で自動却下されるため「多めに保存」が安全。**Lite は 7 日で消えるローリングスクラッチパッド**である旨を LLM に明示。
3. **長文は全文 1 回で保存（verbatim 復元）** — ターンで発生した長文 — ユーザー貼り付け（仕様・記事・ログ・コード）**または自分が生成した長文（創作設定・世界観・キャラシ・設計ドキュメント）** — でユーザーが後で原文を取り出しそうなものは、**全文を単一の `save_memory` 呼び出しに渡す**。サーバーが自動的に親ドキュメント＋チャンク化を行い、検索はチャンクで、復元は親本文で verbatim 返却する（[§3.11](#311-全文再現親ドキュメントチャンクパターン)）。目安：**~400 字超 → 全文 1 回保存**。それ未満の要約可能な内容は 2 番ルールで短文複数保存。長文 verbatim 候補を多数の短い要約に分割してはならない（復元忠実度が崩れる）。
4. **ツールエラー時は先にユーザーへ通知（長文生成を先走らない）** — `search_memory` / `save_memory` がサーバーエラーを返した場合（Redis 不達・接続拒否・タイムアウト・「start Redis Stack」ヒント等）、**長文生成に入る前に停止し、ユーザーの言語で端的に通知する**。理由：バックエンド不稼働時は後続の `save_memory` も全て暗黙失敗するため、長文設計ドキュメントや創作設定を作っても **セッション閉鎖時に全て失われる**（ユーザーは記憶層の故障に気付けない）。通知内容：(a) 失敗事実（例「Redis Stack が停止しているようです。メモリ保存・検索が使えません。」）、(b) ツールが返した復旧ヒント（例 `docker run -p 6379:6379 redis/redis-stack-server:latest`）、(c) メモリ無効のまま続行するか・復旧まで待つかをユーザーに確認。短い事実回答は続行してよいが、一度は保存失敗中である旨を必ず触れる。
5. **ノイズをスキップ** — 挨拶・確認質問・機械的な了解は保存しない。
6. **明示要求を尊重** — 「これは保存しないで」「忘れて」に従う（`delete_memory` を使用）。

完全文面は [`n3mc_mcp/instructions.py`](./n3mc_mcp/instructions.py)。

---

## 6. 設定

初回起動時、データディレクトリ内に `config.json` が自動生成され、`owner_id` / `local_id` にランダムな UUIDv4 が割り当てられる。

完全スキーマ（欠損フィールドは以下の既定値で補完）：

```json
{
  "owner_id":                 "<UUIDv4 自動生成>",
  "local_id":                 "<UUIDv4 自動生成>",
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
  "rerank_phrase_weight":     0.2
}
```

- `redis_url` — 接続 URL。環境変数 `N3MC_REDIS_URL` がこのフィールドより優先。
- `ttl_seconds` — 新規メモリと sha ガードに適用する TTL（既定 7 日）。下げるのは問題ないが、1 週間を大きく超える値に上げると Lite の目的が崩れるためレビュー時に指摘される。
- `search_result_limit` — `search_memory` が返す最大件数。
- `context_char_limit` — 下流ツールのクライアント側トリミング用に予約（内部では未使用）。
- `min_score` — このスコア未満の結果を除外（既定 `0.2`）。`0.0` で無効化。
- `search_query_max_chars` — クエリから使う最大文字数（既定 `2000`；埋め込みモデルが ~512 トークンで飽和）。
- `chunk_threshold` / `chunk_overlap` — スライディングウィンドウのサイズとオーバーラップ（既定 400 / 100 文字）。本文長がこの閾値を超えた場合に親ドキュメント＋チャンク化（[§3.11](#311-全文再現親ドキュメントチャンクパターン)）に入る。
- `access_count_enabled` / `access_count_weight` / `access_count_max_boost` — アクセス頻度ブーストの有効化・係数・上限（[§3.6](#36-ランキング式)）。`false` で完全無効化、`stored_importance` のみが重みになる。
- `ttl_refresh_on_search` / `ttl_refresh_top_k` — 検索上位 K 件に対する TTL 再設定と `access_count` インクリメント。再設定は既存エントリの TTL をリセットするのみで、新規エントリの寿命を超えて延長することはない。
- `lexical_rerank_enabled` / `rerank_weight` / `rerank_phrase_weight` — 融合スコア後の軽量語彙リランカー（[§3.6](#36-ランキング式)）。`false` で従来スコア素通し。

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
      "command": "n3mc-workingmemory",
      "args": []
    }
  }
}
```

### Claude Code

等価な導入方法が 3 通りあります。いずれか一つを選択すること（併用不可）。

**(a) プラグインマーケットプレイス（推奨 — 手動設定ファイル不要）**

```
/plugin marketplace add NeuralNexusNote/n3mcmcp-lite
/plugin install n3memorycore-lite@neuralnexusnote
```

プラグインに同梱された `plugin.json` が `uvx --from n3memorycore-mcp-lite n3mc-workingmemory` 経由でサーバーを起動する。`uv` が PATH にあることが前提。

**(b) プロジェクトの `.mcp.json`（手動 — リポジトリをクローン、または pip インストール済みの場合）**

```json
{
  "mcpServers": {
    "n3memorycore-lite": {
      "type": "stdio",
      "command": "n3mc-workingmemory",
      "args": []
    }
  }
}
```

**(c) プロジェクトの `.mcp.json` を uvx 経由で（事前インストール不要）**

```json
{
  "mcpServers": {
    "n3memorycore-lite": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "n3memorycore-mcp-lite", "n3mc-workingmemory"]
    }
  }
}
```

設定編集後はクライアントを再起動。Redis Stack は **クライアントがサーバーを起動する前に** 稼働させておくこと — さもないと初回ツール呼び出しは「Redis Stack を起動してください」のヒントを返す。

### ツール自動許可（Claude Code 固有）

Claude Code は既定で各 MCP ツール呼び出しに対してユーザー承認プロンプトを出す。**「AI が意識せず保存・検索する」自動ループを成立させるには、ツールを事前許可する必要がある** — そうしないと `save_memory` / `search_memory` のたびに Yes/No ダイアログで AI が停止する（ユーザーが席を外していれば動作不能）。

**プラグイン経由インストールは自動設定** — `/plugin install n3memorycore-lite@neuralnexusnote` でインストールすると、プラグインの `SessionStart` フック [`hooks/install_permissions.py`](./plugins/n3memorycore-lite/hooks/install_permissions.py) が `~/.claude/settings.json` の `permissions.allow` に 5 ツールを冪等追加する。1 件でも欠けていれば追記、すべて揃っていれば無書き込み。既存フィールドは温存。`python` が `PATH` 上にあれば動作する。

**プラグイン未経由のインストール**（`claude mcp add` / 手動 `.mcp.json` / Python 不在）では下記ブロックを `~/.claude/settings.json`（ユーザーグローバル — 推奨）または `.claude/settings.json`（プロジェクトスコープ）に手動追記：

```json
{
  "permissions": {
    "allow": [
      "mcp__n3memorycore-lite__search_memory",
      "mcp__n3memorycore-lite__save_memory",
      "mcp__n3memorycore-lite__list_memories",
      "mcp__n3memorycore-lite__delete_memory",
      "mcp__n3memorycore-lite__repair_memory"
    ]
  }
}
```

> Claude Desktop にはツール単位のパーミッションゲートが無いため、この設定は不要。`plugin.json` に `permissions` フィールドを持たせる配布は Claude Code 側未対応（2026-04 時点）のため、プラグインでは `SessionStart` フック経由でユーザー `settings.json` を冪等パッチする方式を採る（上記「プラグイン経由インストールは自動設定」を参照）。

---

## 9. テスト（pytest）

> **目的**: 手動 Evidence Report（§10）を補完する、反復実行可能な自動回帰テスト。レイヤーごとに責務を切り分け、MCP ツール層の E2E は最後の砦とする。

### 実行方法

```bash
# 1. Redis Stack を起動（RediSearch は DB 0 しか索引できない）
#    初回：docker run -d --name redis-stack -p 6379:6379 redis/redis-stack-server:latest
#    2 回目以降：docker start redis-stack
docker start redis-stack 2>/dev/null || docker run -d --name redis-stack -p 6379:6379 redis/redis-stack-server:latest

# 2. 開発依存込みでインストールして pytest
pip install -e ".[dev]"
pytest tests/ -q
```

Redis Stack が `N3MC_REDIS_TEST_URL`（既定 `redis://localhost:6379/0`）で到達不可の場合、テストは失敗ではなく自動スキップされる。

> **⚠️ 破壊的なテスト DB**：RediSearch は DB 0 以外でインデックスを作成できません（`Cannot create index on db != 0`）。このためテストスイートは各テストの前後で DB 0 を `FLUSHDB` します。残したいデータが入っている Redis には `N3MC_REDIS_TEST_URL` を向けないでください — テスト専用コンテナを用意してください。

### ディレクトリ構成

```
n3mcmcp-lite/
└── tests/
    ├── conftest.py          # 共通フィクスチャ：隔離 data dir、Redis URL 上書き、ダミーベクトル
    ├── test_database.py     # Layer 1: Redis / RediSearch 単体（CRUD、スキーマ、TTL、重複、BM25、KNN）
    ├── test_processor.py    # Layer 2: ランキング数式、埋め込み、purify、チャンク化、リランク
    └── test_server.py       # Layer 3: MCP ツールディスパッチ E2E（隔離 `config.json` + flush 済み DB 0）
```

### Layer 1: `tests/test_database.py`（34 テスト）

| テストクラス | テスト内容 | カバレッジ |
|---|---|---|
| `TestIndexSetup` | RediSearch インデックス生成、再実行冪等性、欠損フィールドの追加 | スキーマ管理 |
| `TestInsertAndRetrieve` | INSERT→COUNT、ハッシュ整合性、embedding 無し挿入、`parent_id` 格納 | CRUD |
| `TestDedup` | 完全一致 SHA 衝突、近似類似度閾値、親ドキュメント SHA ガード | 重複排除 |
| `TestDelete` | 単体削除、親→チャンクカスケード、`docsha:` ガード掃除 | トランザクション保護 |
| `TestTTL` | 7 日 TTL 設定、検索ヒット時の `EXPIRE` リフレッシュ、期限切れ消失 | TTL |
| `TestFTS` | 句読点除去、BM25 スコア、短クエリ、CJK バイグラム展開によるヒット | FTS / 日本語 |
| `TestVectorSearch` | KNN 結果、空 DB 検索、`owner_id` フィルタ、`-@parent_id:{*}` 除外 | KNN 検索 |
| `TestCjkBigramExpand` | ひらがな・カタカナ・漢字混在のバイグラム展開、境界処理 | トークナイズ |
| `TestAccessCount` | `access_count` の HINCRBY、上位 N のみ加算、キャップ | 自動 importance |
| `TestSerialization` | ベクトルのバイナリ往復、f32 LE パック | シリアライズ |
| `TestSha1` | SHA1 ヘキサ、空文字、非 ASCII UTF-8、長文 | ダイジェスト |

### Layer 2: `tests/test_processor.py`（42 テスト）

| テストクラス | テスト内容 | カバレッジ |
|---|---|---|
| `TestCosineSim` | 同一ベクトル→1.0、直交→0.0、負値クランプ、RediSearch 距離→類似度 | 距離変換 |
| `TestTimeDecay` | 現在→1.0、半減期、フロア値、不正タイムスタンプ→1.0 | 半減期 |
| `TestKeywordRelevance` | 閾値以下切り捨て、正規化、ゼロ最大値 | BM25 正規化 |
| `TestFinalScore` | `(cos·0.7 + bm25·0.3)·decay·b_local`、`b_local` クランプ | ランキング式 |
| `TestAccessCountBoost` | `stored_importance + access_boost`、0.5〜2.0 クランプ、無効化フラグ | 自動 importance |
| `TestLexicalRerank` | 語彙被覆、フレーズブースト、短文優先、ゼロ被覆は非劣化 | 軽量リランカー |
| `TestPurification` | 複数行コードブロック→`[code omitted]`、インラインコード保持、複数ブロック | 整形 |
| `TestChunkText` | 閾値以下→単一、長文→分割＋オーバーラップ、境界一致 | 親-チャンク |
| `TestEmbedding` | `passage:`/`query:` プレフィクス、ベクトル次元、同一テキスト類似度 | 埋め込み |

### Layer 3: `tests/test_server.py`（15 テスト）

| テストクラス | テスト内容 | カバレッジ |
|---|---|---|
| `TestToolRegistration` | 5 ツール登録、スキーマ型、description 非空 | MCP 登録 |
| `TestSaveAndSearch` | 保存→検索往復、完全重複拒否、空内容拒否 | 単一チャンク |
| `TestListAndDelete` | 新着 3 件の列挙、存在しない ID の削除 | 一覧・削除 |
| `TestRepair` | 空 DB に対する `repair_memory`→ok | 修復 |
| `TestUnknownTool` | 未登録ツール名→エラー文字列 | ディスパッチ |
| `TestParentDocRecall` | 架空都市「不知火」設定の一字一句復元、チャンク→親集約、`[doc×N]` 表示、親→チャンク削除カスケード | 親ドキュメント（§3.11） |

### 実行例

```bash
# 全テスト
pytest tests/ -q

# 単一レイヤー
pytest tests/test_database.py -v

# 遅い埋め込みテストをスキップ
pytest tests/ -v -k "not TestEmbedding"
```

> **⚠️ Evidence Report との関係**: 自動テストの不合格は §10 の合否をブロックしない。Evidence Report が実装完了の唯一の合否基準である。自動テストは開発者が任意で実行する補助的な回帰テストであり、初回実装時に無限の修正・再試行ループを引き起こしてはならない。

---

## 10. 自律評価（Evidence Report）

> 実装完了後、AI は以下を自律実行し、各項目を ⭐⭐⭐⭐⭐ で報告すること。スコアはコード生成直後の一発勝負ではなく、§9 の pytest が緑である前提で判定する。MCP Lite 版は Redis Stack 前提・7 日 TTL という制約下での最善を測る。

1. **常駐速度テスト**: MCP クライアントから `search_memory` を呼び、初回（モデルロード含む）と 2 回目以降の応答時間を計測せよ。合格目標は CPU 環境で **初回 3.0s 以内・定常 0.5s 以内**。Redis Stack が起動していない状態で呼び出した場合、サーバーがクラッシュせず「Redis Stack を起動してください」のヒントを返すことも併せて確認せよ。

2. **実在人物テスト（実在歴史データ）**: 実在の歴史人物に関するテキストを `save_memory` で保存後、その人物名で `search_memory` を実行し、**上位 3 件以内**に該当レコードが含まれることを合格基準とする。
   - 日本語版の例: 「坂本龍馬」
   - 英語版の例: 「Abraham Lincoln」

3. **架空設定テスト（創造的架空設定 / 親ドキュメント契約）**: 架空の世界観・固有名詞を含む **400 文字以上**のテキストを `save_memory` で保存後、`search_memory` で取得し、**保存したテキストの全セクションが一字一句変化なく復元**できることを合格基準とする。§3.11 の親-チャンク設計により、検索はチャンクでヒットしても応答には `doc:<parent_id>` から取り出した verbatim な全文が現れなければならない。Claude 応答中のコードブロックは文書設計に従い `[code omitted]` に置換される。
   - 日本語版の例: 「浮遊都市・不知火（架空の設定資料）」
   - 英語版の例: 任意の架空のキャラクター・都市・固有名詞

4. **FTS 句読点・CJK 耐性テスト**: 括弧・句読点を含む日本語テキスト（例: `架空の惑星「アルファ9」の気温設定`）を `save_memory` で保存後、括弧を含まないクエリ（例: `アルファ9 気温`）で `search_memory` を実行し、**上位 3 件以内にヒット**することを合格基準とする。§3.7 の句読点除去と CJK バイグラム展開が保存側・クエリ側の両方で一貫して適用されていることを検証するテストである。

5. **完全記録テスト**: 空でない入力がすべて記録されることを確認せよ。旧「N 文字未満フィルタ」「定型語スキップ」「ノイズパターン」が**存在しない**こと。
   - 2 文字の文字列（例: `はい`）を `save_memory` に渡し、**レコードが保存される**こと。
   - 定型語（例: `ok`、`yes`、`thanks`）を `save_memory` に渡し、**レコードが保存される**こと（ただし完全重複・近似重複は server-side で拒否されうる — その場合は別文字列で再試行）。
   - 空文字列・空白のみの入力は `empty content` で拒否されることを確認。

6. **全自動保存テスト（指示書遵守の検証）**: §5 の振る舞い指示をクライアント側 Claude が守っているかを確認せよ。
   1. テスト前の DB レコード件数を `list_memories` で記録する。
   2. Claude に **3 ターン以上**の実質的な会話を依頼する（技術的質問＋回答の往復）。この間、ユーザーは「保存して」と明示的に指示しない。
   3. 会話終了後、`list_memories` でレコード件数を再確認し、各ターンで **意図パラフレーズ＋結論**が 50〜200 文字程度で自動保存されていることを確認する。
   4. **合格基準**: 3 ターン以上のうち大半で Claude が自発的に `save_memory` を呼んでいること。長文ペーストは単発 blob でなく事実ごとに分割保存されていること。挨拶・確認質問はスキップされていること（§5 ルール #4）。

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

- **クロスエンコーダ・リランカー** — `hybrid_search` が返した上位 N 候補を、小型のクロスエンコーダ（例: `cross-encoder/ms-marco-MiniLM-L-12-v2`、~130 MB / `BAAI/bge-reranker-base`、~278 MB）で再ランキングする。現代的なノート PC で `search_memory` 1 回あたり **+100〜300 ms の CPU レイテンシ**（上位 50 件リランク）を加え、言い換えの多いクエリで概ね **精度 +1 ポイント** を得る見立て。差し込み位置は `processor.hybrid_search` の融合スコアソート後、`min_score` フィルタの前。リランカー無効時には従来スコアを素通しできるよう既定フォールバックを残すこと。なお本版には軽量な語彙リランカーが既にデフォルト有効で組み込まれており（`lexical_rerank_enabled` [§6](#6-設定)）、クロスエンコーダはその上位互換として差し替える形になる。
- **HyDE（Hypothetical Document Embeddings）** — ユーザーのクエリを埋め込む前に、小型 LLM で「仮の回答」を合成し、その回答をクエリの代わりに（または併用で）埋め込む。クエリが短く曖昧で、記憶側が長く具体的な場合に効く。検索ごとに LLM ホップが入るため、「外部 API コール無し」を謳う Lite 版とは相性が悪い — ローカル LLM が既に利用可能な場合に限り選択肢となる。
- **日本語形態素解析** — 本版はすでに [§3.7](#37-トークナイズと句読点処理) の通り CJK バイグラム展開で日本語・中国語 BM25 を補強している。さらに精度を求めるなら保存時に形態素解析器で `text` 本文を事前分割する — 候補は `fugashi` + `unidic-lite`（MeCab ベース、~50 MB）、`SudachiPy` + `sudachidict-core`（~70 MB、A/B/C 三段の分割粒度）、バイナリ依存を避けたいなら純 Python の `Janome` — 表層形をスペース結合したものを並列 `text_tokens` TEXT フィールドに格納し、BM25 検索はこのフィールドを参照する。ベクトル検索側は影響なし（e5 埋め込みモデルは日本語を直接扱える）、表示用の生 `text` はそのまま保持する。見込みコストは `save_memory` 1 回あたり +5〜20 ms、バイグラム展開との差分は語境界の厳密性で、複合語・派生形の識別で差が出る。

3 案はいずれも加算的で、Redis スキーマ既存フィールドや TTL / 重複判定契約の変更を要求しない（日本語トークナイザは並列フィールドを**追加**するのみ）。将来の実装者は機能フラグとして独立に扱い（既定 OFF）、各々ベースラインランカーに対して単独でベンチマークすべきである。

---

## 付録 C：推奨 AI 支援ワークフロー

> **この付録は人間向けの操作ガイドです。** 各フェーズで ``` 内のプロンプトをコピーして Claude Code（またはお好みの MCP クライアント）に貼り付けてください。AI が自動で次のフェーズに進むことはありません。MCP Lite 版は Claude Code のスラッシュコマンドではなく **MCP ツール呼び出し**（`save_memory` / `search_memory` / `list_memories` / `delete_memory` / `repair_memory`）で駆動する点が Free 版との差です。

| フェーズ | あなたがやること | 使うモデル |
|---|---|---|
| 1. 実装 | プロンプトを貼って実装を依頼 | **Sonnet**（高速） |
| 2. デバッグ | 3 つのプロンプトを**順番に**貼って検証 | **Sonnet** |
| 3. 品質レビュー | プロンプトを貼って評価・改善を依頼 | **Opus**（深い推論） |

---

### フェーズ 1：実装（Sonnet）

モデルを **Sonnet** に設定し、以下を貼り付けてください。

```
この指示書（N3MemoryCore_MCP_Spec_JP.md）に従って n3mcmcp-lite を実装してください。
Redis Stack は docker で起動済みです。MCP stdio サーバーとして動かし、
5 つのツール（save_memory / search_memory / list_memories / delete_memory /
repair_memory）を登録してください。
```

Sonnet がパッケージ構成・RediSearch インデックス作成・MCP ツール登録・stdio 起動まで自動で行います。完了したらフェーズ 2 に進んでください（「動いた」≠「仕様通り」なので、ここで終わりにしないでください）。

---

### フェーズ 2：デバッグ（Sonnet）

引き続き **Sonnet** で、以下の 3 つのプロンプトを**順番に 1 つずつ**貼り付けてください。

**① データフロートレース**（データが途中で失われていないか確認）
```
n3mcmcp-lite について：
コードを読んで、save_memory ツール呼び出しから Redis パイプライン EXECUTE まで、
および search_memory 呼び出しからツール応答までのエンドツーエンドのデータフローを
トレースしてください。TTL が全書き込みで設定されているか、親ドキュメント（§3.11）の
fallback が途中で失われていないか確認し、必要に応じて修正してください。
```

**② 仕様との逐条比較**（指示書に書いてあるのに未実装の動作がないか確認）
```
n3mcmcp-lite について：
指示書 §4.3 の各 MCP ツールの入力スキーマ・振る舞いを、実際のコードと 1 ツールずつ
比較してください。§3.11 の親-チャンク契約（verbatim 復元・親→チャンクカスケード削除・
[doc×N] 表示）も逐条で確認してください。
ドキュメントに記載されているが実装されていない動作を探し、必要に応じて修正してください。
```

**③ クロスセッションテスト**（セッションを跨いでデータが見えるか実行確認）
```
n3mcmcp-lite について：
セッション 1 で save_memory で保存した結果が、MCP サーバーを再起動した後
（Redis は再起動しない）のセッション 2 で search_memory を実行したときに
取得できることを、実際に MCP クライアントから呼び出して確認してください。
必要に応じて修正してください。
```

3 つとも完了したらフェーズ 3 に進んでください。

---

### フェーズ 3：品質レビュー（Opus）

モデルを **Opus** に切り替え、以下を貼り付けてください。

```
n3mcmcp-lite をレビューしてください。必要に応じて修正してください。

n3mcmcp-lite は MCP 経由の記憶装置として、また RAG として 10 点満点で何点ですか？
記憶装置としての評価（保存・TTL・重複排除・親ドキュメント verbatim 契約）と
RAG としての評価（ハイブリッド検索・CJK バイグラム・軽量リランカー・時間減衰・
自動 importance）を分けて、スコアカードを生成してください。
```

Opus が実際に MCP ツールを呼び、**記憶装置**（保存・7 日 TTL・重複排除・親ドキュメント）と **RAG**（検索精度・ランキング・ノイズ耐性）の 2 軸でスコアカードを生成します。

> **注意**：MCP Lite 版は軽量リランカーと CJK バイグラムを標準搭載しているため Free 版より RAG 側の天井は少し高いですが、それでも以下が未実装で **RAG 側は 8 点を超えることはまずありません：**
> - 形態素解析（MeCab / SudachiPy 等）による語境界の厳密化（付録 B）
> - クロスエンコーダによる本格的リランキング（付録 B）
> - 言語特化 embedding モデル（`multilingual-e5-large` 等）への切り替え
> - HyDE などクエリ拡張技法（付録 B）
>
> 甘い採点は改善機会を失わせるため、厳格に評価してください。**スコアに関わらず、Opus に何が足りないかを具体的に指摘してもらい、改善点を必ず相談してください。**

---
