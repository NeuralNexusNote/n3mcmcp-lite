# N3MemoryCore MCP-Lite v2.1.0 [Volatile Memory — hooks for Claude Code, MCP for other apps]
> NeuralNexusNote™ プロダクト — **Lite（揮発型）版**

> **本版の位置付け**：N3MemoryCore MCP の無償 Lite 版（ワーキングメモリ）。ストレージは **Redis Stack（RediSearch）**、各エントリに **7 日の TTL**、それ以上の永続性はありません。SQLite + sqlite-vec で永続保存する **Pro 版** との差別化を明確化しています。
>
> **構成**：MCP-Lite = N3MC-Free + MCP で、データベースは Redis である。具体的には、Lite 自身の検索と保存の決まり（この文書の §3）に、Free のフック・常駐サーバー・CLI（§3.15、§12、§13）と、フックのないアプリ向けの薄い MCP の入口（§4.6）を足したものである。検索と保存の決まりは、Free と Lite で別々のままである。
>
> **想定ユーザー**：Claude Code の利用者（**フック**経由。すべてのメッセージと応答を自動で保存し、検索する）と、Claude デスクトップアプリなど、ほかの MCP 対応アプリの利用者（**MCP** 経由）で、7 日の窓内で会話をまたいで検索可能な短期メモリを欲しい人。
>
> **通信**：Claude Code は、フックと CLI が `127.0.0.1` の常駐サーバーと HTTP で通信する。ほかのアプリは、Model Context Protocol over stdio (JSON-RPC) で薄い MCP の入口と通信し、入口が同じ常駐サーバーに転送する。動作確認環境は Windows 11 + Python 3.12 + Redis Stack 7.x である（1.x の版は Ubuntu でも確かめた）。macOS は対象外である。

## ⚠️ 免責・配布条件

本ソフトウェア及び仕様書は、いかなる保証もなく **"AS-IS"**（現状のまま）で提供されます。

- **サポート無し**：作者はバグ修正・質問対応・動作保証を一切行いません。
- **無保証・免責**：作者は本ソフトウェアの使用に起因するあらゆる損害（データ損失・業務中断・第三者請求等を含むが限定されない）について一切責任を負いません。
- **自己責任**：本ソフトウェアの使用に伴うリスクは全てユーザーが負担します。
- **変更権**：作者は予告なく本ソフトウェアを修正または提供停止できるものとします。

本ソフトウェアを使用することで、上記条件に同意したものとみなします。

- **ライセンス**：Apache License 2.0。詳細は `LICENSE` を参照。

> **アンインストール**：フックの登録も消えるように、AI に「N3MC Lite を削除してください」と頼む。自分で行うときは次のとおり。(1) プラグインの利用者は `/plugin uninstall n3mc-workingmemory@neuralnexusnote`。pip の利用者は、`~/.claude/settings.json` のうち、コマンドが `n3mc-lite-hook` / `n3mc-lite-stop-hook` を実行する 2 つの項目を消す。(2) 常駐サーバーを止める：`n3mc-lite --stop-server`（またはコマンドラインに `n3mc_mcp.cli` と `--run-server` を含むプロセスを終わらせる）。(3) `pip uninstall n3memorycore-mcp-lite` でパッケージを削除する。(4) `docker rm -f redis-stack` で Redis コンテナを停止・削除すれば、保存済みメモリは即座に消える。(5) `${N3MC_DATA_DIR}`（既定は `~/.n3mc-workingmemory/`）を削除すれば、`config.json`、`audit.log` と §7 のほかのファイルが除去される。(6) Claude デスクトップアプリの設定ファイルから `n3mc-workingmemory` の項目を消す。プロジェクトのフォルダにある `.claude/rules/n3mc-lite-behavior.md` は、手で消してよい。
>
> **バックアップは？** Lite 版は **バックアップを想定していません**。7 日の滑り窓で消滅します。永続メモリが必要な場合は **Pro 版** をご利用ください。

> **実装に関する質問**：作者への問い合わせはできませんが、本仕様書を Claude に読み込ませて直接質問することで、Claude が実装やカスタマイズを支援できます。

---

## Lite: Volatile Memory（揮発性メモリ）

本節は Lite 版固有のトレードオフをまとめたものです。以降は Pro 版と同じ構造で記述し、AI による再生成を容易にしています。

| 項目                   | Lite（本仕様）                              | Pro（別仕様）                            |
| ---------------------- | -------------------------------------------- | ---------------------------------------- |
| ストレージエンジン     | Redis Stack（RediSearch モジュール）           | SQLite + sqlite-vec（ローカルファイル）    |
| 耐久性                 | **エントリごと 7d TTL**・揮発                 | 永続・ディスク保存                       |
| ディスク使用量         | `config.json`（< 1 KB）。フックの `audit.log`（プロンプトと応答の原文。50 MB でローテーションし、最大約 100 MB）と小さなプロセス用ファイル（§7）も | `n3memory.db` が履歴と共に増加             |
| 外部依存               | ユーザー実行の Redis Stack コンテナ           | なし（セルフコンテイン）                 |
| `time_decay` の実効値  | 有意に効く（既定半減期 3 日：新鮮=1.0、7 日経過 ≈ 0.20） | 有意に効く（既定半減期 90 日）         |
| 再インデックス / 修復  | `FT.CREATE` が冪等（古い形の索引は作り直す）・移行マーカーなし | スキーマ＋モデル移行マーカーあり         |
| 想定用途               | 短期作業・ワーキングメモリ・マーケットプレース | 継続プロジェクト                         |

**揮発性の契約：**
- Redis への全書き込みで `ttl_seconds`（既定 604 800 = 7 日）の TTL を設定する。
- 主レコード（`mem:<uuid>`）と完全一致ガードキー（`mem:sha:<sha1>`）は同じ TTL を共有し、保存から 7 日で同時に失効する。検索は TTL を変えない（§3.3）。
- TTL のないキーを作る書き込みは 1 つもない。すでにあるレコードへの書き込み（再埋め込みしたベクトル、ガードの設定し直し）は、レコードがまだあるときだけ行い、確かめることと書くことを 1 つのアトミックな手順で行う（§3.3）。
- 失効は Redis に委任 — バックグラウンド掃除ジョブは動かない。
- Redis コンテナをボリュームごと削除すれば全メモリが即座に消える。
- この契約の対象は Redis である。フックの `audit.log`（§7、§12）は、フックの入力の原文を追記していく別のファイルで、TTL はない。Redis に届かなかったメッセージの最後の控えであり、メモリではない。これを読み戻して検索に入れるものはない。**`audit.log` は Free と同じく残す**（50 MB でローテーションし、最大約 100 MB）。7 日の窓より長く残るが、それでも残す。

**7 日を超えるセッション間保証はない。** Pro 版と違い、Lite 仕様では「永続化ハック」を禁止する — TTL を回避するために RDB スナップショット・AOF リライト・外部ダンプを追加しないこと。永続性が必要なら **Pro 版** を使うこと。

---

## v2.0.0 で変わったこと

v2.0.0 はメジャーバージョンである（セマンティック バージョニング）。Lite と Claude Code のつながり方が変わった。v1.12.2（その指示書の題は v1.9.1）までの Lite は MCP サーバーだけで、何を保存するかはモデルが決めていた。v2.0.0 からの Lite は「N3MC-Free + MCP で、データベースは Redis」である。

**形**

```
Claude Code ──hooks / CLI (HTTP)─────────────────────────┐
                                                         ├─▶ resident server (127.0.0.1:18524, model loaded once) ─▶ Redis Stack
Claude desktop app ─(MCP, stdio)─▶ n3mc-workingmemory ───┘
                                   (thin client, no model)
```

**どのアプリが何を使うか**

| アプリ | つながる経路 | 登録するもの |
|---|---|---|
| Claude Code | **フック**（`UserPromptSubmit`、`Stop`）と CLI `n3mc-lite`（§12、§13）、**または MCP**（薄いクライアント、§4.6）。利用者が選ぶ。両方でもよい | フック：`n3mc-lite --init`。MCP：プラグイン `n3mc-workingmemory`（v1.12.2 と同じく MCP の入口を登録する）、または `claude mcp add`。方法 A は両方を登録する |
| Claude デスクトップアプリと、フックのないほかのアプリ | **MCP だけ**（薄いクライアント `n3mc-workingmemory`、§4.6） | Claude デスクトップアプリは、方法 A のセットアップで AI がその設定ファイルに書く。ほかのアプリは利用者が書く（§8） |

- **Claude Code は、フックでも MCP でも、両方でもつなげる。** フックは、すべてのメッセージと応答を保存し、毎ターンに合う記憶を入れる。MCP では、v1.12.2 と同じく、モデルがツールで検索と保存をする（§11）。プラグイン `n3mc-workingmemory` は、v1.12.2 と同じく MCP の入口を登録する（フックは登録しない）。`n3mc-lite --init` はフックを登録し、MCP の登録には触れない。両方を使うときは、指示（§5 (1) の規則 1 と 2）が、フックの塊があるときは毎ターン保存しないようモデルに伝える。
- どちらの経路も**同じ常駐サーバーと同じ Redis** に届く。Claude Code で話したことはデスクトップアプリから見つかり、デスクトップアプリが保存したことは Claude Code で見つかる。

**更新した後に既存の利用者から見えること**

| 以前（v1.12.2） | 以後（v2.0.0） |
|---|---|
| Claude Code に 7 つの `mcp__…n3mc-workingmemory__*` ツールが見えていた | **変わらない。** プラグインは v1.12.2 と同じく MCP の入口を登録し、7 つのツールが見える。フックを使うときは `n3mc-lite --init` を実行する |
| モデルが `save_memory` に渡すと決めたものだけが保存された | **空でないすべてのメッセージと応答をフックが保存する**（コードブロックは `[code omitted]` にする）。保存は `[user]` / `[claude]` のレコードになる（§3.16） |
| モデルがターンの最初に `search_memory` を呼んでいた | `UserPromptSubmit` フックが、メッセージのたびに検索し、結果を `# Recalled Memory Context` の見出しで Claude のコンテキストに入れる（§12） |
| 検索・一覧・取り出し・削除は MCP のツールだった | Claude Code では CLI が同じことをする：`n3mc-lite --search`、`--list`、`--recall-thread`、`--delete`、`--delete-session`、`--buffer`、`--repair`（§13）。表示する文はツールの文と同じである |
| アプリごとに自分の MCP サーバーを起動し、モデル（約 1.1 GB）を読み込んでいた | 1 つの常駐サーバーがモデルを 1 回だけ読み込む（§3.15）。MCP の入口は薄いクライアントで、モデルを読み込まない（§4.6） |
| セッションの合間に動くプロセスはなかった | 常駐サーバーは、一度起動するとバックグラウンドで動き続ける |
| pip の利用者は MCP サーバーを登録した | その登録はそのまま使える（MCP の入口は薄いクライアントになる、§4.6）。フックも使うときは、`n3mc-lite --init` を 1 回実行する（§12）。`--init` も方法 A も、MCP の登録を消さない（§8） |
| 古いプラグインや `.mcp.json` が `uvx --from n3memorycore-mcp-lite n3mc-workingmemory`（版の指定なし）を起動していた | そのまま動く。`uvx` が 2.0.x を取ってくると、その登録が薄いクライアントを起動し、モデルは v1.12.2 と同じくツールで検索と保存をし、それが常駐サーバーを通る（§4.6）。プラグインを更新すると、`uvx` の版がパッケージの版に固定される（§2） |
| `N3MC_SESSION_ID` で、Claude Code のセッションの保存を 1 つのプロジェクトにまとめられた | フックのレコードは Claude Code 自身の `session_id` を持つ（§3.16）。`N3MC_SESSION_ID` は今も MCP の保存と検索に効く（§4.6）。`delete_memories_by_session` / `--delete-session` には、レコードに出るセッション ID（`--list`）を使う |
| MCP クライアントの `env` の `N3MC_REDIS_URL` で Redis を選べた | 常駐サーバーは、それを起動したプロセスの環境を使う（§3.15）。どのプロセスも同じものを使うよう、`redis_url` を `config.json` に書く。自分の設定が動いているサーバーと違うクライアントには、通知 `redis_url_mismatch` が出る |
| `skip_code_blocks: true` は、フェンス付きの `save_memory` を断った | 同じ設定で、フェンスを含むフックのメッセージや応答も、すべて保存しない（§3.16）。既定の `false` なら `[code omitted]` にして保存する |
| 最初の `uvx` の取得とモデルのダウンロードは、MCP サーバーの起動のときに起きた | 最初のフックの呼び出しのあと、裏で起きる。フックはサーバーを待たない（§12.3）ので、モデルの準備ができるまで、最初のメッセージはベクトルなしで保存され、キーワードだけで検索される |
| `agent_name` のない `save_memory` は空の名前を保存した | MCP：`N3MC_AGENT_NAME`、なければクライアントの `clientInfo.name`、なければ空（§4.6）。フック：`claude-code` |
| 検索は、上位の結果の TTL を張り直していた（`ttl_refresh_on_search`、既定 `true`） | **検索は TTL を変えない**。どのレコードも、それを書いた保存から 7 日で消える（§3.3）。`ttl_refresh_on_search` はもう設定ではなく、`config.json` に残っていても無視する（§6）。検索はレコードに何も書かない：`access_count` はもう数えず、頻度の加点もない（§3.6） |
| 全文検索は、索引 `n3mc_idx` の `content` の欄を使っていた | `content_fts`（役割の接頭辞と `[code omitted]` を除いた本文）を使う（§3.7）。古い形の索引は、起動のときに一度だけ消して作り直す。レコードそのものは残る。それより前に保存したレコードには `content_fts` がないので、期限で消えるまで、BM25 は `content_ngram` だけでそれを見つける（§3.7） |
| MCP の SDK が、ツールの引数をスキーマで確かめ、`Input validation error: …` と答えていた（たとえば 100 を超える `limit`、文字列の `importance`、数の `turn_id`） | Lite が自分で引数を確かめ、自分の文で答える（§4.3、§4.6 の 5）。たとえば 100 を超える `limit` は受け付け（1 未満は 1 にする）、文字列でない `turn_id` には `{"status": "error", "saved": false, "reason": "turn_id must be a string (got int)"}` を返す |
| — | `--stop` は、Claude Code がターンを終えた各プロジェクトの根のフォルダに `.claude/rules/n3mc-lite-behavior.md` を書く（§12.4、§12.9） |
| `examples/claude_code_mcp.json` が Claude Code への MCP の登録を示していた | 消した。Claude Code で MCP を使うときは、プラグインを入れるか `claude mcp add` で登録する（§8） |
| Redis の案内と README は `docker run … -p 6379:6379 …`（すべてのインターフェース）と書いていた | **（表示の変更）** `-p 127.0.0.1:6379:6379` と書く。記憶がネットワークから届かなくなる。すでに作ったコンテナは、自分の設定のままである。変えるには作り直す（`docker rm -f redis-stack`、そのあと新しい `docker run`）。そのとき、そのコンテナの記憶は、ほかの再起動と同じく消える（§3.4） |
| 規則 0 と検索のリマインダは、永続の記憶として `n3mc-longtermmemory` を挙げていた | **（表示の変更）** `n3mc-pro`、`n3mc-pg`、`n3mc-longtermmemory`（N3MC MCP Pro、その PostgreSQL 版、以前の MCP Pro の MCP の登録名）を挙げる（§5 (1)、(2)） |
| 方法 A は `claude_desktop_config.json` を書き換えていた | AI は、控えを取ってから `n3mc-workingmemory` の項目だけを置き換える（§8「自動の登録」） |

**変わらないもの**：PyPI の名前 `n3memorycore-mcp-lite`、Python パッケージ `n3mc_mcp`、MCP のコマンドとサーバー名 `n3mc-workingmemory`（別名 `n3mc-mcp-lite`）、同じ名前・引数・出力の文を持つ 7 つのツール（ただし `search_memory` の文は `context_max_chars` に収まるようになった、§3.17）、Redis のレイアウトと設定（`redis_url`、`N3MC_REDIS_URL`）、保存から数えて 7 日の TTL、データフォルダ `~/.n3mc-workingmemory`（`N3MC_DATA_DIR`）、プラグイン名 `n3mc-workingmemory`、上の表の変更を除く §3 の検索と保存の決まり。v1.12.2 が保存したレコードはそのまま読める（キーとフィールドは同じ。索引は一度だけ作り直す。上の表を参照）。

---

## セットアップ

### 前提条件

| 項目                      | 要件                                                                        |
| :------------------------ | :-------------------------------------------------------------------------- |
| Python                    | 3.10 以上                                                                   |
| クライアント              | Claude Code（フック）と、Claude デスクトップアプリまたはその他の MCP クライアント（MCP）のどちらか、または両方 |
| Redis Stack               | `localhost:6379` で稼働（`N3MC_REDIS_URL` で変更可能）                      |
| pip パッケージ（自動導入） | `mcp`（**`>=1.0.0,<2.0.0`**）`redis`（`<8.0.0`）`sentence-transformers` `uuid-utils` `platformdirs` `numpy`。`fastapi` `uvicorn` `pydantic` `psutil` も（常駐サーバーとそのプロセスの確認、§3.15） |
| OS                        | Windows 11 または Linux。macOS は対象外                                     |

### クイックスタート

> **N3MC シリーズの主たる利用方法は「指示書（本仕様書）から AI に実装を生成させる」ことです。** 本仕様書は AI による再生成を前提に詳細化されており（冒頭「以降は Pro 版と同じ構造で記述し、AI による再生成を容易にしています」）、Claude 等に読み込ませることで実装・カスタマイズ・再生成ができます。**pip / マーケットプレイスでのパッケージ導入は、これを手早く試すための補助的な「おまけ」機能**です。

どちらの方法でも、先に **Redis Stack を起動**しておきます：

```bash
# 初回のみ（コンテナを作成）：
docker run -d --name redis-stack -p 127.0.0.1:6379:6379 redis/redis-stack-server:latest

# 2 回目以降（コンテナ既存）：
docker start redis-stack
```
初回作成後は `docker run` を再実行すると `Conflict. The container name "/redis-stack" is already in use` になるので、以降は `docker start` を使用。

> **永続化は docker 引数ではなく、サーバーが無効にする**。
> MCP サーバーは接続のたびに `CONFIG SET appendonly no` と
> `CONFIG SET save ""` を発行します（§3.4 `enforce_ephemeral()`）。
> セッション間で手動で永続化を有効にしても次回 Lite 起動時に元に
> 戻されます — **ただし、その Redis に N3MC のキーしか入っていない
> 場合に限ります**。ほかのデータも入っている Redis の永続化
> 設定は変更せず、専用コンテナを勧める `shared_redis` 通知を出します
> （§3.4）。docker コマンドに `--appendonly no --save ""` を付けては
> いけません。`--save ""` の空文字列引数が Windows PowerShell および
> cmd.exe のクォート処理で壊れ、コンテナのエントリポイントが起動不能
> になることがあるためです。サーバー側の強制のみを真の源とします。**禁止の理由**：揮発性こそが無償 Lite 版と **Pro 版
> （永続）** を分ける製品境界であり、Lite は「再起動で本当に
> 忘れる 7 日ローリング・スクラッチパッド」であって「TTL 付きの
> 永続ストア」ではありません。継続的なメモリが欲しければ **Pro 版**
> を使ってもらう設計です。専用コンテナでは `enforce_ephemeral()` により、**ユーザー
> のシェルや docker フラグに関わらず、Lite を誤って永続ストアに変え
> てしまうことが機構的に不可能**になります。

#### 方法 A：指示書から作成（メイン・推奨）

1. **Redis Stack を起動**（上記）。
2. **本仕様書を AI に読み込ませて実装を依頼**：
   1. **フォルダを作る**:エクスプローラーで、空のフォルダを 1 つ作ります(例:`C:\Users\<あなたの名前>\n3mcmcp-lite`)。コードはこのフォルダに作られ、セットアップのあとも使い続けます。あとで消したり動かしたりしないでください。
   2. **指示書を入れる**:ダウンロードした指示書 `N3MemoryCore_MCP_Spec_JP.md` を、そのフォルダに入れます(このフォルダには、指示書のほかに何も入れません)。
   3. **そのフォルダで Claude Code を開く**:Claude のデスクトップアプリで、サイドバー上部の **Code** タブを開き、**+ New** で新しいセッションを始めます。プロンプト欄の下の行で、環境は **Local** を選び、その横のフォルダの欄で **Open folder…** を選んで、1 のフォルダを選びます。(ターミナルで使うときは、そのフォルダに移って `claude` を起動します。)
   4. **頼む**:プロンプト欄に「この指示書 `N3MemoryCore_MCP_Spec_JP.md` に従って n3mcmcp-lite を実装してください」と書いて送ります。

   AI は最初にフォルダ・Python・Redis を確かめ([§1](#1-ビジョン) の「セットアップ前の確認」)、足りないものがあれば手順を示して止まります。AI がパッケージ構成・RediSearch インデックス作成・常駐サーバー・フック・CLI・MCP の薄いクライアントまで生成します。段階的な **実装 → デバッグ → 自律評価 → 品質レビュー** の 4 フェーズ・ワークフローは [付録 B](#付録-b推奨-ai-支援ワークフロー) を参照。
3. **導入と登録(AI が行う)**：AI がプロジェクトルートで `pip install -e .` を実行し、`n3mc-lite --init` で Claude Code に**フック**を、`claude mcp add` で **MCP の入口**を登録し、Claude デスクトップアプリが入っていれば、その設定ファイルに MCP の入口の項目(絶対パス入り)を書き込み(控えを取り、この項目だけを置き換える)、すべてが起動することを確かめます([§8「コード生成の後の自動登録」](#コード生成の後の自動登録方法-a--必須))。Claude Code の設定ファイルを自分で編集する必要はありません。
4. **クライアントを再起動**。再起動の後の最初の記憶の検索は、常駐サーバーが ~1.1 GB の埋め込みモデルをダウンロードしてロードするので 30–60 秒かかります。

#### 方法 B：パッケージ導入（おまけ）

公開済みパッケージをそのまま入れて手早く動かしたい場合はこちら：

1. **Redis Stack を起動**（上記）。
2. **パッケージをインストール**（いずれか一つ）：
   - **pip**（グローバルまたは venv）：
     ```bash
     pip install n3memorycore-mcp-lite
     ```
   - **uvx**（事前インストール不要・分離環境 — [`uv`](https://docs.astral.sh/uv/) が必要）。Claude デスクトップアプリ向け：
     ```bash
     uvx --from n3memorycore-mcp-lite==2.1.0 n3mc-workingmemory
     ```
   - **Claude Code プラグインマーケットプレイス**（pip/uvx コマンドを手動で打つ必要なし — プラグインが MCP の入口を `uvx` で動かすが、`uv` は PATH に必要）：
     ```
     /plugin marketplace add NeuralNexusNote/n3mcmcp-lite
     /plugin install n3mc-workingmemory@neuralnexusnote
     ```
3. **アプリをつなぐ**：
   - **Claude Code**：プラグインの利用者は何もしない。プラグインが、v1.12.2 と同じく MCP の入口を登録する。すべてのメッセージと応答を保存するフックを使うときは、pip で入れて `n3mc-lite --init` を 1 回実行する（§12）。MCP、フック、その両方のどれを使ってもよい。
   - **Claude デスクトップアプリ**（とその他の MCP クライアント）：アプリの設定に MCP の入口を登録する（[§8](#8-クライアント設定) 参照）。
4. **クライアントを再起動**。最初の記憶の検索は、常駐サーバーが ~1.1 GB の埋め込みモデルをダウンロードしてロードするので 30–60 秒かかります。

### データバックアップ

適用外。[Lite: Volatile Memory](#lite-volatile-memory揮発性メモリ) を参照 — 本版は意図的に揮発的です。`config.json`（`owner_id` / `local_id` UUID を含む）が ID にかかわる唯一のファイルで、再インストール時に同じオーナー ID を維持したい場合にのみコピーすれば十分です。データフォルダには `audit.log`（フックの入力の原文、§12）とプロセス用のファイル（§7）も置かれる。これらはメモリではなく、バックアップは要らない。

---

## 1. ビジョン

MCP クライアント向けに「気軽に試せる」メモリエンドポイントを提供する：ハイブリッド検索（ベクトル + RediSearch BM25）、数学的に正しいランキング、7 日での自動ガベージコレクション。フックのないアプリには、MCP サーバーが振る舞いの指示を配信し、接続中の LLM が各ターンの先頭で自動検索、応答後に自動保存を行う。Claude Code では、フックが両方を行う（下記）。

Lite は Claude Marketplace で N3MemoryCore MCP の外向き仕様をゼロリスクでデモするために存在する。**Pro 版** は記憶を SQLite + sqlite-vec に永続で持つ（N3MC Pro。MCP の入口は N3MC MCP Pro にあり、ツールの組は違う：§14）。

**Claude Code にはフック。** 振る舞いの指示では、モデルに保存させることはできない（§11）。フックを持つ Claude Code のために、Lite は Free のフックを使う。`UserPromptSubmit` フックが利用者のメッセージを保存してそれで検索し、`Stop` フックが応答を保存する。そのため、ツールを呼ばなくても、すべてのやりとりが保存され、すべてのメッセージに合う記憶が付く。MCP の指示は、フックのないアプリのために残す。検索と保存の決まりは、どちらの経路でも Lite 自身のもの（§3）である。違うのは呼び出し方だけである。

> **⚠️ セットアップ前の確認(方法 A — ファイルを書く前に、この順で行う)**：確認 1〜3 は、満たさないときにセットアップを止める。確認 4 は、AI が自分で直す。これらの確認があるのは、セットアップを始めるフォルダに別の PC の以前のセットアップの写しが入っており、Docker は止まっていて、確かめた版より新しい Python(たとえば 3.15)しかなく、`.mcp.json` は別の PC の Python を指している、ということがありうるためである。そのままでは、実装するセッションはビルドもサーバーの起動もできない。確認 1〜3 のどれかを満たさなければ、そこで止まり、利用者にすることを示せ。

> **1. インストール先のフォルダの確認**：方法 A は、AI を開いているフォルダ(カレントディレクトリ)にコードを書き込む。書き込むのは `pyproject.toml`、`README.md`、`tests/`、`n3mc_mcp/` など §2 のファイルである。このフォルダはセットアップの後も使われ続ける。`pip install -e .` で入れた場合、フック・常駐サーバー・MCP の入口はこのフォルダのコードを実行するので、あとでフォルダを消したり移したりすると記憶が動かなくなる。
> 1. カレントディレクトリの絶対パスを示し、次のメッセージを表示せよ。「N3MC Lite をこのフォルダに入れます: <絶対パス>。コードはここに置かれ、あとでこのフォルダを消したり移したりすると記憶のフックと記憶のサーバーが動かなくなります。続けますか? (yes/no)」。**利用者の答えを待て。**はっきり yes のときだけ続けよ。
> 2. 聞く前にフォルダの中を見よ。この指示書(`N3MemoryCore_MCP_Spec_EN.md` / `_JP.md`)、`.claude/`、`.git/`、この製品の以前のセットアップのファイル(`n3mc_mcp/`、`tests/`、`.mcp.json`、`name` が `n3memorycore-mcp-lite` の `pyproject.toml`)のほかに何かあれば、**聞かずに中止せよ**。表示するメッセージは「このフォルダには、ほかのファイル(たとえばあなたのプロジェクト)がすでに入っています。ここでセットアップすると上書きするおそれがあります。`~/n3mcmcp-lite` のような空のフォルダを作り、そこで AI を開いて、もう一度セットアップを頼んでください。」。既存のプロジェクトを上書きしたり、混ぜたりしてはならない。
> 3. 以前のセットアップが入っているときは、そのことを(その `pyproject.toml` の版と一緒に)メッセージに書き、この指示書からコードを作り直すか、そのまま使うかを聞け。以前のセットアップは別の PC からコピーされたものかもしれない。`.mcp.json` や `.claude/settings*.json` にある絶対パスのうち、この PC に存在しないものは、サーバーを登録するとき(§8)にこの PC のパスに書き換えよ。存在しないパスを残してはならない。
> 4. ホームフォルダそのもの(`~`)やシステムのフォルダには入れない。

> **2. Python の確認**：`python --version` か `python3 --version` で、Python 3.10 以上があることを確かめよ。動作を確かめた版は **3.12** である(冒頭を参照)。見つからなければ「Python 3.10 以上が必要です。https://www.python.org/ から Python 3.12 を入れてください」と表示し、**セットアップを中止せよ**。
>
> **Windows の注意**：Windows では `python` / `python3` が Microsoft Store 用のショートカット(`%LOCALAPPDATA%\Microsoft\WindowsApps` のアプリ実行エイリアス)しか無いことがある。この場合はバージョンが表示されず、Store が開くか「Python was not found」と表示される。これは「Python が見つからない」として扱え。判断する前に `py -0p`(python.org 版のランチャーが、入っている版をすべて表示する)も試せ。Windows で中断するときは、次の手順をメッセージに添えよ。
> 1. https://www.python.org/ から Python 3.12 を入れ、インストーラーで「Add python.exe to PATH」にチェックを入れる。または `winget install Python.Python.3.12` を実行する。
> 2. それでも `python` で Microsoft Store が開くときは、設定 → アプリ → アプリの詳細設定 → アプリ実行エイリアス で、`python.exe` と `python3.exe` をオフにする。
> 3. **やり直しが必要です。** AI のアプリ(Claude Code、Claude Desktop)は、起動したときの `PATH` を持ち続けるので、後から入れた Python が見えない。ウィンドウを閉じるだけでは足りない(アプリがタスクトレイなどで動き続けることがある)。Windows から一度サインアウトして、サインインし直す。スタートメニュー → 左下のアカウントのアイコン → **サインアウト**。PC の再起動でもよい。
> 4. サインインした後、新しいターミナルを開き、`python --version` が 3.10 以上を表示することを確かめる。
> 5. AI のアプリを起動し、同じフォルダを開いて、もう一度セットアップを頼む。
>
> このメッセージは初心者向けに書け。1 つの手順に 1 つの操作、やさしい言葉で、サインアウト(または再起動)が必要なことをはっきり書け。
>
> **確かめた版より新しい Python**：3.12 より新しい Python しか無いと、依存パッケージの一部がその版向けのビルド済みのパッケージをまだ出しておらず、`pip install -e .` がソースからのビルドを試みて失敗することがある(たとえば Microsoft C++ Build Tools を求めるメッセージが出る)。そのときはビルドツールを入れず、上の手順で Python 3.12 を入れてもらい、その版で環境を作って(Windows では `py -3.12 -m venv .venv`)、そこに入れよ。
>
> Python を自分で入れてはならない。Python のインストールは利用者に任せる。

> **3. Redis の確認**：`docker ps` を実行せよ。Docker につながらない(Docker Desktop が動いていない)ときは、Docker Desktop を起動してもらい、動き出すまで待て。次に `docker ps -a --filter name=redis-stack` で確かめ、コンテナが**この PC に**無ければ、[クイックスタート](#クイックスタート)の `docker run` の行で作れ(別の PC で作ったコンテナは、この PC には無い)。あるが止まっているときは `docker start redis-stack` を実行せよ。`redis-stack` が動いているときだけ続けよ。利用者が `N3MC_REDIS_URL` でほかの Redis を指定しているときは、Docker の代わりにその Redis が応答することを確かめよ。
>
> **4. Windows: Visual C++ ランタイム(確認せず自動で入れる)**：`sentence-transformers` は PyTorch を使い、Windows の PyTorch には Microsoft Visual C++ 再頒布可能パッケージ(x64)が要る。無いと `torch` の読み込みに失敗する(たとえば `c10.dll` を示す `OSError: [WinError 126]` や `[WinError 1114]`)。`pip install -e .` の前に `reg query "HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" /v Installed` で確かめよ。キーが無いか、値が `0x1` でなければ、利用者に聞かずに `winget install --id Microsoft.VCRedist.2015+.x64 --silent --accept-package-agreements --accept-source-agreements` で入れよ。Windows が管理者の確認(UAC)を出すので、許可するよう利用者に 1 行で伝えよ。あとで `import torch` が上のエラーで失敗したときも同じようにせよ。`winget` が無いときは、公式のインストーラー https://aka.ms/vs/17/release/vc_redist.x64.exe を示して待て。これは実行用のランタイムで、コンパイラではない。依存パッケージのビルドに失敗したとき(確認 2)に、Python 3.12 の代わりにはならない。
>
> git は要らない。`git clone` ではなくコピーでコードを受け取った場合も、git なしで続けよ。

> **⚠️ 初回ダウンロード**：`sentence-transformers` が初回ツール使用時に `multilingual-e5-base` モデル（~1.1 GB）をダウンロードします。その間サーバーは無応答に見えますが、これは想定動作です。キャッシュ後は数秒で起動します。

> **重要：文字数上限（設計制約）**
> - フックの保存（Claude Code）：空でないすべてのメッセージと応答を丸ごと保存する。長さやスキップのパターンによる絞り込みはない。長いものは下の親ドキュメントの経路をとる（§3.16）。
> - 1 エントリの自動保存（MCP の `save_memory`）：**50–200 文字推奨**（1 エントリ 1 事実）。
> - 検索クエリ：**2,000 文字**（`search_query_max_chars` で調整可能）。
> - ベクトル検索：レコードの先頭 **~2,000 文字** のみがセマンティック検索対象（モデル上限 512 トークン）。それ以降は保存・BM25 検索可能だがベクトル類似性には見えない。
> - **長文貼り付けの扱い（2 モード）**：
>   - **要点抽出（推奨・事実ベースの記憶）**：内容を読解し、各要点を短文（~50–200 字）に抽出して 1 事実ごとに `save_memory` を呼ぶ。検索精度・アクセス頻度ブースト・重要度調整の効果が最大化される。
>   - **全文保存（verbatim recall）**：ユーザーが「この設定資料を後で同じものを表示してほしい」のように**原文をそのまま再現したい**場合は、長文を 1 回の `save_memory` に渡してよい。サーバーは `chunk_threshold`（既定 400 文字）を超える本文を自動的にスライディングウィンドウでチャンク化し、同時に親ドキュメント（`doc:<uuid>`）に全文を verbatim 保存する。後続の `search_memory` で任意のチャンクがヒットすると、親ドキュメントの全文が復元される（[§3.11](#311-全文再現親ドキュメントチャンクパターン) 参照）。

---

## 2. パッケージ構成

```
n3memorycore-mcp-lite/
├── pyproject.toml                  # Package metadata and the five console scripts (below)
├── README.md / README_JP.md        # User guide (generated from this spec)
├── LICENSE / NOTICE / CHANGELOG.md / PHILOSOPHY.md
├── N3MemoryCore_MCP_Spec_EN.md / _JP.md   # this specification
├── n3mc_mcp/                       # Python package
│   ├── __init__.py                 # Version marker (__version__ = "2.1.0")
│   ├── __main__.py                 # Entry point: python -m n3mc_mcp (= the MCP thin client, run())
│   ├── server.py                   # MCP thin client: stdio, tools/list, forwards tools/call to the resident server (§4.6); run()
│   ├── tooldefs.py                 # The seven Tool definitions of §5 (3), shared by the thin client and the resident server
│   ├── tools.py                    # The tool handlers, nudges, _render_search, notices (§4.3, §5 (2)–(4)); run inside the resident server
│   ├── instructions.py             # Behavioral instructions delivered at initialize (§5 (1))
│   ├── app.py                      # Resident FastAPI server: /health /shutdown /buffer /search /repair /tools/{name} (§4.5)
│   ├── cli.py                      # CLI `n3mc-lite`: --init --stop --hook-submit --save-claude-turn --run-server --search --buffer --list --recall-thread --delete --delete-session --repair; ensure_server, _start_server, _notify (§3.15, §12, §13)
│   ├── lite_hook.py                # UserPromptSubmit hook entry: audit.log → `cli --hook-submit`
│   ├── lite_stop_hook.py           # Stop hook entry: audit.log → `cli --save-claude-turn` → `cli --stop`
│   ├── context.py                  # `# Recalled Memory Context` block, context_size, size limit (§3.17)
│   ├── database.py                 # Redis layer: index, CRUD, TTL, dedup (+ role prefixes §3.16, direct save without vectors)
│   ├── processor.py                # Embedding, ranking, CJK tokenization, reranker
│   ├── config.py                   # config.json load/save + UUID generation
│   └── paths.py                    # data folder (~/.n3mc-workingmemory/) and its files (§7) + legacy platformdirs path for migration
├── plugins/n3mc-workingmemory/     # Claude Code plugin: the MCP entry, as in v1.12.2 (§8)
│   ├── .claude-plugin/plugin.json  # "mcpServers": n3mc-workingmemory through uvx
│   └── hooks/
│       ├── hooks.json              # SessionStart only
│       └── install_permissions.py  # SessionStart: permission rules for the MCP tools (§8)
├── .claude-plugin/marketplace.json # Marketplace entry; metadata.version = package version
├── tests/                          # pytest suite (§9)
│   ├── conftest.py
│   ├── test_database.py  test_processor.py  test_server.py  test_regressions.py
│   └── test_hooks.py  test_api.py  test_cli.py  test_mcp_client.py  test_context_limit.py  test_coexistence.py  test_safe_path.py
└── examples/
    └── claude_desktop_config.json  # the desktop registration of §8 (no claude_code_mcp.json: Claude Code uses hooks)
```

`config.json` は **`~/.n3mc-workingmemory/`** に置かれます（[§7](#7-データ保存先) 参照）。ディスク上に DB ファイルは存在しません — メモリ状態は Redis 内にのみ存在します。

**コンソールスクリプト**

| スクリプト | 対応先 | 用途 |
|---|---|---|
| `n3mc-workingmemory` | `n3mc_mcp.__main__:run` | MCP の薄いクライアント（Claude デスクトップアプリが起動するコマンド） |
| `n3mc-mcp-lite` | `n3mc_mcp.__main__:run` | `n3mc-workingmemory` の非推奨の別名（1.5 より前の設定向け） |
| `n3mc-lite` | `n3mc_mcp.cli:main` | Claude Code と人のための CLI（§13） |
| `n3mc-lite-hook` | `n3mc_mcp.lite_hook:main` | UserPromptSubmit フック（§12） |
| `n3mc-lite-stop-hook` | `n3mc_mcp.lite_stop_hook:main` | Stop フック（§12） |

常駐サーバーは `python -m n3mc_mcp.cli --run-server` として起動する（§3.15）。`python -m n3mc_mcp.cli` は、ルールファイルが Claude に示すコマンドの形でもある（§12）。

**`pyproject.toml`**：公開リポジトリでは、今のファイル（ビルドバックエンド hatchling、メタデータ、キーワード、分類子、`[tool.hatch…]` の各節）を残し、下の項目だけを変える。**方法 A が空のフォルダに生成するとき**は、次のファイルを書いてから同じ項目を当てる：`[build-system]` `requires = ["hatchling>=1.21.0"]`、`build-backend = "hatchling.build"`。`[project]` `name = "n3memorycore-mcp-lite"`、`description = "N3MemoryCore MCP Lite - ephemeral 7-day hybrid-search memory over Redis Stack"`、`readme = { file = "README.md", content-type = "text/markdown" }`、`license = { file = "LICENSE" }`、`requires-python = ">=3.10"`、`authors = [{ name = "NeuralNexusNote" }]`、下の依存。`[tool.hatch.build.targets.wheel]` `packages = ["n3mc_mcp"]`。`readme` と `license` が指すファイルがないと、hatchling がメタデータを作れず `pip install -e .` は失敗する。そのため方法 A は、`README.md`（この指示書から書く短い利用案内）と `LICENSE`（Apache License 2.0 の全文）も書く。キーワード、分類子、URL は、手元へのインストールには要らない。プラグインのファイル（`plugins/…/plugin.json`、`.claude-plugin/marketplace.json`）は公開リポジトリだけのもので、方法 A は作らない。

- `version = "2.1.0"`。
- `dependencies`：今の 6 つ（`mcp>=1.0.0,<2.0.0`、`redis>=5.0.0,<8.0.0`、`sentence-transformers>=2.7.0`、`uuid-utils>=0.6.0`、`platformdirs>=4.0.0`、`numpy>=1.26.0`）に、`fastapi>=0.110`、`uvicorn>=0.27`、`pydantic>=2.0`、`psutil>=5.9` を足す（下限は Free のもの）。
- `[project.optional-dependencies] dev`：`httpx>=0.27` を足す（FastAPI の `TestClient` 用）。
- `[project.scripts]`：上の表の 5 つのスクリプト。
- 分類子 `Operating System :: OS Independent` を、`Operating System :: Microsoft :: Windows` と `Operating System :: POSIX :: Linux` に変える（macOS は対象外）。

`.claude-plugin/marketplace.json` の `metadata.version` と、`plugins/n3mc-workingmemory/.claude-plugin/plugin.json` の `version` は、パッケージのバージョン（`2.1.0`）である。**`uvx` のコマンドは同じバージョンに固定する**。この文書にあるすべての `uvx --from n3memorycore-mcp-lite==<version>`（プラグインの MCP の入口、ルールファイルの `{cli}`、デスクトップの登録）は、パッケージのバージョンを書く。リリースでは、これらすべてを 2 つの `version` フィールドと一緒に変える。固定しないと、プラグインと pip のインストールが別のバージョンを動かしうる。それらのサーバーは `code_version` が違うので、互いを置き換え合う（§4.6）。

**そのまま残すファイル（この指示書からは作らない）**：`SECURITY.md`、`CONTRIBUTING.md`、`.github/`（issue と PR のテンプレート、`workflows/test.yml`、`workflows/pypi-publish.yml`）、`server.json`（MCP レジストリの項目）、`.gitignore`、評価用スクリプト `tests/eval_*.py`、`tests/test_cross_session.py`。コードを作り直しても、これらを消したり書き換えたりしてはならない。`.github/workflows/test.yml` の CI マトリクスを手で直すときは、Windows と Linux だけを並べる。

---

## 3. 技術仕様（変更不可）

> **⚠️ AI は自動改変してはならない**：速度向上・最適化の名目で以下の仕様を自律的に変更してはなりません。埋め込みモデル・ベクトル次元・TTL の変更は人間による `config.json` 手編集でのみ許可されます。

> ### 📌 実装上の既知の落とし穴（必読）
>
> 本仕様書から再実装する際、以下は「動いているように見えて核心機能が壊れる」典型的な罠である。各詳細は該当節の「⚠️ 実装注意」を参照。
>
> | # | 罠 | 症状 | 対処 | 詳細 |
> |---|----|------|------|------|
> | 1 | redis-py 8.0 が **RESP3** 既定 | `FT.SEARCH` が常に 0 件 | RESP3 の dict 応答をハンドリング（現行実装）＋ `redis<8.0.0` ピン、または `from_url(..., protocol=2)` | [§3.3](#33-redis-接続と-ttl) |
> | 2 | BM25 スコア未取得 | キーワード検索が無効化（ベクトルのみに退化） | `.with_scores().scorer("BM25")` | [§3.6](#36-ランキング式) |
> | 3 | NFKC 正規化の適用漏れ | 全角/半角の重複・検索ゆれ | embedding・dedup hash・BM25 用テキストに NFKC（保存する `content` には適用しない） | [§3.7](#37-トークナイズと句読点処理) |
> | 4 | モデルロードが起動を**ブロック** | MCP 接続タイムアウト（~40s） | ライブラリの import は stdio の前、モデル本体の構築は stdio 後にバックグラウンド + ロック（import をバックグラウンドにすると Windows で固まる） | [§3.9](#39-起動シーケンスと自己回復) |
> | 5 | `doc.id` が**フルキー**（`mem:<uuid>`） | `mem:mem:...` ゴーストキー（TTL なし孤立） / `[doc×0]` / 削除失敗 | `_doc_bare_id()` で裸 UUID 統一 | [§3.12](#312-tag-クエリの-uuid-制約と-python-側フィルタリング) |
> | 6 | `SCAN mem:*` が `mem:sha:*` にもマッチ | `HGET` が `WRONGTYPE` で落ちる | プレフィックス除外 or 例外捕捉 | [§3.12](#312-tag-クエリの-uuid-制約と-python-側フィルタリング) |
> | 7 | `serverInfo.version` / `instructions` 未設定 | version 空・指示未広告 | `Server(name, version=, instructions=)` | [§4.2](#42-initialize-応答) |
> | 8 | `search_memory` 応答に `turn_id` 無し | `recall_thread` が呼べない（運用フロー断絶） | RETURN に `turn_id`、markdown に出力 | [§4.3.1](#431-取り出し拡張ツールretrieval-extensions) |
> | 9 | Evidence Report 中に pytest 実行 | `FLUSHDB` がライブデータを消す | §10 中は pytest を止める／別 Redis | [§9](#9-テストpytest) |
> | 10 | 「定常 0.5s」を MCP 往復で計測 | 5〜10s に見え誤判定 | 0.5s は純サーバー側目標と理解 | [§10](#10-自律評価evidence-report) |
> | 11 | テストをライブの Redis に向ける | `FLUSHDB` がユーザーのワーキングメモリを消す | テストは `N3MC_REDIS_TEST_URL` にのみ接続し、既定値は持たない | [§9](#9-テストpytest) |
> | 12 | BM25 クエリに制御文字・CJK 句読点が残る | `Syntax error`（BM25 が黙って抜ける）／`「記憶装置」は？` で BM25 ヒット 0 件 | 制御文字 → 空白、CJK・全角句読点も除去 | [§3.7](#37-トークナイズと句読点処理) |
> | 13 | Redis を起動時にしか確かめない／接続エラーを握りつぶす | `docker start redis-stack` のあとも Redis が止まっていると言い続ける／あとから止まった Redis で `_No memories found._` | 毎回のツールの前に `ensure_connected()`（PING／つなぎ直し）、`ConnectionError` / `TimeoutError` は送り出す | [§3.9](#39-起動シーケンスと自己回復) |
> | 14 | 設定を `%LOCALAPPDATA%` に置く | ストア（MSIX）版 Claude デスクトップアプリは MCP サーバーの `%LOCALAPPDATA%` を振り替える → `config.json` と `owner_id` が 2 つになり、デスクトップアプリで保存した記憶がターミナルの `claude` から見えない（逆も） | 設定は `~/.n3mc-workingmemory/`（振り替えの対象外）に置き、1 回だけ引き継ぐ | [§7](#7-データ保存先) |
> | 15 | プラグインの自動許可ルールにサーバー名をそのまま使う／フックの失敗が exit 非ゼロだけ | プラグインの利用者は記憶の呼び出しのたびに許可を求められる／Python が無いときの手がかりは `SessionStart hook error … python: command not found` だけ | `mcp__plugin_n3mc-workingmemory_n3mc-workingmemory__*`（+ `mcp__n3mc-workingmemory__*`）を許可する。Python を使わないフォールバックが `systemMessage` の JSON を出して exit 0 | [§8](#自動許可claude-code-固有) |
> | 16 | `mcp` に上限がない | 新しく入れると（`uvx`・`pip`・プラグイン）`mcp` 2.x が入り、import の時点で落ちる：`AttributeError: 'Server' object has no attribute 'list_tools'`。`/mcp` には `Connection closed` としか出ない | サーバーを 2.x の API に移すまで、`pyproject.toml` で `mcp>=1.0.0,<2.0.0` に固定する | [セットアップ](#前提条件) |
> | 17 | モデルの取得を呼び出しのたびにやり直す／劣化した検索を伝えない | オフラインの初回：`save_memory` は毎回約 24 秒、`search_memory` は約 12 秒。キーワードだけの結果が普段と同じに見える | 再試行は 1 分に 1 回まで。劣化中は毎回の `search_memory` に知らせ、`save_memory` に `detail` | [§3.9](#39-起動シーケンスと自己回復) |
> | 18 | 「空」の判定を `strip()` だけで行う | `\x00`、単独の `U+200B` や `U+FEFF` が、何も表示されない記録として保存される | 見える文字のない本文を拒否する（`has_visible_text`）。それ以外はすべて verbatim に保存する | [§3.13](#313-エンコーディング安全策) |
> | 19 | `min_score`／相対カットを最終スコアで判定する | 約 2 日より古い記録と、MCP サーバーの再起動より前に保存した記録（別の `session_id`）がすべてトップスコアの 0.6 倍を下回って消える | 2 つの下限はどちらも `filter_score`（`time_decay` も `b_session` も掛けない）で判定し、並び順は最終スコアで決める | [§3.14](#314-検索結果の整形) |
> | 20 | `socket_connect_timeout` だけを設定する | 接続は受け付けるが応答しない Redis（`docker pause`、スリープからの復帰）で `PING` が永久に止まり、イベントループとその後のツール呼び出しがすべて固まる | `socket_timeout=5`。読み取りのタイムアウトは接続断として扱う | [§3.9](#39-起動シーケンスと自己回復) |
> | 21 | 起動するプロセスがそれぞれ `config.json` を作る | 同時に起動した 2 つのサーバーが別々の `owner_id` を持ち、片方が保存した記録は次の起動から誰にも見えない | 排他的に作成し、既にあれば読み直す。書き込みはすべて一時ファイル＋`os.replace` | [§6](#6-設定) |
> | 22 | 親ドキュメントをチャンクより先に書く | 2 回の書き込みの間で失敗すると、チャンクのない `doc:`＋`docsha:` が残る：検索できず、再保存も 7 日間 `duplicate` で拒否される | 先にすべてのチャンクを埋め込み、そのあとガード（`SET NX`）と親とチャンクを 1 つの Lua スクリプトで書く（`SAVE_WITH_GUARD`、§3.8） | [§3.8](#38-重複判定完全一致のみ) |
> | 23 | 数値の設定に範囲チェックがない | `chunk_overlap >= chunk_threshold` で無限ループ。`ttl_seconds: 604800.5` や `0` で保存がすべて失敗。`embedding_dim: 768.0` で起動時にサーバーが止まる | キーごとの型と範囲の表。`chunk_text` は必ず先へ進む | [§6](#6-設定) |
> | 24 | 起動時の `FT.CREATE` の失敗を送り出す | RediSearch のない素の Redis（または起動中の接続断）でプロセスが終わる。クライアントには落ちたサーバーしか見えない | 索引なしで動き続け、呼び出しごとに再試行し、「Redis Stack が必要」というエラーを返す | [§3.9](#39-起動シーケンスと自己回復) |
> | 25 | どの Redis でも永続化を無効にする | `localhost:6379` で動いている他人の Redis が、黙って RDB/AOF を失い、次の再起動でデータを失う | 接続した DB が空か N3MC のキーだけで、**サーバーのほかの DB にキーがない**ときに限る。それ以外は `shared_redis` 通知 | [§3.4](#34-揮発性の厳守変更最適化禁止) |
> | 26 | ツール呼び出しがモデルのロックを無制限に待つ | 初回のダウンロード中（数分）はツール呼び出しがすべて止まり、クライアントのツールタイムアウトに達しうる | 待つのは最大 20 秒。そのあとは通知付きでキーワードだけで続ける | [§3.9](#39-起動シーケンスと自己回復) |
> | 27 | TTL の更新が重複ガードを飛ばす／BM25 の語を AND でつなぐ／読めない `since` を無視する | よく使う記録が 7 日後に 2 回保存されうる／文のようなクエリでキーワードがヒットしない／`since="yesterday"` で全件を検索する | TTL の更新は一切しない：検索は TTL を変えない（§3.3）。語は OR でつなぐ。読めない時刻の範囲は拒否する | [§3.3](#33-redis-接続と-ttl)、[§3.7](#37-トークナイズと句読点処理)、[§4.3.1](#431-取り出し拡張ツールretrieval-extensions) |
> | 28 | 「キーがあるか」の確認と書き込みを別々のコマンドで送る | 確認と書き込みの間に削除（別のプロセスの `delete_memory`）または失効したレコードが、`embedding` だけを持つハッシュとして **TTL なしで**作り直される — 失効せず、どの削除でも消えない。レコードの削除後にガードを設定し直すと、同じ内容を 7 日間保存できなくなる | 確認と書き込みを **1 つの Lua スクリプト**で行う（`EXISTS` のあとに `HSET` / `SET … EX`） | [§3.3](#33-redis-接続と-ttl) |
> | 29 | MCP の入口がモデルを import する、または stdout に出力する | アプリごとに約 1.1 GB をまた読み込む。紛れ込んだ `print()` が JSON-RPC の流れを壊す | 薄いクライアントは `sentence_transformers` も `torch` も import しない。stdout には MCP のメッセージ以外を出さない | [§4.6](#46-mcp-の薄いクライアント) |
> | 30 | `127.0.0.1` に `urllib.request.urlopen` を使う | `HTTP_PROXY` が設定されている（会社の PC）と、すべてのリクエストがプロキシに行く。サーバーが止まっているように見え、フックはすべて待たされる | リクエストは `build_opener(ProxyHandler({}))` で開く | [§3.15](#315-常駐サーバー) |
> | 31 | エンドポイントを `async def` で書く | 長い `/repair` や検索が `/health` を止める。CLI はサーバーがないと判断し、もう 1 つ起動する | Redis かモデルにさわるエンドポイントは、すべて普通の `def` にする | [§3.15](#315-常駐サーバー) |
> | 32 | プラグインの MCP の入口（`uvx` で動く PyPI のコード）と、生成したインストールのフック・CLI が並ぶ | 呼び出しのたびに互いのサーバーを止めて起動し直す（`code_version` が違う） | プラグインが有効な間、方法 A は止めて利用者に選ばせる（§8） | [§14](#14-ほかの-n3mc-製品との共存) |
> | 33 | フックの出力が 10,000 文字を超える | Claude Code はそれをファイルに移し、Claude には 2,000 文字のプレビューしか見えない | フックと MCP の `search_memory` の塊に `context_max_chars`（既定 9,500） | [§3.17](#317-コンテキストの大きさの上限) |
> | 34 | Redis かサーバーが止まっているとき、フックが例外を出すか非ゼロで終わる | Claude Code がメッセージのたびにフックのエラーを表示する。プロンプトが失われる | 先に `audit.log`。ベクトルなしで直接保存。通知。exit 0 | [§12](#12-claude-code-のフック) |

### 3.1 ID 階層

N3MemoryCore は各レコードの出所と文脈を識別する 5 つの ID フィールドを使います：

| ID           | 保存場所         | 生成タイミング                          | 粒度                    | 用途                                                                                          |
| ------------ | ---------------- | --------------------------------------- | ----------------------- | --------------------------------------------------------------------------------------------- |
| `id` (PK)    | Redis ハッシュ    | レコード毎（UUIDv7、時刻順）             | **1 レコード**          | 各メモリの一意識別子 — 削除・重複判定に使用                                                    |
| `owner_id`   | `config.json`    | 初回起動時（UUIDv4）                     | **オーナー**            | 誰のデータか — HASH フィールドとして保存・返却し、Python 側でフィルタリングする（§3.12 参照）  |
| `local_id` (agent_id)   | `config.json`    | 初回起動時（UUIDv4）                     | **エージェント / 導入** | インストールの UUIDv4 識別子。互換性のため保存（Lite のランキングでは未使用）。                |
| `session_id` | メモリ内          | サーバープロセス起動時（UUIDv4）         | **サーバープロセス**    | どのプロセスが書いたかの label。`save_memory` / `search_memory` の引数、または `N3MC_SESSION_ID` 環境変数で上書き可。**Lite でも Pro と同じ `b_session` ランキングが適用される**（match=1.0 / mismatch=0.6）。同一 `session_id` をプロジェクトごとに固定して渡すことで、その chat / プロジェクトのメモリを他プロジェクト由来のノイズより上位に押し上げられる。`delete_memories_by_session` のフィルタキーも兼ねる。「サーバープロセス」は MCP の入口のプロセスを指す（薄いクライアントが既定値を決め — 引数 → 自分の `N3MC_SESSION_ID` → 自分の起動時の UUIDv4 — 常に送る、§4.6）。フックのレコードは、フックの入力にある Claude Code の `session_id` を持つ（§12）。 |
| `agent_name`   | Redis ハッシュ    | `save_memory` 呼び出し毎（自由文字列）   | **エージェント表示名**  | 人間向けラベル（例：`"claude-desktop"`、`"claude-code"`）。フックのレコードは常に `"claude-code"`。MCP は `agent_name` 引数、なければ `N3MC_AGENT_NAME`、なければクライアントの `clientInfo.name`、なければ空（§4.6）。 |

### 3.2 埋め込み

- モデル：`intfloat/multilingual-e5-base`（既定） / ベクトル：`float[768]`（既定）
- **モデルは設定で差し替え可能**：`embedding_model`（既定 `intfloat/multilingual-e5-base`）と `embedding_dim`（既定 `768`）を `config.json` で変更することで、各デプロイが品質・リソース・言語のトレードオフを自分で選べる（例：`intfloat/multilingual-e5-large`（1024 次元）、ドメイン特化の多言語モデル）。製品自体は特定言語に肩入れせず中立を保ち、選択権は運用者にある。`embedding_dim` は選んだモデルの出力次元と**一致させること**——RediSearch のベクトルインデックスはこの値で構築される。モデルまたは次元を変更したらプロセス再起動とインデックスのフラッシュが必要（次元が違うと既存ベクトルと非互換）。**AI はこれらを自動改変してはならない（§3 冒頭の警告）——人間による `config.json` 手編集でのみ変更する。**
- エンコード時は必ず `normalize_embeddings=True` を指定し、L2 正規化ベクトル（ノルム=1）を保証する。コサイン距離を使う場合でも重要：正規化されていない入力は `(1 − cosine_distance)` ↔ 類似度 の等価を崩す。
- **入力プレフィックス（必須）**：プレフィックスなしでは本モデルの精度は著しく低下します：

  ```python
  # 保存時（文書として登録）
  text_to_embed = "passage: " + content

  # 検索時（クエリとして照合）
  text_to_embed = "query: " + keyword
  ```

### 3.3 Redis 接続と TTL

**接続**：`redis_url`（設定フィールド）または環境変数 `N3MC_REDIS_URL`（環境変数が優先）から構築。既定値：`redis://localhost:6379/0`。`decode_responses=False` — クライアントはバイナリ埋め込みペイロードを扱う必要がある。**ホストが `localhost` のとき**：クライアントを作る前に、`127.0.0.1:<port>` に TCP で 0.3 秒だけつないでみる。つながれば、`localhost` を `127.0.0.1` に置き換えた URL（利用者名、パスワード、ポート、DB は同じ）でクライアントを作る。つながらなければ、URL をそのまま使う。設定の `redis_url` と、それを表示する文は変えない。**理由**：Windows は `localhost` を先に `::1` として試す。`127.0.0.1` だけで待ち受ける Redis（`docker run -p 127.0.0.1:6379:6379`）では、新しく接続するたびに `socket_connect_timeout`（2 秒）を待つことになる。実測で 2.0 秒、`127.0.0.1` では 0.005〜0.04 秒。

> **⚠️ 実装注意（redis-py 8.0+ / RESP3）**：redis-py 8.0 から `from_url()` の既定プロトコルが **RESP3** になった。`decode_responses=False` と RESP3 を併用すると `FT.SEARCH` の応答が **bytes キーの dict** として届き、`Result.from_resp3` のパースが失敗して**検索結果が常に 0 件**になる。**現行実装はこれを (a) `pyproject.toml` で `redis<8.0.0` をピンし、かつ (b) `FT.SEARCH` が dict 応答を返した場合のハンドリングを併用して吸収している。** 新しい redis-py を意図的に使う場合は、接続時に **`protocol=2`（RESP2）を明示**すること（`redis.from_url(url, decode_responses=False, protocol=2)`）。この設定は接続クライアント・テストフィクスチャの**両方**に適用する。

**TTL**：`mem:<uuid>` への `HSET` は毎回、同じパイプラインで `EXPIRE mem:<uuid> <ttl_seconds>` と組み合わせる。兄弟である `mem:sha:<sha1>` ガードは、そのパイプラインより**前に** `SET … NX EX <ttl_seconds>` で取る（§3.8）。TTL は同じになる。既定 TTL は 604 800 秒（7 日）。

**アトミック性とガード**：ガードとレコードは **1 つの Lua スクリプト** `SAVE_WITH_GUARD`（§3.8）で書く。スクリプトは `SET <guard> <id> NX EX <ttl>` を実行し、それが成功したときだけ、すべてのレコード（単独のレコード、または親ドキュメントと**すべての**チャンク）に `HSET` + `EXPIRE` を行う。ここまでが 1 つのアトミックな手順である。同じ内容を同時に 2 回保存したときにどちらが勝つかは今までどおり `SET NX` が決め、「レコードはあるが TTL なし」は起きず、**「ガードはあるがレコードなし」も起きない**。先にガードを単独の `SET NX` で取り、レコードをそのあと `MULTI`/`EXEC` のパイプラインで書いてはならない。失敗の処理でガードを削除し直しても、その間にプロセスが終わると（サーバーが止まった、PC を切った）ガードだけが残り、同じ文をもう一度保存しても、何も保存されていないのに最大 `ttl_seconds` のあいだ `duplicate` と答えることになる。

**検索は TTL を設定し直さない**：すべてのレコード、チャンク、親ドキュメント、ガードは、それを書いた保存から `ttl_seconds`（7 日）で消える。検索、想起、一覧、そのほかの読み出しは、どの TTL も変えない。検索ヒットの TTL（たとえば普通の行の上位と、その親ドキュメント、兄弟チャンク、ガード）を設定し直してはならない。設定し直すと、検索に出続ける記録は、INSTRUCTIONS（§5 (1)）、ツールの説明（§5 (3)）、ルールファイル（§12.9）、README が利用者に伝える「保存から 7 日で消える」より長く残る。この約束が仕様である。`ttl_refresh_on_search` は設定の項目ではない（今ある `config.json` に残った値は無視する、§6）。レコードとそのガードは、1 つのスクリプト（`SAVE_WITH_GUARD`、§3.8）が同じ TTL で書き、どちらも設定し直さないので、ガードを設定し直さなくても同時に失効する。

**すでにあるレコードへの条件付きの書き込み**：`HSET` は、キーがなければ作ってしまう。そのため、`save_memory` 自身が行わないただ 1 つの書き込み — `repair_memory` が再埋め込みしたベクトル（§3.10）— は、**レコードの確認と書き込みを同じアトミックな手順で行う Lua スクリプト 1 つ**で実行する（`KEYS` / `ARGV` は `EVAL` と同じ）：

```lua
-- HSET_IF_EXISTS   KEYS[1] = mem:<id>        ARGV[1] = field, ARGV[2] = value
if redis.call('EXISTS', KEYS[1]) == 1 then
  redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
  return 1
end
return 0
```

- すでにあるキーへの `HSET` は、その TTL を変えない。スクリプトは `EXPIRE` を呼ばず、レコードを作ることもない。
- スクリプトは一度だけ登録し（redis-py の `register_script`）、パイプラインから実行する（`script(keys=…, args=…, client=pipe)`）。サーバーがまだ知らないスクリプトは redis-py が読み込む。スクリプトはそれ自体がアトミックなので、スクリプトを並べたパイプラインに `MULTI` は要らない。
- ただの `EXPIRE` にスクリプトは要らない。`EXPIRE` はキーを作らない。

> **⚠️ 理由**：確かめてから後で書いてはならない（たとえば `repair_memory` で `EXISTS` のあとに `HSET` を送る、確かめたあとでガードの `SET` を送る）。レコードが削除されたあとにガードが書き直されると、レコードが無いのに同じ内容が 7 日間 `duplicate` として拒否される。`EXISTS` と `HSET` の間に失効したレコードは、`embedding` だけを持つ TTL のないハッシュとして戻る。こうしたハッシュには `owner_id` がないので、どの検索にも出ず、どの削除でも消えない — Redis にずっと残り、「すべての書き込みで TTL を設定する」（§3.4）に反する。隙間はミリ秒単位だが、デスクトップアプリとターミナルの `claude` は、同じ Redis に別々のサーバープロセスでつながる。

### 3.4 揮発性の厳守（変更・最適化禁止）

`save_memory` 呼び出し時、sha1 ガードを取り（`SET NX EX`）、HSET + EXPIRE を 1 つの Lua スクリプト（`SAVE_WITH_GUARD`）で書く。同じ呼び出しの中で行う（§3.3、§3.8） — バッチ化なし、キューイングなし。`save_memory` が応答を返す時点で、キーには有限の TTL が設定されていなければならない。

**以下は絶対に禁止（「性能」や「永続化」目的であっても）：**
- `EXPIRE` なしの書き込み（＝無限 TTL）。
- Lite メモリ寿命の延長のみを目的として、コンテナ削除後も生き残る Redis RDB / AOF 永続化設定を有効にすること。（ユーザーが自分の都合で Redis 設定を選ぶのは自由だが、仕様上はそれに依存しない。）
- 読み込み時の TTL の延長（`search_memory`・`recall_thread`・`list_memories`・フックの検索での `TOUCH`・`EXPIRE`、そのほかの TTL の設定し直し）。**例外はない**（§3.3）：すべての記録は、保存から 7 日で消える。
- 1 保存呼び出しを超える範囲でのライトバッファリング / 遅延パイプライン。

**理由**：Lite の差別化は明示的な揮発性である。回避するとプロダクト区分が崩れる。

**`enforce_ephemeral()` — Redis の永続化を無効にする**：接続に成功するたびに（起動時とつなぎ直しのたび、§3.9）呼ぶ。

1. **先にほかの DB を見る**：`INFO keyspace` を実行する。接続した DB（`redis_url` の `db`、既定は 0）以外の DB にキーがあれば、手順 4 に進む。`INFO` が拒否されたときも、ほかの DB を確かめられないので手順 4 に進む。
2. 接続した DB のキーを `SCAN`（`COUNT 1000`）で調べ、`mem:`・`doc:`・`docsha:` のどれでも始まらないキーが最初に見つかった時点で止める（`mem:sha:` のキーは `mem:` で始まる）。RediSearch の索引そのものはキーではない。
3. 接続した DB が空か、それらのキーだけなら：`CONFIG SET appendonly no` と `CONFIG SET save ""`。
4. それ以外（接続した DB に N3MC 以外のキーがある、ほかの DB にキーがある、`INFO` が拒否された）なら**設定を変更しない**。通知 `shared_redis` を加える（§6 の設定通知と同じく、最初に成功した `search_memory` で表示）：`shared_redis: the Redis at <host>:<port> also holds keys that are not N3MC's, so its persistence settings were left unchanged. N3MC memories in it may survive a Redis restart. Use a dedicated container (docker run -d --name redis-stack -p 127.0.0.1:6379:6379 redis/redis-stack-server:latest) for Lite.`
5. `CONFIG` が拒否されたら（マネージド Redis、`rename-command`、ACL）、stderr に 1 行出し、通知 `config_refused` を加える（文面の決まりは同じ：何が起きたか、永続化がまだ有効かもしれないこと、専用コンテナの案内）。これを理由に接続を失敗させてはならない。

> **⚠️ 理由**：既定の URL は `localhost:6379` であり、利用者がそこでほかの用途の Redis を動かしていることがある。向けられた Redis が何であっても永続化設定を変えると、その利用者は次の Redis 再起動でそのデータを何のメッセージもなく失う。Lite の 7 日の約束はすべてのキーの TTL で守られている。永続化を無効にするのは N3MC のデータをディスクに残さないためだけであり、ほかのプログラムのデータに手を出す理由にはならない。

> **⚠️ ほかの DB も見る理由**：`SCAN` は接続した DB しか見ないが、`CONFIG SET` はサーバー全体の設定を変える。この確認がないと、ほかの DB に別のプログラムのデータがある Redis の DB 0 に Lite を向けたとき、そのすべての DB の永続化を止め、次の Redis の再起動でそのデータが失われる。

### 3.5 データレイアウト

```
mem:<uuid>                  HASH（メモリレコード or チャンク）
    id              string      UUIDv7（キーサフィックスと同じ）
    content         string      元テキスト verbatim（チャンクなら部分文）
    content_ngram   string      CJK バイグラム展開（BM25 用のサイドチャネル）
    content_fts     string      索引用の文（役割の前置きと [code omitted] を除いた本文、§3.7）
    timestamp       string      ISO 8601 UTC
    timestamp_epoch number      unix 秒（SORTABLE）
    owner_id        string      TAG
    local_id        string      TAG
    agent_name        string      TAG
    session_id      string      TAG
    turn_id         string      TAG save_memory の turn_id（ないときは空文字列）
    importance      number      0.5〜2.0（save_memory 時指定、既定 1.0）
    access_count    number      いつも 0（保存の形を変えないために残す。読まない）
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
    turn_id         string      save_memory の turn_id（ないときは空文字列）
    chunk_count     number      生成されたチャンク数
    chunk_ids       string      子チャンク id の空白区切りリスト（連鎖削除用、§3.5）
    TTL                         ttl_seconds

docsha:<sha1>               STRING
    value = 対応する doc id（親全文の完全一致ガード）
    TTL = doc:<uuid> と同値

n3mc_idx                    RediSearch インデックス、ON HASH PREFIX 1 mem:
    SCHEMA:
        content_fts     TEXT weight 1.0
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

- **主キー**：UUIDv7（時刻順、挿入時に生成）。リファレンス実装は `uuid_utils.uuid7` を使用。
- **親ドキュメントは非インデックス**：`doc:` / `docsha:` は RediSearch の `PREFIX 1 mem:` に該当しないため検索インデックス対象外。検索ヒットは常にチャンク（`mem:*`）経由で、親は post-lookup で引く（[§3.11](#311-全文再現親ドキュメントチャンクパターン)）。
- **削除のセマンティクス**：`delete_memory` は ID のキーを見て自動分岐する：
  - 設定の `owner_id` のレコードだけを削除する（別のオーナーの ID → `not_found`）。
  - ID が `doc:<uuid>` に存在 → 親ドキュメント＋`docsha:<sha1>`＋その `chunk_ids` に列挙された全チャンク（`chunk_ids` のない古い親では `parent_id` が一致するチャンク、§3.11）を単一の `MULTI`/`EXEC` パイプラインで削除（cascade）。ガード `docsha:<turn_id>:<sha1>`（または `docsha:<sha1>`）は、**`GET` の値がこの親の ID と一致するときだけ**消す。消すのは、ガードの鍵だけを受け取る Lua スクリプト（`DEL_GUARD_IF_OWNED`：`GET KEYS[1] == ARGV[1]` なら `DEL KEYS[1]`）。このスクリプトにほかの鍵を渡さない（仮の名前の鍵も渡さない）：削除が持ち主でない鍵にはさわらない。Redis にはほかのデータがあるかもしれないため。`delete_memories_by_session` も、そのセッションの親ごとに同じように消す。
  - それ以外 → `mem:<uuid>` とその sha ガードを削除。**ただし独立したレコードに限り、ガードはこのレコードを指しているときだけ消す**：子チャンク（`parent_id` が空でない）はガードを持たないので、消すのは `mem:<uuid>` だけ。独立したレコードでは、1 つの Lua スクリプトでレコードを消し、`mem:sha:<turn_id>:<sha1>` は `GET` の値がこの ID と一致するときだけ消す。`delete_memories_by_session` も、独立したレコードごとに同じ確認をする。（チャンクと一緒にガードを消すと、同じ本文の独立したレコードのガードまで消え、次にその本文を保存すると再び `saved` になり、二重に保存される。）

### 3.6 ランキング式

Pro 版と同一：

```
Final Score = (cos_sim × 0.7 + keyword_relevance × 0.3) × time_decay × b_local × b_session
```

ここで `b_local` は **重要度係数**：

```
b_local = clamp(0.5, 2.0, stored_importance)
```

- `stored_importance`：`save_memory` 時に指定（既定 `1.0`、範囲 `0.5〜2.0`）。
**検索はレコードに何も書かない**：ヒットの回数は数えず、頻度の加点もない。検索でヒットした記録の `access_count` を増やしてはならない（ほかの項目も書かない）。索引の対象のハッシュに書くと、RediSearch はその文書を索引に入れ直す。古い項目は、ガベージコレクタが動くまで（既定では、そうした文書が 100 件たまるまで）逆引き索引に残り、その文書のすべての語の文書頻度に数えられる。そのため、よく見つかる記録ほど、検索のたびに BM25 が下がり、0 になる（実測：26 回の加算で 1.623 → 0.0。2 分後も 0.0）。検索の結果も、それより前の検索に左右される。`access_count_enabled`、`access_count_weight`、`access_count_max_boost`、`ttl_refresh_top_k` は設定の項目ではない。今ある `config.json` に残った値は無視する（§6）。

`b_session` は **セッション一致係数**（Pro と同一の挙動）：

```
b_session = b_session_match     if  row.session_id == effective_session
          = b_session_mismatch  otherwise
```

- `b_session_match`：既定 `1.0`。リクエストの `effective_session`（呼び出し時引数 → `N3MC_SESSION_ID` 環境変数 → プロセス起動時 UUID の優先順）と一致した行に乗算。
- `b_session_mismatch`：既定 `0.6`。同一 Redis インスタンスを共有する他プロジェクトのメモリを順位の下に押す。
- ChatLink 風「1 chat = 1 session_id」運用で、現在の chat に紐づくメモリを他プロジェクトのノイズより上位に出すための主要シグナル。`save_memory` / `search_memory` の両方で同一 `session_id` を固定して渡すことが前提。

`effective_session` が空文字列のときはマッチ判定が常に偽となるため、すべての行に `b_session_mismatch` が乗る — つまり session_id を一切渡さなければ全レコードが対称に扱われる（実質的に b_session 無効化と等価）。明示的に無効化したい場合は `b_session_match` と `b_session_mismatch` を両方 `1.0` に設定する。

**cos_sim** — **RediSearch のコサイン距離から直接導出**：

$$cos\_sim = \max(0,\ \min(1,\ 1.0 - cosine\_distance))$$

RediSearch は正規化ベクトルに対し `cosine_distance ∈ [0, 2]` を返す。`[0, 1]` にクランプすることで「正反対方向」の半空間を捨てる（メモリ検索では無関係と扱う）。

**keyword_relevance** — RediSearch の BM25 スコアを `[0.0, 1.0]` に正規化：

1. `|bm25_score| < bm25_min_threshold`（既定 `0.1`）なら `0.0`。
2. それ以外：`|bm25_score| / max(1.0, 結果集合内の max_|bm25_score|)`。

（RediSearch の BM25 は非負だが、FTS5 が負値を返す Pro 版と同じアルゴリズムに揃えるため `abs()` を保つ。）

> **⚠️ 実装注意（BM25 スコアの取得・最頻出バグ）**：RediSearch の `FT.SEARCH` は **既定ではスコアを返さない**。redis-py の `Query` で **`.with_scores()` を呼ばないと `doc.score` が `None`** となり、実装が `bm25 = 0.0` にフォールバックして **keyword_relevance が常に 0 → ハイブリッド検索の BM25 側（重み 0.3）が完全に死ぬ**（実質ベクトル検索のみに退化する。語彙リランクが代償するため一見動作して見えるのが厄介）。
> さらに `.with_scores()` を付けても **既定スコアラーは TFIDF** で、小規模コーパスでは全 doc が一律 `1.0` を返し term frequency が反映されない。**スコアラーを `BM25` に明示指定**すること：
> ```python
> Query(raw_q).with_scores().scorer("BM25").paging(0, k).dialect(2)
> ```
> Redis のバージョン差（`BM25` → `BM25STD` への改称）に備え、`BM25` → `BM25STD` → 既定 の順でフォールバックすると堅牢。実測（同一語を 1 回 vs 5 回含む 2 文書）で `BM25` がそれぞれ `0.20` / `0.56` と差別化することを確認済み。

**time_decay**：

$$time\_decay = 2^{-\frac{days\_elapsed}{half\_life\_days}}$$

既定 `half_life_days = 3` — 7 日の TTL より意図的に短く設定しており、Lite でも `time_decay` が実際に効く。新鮮なエントリは 1.0、3 日経過で 0.5、7 日経過（失効直前）で ≈ 0.20 となり、直近の文脈がランキング上位に押し出される。これは Lite 固有のチューニングで、永続化を前提とする **Pro 版** は 90 日の半減期を維持する。

**軽量語彙リランク**（融合後）：

上記ハイブリッドスコア計算後、CPU のみの任意リランクパスで各候補のスコアに以下を加算する：

- `coverage × rerank_weight`（既定重み `0.3`）：`coverage` はクエリトークンのうち content に出現するものの割合。**クエリと content の両方を `prepare_query`（§3.7）でトークナイズする** — NFKC、制御文字と ASCII・CJK・全角の句読点を空白に置換、**空白分割 + CJK バイグラム**の和集合、小文字化 — ので、content 中の `note,` は `note` として数えられる（トークンは `prepare_query(text).split()` を小文字にした集合である。純 CJK の連なりは 2 文字ずつのバイグラムだけを出し、連なり全体を 1 つのトークンとしては出さない）。バイグラムを足さないと `.split()` が純 CJK クエリ全体を 1 トークンに潰してしまい、coverage が「クエリ全体の部分文字列一致か否か」の二値に退化するため、日本語・中国語でも coverage が実効的な信号となるよう拡張している。（空白だけで分割すると、句読点が続く語は一度も数えられない。）
- `rerank_phrase_weight`（既定 `0.2`）：クエリ文字列全体が content の部分文字列として出現する場合に加算（大文字小文字非依存）。

親ドキュメントの解決は**語彙リランクの前に**行う：チャンクヒットは先に `doc:<parent_id>` の全文 verbatim に展開されてからリランク対象になる。これにより、クエリ句が「ヒットしたチャンク」ではなく「同じ文書の別チャンク」に存在する場合でも、親ドキュメントのランクが正しくブーストされる（[§3.11](#311-全文再現親ドキュメントチャンクパターン) 参照）。

設定で `lexical_rerank_enabled: false` にするとこのパスをスキップし、ハイブリッドスコアのみで並び替える。

**`min_score` フィルタは語彙リランクの後に、`filter_score` に対して適用する（下記）。** 融合スコアが `min_score` 直下の候補でも、フレーズ一致・coverage によるリランク加点で閾値を超えれば結果に残せるよう、スコアフロアによる足切りは融合時点ではなくリランク**後**に行う。融合時点で枝刈りすると、埋め込みが弱いがクエリ句を完全に含むレコードをリランクが救済する前に捨ててしまうため、この順序が重要である（[§6](#6-設定) `min_score`）。

**`filter_score` — 下限で判定する値**：各候補は 2 つの値を持つ。

| 値 | 式 | 用途 |
|---|---|---|
| `score` | `(cos_sim × 0.7 + keyword_relevance × 0.3) × time_decay × b_local × b_session` + リランク加点 | 結果の並び順と、表示する `score=` |
| `filter_score` | `(cos_sim × 0.7 + keyword_relevance × 0.3) × b_local` + リランク加点 | `min_score`（§3.6）と相対カット（§3.14 手順 5）、弱い候補の並び順（§3.14 手順 6） |

`time_decay` と `b_session` が決めるのは**どの記録を先に出すか**であって、記録を出すかどうかではない：3 日前の記録や、サーバー再起動より前に保存した記録（別の `session_id`）も、トップのヒットと同じくらい関連していれば、一覧の下のほうに出る。リランク加点は両方の値に加える。

この式の前後で行う整形処理（ベクトル下限・新しい版を先頭へ・相対カット）は [§3.14](#314-検索結果の整形) を参照。

### 3.7 トークナイズと句読点処理

**トークナイザ**：RediSearch 内蔵のトークナイザ（空白＋句読点区切り、大文字小文字統一）。Pro 版で使われる Porter ステマーは本版では利用不可。

**CJK バイグラム展開**：日本語・中国語は単語間スペースがないため、素の RediSearch トークナイザでは 1 文が 1 トークンに潰れ BM25 がほぼ効かない。これを補うため、保存時に索引用の文（`content_fts`、下記）中の連続 CJK ランを **オーバーラップバイグラム**（例：「記憶装置」→「記憶 憶装 装置」）に展開し、並列の `content_ngram` TEXT フィールドに格納する。BM25 検索はクエリ側にも同じ展開を適用した上で `@content_fts:(...) | @content_ngram:(...)` の OR クエリを走らせるため、日本語の部分一致が RediSearch 内で機能する。ベクトル検索側には一切干渉しない（e5 埋め込みは日本語を直接扱える）。

**索引用の文**（`fts_text`）：`content_fts` と `content_ngram` は、`content` そのものではなく、§3.16 の役割の前置き（`[user] `、`[claude] `、`[user i/N] `、`[claude i/N] `）を付ける前の本文（チャンクならそのチャンク）から、`[code omitted]` をすべて空白に置き換えた文で作る。`content_fts` はその文、`content_ngram` はそれを今までどおり NFKC 正規化・句読点除去・バイグラム展開した形（§5 (5)）である。前置きのない MCP の `save_memory` でも、`[code omitted]` は同じく除く。保存する `content` は変えない（表示、重複の確認、削除は今までどおり `content` を使う）。**理由**：前置きと `[code omitted]` が索引に入ると、`user`、`claude`、`code`、`omitted` を含むクエリが、BM25 でフックのすべての記録や、コードを含んでいたすべての記録に当たり、関係のない記録が上がる。

**古い形の索引**：既存の `n3mc_idx` に `content_fts` の属性がなく（`FT.INFO` の `attributes`）、ベクトルの `DIM` が `embedding_dim` と同じときだけ、`ensure_index()` は `FT.DROPINDEX n3mc_idx`（`DD` を付けない。ハッシュは消さない）のあと `FT.CREATE` をやり直す。`FT.DROPINDEX` が、メッセージに `unknown index` を含むエラーで失敗したとき（小文字にして比べる。ほかのプロセスが先に消した）は、そのまま `FT.CREATE` に進み、`already exists` は成功として扱う。`DIM` が違うときは作り直さず、今までどおり `index_dim_mismatch` を出す（作り直すと、次元の違うベクトルを持つ既存の記録が索引に入らなくなる）。作り直す前に保存した記録は `content_fts` を持たないので、失効するまで（保存から `ttl_seconds` の後。既定は 7 日）BM25 では `content_ngram`（前置きを含んだまま）だけで当たる。これらの記録は書き直さない。前の版のサーバーは、作り直した索引をあるもの（`already exists`）として扱い、作り直さない。そのため、前の版に戻すときは、先に `FT.DROPINDEX n3mc_idx`（`DD` を付けない）を実行する。

**クエリ整形**（`prepare_query`） — ユーザー入力クエリは RediSearch へ送る前に、NFKC 正規化 → 制御文字を空白に置換 → `strip_fts_punctuation`（ASCII **および** CJK・全角の句読点）→ CJK バイグラム展開 → 空白の連続を 1 つに畳む、の順で整形する。RediSearch の特殊文字はすべて除去されるため、エスケープは不要。`content` はハッシュにそのまま（verbatim）保存する（索引は `content` を使わない：上の索引用の文 `content_fts` を RediSearch が動的にトークナイズし、`content_ngram` はそれを同じ句読点除去を通して事前展開した形を持つ）。

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
    # RediSearch and leave an empty '@content_fts:()' → syntax error.
    return ' '.join(t for t in tokens if any(ch.isalnum() for ch in t))

def drop_lone_cjk_tokens(q: str) -> str:
    # BM25 query only: drop single-character CJK tokens (particles such as
    # は / の / を left over by bigram expansion) when other tokens remain.
    tokens = q.split()
    kept = [t for t in tokens if not (len(t) == 1 and _is_cjk(t))] or tokens
    # Hiragana-only tokens (して / ます / どう) too, when
    # another token remains.
    content = [t for t in kept if not all(
        0x3040 <= ord(c) <= 0x309F or c == 'ー' for c in t)]
    return ' '.join(content or kept)
```

`_bm25_search` は `drop_lone_cjk_tokens(prepare_query(query))` を使い、**残った語を OR で結ぶ**：

```python
terms = drop_lone_cjk_tokens(prepare_query(query)).split()
q = "|".join(terms)
fts_query = f"(@content_fts:({q}) | @content_ngram:({q}))"
```

記録は**どれか 1 つ**の語を含めばヒットする。BM25 は多くの語・珍しい語を含む記録ほど高く採点し、語彙リランクがすべての語の coverage を加えるので、すべての語を含む記録はやはり上に来る。バイグラム展開のあとに残る 1 文字の CJK（`「記憶装置」は？` → `記憶 憶装 装置 は`）はたいてい助詞で、OR ではほぼすべての日本語の記録にヒットしてしまうので、引き続き落とす。1 文字の CJK だけのクエリは、そのまま使う。

**ひらがなだけの語**: ひらがなだけでできたバイグラム（`して`、`ます`、`どう`）も、ほかの語が残るなら同じように落とす。日本語のほとんどの記録に出るので、OR では無関係な質問がこれで当たる（`来週の株価はどうなる` が `はど` と `どう` で `今日の天気はどうですか` に当たる）。同じ決まりを N3MC-Pro のバイグラムの索引で測った。ひらがなだけの語しかないクエリは、そのまま使う。Lite はもともと CJK のバイグラムを索引しているので、SQLite 版のほかの全文検索の決まり（3 文字ずつに分ける、2 つ目の索引）は当てはまらない。

> **⚠️ OR にする理由**：RediSearch は `@field:(a b c)` の中の語を AND で結ぶ。AND のままだと、`quick fox` では記録が見つかるが、`quick fox elephant`、`what did the quick fox do`、`記憶装置 大阪` では、すべての語を含む記録がなければ何も見つからず、スコアのキーワード部分（30 %）が 0 になる。Free も語を OR で結ぶ。

また `prepare_query` は、文字も数字も含まない語（`str.isalnum` が真の文字がない語。単独の `_` など）を落とす。RediSearch はこれを語として索引しないので、そればかりのクエリは `@content_fts:()` になり、構文エラーになる。

> **⚠️ 制御文字と CJK 句読点を除去する理由**：クエリに `\x00` などの制御文字が残ると `FT.SEARCH` が `Syntax error` で失敗する。このエラーは捕捉されるため、そのクエリでは BM25 側が黙って抜け落ちる。CJK 句読点は ASCII の除去対象に入っていないため、除去しないと `「記憶装置」は？` が `「記` や `は？` のようなバイグラムに展開される。語を AND で結合すると、どの文書にも現れないバイグラムが 1 つあるだけで BM25 側全体が **0 件** になる — そして日本語の質問はほぼ必ず `？` で終わるか `「」` を含む。保存側・クエリ側の両方でこれらを除去することでキーワード一致が回復する。BM25 の失敗で `search_memory` 自体を失敗させてはならない：捕捉してベクトル結果だけで続行する。

**空クエリ規則**：整形後に空文字列になった場合、キーワード検索はスキップしベクトル検索のみでランキングする。

> **⚠️ 実装注意（NFKC 正規化の適用漏れに注意）**：[§3.13](#313-エンコーディング安全策) が定める通り、**NFKC 正規化は「embedding 生成」「dedup hash 計算」「BM25 用のテキスト（`prepare_query` と保存する `content_ngram`）」に適用**する（`unicodedata.normalize("NFKC", text)`）。これにより全角↔半角（`ＡＩ`↔`AI`、`２０２６`↔`2026`）・合成/分解形が検索・重複判定で同一視される。**実装時に忘れやすい**ため明示する：`embed()` のエンコード対象テキストと、`mem:sha:` / `docsha:` ガードキー生成用の SHA1 入力の両方を NFKC 正規化すること。**保存される `content` HASH フィールドには適用しない**（verbatim 契約を守る）。save / dedup-check / delete の全経路で一貫して同じ正規化を通すこと（さもないと保存時と削除時でハッシュが一致しない）。

### 3.8 重複判定（完全一致のみ）

`save_memory` は入力本文長が `chunk_threshold`（既定 400 文字）以下かどうかで分岐する。

**(A) 単一チャンクパス**（本文が `chunk_threshold` 以下）：

1. **完全一致（O(1)）** — `EXISTS mem:sha:<sha1(content)>`。キーが存在すれば `{"status": "duplicate", "saved": false}` を返す。
2. 埋め込みを計算する。
3. レコードのフィールドを組み立てる。
4. **ガードを取り、レコードを書くのを 1 つの Lua スクリプトで行う** — `SAVE_WITH_GUARD` を `KEYS = [mem:sha:<sha1>, mem:<id>]`、`ARGV = [<id>, <ttl_seconds>, <フィールドと値の組の数>, <フィールド>, <値>, …]` で呼ぶ。スクリプトは `SET <guard> <id> NX EX <ttl>` を実行し、何も返らなければ（同じ内容の別の保存が先に取った）何も書かずに 0 を返し、`save_memory` は `duplicate` を返す。そうでなければレコードに `HSET` + `EXPIRE` を行って 1 を返す。（手順 1 は明らかな重複で埋め込みを計算しないためだけのもので、決めるのは手順 4。`EXISTS` のあとに別に書き込むと、同じ内容を同時に 2 回保存したとき両方が通ってしまう。）フィールドの組み立てやスクリプトの送信が失敗したとき、この保存のものは Redis に何も残らない。スクリプトが途中で失敗したときだけは、ガードがまだこの保存の `<id>` を持ち、`mem:<id>` がないときに限ってガードを削除し、エラーを報告する。別の id を持つガードは削除しない（別の保存のもの）。（フィールドを組み立てるときの失敗 — `agent_name`・`session_id`・`turn_id` の単独のサロゲートや、文字列でない値（入力の型を確かめない古い MCP SDK からの `turn_id: 12` など）— も同じである。ガードが残ると、何も保存されていないのに、その内容は `ttl_seconds` のあいだ `duplicate` と答えられる。）

**(B) マルチチャンクパス**（本文が `chunk_threshold` 超）：重複判定は**親ドキュメント全文**のレベルで行う。

1. **親レベル完全一致（O(1)）** — `EXISTS docsha:<sha1(full_text)>`。キーが存在すれば `{"status": "duplicate", "saved": false, "parent_id": "<既存>"}` を返す。
2. チャンク側は個別の sha ガードを付けない。理由：スライディングウィンドウで生成される隣接チャンクは設計上ほぼ重複しており、個別に dedup すると自分自身を却下してしまう。

親レベルのチェックを通過した場合、単一の `save_memory` 呼び出しで：
1. 本文を分割し、**先にすべてのチャンクの埋め込みを計算する**（まだ Redis には書かない）。
2. `doc:<parent_id>` と**すべての**チャンクのフィールドを組み立てる（チャンクごとの sha ガード無し、各チャンクの `parent_id` フィールドに親 ID を格納）。
3. ガードを取り、それらを書くのを **1 つの** Lua スクリプト `SAVE_WITH_GUARD` で行う（(A) の手順 4 と同じ）。`KEYS = [docsha:<sha1(full_text)>, doc:<parent_id>, mem:<chunk_id>, …]` で、`SET <guard> <parent_id> NX EX <ttl_seconds>` のあと、親とすべてのチャンクに `HSET` + `EXPIRE` を行う。`ARGV` は `[<parent_id>, <ttl_seconds>]` に続けて、`KEYS` の 2 番目以降の各キーについて、その順に `<フィールドと値の組の数>, <フィールド>, <値>, …` を並べたものである（(A) の手順 4 の `ARGV` は、レコードが 1 つのときのこの並びである）。ガードがもうあれば何も書かず、既存の `parent_id` を付けて `duplicate` を返す。
4. 失敗したときは (A) の手順 4 と同じ：書きかけのものは残らず、ガードはこの `parent_id` を持ち `doc:<parent_id>` がないときだけ削除し、同じ本文をもう一度保存できる。

> **⚠️ 理由**：親と `docsha:` を先に書き、そのあとでチャンクの埋め込みを計算してチャンクを書くと、その間で失敗した（接続断、埋め込みの例外）とき、チャンクのない親とガードが残る：文書は見つからず（検索はチャンクにしかヒットしない）、再保存は 7 日間 `duplicate` で拒否される。

> **⚠️ 近似（類似度）による重複拒否は行わない**：既存エントリとの `cos_sim >= 0.95` を理由に新規エントリを拒否してはならない（短文でも文書全体でも）。`multilingual-e5` ではこの閾値で「同じ内容の再保存」と「更新版」「形が同じ別項目」を区別できない。新しい値で保存し直した事実（「予算は 100」の後の「予算は 120」）や、数値を少し変えて作り直した設定資料は 0.95 以上になり、**新しい版が黙って捨てられる** — ユーザーの最新の決定がメモリに届かない。完全一致の重複のみを拒否する。両方の版が返ってきた場合、`search_memory` は最新の版を先頭に置き、`saved=` 時刻を表示する（[§3.14](#314-検索結果の整形)）。設定キー `dedup_threshold` は無視される。

> **⚠️ 1 つの Redis は 1 つのインストールで使う**：ガード `mem:sha:<sha1>` と `docsha:<sha1>` は本文だけから作り、`owner_id` を含まない。1 つのインストールが使う Redis（この指示書が説明する形：1 台の PC、専用のコンテナ、1 つの `config.json`）では、これが上の「完全一致の重複」の規則になる。2 つの `owner_id` が 1 つの Redis を使う場合 — 何台かの PC や何人かが同じ Redis を指している、または古い記録が残ったまま `config.json` が作り直されて `owner_id` が新しくなった場合 — 一方のオーナーが保存した本文は、もう一方には `duplicate` と答え、ガードが記録と一緒に切れるまで（`ttl_seconds`、既定 7 日）もう一方のオーナーには何も保存しない。何も消えない。Lite 版はこの設計のままとする。インストールごとに 1 つの Redis を使うこと。

### 3.9 起動シーケンスと自己回復

> **これらの手順を動かす場所。** Redis への接続・索引・埋め込みモデルは、MCP の入口ではなく**常駐サーバー**（`python -m n3mc_mcp.cli --run-server`、§3.15）にある。この節は、次のように読み替えて読む。
> - 「サーバー」／「MCP サーバー」／「プロセス」＝常駐サーバーのプロセス。`_main()` ＝ `cli.py` の `run_server()`。
> - 「stdio ループがリクエスト受付を始める前」＝ uvicorn が待ち受けを始める前。手順 1〜3 はそこで行う。手順 4（モデル）は FastAPI の lifespan がバックグラウンドのスレッドで始める。そのため `/health` は数秒で応答し、ロード中に届いたツール呼び出しは下の 20 秒の決まりに従う（「まだロード中」の通知付きで、キーワードだけで検索する）。
> - 「読み込みは 2 段に分ける」の注にあるライブラリの import は、今も uvicorn が始まる前に同期して行う（そこでは害がなく、コードの経路が 1 つで済む）。モデルの構築はバックグラウンドのスレッドで行う。
> - 「MCP クライアントにはサーバーが接続済みと表示される」（§4.4）：薄いクライアントは、`initialize` と `tools/list` に常に自分で応答する（§4.6）。
> - 「MCP クライアントを再起動する」（設定を反映するため）：`config.json` が変わった後の次のフック・CLI・MCP の呼び出しで、常駐サーバーは自分で再起動する（`code_version`、§3.15）。
> - MCP の入口（薄いクライアント）は、これらの手順をどれも行わない。モデルを読み込まず、Redis にもつながない。例外は §4.6「サーバーが使えないとき」の直接保存だけである。

サーバーの `_main()` は stdio ループがリクエスト受付を始める**前**に、次の手順を順に実行する：

1. **設定読み込み**（`load_config()`）：
   - データディレクトリから `config.json` を読み込む。
   - 設定は `~/.n3mc-workingmemory/config.json` に置く。v1.6.x から更新して最初に起動したときは、旧来の場所から引き継ぐ（[§7](#7-データ保存先)）。
   - **ユーザーが書き間違えた `config.json` を上書きしない**（読めないファイルを既定値で置き換えると、ユーザーの設定が失われる）。[§6](#6-設定) の表を参照：読めないファイルは変更せず、このプロセスでは既定値とメモリ上の UUID で動く。型の誤った項目は個別に無視する。Pro 版と違い、Lite では DB からの ID 回復を**試みない** — Redis は TTL 切れで空かもしれないため。
   - `N3MC_REDIS_URL` 環境変数のオーバーライドを適用（ファイルより優先）。
   - ファイルが読めて欠損フィールドがある場合は、既定値で埋めて保存。

2. **Redis 接続と ping**：
   - `redis_url` からクライアントを構築。
   - `PING`。**失敗時**は `stderr` に復旧の案内（下記）を出し、接続のないまま続行する。サーバー自体は生存し続け、ユーザーは MCP を再起動せずに Redis を起動できる（下記「ツール呼び出しごとの Redis の確認」）。
   - クライアントは `socket_connect_timeout=2` **と `socket_timeout=5`** で作る。接続のタイムアウトで、止まっている Redis へのつなぎ直しが数秒で失敗する。読み書きのタイムアウトで、接続は受け付けたが応答しなくなった Redis（`docker pause`、スリープから復帰した直後のホスト、混雑したネットワーク）が、永久に止まらずに 5 秒以内に失敗する。`redis.TimeoutError` は接続断とまったく同じに扱う（下の 3）。
   - **5 秒にする理由**：Lite が行う 1 回の Redis 往復でいちばん大きいのは、長い文書を 1 つのパイプラインで書くとき（§3.8 (B)）で、既定の `chunk_threshold` では 100,000 文字の本文が約 260 チャンクになる。テスト用 Redis（Windows 11、Docker Desktop）での実測：このパイプライン（親・ガード・768 次元ベクトル付きの 260 チャンク）は 0.031 秒（5 回の中央値。最大 0.044 秒）、KNN 1 回と BM25 の `FT.SEARCH` 1 回は 0.007 秒（最大 0.008 秒）で、5 秒より 2 桁小さい。
   - クライアントを作り直す（つなぎ直す）ときは、先に古いクライアントを `close()` し、その接続プールを解放する。

   > **⚠️ 理由**：`socket_connect_timeout` しか設定しないと、Redis のコンテナを一時停止したとき、`ensure_connected()` は 20 秒たっても待ち続ける。ツールはイベントループの中で同期的に動くので、その後のツール呼び出しもすべて固まり、サーバーは自力で回復しない。

   **ツール呼び出しごとの Redis の確認** — 接続は起動時だけでなく、呼び出しのたびに確かめる：

   1. ディスパッチャは、どのツールを実行する前にも `Database.ensure_connected()` を呼ぶ。つながっていれば `PING` を送り、つながっていない（または `PING` が失敗した）ときは `redis_url` からクライアントを作り直して、もう一度 `PING` する。接続が戻ったら `enforce_ephemeral()` と `ensure_index()` をやり直す（新しいコンテナにはインデックスがない）。`stderr` には、状態が変わったとき（切れた／戻った）に 1 行だけ出す。呼び出しごとには出さない。
   2. Redis に届かないときはツールを実行せず、**復旧の案内付きのバックエンドエラー**を返す：`search_memory` / `list_memories` / `recall_thread` → `Error: memory backend unreachable. <案内>` で始まる `TextContent`、`save_memory` → `{"status": "error", "saved": false, "reason": "<案内>"}`、`delete_memory` / `delete_memories_by_session` → `{"status": "error", "reason": "<案内>"}`、`repair_memory` → `{"status": "error", "message": "<案内>"}`。
   3. ツールの**途中で**接続が切れた（`redis.ConnectionError` / `redis.TimeoutError`）ときは、未接続の状態にして、そのツールにも同じバックエンドエラーを返す。ベクトル検索・BM25 の補助関数は、この 2 つの例外を握りつぶさずに**送り出す**こと — 握りつぶすと、止まった Redis が `_No memories found._` になり、LLM には「何も保存されていない」と見える。それ以外の `FT.SEARCH` のエラー（クエリの構文エラーなど）は捕まえ、その経路だけを飛ばす。
   4. **案内の文**（`backend_hint(redis_url)`。このサーバーは世界中で使われるので英語）：`Redis Stack is not reachable at <host>:<port>. If Docker itself is not running, start Docker (Docker Desktop) first. Then run: docker start redis-stack (first time: docker run -d --name redis-stack -p 127.0.0.1:6379:6379 redis/redis-stack-server:latest). The memory server reconnects automatically — no restart needed; just retry.` `<host>:<port>` は `redis_url` から取り、**ユーザー名とパスワードは出さない**。

   > **⚠️ 理由**：Redis を起動時にしか確かめないと、Redis が止まった状態で起動したサーバーは、ユーザーが `docker start redis-stack` を実行したあとも「Redis Stack が起動していません」と答え続け、MCP クライアントを再起動するまで使えない。あとから Redis が止まったサーバーは、`search_memory` が `_No memories found._` を返し、`save_memory` は案内のない生の `Error 10061 connecting to localhost:6379` を返す。Docker Desktop 自体が止まっていると `docker start redis-stack` は Docker の API のエラーになるので、案内に「先に Docker を起動する」を入れる。

3. **RediSearch インデックスの確保**（`ensure_index()`）：
   - [§3.5](#35-データレイアウト) の通り `FT.CREATE n3mc_idx ON HASH PREFIX 1 mem: SCHEMA ...`。
   - `ResponseError` のメッセージに `"already exists"` を含む場合は成功。そのあと、索引のベクトル次元（`FT.INFO n3mc_idx` の `embedding` 属性の `DIM`）を `embedding_dim` と比べ、違えば通知 `index_dim_mismatch` を加える（`index_dim_mismatch: the existing index n3mc_idx was built for <old> dimensions but embedding_dim is <new>; new vectors are not indexed. Drop the index (FT.DROPINDEX n3mc_idx) or flush the test DB, then restart.`）。（古い索引を黙って受け入れると、新しいベクトルは索引に入らない。）続けて §3.7「古い形の索引」の確認をする。
   - **それ以外の失敗でもプロセスを止めない**。サーバーは「索引なし」の状態で動き続け、`ensure_connected()` がツール呼び出しのたびにその前に `ensure_index()` をやり直す。索引がない間は、どのツールも下の 2 のバックエンドエラーを、次の案内付きで返す：`The Redis at <host>:<port> does not provide RediSearch (FT.CREATE failed: <reason>). N3MC Lite needs Redis Stack, not plain Redis: docker run -d --name redis-stack -p 127.0.0.1:6379:6379 redis/redis-stack-server:latest`。`ensure_index()` の途中の接続エラーは、ほかの接続断と同じに扱う。
   - 冪等：毎起動ごと呼んでも安全。

   > **⚠️ 理由**：それ以外の `FT.CREATE` のエラーを `_main()` の外へ再 raise すると、素の Redis に向けたとき（`unknown command 'FT.CREATE'`）— あるいは起動中に接続が切れたとき — プロセスが終わり、クライアントには起動しないサーバーが見えるだけで、手がかりがない。§4.4 はその逆を約束している：サーバーは接続を保ち、LLM がユーザーにすべきことを伝える。

4. **埋め込みモデル事前ロード**（`get_model()`）：
   - `intfloat/multilingual-e5-base` をメモリにロードし、初回ツール呼び出しが一度だけのモデルロードで遅くならないようにする。
   - **まずローカルのキャッシュだけで構築する**：`huggingface_hub.constants.HF_HUB_OFFLINE` を `True` にした状態で（終わったら元の値に戻す）`SentenceTransformer(<model>, local_files_only=True)` で構築する。それが失敗したとき（初回の起動、キャッシュが欠けている）だけ、これらの指定なしで構築し直す（ここでダウンロードする）。**理由**：ファイルがキャッシュ済みでも、通常の構築は各ファイルが更新されていないかを Hub に問い合わせる。transformers 5.x は `local_files_only` を指定しても、tokenizer の読み込みでモデルの情報を Hub に問い合わせる。Windows 11 での実測：構築は通常 5.5 秒・キャッシュだけ 2.0 秒、この構築を待つ `initialize` 直後の最初の `search_memory` は 5.7〜5.9 秒と 1.8〜2.0 秒で、§10-1 の目標 3.0 秒を超えるか下回るかが分かれた。
   - **非致命的**：ロード失敗（オフライン・HF キャッシュ未生成等）時は警告を出して続行。あとの `save_memory` / `search_memory` で遅延リトライするが、**再試行は 60 秒に 1 回まで**（`MODEL_RETRY_SECONDS`）。その間は `embed()` が取得を試みずにすぐ `None` を返す。
   - **ロード中のツール呼び出しが待つのは最大 20 秒**：バックグラウンドのロードがロックを持っている間にモデルを必要とするツール呼び出しは、ロックを最大 `MODEL_WAIT_SECONDS = 20` 秒だけ待つ。それまでにロードが終わらなければ、この呼び出しでは `embed()` が `None` を返す：`search_memory` はキーワードだけで動き、先頭に `> ⚠️ N3MC notice: The embedding model (<embedding_model>) is still loading (the first start downloads ~1.1 GB), so this search used keywords only and new entries are saved without vectors. Retry in a minute.` を付ける。`save_memory` はベクトルなしで保存し、`"warning": "embedding_unavailable", "detail": "<the same text>"` を付ける。ベクトルなしで保存したエントリは、あとで `repair_memory`（§3.10）がベクトルを付ける。**20 秒にする理由**：Hugging Face のキャッシュがあれば、モデルの構築は import のあと約 5 秒（実測、下の注記）なので、通常の起動中に来た呼び出しも 4 倍の余裕でベクトルを得られる。初回のダウンロードは数分かかり、そのままではすべてのツール呼び出し（とイベントループ）を、一般的なクライアントのツールタイムアウトを超えて止めてしまう。**フックはまったく待たない**：フックがサーバー経由で送る保存と検索は `"no_model_wait": true` を付ける（§4.5、§12.3 の手順 4）。その要求の中では、`get_model()` はロックを取らず、待たず、自分でモデルを読み込まない。モデルの準備ができていなければ、上の通知の文とともにすぐ戻る（検索はその通知を付けてキーワードだけで動き、保存はベクトルなしで続け、あとで `/repair` がベクトルを足す）。モデルが読み込まれておらず、読み込みも動いていない（失敗した読み込みの 60 秒の再試行の待ちも過ぎている）ときは、裏のスレッドで読み込みを始める。**理由**：フックは発言のたびにこの呼び出しを 3 回する（前の応答、検索、発言）ので、20 秒の待ちでは、初回のダウンロードの間、発言のたびに約 55 秒（フックの子プロセスのタイムアウト）待つことになる。「フックはサーバーの起動を待たない」（§12.3）と同じ決まりである。MCP のツールと CLI のコマンドは、20 秒の待ちのまま。
   - **劣化中の知らせ**：モデルが使えない間は、黙って劣化せずにそう伝える。`processor.model_status()` は最後の読み込みエラーを返す。`search_memory` は出力の先頭に `> ⚠️ N3MC notice: <model_unavailable_message>` を付け（最初の 1 回だけでなく、劣化中は毎回）、`save_memory` は `"warning": "embedding_unavailable"` の横に `"detail": "<model_unavailable_message>"` を付ける。文面（英語）：`The embedding model (<embedding_model>) could not be loaded (<error>), so search is keyword-only (BM25) and new entries are saved without vectors. The first start downloads the model (~1.1 GB) from Hugging Face: check the network or proxy, or pre-download it. The server retries automatically.` `stderr` には、失敗した試行ごとに 1 行（多くても 1 分に 1 回）出す。

     > **⚠️ 理由**：ネットワークがなくモデルも未取得のとき、ツールの呼び出しのたびにダウンロードをやり直すと、`save_memory` は 1 回約 24 秒、`search_memory` は約 12 秒かかる。知らせがないと `search_memory` にはベクトル検索が止まっている印がなく、ログも `Cannot send a request, as the client has been closed.` だけになる。

> **⚠️ 実装注意（モデルロードは stdio ループ起動「後」にバックグラウンドで）**：`SentenceTransformer` の初期化は HF キャッシュ済みでも PyTorch 初期化のため **~30〜40 秒** かかる（実測 39s）。これを stdio ループ起動「前」に同期実行すると、**MCP `initialize` ハンドシェイクがタイムアウトしてクライアント（Claude Code 等）が接続失敗**する。手順 4 は **`stdio_server()` が listening を開始した後**に `ThreadPoolExecutor` 等でバックグラウンド起動すること。これにより手順 1〜3（実測 ~2s）の完了直後に initialize 応答を返せ、モデルは最初のツール呼び出しが来るまでに準備が整う。
>
> **⚠️ 読み込みは 2 段に分ける（Windows のデッドロック）**：`sentence_transformers`（torch、sklearn など）の import は子プロセスを起動することがある。Windows では、MCP の stdio が stdin のパイプを同期で読んでいる間に子プロセスを作ると、そのハンドルで止まる。バックグラウンドの読み込みが import の途中で止まり、最初のツール呼び出しが `get_model()` のロックで待つので、サーバー全体が固まる。そこで：
>
> 1. **`stdio_server()` を始める前に**、ライブラリの import だけを同期で行う（`import_backend()`）。その間は fd 1 を stderr に向け、余計な出力が JSON-RPC の経路に入らないようにする。実測は温まった状態で約 10 秒。
> 2. **stdio を始めたあとで**、モデル本体の構築（`SentenceTransformer(...)`、温まった状態で約 5 秒。初回のダウンロードもここ）をバックグラウンドで行い、下のロックで守る。
>
> Windows 11 での実測：`initialize` の応答は 9.5 秒、最初の `save_memory` は 15 秒。stdio を始めたあとで子プロセスを起動する処理は、どれも同じ危険がある。
>
> **スレッド安全性**：上記バックグラウンドロードと「最初のツール呼び出し」が競合して `get_model()` が 2 スレッドから同時に呼ばれうる。`get_model()` は **double-checked locking**（`threading.Lock`）でシングルトンを保護すること。

手順 1 は完了してからでないとツール呼び出しを受け付けない。手順 2・3・4 はベストエフォート — 到達できない Redis や、RediSearch のない Redis（手順 3）はプロセスを止めないが、使える状態になるまでツールは無効（ツール呼び出しのたびに再試行する）。**手順 4 は上記の通りバックグラウンド化必須。**

### 3.10 修復

Lite 版の `repair_memory` ツールは **冪等な薄い操作**：再度 `ensure_index()` を呼び、**欠けているベクトルを付け足す**。移行マーカー無し、FTS 再構築無し — 存在している Redis レコードは RediSearch サイドチャネルですでにインデックス化されており、失効レコードは単に消えている。

**再埋め込み**：モデルが使えない間に保存したエントリ（§3.9 手順 4）には `embedding` フィールドがなく、BM25 でしか見つからない。モデルが使えるとき、`repair_memory` は `mem:*` を `SCAN` し（`mem:sha:*` は飛ばす）、設定の `owner_id` のハッシュで `embedding` のないものを最大 200 件、`embed(content)`（passage プレフィックス）を計算し、そのフィールドを条件付きスクリプト `HSET_IF_EXISTS`（§3.3）で書く — TTL は変えない。`SCAN` から書き込みまでの間に失効したレコードは飛ばし（スクリプトが 0 を返す）、`reembedded` に数えない。ただの `HSET` だと、`embedding` だけを持つ TTL のないハッシュとして作り直してしまう。モデルが使えなければ何も再埋め込みしない。応答は `"message": "index ensured"` のままで、`"warning": "embedding_unavailable"` とモデルの `detail` を加える（§3.9 手順 4）。

返却形：成功時 `{"status": "ok", "message": "index ensured", "reembedded": N, "remaining": M}`（`remaining` = この呼び出しのあともベクトルのないエントリの数で、走査しながら数える。0 より大きければもう一度呼ぶ）、失敗時 `{"status": "error", "message": "<詳細>"}`。

これは Pro 版（FTS 句読点移行・vec モデル版移行・未インデックス行修復ループ）からの意図的な簡略化。Lite では最古レコードが高々 7 日なので、移行対象が存在しない。

### 3.11 全文再現（親ドキュメント＋チャンクパターン）

`save_memory` に渡された本文が `chunk_threshold`（既定 400 文字）を超えた場合、サーバーは以下を自動実行する：

1. 本文全体を `chunk_threshold` 文字のスライディングウィンドウ（オーバーラップ `chunk_overlap`、既定 100 文字）に分割する。**どのウィンドウも最低 1 文字は先へ進む**：次のウィンドウは、区切りで切ったときはその区切りのあとから、強制的に切ったときは `end - overlap` から始まるが、現在の開始位置以前には決して戻らない（`start = max(start + 1, …)`）。§6 は `chunk_overlap >= chunk_threshold` も拒否するが、この規則があれば、そのような値で呼ばれても `chunk_text` は安全である。（この規則がないと、`chunk_overlap >= chunk_threshold` のとき、区切りのない本文で無限ループする。）**本文のどの文字も、少なくとも 1 つのチャンクに入る**：最後のウィンドウが 20 文字（`_MIN_CHUNK_CHARS`）より短くなるときは、それを独立したチャンクにせず（短すぎる文は埋め込みの質が低い）、その文を**直前のチャンクの末尾につなぐ**。このため直前のチャンクは `chunk_threshold` より最大 19 文字長くなることがある。（この末尾を捨てると、区切りで切ったあとに残る長文の最後の数文字 — たとえば最終行だけの `PIN: 4471-ZX.` — はどのチャンクにも入らず、親ドキュメントには残っていても、BM25 でもベクトル検索でも、その文字からは文書を見つけられない。）
2. すべてのチャンクの埋め込みを計算し、そのあと（§3.8 (B)）`docsha:<sha1(full_text)>` ガードを取り、`doc:<parent_id>` — **全文 verbatim**（切り詰め無し。§3.13 の入口の 2 つの決まり — 外側の `strip()` と、コードブロック → `[code omitted]` — の後をバイト単位でそのまま保存）— をすべてのチャンクと一緒に **1 つの Lua スクリプト**（`SAVE_WITH_GUARD`。ガードも一緒に）で書き、各 `mem:<chunk_id>` の `parent_id` TAG フィールドに親 ID を格納する（個別 sha ガードはスキップ — 文書全体の重複は親レベル完全一致ガード `docsha:` が担う。[§3.8 (B)](#38-重複判定完全一致のみ) を参照）。

`search_memory` 側の整合：

- `hybrid_search` は通常どおりチャンクを対象に BM25 + ベクトル検索を行い、各候補の `parent_id` を結果辞書に含める。
- ディスパッチャは**語彙リランクの前に**結果を走査し、`parent_id` が非空のヒットについて：
  - 同じ `parent_id` が既出 → 重複として破棄（最高スコアのヒットのみ残す）
  - 初出 → `doc:<parent_id>` を HGET し、`content` を親の全文に置換、ID も親 ID に差し替える。
- 後続の語彙リランク（トークン coverage + フレーズボーナス）はこの時点で**親の全文 verbatim** を対象に動作する。これにより、クエリ句がヒットチャンクではなく同一文書の別チャンクに現れる場合でも親ドキュメントのランクが正しくブーストされる。
- 検索は、チャンクにも、その親にも、兄弟チャンクにも何も書かない（§3.6）。**TTL は設定し直さない** — チャンクのも、親ドキュメントのも、兄弟チャンクのも（§3.3）。親とすべてのチャンクは 1 つのスクリプトが同じ TTL で書くので、保存から 7 日で一緒に失効する。
- 最終出力は markdown 先頭に `[doc×N]`（N=`chunk_count`）タグ付きで表示される。

`list_memories` 側の整合：

- FT.SEARCH に `*`（全件）クエリを使い、返却された `owner_id` フィールドと `parent_id` フィールドを Python 側でチェックして、対象オーナーの独立メモリ（`parent_id` が空文字列）だけを残す（§3.12 参照）。
- これに `SCAN doc:*` でオーナー一致の親ドキュメントを重ねて timestamp 降順でマージする。
- 親は `[doc×N]`（N=`chunk_count`）タグ付きで表示される。

`delete_memory` 側の整合：

- **オーナーの確認**：レコード（または親）が別の `owner_id` のものなら、`{"status": "not_found", "id": ...}` を返し、何も削除しない — `delete_memories_by_session` と同じ範囲。
- ID が `doc:<uuid>` キーを指していれば、削除するチャンクはその `chunk_ids` フィールドの ID である。`chunk_ids` ができる前に保存した親に限り、`@parent_id:{<id>}` の FT.SEARCH、さらに `SCAN mem:*` と Python 側の `parent_id` 照合にフォールバックする（UUID のハイフン問題 — §3.12）。最終的に親＋`docsha:`＋全チャンクを単一パイプラインで削除する。
- それ以外は通常どおり `mem:<uuid>` とその sha ガードを削除する。

**設計上の不変条件**：
- 親レコードは検索インデックスに含めない（`PREFIX 1 mem:` の外に置く）。これによりランキング式は常にチャンク本文に対して計算され、親の長大な本文が time_decay / BM25 を歪めない。
- `stored_importance` はチャンクにのみ付く。親ドキュメントは「verbatim の箱」であり、ランキング要素を持たない。
- 親が生存している限り、チャンクが 1 個でもヒットすれば全文が返る。親とすべてのチャンクは 1 つの `SAVE_WITH_GUARD` スクリプトが同じ TTL で書き、設定し直さない（§3.3）ので、一緒に失効する。万一、親がチャンクより先になくなった場合（Redis で手で消したときなど）、孤児チャンクは個別メモリとして（短文ヒットの形で）表示される（退行ではなく graceful degrade）。

**用途**：ユーザーが「この設定資料 / 仕様書 / 記事をあとで verbatim に取り出したい」と要求する場面。要点抽出との使い分けは [§1 の「長文貼り付けの扱い（2 モード）」](#1-ビジョン) を参照。

### 3.12 TAG クエリの UUID 制約と Python 側フィルタリング

**背景**：RediSearch の TAG フィールドクエリ（`@field:{value}`）は、値内のハイフン（`-`）を特殊文字として解釈する。UUID（例：`041500aa-4b54-4f49-ab4c-82045865072c`）は全てのセグメントにハイフンを含むため、エスケープあり（`\-`）・なし問わず構文エラーになる。この問題は Redis Stack 7.x 上の RediSearch で確認されており、KNN ハイブリッドクエリと BM25 クエリの双方に影響する。

**設計上の決定**：`owner_id`（および検索・削除フローで ID 指定が必要な `parent_id`）による絞り込みを FT.SEARCH クエリから除外し、Python 側でフィルタリングする。

**影響を受けるメソッドと具体的な対処**：

| メソッド | FT.SEARCH クエリ | Python フィルタリング |
|---|---|---|
| `_vector_search` | `*=>[KNN N @embedding $vec AS __dist]`（owner フィルタなし）、RETURN に `owner_id` 追加 | `owner_id` 一致のレコードのみ採用 |
| `_bm25_search` | `(@content_fts:(...) \| @content_ngram:(...))` のみ（owner フィルタなし）、RETURN に `owner_id` 追加 | `owner_id` 一致のレコードのみ採用 |
| `list_memories` | `*`（全件取得）、RETURN に `owner_id`・`parent_id` 追加 | `owner_id` 一致かつ `parent_id` が空文字列のレコードのみ採用 |
| `delete_memory`（cascade） | 親に `chunk_ids` があれば不要。それがない古い親に限り、`@parent_id:{<id>}` で FT.SEARCH を試み、パースエラー時は `SCAN mem:*` でフォールバック | 先にレコードのオーナーを確認する。フォールバック時は `parent_id` フィールド値を Python で照合 |

**パフォーマンス上の注意**：グローバル取得後の Python フィルタリングは、複数ユーザーが同一 Redis を共有する場合（`owner_id` の違うレコードが混在する場合）に余分なネットワーク転送が発生する。Lite 版は単一ユーザー・単一インストールを想定しているため実用上の問題は生じないが、**オーナーが実際に Redis を共有すると、転送量だけの問題ではない**：ベクトルと BM25 の検索は、オーナーで絞る前に**すべての**オーナーの上位の候補（1 回の検索で `min(limit × 5, 100)` 件）を取るので、ほかのオーナーの記録がその候補を埋めると、このオーナーの当てはまる記録が 1 件も返らない。上限を増やしても、これはなくならない。インストールごとに 1 つの Redis を使うこと（§3.8）。マルチテナント要件がある場合は `owner_id` からハイフンを除いた派生フィールド（TAG 用）を別途保持する構成を検討する。

**TAG フィールドは引き続きインデックス定義に残す**：将来の RediSearch バージョンで UUID TAG クエリの構文エラーが解消された場合、上記フィルタリングを FT.SEARCH クエリ側に戻せるようにするため、`owner_id`・`parent_id` の TAG インデックス定義（§3.5）はそのまま維持する。

> **⚠️ 実装注意（`doc.id` はフル Redis キーを返す — 二重プレフィックスバグ多発）**：RESP2（`protocol=2`）では `FT.SEARCH` 結果の **`doc.id` は常にフル Redis キー（`"mem:<uuid>"`）** を返す（HASH の `id` フィールド値である裸 UUID ではない）。`RETURN_FIELDS` に `id` を含めても同様。これを知らずに `MEM_PREFIX + doc.id` とすると **`"mem:mem:<uuid>"` という二重プレフィックスのゴーストキー**が生まれ、`EXPIRE`/`HSET` がそこへ作用して **TTL なしの孤立キー**を作る（§3.4 の「全書き込みに TTL」契約を破る）。
>
> 対処：`_doc_bare_id(doc)` ヘルパーで先頭の `mem:` を 1 回だけ剥がして**裸 UUID に統一**し、行 dict の `id` には裸 UUID を格納する。`MEM_PREFIX` を付ける操作（`EXPIRE`・`HSET`・`DELETE`）は常にこの裸 UUID から組み立てる。**影響範囲は広い**ので全 FT.SEARCH 消費箇所を点検すること：
> - `hybrid_search` の親解決の後：`row["id"]` は裸の親 UUID になる。元のチャンク UUID は別のフィールド（`_chunk_id`）に残し、版の並べ替え（§3.14 手順 4）に使う。
> - `delete_by_session` の **単一メモリ削除**（`MEM_PREFIX + bare_id`）
> - `list_memories` の **表示 ID**（ユーザー向け ID を裸 UUID で統一）
> - `delete_memory` のカスケード（`@parent_id` SCAN フォールバックでも同様に裸 UUID 化）
>
> あわせて **`SCAN mem:*` は `mem:sha:*`（STRING 型）にもマッチする**（`SCAN doc:*` は `docsha:*` にはマッチしない）。HASH フィールドを読む前にプレフィックスで除外するか `WRONGTYPE` 例外を捕捉すること（さもないと `HGET` が `WRONGTYPE` で落ちる）。
>
> **親解決時の `chunk_count` 取得**：チャンクヒットを親に折りたたむ際、`doc:<pid>` から `content` だけでなく **`chunk_count`（と `chunk_ids`・`turn_id`）も取得**しないと、検索結果の `[doc×N]` タグが常に `[doc×0]` になる（1 パイプラインでまとめて取得すると効率的）。

### 3.13 エンコーディング安全策

ツール本体が動く前に、エンコーディング安全策が 2 層走る。Free 版が持つ防御
（`n3mc_hook.py` のストリーム再設定 + `core/processor.py` の
`sanitize_surrogates`）と同じレイヤ構成を Lite にも揃え、Windows 日本語環境で
のベースライン信頼性を担保する。

**(1) stdio の UTF-8 再設定** — `n3mc_mcp.server` モジュールの import 時点
（`n3mc_mcp.cli`、`n3mc_mcp.lite_hook`、`n3mc_mcp.lite_stop_hook` の
モジュールの先頭でも、最初の `sys.stdin.read()` より前に行う — Free の決まり）
（stdout/stderr を触りうる他の import より前）で、`sys.stdin` / `sys.stdout`
/ `sys.stderr` の各ストリームに対し、`reconfigure()` を持っていれば
`encoding="utf-8"` に切り替える（Python 3.7+）。Windows 日本語環境では既定の
コンソールコードページが cp932 のため、これを行わないと MCP JSON-RPC
チャネル上で非 ASCII バイトが軒並み化ける。POSIX 系は既定で UTF-8 のため、
この呼び出しは安全な no-op となる。`hasattr(stream, "reconfigure")` ガード
は、ストリームを生のファイルオブジェクトに差し替えた環境（テストハーネス、
組み込みインタプリタ等）も保護する。

```python
for _stream_name in ("stdin", "stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
```

**(2) 孤立サロゲートのサニタイズ** — （フックの JSON 全体も、
パースした直後に通す。transcript から読んだ直前の応答と、`--search` の引数も
通す。Free §4.5 と同じ）`save_memory.content`・
`save_memory.agent_name`・`save_memory.session_id`・`save_memory.turn_id`
（サロゲートのない値は変わらない）および `search_memory.query` は、`.encode("utf-8")` を伴うあらゆる経路の手前で
`sanitize_surrogates()` を通過する。孤立 UTF-16 サロゲートハーフ
（`U+D800`–`U+DFFF`）は、Windows サブプロセスのパイプが渡してくる UTF-8
バイトを Python のデコーダが `errors="surrogateescape"` でマップした場合に
発生する。これは `json.loads` を素通りするが、SHA1 計算・Redis HSET・
埋め込み生成のいずれかの段階で `UnicodeEncodeError` を投げる。このガードが
無いと書き込みごと黙って失われる（dispatcher が例外を捕捉して汎用の
`Error: ...` 応答を返すが、呼び出し側のコンテンツは Redis に到達しない）。

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

本関数は `dict` / `list` を再帰的に処理するため、ネストした JSON ペイロード
内部に埋まったサロゲート（マルチモーダル tool-call の監査 blob 等）も 1 回で
クリーンアップされる。文字列以外のスカラ（`None`、`int`、`bytes`）はそのまま
通過する。

**退化入力の契約**：`save_memory.content` が *全てサロゲートで構成されていた*
場合、サニタイズ後は空文字列に縮退し、通常の empty-content 拒否経路に合流する
— `{"status":"error","saved":false,"reason":"empty content"}` を返す。これは
決定論的な失敗モードであり、呼び出し側はエンコード起因のサイレントクラッシュ
ではなく明示的な拒否応答を受け取る。

**見える文字のない入力の契約**：`save_memory` は、**見える文字を 1 つも
含まない**本文も拒否する。すべての文字が、空白、制御文字・書式文字（Unicode の
カテゴリ `Cc` / `Cf`。例：`U+0000`、`U+001B`、`U+200B`、`U+FEFF`）、区切り文字
（`Zs` / `Zl` / `Zp`）、異体字セレクタ（`U+FE00`–`U+FE0F`、`U+E0100`–`U+E01EF`）、
または空白に見える文字 `U+115F`、`U+1160`、`U+3164`、`U+FFA0`（ハングルの
フィラー）と `U+2800`（点字の空白）— これらのカテゴリは `Lo` / `So` だが何も表示
されない — のいずれかである場合だ。結合文字は単独でも「見える」
扱いのまま。応答は `{"status":"error","saved":false,"reason":"empty
content (no visible characters)"}`。判定は `processor.has_visible_text()` で行う。
この判定は保存する *かどうか* だけを決め、保存する内容は変えない — 見える文字を
1 つでも含む本文（例：`A\x00B`、単独の結合文字 `U+309A`）は下記のとおり verbatim
に保存する。空白だけの入力の理由は `empty content` のまま。

> **⚠️ 理由**：`str.strip()` は空白を落とすが
> `U+0000` や `U+200B` は落とさない。この判定がないと `\x00` や単独のゼロ幅スペースが、
> 何も表示されない記録として保存される。`list_memories` で見分けがつかず、
> 検索のノイズになるだけである。

**サニタイズ対象に含まれないもの（意図的）** — 以下は **保存・取り出しを通じて
verbatim に保持される**（§3.11 の親ドキュメント verbatim 契約と整合）。表示・
グラフ可逆性・異体字情報・後方互換のため、変換を行わない:

- **`U+0000`（NUL）等の C0 制御文字** — Redis HSET / RediSearch HASH はバイナリ
  セーフのため、NUL を含む文字列もそのまま round-trip する。`A\x00B` のような
  入力は 3 文字として保存される（empty-content 拒否経路には進まない）。こうした
  文字 **だけ** でできた本文は拒否する（上の「見える文字のない入力の契約」）。
- **ゼロ幅スペース・結合用スペーサ**（`U+200B`、`U+200C`、`U+200D`、`U+FEFF` 等） —
  検索時のトークナイズには影響しうるが（RediSearch 側の挙動）、保存本文には残る。
- **異体字セレクタ**（`U+E0100`–`U+E01EF`、`U+FE00`–`U+FE0F`） — `神󠄀` のような
  字形指定が **verbatim** で保たれる（CJK 拡張と組み合わせた異体字情報も同様）。
- **結合用ダイアクリティクス**（`U+0300`–`U+036F`、`U+3099`–`U+309A` 等） — 入力が
  decomposed 形式の場合、保存本文も decomposed のまま。NFKC 正規化は **embedding
  生成と dedup hash 計算のみ**に適用されるため（§3.7 / §3.8）、検索・重複判定は
  decomposed と precomposed を同一視するが、`list_memories` / `search_memory` 応答
  は入力形式を尊重する。

つまり「`save_memory` に渡したバイト列は、サロゲートだけ落として、それ以外は
丸ごと帰ってくる」という最小限のサニタイズ契約を採る。ただし外側の `strip()`
が 1 回だけある：MCP Pro と同じく、`save_memory` は入口で前後の空白（空白・タブ・
改行）を 1 回落とす。`' \nAAA\n '` は `AAA` として保存され、そのまま返る。前後の
空白だけが違う本文は、保存済みの本文の完全一致の重複になる。本文の中はすべて
残す。2 つ目の例外は
コードブロックである（下記）。これは silent な変換が
verbatim 契約を破る可能性を最小化するための設計判断である。

**コードブロックは保存の時点で置き換える**：`save_memory` は、
`skip_code_blocks` の確認（§6）の直後に、コードブロックをすべて `[code omitted]`
に置き換える — `purify(text)` = `re.sub(r"```[\s\S]*?(?:```|\Z)",
"[code omitted]", text)`（閉じていないフェンスは終わりまで。インラインの
バッククォートは残す）。重複判定のハッシュ、チャンク分割、埋め込み、索引、
保存は、置き換えた後の文で行う。`search_memory`・`list_memories`・
`recall_thread` は、見せる文にも引き続き `purify` をかける。1.11.x 以前で保存した
記録（TTL が切れるまで Redis にコードが残っている）も同じに見える。

> **⚠️ 理由**：コードブロックは見せるときに必ず `[code omitted]` になるので、
> 保存しても、どのツールもコードを返せない。場所を取り、検索に影響し、
> 「verbatim」の約束を誤って読ませるだけである。N3MemoryCore が覚えるのは
> 話したことと決めたことで、ソースコードではない（コードはプロジェクトの
> ファイルにある）。コードブロックの中にだけある語では記録に当たらず、コード
> ブロックの中だけが違う 2 つの本文は完全一致の重複になる。

**pre-1.2.0 のモジバケ復旧ルーチンは意図的に Lite には移植しない**。
あのルーチンは初期 Free 版が cp932 でデコードしてしまった行をレトロアクティブ
に書き直すためのもの。Lite は全エントリが `ttl_seconds`（既定 7 日）で消滅
するため、復旧対象となる過去データが存在しない。復旧ルーチンを足したところで
ユーザーが既に ephemeral と受け入れたデータの上でしか動かない。

### 3.14 検索結果の整形

KNN の候補をそのまま全部順位付けしてはならない。`multilingual-e5` では無関係なレコードでもほぼどのクエリに対してもコサイン 0.80 かそれより少し上になるため、`search_memory` は関連の薄い行を長々と返し、事実や文書の更新版が古い版より下に来ることがある（古い版のほうが、周りに一致する文を多く持つことがある）。そこで `search_memory` は候補を次の順で整形する：

1. **時間窓（事前フィルタ）** — `since` / `until` はランキング後ではなく、KNN と BM25 のクエリ内で適用する（[§4.3.1](#431-取り出し拡張ツールretrieval-extensions) 参照）。
2. **ベクトル下限** — `cos_sim < vector_min_cos`（既定 `0.83`）のベクトル候補を落とす。落ちた行も BM25 経由では入りうる（`cos_sim = 0.0` として）。**0.83 は `multilingual-e5-base` で計測した値（0.85 では、言い換えの問いの正しい記録がコサイン 0.83〜0.85 で落ちやすい。0.80 以下では無関係な行が急に増える）。`embedding_model` を変えたら計測し直すこと。**
3. **融合スコア** — §3.6 の式、続いて親ドキュメント解決（§3.11）、語彙リランク（§3.6）、`filter_score` に対する `min_score` の足切り（§3.6）。
4. **新しい版を先頭へ**（`version_order_enabled`、既定 `true`） — ユーザーが設定や事実を作り直すと、旧版と新版の両方が保存され（近似重複は拒否しない、§3.8）、両方が返ってくる。
   - 各結果の**代表埋め込み**を取る：独立行は自身の `embedding`、親ドキュメントは先頭チャンク（`chunk_ids[0]`）の `embedding`。代表テキスト（独立行の `content`、または先頭チャンクの `content`）が `version_min_chars`（既定 `100`）より短い行は対象外 — 短い行同士は似すぎていて、版違いと別の事実を区別できない。
   - 代表コサインが `>= version_sim_threshold`（既定 `0.98`）の 2 結果は同じ内容の版とみなし、推移的にグループ化する。
   - 各グループ内で行を `(timestamp_epoch, id)` の新しい順に並べ替える（`id` は UUIDv7 なので、同じ秒に保存した行も順序が付く）。**各位置のスコアはそのまま** — 動くのは行だけなので、最新版がグループ内の最高スコアを引き継ぐ。**2 件以上のグループの全員**に `_version_group`（1 から始まるグループ番号）を付ける。最新版がもともと先頭で何も動かなかったときも付ける（下の相対カットで旧版が残るように）。何も削除しない。`chunk_ids` がなかった時期に保存した親ドキュメントは代表がないので、グループにしない。
   - 0.98 は `multilingual-e5-base` での計測値：同じ設定の作り直し → 1.0、形が同じで値が違う設定 → 0.88〜0.91、1 つの長い返答内の別項目 → 最大 0.95。
5. **相対カット** — **`filter_score`** が最も高い `filter_score` × `relative_min_score_ratio`（既定 `0.6`）未満の結果を落とす。ただし `_version_group` の印が付いた行は除く（旧版を新版の隣に残すため）。`0.0` で無効化。手順 4 は各位置の `score` をそのままにして行を動かすが、`filter_score` も同じように位置と一緒に残る。
6. **弱い候補による最低件数の補充** — 手順 5 の後に残った結果が `min(min_results, limit)` 件に満たないとき（`limit` は `search_memory` の引数、なければ `search_result_limit`）、**この検索で取得したが手順 2・3・5 で落ちた候補**から、足りない分を足す。
   - 弱い候補になるのは、手順 2 の `vector_min_cos` で落ちたベクトル候補（**実際の `cos_sim` で計算する**。BM25 のみの行としての `0.0` ではない）と、`min_score`（手順 3）・相対カット（手順 5）で落ちた結果。手順 2 で落ちて BM25 にもヒットした行は、実際の `cos_sim` と `keyword_relevance` を持つ 1 件の候補とする。`filter_score` と `score` は、普通の行と同じ親ドキュメントの解決（§3.11）と語彙リランクを通して §3.6 の式で計算する。`filter_score` が `1e-6` 未満（類似度もキーワードも寄与なし。float32 のベクトルのコサイン距離では、直交するベクトルでも約 `1e-7` が残るので、ちょうど `0` とは比べない）の候補は足さない。`since` / `until` の範囲は普通の行と同じく効く（弱い候補も同じ事前フィルタ済みのクエリから来る）。
   - **`filter_score` の高い順**に取る（同じ値なら `(timestamp_epoch, id)` の新しいほうが先）。`score` は `score=` に表示するが、弱い候補の並び順には使わない。
   - **親ドキュメントごとに 1 行**。すでに結果にある行と合わせて数える：解決先の親ドキュメントがすでに結果にある（または弱い候補としてすでに足した）候補や、同じ独立 `id` の候補は飛ばして次を見る。
   - 合計が `min(min_results, limit)` に達するか、候補がなくなったら止める。足した行にはすべて `_weak` の印を付ける。弱い候補は常に普通の行のすべてより後に来る。
   - 検索は、普通の行にも弱い候補にも何も書かず、どの行の TTL も変えない（§3.3／§3.4／§3.6）。
   - `min_results: 0` ではこの手順を行わない。**空のクエリ、または空白だけのクエリでも行わない**（弱い候補は足さない）：空のクエリでもベクトル検索は空文字列を埋め込んで行を返すので、中身のないクエリに補充しても雑音が増えるだけである。
   - 弱い候補は手順 4 と 5 に関わらない（版の並べ替えで動かさず、相対カットの 1 位の値にも数えない）。
7. 普通の行を `limit` 件に切り詰める。**何も書かない**：`access_count` を増やさず（§3.6）、`EXPIRE` もガードの書き込みもしない（検索は TTL を変えない、§3.3）。

> **⚠️ `filter_score` で判定する理由**：最終スコアにはすでに `time_decay`（半減期 3 日）と `b_session`（別セッションは 0.6）が掛かっている。最終スコアで比べると、関連度が同じなら、トップのヒットより約 2.2 日古い記録（0.5^(2.2/3) ≈ 0.6）と、別セッションの記録はすべてカットを下回る — しかも既定の `session_id` はサーバープロセスごとの新しい UUID なので、MCP サーバーが最後に再起動する前に保存した記録はすべて別セッション扱いになる。例：現セッションの新しい記録が cos 0.86（スコア 0.602、カット 0.361）のとき、同じセッションの 3 日前の記録 cos 0.95（0.332）と、別セッションの 1 日前の記録 cos 0.99（0.330）・0.95（0.317）がすべて落ちる。Lite は 7 日のワーキングメモリなのに、最近のヒット 1 件が 2 日より古いものをすべて隠してしまう。

> **注: 弱い候補を足す理由**：実モデルでの測定（記録 50 件、質問 26 問、別セッションから検索）では、**どの質問でも結果は最大 1 件**（平均 0.9 件）だった。recall@1 と recall@3 がどちらも 22/26 で、1 位が外れると正解は出ず、26 問中 2 問は何も返らなかった。3 件まで補うと、LLM は次の候補を別の見出しの下で見られる。**欠点**：検索のたびに最大 3 行が増え、その大半は関係の薄い記録で、無関係な質問でも必ず返る。Free（同じ記録と質問）では、補充で recall@3 が 23/26 から 24/26 になり、1 問あたりおよそ 2 件の弱い候補が加わった。見出し、`min_results` の名前、指示の文面は N3MC Free と共通にする。

**出力形式**（`search_memory` の markdown）：

```markdown
> ⚠️ N3MC notice: <only if a notice is pending, §6, or the embedding model is unavailable, §3.9>

_If several versions of the same content appear, the one with the newest `saved` time is the current one._

### [doc×3] [<id>] turn=<turn_id> score=0.812 saved=2026-11-13 10:34 (UTC+0900)
<content>

#### Possibly related (weak matches — may be unrelated)

### [<id>] turn=<turn_id> score=0.412 saved=2026-11-12 21:15 (UTC+0900)
<content>
```

- `saved=` は保存された UTC タイムスタンプを**サーバーのローカル時刻**に変換し、オフセットを付けたもの（`_fmt_saved`）。
- 版についての一文は、ヒットが 1 件以上あれば最初のヒットの上に 1 回だけ出す。
- `turn=` は行に `turn_id` が無ければ省略。`[doc×N]` は親ドキュメントのみ。
- **弱い候補**：`_weak` の行は、普通の行のすべての後に、`#### Possibly related (weak matches — may be unrelated)` の見出しの下に、同じ `### …` の形で出す。見出しは弱い候補が 1 件以上あるときだけ出す。
- **普通の行が 0 件で弱い候補があるとき**：空の結果の文面 `_No memories found._` を出し、その後に弱い候補の見出しと行を出す。この場合、版についての一文は出さない。どちらも 0 件のときは `_No memories found._` だけ。
- フックの `# Recalled Memory Context` の塊の中でも、同じ表示を使う（§3.17、§12）。Lite には、別に Q-A のやりとりの塊（Free の `## Previous matching Q-A exchanges`）は**ない**。ヒットはレコード 1 つか親ドキュメント全体であり、その前後の会話は `recall_thread` / `--recall-thread` で読む。

### 3.15 常駐サーバー

この節はすべて N3MC-Free §3「Resident FastAPI Server」から、名前を Lite のものにして取り入れた。サーバーは Lite の中核 — §3.3〜§3.14 の `Database`、§3.2 / §3.9 の埋め込みモデル、§4.3 のツールのハンドラ — を動かす。ほかにモデルを持つものはない。

**常駐サーバーにする理由**：フックはメッセージのたびに新しいプロセスとして動く。そこでモデルを読み込むと、メッセージごとに 13〜40 秒かかる（§3.9）。MCP クライアントがそれぞれ自分のサーバープロセスを起動すると、それぞれがモデルを読み込むことになる。1 つの常駐サーバーが、フック・CLI・すべての MCP クライアントのために、モデルを 1 回だけ読み込む。

- **ポート**：`config.json` の `server_port`。既定は **`18524`**。ほかの N3MC 製品は 18520（Free）、18521（Pro）、18522（PostgreSQL 版）、18523（Ollama 版）を使う。そのため Lite は、それらのどれとも同じ PC で動かせる（§14）。
- **待ち受けるアドレス**：`bind_host`（既定 `127.0.0.1`）。**既定のままにする。** サーバーに認証はない。ほかの値にすると、記憶がネットワークにさらされる。ほかの値が設定されていれば、起動時に stderr に警告を出す。
- **起動**：`python -m n3mc_mcp.cli --run-server`（`run_server()`）。サーバーを使う CLI のコマンド、フック、MCP の薄いクライアントから、`ensure_server`（下記）が起動する。PID ファイルは排他的な作成（`open(path, "x")`）で作る。同時に確かめた 2 つのプロセスが、両方ともサーバーを起動しないようにするためである。
- **通信**：TCP 上の HTTP（`http://127.0.0.1:<server_port>`）、JSON の本文、UTF-8。**プロキシは決して通さない**：リクエストはすべて `urllib.request.urlopen` ではなく、`urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=…)` で開く。（Free と同じ。`HTTP_PROXY` が設定され、`127.0.0.1` の `NO_PROXY` がないと、すべてのリクエストがプロキシに行き、サーバーが止まっているように見える。）
- **Host ヘッダの確認**：`Host` ヘッダが `127.0.0.1:<port>` か `localhost:<port>` でなければ、どのエンドポイントも 403 を返す（そうしないと、Web ページからの DNS リバインディングで記憶を読まれうる）。CLI と薄いクライアントは、常に `Host: 127.0.0.1:<port>` を送る。テストではヘッダを明示する（FastAPI の `TestClient` は `testserver` を送る）。
- **`Origin` ヘッダ付きのリクエストを断る（Free v1.9.1 から）**：リクエストに `Origin` ヘッダがあれば、値に関係なく（`null` を含む）、どのエンドポイントも 403 を返す。ブラウザは、Web ページが別のサイトに送るリクエスト（`fetch`、フォームの POST）に `Origin` を付ける。サーバーは `127.0.0.1` で認証なしに待ち受けるので、利用者が開いたページからリクエストを送れてしまい、`Host` の確認では止まらない（その `Host` は `127.0.0.1:<port>` になる）。フック、CLI、薄いクライアントは `Origin` を送らないので、この確認の影響を受けない。この確認は `Host` の確認と同じ場所で、どのエンドポイントの処理よりも前に行う。
- **エンドポイントは互いを止めない**：Redis かモデルにさわるエンドポイントは、すべて普通の `def` にする（FastAPI がスレッドプールで動かす）。そのため長い保存や `/repair` の間も `/health` は応答し続ける。Lite の中核には、次のことが必要になる。`Database` はサーバープロセスに 1 つ（redis-py のクライアントとプールはスレッドセーフ）。`ensure_connected()` と §3.9 のつなぎ直しは 1 つの `threading.Lock` の下で動く。通知のリスト（`cfg["_notices"]` と「表示済み」の集合）はロックの下で変える。`get_model()` は二重チェックのロックを保つ（§3.9）。
- **タイムアウト（クライアント側）**：`/health` と `/shutdown` は 5 秒。`/buffer`・`/search`・`/repair` は 30 秒（Free と同じ）。`/tools/<name>` は 300 秒（長い文書の `save_memory` は CPU で数百のチャンクを埋め込む。MCP クライアントはどのみちツールの結果を待つ）。サーバーに停止を頼んだ後は、そのポートが空くまで最大 15 秒、0.3 秒ごとに確かめて待つ。終わらせる必要のあるプロセスは `terminate()` し、5 秒待ってから `kill()` する。`/shutdown` は先に応答し、0.3 秒後にプロセスを終える。uvicorn は `log_level='error'` で動かす。

#### サーバーの識別（`/health`）

`GET /health` は `{"status": "ok", "root": "<absolute path of the data folder>", "code_version": "<16 hex chars>", "pid": 12345, "redis": "<host>:<port>/<db>"}` を返す。

- `root`：データフォルダの絶対パス（`N3MC_DATA_DIR` か `~/.n3mc-workingmemory`）。`os.path.normcase(os.path.abspath(...))` で比べる。
- `code_version`：`app.py`・`tools.py`・`tooldefs.py`・`instructions.py`・`context.py`・`database.py`・`processor.py`・`config.py`・`paths.py`・`cli.py` の内容に、続けて `config.json` の内容を足したものの SHA-256 の、先頭 16 桁の 16 進数。`config.json` は、JSON のオブジェクトとして読めればキーを並べた `json.dumps(..., sort_keys=True)`、読めなければ生のバイト列、ファイルがなければ何も足さない。空白やキーの順だけでは変わらない。**そのため `config.json` を直すと、次の呼び出しでサーバーが再起動する。** 変えた設定はこうして反映される。サーバーはこの値を**起動したときに 1 回だけ、`load_config()` が使ったのと同じ `config.json` の内容（読んだバイト列。補完して書いたときは書いたバイト列）から計算し、ファイルを読み直さない。計算は Redis への接続やモデルの読み込みより前で、`run_server` がそれを `create_app(..., code_version=...)` に渡す。**（ファイルを 2 回読んだり、もっとあとで計算したりすると、起動の途中で `config.json` が変わったとき、サーバーは 1 回目の内容で動くのに 2 回目の内容のハッシュを返し、次に変えるまで置き換わらない。） `/health` は以後その値を返し続ける（`/health` のたびに計算し直すと、動いているサーバーは常にディスクのファイルと一致して見え、置き換わらない）。
- `pid`：サーバーのプロセス ID。
- `redis`：サーバーが使う Redis（**サーバーの**環境の `N3MC_REDIS_URL` で上書きしたあとの `redis_url`、§3.3）。ユーザー名とパスワードは除く。**`redis_url` を解析できないとき**（例：ポートが数字でない）は、`redis_display` は固定の文 `<unparseable redis_url>` を返し、入力のどの部分も返さない（入力を返すと、`redis_url_mismatch` の通知を通じて、パスワードが `/health`、stderr、`notices.json`、`audit.log` に届きうる）。Redis の場所を示す文は、どれも `redis_display` か `_hostport` を使う。サーバーは、それを起動したプロセス（フック、CLI、MCP の入口）の環境を引き継ぐ。そのため `ensure_server` は、これを自分の実際の `redis_url` と比べる。違うときは、そのサーバーをそのまま使い（データフォルダは同じ）、通知 `redis_url_mismatch` を出す（`The N3MC Lite server uses Redis at <server's>, but this process is set to <own>. Put redis_url into <config.json path> instead of setting N3MC_REDIS_URL per application, then restart the server (it restarts after config.json changes).`）。これだけでサーバーを置き換えることはしない。設定の違う 2 つのアプリが、呼び出しのたびに互いのサーバーを止め合うからである。**消すとき**：自分のサーバーが答え、その `redis` がこのプロセスの実際の `redis_url` と一致したら、`ensure_server` は `redis_url_mismatch` を消す（`_clear_notice`）。（消さないと、設定を直したあとも通知が 1 日 1 回ずっと出る。）

#### サーバーの確認と置き換え（`ensure_server`）

`_health_decision(health)` は `'ok'`・`'stale'`・`'foreign'`・`'legacy'`・`'none'` のどれかを返す：

| 応答 | 判定 | 動作 |
|---|---|---|
| 同じ `root`、同じ `code_version` | 正常 | それを使う |
| 同じ `root`、違う `code_version` | 自分の古いサーバー | 止めて、新しいものを起動する |
| 違う `root` | ほかのフォルダのサーバー | **止めない。使わない。**「ポートがほかのフォルダのサーバーのものだったとき」を参照 |
| `root` がない | 確かめが要る | psutil で自分のサーバーだと確かめられたときだけ止める。そうでなければ、ほかのフォルダのサーバーとして扱う（`root` は `(unknown: the server did not report its folder)` と表示する） |
| 応答がない | サーバーがない | 「PID ファイルの扱い」を参照 |

- 実行するもの：`--hook-submit`、`--save-claude-turn`、`--search`、`--buffer`、`--list`、`--recall-thread`、`--delete`、`--delete-session`、`--repair`、MCP の薄いクライアント（§4.6）。`--init`、`--stop`、`--run-server` は実行しない。
- **止め方（`_stop_stale_server`）**：`{"root": "<our data folder>"}` を付けて `POST /shutdown` を送る。`/shutdown` を知らないサーバーだけを、psutil で確かめた後に終わらせる。**PID ファイルの番号でプロセスを止めてはならない**（その番号は使い回されているかもしれない）。
- **psutil での確かめ（`_is_own_server_process`）**：PID は `/health` の `pid` から、またはポートで待ち受けているプロセス（`psutil.net_connections`）から取る。コマンドラインに `n3mc_mcp.cli` と `--run-server` の両方を含み、かつその `N3MC_DATA_DIR` 環境変数から決まるデータフォルダ（既定 `~/.n3mc-workingmemory`）が自分のものと同じときだけ、「自分のサーバー」である。psutil がない、権限で拒否された、または何かが一致しないときは「確かめられない」：止めない。通知 `stale_server`（"Cannot stop the old N3MC Lite server. Another program may be using server_port <port>. Check server_port in config.json and restart manually."）を出す。自分の古いサーバー（`root` が同じで `code_version` が違う）を止められなかったときは、**`/health` をもう一度見る**。`ok` ならそれを使い（ほかのプロセスが入れ替えを済ませた。2 つのプロセスが同時に入れ替えようとすると、遅れた側の `/shutdown` は失敗する）、答えがなければ止まったものとして「サーバーの起動」に進む。まだ古いサーバーが答えるなら、**そのサーバーは使わない**：この呼び出しはサーバーなしで進む（保存は直接保存（§3.16）、検索は `_(memory search unavailable: an old N3MC Lite memory server could not be stopped)_`、ほかのツールは §4.6 のバックエンドエラー）。`code_version` には `config.json` の内容が入るので、古いサーバーは別の `redis_url` を使っていることがある。使い続けると、`redis_url` を変えたあとも記録が古い Redis に入る。
- **PID ファイルの扱い**：`/health` が応答せず、PID ファイルの番号のプロセスがこのフォルダのサーバー（起動中）だと確かめられたときは、PID ファイルを残し、最大 60 秒（0.5 秒ごと）`/health` を確かめる。60 秒たってもまだ起動中（初回の起動はモデルをダウンロードする）なら、別のサーバーを起動せず、PID ファイルも消さない。この呼び出しはサーバーなしで続ける（下記）。それ以外（プロセスが死んでいる、番号が使い回された）のときは、PID ファイルを消してサーバーを起動する。それでも `import psutil` が失敗したときは、PID ファイルのプロセスを、ファイルの更新時刻から 60 秒の間は「まだ起動中」、その後は死んでいるものとして扱う。
  - **サーバーが別のポートにいるとき**：確かめた PID ファイルのプロセスを待つ前に、それがいまの `server_port` 以外のポートで待ち受けていないかを調べる（psutil で、その PID の `LISTEN` の接続）。サーバーが動いている間に利用者が `server_port` を変えると、こうなる。新しいポートの `/health` はずっと答えないので、この確認がないとサーバーは再起動しない（CLI は呼び出しのたびに 60 秒待ち、フックはずっとベクトルなしで保存する）。そうしたポートのうち `/health` がこのフォルダの `root` を返すものに `POST /shutdown`（`{"root": "<自分のデータフォルダ>"}`）を送り、そのポートが空くまで待ち（上と同じ 15 秒）、PID ファイルがまだその PID を持っていれば消し、待たずに「サーバーの起動」へ進む。`/health` が答えないポートや、別の `root` を返すポートには何もしない。PID ファイルの番号でプロセスを終わらせることはしない。起動中のサーバーはまだどのポートでも待ち受けていないので、上の待ちはそのまま働く。この確認には、PID ファイルにサーバー自身の PID が要る。サーバーは自分でそれを書く（§13「PID ファイル」）。サーバーの `Host` の確認は起動時のポートを使う（§4.5）ので、古いサーバーは自分のポートで答え続ける。
- 自分が起動したサーバーが応答したら、通知 `stale_server`・`port_conflict`・`server_start_failed` を消す。

#### サーバーの起動（`_start_server`）

1. `subprocess.Popen([*python_cmd(), "-m", "n3mc_mcp.cli", "--run-server"], stdin=DEVNULL, stdout=DEVNULL, stderr=<server.log opened for append>, …)`。`server.log` が 1 MB より大きければ、先に空にする。切り離して起動する：Windows では `creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`、それ以外では `start_new_session=True`。そのためサーバーは、それを起動したフックや MCP の入口より長く生きる。ただし、子プロセスをジョブオブジェクトに入れ、閉じるときに子孫ごと終わらせる MCP クライアント（`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` で、ジョブからの離脱を許さないもの）では、サーバーは入口と一緒に終わることがある。クライアントとその `mcp` の版によって、サーバーは入口と一緒に終わるか、入口を閉じた後も動き続ける（`mcp` 1.27.0・Windows 11 では動き続ける）。どちらも正しい動作である。記憶は失われない：次の呼び出しがサーバーを起動し直し、そのあいだの保存はベクトルなしの直接保存になる（§4.6）。`-m` の形が必須である（パッケージ相対の import のため）。ファイルのパスで起動してはならない。サーバーの stdout は、フックや MCP の入口の stdout には決して届かない。**作業フォルダはデータフォルダにする**（`cwd=<データフォルダ>`：`N3MC_DATA_DIR` か `~/.n3mc-workingmemory`）。サーバーは何日も動き、Windows ではプロセスの作業フォルダを消すことも名前を変えることもできないので、たまたまサーバーを起動したフックや MCP の入口のフォルダ（利用者の作業ツリーなど）にしない。例外は、ソースのフォルダでそのまま動かすとき（このパッケージの親フォルダが今のフォルダなので、`python_cmd()` は `-P` を付けない。下の「安全なインポートパス」）で、子はそこからしかパッケージを見つけられないので、そのフォルダにする。サーバーの中の処理は作業フォルダに頼らない。子の環境の `N3MC_DATA_DIR` には、データフォルダを絶対パスで渡す（利用者が相対パスを設定していると、新しい作業フォルダから見て別のフォルダを指すため）。
2. 0.5 秒ごとに最大 60 秒、`/health` を確かめる。その間に子プロセスが終わったら（`proc.poll() is not None`）、すぐに待つのをやめる。
3. その早い終了のとき：`server_start_failed`（"The N3MC Lite memory server could not start (exit code <n>). See <server.log path> for the reason."）を出し、データフォルダの目印のファイル `server_start_failed` に時刻を書き、PID ファイルが子プロセスの PID を持っていれば消し、サーバーなしで続ける。`Popen` 自体が例外を出したときは、サーバーなしで続ける（stderr に 1 行）。
   - **サーバーのプロセス自身も起動の失敗を記録する**：`run_server` が例外で終わるとき、または 0 / `None` 以外のコードの `SystemExit` で終わるとき（uvicorn はポートを bind できないとき、こう終わる）は、終わる前に自分で `server_start_failed`（"The N3MC Lite memory server could not start (<exit code n \| ErrorType: message>). See <server.log path> for the reason."）を出し、目印のファイルを書く。例外は握りつぶさない。**理由**：フックは `ensure_server(cfg, max_wait=0)` で呼ぶので子プロセスの終了を見ない。これがないと、起動できないサーバーをフックのたびに起動し直し、通知も 5 分の待ちも働かず、表示は「still starting」のままになる。
4. 目印が 5 分より新しい間は、サーバーをもう一度起動しようとしない。すぐにサーバーなしの経路に進む。サーバーが応答したら、目印を消し、通知を消す。
5. 60 秒たってもまだ生きている子プロセスは、まだ起動中である。そのまま動かしておく（上の「まだ起動中」の決まり）。

#### 安全なインポートパス

`python -m <モジュール>` は、今のフォルダを `sys.path` の先頭に置く。フックと規則ファイルが Claude に示すコマンドは、利用者が Claude Code で開いているフォルダで動く。下の 2 つの決まりがないと、そのフォルダにある、N3MC が import するモジュールと同じ名前のファイル（たとえば、利用者がクローンしたばかりのリポジトリの中の `shlex.py` や `fastapi.py`）が import され、N3MC の中で動く。

**決まり 1 — 入口のモジュールは、今のフォルダを外す。** パッケージの中で、プログラムの入口になりうるモジュール（`if __name__ == "__main__":` で終わるもの。ここでは `cli.py`, `lite_hook.py`, `lite_stop_hook.py`, `server.py` and `__main__.py`）は、次のブロックを**最初の文**として持つ。docstring の直後、ほかのどの import よりも前に置く（フックのモジュールでは、UTF-8 のブロックをこのあとに置く）。

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

- モジュールがプログラムとして動いたとき（`python -m …`）だけ働く。コンソールスクリプトやテストから import されたときは、何もしない。ライブラリは、自分を import したプログラムの `sys.path` を変えてはならない。
- ブロックが動く時点で、`os` と `sys` は読み込み済みである。import しても、ファイルを読まない。
- パッケージ自身が今のフォルダにあるとき（インストールせずに、ソースのフォルダでそのまま動かすとき）は、外さない。外すと、パッケージが見つからなくなる。

**決まり 2 — 子プロセスは `python_cmd()` で起動する。** `paths.py` に次の関数を置く。

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

子の Python のプロセス（上の「サーバーの起動」のサーバーと、フックのモジュールからの呼び出し）は、すべて `[*python_cmd(), "-m", …]` で起動する。`[sys.executable, "-m", …]` で起動してはならない。`-P`（Python 3.11 から）は、今のフォルダを `sys.path` に足すこと自体をやめさせる。今のフォルダに、このパッケージと同じ名前のフォルダがある場合にも効く。

**この決まりで防げないもの**: 今のフォルダにある、このパッケージ自身と同じ名前（`n3mc_mcp`）のフォルダやファイル。そのフォルダで `python -m …` と打ってプログラムに入るとき（規則ファイルが Claude に示すコマンドと、`python -m` の形で登録されたフック）は、もう一方のパッケージが import されるので、ブロックに届かない。Python 3.10 では、子プロセスも同じである（`-P` がない）。そのフォルダに置かれた `runpy.py` も動く。コンソールスクリプトから入るか、Python 3.11 以降で `python -P -m …` を使えば、起きない。（今のフォルダに `shlex.py` を置いても、この決まりがあれば `python -m n3mc_mcp.cli` はそのファイルを動かさない。そこに `n3mc_mcp/` というフォルダがあれば、動く。）

#### `/shutdown`

`{"root": str}` を付けた `POST /shutdown`：`root` がサーバー自身のデータフォルダと同じときだけ、`{"status": "ok"}` と応答し、0.3 秒後に終わる（PID ファイルが自分の PID を持っていれば消す。ファイルを読んで閉じてから消す：Windows は開いているファイルを消せない）。そうでなければ 403。テストでは `_request_shutdown` と HTTP の送り手を差し替え、本物のサーバーには何も届かないようにする。

#### ポートがほかのフォルダのサーバーのものだったとき

`ensure_server` は False を返し、そのサーバーの `root` を `_FOREIGN_SERVER_ROOT` に残し、`port_conflict`（"Another N3MC server (<root>) is using port <port>. Give N3MC Lite its own server_port in <config.json path>."）を出す。そのうえで、ほかの製品の記憶にレコードが入らないように、次のようにする。

- フックの保存は、ベクトルなしで Redis に直接行う（§3.16「直接保存」）。MCP の `save_memory` も同じにする（§4.6）。
- 検索はしない。フックと `--search` は `# Recalled Memory Context\nSource: N3MC Lite (7-day working memory)\n\n_(memory search unavailable: another N3MC folder's server is using port <port>)_\n` を出力する。
- `--repair`、`--list`、`--recall-thread`、`--delete`、`--delete-session` と、ほかの MCP のツールは、§4.6「サーバーが使えないとき」のバックエンドエラーを、その理由を付けて出力するか返す。

#### 通知（`_notify`）

人の対応が要り、サーバーの外から来る通知 — `stale_server`、`port_conflict`、`server_start_failed`、`redis_unreachable`（§12）、`save_failed`（§12）、`rules_md_edited:<hash>`（§12）、`redis_url_mismatch`（上の「サーバーの識別」）— は、Free の `_notify(key, message)` を通す：

1. stderr に出す。
2. `{"ts", "hook": "notice", "key", "message"}` を `audit.log` に追記する（同じキーとメッセージでは 1 回だけ）。
3. データフォルダの `notices.json` に残す。これは 1 つの JSON オブジェクト `{<key>: {"message": <text>, "last_shown": <epoch seconds or null>}}` で、一時ファイルと `os.replace` で書く。キーごとに **1 日 1 回**表示する（`last_shown` が null か、86,400 秒以上前のとき。表示すると `last_shown` を設定する）。`last_shown` が数として読めない（文字列、壊れた値）ときは、まだ出していないもの（null）として扱い、出したあと今の時刻を書く。例外にすると、検索とその発言の保存が止まる。
4. 原因がなくなったら、`_clear_notice(key)` が消す。

**通知を表示する場所**（サーバーの `_take_notices()`）：どちらの種類でも最初に成功した検索 — フックか CLI の `/search`、または MCP の `search_memory` — に、§6 の Lite のプロセス内の通知（`config_unreadable`、`config_type`、`shared_redis`、`config_refused`、`index_dim_mismatch`。§6 と同じくサーバープロセスごとに 1 回）を出し、続けて表示の時期が来た `notices.json` の通知（1 日 1 回）を、`> ⚠️ N3MC notice: <message>` の行として出す。§3.9 のモデル劣化の通知は、劣化した検索のたびに足す。フックと CLI の劣化時の代わりの出力（§12）も、表示の時期が来た `notices.json` の通知を、見出しのすぐ後に出す。壊れているのがサーバーそのものかもしれないからである。

テストでは、`notices.json`、`audit.log`、PID ファイル、`server.log`、目印は、常に一時的なデータフォルダに置く。

### 3.16 フックのレコード：役割の前置きと直接保存

フックの保存は `POST /buffer`（§4.5）を通る。これは §3.8 / §3.11 / §3.13 の Lite の `save_memory` を、引数を 1 つ足して呼ぶ。足す引数は `role`（`"user"` か `"claude"`）である。**`save_memory` の決まりはすべてそのまま当てはまる** — 孤立サロゲートの除去、外側の `strip()`、空と見えない本文の確認、`skip_code_blocks`、`[code omitted]`、チャンクの閾値、Lite のチャンク分割（`chunk_threshold` 400 / `chunk_overlap` 100、§5 (7)）、親ドキュメント、`mem:sha:` / `docsha:` による完全一致の重複の拒否、`SET NX` で取るガード、1 つのパイプライン、TTL。`importance` は 1.0。役割の前置きは、`purify` の**後**、長さの判定の前に、次のように付ける（Free の `add_chunk_prefixes` の形）：

| `strip()` と `purify` の後の本文（`B`） | 保存するもの |
|---|---|
| `len(B) <= chunk_threshold` | 独立したレコード `mem:<id>` 1 つ。その `content` は `[<role>] ` + `B`。 |
| `len(B) > chunk_threshold` | `chunks = chunk_text(B, chunk_threshold, chunk_overlap)`（前置きのない `B` に対する Lite のチャンク分割）。親ドキュメント `doc:<id>` の `content` は `[<role>] ` + `B`。*N* 個のうち *i* 番目のチャンクの `content` は `[<role> i/N] ` + チャンク *i*。`chunk_text` が 1 つのチャンクしか返さないとき（区切りで切ったあとの残りが 20 文字より短い本文、§3.11。区切りのない本文は、閾値を少し超えるだけでも 2 つのチャンクになる）は、`[<role>] ` + そのチャンク。 |

- 長さの判定には、前置きを付けた文ではなく `B` を使う。そのため同じ文は、フックから来ても MCP から来ても同じ経路をとる。
- ガードは、保存するものを**そのやりとりの中で**ハッシュする（下記）：独立したレコードなら `mem:sha:<turn_id>:<sha1(NFKC([<role>] B))>`、親なら `docsha:<turn_id>:<sha1(NFKC([<role>] B))>`。Lite のガードと同じく、`SET NX` で取り、レコードの TTL を持つ。`turn_id` のないフックの保存は起きない（§12 は常に持つ）。**このガードの削除**：§3.5 / `delete_memories_by_session` の削除は、保存したレコードからガードのキーを計算し直す。`turn_id` を持ち、**かつ** `content` が役割の前置き（`[user` か `[claude`。つまりフックのレコード）で始まるレコードは、`mem:sha:<turn_id>:<sha1(NFKC(content))>`（親なら `docsha:<turn_id>:<sha1(NFKC(content))>`）を使う。保存した `content` は、ちょうど `[<role>] B` である。ほかのレコードは §3.8 のキーを使う。（自分の文が `[user` で始まり、`turn_id` を渡す MCP の保存は、ここではフックのレコードとして読まれる。削除はそのガードを外し、ガードは保存の TTL で消える。そのため、同じ文を 7 日後にまた保存できるようになるだけである。）チャンクの埋め込みは、ほかの保存された `content` と同じく、前置きを付けたチャンクの文から作る（`"passage: " + content`）。
- 閉じ括弧の後には空白を 1 つ置く。親ドキュメントは、この前置きを除けば「verbatim」である。
- `agent_name` = `"claude-code"`、`session_id` = フックの入力にある Claude Code の `session_id`（空ならサーバーの既定：サーバーのプロセスの `N3MC_SESSION_ID`、なければサーバーが起動のときに作る UUIDv4）、`turn_id` = そのやりとりの `turn_id`（§12）。
- **索引用の文に前置きは入れない**：各レコードの `content_fts` と `content_ngram` は、前置きを付ける前の `B`（チャンクならチャンク *i*）から作る（§3.7「索引用の文」）。
- **MCP の `save_memory` に `role` はない。** 前置きを付けずに保存する（§3.8）。
- **フックの保存：完全一致の重複は同じやりとりの中だけ。** フックの保存では、完全一致の重複の確認（§3.8）は、Free と同じく、同じ `turn_id` のレコードとだけ比べる。そのため**すべてのメッセージと応答が保存される**：2 つのやりとりで送った `ok` は、それぞれに 1 回ずつ、2 回保存される。1 つのやりとりの中では、確認は今も働く：Stop フックと §12 の transcript からの回収の両方が、同じ応答を同じ `turn_id` で保存したとき、2 回目の保存は `duplicate` と答える。**MCP の `save_memory` は Lite の決まりを保つ**（§3.8：すべてのレコードにわたる完全一致の重複）。`role` がなく、前置きを付けずに保存するからである。
- **`skip_code_blocks: true` はフックの保存にも当てはまる**：フェンスを含むメッセージや応答は `skipped_code` と答え、保存しない。既定は `false` で、`[code omitted]` にして保存する。

**`/buffer` の応答**：§4.3 の `save_memory` の結果の dict を JSON にしたもの（`saved`、`duplicate`、`skipped_code`、または `reason` 付きの `error`）。HTTP 200。Redis に届かないときは、HTTP 503 で `{"status": "error", "saved": false, "reason": "<backend_hint>"}`。

**直接保存（サーバーなし）**：サーバーが使えないとき（応答がない、まだ起動中、起動に失敗した、ポートにほかのフォルダのサーバーがいる、タイムアウト、503 以外の HTTP エラー）、フック — と MCP の `save_memory`（§4.6）— は、自分のプロセスから Redis に直接保存する：`Database(cfg).save_memory(..., role=…, embed=False)`。これは同じ関数を `embed=False` で呼ぶもので、`sentence_transformers` を import せず、埋め込みを計算しない。そのため、どのレコード（独立したレコードもチャンクも）も **`embedding` フィールドなしで**書かれる。モデルが使えない間の保存（§3.9 手順 4）とまったく同じである。こうしたレコードは BM25 ですぐに見つかり、Lite の修復（`repair_memory` / `/repair`、§3.10）でベクトルを得る。フックはメッセージのたびに修復を呼ぶ（§12）。直接保存の前に、プロセスは §3.9 のとおり接続して `ensure_index()` を実行する（`enforce_ephemeral()` は実行しない。サーバーが接続したときに行う）。Redis にも届かないときは、何も保存しない：`audit.log` がすでに文を持っており（§12）、通知 `redis_unreachable` を出す。**送り直すためのキューは作らない。**

### 3.17 コンテキストの大きさの上限

Lite 自身の決まりである（N3MC Free にはこの上限はない）。N3MC Pro の文脈の大きさの上限と同じ形で、Lite の結果の形に合わせてある（Lite には Q-A のやりとりの塊がない、§3.14）。

**理由**：Claude Code は、フックの普通の stdout を 10,000 文字までに制限する。それより長い出力はファイルに移され、Claude には 2,000 文字のプレビューしか見えない（N3MC Pro での実測：結果が 20 件のとき、138 問の 45 % で塊が 10,000 文字を超えた）。Lite のヒットは親ドキュメント全体のこともあるので、1 つのヒットだけで上限を超えうる。

- **当てはめる場所**：フックの `# Recalled Memory Context` の塊（`--hook-submit`）と、MCP の `search_memory` の結果（これもモデルのコンテキストに入り、ツールの結果の上限はクライアントごとに違う）。どちらも `max_chars = context_max_chars` を使う（§6、既定 9,500、許す範囲 2,000〜10,000）。手で行う `--search` は制限**しない**。劣化時の代わりの出力（§12）も制限しない。
- **表示だけ**：検索そのものは、表示の前に、すべての結果に対して行う。上限は出力から文を取り除くだけである。
- **大きさ**（`context_size(text)`）：文の UTF-16 のコード単位の数（BMP の外の文字は 2 と数える）に、改行の数を足したもの（改行はそれぞれ 2 と数える：Windows のパイプは CR LF として渡しうる）。`context_size(output) + 2 <= max_chars` のとき、出力は収まる。
- 出力の**部分**は、順に次のとおり：**頭**（フック：`# Recalled Memory Context`、`Source: N3MC Lite (7-day working memory)`、`Search query: <query>` と空行。MCP：なし）、**通知**、**版についての一文**、**普通の行**（`\n\n---\n\n` でつなぐ）、**弱い部分**（見出しと弱い行）、**尾**（MCP：`_NUDGE_AFTER_SEARCH`。フック：なし）。頭と尾以外の部分は、§3.14 / §5 (4) とまったく同じに `\n\n` でつなぐ。
- **縮め方**：先にすべての出力を作る。収まれば、そのまま使う。収まらなければ、次を順に行い、収まった時点でやめる：
  1. 普通の行を、いちばん順位の低いものから 1 つずつ取り除く。ただし 3 つより多く残っている間だけ。最後に残した普通の行の後に、部分 `_(N lower-ranked match(es) omitted to keep this block within the context limit.)_`（N は取り除いた行の数）を、`\n\n` でつなげて足す（`---` ではつながない）。
  2. 文を切る：尾を除いた出力をとり、末尾の空白を除いたその先頭部分のうち、先頭部分 + `\n\n` + `_(cut here to keep this block within the context limit.)_` + 尾 が収まる最も長いものを残す。残した行の中の長い親ドキュメントも、こうして縮める。
- 手順 1 は、通知と弱い行を決して取り除かない（弱い行は、`min_results` より少ない行しか下限を通らなかったときだけあるので、そのとき出力は短い）。頭と尾は決して取り除かない。
- ルールファイル（§12）は、同じキーワードで手で `--search` すれば省かれたものが見えることを Claude に伝える。MCP の指示は、このためには変えない。

---

## 4. MCP プロトコル表面

### 4.1 通信

stdio。サーバーは `stdin` から JSON-RPC 行を読み、`stdout` に応答を書く。ログは `stderr`。Windows では起動時に `stdin`/`stdout`/`stderr` を UTF-8 に再設定する（エンコーディング安全策の全契約 — 各ツール入力に対する孤立サロゲート除去を含む — は [§3.13](#313-エンコーディング安全策) を参照）。

§4.1 と §4.2 の「サーバー」は、MCP の薄いクライアント `n3mc-workingmemory`（§4.6）である。`initialize` と `tools/list` には自分で応答し、すべての `tools/call` を常駐サーバー（§3.15、§4.5）に転送する。§4.3 のツールのハンドラは常駐サーバーが動かす。新しいネットワークのポートは開かない：MCP の側は stdio だけで、常駐サーバーは `127.0.0.1` だけで待ち受ける。

### 4.2 `initialize` 応答

サーバーが広告する内容：
- `protocolVersion` — **MCP SDK が交渉して決める**：クライアントが `initialize` で送った版を、SDK が対応していればそのまま返し（`mcp` 1.27 では `2024-11-05`・`2025-03-26`・`2025-06-18`・`2025-11-25`）、対応していなければ SDK の最新版を返す。コードで固定しない。（`2025-06-18` を求めるクライアントには `2025-06-18` が返る。）
- `serverInfo: { name: "n3mc-workingmemory", version: <package __version__> }`
- `capabilities.tools` with `listChanged: false`
- `instructions:` — 振る舞い指示の複数行文字列（[§5](#5-振る舞い指示自動保存戦略) 参照）。**Lite 用文面には「メモリは 7 日で失効する」旨を明示する。**（ルール 1 と 2 は、フックの `# Recalled Memory Context` の塊を持つクライアントがすることも述べる、§5 (1)。）
- 薄いクライアントは `initialize` に 2 秒以内に応答する：モデルを読み込まず、常駐サーバーも待たない（§4.6）。

> **⚠️ 実装注意（version / instructions の設定漏れに注意）**：`serverInfo.version` と `instructions` は **`Server()` コンストラクタ引数で渡せる**（`Server(name, version=__version__, instructions=INSTRUCTIONS)`）。これを忘れると version が空・instructions が未広告になりやすい。コンストラクタで渡せば `create_initialization_options()` が自動で拾う。MCP SDK のバージョンによっては `init_options.instructions` への後付け代入も併用すると確実。

### 4.3 ツール

`tools/list` で公開する 7 ツール（6 基本ツール + `recall_thread`（§4.3.1））。N3MC MCP Pro 1.0.0 のツールは 5 つで、一部が同じ名前である（`search_memory`、`save_memory`、`list_memories`、`delete_memory`、そして `recall_thread` の代わりに `show_turn`）。`delete_memories_by_session` と `repair_memory` は Lite 専用 — Pro 版は永続化を重視するため誤削除リスクを避け個別 `delete_memory` のみを公開する：

| 名前            | 入力                                      | 振る舞い                                                                 |
| --------------- | ----------------------------------------- | ------------------------------------------------------------------------ |
| `search_memory` | `query: string, limit?: int, session_id?: string, since?: string, until?: string` (§4.3.1) | ハイブリッド（ベクトル + BM25）検索、時間減衰＋保存した importance ＋ `b_session` ランキング、語彙リランク。チャンクヒットは親ドキュメントに折りたたまれ全文 verbatim で返る（[§3.11](#311-全文再現親ドキュメントチャンクパターン)）。結果は [§3.14](#314-検索結果の整形) の通りに整形・表示される（ベクトル下限、新しい版を先頭へ、相対カット、`saved=` 時刻）。**下限を通った行が `min_results`（既定 3）件に満たないときは、落ちた行のうち近いものを `#### Possibly related (weak matches — may be unrelated)` の下に足す。ツールの説明では LLM に「弱い候補は関連度の下限を通っておらず、関係がないことが多い。質問に実際に関係があるときだけ使い、関係がなければ無視して触れない」と伝える。**`session_id` 引数は **Pro と同じ b_session ランキング**（match=1.0 / mismatch=0.6）に直接作用し、同一 `session_id` で保存されたメモリを上位に押し上げる。省略時はサーバー既定（`N3MC_SESSION_ID` 環境変数 → プロセス起動時 UUIDv4）が effective_session として使われる。markdown を返す。 |
| `save_memory`   | `content: string, agent_name?: string, owner_id?: string, importance?: number, session_id?: string, turn_id?: string` (§4.3.1) | 本文長 ≤ `chunk_threshold` なら完全一致の重複判定のみ（§3.8）を行った後、HSET + EXPIRE し `{"status": "saved", "saved": true, "id": "...", "ttl_seconds": ...}` を返す。超過なら**親ドキュメント**を `doc:<id>` に verbatim 保存し、スライディングウィンドウでチャンク化した `mem:<id>` を並列登録、`{"status": "saved", "saved": true, "parent_id": "...", "chunks": N, "saved_count": N, "ids": [...], "ttl_seconds": ...}` を返す。`owner_id` を指定した場合、サーバー設定と不一致なら `{"status":"error","saved":false,"reason":"owner_id mismatch"}` を返す。`importance` は 0.5〜2.0 の範囲でクランプされ、保存時スコア重みに反映される。数値でない値（`NaN` を含む。例：文字列 `"nan"`）は `{"saved": false, "status": "error", "reason": "importance must be numeric (got <value>)"}` を返し、何も保存しない（そうしないと `NaN` がクランプを素通りし、最大の重要度 `2.0` として保存される）。`session_id` 省略時はサーバー既定（`N3MC_SESSION_ID` 環境変数、なければプロセス起動時の UUIDv4）が write-time tag として使われる（`delete_memories_by_session` のフィルタキー、および後続 `search_memory` の `b_session` マッチ対象）。 |
| `list_memories` | `limit?: int (既定 20)`                   | 親ドキュメントと独立メモリを新しい順で並べた markdown。`limit` がない、空、または 0 のときは 20、それ以外の 1 未満の値は 1 として扱う（負の値で一覧の末尾から行を削ってはならない。§5 (4)）。親は `[doc×N]` タグ付き。チャンクは隠蔽（FT.SEARCH `*` クエリ後に Python 側で `parent_id` 空文字列フィルタ、§3.12 参照）。**索引をページ単位で読む**：新しい順（`timestamp_epoch` の降順）に 200 件ずつ（`LIMIT <offset> 200`）取り出し、このオーナーの独立メモリが `limit` 件集まるか索引が尽きるまでページを進める。（`max(limit × 5, 200)` 行を 1 回だけ読むのでは足りない。260 チャンクの文書のあとではその塊がチャンクだけになり、その直前に保存した独立メモリが一覧に出ない。） |
| `delete_memory` | `id: string`                              | 設定の `owner_id` のレコードに限る（別のオーナーの ID → `{"status":"not_found","id":...}`）。**ID は前置きなし（`<uuid>`、`search_memory` / `list_memories` の表示どおり）でも、先頭に `doc:` か `mem:` を 1 つ付けてもよい。そうした前置きを 1 つだけ先に外し（§3.12 の「前置きのない UUID にそろえる」）、応答の `id` は前置きのない UUID にする。** ID が親（`doc:<uuid>` が存在する）なら親＋`docsha:`＋その `chunk_ids` に列挙された全チャンクを連鎖削除（§3.11）。それ以外は `mem:<uuid>` を消し、独立したレコード（`parent_id` なし）ならガードの値がこの ID のときだけ `mem:sha:<sha1>` も消す。アトミックに行う（§3.5「削除のセマンティクス」）。成功の応答は、親なら `{"status": "deleted", "id": "<uuid>", "chunks_deleted": N}`、それ以外は `{"status": "deleted", "id": "<uuid>"}` である（1.x と同じ）。 |
| `delete_memories_by_session` | `session_id: string`         | 指定 `session_id` に紐づく独立メモリ・親ドキュメント・子チャンク・sha ガードを、設定 `owner_id` のレコードに限定して一括削除。応答は `{"status":"deleted", "session_id": ..., "documents_deleted": D, "chunks_deleted": C, "singles_deleted": S, "deleted": D+C+S}`。ヒットゼロのときは `{"status":"not_found", "session_id": ..., "deleted": 0}`（再呼び出しは安全な no-op）。**不可逆操作のため呼び出し前に `session_id` をユーザーに確認すること。** Lite 専用（[§10 Test 6](#10-自律評価evidence-report) 参照）。 |
| `repair_memory` | —                                         | `ensure_index()` と、欠けているベクトルの付け足し（1 回の呼び出しで最大 200 件）。[§3.10](#310-修復) 参照。 |

**ツールが動く場所**：下のハンドラは常駐サーバーの中で動く（`tools.py`。`POST /tools/<name>` から呼ばれる、§4.5）。薄いクライアントは、その文をそのまま返す（§4.6）。引数・振る舞い・出力の文は 1.x の版（MCP サーバーだけだった版）と同じである。1 つだけ足した：`search_memory` の文は `context_max_chars` の中に収める（§3.17）。§13 の CLI は同じハンドラを呼び、リマインダを除いて同じ文を出力する。

全ツールの応答は単一の `TextContent`。`save_memory` / `delete_memory` / `delete_memories_by_session` / `repair_memory` は JSON 文字列、`search_memory` / `list_memories` は人間可読 markdown。**いずれの応答も末尾に短い auto-save リマインダ（[§11](#11-保存の確実性と-mcp-プロトコルの限界) で利用するナッジチャネル）を `\n\n---\n` 区切りで付加する。** **エラーの応答も含む**：§4.4 の `Error: …` の文（読めない `since` / `until`、予期しない例外、`search_memory` / `list_memories` / `recall_thread` のバックエンドエラー）と、`"status": "error"` の JSON（例：数値でない `importance`）。エラーの文が先頭のままなので、§4.4 の `Error: ` の前置きは変わらない。機械的に応答 JSON をパースする呼び出し側は、先頭から JSON ドキュメント 1 つを取り出すストリーミングデコード（例：`json.JSONDecoder().raw_decode()`）を使うこと。

### 4.3.1 取り出し拡張ツール（Retrieval Extensions）

> 既存ツールの挙動は不変。以下は**追加ツール・追加引数**である。Pro 版では更にエンティティ索引・遅延要約のツールが追加される（本 Lite 版には含めない）。

#### `search_memory` の追加引数

| 引数 | 型 | 振る舞い |
| --- | --- | --- |
| `since`  | `string` (ISO 8601 日付 or 日時) | このタイムスタンプ以降に保存されたエントリのみに絞る。日付のみ指定時は `00:00:00` として解釈。 |
| `until`  | `string` (ISO 8601 日付 or 日時) | このタイムスタンプ以前に保存されたエントリのみに絞る。日付のみ指定時は `23:59:59.999999` として解釈。 |

**タイムゾーン**：日付だけの値と、オフセットのない日時は、**サーバーのローカルタイムゾーン**で読む — 出力の `saved=`（[§3.14](#314-検索結果の整形)）と同じ時計 — そしてエポック秒に変換する。オフセット付きの値（`2026-11-13T09:00:00+09:00`、`…Z`）はそのまま使う。日付と時刻の区切りは `T` でも空白でもよい。

**読めない値**：`since` または `until` が指定されていて解析できない（`yesterday`、`2026/11/13`、`2026-13-01`）とき、またはこのプラットフォームでエポック秒に変換できない（Windows のローカル時刻での `9999-12-31`、`1970-01-01`、`1969-12-31`：変換が `OSError` か `OverflowError` を出す。これも読めない値とみなす）ときは、検索を実行せず、`TextContent` の `Error: since must be an ISO 8601 date or datetime, e.g. 2026-11-13 or 2026-11-13T09:00:00+09:00 (got '<value>')` を返す（`until` も同様）。`since` が `until` より後なら `Error: since is later than until`。どちらの文の後にも §4.3 のリマインダが付く。空文字列は「範囲なし」を意味する。

> **⚠️ 理由**：解析できない値を「フィルタなし」として扱うと、`since="yesterday"` はストア全体から結果を返し、LLM は絞り込まれたものとして答えてしまう。また、オフセットのない値を UTC として読むと、`saved=` はローカル時刻を表示するので、UTC+9 のホストでは `since="2026-11-13"` が、`saved=` でその日の 00:00〜08:59 に保存されたと表示されるものを落とす。Free も同じ決まりである。

`since` / `until` は片方のみ指定可。ランキング式には影響しない。両方のクエリの内部で**事前フィルタ**として適用する — KNN：`@timestamp_epoch:[<since> <until>]=>[KNN N @embedding $vec AS __dist]`、BM25：`(<text query>) @timestamp_epoch:[<since> <until>]`（片側が開いている場合は `-inf` / `+inf`）。NUMERIC フィルタは §3.12 の UUID TAG 問題に該当しない。

> **⚠️ 事前フィルタにする理由**：KNN はまず全体で最も近い N 件を返す。ランキング*後*に時間で絞ると、窓外の行のほうがクエリに近いものが多いとき、窓内の行が候補集合に入らず、`search_memory(since=...)` が何も返さない。

#### 新ツール `recall_thread`

| 名前 | 入力 | 振る舞い |
| --- | --- | --- |
| `recall_thread` | `turn_id: string, session_id?: string, before?: int (既定 2), after?: int (既定 2)` | 指定 `turn_id` のエントリと、**同じセッションの**それ以前 `before` 件・それ以後 `after` 件を時系列で並べた markdown を返す。並び替えは保存時刻の昇順。**出すのは、`session_id` がそのターンのセッションと同じエントリだけ**：1 つの Redis には、いくつものセッション（2 つの Claude Code の窓、デスクトップアプリ）が同時に保存し、エントリは時刻の上で入り混じる。別のセッションの会話をスレッドに出してはならない。**ターンのセッション**：この `turn_id` を持つエントリの `session_id`（`session_id` を渡したときは、そのセッションのエントリだけを数える）。それが 2 つ以上のセッションにまたがり、`session_id` が渡されていないとき（MCP クライアントは `turn-1` のような `turn_id` を使い回すことがある）は、まとめずに JSON 文字列 `{"status": "ambiguous", "turn_id": ..., "session_ids": [<並べた一覧>], "reason": "this turn_id is in several sessions; call recall_thread again with one of session_ids as session_id"}` を返す。1 つのセッションだけの `turn_id`（フックは必ず新しい UUID を作る）は、`session_id` なしのときと同じに動く。Lite では Redis に保存された全エントリの中で「同一 turn_id を持つ親ドキュメント／単独メモリ／チャンク」をスキャンして返す。**親ドキュメントがまだあるチャンクは飛ばす**（全文は `[doc×N]` 付きの親が持つ）。**親を失ったチャンク（§3.11 の孤立チャンク）は、そのチャンクの本文で 1 件として出す** — `search_memory` と同じ退避（チャンクをすべて飛ばすと、親が失効した turn は、`search_memory` にはチャンクが出るのに `not_found` になる）。ヒットゼロの場合 `{"status":"not_found","turn_id":...}` を JSON 文字列で返す。 |

**運用**: 接続中の LLM は `search_memory` のスニペットで文脈不足と判断したとき、ヒットの `turn_id` を `recall_thread` に渡して周辺会話を取得すること。ユーザーがこのツールを直接指定する必要はない。

> **⚠️ 実装注意（`search_memory` 応答に `turn_id` を出力すること）**：上記運用が成立するには、`recall_thread` に渡す `turn_id` を LLM が `search_memory` の結果から読み取れる必要がある。したがって **`search_memory` の markdown 出力に各ヒットの `turn_id` を含める**こと（空のときは出さない）。実装上は `_vector_search` / `_bm25_search` の `RETURN_FIELDS` に `turn_id` を追加し、親解決時は `doc:<pid>` から `turn_id` を取得して行 dict に載せ、最終 markdown に 各ヒットの見出し行に `turn=<turn_id>` として出す（[§3.14](#314-検索結果の整形)）。これが無いと `recall_thread` は事実上呼べない（仕様の運用フローが閉じない）。

### 4.4 エラー処理

ツール例外はディスパッチ層で捕捉し、先頭 `"Error: "` を付けた `TextContent` で返す。ツールレベル例外で stdio ループがクラッシュすることはない。Redis の状態は呼び出しのたびに確かめ、切れた接続は自動でつなぎ直す。Redis に届かない間は、ディスパッチャはツールを実行せず、復旧の案内付きのバックエンドエラーを返す（[§3.9](#39-起動シーケンスと自己回復)「ツール呼び出しごとの Redis の確認」）。そのため Redis が止まっていても、MCP クライアントにはサーバーが**接続済み**と表示される。これは設計どおり：指示文とツールが届いているので、LLM がユーザーにすべきことを伝えられる（§5 ルール 4）。

常駐サーバーが使えない間も同じである：薄いクライアントは接続したままで、各ツールに §4.6「サーバーが使えないとき」の文で答える。`isError` は決して設定しない。エラーはすべて普通の結果の文である。

### 4.5 常駐サーバーのエンドポイント

すべて `http://127.0.0.1:<server_port>` にある。`Host` ヘッダがローカルでないか、要求に `Origin` ヘッダがあれば、どのエンドポイントも 403 を返す（§3.15）。`Host` の確認には、サーバーを起動したときの `server_port` を使い、あとで `config.json` が持つ値は使わない（§3.15「PID ファイルの扱い」）。`/health` と `/shutdown` 以外のエンドポイントは、すべて普通の `def` である。

| メソッド | パス | 使うもの | 本文 | 応答 |
|---|---|---|---|---|
| `GET` | `/health` | `ensure_server` | — | `{"status": "ok", "root", "code_version", "pid", "redis"}`（§3.15） |
| `POST` | `/shutdown` | `_stop_stale_server` | `{"root": str}` | `{"status": "ok"}`。`root` がサーバーのものでなければ 403（§3.15） |
| `POST` | `/buffer` | フック（`--hook-submit`、`--save-claude-turn`） | `{"content": str, "role": "user" \| "claude", "agent_name": str?, "session_id": str?, "turn_id": str?, "no_model_wait": bool?}` | `save_memory` の結果の dict（§3.16）、HTTP 200。`no_model_wait: true`（フックは必ず送る）：モデルを待たない（§3.9）。Redis に届かない → 503 で `{"status": "error", "saved": false, "reason": "<hint>"}` |
| `POST` | `/search` | `--hook-submit`、`--search` | `{"query": str, "session_id": str?, "since": str?, "until": str?, "limit": int?, "max_chars": int?, "no_model_wait": bool?}` | `{"text": "<# Recalled Memory Context block>"}`（§12「検索の出力」）。`no_model_wait: true`（`--hook-submit` だけが送り、`--search` は送らない）：モデルを待たない（§3.9）。サーバーはフックと `--search` をこの項目だけで見分け、`max_chars` では見分けない。`max_chars` がある → §3.17 の上限。ない → 上限なし。読めない `since` / `until`、または `since` が `until` より後 → 400 で `{"detail": "<the Error: text of §4.3.1>"}`。Redis に届かない → 503 で `{"detail": "<hint>"}` |
| `POST` | `/repair` | `--hook-submit` | `{}` | §3.10 の `repair_memory` の結果の dict（1 回の呼び出し：`ensure_index()` と最大 200 のベクトル）。修復は 1 つのサーバーのプロセスで同時に 1 つだけ：動いている間の別の呼び出しは、すぐに `{"status": "ok", "message": "repair already running", "reembedded": 0, "remaining": 0}` を返す。Redis に届かない → 503 で `{"status": "error", "message": "<hint>"}` |
| `POST` | `/tools/{name}` | MCP の薄いクライアント（`nudge=1`）、CLI（`nudge=0`） | ツールの引数を 1 つの JSON オブジェクトで（それ以外は `{}` として読む） | `{"text": "<the tool's result text>"}`、常に HTTP 200（エラーは §4.3 / §4.4 と同じく文の中にある）。ハンドラは §4.3 のハンドラで、呼び出しごとの `ensure_connected()` と §3.9 のバックエンドエラーを含む。`search_memory` には §3.17 の大きさの上限が加わる。クエリパラメータ `nudge`（既定 `1`）：`0` なら §5 (2) のリマインダを付けない |

- **ほかのエンドポイントはない。** Free の `/list` は要らない（一覧は `list_memories` ツール）。Free の Pro 専用の `/delete`、`/gc`、`/import` はない（削除は `delete_memory` / `delete_memories_by_session` ツール。レコードは Redis が自分で失効させる）。
- 知らない `{name}` には `{"text": "Error: unknown tool '<name>'" + _NUDGE_GENERIC}` を返す（`nudge=0` ならリマインダなし）。
- `nudge=0` がある理由：リマインダはモデルに `save_memory` を呼ぶよう伝える。Claude Code ではフックが保存するので、CLI はリマインダを除いてツールの文を出力する。
- `/search` と `/tools/search_memory` は、どちらも Lite の `search_memory`（§3.14）を動かす。`/search` は表示した文をフックの見出しで包む（§12）。`/tools/search_memory` はツール自身の文を返す（§3.14）。

### 4.6 MCP の薄いクライアント

仕組みは N3MC MCP Pro v1.0.0 §4.6 の MCP の入口のものである。ツール・出力・`isError` の振る舞いは Lite 自身のもの（§4.3、§4.4）である。

| 項目 | 値 |
|---|---|
| モジュール | `n3mc_mcp/server.py`（`run()`。`__main__.py` がこれを呼ぶ） |
| コマンド | `n3mc-workingmemory`、別名 `n3mc-mcp-lite` |
| 通信 | stdio だけ |
| ライブラリ | `mcp` 1.x（`>=1.0.0,<2.0.0`）。低レベルの `mcp.server.lowlevel.Server` と `mcp.server.stdio.stdio_server` |
| `serverInfo` | `name` = `n3mc-workingmemory`、`version` = `__version__` |
| 設定 | サーバーと同じ `config.json` とデータフォルダ（`load_config()`、`N3MC_DATA_DIR`。§6、§7）。`server_port` が要る。**ツールの呼び出しのたびに、最初に読み直す**。読み直した設定を `ensure_server` と HTTP の要求の両方に使う。エントリーはクライアントのセッションのあいだずっと動くので、起動時に 1 回だけ読むと、`server_port` を変えたあとも毎回古いポートに送ってしまう。そのときファイルを読めなければ、最後に読めた設定を使い続ける（その後一度も読めていなければ、起動時の設定）。読めないまま読み直すと、呼び出しのたびに新しい仮の `owner_id` になるため |

1. **stdout は MCP のもの。** `run()` は最初に stdin / stdout / stderr を UTF-8 に再設定する（§3.13 (1)）。stdout には MCP のメッセージ以外を書かない。警告、`_notify` の出力、ライブラリのメッセージは stderr に出す。
2. **モデルなし、Redis なし。** このモジュールは `sentence_transformers` も `torch` も import しない（直接にも、`processor.py` のモデルのコードを通しても）。下の直接保存を除き、Redis への接続も開かない。§3.9 の `import_backend()` とバックグラウンドのモデルのロードは、ここでは行わない。直接保存が読み込むコンパイル済みのモジュール（`psutil` と、入っていれば `redis` が読み込む `hiredis`）は、`stdio_server()` を始める前に import する。Windows では、stdio が stdin のパイプを読んでいる間にワーカースレッドでコンパイル済みのモジュールを import すると、入口が止まることがある（§3.9 と同じ危険）。`redis` と `database.py` は Python だけで書かれているので、始めではなく直接保存のときに import する：この 2 つの import には約 0.5 秒かかり、`initialize` は 2 秒以内に答えなければならない。そのため、薄いクライアントが始めに import するモジュール（ツール名と念押しの文のための `tools.py`、`cli.py`）は、モジュールの先頭で `redis` を import しない。`tools.py` は `ToolRunner.call` の中で import する。
3. **サーバーの起動。** 始まった直後に、`run()` は `ensure_server(cfg)` を**バックグラウンドのスレッドで** 1 回呼ぶ。そのため `initialize` にはすぐに応答する（サーバーの起動には最大 60 秒かかりうる、§3.15）。`_start_server` は子プロセスの stdout を `DEVNULL` に、stderr を `server.log` に送る。そのため子プロセスが MCP の流れに書き込むことはない。
4. **`initialize` / `tools/list`** には手元で応答する：§5 (1) の指示と、§5 (3) の 7 つのツールの定義（`tooldefs.py`。サーバーと共用）。これらはサーバーなしで動く。
5. **入力の確認を切る**：ハンドラは `server.call_tool(validate_input=False)` で登録する。入っている `mcp` がその引数を拒んだら（`TypeError`）、`server.call_tool()` で登録する。新しい `mcp` 1.x は、ハンドラが動く前に引数をスキーマで確かめる。そうすると Lite 自身の文（たとえば §4.3 の `importance must be numeric` の JSON）が SDK のものに置き換わってしまう。
6. **各 `tools/call`**：
   1. JSON オブジェクトでない引数は `{}` として読む。知らないツール名には、手元で `Error: unknown tool '<name>'` + `_NUDGE_GENERIC` と答える（リクエストは送らない）。
   2. `search_memory` と `save_memory`：`session_id` がないか空白のときは、入口のセッションを入れる — このプロセスの `N3MC_SESSION_ID`、なければこのプロセスが始まったときに作った UUIDv4 1 つ。**理由**：既定のセッションは MCP クライアントのプロセスごとである（各クライアントが自分のサーバーを動かしていた 1.x の版と同じ）。常駐サーバーはすべてのクライアントが共用し、それらより長く生きる。そこで薄いクライアントが自分の既定値を送り、その意味（`b_session`、`delete_memories_by_session`）を保つ。
   3. `save_memory`：`agent_name` がないか空白のときは、(1) 環境変数 `N3MC_AGENT_NAME`、(2) なければクライアントが `initialize` で送った `clientInfo.name`、(3) なければ空のままにする。どの値も `strip()` した後、空でないときだけ使う。
   4. `ensure_server(cfg)` をワーカースレッドで動かす（`asyncio.to_thread`）。そのため stdio のループは読み続ける。入口が動いている間に止まったサーバーは、ここでもう一度起動する。
   5. 引数を付けて `POST /tools/<name>?nudge=1` を送る。`TextContent(type="text", text=<the answer's "text">)` を**そのまま**返す。
   6. ハンドラの中のどんな例外も、`Error: <message>` + `_NUDGE_GENERIC`（§4.4 の決まり）と、stderr の 1 行になる。入口はツールの呼び出しのせいで終わることはない。
7. **終わり方。** アプリが stdin を閉じると、入口は終わる。`/shutdown` は決して送らない：サーバーは切り離して起動されており、フックも使っている。

#### サーバーが使えないとき

「使えない」とは、`ensure_server` が使えるサーバーなしで戻ったこと（60 秒たってもまだ起動中、起動に失敗した、または失敗から 5 分以内なので起動を飛ばした、ほかのフォルダのサーバーがポートを持っている、自分の古いサーバーを止められなかった）、または `/tools` のリクエストが失敗したこと（接続エラー、タイムアウト、2xx でない応答）である。`<reason>` は、`the memory server is still starting (the first start downloads the embedding model, ~1.1 GB)`、`the memory server could not start; see <server.log path>`、`another N3MC folder's server is using port <port>`、`an old N3MC Lite memory server could not be stopped`、またはリクエストのエラーのどれかである。

| ツール | 応答 |
|---|---|
| `save_memory` | §3.16 の**直接保存**（`role` なし、`embed=False`）。先に、§4.3 の引数の確認（`importance`、`owner_id`）を手元で行う。結果の JSON は、`save_memory` の結果に `"warning": "embedding_unavailable"` と `"detail": "The N3MC Lite memory server was not available (<reason>), so this entry was saved without a vector. It is found by keywords now and gets its vector from repair_memory once the server runs."` を足したもので、その後に `_NUDGE_AFTER_SAVE`。Redis にも届かないとき：§3.9 項目 2 のバックエンドエラーの JSON（`{"status": "error", "saved": false, "reason": "<backend_hint>"}`）+ `_NUDGE_GENERIC` |
| `search_memory`、`list_memories`、`recall_thread` | `Error: memory backend unreachable. The N3MC Lite memory server is not available (<reason>). It is started automatically; retry in a minute.` + `_NUDGE_GENERIC`（§3.9 項目 2 の前置きなので、指示のルール 4 が当てはまる） |
| `delete_memory`、`delete_memories_by_session` | `{"status": "error", "reason": "The N3MC Lite memory server is not available (<reason>). Nothing was deleted; retry in a minute."}` + `_NUDGE_GENERIC` — **要求を送らなかったときだけ**（`ensure_server` のあとに使えるサーバーがない、または接続を拒まれた）。要求を送ったのに応答が来なかったとき（タイムアウト、送ったあとの接続の切断、2xx 以外）は、`{"status": "unknown", "reason": "The request reached the N3MC Lite memory server but no answer came back (<reason>). The delete may have completed; check with list_memories or search_memory before deleting again."}` + `_NUDGE_GENERIC`（削除が済んでいるのに "Nothing was deleted" を返すのは誤りで、`delete_memories_by_session` をやり直すと、その間に同じセッションに足された記録まで消える） |
| `repair_memory` | `{"status": "error", "message": "The N3MC Lite memory server is not available (<reason>). Retry in a minute."}` + `_NUDGE_GENERIC` |

**直接保存する理由**：そうしないと、モデルが保存すると決めた事実が失われる。フックも同じことをする（§3.16）。Lite は、モデルが使えないときはいつもベクトルなしで保存する（§3.9）。タイムアウトの後に、同じ文のサーバーでの保存と競合した直接保存は、`SET NX` のガード（§3.8）が決着をつけ、レコードは 1 つになる。

#### ほかのアプリへの登録

§8「Claude Desktop」を参照。Claude Code では、プラグイン `n3mc-workingmemory` か `claude mcp add` で登録する（§8「Claude Code」）。

#### この設計の限界

- **フックのないアプリでは、保存はモデル次第である**（§11）。指示とリマインダが保存を頼む。会話を丸ごと残せるのは Claude Code だけである。
- **常駐サーバーが動けなければならない**（§2 の依存パッケージを持つ Python）。起動できないときは、検索は上のエラーを返し、保存はベクトルなしで入る。
- **すべてのクライアントに 1 つのインストール。** フック・CLI・薄いクライアントは、同じコードを動かさなければならない。別のバージョンの写しは別の `code_version` を持ち、呼び出しのたびに互いのサーバーを置き換え合う（§3.15）。デスクトップアプリは、フックが使うのと同じスクリプトのフォルダの絶対パスで登録する。プラグインの利用者は、同じ `uvx --from n3memorycore-mcp-lite==2.1.0 …` の形で登録する（§8）。プラグインの `uvx` のコマンドはパッケージのバージョンに固定されている（§2）。そのため、どちらもこのリリースを使う限り、プラグインとデスクトップアプリは同じバージョンを動かす。

---

## 5. 振る舞い指示（自動保存戦略）

MCP には Claude Code の `UserPromptSubmit` / `Stop` フック相当が無いため、自動保存の振る舞いは `initialize` 応答で **自然言語の指示** として返す。接続中の LLM がシステム指示として読む。

この指示は、MCP を使うアプリ（Claude デスクトップアプリなど）のためのものである。Claude Code は、フック、MCP、またはその両方を使う（§8）。フックを使うときは、ルールファイル `.claude/rules/n3mc-lite-behavior.md` から指針を得る（§12）。指示の文は 1 つで、すべてのクライアントに送る。クライアントがフックを持つかどうかは分からないので、ルール 1 と 2 は、コンテキストにすでに `Source: N3MC Lite (7-day working memory)` の行を持つ塊がある（Lite のフックが入れた。N3MC Free と Pro のフックの塊にはこの行がない）ときにすることを述べる：塊で足りないときだけ検索し、利用者が頼んだときだけ保存する。塊のないクライアントは MCP の振る舞い（毎ターン検索し、自動で保存する）に従う。これが効くのは、Claude Code で MCP の入口とフックの両方を使うときである（§8）。

指示は LLM に以下を要求する：

0. **Pro が同時接続中の場合の役割分担（共存ポリシー）** — 永続の N3MemoryCore の記憶 — `n3mc-pro`（N3MC MCP Pro）、`n3mc-pg`（その PostgreSQL 版）、`n3mc-longtermmemory`（以前の MCP Pro）— が同一セッションに MCP として登録されている場合、Lite はワーキングメモリ（セッション内進捗メモ・一時デバッグ状態・使い捨て中間結果）として、Pro は正規長期ストア（ユーザープロフィール・プロジェクト仕様・設計決定・世界観設定・API 契約・調査サマリ等、セッション跨ぎで残す価値のあるもの）として機能する。疑わしい場合は Pro 側に倒す。Pro 接続時は TTL 警告（ルール 7）は基本不要 — 長期コンテンツを Pro にルーティングするだけでよい。Pro が未接続の場合は本サーバーが唯一のメモリストアとなり全コンテンツを受け入れ、ルール 7 を有効化する。同一事実を両サーバーに二重保存しない。

1. **先に検索し、想起したら明示する** — （コンテキストにすでに `Source: N3MC Lite (7-day working memory)` の行を持つ塊があるときは、その塊で足りないときだけ `search_memory` を呼ぶ。そうでなければ次のとおり）各ユーザーターンの先頭で、意図を反映した簡潔なクエリで `search_memory` を呼ぶ。検索結果を実際に応答に活用した（以前の記憶を想起して答えた）場合は、返答冒頭にユーザーの言語で一言そえる。例:「前回の回答がメモリに保存されています。」「以前の会話から該当情報を取り出しました。」／英語なら "Pulling this from earlier memory in this session."。**関連メモリが無かった / 想起内容を使わなかった場合は告知しない。** 単に「検索した」ことを述べるのは禁止 — 「想起した」ことだけを述べる。
2. **毎ターン自動保存（許可を求めない）** — （コンテキストに `Source: N3MC Lite (7-day working memory)` の行を持つ塊があるときは、すべてのメッセージと応答をフックがすでに保存している。毎ターン `save_memory` を呼ばず、利用者が何かを覚えるよう頼んだときだけ呼ぶ。そうでなければ次のとおり）保存は静かに自動で行う。ユーザーが「保存して」「覚えておいて」と言う必要は一切なく、**既定で保存する**。保存してよいか確認する質問もしない。各意味のあるターン後に `save_memory` を呼び、(a) ユーザーの意図・質問の言い換え、(b) **自分が生成した実質的な出力** — 特にユーザーが後で参照しそうな創作・生成コンテンツ（世界観設定、キャラクター設定、設計スケッチ、コードアーキテクチャ、リサーチ要約、アウトライン等。1〜2 文を超えて作ったものは保存）、(c) 確立した事実・嗜好・未解決の問いを保存。1 事実 = 1 `save_memory` 呼び出し、各 50–200 字（長文は 3 番ルール）。完全一致の重複はサーバー側で自動却下されるため「多めに保存」が安全。近似重複は却下**しない**：更新した事実や作り直した文書は新しいエントリとして保存され、`search_memory` は最新の版を先頭に表示する。**Lite は 7 日で消えるローリングスクラッチパッド**である旨を LLM に明示。
3. **長文は全文 1 回で保存（verbatim 復元）** — ターンで発生した長文 — ユーザー貼り付け（仕様・記事・ログ。コードブロックは `[code omitted]` として保存される、§3.13）**または自分が生成した長文（創作設定・世界観・キャラシ・設計ドキュメント）** — でユーザーが後で原文を取り出しそうなものは、**全文を単一の `save_memory` 呼び出しに渡す**。サーバーが自動的に親ドキュメント＋チャンク化を行い、検索はチャンクで、復元は親本文で verbatim 返却する（[§3.11](#311-全文再現親ドキュメントチャンクパターン)）。目安：**~400 字超 → 全文 1 回保存**。それ未満の要約可能な内容は 2 番ルールで短文複数保存。長文 verbatim 候補を多数の短い要約に分割してはならない（復元忠実度が崩れる）。
4. **ツールエラー時は先にユーザーへ通知（長文生成を先走らない）** — `search_memory` / `save_memory` がサーバーエラーを返した場合（Redis 不達・接続拒否・タイムアウト・「start Redis Stack」ヒント等）、**長文生成に入る前に停止し、ユーザーの言語で端的に通知する**。理由：バックエンド不稼働時は後続の `save_memory` も全て暗黙失敗するため、長文設計ドキュメントや創作設定を作っても **セッション閉鎖時に全て失われる**（ユーザーは記憶層の故障に気付けない）。通知内容：(a) 失敗事実（例「Redis Stack が停止しているようです。メモリ保存・検索が使えません。」）、(b) ツールが返した復旧ヒント（例 `docker run -p 127.0.0.1:6379:6379 redis/redis-stack-server:latest`）、(c) メモリ無効のまま続行するか・復旧まで待つかをユーザーに確認。短い事実回答は続行してよいが、一度は保存失敗中である旨を必ず触れる。
5. **ノイズをスキップ** — 挨拶・確認質問・機械的な了解は保存しない。
6. **明示要求を尊重** — 「これは保存しないで」「忘れて」に従う（`delete_memory` を使用）。
7. **ユーザーが長期保存を期待したとき 7日 TTL を伝える** — 7日 TTL は LLM からは見える（この INSTRUCTIONS ブロック／`search_memory` のツール説明／`save_memory` 応答の `ttl_seconds` フィールド）が、**人間のユーザーには見えない**。既定は 2 番ルールの通り沈黙自動保存で、毎回 TTL に言及する必要はない。ただしユーザーの発言に「長期保存を期待するシグナル」が現れた場合 — 明示的な永続化語句（"remember this forever" / "don't forget" / "save permanently" /「ずっと覚えておいて」「永続的に保存して」「絶対忘れないで」「次回も覚えていて」）、ユーザーが明らかに時間投資した長文（世界観・キャラ設定・仕様書・コード・設計ドキュメント等）、~5 日以上前に保存したコンテンツへの言及（失効が迫っている）— に限り、ユーザーの言語で**一文だけ**「Lite 版のメモリは保存から7日で自動削除される旨／永続保存が必要なら **Pro 版（sqlite-vec バックエンド）** を、そうでなければ別途バックアップを推奨」を添える。保存は通常通り実行し、許可は求めない。通知は**長期シグナル 1 回につき 1 度だけ**（毎ターン・毎保存では出さない）。既に同一会話内で通知済みなら繰り返さない。根拠：この規則がないと、LLM はユーザーが「永続的に保存した」と思い込んでいる内容を沈黙のうちに揮発させ、ユーザーは想起失敗で初めて損失に気付く。

8. **`skip_code_blocks` サーバーポリシーを尊重** — `save_memory` が `{"status": "skipped_code", "saved": false}` を返した場合、サーバーはトリプルバッククォートフェンスを含むペイロードを拒否する設定になっている（[§6](#6-設定)参照）。これは FastAPI 時代の N3MemoryCore のコード除外挙動をオプトインで再現するもので、ユーザーが意図的にコードをメモリ外に置いている状態。既定の `false` では、本文はコードブロックを `[code omitted]` にして保存され、INSTRUCTIONS の文もそう伝える。**同一ペイロードの再送は禁止** — 代わりにそのコードが何をしているかの散文要約を保存するか、当該ターンの保存をスキップすること。ユーザーが「さっきの保存はどうなった？」と明示的に聞かない限り、スキップを自発的に告知する必要はない。

9. **`session_id` 単位での一括掃除** — `delete_memories_by_session` は完了したプロジェクトのメモリを TTL 前に除去し、検索結果のノイズを低減するために使う。操作は**不可逆**なので、呼び出す前に対象 `session_id` をユーザーに確認すること。

10. **スレッドコンテキスト取り出し — `recall_thread`（§4.3.1）** — `search_memory` のスニペットで文脈が不足していると判断した場合、ヒットの `turn_id` を `recall_thread` に渡して前後の会話スレッドを時系列で取得する。`save_memory` に `turn_id` 引数を渡して同一ターン内の保存をグルーピングしておくと後の取り出しが容易になる（推奨形式：短い UUID・タイムスタンプ・`"turn-N"` 等）。ユーザーがこのツールを直接指定する必要はない — LLM が想起品質を高めるために主体的に使うツールである。

11. **版と通知** — `search_memory` の結果に同じ内容の版が複数現れたら、`saved=` 時刻が最も新しいものが現行版である。それに基づいて答える（変更が重要なら変更に触れる）。結果が `> ⚠️ N3MC notice:` で始まる場合は、その内容をユーザーの言語で伝える（例：`config.json` が読めない）。

12. **弱い候補** — `#### Possibly related (weak matches — may be unrelated)` の下の記録は関連度の下限を通っておらず、検索が空にならないように足しただけのもの。質問に実際に関係があるときだけ使う。関係がなければ無視し、触れない（「関係のない記憶が見つかった」とも言わない）。

完全文面は [`n3mc_mcp/instructions.py`](./n3mc_mcp/instructions.py)。

### 文面と決まり（そのまま実装する）

以下はすべて仕様の一部である。**上の要約と下の文面が違うときは、下の文面を実装する**（例: 配る指示文の規則 8 は `session_id` の規則で、`skip_code_blocks` は規則 2 の中の注記である）。

**(1) `initialize` で返す指示文 — `n3mc_mcp/instructions.py` の `INSTRUCTIONS`。** この文そのままで、最後に改行を 1 つ付ける。言い換えない。順序を変えない。翻訳しない（英語のまま配る）。

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

**(2) すべてのツールの結果の末尾に付ける催促文 — `n3mc_mcp/tools.py`。**（`/tools/<name>` が `nudge=0` で呼ばれたとき以外、常駐サーバーが付ける、§4.5。） `search_memory` の結果には `_NUDGE_AFTER_SEARCH` を付ける。`save_memory` の結果には `_NUDGE_AFTER_SAVE` を付ける。それ以外のすべての結果 — エラーの結果と、知らないツール名への応答（`Error: unknown tool '<name>'`）を含む — には `_NUDGE_GENERIC` を付ける。

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

**(3) `tools/list` で返すツールの定義 — `n3mc_mcp/server.py` の `list_tools()`。**（`Tool` の一覧は `n3mc_mcp/tooldefs.py` にあり、薄いクライアントの `list_tools()` がそれを返す。） ツールは 7 つ。description、引数の説明、`minimum` / `maximum` / `default` の値、`required` の一覧は、接続したモデルが読むものである。下のとおりに書くこと。

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
                "Only entries of the session of that turn are returned. "
                "Returns {status:'not_found'} if turn_id is unknown, and "
                "{status:'ambiguous', session_ids} if the turn_id is in several "
                "sessions: call again with one of them as session_id."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "turn_id": {
                        "type": "string",
                        "description": "The turn_id from a search_memory result.",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Optional. The session of the turn, when the same turn_id is in several sessions.",
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

**(4) 一覧を返すツールの文の形。**

- `list_memories`: 何も保存されていなければ `_No memories stored._`。そうでなければ、項目ごとに 1 つの塊 `**<tag> [<id>]** <timestamp の先頭 19 文字>` + 改行 + purify した本文。`<tag> ` は、項目にタグがあるときだけ付く（親文書は `[doc×N]`）。塊は `\n\n---\n\n` でつなぐ。親文書の本文は、先頭 200 文字にする。200 文字より長かったときだけ、あとに `…` を付ける。単独のメモリ（`limit` 件まで）とすべての親文書を合わせ、timestamp の文字列の降順に並べ、`limit` 件に切る。`limit` がない、空、または 0 のときは 20。1 未満の値は 1。
- `recall_thread`: `turn_id` が空なら `{"status": "error", "reason": "turn_id required"}`。行がなければ `{"status": "not_found", "turn_id": "<turn_id>"}`。`session_id` なしで `turn_id` が 2 つ以上のセッションにあれば、§4.3.1 の `ambiguous` の JSON。そうでなければ、行ごとに 1 つの塊 `<印>**<tag> [<id>]** <timestamp の先頭 19 文字>` + 改行 + purify した本文。`<印>` は、求められたターンの行では `▶ `、ほかの行では空白 2 つである。塊は `\n\n---\n\n` でつなぐ。範囲は、そのターンの最初の行から `before` 行前まで、最後の行から `after` 行後までで、間にある**同じセッションの**ほかのターンの行も含む。ほかのセッションの行は数えず、出さない。ここでは、親文書は全文を出す（200 文字に切らない）。
- `search_memory`: ヒットどうしは `\n\n---\n\n` でつなぐ。知らせ、版の文、ヒット、弱い候補の見出し、弱い候補は、空行 1 つ（`\n\n`）でつなぐ。その後に §3.17 の大きさの上限を当てる。

**(5) バイグラムに展開する文字（§3.7）— `n3mc_mcp/processor.py`。** `_is_cjk(ch)` は、コードポイントが次の範囲のどれかに入るときに真である。§3.7 の「CJK の連続」は、この文字の連続のことである。1 文字だけの連続は、その 1 文字のまま残す。

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

それ以外の文字の連続では、結合文字を取り除いた形（`fold_diacritics`: NFD に分解し、Unicode のカテゴリ `Mn` の文字をすべて捨てる）も `content_ngram` に入れる。そのため `café` は `cafe` としても索引に入る。保存する本文は変えない。

**(6) 語彙リランク（§3.6）: 短い本文の減衰。** 行の加点は `(coverage × weight + phrase_bonus) × length_factor` である。`length_factor` は、正規化して小文字にした本文が 50 文字より短いときは `len(content) / 50`、そうでなければ 1.0 である。加点は `score` に足し、`filter_score` があればそれにも足す。この係数がないと、質問の語をたまたますべて含むごく短い断片が、本当の答えより上に出る。

**(7) チャンク分け（§3.11）: 区切り。** 各ウィンドウで、区切りは末尾の `overlap` 文字の中（`max(start + 1, end − overlap)` から `end` まで）だけで探す。区切りの文字を下の順に試し、最初に見つかった種類の、**最後の**出現の直後で切る。区切りで切ったあとは、次のウィンドウを切れ目から始める（重なりなし）。見つからなければ `end` で切り、次のウィンドウを `overlap` 文字前から始める（必ず 1 文字以上進む）。

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

## 6. 設定

初回起動時、データディレクトリ内に `config.json` が自動生成され、`owner_id` / `local_id` にランダムな UUIDv4 が割り当てられる。

完全スキーマ（欠損フィールドは以下の既定値で補完）：

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
  "vector_min_cos":           0.83,
  "relative_min_score_ratio": 0.6,
  "min_results":              3,
  "version_order_enabled":    true,
  "version_sim_threshold":    0.98,
  "version_min_chars":        100,
  "search_query_max_chars":   2000,
  "chunk_threshold":          400,
  "chunk_overlap":            100,
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

1.x の版の既存の `config.json` には最後の 3 つのキーがない。次の起動でこれらが既定値で補われる（下の「欠損フィールド」の決まり）。

- `redis_url` — 接続 URL。環境変数 `N3MC_REDIS_URL` がこのフィールドより優先。
- `ttl_seconds` — 新規メモリと sha ガードに適用する TTL（既定 7 日）。下げるのは問題ないが、1 週間を大きく超える値に上げると Lite の目的が崩れる。
- `search_result_limit` — `search_memory` が返す最大件数。
- `vector_min_cos` / `relative_min_score_ratio` / `version_order_enabled` / `version_sim_threshold` / `version_min_chars` — 検索結果の整形（[§3.14](#314-検索結果の整形)）。`vector_min_cos`（0.83）と `version_sim_threshold`（0.98）は `multilingual-e5-base` で計測した値。別の `embedding_model` では計測し直すこと。`vector_min_cos` の妥当な範囲は 0.80〜0.88（低くすると再現率が上がり関連の薄い行も増える、高くすると結果が短くなり関連行も落ちやすい）。変更は MCP サーバープロセスの再起動時に反映される（MCP クライアントを再起動する）。
- `min_results` — 下限を通った結果がこの件数に満たないとき、足りない分を**弱い候補**（下限で落ちた候補）で補う。別の見出しの下に出す（既定 `3`、[§3.14](#314-検索結果の整形) の手順 6）。`0` で補充しない。MCP サーバーのプロセスを起動し直すと効く。
- `dedup_threshold` と `context_char_limit`（v1.6.x）は**廃止** — 既存の `config.json` にあっても無視される（削除する必要はない）。
- `min_score` — `filter_score`（`time_decay` と `b_session` を掛けないスコア、[§3.6](#36-ランキング式)）がこの値未満の結果を除外（既定 `0.2`）。`0.0` で無効化。**フィルタは語彙リランク（[§3.6](#36-ランキング式)）の後に適用される** — フレーズ一致・coverage の加点を受けたスコアで判定するため、融合スコアが閾値直下でもリランク加点で救済されうる。
- `search_query_max_chars` — クエリから使う最大文字数（既定 `2000`；埋め込みモデルが ~512 トークンで飽和）。
- `chunk_threshold` / `chunk_overlap` — スライディングウィンドウのサイズとオーバーラップ（既定 400 / 100 文字）。本文長がこの閾値を超えた場合に親ドキュメント＋チャンク化（[§3.11](#311-全文再現親ドキュメントチャンクパターン)）に入る。
- `access_count_enabled`、`access_count_weight`、`access_count_max_boost`、`ttl_refresh_top_k`、`ttl_refresh_on_search` は設定の項目ではない：検索はレコードに何も書かない（[§3.6](#36-ランキング式)）。今ある `config.json` に残った値は、`config_type` の通知を出さずに無視する（消さなくてよい）。
- `lexical_rerank_enabled` / `rerank_weight` / `rerank_phrase_weight` — 融合スコア後の軽量語彙リランカー（[§3.6](#36-ランキング式)）。`false` で融合スコアをそのまま使う。
- `b_session_match` / `b_session_mismatch` — ランキング式の `b_session` 係数（[§3.6](#36-ランキング式)）。検索リクエストの `effective_session`（呼び出し時引数 → `N3MC_SESSION_ID` 環境変数 → プロセス起動時 UUID）と行の保存時 `session_id` を比較し、一致なら `b_session_match`（既定 `1.0`）、それ以外なら `b_session_mismatch`（既定 `0.6`）を最終スコアに乗算する。両方を `1.0` に設定すれば実質無効化（全行対称）。
- `skip_code_blocks` — `true` のとき `save_memory` はトリプルバッククォートフェンス（```` ``` ````）を含む本文を拒否し、`{"status": "skipped_code", "saved": false}` を返す。既定は `false`（FastAPI 時代の N3MemoryCore に倣い「コードをメモリに入れたくない」ユーザー向けのオプトイン）。ヒューリスティックはフェンス記号のみ — 散文とコード混在でも一括拒否である。`false` のときは、コードブロックを `[code omitted]` に置き換えて保存する（§3.13）。LLM は §5 の指示で、`skipped_code` を受けた同一ペイロードを再送せず、代わりにコードの要約散文を保存するよう誘導される。
- `server_port` — 常駐サーバーのポート（既定 `18524`、§3.15）。Free は 18520、Pro は 18521、PostgreSQL 版は 18522、Ollama 版は 18523 を使う。1 台の PC では、N3MC の製品ごとに自分のポートを持たせる（§14）。
- `bind_host` — 常駐サーバーが待ち受けるアドレス。既定の `127.0.0.1` のままにする（§3.15）。
- `context_max_chars` — フックの `# Recalled Memory Context` の塊と、MCP の `search_memory` の結果の最大の大きさ。§3.17 のとおりに数える（既定 `9500`）。Claude Code は 10,000 文字を超えるフックの出力をファイルに移し、2,000 文字のプレビューを残すので、許す範囲は 2000〜10000 である。手で行う `--search` は制限しない。
- **変更が効くとき**：`config.json` を直すと常駐サーバーの `code_version` が変わる。そのため次のフック・CLI・MCP の呼び出しがサーバーを止め、そのファイルを読む新しいサーバーを起動する（§3.15）。この節で「MCP サーバーのプロセスを再起動したとき（MCP クライアントを再起動する）」とあるところは、「直した後の次の呼び出しで」と読む。
- `embedding_model` / `embedding_dim` — 埋め込みモデル名（既定 `intfloat/multilingual-e5-base`）と、そのモデルの出力ベクトル次元（既定 `768`）。[§3.2](#32-埋め込み) 参照。運用者が品質・リソース・言語のトレードオフを選べるよう設定可能化した項目で、製品自体は言語中立を保つ。`embedding_dim` は **`embedding_model` の実際の出力次元と一致させること** — RediSearch のベクトルインデックス（`DIM`）はこの値で構築されるため、不一致だと保存・検索が失敗する。両者の変更は**プロセス再起動 + インデックスフラッシュ**を要する（Lite は 7 日 TTL なので、既存データは放置しても自然消滅する）。既定のままなら従来挙動と完全に同一。

**読めない・型を誤った `config.json`** — ユーザーはこのファイルを手で編集する（例：`vector_min_cos` の調整）ため、書き間違いは起こる前提とする。**上書きしない。**

| ケース | 挙動 |
|---|---|
| ファイルが無い | 新しい `owner_id` / `local_id` 付きの既定値で**排他的に**作成する（下記）。別のプロセスが先に作っていたら、そのファイルを使う |
| JSON として不正、またはトップレベルがオブジェクトでない | **ファイルを変更しない。** 既定値で動き、このプロセスに限り新しい UUID を使う（保存しない）。通知 `config_unreadable` を出す（文面: `config_unreadable: <path> could not be read (<理由>) …`） |
| 値の型が誤っている（例：`"vector_min_cos": "0.80"`、引用符付きの数値）、または許される範囲（下の表）の外 | **その項目だけ**無視して（既定値を使い）、他の項目は使う。ファイルは変更しない。項目名を挙げた通知 `config_type` を出す |
| 読める | 欠損フィールドを既定値で埋めて保存（欠けた `owner_id` / `local_id` は下の「`config.json` の書き込み」のロックの中で補う） |

- 型チェック（`config_value_ok(key, value, chunk_threshold=None)`。`chunk_threshold` は有効な値で、`chunk_overlap` に必要）：既定値が bool なら bool のみ、文字列なら文字列のみ受け付ける。数値は下の表に従う（bool は数値として扱わない）。既定値の無いフィールド（`owner_id`、`local_id`）と未知のフィールドはチェックしない。

**許される値**：

| キー | 型 | 許される範囲 |
|---|---|---|
| `ttl_seconds` | 整数 | ≥ 1 |
| `search_result_limit`, `search_query_max_chars`, `embedding_dim` | 整数 | ≥ 1 |
| `chunk_threshold` | 整数 | ≥ 50 |
| `chunk_overlap` | 整数 | ≥ 0 かつ < `chunk_threshold`（`chunk_threshold` を確認したあとの有効な値）。設定値 — キーがないか拒否されたときは既定の 100 — が `chunk_threshold` 未満でなければ、`chunk_threshold // 4` を使う（`chunk_threshold` だけを 100 以下に下げても保存が壊れてはならない）。確かめて知らせるのは、利用者が書いたキーだけである。`chunk_overlap` がないときは、有効な値（この場合は `chunk_threshold // 4`）でファイルを補う。そうしないと、次の起動で、利用者が書いていない `chunk_overlap` を知らせてしまう |
| `version_min_chars`, `min_results` | 整数 | ≥ 0 |
| `half_life_days` | 数値 | > 0 |
| `bm25_min_threshold`, `rerank_weight`, `rerank_phrase_weight` | 数値 | ≥ 0 |
| `min_score`, `vector_min_cos`, `relative_min_score_ratio`, `version_sim_threshold`, `b_session_match`, `b_session_mismatch` | 数値 | 0〜1 |
| `server_port` | 整数 | 1〜65535 |
| `context_max_chars` | 整数 | 2000〜10000 |
| `bind_host` | 文字列 | 何でもよい（`127.0.0.1` 以外の値は警告を出す、§3.15） |

- 「整数」は JSON の整数を意味する：`604800.5` も `768.0` も拒否する。受け付けると、`ttl_seconds: 604800.5`（または `0`、負の値）ではすべての `save_memory` が Redis の `DataError` で失敗し、`embedding_dim: 768.0` では起動時に `FT.CREATE` が失敗し、`chunk_overlap >= chunk_threshold`（例：`chunk_threshold` だけを 100 に下げた）では区切りのない本文で `chunk_text` が無限ループする — どれも通知なしで。
- 範囲外の値は型の誤りと同じに扱う：そのキーだけ既定値を使い、`config_type` がそのキーを挙げる。

**`config.json` の書き込み**：

- 無いファイルを**作成する**とき：内容を同じフォルダの一時ファイルに書き、ターゲットを排他的に作成する — `os.link(tmp, path)`（ターゲットがあれば失敗する）、ハードリンクが使えない場合は `open(path, "x")` — そして一時ファイルを削除する。ターゲットがすでにあれば、別のプロセスが勝ったので作成をやめ、そのファイルを読んでその `owner_id` / `local_id` を使う。まだ解析できなければ（この規則に従わない書き手）、0.1 秒待って読み直し、最大 2 秒まで繰り返す。
- **更新する**とき（欠損フィールドの補完）と旧来の場所からの引き継ぎ（§7）：同じフォルダに一時ファイルを書き、`os.replace` でターゲットに置き換える。書き込みが途中で止まっても、途中で切れたファイルは残らない。
- **既存のファイルに `owner_id` / `local_id` を補うとき**：同じフォルダのロックファイル `config.json.lock` を取り（`open(..., "x")`。0.05 秒ごとに最大 10 秒やり直す。10 秒より古いロックファイルは残骸とみなして消す）、**ロックの中で `config.json` を読み直し**、まだ欠けているときだけ ID を作り、`os.replace` で書き、ロックを消し、ロックの中で読んだファイルの ID を使う。書き込みそのものが失敗したとき（今の `could not update` の経路）を除き、ファイルにない ID でプロセスが動くことはない。10 秒以内にロックを取れないとき（または作れないとき）は、ロックなしで補う。**Windows では**、`config.json` の読み取りと `os.replace` を、`PermissionError` のとき 0.05 秒ごとに最大 1 秒やり直す（ほかのプロセスやウイルス対策が一瞬ファイルを開いていることがある。やり直しがないと、同時の起動で、ファイルにない ID のまま終わることがある）。（ロックがないと、ID のない同じファイルを読んだ 2 つのプロセスが、それぞれ自分の `owner_id` を書く。負けた側はファイルにない ID で動き続け、保存した記録は次の起動から `owner_id` の絞り込みで見えなくなる。下の理由と同じことが既存のファイルで起きる。）

> **⚠️ 理由**：起動するプロセスがそれぞれ自分の `owner_id` を生成してファイルに書くと、8 プロセスを同時に起動したとき 8 つすべてが別々の `owner_id` になり（試験では 5 回中 5 回）、7 つはファイルにない ID のまま動き続ける。それらの保存は `saved: true` を返すが、検索と一覧は `owner_id` で絞るので、それらの記憶は次の起動から見えない。Claude デスクトップアプリは、2 つの登録を同時に起動することがある。

- **`config.json` が読めない間**（この実行はメモリ上の新しい ID を使う、上の表）は、すべての `save_memory` の応答に `"warning": "config_unreadable", "detail": "config.json could not be read, so this entry was saved under a temporary owner_id and will not be visible after the server restarts. Fix <path> and restart the MCP client."` を付ける。この警告は何かを保存した応答に付ける。そのエントリがベクトルなしで保存された場合は 2 つをまとめる：`"warning": "embedding_unavailable; config_unreadable"` とし、2 つの `detail` の文を空白でつなぐ。
- **通知**：`load_config()` はこれらのメッセージを `cfg["_notices"]`（文字列のリスト）に集め、それぞれを `stderr` に出力する。各メッセージは先頭にキーを付ける（例 `config_type: …`）。接続の層は `shared_redis` / `config_refused`（§3.4）と `index_dim_mismatch`（§3.9）を同じリストに加える。サーバーは各通知を、その通知が加わったあとで**最初に成功した** `search_memory` 呼び出しの出力の先頭に**1 回だけ**（エラー応答は `Error:` で始める必要があるので、その場合は次に成功した検索まで持ち越す） `> ⚠️ N3MC notice: <message>` 行として付け、接続中の LLM がユーザーに伝えられるようにする（§5 ルール 11）。したがって、プロセスの途中で加わった通知 — たとえばつなぎ直し後の `shared_redis` — は、次に成功した検索で表示される。原因を直してクライアントを再起動すれば消える。「プロセス」は常駐サーバーである。「最初に成功した `search_memory`」は、どちらの種類でも最初に成功した検索である（MCP の `search_memory`、またはフックか CLI の `/search`、§3.15「通知を表示する場所」）。`config.json` を直すと、サーバーは自分で再起動する（`code_version`）。`config.json` を読むプロセス（CLI、フック、薄いクライアント）は、どれも、これらのメッセージを自分の stderr に出す。

> **1 PC 内の複数アカウント**：OS ユーザーごとに各自の `config.json` で動くので、`owner_id` もそれぞれ違う。Lite は、持ち主の違う記憶を共有しない：検索と一覧は、設定の `owner_id` の記録だけを出す（§3.12）。2 つのアカウントを 1 つの Redis に向けないこと — 重複のガードは `owner_id` を含まないので、一方が保存した本文は、もう一方では `duplicate` と答えられる（§3.8）。また、もう一方の記録が検索の候補を埋めることがある（§3.12）。アカウントごとに別の Redis を使う（別のポートの別のコンテナを、それぞれの `redis_url` に設定する）。記憶を共有できるのは、同じ `config.json`（同じ `owner_id`）を使うサーバーどうしだけである。たとえば、1 つのアカウントの Claude デスクトップアプリとターミナルの `claude`。

---

## 7. データ保存先

既定では、ディスク上に置くのは `config.json` だけで、場所は **`~/.n3mc-workingmemory/`**（ユーザーのホームフォルダ。Windows では `%USERPROFILE%\.n3mc-workingmemory\`）。環境変数 `N3MC_DATA_DIR`（絶対パス）で上書きできる。

データフォルダ内のファイル：
- `config.json` — 設定
- `n3mc-lite.pid` — 常駐サーバーの PID ファイル（サーバーが動いている間だけある、§3.15）
- `server.log` — 常駐サーバーの stderr（起動時に 1 MB を超えていれば空にする、§3.15）
- `server_start_failed` — サーバーの起動に失敗した目印（5 分の待ち、§3.15）
- `notices.json` — 人の対応が要る通知（§3.15）
- `audit.log`、`audit.log.1` — フックの呼び出しをすべて記録する JSONL の監査記録。ほかの何よりも先に書く。プロンプトと応答の原文を、**コードブロックや貼り付けた文も含めて、TTL なしで**持つ（§12）。個人のデータとして扱う。`--stop` が 50 MB でローテーションするので、残るのは最大約 100 MB である。
- `turn_id_<session>.txt`（セッションがなければ `turn_id.txt`） — UserPromptSubmit フックから Stop フックに渡す `turn_id`（§12）。30 日より古いファイルは `--stop` が消す

これらのファイルは、データフォルダに直接置く（Free は `.memory/` のサブフォルダに置く。Lite には、分けておくべき DB のファイルがない）。Redis の TTL を超えて会話の文を残すファイルは `audit.log` だけである。これがあるのは、Redis が止まっているときに、フックがメッセージを失ってはならないからである（§12）。

> **⚠️ アプリ用のデータフォルダではなくホームフォルダに置く理由**：1.6.x までの版は、設定を OS 標準のユーザーデータフォルダ（Windows では `platformdirs` による `%LOCALAPPDATA%\n3mc-workingmemory\n3mc-workingmemory\`）に置いていた。Microsoft Store（MSIX）版の Claude デスクトップアプリは、起動した MCP サーバーをパッケージの中で動かすので、Windows はその **`%LOCALAPPDATA%` 以下の読み書きを** `%LOCALAPPDATA%\Packages\<パッケージ>\LocalCache\Local\…` **に振り替える**。そこに置くと、同じ PC に `config.json` が 2 つでき、`owner_id` も 2 つになる（デスクトップアプリが起動したサーバー用と、ターミナルの `claude` やスクリプトが起動したサーバー用）。検索と一覧は `owner_id` で絞る（§3.12）ので、同じ Redis なのに、一方で保存した記憶がもう一方から見えない。ホームフォルダは振り替えの対象外なので、どこから起動しても同じファイルを読む。

**引き継ぎ（1 回だけ）**：`~/.n3mc-workingmemory/config.json` がなく、`N3MC_DATA_DIR` も設定されていないとき、`load_config()` は旧来の場所（起動したプロセスから見た `platformdirs.user_data_dir("n3mc-workingmemory")` + `config.json`）のファイルを、新しい場所へバイト単位でそのまま写し、`stderr` に 1 行出す。旧来のファイルは残す。写すときは §6 の排他的作成の規則（一時ファイル＋`os.link`）に従うので、2 つのプロセスが同時に引き継いでもファイルは 1 つになる。どちらの `owner_id` が残るかは、更新後に最初に起動したプロセスで決まる。もう一方の `owner_id` で保存した記録（長くても `ttl_seconds` の 7 日分）は結果に出なくなり、そのまま期限で消える。旧来のファイルもなければ、通常どおり新しい設定を作る。

Redis 状態は Redis コンテナが置く場所（既定では無名 Docker ボリュームで、`docker rm -f redis-stack` で消える）に保存される。

---

## 8. クライアント設定

> **どのアプリが何を使うか。** Claude Code は**フックか MCP**（利用者が選ぶ。両方でもよい）を使う。Claude デスクトップアプリと、フックのないほかのアプリは **MCP だけ**を使う（「v2.0.0 で変わったこと」を参照）。

### Claude Desktop

デスクトップアプリは、`claude_desktop_config.json` に並べた MCP サーバーを起動する。このファイルはアプリから開く（設定 → 開発者 → 構成を編集）。Windows での普通の場所は `%APPDATA%\Claude\claude_desktop_config.json` だが、Microsoft Store 版のアプリは別のフォルダに置くことがあるので、アプリから開くほうがよい。`mcpServers` の下に項目を足し（すでにある項目は残す）、保存して、**アプリを完全に終了してから起動し直す**。方法 A のセットアップでは、AI が下の「自動の登録」でこの項目を書き込むので、利用者が手で足すのは、プラグインの利用者とほかの MCP クライアントのときだけである。

pip でのインストール（絶対パスと `env`）：

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

プラグインの利用者（pip のインストールなし。フックは `uvx` で動く）：

```json
{
  "mcpServers": {
    "n3mc-workingmemory": {
      "command": "uvx",
      "args": ["--from", "n3memorycore-mcp-lite==2.1.0", "n3mc-workingmemory"],
      "env": {
        "N3MC_AGENT_NAME": "claude-desktop"
      }
    }
  }
}
```

- `command`：フックが使うのと同じスクリプトのフォルダにある `n3mc-workingmemory` の**絶対パス**（Windows：`where n3mc-workingmemory`、Linux：`which n3mc-workingmemory`）。アプリはターミナルの `PATH` を使わない。フックと MCP は同じコードを動かさなければならない（§4.6「この設計の限界」）。
- `env.N3MC_AGENT_NAME`：モデルが `agent_name` を渡さないときに、`agent_name` として保存する名前（§4.6）。アプリごとに 1 つの名前を使う。
- フックが別のデータフォルダ（`N3MC_DATA_DIR`）を使うときは、同じ `N3MC_DATA_DIR` を `env` に足す。そうしないと、デスクトップアプリは `~/.n3mc-workingmemory` と、別の `config.json`、別の `server_port` を使ってしまう。
- このアプリの既定の `session_id` を固定するために、`N3MC_SESSION_ID` を足してもよい（§3.1）。
- Windows や Linux のほかの MCP クライアントも、同じように登録する（command、args、env）。
- **デスクトップアプリの Code タブ**：デスクトップアプリは、この登録を Code タブ（Claude Code）のセッションにも渡す。そのままでは Code タブでフックと MCP の両方が同じ記憶につながり、モデルが頼まれずに `save_memory` を呼ぶと、Code タブの保存が `claude-desktop` の名前で入る。Claude Code をフックだけにしたい利用者は、`~/.claude/settings.json` の `permissions.deny` に `"mcp__n3mc-workingmemory"` を足してよい（Claude Code の設定なので、チャットには効かない）。こうすると、Code タブのモデルからこの登録のツールが見えなくなる。方法 A のセットアップは、この行を足さない。Claude Code は MCP でつないでよいからである。
- **自動の登録（方法 A のセットアップの手順 4）**：AI が次の順で行う。利用者に手で登録させない。
  1. 設定ファイルを探す。候補は、Windows では `%APPDATA%\Claude\claude_desktop_config.json`、`%LOCALAPPDATA%\Claude-Data\*\claude_desktop_config.json`（Microsoft Store 版。アプリはこのファイルを読む）、`%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\claude_desktop_config.json`。Linux では `~/.config/Claude/claude_desktop_config.json`。あるものの中で、更新日時がいちばん新しいものを使う。アプリは動くたびにこのファイルを書き直し、読まれていない古いファイルが別の場所に残っていることがある。1 つもなければ、デスクトップアプリは入っていないとみて登録を飛ばし、そう報告する。
  2. 書き込む前に、同じフォルダに控え `claude_desktop_config.json.bak-n3mc-<YYYYMMDDHHMMSS>` を取る。
  3. JSON として読み、`mcpServers` の下の `n3mc-workingmemory` の項目だけを上の pip の項目にする（`command` は `n3mc-workingmemory` の絶対パス）。フックが `N3MC_DATA_DIR` を使っているときは、同じ値を `env` に入れる。ほかの項目とキーは変えない。同じ名前の項目があれば置き換える。JSON として読めない、または一番外がオブジェクトでないときは、書き換えずに報告する。
  4. 同じフォルダの一時ファイルに書いてから置き換える。書いたあと読み直し、項目が入っていることを確かめる。
  5. 書いたファイルと控えの場所を、最後のメッセージで利用者に伝える。アプリを完全に終了して起動し直すまで、新しい登録は効かない。

### Claude Code（フックか MCP）

Claude Code は、フック（すべてのメッセージと応答を保存し、毎ターンに合う記憶を入れる）と CLI `n3mc-lite` から記憶を得るか（§12、§13）、MCP の入口から得る（§4.6。v1.12.2 と同じく、モデルがツールで検索と保存をする）。利用者が選ぶ。両方でもよく、そのときは指示（§5 (1) の規則 1 と 2）が、フックの塊があるときは毎ターン保存しないようモデルに伝える。入れ方は次のとおり：

**(a) プラグインマーケットプレイス — MCP（v1.12.2 と同じ。手動設定ファイル不要）**

```
/plugin marketplace add NeuralNexusNote/n3mcmcp-lite
/plugin install n3mc-workingmemory@neuralnexusnote
```

`uv` が PATH にあることが前提。`plugins/n3mc-workingmemory/.claude-plugin/plugin.json` はメタデータと、次の `mcpServers` を持つ（v1.12.2 の形。`uvx` の版はパッケージの版に固定する、§2）：

```json
{
  "mcpServers": {
    "n3mc-workingmemory": {
      "command": "uvx",
      "args": ["--from", "n3memorycore-mcp-lite==2.1.0", "n3mc-workingmemory"]
    }
  }
}
```

`plugins/n3mc-workingmemory/hooks/hooks.json` は `SessionStart` の 1 つだけを登録する（`UserPromptSubmit` と `Stop` は登録しない）：

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
    ]
  }
}
```

COMMAND A（シェルのコマンド。`hooks.json` では JSON の文字列として書き、`"` と `\` をエスケープする）：

```text
python "${CLAUDE_PLUGIN_ROOT}/hooks/install_permissions.py" || py "${CLAUDE_PLUGIN_ROOT}/hooks/install_permissions.py" || python3 "${CLAUDE_PLUGIN_ROOT}/hooks/install_permissions.py" || echo '{"systemMessage": "<FALLBACK>", "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "<FALLBACK>"}}'
```

`<FALLBACK>` は、下の「自動許可」にあるフォールバックの JSON の文（`\"` のエスケープ込み）である。

- 常駐サーバーは、MCP の入口が自分の `sys.executable` で起動する（`-m n3mc_mcp.cli --run-server`、§3.15）。つまり `uvx` の環境の中で動く。
- `--uvx` は、`uvx` で動くフックの入口（手で `uvx --from n3memorycore-mcp-lite==2.1.0 n3mc-lite-hook --uvx` と登録したもの）に、ルールファイルを `uvx --from n3memorycore-mcp-lite==2.1.0 n3mc-lite` のコマンドの形で書くよう伝える（§12）。これがなければ `python -m n3mc_mcp.cli` の形を使う（pip のインストール）。
- **`uvx` の起動時間（Windows 11、uv 0.11.7、パッケージは uv のキャッシュに入っている。1 回の慣らしの後の 10 回の中央値）**：システムの Python で `python -c pass` は 125 ms。`uvx --from n3memorycore-mcp-lite==2.0.2 python -c pass` は 316 ms。`uvx --from n3memorycore-mcp-lite==1.12.2 python -c "import n3mc_mcp.config"` は 503 ms。したがって `uvx` は、起動ごとに約 0.19 秒を足す（プラグインの MCP の入口は、セッションごとに 1 回起動する）。計測していないもの：パッケージのダウンロードや更新が要る実行、Linux。`uvx` のコマンドはパッケージのバージョンに固定する（§2）。

**(b) pip**

```bash
pip install n3memorycore-mcp-lite
n3mc-lite --init
```

`n3mc-lite --init` は、`n3mc-lite-hook` / `n3mc-lite-stop-hook` の絶対パスで 2 つのフックを `~/.claude/settings.json` に登録し、下の許可ルールを足す（§12「`--init`」）。Claude Code を再起動する（ウィンドウを閉じるだけでは足りないことがある。タスクバーの右端の「^」から Claude のアイコンを右クリックして終了するか、Windows から一度サインアウトする）。記憶は次のセッションから始まる。

**Claude Code の MCP の登録**：Claude Code にある `n3mc-workingmemory` の項目（`~/.claude.json` の user スコープ、またはプロジェクトの `.mcp.json`）は、そのまま使える。`n3mc-lite --init` は `~/.claude.json` — 一番上の `mcpServers` と、`projects` の各項目の `mcpServers` — と、今のフォルダの `.mcp.json` を読み（読むだけ）、見つけたら `An MCP registration of n3mc-workingmemory was found in Claude Code (<file>). It is kept: Claude Code can use the MCP tools and the hooks together.` を出力する（`projects` の項目なら `(<file>, project <project path>)`）。これらのファイルは直さない。両方を使うとき、指示は毎ターン保存しないようモデルに伝える（§5）。

設定を直した後はクライアントを再起動する。最初のメッセージの前に Redis Stack を動かしておくこと — そうしないと、フックは文を `audit.log` にだけ残し、「Redis Stack を起動してください」の通知を出す（§12）。

### コード生成の後の自動登録(方法 A — 必須)

**方法 A は、AI がフックを登録し、確かめたところで終わる。利用者に残る作業は、完全な再起動だけである。**利用者に設定ファイルを編集させてはならない。どのクライアントも使っていないパッケージのままフェーズ 1 を終えてはならない。`pip install -e .` が成功したら、すぐに次を行え。

1. **この PC の入口を見つける。**`pip install -e .` を実行した Python の scripts フォルダ(`python -c "import sysconfig; print(sysconfig.get_path('scripts'))"`、venv なら `Scripts\` / `bin/`)に、`n3mc-lite`、`n3mc-lite-hook`、`n3mc-lite-stop-hook`、`n3mc-workingmemory`(Windows では `.exe`)があることを確かめよ。コマンド名ではなく**絶対パス**を使え。Claude Code は、scripts フォルダを含まない `PATH` でコマンドを動かすことがある。
2. **Claude Code に MCP の入口を登録する。**`claude mcp add n3mc-workingmemory -s user -- <n3mc-workingmemory の絶対パス>` を実行せよ。user スコープに `n3mc-workingmemory` がすでにあり、そのコマンドが手順 1 のパスと違う(またはこの PC に存在しない)ときは、先に `claude mcp remove n3mc-workingmemory -s user` を実行してから足し直せ。`claude` コマンドが `PATH` に無いときは、代わりに `~/.claude.json` の最上位の `mcpServers` に項目 `{"type": "stdio", "command": "<絶対パス>", "args": [], "env": {}}` を書け。その前にファイルをバックアップし、ほかのキーはすべて残し、同じフォルダの一時ファイルに書いてから置き換える形で書け。プロジェクトの `.mcp.json` にある `n3mc-workingmemory` の項目は消してはならない。その中の絶対パスがこの PC に存在しないときだけ、手順 1 のパスに書き換えよ(§1 の確認 3)。**理由**：Claude Code は MCP とフックでつなぐ。評価(§10 の項目 1〜6、付録 B のフェーズ 3)は、再起動した Claude Code のセッションの MCP のツールで行う。
3. **フックを登録する**:`<n3mc-lite の絶対パス> --init` を実行せよ(§12)。これはフックと許可ルールを `~/.claude/settings.json` に書き、ほかの項目(ほかの N3MC 製品のフックを含む、§14)はすべて残す。**プラグイン `n3mc-workingmemory` が有効なら（`~/.claude/settings.json` の `enabledPlugins`。`--init` も知らせる）、止めて利用者に選ばせよ**：プラグインの MCP の入口は PyPI のコードを `uvx` で動かし、生成したコードとは `code_version` が違う（§3.15）。そのため、プラグインの MCP の入口と、このインストールのフックと CLI が、呼び出しのたびに互いのサーバーを止めて起動し直す。次を示せ：`The n3mc-workingmemory plugin is enabled. Choose one: (a) keep the plugin and remove this generated installation (pip uninstall n3memorycore-mcp-lite), or (b) disable the plugin (/plugin disable n3mc-workingmemory@neuralnexusnote), then run this setup step again.` 答えを待て。(b) なら、利用者がプラグインを無効にしたあとで手順 3 をもう一度行え。
4. **Claude Desktop**:上の「Claude Desktop」の「自動の登録」の 1〜5 を行い、手順 1 の絶対パスを入れた pip の項目を、デスクトップアプリの設定ファイルに書き込め。利用者に項目を示して手で足させてはならない。デスクトップアプリが入っていなければ、この手順を飛ばし、最後のメッセージでそう伝えよ。
5. **再起動を頼む前に確かめる。**
   - §10 項目 7.1 のフックの関門が `OK` を出力する。
   - `claude` が使えるなら、`claude mcp get n3mc-workingmemory` が手順 1 の絶対パスと `Connected` を示す。
   - `<n3mc-lite の絶対パス> --search "setup check"` が `# Recalled Memory Context` で始まる塊を出力する(これで常駐サーバーが起動する。初回の起動はモデルのダウンロードで数分かかることがある — 「まだ起動中」の代わりの出力でよい。その後、本当の結果か `_No memories found._` が出るまで繰り返せ)。
   - `n3mc-workingmemory` を自分で一度起動し、stdio で MCP の `initialize` を送ってから `tools/list` を送り、§4.3 の 7 つのツールがそろうことを確かめてから(応答は 2 秒以内に来る、§4.6)、その stdin を閉じよ。
   - どれかが失敗したら、直してやり直せ。動かないセットアップのまま渡してはならない。
6. **最後にメッセージを 1 つだけ出す**:利用者が再起動を見落とさないように、次のひな形どおりに書け。**メッセージの 1 行目は、必ず見出し `## ⚠️ Claude Code の再起動が必要です` にする。**その前に「登録が完了しました」などの文を置いてはならない。登録したものや注意点は、再起動の手順のあとに書く。

   ```text
   ## ⚠️ Claude Code の再起動が必要です

   セットアップが終わりました。Claude Code(と Claude Desktop)を完全に終了して、開き直してください。次のセッションから記憶が始まります。

   1. 開いている Claude Code をすべて閉じます(デスクトップアプリ、ターミナル、VS Code などの Claude Code の画面)。
   2. タスクバーの右端(時計の近く)の「^」を開き、Claude のアイコンがあれば右クリックして終了します。
   3. よくわからないときは、Windows から一度サインアウトして、サインインし直します(PC の再起動でもかまいません)。
   4. Claude Code を開き直し、このセッションを開いて「再起動しました」と送ってください。

   (ここに、登録したもの、書いたファイルと控えの場所、そのほかに伝えること)

   > ⚠️ **Claude Code を再起動するまで、記憶は働きません。**
   ```

   「Claude Code を再起動してください」の一言だけで済ませてはならない。「再起動しました」と送られてきたら、その発言が保存されたこと（`python -m n3mc_mcp.cli --list` に `[user]` の行が出る）、そのターンに `# Recalled Memory Context` が入ったこと、セッションに `n3mc-workingmemory` の 7 つのツール（§4.3）があることを確かめ、記憶が働いているかを短く伝え、次にすること（付録 B のフェーズ 2 に進み、そこにあるプロンプトを順に貼る）を案内せよ。

> **⚠️ 理由**:コード生成によるセットアップは、パッケージは入ったのに登録が動かない状態で終わることがある。たとえば、別の PC から写したフォルダの `.mcp.json` が `C:\Users\<別の PC のユーザー>\…\Python312\Scripts\n3mc-workingmemory.exe` を指していて、この PC には存在しないときや、`PATH` に頼るコマンド名だけで登録したときである。Lite の主なインストール方法はコード生成なので、手作業の設定なしで、動くセットアップで終わらなければならない。絶対パスの決まりは、フックのコマンドとデスクトップの項目に当てる。

### 自動許可（Claude Code 固有）

フックは Claude Code 自身が動かすので、許可は要らない。許可が要るのは、Claude が自分から検索するときに動かす CLI のコマンド（§13）と、Claude Code が MCP を使うときの MCP の入口のツールである。ルールがなければ、`--search` やツールの呼び出しのたびに利用者に聞く。

**pip（`PERMISSION_RULES`。`n3mc-lite --init` が書く）** — `~/.claude/settings.json` の `permissions.allow` に足す（欠けているものだけ。何も消さない）：

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

**プラグイン（`hooks/install_permissions.py`、SessionStart）** — v1.12.2 と同じ MCP のツールのルール：`mcp__plugin_n3mc-workingmemory_n3mc-workingmemory__*`（Claude Code は、プラグインが登録した MCP サーバーのツールを `mcp__plugin_<plugin>_<server>__<tool>` と名付ける）と `mcp__n3mc-workingmemory__*`（`claude mcp add` や `.mcp.json` で直接登録した同じツール）。

- **CLI のコマンド `--delete` と `--delete-session` は決して事前に許可しない**：不可逆なので（§4.3）、Claude Code はそのたびに利用者に聞く — ツールの説明がモデルに求めているのと同じ確認である。
- `<python> -m n3mc_mcp.cli` の形は、ルールファイルが Claude に示す形である（Free §4 ②：Claude Code が起こす bash のサブシェルには、scripts フォルダが `PATH` にあるとは限らない）。**`--init` は上のルールを、`<python>` = `shell_path(sys.executable)` で書く**。ルールファイルと同じ絶対パスなので、パッケージが venv にあっても、Claude が実行するものと合う。上の JSON は、`PATH` 上の `python` のときの形を示している。`n3mc-lite` の形は、対話で使うためのものである。

プラグインのスクリプトは v1.12.2 のものである（`plugins/n3mc-workingmemory/hooks/` は公開リポジトリだけのもので、方法 A は作らない、§2）。次のように動き、文は MCP のツールについてのものである：

- **書き込み**：書く直前に `settings.json` を読み直し、欠けているルールだけを合わせ、同じフォルダに一時ファイルを書いて `os.replace` する。項目は決して消さない。関係のないフィールドには触れない。
- **`settings.json` を更新できないとき**：何も変えず、stderr に 1 行と、`systemMessage` + `additionalContext` の JSON を出し、exit 0：`[n3mc-workingmemory] Could not update ~/.claude/settings.json (<ErrorType>: …), so the memory tools were not pre-approved and Claude Code will ask for permission on every memory call. Make the file writable, or add "mcp__plugin_n3mc-workingmemory_n3mc-workingmemory__*" to permissions.allow yourself.` ルールがすでにあるときは何も伝えない。
- **Python のフォールバックと、目に見える失敗**：`hooks.json` のコマンドは `python` → `py` → `python3` を試す。どれも動かない（またはスクリプトが非ゼロで終わる）ときは、Python を使わない段が JSON オブジェクトを 1 つ出して exit 0 で終わる：

```json
{"systemMessage": "[n3mc-workingmemory] Python was not found on PATH (tried python, py, python3), or the permission script failed, so the memory tools may not be pre-approved and Claude Code may ask for permission on every memory call. Install Python 3, or add \"mcp__plugin_n3mc-workingmemory_n3mc-workingmemory__*\" to permissions.allow in ~/.claude/settings.json.", "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "<the same text>"}}
```

- **`uvx` の事前チェック**：`shutil.which("uvx")`。なければ、次の日英両言語のインストール手順を載せた同じ形の JSON を出す：``[n3mc-workingmemory] `uvx` was not found on PATH. The plugin's MCP server is launched via `uvx --from n3memorycore-mcp-lite n3mc-workingmemory`, so it will fail to start until `uv` is installed. Install (Windows): `pipx install uv` or `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`; (Linux): `curl -LsSf https://astral.sh/uv/install.sh | sh`. Docs: https://docs.astral.sh/uv/ / [n3mc-workingmemory] `uvx` が PATH 上に見つかりません。`uv` を入れるまで MCP サーバーは起動しません。上のいずれかのコマンドで `uv` を入れてください。``ほかのメッセージと 1 つの JSON オブジェクトにまとめる。`json.dumps(..., ensure_ascii=True)` で書く。

> Claude Desktop にはツール単位のパーミッションゲートが無いため、これらはどれも不要。`plugin.json` に `permissions` フィールドを持たせる配布は Claude Code 側未対応（2026-04 時点）のため、プラグインでは `SessionStart` フック経由でユーザーの `settings.json` をパッチする方式を採る。

---

## 9. テスト（pytest）

> **目的**: 手動 Evidence Report（§10）を補完する、反復実行可能な自動回帰テスト。レイヤーごとに責務を切り分け、MCP ツール層の E2E は最後の砦とする。

### 実行方法

```bash
# 1. テスト専用の Redis Stack を起動（メモリが入っている Redis は絶対に使わない）
#    RediSearch は DB 0 しか索引できず、テストスイートはそれを FLUSHDB する
#    初回：docker run -d --name redis-stack-test --restart=no -p 127.0.0.1:6390:6379 redis/redis-stack-server:latest
#    2 回目以降：docker start redis-stack-test
docker start redis-stack-test 2>/dev/null || docker run -d --name redis-stack-test --restart=no -p 127.0.0.1:6390:6379 redis/redis-stack-server:latest

# 2. テストの接続先を明示して pytest
pip install -e ".[dev]"
N3MC_REDIS_TEST_URL=redis://localhost:6390/0 pytest tests/ -q
```

Windows PowerShell:

```powershell
docker start redis-stack-test 2>$null; if ($LASTEXITCODE -ne 0) { docker run -d --name redis-stack-test --restart=no -p 127.0.0.1:6390:6379 redis/redis-stack-server:latest }
pip install -e ".[dev]"
$env:N3MC_REDIS_TEST_URL = "redis://localhost:6390/0"; pytest tests/ -q
```

**テスト URL の既定値は無い。** Redis を要するテストは `N3MC_REDIS_TEST_URL` が設定されていなければスキップされる。またその URL がユーザーの実際の `config.json` の `redis_url` または `N3MC_REDIS_URL` と同じ場合、スイートは実行を拒否する（理由付きでスキップ）。実際の `config.json` は、サーバーと同じく `encoding="utf-8-sig"` で読む（BOM でその `redis_url` が隠れてはならない）。

**2 つ目の防御：DB そのもの**：URL の比較では、同じサーバーの別名（PC のホスト名、LAN のアドレス、`127.0.0.2`）を見逃す。フィクスチャは**毎回の** `FLUSHDB` の前に DB を確かめる：`DBSIZE` が 0 か、目印のキー `__n3mc_test_db__` があるときだけフラッシュし、フラッシュした直後に `SET __n3mc_test_db__ 1` する。データがあって目印のない DB は決してフラッシュしない — Redis のテストは `N3MC_REDIS_TEST_URL points at a Redis that holds data and is not marked as a test DB` という理由でスキップする。古いスイートの残りが入ったテスト用コンテナを使い回すには、一度だけ自分でフラッシュする。（目印は N3MC のキーではないので、`enforce_ephemeral()` は目印付きのテスト DB を共有とみなし、その永続化設定を変えない — 安全側。`TestEnforceEphemeral` は自分で用意した空の状態と N3MC のキーだけの状態を使う。）すべてのテストファイルは `conftest.py` の同じ `REDIS_TEST_URL` を使い、Redis URL をハードコードしてはならない。既定で `redis://localhost:6379/0` — MCP サーバーと同じ Redis — を使うと、素の `pytest` がユーザーのワーキングメモリを消してしまう。

`N3MC_REDIS_TEST_URL` が未設定、またはその先の Redis Stack に到達できない場合、テストは失敗ではなく自動スキップされる。Redis を必要としないテスト（ランキング、整形、設定）は Redis 無しで実行される。

**実データを守る（必須）**：`conftest.py` の autouse のフィクスチャが、すべてのテストで次を行う。

- `N3MC_DATA_DIR` を新しい一時フォルダに向け、そこから決まる `paths.py` のすべてのパス（`config.json`、PID ファイル、`server.log`、起動失敗の目印、`notices.json`、`audit.log`、`turn_id_*.txt`）も向ける — `cli.py`、`lite_hook.py`、`lite_stop_hook.py`、`server.py` のモジュールにある写しも向ける — ので、どのテストも `~/.n3mc-workingmemory` を読み書きしない。
- その一時的な `config.json` に、テスト用の Redis の URL と、**自分の `server_port`** を書く：テストのセッションごとに 1 回、ソケットを `127.0.0.1:0` に bind して取った空きポートである。テストは 18524（本物の Lite のサーバー）、18520（Free）、18521（Pro）を決して使わない。
- `HOME` と `USERPROFILE` を一時フォルダにする。そのため `--init`、プラグインの許可スクリプト、`~/.claude.json` の確認は一時ファイルを読み書きする。**本物の `~/.claude/settings.json` は決して読み書きしない。**
- テストが自分のポートで自分のサーバーを起動するとき以外は、`_request_shutdown` と HTTP の送り手（`_post`、`_get`）を偽物に差し替える（HTTP の層のテストは、`Host: 127.0.0.1:<port>` を付けて FastAPI の `TestClient` を使う）。
- テストが起動したサーバーは、テストが終わる前にすべて止める（自分のポートへの `/shutdown` で）。

`monkeypatch.setattr` / `monkeypatch.setenv` を使い、テストの終わりに pytest に元に戻させる。テストの中で **`monkeypatch.undo()` を決して呼ばない**：テストの途中で本物のパスが戻り、その後の書き込みが本物のデータフォルダに行く。

> **⚠️ 破壊的なテスト DB**：RediSearch は DB 0 以外でインデックスを作成できません（`Cannot create index on db != 0`）。このためテストスイートは各テストの前後で DB 0 を `FLUSHDB` します。残したいデータが入っている Redis には `N3MC_REDIS_TEST_URL` を向けないでください — テスト専用コンテナを用意してください。

> **⚠️ Evidence Report（§10）実施中は pytest を走らせない**：§10 はライブの MCP サーバー（同じ DB 0）に対して実メモリを保存・検索する。その最中に `pytest` を実行すると **`FLUSHDB` が §10 のデータごと消す**ため、検証が突然「該当なし」になり混乱する。§10 を回している間は pytest を止めるか、テストを別コンテナ／別ホストの Redis（テスト専用）に向けること。

### ディレクトリ構成

```
n3mcmcp-lite/
└── tests/
    ├── conftest.py          # Shared fixtures: isolated data dir, Redis URL override, dummy vectors
    ├── test_safe_path.py    # 安全なインポートパス: 今のフォルダのファイルが N3MC の中で動かない（モデル不要）
    ├── test_database.py     # Layer 1: Redis / RediSearch unit tests (CRUD, schema, TTL, dedup, BM25, KNN)
    ├── test_processor.py    # Layer 2: ranking math, embeddings, purify, chunking, reranker
    ├── test_server.py       # Layer 3: MCP tool dispatch E2E (isolated `config.json` + flushed DB 0)
    ├── test_regressions.py  # Layer 4: regression tests (query cleaning, exact-only dedup, pre-filter, shaping, config)
    ├── test_hooks.py        # Layer 5: hook flow, role prefixes, direct save
    ├── test_api.py          # Layer 6: resident server endpoints and process management
    ├── test_cli.py          # Layer 7: CLI commands
    ├── test_mcp_client.py   # Layer 8: MCP thin client
    ├── test_context_limit.py # Layer 9: context size limit
    └── test_coexistence.py  # Layer 10: --init, plugin files, coexistence with Free / Pro
```

**Layer 3**：ツールのハンドラは `tools.py` にあり（常駐サーバーの中で `/tools/<name>` のハンドラとして呼ばれる）、テストはその文を確かめる。

**残すテスト `tests/test_cross_session.py` が使う内部の形**：このファイルはこの指示書からは作らないが、次の名前を呼ぶので、生成するコードと `conftest.py` はそれらを備えなければならない。`conftest.py`：`cfg` と並べて、フィクスチャ `rconn`（フラッシュ済みのテスト用 Redis の接続。決定的な偽の埋め込みを適用済み）。`Database(cfg, default_session=<str>)`：セッションを渡さなかったときの `save_memory` と `search` のセッション。`Database.ensure_connected()` は成功すると `"ok"` を返す。`Database.client` は redis-py のクライアント（`close()` できる）。`Database.search(query)` は `(普通の行, 弱い行, メッセージ)` の 3 つ組を返し、行は `content`・`score`・`session_id` を持つ dict で、親ドキュメントのヒットは `_doc` が真。`tools.ToolRunner(cfg, db).call(name, args, nudge=False)` は、その `Database` でツールのハンドラを動かし、文を返す。

### Layer 1: `tests/test_database.py`（34 テスト）

| テストクラス | テスト内容 | カバレッジ |
|---|---|---|
| `TestIndexSetup` | RediSearch インデックス生成、再実行冪等性、欠損フィールドの追加 | スキーマ管理 |
| `TestInsertAndRetrieve` | INSERT→COUNT、ハッシュ整合性、embedding 無し挿入、`parent_id` 格納 | CRUD |
| `TestDedup` | 完全一致 SHA 衝突、親ドキュメント SHA ガード（類似度による拒否はない） | 重複排除 |
| `TestDelete` | 単体削除、親→チャンクカスケード、`docsha:` ガード掃除。長文と、その最初のチャンクと同じ本文の独立したレコードを保存し、チャンクを消しても独立したレコードのガードは残り、同じ本文の再保存は `duplicate`。独立したレコード自体を消したあとの再保存は `saved` | トランザクション保護 |
| `TestTTL` | 7 日 TTL 設定、**検索ヒットで TTL が変わらない**、期限切れ消失 | TTL |
| `TestFTS` | 句読点除去、BM25 スコア、短クエリ、CJK バイグラム展開によるヒット | FTS / 日本語 |
| `TestVectorSearch` | KNN 結果、空 DB 検索、`owner_id` フィルタ、独立したレコードだけ（`parent_id` が空。Python で絞る — `-@parent_id:{*}` の TAG のフィルタは使わない、§3.12） | KNN 検索 |
| `TestCjkBigramExpand` | ひらがな・カタカナ・漢字混在のバイグラム展開、境界処理 | トークナイズ |
| `TestSearchWritesNothing`（*Redis*、test_database.py） | 検索を 5 回ずつ（短い記録と長い記録。設定に `access_count_enabled: true` と `ttl_refresh_top_k: 5` を置く）しても、どのハッシュも 1 バイトも変わらない。30 回の検索のあとも、索引の `max_doc_id` とヒットの BM25 は変わらない | 検索の副作用（§3.6） |
| `TestSerialization` | ベクトルのバイナリ往復、f32 LE パック | シリアライズ |
| `TestSha1` | SHA1 ヘキサ、空文字、非 ASCII UTF-8、長文 | ダイジェスト |

### Layer 2: `tests/test_processor.py`（52 テスト）

| テストクラス | テスト内容 | カバレッジ |
|---|---|---|
| `TestCosineSim` | 同一ベクトル→1.0、直交→0.0、負値クランプ、RediSearch 距離→類似度 | 距離変換 |
| `TestTimeDecay` | 現在→1.0、半減期、フロア値、不正タイムスタンプ→1.0 | 半減期 |
| `TestKeywordRelevance` | 閾値以下切り捨て、正規化、ゼロ最大値 | BM25 正規化 |
| `TestFinalScore` | `(cos·0.7 + bm25·0.3)·decay·b_local`、`b_local` クランプ | ランキング式 |
| `TestFinalScoreProduct`（*Redis*、test_database.py） | キーワードの部分を固定し、語彙の加点を 0 にすると、検索の点数は基本点 × `time_decay` × `b_local` × `b_session`（不一致は 0.6）になる。どれか 1 つの因子だけを変えると、点数はその因子の分だけ変わる | ランキング式（§3.6） |
| `TestBLocal` | `b_local` は保存した importance を 0.5〜2.0 に収めたもの。読めない importance と NaN は 1.0 | ランキング式（§3.6） |
| `TestRemovedCounterSettings` | `config.json` の `access_count_enabled`、`access_count_weight`、`access_count_max_boost`、`ttl_refresh_top_k`（型や範囲が誤っていても）は `config_type` の通知を出さず、既定値にもなく、ファイルも書き換えない | 設定（§6） |
| `TestLexicalRerank` | 語彙被覆、フレーズブースト、短文優先、ゼロ被覆は非劣化 | 軽量リランカー |
| `TestPurification` | 複数行コードブロック→`[code omitted]`、インラインコード保持、複数ブロック | 整形 |
| `TestChunkText` | 閾値以下→単一、長文→分割＋オーバーラップ、境界一致。20 文字より短い末尾は直前のチャンクにつなぐ（本文のどの文字もチャンクに入る） | 親-チャンク |
| `TestEmbedding` | `passage:`/`query:` プレフィクス、ベクトル次元、同一テキスト類似度 | 埋め込み |
| `TestEncodingSafety` | 孤立サロゲート除去（`str` / `list` / `dict` / `None`）、全サロゲート入力は空文字列に縮退、除去後の `.encode("utf-8")` が成功 | エンコーディング安全策（§3.13） |

### Layer 3: `tests/test_server.py`（19 テスト）

| テストクラス | テスト内容 | カバレッジ |
|---|---|---|
| `TestToolRegistration` | 7 ツール登録（`recall_thread` 含む）、スキーマ型、description 非空 | MCP 登録 |
| `TestSaveAndSearch` | 保存→検索往復、完全重複拒否、空内容拒否、コードブロック purify（§10.3）| 単一チャンク |
| `TestDeleteBySession`（*Redis*） | 親文書 1 つと単独の記録 2 つを持つセッションの `delete_memories_by_session`：結果の JSON がちょうど（`documents_deleted` 1、`chunks_deleted` は親のチャンクの数、`singles_deleted` 2、`deleted` はその和）になる。ほかのセッションの記録は残る。`doc:` / `docsha:` のキーは残らず、同じ文をまた保存できる。2 回目は `{"status": "not_found", "session_id": …, "deleted": 0}`。空白の `session_id` は `session_id required`。値がほかの記録の id のガードはそのまま残る | §4.3 |
| `TestOwnerMismatch`（*Redis*） | ほかの `owner_id` を付けた `save_memory` は `{"status": "error", "saved": false, "reason": "owner_id mismatch"}` を返す | §4.3 |
| `TestListAndDelete` | 新着 3 件の列挙、存在しない ID の削除 | 一覧・削除 |
| `TestRepair` | 空 DB に対する `repair_memory`→ok | 修復 |
| `TestUnknownTool` | 未登録ツール名→エラー文字列 | ディスパッチ |
| `TestParentDocRecall` | 架空都市「不知火」設定の一字一句復元、チャンク→親集約、`[doc×N]` 表示、親→チャンク削除カスケード | 親ドキュメント（§3.11） |
| `TestEncodingSafetyE2E` | 孤立サロゲート混じりの content で `save_memory` が例外を投げない、全サロゲート入力は空内容エラー、孤立サロゲート混じりのクエリで `search_memory` が正常応答 | エンコーディング安全策 E2E（§3.13） |

### Layer 4: `tests/test_regressions.py`

埋め込みは決定的なフェイク（モデル不要）。*Redis* と記したクラスは `N3MC_REDIS_TEST_URL` を要する。

| テストクラス | テスト内容 |
|---|---|
| `TestQueryCleaning` | 制御文字と CJK・全角句読点が除去される。`「記憶装置」は？` が 記憶装置 を含むレコードにヒットする（*Redis*）。記号・制御文字リストのどのクエリでも `FT.SEARCH` が例外を投げない（*Redis*） |
| `TestExactOnlyDedup` | 値だけ新しいほぼ同一の短い事実が保存される。数か所を変えて作り直した長文文書が保存される。完全一致の重複は引き続き拒否される（*Redis*） |
| `TestTimeWindow` | クエリにより近い新しい行が `limit × 5` 件より多くあっても、`since` / `until` が範囲内の行を返す（*Redis*） |
| `TestShaping` | `vector_min_cos` が低コサインのベクトル候補を落とす。相対カット。最新版が先頭に移り、その位置のスコアを引き継ぐ。`_version_group` の行は相対カットで残る。短い行（< `version_min_chars`）はグループ化されない |
| `TestRender` | ローカル時刻の `saved=`、版についての一文、各通知が 1 回だけ出る。最初の検索のあとで加わった通知は次の検索で出る。弱い候補が普通の行のすべての後に `#### Possibly related (weak matches — may be unrelated)` の下に出る、普通の行が 0 件なら `_No memories found._` の後に弱い候補の見出しが出て版の一文は出ない、弱い候補がなければその見出しは出ない |
| `TestConfig` | 読めない `config.json` が変更されない。型の誤った項目が無視され報告される。bool は数値として受け付けない |
| `TestTestSafety` | `conftest` がライブの `redis_url` と同じテスト URL を拒否する。ライブの `config.json` を BOM 付きでも読める。データがあって目印のない DB はフラッシュしない |
| `TestFilterScore` | 現セッションの新しい記録が cos 0.86 で先頭にあるとき、cos 0.95〜0.99 の同じセッションの 3 日前の記録と別セッションの 1 日前の記録が、相対カットと `min_score` を生き残る。並び順は引き続き最終スコアによる |
| `TestRedisTimeouts`（*Redis*） | クライアントが `socket_timeout=5` で作られる。接続を受け付けて応答しないサーバーソケットで、`ensure_connected()` が約 5 秒以内にバックエンドエラーで戻る。つなぎ直しで古いクライアントが閉じられる |
| `TestConfigCreate` | 複数のプロセスが同時に `config.json` を作成しても `owner_id` は 1 つになる（どのプロセスもファイルの ID を使う）。ID のない既存の `config.json` から 2 つのプロセスを同時に起動すると、どちらもディスクの `owner_id` / `local_id` で終わり、利用者の設定は残り、両方が保存した記録は再起動後に一覧に出る。更新は一時ファイルと `os.replace` を通る。旧来の場所からの引き継ぎは排他的作成の規則（一時ファイル＋`os.link`）を使う |
| `TestConfigRanges` | `ttl_seconds` の `604800.5` / `0`、`embedding_dim` の `768.0`、`chunk_overlap >= chunk_threshold`、`chunk_threshold` 10、`half_life_days` 0、`min_score` 1.5 が `config_type` 付きで無視される（`min_results` の `-1` と `1.5` も同じ。`0` は受け付ける）。区切りのない本文で `chunk_text(text, 100, 100)` が終わる。`chunk_threshold: 100` だけのファイルでは、`chunk_overlap` は 25 で `config_type` は出ない（次の起動でも出ない）。補ったファイルには 25 が入る |
| `TestLongSaveAtomic`（*Redis*） | 長文の保存の途中で埋め込みが失敗しても `doc:` / `docsha:` のキーが残らず、そのあと同じ本文を保存できる。ガード、親、すべてのチャンクは 1 つの `SAVE_WITH_GUARD` スクリプトで書かれる |
| `TestGuardScriptAndPortReload`（*Redis*） | スクリプトを送るところで中断した保存（短文と長文）はガードを残さず、やり直しは `saved`（レコード 1 件、または親とすべてのチャンク）、3 回目は `duplicate`。ガード、親、チャンクに TTL がある。失敗のあとは、この保存の id を持つガードだけを削除する。開いたままの MCP エントリーは、`server_port` を変えたあとの 2 回目の呼び出しを新しいポートに送る。読めない `config.json` では、最後に読めた設定（同じ `owner_id`）を使い続ける |
| `TestIndexFailure` | `FT.CREATE` が `unknown command` で失敗しても `_main()` が終わらない。ツールは「Redis Stack が必要」の案内を返す。次の呼び出しで索引を再試行する。既存の索引の `DIM` が違えば `index_dim_mismatch` が加わる |
| `TestIndexFtsField`（*Redis*） | 新しい記録の `content_fts` と `content_ngram` に、`[user]` や `[claude 1/2]` の前置きと `[code omitted]` が入らない（生の `HGET`）。`content` は前置き付きのまま。フェンスを含む MCP の `save_memory` の `content_fts` にも `[code omitted]` が入らない。クエリ `claude`・`omitted` は、本文にその語を持たないフックの記録に BM25 で当たらない。`content_fts` のない古い形の索引（`content TEXT`）は `ensure_index()` が 1 回だけ作り直し（ハッシュは残る）、次の呼び出しでは作り直さない。`DIM` が違う索引は作り直さず、`index_dim_mismatch` を出す |
| `TestEnforceEphemeral`（*Redis*） | 空の DB や `mem:` / `doc:` / `docsha:` のキーだけの DB では `CONFIG SET` される。ほかのキーがある DB ではされず、`shared_redis` が加わる。サーバーのほかの DB にキーがあるときや `INFO` が拒否されたときも、設定を変えずに `shared_redis` が加わる。拒否された `CONFIG` は接続を失敗させずに `config_refused` を加える。**ほかの DB に残さない**：ほかの DB のテストが DB 1 に置くキーは、60 秒の有効期限（`EX 60`）付きで置き、`finally` で消す。空の DB のテストは、接続する前に DB 1 のそのキーを消す。**理由**：イメージの既定の保存の決まりを持つテスト用 Redis は、そのキーがある間にスナップショットを書くことがある。再起動するとキーをまた読み込み、空の DB のテストがほかの DB のキーを見て、最初の 1 回だけ `shared_redis` で落ちる（毎回新しい Redis を起動する CI では起きない） |
| `TestCodeOmittedOnSave`（*Redis*） | 短い本文も親ドキュメントも、コードブロックを `[code omitted]` に置き換えて保存される（生の `HGET` で確認）。インラインのバッククォートは残る。閉じていないフェンスは終わりまで置き換える。コードブロックの中だけが違う 2 つの本文は完全一致の重複になる。`skip_code_blocks: true` は本文を断る。フェンスのまま保存された記録（1.11.x 以前）も `[code omitted]` と見える |
| `TestSurroundingWhitespace`（*Redis*） | 外側の `strip()`：`' \nAAA\n '` は `AAA` として保存される。親ドキュメントは中の改行を残し、外側の空白だけを落とす。前後の空白だけが違う本文は完全一致の重複になる。空白だけの本文は `empty content` で拒否される |
| `TestModelWait` | ロードが `MODEL_WAIT_SECONDS`（テストでは短く差し替え）より長くロックを持つ間、`search_memory` は「still loading」の通知付きでキーワードだけで答え、`save_memory` は `detail` 付きでベクトルなしで保存する |
| `TestListPaging`（*Redis*） | 独立メモリと 260 チャンクの文書を保存したあと、`list_memories(20)` が両方を一覧に出す |
| `TestNoTtlRefresh`（*Redis*） | 検索ヒットは、`mem:` / `doc:` / `mem:sha:` / `docsha:` のどのキーの TTL も変えない：単独のレコード、親ドキュメント、そのチャンク、それらのガードは、保存の TTL のままである |
| `TestOrBm25`（*Redis*） | `quick fox elephant` と `what did the quick fox do` が `quick fox` を含む記録にヒットする。`記憶装置 大阪` が 記憶装置 を含む記録にヒットする。リランクの coverage が `note,` を `note` として数える |
| `TestTimeBoundsStrict` | `since="yesterday"` / `"2026/11/13"` が `Error:` の文を返す。`since > until` はエラー。日付だけの値はローカルの 0 時（UTC 以外の固定タイムゾーン） |
| `TestRepairReembed`（*Redis*） | `embedding` なしで保存したエントリに `repair_memory` がベクトルを付ける（1 回の呼び出しで最大 200 件、TTL は変えない、`reembedded` / `remaining` を報告） |
| `TestDeleteOwner`（*Redis*） | 別のオーナーの ID への `delete_memory` は `not_found` を返し、何も削除しない。親のチャンクは `chunk_ids` から削除される |
| `TestGuardNx`（*Redis*） | 同じ内容を同時に 2 回保存してもレコードは 1 つ。ガードを取ったあとの書き込みが失敗するとガードを解放する |
| `TestAccessCountNoGhost`（*Redis*） | 検索の途中で失効したか削除されたレコードは作り直されない：キーが 1 つも残らない（TTL のないハッシュも、`mem:sha:` / `docsha:` ガードも残らない） |
| `TestRecallOrphanChunks`（*Redis*） | 親があるとき、`recall_thread` は親を 1 回だけ出し（`[doc×N]`、全文）、チャンクは出さない。`doc:<id>` を消したあとは、`not_found` ではなく、その turn の各チャンクをチャンクの本文で出す |
| `TestRecallThreadSessions`（*Redis*） | セッション SA のターン A、セッション SB の発言、ターン A の応答の順に保存すると、`recall_thread(A, before=0, after=0)` は A の 2 行だけを返す。`before=1, after=1` の前後も SA の行だけ。同じ `turn_id` を 2 つのセッションに保存すると、両方の `session_ids` を持つ `ambiguous` を返し（ツール経由でも）、`session_id` を渡すとそのセッションの行だけ。そのターンのない `session_id` では行なし。親への折りたたみ、孤立チャンク、`not_found` は従来どおり |
| `TestParentDeleteTouchesOnlyItsKeys`（*Redis*） | DB にほかの鍵（たとえば `__n3mc_none__`、`other:data`）があるとき、親の `delete_memory` と `delete_memories_by_session` は、親とそのチャンクと自分の `docsha:` ガードだけを消し、ほかの鍵はすべて残す。別の ID を持つガードは残す。独立したレコードのガードは、従来どおり一緒に消す |
| `TestDeleteIdPrefix`（*Redis*） | `doc:<uuid>` を渡した `delete_memory` は、前置きのない親の ID と同じく連鎖削除する。`mem:<uuid>` は単独レコードとそのガードを消す。応答の `id` は前置きのない UUID。`doc:doc:<uuid>`（前置き 2 つ）は `not_found` |
| `TestNudgeOnErrors` | 数値でない `importance` の JSON、読めない `since` の `Error:`、予期しない例外、`search_memory` のバックエンドエラーが、どれもエラーの文で始まり、リマインダで終わる。予期しない例外は、ちょうど `Error: <メッセージ>` + リマインダになる（1.x と同じく、例外の型名を付けない） |
| `TestConditionalWrites`（*Redis*） | `HSET_IF_EXISTS` は、レコードがあれば書き込んで TTL を保ち、なければ何も作らない。`SCAN` のあとに消えたレコードに対する `repair_memory` はキーを作らず、`reembedded` に数えない。保存・検索・修復のあと、テスト DB のすべてのキー（テスト用の印を除く）に TTL がある |
| `TestHangulFiller` | `U+3164`、`U+115F`、`U+1160`、`U+FFA0`、`U+2800` だけの本文は見える文字なしとして拒否される。見える文字と一緒なら verbatim に保存される |
| `TestPluginHook`（追加分） | プラグインのスクリプトが §8 の MCP のツールのルールを書き、`settings.json` をアトミックに置き換える。フォールバックの文面に "or the permission script failed" が入る。`uvx` がないときは、§8 の案内の文が入る |
| `TestWeakMatches`（*Redis*） | 下限を通った行が 1 件で `min_results: 3` なら、弱い候補が 2 件その後に足され、`filter_score` の順に並び、`_weak` の印が付く、`vector_min_cos` 未満の候補は実際の cos で足される、`min_results: 0` では何も足さない、十分な件数が通ったときは足さない、`limit` が `min_results` より小さければ合計はその上限まで、1 つの親ドキュメントの 2 つのチャンクのヒットは弱い候補 1 行になり、すでに結果にある親は足さない、`filter_score` が `1e-6` 未満の候補は足さない、**どの行の `access_count` も変わらない**（弱い候補も普通の行も）、空のクエリ、または空白だけのクエリでは弱い候補を足さない、既定の設定は `min_results: 3` |
| `TestGuardReleasedOnAnyFailure`（*Redis*） | 短い本文と長い本文で：`turn_id: 12` の保存は失敗し、`mem:sha:` / `docsha:` のキーを残さず、そのあと同じ本文を保存できる。`agent_name`・`session_id`・`turn_id` の単独のサロゲートは除かれ、記録は保存される。サロゲートのないフィールドは変わらずに入る |
| `TestPluginHookAsciiStdout` | `uvx` がないとき、フックの標準出力は ASCII だけの 1 行で、`json.loads` すると `systemMessage` に日本語の案内がそのまま入っている |
| `TestLocalhostIpv4First` | ホストの `localhost` は、`127.0.0.1` に TCP でつながれば `127.0.0.1` になり（利用者名とパスワードは残る）、何も待ち受けていなければそのまま。ほかのホストは変えない。`localhost` のテスト用の URL が 1 秒以内につながる（§3.3） |
| `TestPublicRepoHygiene` | `tests/eval_uvx_missing.py` に絶対パスのユーザーのフォルダがない（外すフォルダは、`uvx` がある `PATH` の項目）。`.claude-plugin/marketplace.json` の `metadata.version` がパッケージの版と同じ |

`TestPluginHook` と `TestPluginHookAsciiStdout` は、MCP のツールのルール（`mcp__plugin_n3mc-workingmemory_n3mc-workingmemory__*`、`mcp__n3mc-workingmemory__*`）を確かめる。

### Layer 5: `tests/test_hooks.py`

偽の埋め込み（モデルなし）。*Redis* の印があるクラスは `N3MC_REDIS_TEST_URL` が要る。サーバーは、偽の `_post` の後ろに置いた `app.py` の `TestClient` か、なしである。

| テストクラス | テスト内容 |
|---|---|
| `TestAuditFirst` | どちらのフックの入口も、ほかの何よりも先に `{"ts", "hook", "raw", "payload"}` を `audit.log` に追記する。サーバーと Redis が止まっているときも、stdin が JSON でないとき（`payload` = `{"raw": …}`）も同じ。`raw` は 4,096 文字で切る。`audit.log` への書き込みが失敗してもフックは止まらない |
| `TestTurnIdHandoff`（*Redis*） | `--hook-submit` は新しい `turn_id` を作り、`turn_id_<session>.txt`（名前は無害化し、64 文字）に書き、それで `[user]` を保存する。`--save-claude-turn` はそれを読み、同じ `turn_id` で `[claude]` を保存する。ファイルは空にしない。ファイルがなければ新しい UUID を使う。画像だけのプロンプトでも新しい `turn_id` を書き、`[user]` は保存しない |
| `TestStopReadsTurnIdFirst` | `--save-claude-turn` は `turn_id_<session>.txt` を、設定の読み込みと `ensure_server` より前に読む：`ensure_server` の中でファイルを書き換えても（差し替え）、応答は書き換える前の `turn_id` で保存される |
| `TestTranscriptRecovery`（*Redis*） | Free の transcript の場合（最後のテキストブロックだけ。`tool_result` だけ、`isMeta`、中断の印、スラッシュコマンド、`thinking`、`tool_use`、`isSidechain` の行は飛ばす。末尾の今のプロンプトは飛ばす。中断された応答は見つける）で、直前の応答を直前の `turn_id` で保存する。Stop がすでに保存していれば、完全一致の重複の決まりで新しく何も保存しない。transcript がない、または壊れているときは `transcript_unreadable` を記録し、フックはそれでも検索し、保存する |
| `TestTranscriptUnicodeLineBreaks`（最後の場合は *Redis*） | U+2028、U+2029、U+0085 をエスケープせずに含む応答（JSONL にそのまま書かれたもの）を、全文のまま回収する。CRLF の行も読む。エスケープされた LF は本文に残る。5 MB のシークで途中から始まった最初の行と JSON でない行は飛ばす。次のプロンプトで、応答を前のターンの `turn_id` で 1 回だけ保存する |
| `TestSystemPrefixes` | `<task-notification>`、`<agent-message from=`、`<system-reminder>` のプロンプトは保存も検索もせず、`turn_id` を保ち、`stage: skipped` を書き、`# Recalled Memory Context\nSource: N3MC Lite (7-day working memory)\n\n_No memories found._` を出力する。`<div>` で始まるプロンプトは飛ばさない |
| `TestRolePrefix`（*Redis*） | 短いメッセージは、`agent_name` `claude-code` とフックの `session_id` を付けて `[user] <text>` として保存する。1,000 文字の本文は、親 `[claude] <body>` とチャンク `[claude 1/N] …` になる。`chunk_threshold` を少し超え、区切りで切ったあとの残りが 20 文字より短い本文（チャンク 1 つ。例：399 文字 + 空白 + 10 文字）は、親とチャンク `[claude] …` になる。長さの判定は前置きを無視する。`[code omitted]` は前置きより前にフェンスを置き換える。`mem:sha:<turn_id>:` / `docsha:<turn_id>:` は前置きを付けた文をそのやりとりの中でハッシュする。そのため MCP が保存した `ok` とフックの `[user] ok` はどちらも保存され、後のやりとりの同じ `[user] ok` はもう一度保存され、1 つの `turn_id` で 2 回保存した同じ応答（Stop フックと transcript からの回収）は `duplicate` になる。`skip_code_blocks: true` では、フェンスのあるメッセージは `skipped_code` になり、`audit.log` に残る |
| `TestDirectSave`（*Redis*） | サーバーがない（ポートにほかのフォルダのサーバーがいる、サーバーがまだ起動中、も同じ）とき、フックは直接保存する：同じレコードと前置き、`embedding` フィールドなし、TTL あり。`sentence_transformers` は import されない（`sys.modules` で確かめる）。後の `/repair` がベクトルを足す。何もキューに入れない |
| `TestRedisDown` | Redis に届かない（サーバーあり → `/buffer` が 503、サーバーなし → 直接保存が失敗）とき、フックは exit 0 で終わり、`audit.log` が文を持ち、通知 `redis_unreachable` は `docker start redis-stack` の案内を含み、次の検索の出力に出る。次に保存が成功した後で消える |
| `TestHookSearchOutput` | フックは `# Recalled Memory Context`、`Source: N3MC Lite (7-day working memory)`（空の塊とどの代わりの出力にも）、`Search query: …` と Lite の行（`### [<id>] turn=… score=… saved=…`）を出力する。空なら `_No memories found._`。サーバーがなければ `_(memory search unavailable: <reason>)_`。表示の時期が来た通知は見出しのすぐ後 |
| `TestHookSearchException` | `notices.json` の `last_shown` が文字列や壊れた値のとき、通知は出され、`last_shown` に今の時刻が書かれる。検索、描画、代わりの出力が例外を出しても（差し替え）、`--hook-submit` は `_(memory search unavailable: <例外の型>)_` を出力し、`[user]` を新しい `turn_id` で保存する |
| `TestHookExitStatus` | `n3mc-lite-hook` と `n3mc-lite-stop-hook` は、上のどの経路でも exit 0 で終わる |

### Layer 6: `tests/test_api.py`

偽の埋め込みを使う `app.py` の `TestClient`。一時的なデータフォルダ。印のあるところは *Redis*。

| テストクラス | テスト内容 |
|---|---|
| `TestHealth` | `/health` は `status`、`root`（一時的なデータフォルダ）、16 桁の 16 進数の `code_version`、`pid` を返す。`code_version` は `config.json` の内容が変わると変わり、空白やキーの順だけが変わったときは変わらない |
| `TestHostHeader` | `Host: evil.example` は、どのエンドポイントでも 403 になる。`127.0.0.1:<port>` と `localhost:<port>` は通る。`Origin: http://evil.example` と `Origin: null` は、`Host: 127.0.0.1:<port>` でも、どのエンドポイントでも 403。同じリクエストから `Origin` を外すと通る。CLI と薄いクライアントのリクエストには `Origin` がない |
| `TestPlainDef` | `/buffer`、`/search`、`/repair`、`/tools/{name}` は普通の `def` の関数である |
| `TestServerProcessEdgeCases` | 確かめた PID ファイルのサーバーが別のポートで待ち受け、その `/health` がこのフォルダを返すときは、そのポートに `/shutdown` を送り、新しいサーバーを起動する。そこにいる別のフォルダのサーバーには何もしない。`run_server` が `SystemExit(1)` か例外で終わると目印と `server_start_failed` を書き、正常の終了では何も書かず、次の `ensure_server(max_wait=0)` は起動し直さない。モデルのロックを持った状態で、`no_model_wait` はすぐ戻り、普通の呼び出しは待つ。`no_model_wait` つきの `/buffer` と `/search` は約 1 秒以内に答え、`embedding` なしで保存する。フックの要求には項目があり、`--search` の要求にはない。あとの保存が `save_failed` を消す。修復の実行中の 2 つ目の `repair` は "repair already running" を返す。`--search` は取り除いたあとの文を受け取る。Stop の入口の 2 つ目の手順は `CHILD_TIMEOUT` の残りを使う |
| `TestBufferEndpoint`（*Redis*） | `role` が user / claude なら、§3.16 の前置きを付けて保存する。応答は `save_memory` の dict。Redis が止まっている → 案内付きの 503 |
| `TestSearchEndpoint`（*Redis*） | `{"text"}` は `# Recalled Memory Context` で始まる。`max_chars` があれば制限し、なければ制限しない。読めない `since` → `Error:` の文付きの 400。Redis が止まっている → 503 |
| `TestToolsEndpoint`（*Redis*） | 7 つのツールはどれも、`nudge=1` ではハンドラと同じ文（Layer 3 の確認）を返し、`nudge=0` ではリマインダを除いた同じ文を返す。知らない名前 → `Error: unknown tool '<name>'`。オブジェクトでない本文は `{}` として読む。`search_memory` は `context_max_chars` に従う |
| `TestShutdown` | 別の `root` の `/shutdown` → 403。自分の `root` → `ok`（終了は偽物に差し替える） |
| `TestConcurrency`（*Redis*） | 8 つのスレッドから同時に行う保存と検索は、すべて成功する。同じ文を 2 回保存すると、レコードは 1 つになる |

### Layer 7: `tests/test_cli.py`

| テストクラス | テスト内容 |
|---|---|
| `TestServerDecision` | 正常 / 別の `code_version` / 別の `root` / `root` がなく確かめられない。先に `/shutdown`。PID ファイルの番号でプロセスを止めない。死んだ PID ファイルで待たない |
| `TestServerStart` | プラットフォームごとの切り離しのフラグ、stderr は `server.log` へ、`-m n3mc_mcp.cli --run-server`。早く終わった子プロセスは待ちを終わらせ、`server_start_failed` を出し、目印を書く。5 分以内には新しく起動しない。後で成功すれば両方を消す。60 秒たってもまだ起動中の PID ファイルのプロセスは、置き換えも重複もしない（プロセスの作成は偽物） |
| `TestServerCwd` | `_start_server` の `Popen` の `cwd` はデータフォルダである。このパッケージの親フォルダを今のフォルダにして動かしたときは、そのフォルダである |
| `TestStaleServerUnstoppable` | 自分の古いサーバーが止まらないとき（差し替え）、`/health` をもう一度見て、`ok` ならそれを使い、答えがなければ起動し、まだ古いサーバーが答えるなら使わない（`ensure_server` は使えるサーバーなしで戻り、フックは直接保存し、検索は `an old N3MC Lite memory server could not be stopped` を出す） |
| `TestNoProxy` | `HTTP_PROXY` がほかのローカルのサーバーを指していても、`_get` は N3MC Lite のサーバーに届き、プロキシには何も届かない |
| `TestCommands` | `--search` → `max_chars` なしの `/search`。`--search --since/--until` はそのまま渡す。読めない `--since` は `Error:` の文を stderr に出し、exit 2 で終わる。`--buffer` → `/tools/save_memory?nudge=0`。`--list` → `list_memories`。`--recall-thread` → `--before` / `--after` 付きの `recall_thread`。`--delete` → `delete_memory`。`--delete-session` → `delete_memories_by_session`。`--repair` → `repair_memory`。どれも応答の文をそのまま出力する。`--since yesterday`、`--until 9999-12-31`、`--since 2026-13-01` は、§4.3.1 の `Error:` の 1 行をちょうど stderr に出して exit 2 で終わる（Windows では `--since 1969-12-31` も。そこでは変換できないため。ほかの OS ではこのテストを飛ばす）。`search_memory` / `/search` は §4.3.1 の `Error:` の文を返す（`/search` は 400） |
| `TestServerPidAndIdentity` | `_write_own_pid` は、別の番号の入った PID ファイルを上書きし、ないファイルは作る。`run_server` はそれを 2 回呼び、2 回目は待ち受けの前。PID ファイルのサーバーが別のポートで待ち受け、その `/health` がこのフォルダを返すとき、`_handle_other_port` はそのポートに `/shutdown` を送る。`redis_url_mismatch` を出したあと、`redis` の一致する自分のサーバーが答えると消える。`run_server` は Redis への接続とモデルの読み込みより前に `code_version` を計算し、その値を `create_app(..., code_version=...)` に渡す |
| `TestForeignPort` | ポートにほかのフォルダのサーバーがいるとき：検索は `port_conflict` の代わりの出力を出し、`--list` / `--delete` はバックエンドエラーを出し、フックの保存は直接入る |
| `TestStop` | `--stop` はルールファイルを書き（§12）、30 日より古い `turn_id_*.txt` を消し、50 MB を超えた `audit.log` をローテーションし（閾値は差し替える）、何も出力せず、サーバーは止めない。フックの `cwd`（なければ作業フォルダ）がホームフォルダのとき、`--stop` は `.claude/rules/` に何も作らない（§12.4 の手順 1）。**プロジェクトのフォルダ**: `CLAUDE_PROJECT_DIR` が別の一時フォルダを指すときは、規則ファイルをそこに作り、フックの `cwd` には何も作らない。変数がないときはフックの `cwd` に作る。変数がホームを指すときは何も作らない |

### Layer 8: `tests/test_mcp_client.py`

モデルなし。薄いクライアントのハンドラを、HTTP の送り手を偽物にして関数として呼ぶ。ほかにサブプロセスのテストが 1 つ。

| テストクラス | テスト内容 |
|---|---|
| `TestThinNames` | `n3mc-workingmemory` と `n3mc-mcp-lite` は `n3mc_mcp.__main__:run` に対応する。`serverInfo.name` は `n3mc-workingmemory`。`pyproject.toml` はバージョン `2.1.0` と `mcp>=1.0.0,<2.0.0` を宣言する |
| `TestThinTools` | `tools/list` は、サーバーなしで §5 (3) の 7 つの定義をそのまま返す。指示は §5 (1) の文そのまま。ハンドラは `validate_input=False` で登録する（拒まれたらなしで） |
| `TestForwarding` | 各ツールは引数を付けて `POST /tools/<name>?nudge=1` として送り、応答の文をそのまま（バイト単位で同じに）返す。`session_id` は `search_memory` / `save_memory` で、ないときだけ埋める（引数 → `N3MC_SESSION_ID` → プロセスの UUID。どの呼び出しでも同じ）。`agent_name` を埋める（引数 → `N3MC_AGENT_NAME` → `clientInfo.name` → 空。空白の引数でほかに値がなければ空にし、空白のまま保存しない）。ハンドラの中の例外は、ちょうど `Error: <メッセージ>` + `_NUDGE_GENERIC` になる。どの結果も `isError` を設定しない。`importance: "nan"` はサーバーに届き、Lite の JSON を受け取る（SDK の検証エラーではない） |
| `TestThinNoServer` | サーバーがない / まだ起動中 / 起動に失敗した / ほかのフォルダのサーバー：`save_memory` は直接保存する（`embedding` フィールドなし、§4.6 の `warning` と `detail`）。ほかのツールは §4.6 の文を返す。Redis も止まっている → バックエンドエラーの JSON。`delete_memory` / `delete_memories_by_session` と `--delete` / `--delete-session`：未送信 → "Nothing was deleted"、完了 → 通常の応答、削除後に応答が失われた（タイムアウトを差し込む）→ `"status": "unknown"` |
| `TestRedisDisplayAndCodeVersion` | 利用者とパスワードを含む正しい URL の `redis_display` はホスト・ポート・DB だけを示す。解析できない URL（`redis://u:secret@host:abc/0`）では `<unparseable redis_url>` になり、`/health` にも `redis_url_mismatch` の通知（stderr、`notices.json`、`audit.log`）にも `secret` が入らない。何も変えなければサーバーの `code_version` はクライアントと一致し（補完して書いたあとも）、`load_config()` と起動の間に `config.json` が変わると `stale`、再起動後に `ok`。`--stop-server`：止めた、動いていない、ほかのフォルダのサーバー、止められなかった（exit 1）：要求は通ったがポートが使われたまま、要求が失敗してポートも使われたまま。要求が失敗してもポートが空いたら、止めた（exit 0） |
| `TestThinStart` | `ensure_server` は、始まったときにバックグラウンドのスレッドで 1 回（それが止まっている間も `initialize` に応答する。差し替えて確かめる）と、毎回のツール呼び出しの前に動く。入口は決して `/shutdown` を呼ばない |
| `TestThinNoModel` | 薄いクライアントを import して動かしても、`sentence_transformers` や `torch` を import しない（`sys.modules`）。MCP のメッセージ以外は stdout に何も書かない（取り込んで確かめる）：stdin が空なら stdout も空。`initialize` と `tools/list` のあとは、ちょうど 2 つの応答（`serverInfo`、そのままの指示、7 つのツール）だけがある |
| `TestThinSubprocess`（*Redis*） | 一時的な `N3MC_DATA_DIR` と、`HF_HOME` に本物の Hugging Face のキャッシュのフォルダ（fixture が `HOME` を差し替える前に求める。モデルがそのキャッシュになければ飛ばす。テストが約 1.1 GB を取りに行くことはない）を付けて子プロセスとして起動した `n3mc-workingmemory` と、`mcp.client.stdio.stdio_client` で話す：2 秒以内の `initialize`、7 つのツール、テスト用のポートの本物のサーバーを通した `save_memory` から `search_memory` への往復。クライアントを閉じた後もサーバーは動いており、テストがそれを止める。ただし、クライアントのジョブがサーバーも終わらせることがある（§3.15）ので、閉じた後にサーバーが動いていれば止め、なくなっていれば次の CLI の呼び出しで起動し直すことを確かめる（どちらでも合格） |

### Layer 9: `tests/test_context_limit.py`

モデルなし、Redis なし（用意した結果の行から表示する）。

| テストクラス | テスト内容 |
|---|---|
| `TestContextSize` | `context_size` は UTF-16 のコード単位を数え（`😀` は 2）、改行はそれぞれ 2 回数える |
| `TestRenderLimit` | `max_chars` がなければ出力は変わらない（20 行、10,000 超）。収まる出力は、制限のないものと同じ。上限を超えると、残る行は順番どおりの最初の行で、もう 1 行足すと収まらない。正しい N の省略の行が、最後に残した普通の行の後、弱い候補の見出しの前に来る。3 行のときは文を切り、切った印の行で終わる。最初のヒットの 30,000 文字の親ドキュメントは切る。手順 1 は通知と弱い行を残す。日本語と絵文字の行も上限の中に収まる。MCP の出力は末尾に `_NUDGE_AFTER_SEARCH` を残し、それも数える |
| `TestHookUsesLimit` | `--hook-submit` は最大 `context_max_chars`（既定 9,500）まで出力する。手で行う `--search` はすべての行を出力する |
| `TestConfigContextMaxChars` | 既定 9500。`1999`、`10001`、`9500.0`、`"9500"` は `config_type` 付きで無視する。`2000` と `10000` は受け付ける |

### Layer 10: `tests/test_coexistence.py`

一時的な `HOME` だけ。

| テストクラス | テスト内容 |
|---|---|
| `TestInit` | `--init` は、2 つのフックを絶対パス（スラッシュ区切り、`shell_path` が要るとしたときだけ引用符）で書き、`PERMISSION_RULES` を書く。`shell_path` は、ふつうのパスと 8.3 形式の短い名前（`PROGRA~1`）はそのままにし、空白・`'`・`&` を含むパスは二重引用符で囲み、`$` と `` ` `` をエスケープする。scripts フォルダが `O'Brien` という名前のフォルダの下にあるとき、フックは引用符付きで書かれ、自分のものと分かる。もう一度実行しても何も書かない。変える前にバックアップを作る。不正な `settings.json` は変えず、終了コード 1。自分の入口・`-m`・パッケージのスクリプトの形は置き換え、重複させない（バックスラッシュで書かれていても同じ） |
| `TestInitNonStringCommand` | `command` が null や数のフックの項目は残り、`--init` は例外で終わらずに自分の項目を足す |
| `TestKeepsOtherProducts` | Free の `n3mc-hook` / `n3mc-stop-hook` の項目、Pro の `python ".../n3mc_hook.py"` の項目、ほかのどのフックも残し、知らせる。Free 自身の見分け方 — その規則をここに書く：実行ファイル `n3mc-hook` / `n3mc-stop-hook`（`.exe` の有無を問わず）、`-m n3memorycore.n3mc_hook` / `-m n3memorycore.n3mc_stop_hook`、または Free のパッケージのフォルダの中のスクリプト `n3mc_hook.py` / `n3mc_stop_hook.py` — は Lite の項目に当たらない |
| `TestPluginEnabled` | `enabledPlugins["n3mc-workingmemory@neuralnexusnote"]` が true でも、`--init` はフックと許可ルールを登録し、§12.8 の手順 3 のメッセージを出力する |
| `TestPluginRegistersMcp` | `plugin.json` に §8 の `mcpServers` がある（`uvx` はパッケージの版に固定）。`hooks.json` は `SessionStart` だけ。マーケットプレイスのプラグインは 1 つ。`--init` は `~/.claude.json` にも `.mcp.json` にも MCP サーバーを足さず、消さない。そこにある `n3mc-workingmemory` の項目は「残す」と知らせる |
| `TestSeparateNames` | 既定のポート 18524、データフォルダ `~/.n3mc-workingmemory`、ルールファイル `n3mc-lite-behavior.md`、PID ファイル `n3mc-lite.pid`、コマンド `n3mc-lite*` — どれも Free のもの（`18520`、`~/.n3mc`、`n3mc-behavior.md`、`n3mc*`）や Pro のもの（`18521`）と同じでない |
| `TestRulesFile` | ルールファイルは、`python -m n3mc_mcp.cli` の形か、`--uvx` 付きなら `uvx` の形で、テンプレートから書く。もう一方の形のテンプレートと同じファイルは置き換える（バックアップ付き）。手で直したファイルは置き換えず、`rules_md_edited:<hash>` を出す。1 行目は `<!-- n3mc-lite-behavior v1 -->` |

### 安全なインポートパス: `test_safe_path.py`（モデル不要）

プロセスを起動するテストは、どれも一時的なデータフォルダと一時的なホームフォルダを渡す。

| Test | 確かめること |
|---|---|
| `test_there_are_entry_modules` | メインのモジュールが、`if __name__ == "__main__":` で終わるモジュールの 1 つである。そうしたモジュールが 3 つ以上ある |
| `test_entry_module_starts_with_the_safe_path_block`（入口のモジュールごとに 1 回） | docstring のあとの最初の文が、`import os as _os`、`import sys as _sys`、`sys.path[0]` を消す `if __name__ == "__main__":` のブロックである |
| `test_files_in_the_current_folder_do_not_run` | `argparse.py`・`shlex.py`・`queue.py`・`fastapi.py`・`pydantic.py`・`redis.py`・`numpy.py`・`uvicorn.py`（どれも、目印のファイルを書いて `ImportError` を出す）を置いたフォルダで、`python -m n3mc_mcp.cli --help` が使い方を出し、0 で終わり、目印を残さない |
| `test_without_the_block_they_would_run` | 同じフォルダで `python -c "import shlex"` を実行すると、目印が書かれる（上のテストが空振りでないことの確認） |
| `test_a_source_checkout_run_in_place_still_works` | リポジトリのルートから、`PYTHONPATH` なしで、`python -m n3mc_mcp.cli --help` が 0 で終わる |
| `test_children_are_started_with_dash_P_outside_a_checkout` | `python_cmd()` は、ほかのフォルダでは Python 3.11 以降で `[sys.executable, "-P"]`（3.10 では `[sys.executable]`）、リポジトリのルートでは `[sys.executable]` になる |
| `test_no_child_is_started_without_python_cmd` | パッケージのどのモジュールにも `[sys.executable,` がない |

### 実行例

```bash
# 全テスト
pytest tests/ -q

# 単一レイヤー
pytest tests/test_database.py -v

# 遅い埋め込みテストをスキップ
pytest tests/ -v -k "not TestEmbedding"
```

> **⚠️ `slow` マーカー（任意）**：埋め込みモデルをロードする重いテストは上記のとおり `-k "not TestEmbedding"` で除外できる。代替として `@pytest.mark.slow` を付け、`pyproject.toml` の `[tool.pytest.ini_options].markers` に `slow` を登録しておけば `pytest -m "not slow"` でも高速回帰できる（マーカー未登録だと `PytestUnknownMarkWarning` が出る）。

> **⚠️ Evidence Report との関係**: 自動テストの不合格は §10 の合否をブロックしない。Evidence Report が実装完了の唯一の合否基準である。自動テストは開発者が任意で実行する補助的な回帰テストであり、初回実装時に無限の修正・再試行ループを引き起こしてはならない。

---

## 10. 自律評価（Evidence Report）

> 実装完了後、AI は以下を自律実行し、各項目を ⭐⭐⭐⭐⭐ で報告すること。スコアはコード生成直後の一発勝負ではない。先に §9 の pytest を実行し、その結果を報告する。§9 のとおり、失敗したテストはこの判定を止めない（合否を決めるのは検証レポートである）が、失敗したテストはすべて報告に名前を挙げる。MCP Lite 版は Redis Stack 前提・7 日 TTL という制約下での最善を測る。

> **進め方（時間を減らす）**: 一時的なデータフォルダや試験用の Redis でテストを行うときは、次のとおりにする。
> - 一時的なデータフォルダ、常駐サーバーのポート、試験用の Redis は 1 組だけ用意し、テストの間は常駐サーバーを止めずに使い回す。テストごとに作り直したり、サーバーを起動し直したりしない（起動のたびに、埋め込みモデルの読み込みで 20〜30 秒ほどかかる）。
> - `config.json` を書き換えるテストは、最後にまとめて行う（設定を変えると、サーバーが起動し直すことがある）。
> - 一時的にするのはデータフォルダだけにする。`HOME` / `USERPROFILE` を替えるときも、`HF_HOME` は本物のモデルのキャッシュ（`~/.cache/huggingface`）を指したままにする（約 1.1 GB のモデルの再ダウンロードを防ぐ）。
> - pytest（§9）は、直している途中は関係するテストのファイルだけを流す。全体は、この評価の前に 1 回だけ流す（上のとおり）。

> **項目 1〜6 でのツールの呼び方**：各ツールは **MCP で**呼ぶ。方法 A のあとに再起動した Claude Code のセッションの `n3mc-workingmemory` のツールを使う（付録 B のフェーズ 3）。確かめは、利用者の呼び出しと同じ道を通る：MCP の入口（薄いクライアント、§4.6）、常駐サーバー、Redis。項目 1 の前に、`claude mcp get n3mc-workingmemory` が `Connected` を示し、セッションに §4.3 の 7 つのツールがあることを確かめる。**ツールがなければ、止めて、利用者に Claude Code の再起動を頼む（§8 のひな形）。CLI に切り替えてはならない。** Claude Code がツールの許可を求めたとき（たとえば `delete_memories_by_session`）は、許可する。報告には、項目ごとに使った道を書く。項目 1 の (d) はサーバー側の関数のマイクロベンチマークで、項目 1 の Redis が止まったときの確かめは項目 10 の MCP の入口で行う（項目 1 を参照）。項目 10 でも、一時的なフォルダで MCP の入口を確かめる。

1. **常駐速度テスト**: 次を分けて測る：(a) MCP の入口の `initialize` への答え（記録する）、(b) サーバーの起動から `/health` が答えるまで（記録する。ライブラリの import に約 10 秒かかる、§3.9）、(c) モデルを読み込んだあとの最初の検索（記録する）、(d) その後の検索 10 回の中央値。(d) はサーバー側の処理（埋め込み + Redis の KNN/BM25 + ランキング）として、`processor` / `database` の関数を直接呼ぶマイクロベンチで測る。**合格：(d) が CPU 環境で 0.5s 以内。** (a)〜(c) は PC によるので、報告するが判定しない。Redis Stack が起動していない状態で呼び出した場合、サーバーがクラッシュせず「Redis Stack を起動してください」のヒントを返すことも併せて確認せよ。これは項目 10 の MCP の入口（`redis_url` を §9 のテスト用の Redis にした一時的なデータフォルダ）で確かめる：`docker pause redis-stack-test` をしてツールを呼び、そのあと `docker unpause redis-stack-test`。**利用者の Redis は決して止めたり一時停止したりしない**：永続化を切ってある（§3.4）ので、再起動するとすべての記憶が消える。（サーバーが動いている間 PID ファイルがあること、`config.json` を直した後の次の呼び出しで新しいサーバーが起動することも確かめよ。）

   > **⚠️ 計測の解釈**：「定常 0.5s 以内」は **純サーバー側処理**（埋め込み生成 + Redis KNN/BM25 + ランキング）の目標値である。MCP クライアント経由（Claude Code 等）で計測すると **JSON-RPC 往復・クライアント側の LLM 処理時間が上乗せ**され、エンドツーエンドの中央値は **5〜10s** になることがある（これは仕様逸脱ではない）。純サーバー側を測りたい場合は `processor`/`database` 関数を直接呼ぶマイクロベンチを使うこと。また §3.9 の通りモデルロードはバックグラウンド化されるため、**バックグラウンドのロードが終わる前に届いた呼び出しは、最大 20 秒待ち**（`MODEL_WAIT_SECONDS`、§3.9）、そのあと「まだ読み込み中」の通知付きでキーワードだけで動く点も計測時に留意（モデルロード後の初回は数秒）。

2. **実在人物テスト（実在歴史データ）**: 実在の歴史人物に関するテキストを `save_memory` で保存後、その人物名で `search_memory` を実行し、**上位 3 件以内**に該当レコードが含まれることを合格基準とする。
   - 日本語版の例: 「坂本龍馬」
   - 英語版の例: 「Abraham Lincoln」

3. **架空設定テスト（創造的架空設定 / 親ドキュメント契約）**: 架空の世界観・固有名詞を含む **400 文字以上**のテキストを `save_memory` で保存後、`search_memory` で取得し、**保存したテキストの全セクションが一字一句変化なく復元**できることを合格基準とする。§3.11 の親-チャンク設計により、検索はチャンクでヒットしても応答には `doc:<parent_id>` から取り出した verbatim な全文が現れなければならない。Claude 応答中のコードブロックは文書設計に従い `[code omitted]` に置換される。
   - 日本語版の例: 「浮遊都市・不知火（架空の設定資料）」
   - 英語版の例: 任意の架空のキャラクター・都市・固有名詞

4. **FTS 句読点・CJK 耐性テスト**: 括弧・句読点を含む日本語テキスト（例: `架空の惑星「アルファ9」の気温設定`）を `save_memory` で保存後、括弧を含まないクエリ（例: `アルファ9 気温`）**と CJK 句読点を残したクエリ**（例: `「アルファ9」の気温は？`）で `search_memory` を実行し、どちらでも**上位 3 件以内にヒット**することを合格基準とする。§3.7 の句読点除去と CJK バイグラム展開が保存側・クエリ側の両方で一貫して適用されていることを検証するテストである。

5. **完全記録テスト**: 空でない入力がすべて記録されることを確認せよ。旧「N 文字未満フィルタ」「定型語スキップ」「ノイズパターン」が**存在しない**こと。
   - 2 文字の文字列（例: `はい`）を `save_memory` に渡し、**レコードが保存される**こと。
   - 定型語（例: `ok`、`yes`、`thanks`）を `save_memory` に渡し、**レコードが保存される**こと（ただし完全重複は server-side で拒否されうる — その場合は別文字列で再試行）。
   - 更新した事実（例: `The project budget is 100 dollars.` の後に `The project budget is 120 dollars.`）が**新しいエントリとして保存され**（`near_duplicate` にならない）、それを `search_memory` で検索すると両方が `saved=` 時刻付きで並ぶこと。
   - 空文字列・空白のみの入力は `empty content` で拒否されることを確認。見える文字のない入力（例：`\x00`、単独の `U+200B`）は `empty content (no visible characters)` で拒否されることを確認（§3.13）。

6. **session_id 一括削除テスト（`delete_memories_by_session`）**: 不要になったプロジェクト／タスクのワーキングメモリを TTL を待たずに整理できることを確認せよ。
   1. 一意な `session_id`（例: `eval-cleanup-YYYY-MM-DD`）を決め、その session_id を渡しながら **複数の異なる形態**のレコードを `save_memory` する：
      - 短い single レコード（例: `はい` / `ok` などの 2〜10 文字）
      - 中程度の single レコード（数十〜数百字の事実 1 件）
      - **`chunk_threshold`（既定 400）字を超える長文**（→ サーバが親ドキュメント `doc:<uuid>` ＋複数チャンクに自動分解する）
   2. 削除前に `search_memory(query=..., session_id=<対象 session_id>)` で該当レコードがヒットすること、`list_memories` で `[doc×N]` タグ付き親ドキュメントが見えていることを確認する。
   3. `delete_memories_by_session(session_id=<対象 session_id>)` を **1 回**呼ぶ。応答は `{"status": "deleted", "session_id": ..., "documents_deleted": D, "chunks_deleted": C, "singles_deleted": S, "deleted": D+C+S}` の形であること。投入したレコード数と整合する数値が返ることを確認する（親ドキュメント＋全子チャンクがカスケード削除される）。
   4. 削除直後に同じ `session_id` で `search_memory` を実行し、対象レコードが**1 件もヒットしないこと**を確認する（他 session_id のレコードは残る — 当該 session_id だけがピンポイントで除去されるハードフィルタ削除である点を確認）。
   5. **冪等性確認**: `delete_memories_by_session` を**もう一度**同じ `session_id` で呼ぶと `{"status": "not_found", ..., "deleted": 0}` が返り、エラーやクラッシュは起きないこと。
   6. **合格基準**: 投入件数とサーバ応答の `deleted` 合計が一致／親-チャンクのカスケード削除が成立／他 session_id のレコードに副作用なし／2 回目呼び出しが安全に no-op になること。**不可逆操作のため、本テストは専用 session_id でのみ実施し、本番運用 session_id では絶対に走らせないこと。**

7. **フックの統合（Free §6 テスト 7 を手直し）**：Claude Code の中で確かめる：
   1. **グローバルのフックの登録がある**（必須の関門）。次を実行する：
      ```bash
      python -c "import json,os; s=json.load(open(os.path.expanduser('~/.claude/settings.json'),encoding='utf-8')); h=s.get('hooks',{}); p=s.get('enabledPlugins',{}).get('n3mc-workingmemory@neuralnexusnote'); ok=lambda ev,names: any(any(n in c.get('command','').lower() for n in names) for e in h.get(ev,[]) for c in e.get('hooks',[])); assert not p, 'the plugin is enabled: its MCP entry runs the PyPI code, not this installation (Method A step 3)'; assert ok('UserPromptSubmit',('n3mc-lite-hook','n3mc_mcp.lite_hook')), 'UserPromptSubmit hook missing'; assert ok('Stop',('n3mc-lite-stop-hook','n3mc_mcp.lite_stop_hook')), 'Stop hook missing'; assert any(r.endswith('-m n3mc_mcp.cli --search *)') for r in s.get('permissions',{}).get('allow',[])), 'permission rules missing'; print('OK')"
      ```
      合格：`OK` を出力する。この確認は、このインストールの `--init` が書いたフックを求める。プラグインが有効だと失敗する。プラグインの MCP の入口は、生成したコードではなく PyPI のコードを動かすからである（方法 A の手順 3）。失敗したら実装は終わっていない：`n3mc-lite --init` を実行する（§12）。
   2. **UserPromptSubmit**：**インストール先のフォルダの外**（例 `~`）で開いた Claude Code のセッションからメッセージを送り、そのターンのコンテキストに `# Recalled Memory Context` があること、直前の応答とメッセージが保存されていること（`python -m n3mc_mcp.cli --list` が `[claude] …` と `[user] …` のレコードを表示する）を確かめる。インストール先のフォルダの外であることが肝心である：その中では、グローバルの登録が欠けていても隠れてしまうことがある。
   3. **Stop**：そのセッションの応答の後で、応答が保存されていること（`[claude] …`。`[user]` のレコードと同じ `turn_id` — `--recall-thread <turn_id>` で確かめる）と、プロジェクトの `.claude/rules/n3mc-lite-behavior.md` が `<!-- n3mc-lite-behavior v1 -->` で始まることを確かめる。

8. **フックによる完全記録（Free §6 テスト 9 を手直し）**：
   - 2 文字の応答を `last_assistant_message` として `--save-claude-turn` に、5 文字のプロンプトを `--hook-submit` に渡す（フックの JSON を stdin で）：どちらも `[claude] …` と `[user] …` として保存される。
   - 決まり文句のプロンプト（`ok`、`yes`、`thanks`）は、送るたびに、やりとりごとに 1 回ずつ保存される。フックの保存は、完全一致の重複を同じ `turn_id` の中だけで断る（§3.16）。
   - フックの呼び出しはどれも、保存に失敗したときも、`audit.log` のレコードを書く。**失敗する保存は、`redis_url` が §9 のテスト用の Redis の、一時的なデータフォルダで確かめる**：1 回のフックの呼び出しの間だけ `docker pause redis-stack-test`、そのあと `docker unpause redis-stack-test`。**利用者の Redis は決して止めたり一時停止したりしない**：その永続化は切ってある（§3.4）ので、再起動するとすべての記憶が消える。
   - 長く、形のそろった応答（12 項目の設定の文、約 2,000 文字）は、1 つの親ドキュメント `[claude] …` とチャンク `[claude i/N] …` として保存され、`--search` はそれを 1 つの `[doc×N]` のヒットとして全文で返す。別のやりとりのほとんど同じ設定の文（いくつかの値を変えたもの）も保存され、両方の版が新しいものから順に表示される。

9. **完全自動の保存（Free §6 テスト 10 を手直し）**：
   1. `python -m n3mc_mcp.cli --list` で、いちばん新しいレコードを控える。
   2. Claude Code で **3 ターン以上**会話する。Claude はどの時点でも `--buffer` を呼んでは**ならない**。
   3. もう一度 `--list`：どのターンにも `[user]` と `[claude]` のレコードがあり（メッセージが前のものの繰り返しでも同じ）、ターンごとに `turn_id` が 1 つである。

10. **MCP の薄いクライアント（N3MC MCP Pro §6 テスト 20 と同様）**：`config.json` に自分の `server_port` と、`redis_url` として**テスト用**の Redis（`redis://localhost:6390/0`、§9 — 一時的な `config.json` は別の `owner_id` を持ち、本物の Redis に入ったそのレコードは、本物の持ち主の同じ文を拒んでしまう、§3.8）を書いた一時的なデータフォルダ（`N3MC_DATA_DIR`）で、`N3MC_DATA_DIR` と `N3MC_AGENT_NAME=test-client` を設定して `n3mc-workingmemory` を子プロセスとして起動し、`mcp` のクライアント（`mcp.client.stdio.stdio_client`、`ClientSession`）で stdio で話す：
    1. `initialize` と `tools/list`：§4.3 の 7 つのツールが §5 (3) の定義で並び、指示は §5 (1) の文である。子プロセスが stdout に書いたものは、すべて MCP のメッセージだった（パースのエラーなし）。
    2. 一時的なサーバーを止めた状態で `search_memory` を呼ぶ：サーバーが起動し、結果が返る。次に、サーバーが動いている間に 2 つ目の MCP の入口を起動し、`initialize` にかかった時間を記録する（判定しない。入口がモデルを読み込まないことは Layer 8 の `TestThinNoModel` が確かめる）。両方の入口が 1 つのサーバーを共用する（PID ファイル 1 つ、プロセス 1 つ）。
    3. `save_memory` で保存し、`python -m n3mc_mcp.cli --search`（同じ `N3MC_DATA_DIR`）で見つける。`--buffer` で保存し、`search_memory` で見つける。`save_memory` のレコードは `agent_name` が `test-client`（呼び出しが何も渡さないとき）で、役割の前置きはない。
    4. `list_memories`、`recall_thread`、`delete_memory`、`delete_memories_by_session`、`repair_memory` は §4.3 の文を返す（Layer 3）。
    5. サーバーを止め（`/shutdown`）、その起動をふさぎ（ポートを使用中にする）、`save_memory` を呼ぶ：レコードは §4.6 の `warning` / `detail` 付きでベクトルなしで保存される。`search_memory` は `Error: memory backend unreachable. …` を返す。入口は動き続ける。
    6. MCP の入口を閉じる：サーバーは（動いていれば）まだ動いている（`/health`）。その後 `/shutdown` で止め、一時フォルダを消す。Windows では、このテストが使う Python の `mcp` の stdio クライアントが、閉じるときにジョブで子孫のプロセスを終わらせ、サーバーも終わることがある（§3.15）。手順 5 でポートをふさいだため、サーバーの起動は失敗し、データフォルダに起動失敗の目印 `server_start_failed` が残っている。この目印がある 5 分間は、サーバーを起動し直さない（§3.15）。そこで、確かめる前に目印を消す。終わっていたら、次の CLI の呼び出しでサーバーが起動し直すことを確かめる。動いていたら、上のとおり `/shutdown` で止める。どちらでも合格である。
    このテストのために、MCP の入口を Claude Code や本物のデスクトップアプリの設定に登録しては**ならない**。

11. **コンテキストの大きさの上限（N3MC-Pro §6 テスト 19 と同様）**：`config.json` に `context_max_chars: 4000` と自分の `server_port` を書いた一時的なデータフォルダで、**テスト用**の Redis に対し、同じ話題についての 300〜400 文字のレコード 30 件（同じ文の単語をいくつかずつ変えたもの）を `--buffer` で保存する。その話題について尋ねる `prompt` を持つ `UserPromptSubmit` の JSON で `--hook-submit` を実行し、同じ質問で手の `--search` を実行し、それで MCP の `search_memory` を実行する。`--hook-submit` の stdout と `search_memory` の文は、それぞれ `context_size` の数え方（§3.17）で最大 4,000 であり、手の検索より行が少なく、`lower-ranked match(es) omitted to keep this block within the context limit` を含む。手の `--search` はすべての行を出力し、そのような行はない。次に、同じ話題の 20,000 文字の親ドキュメントを 1 つ保存し、フックの検索を繰り返す：出力はやはり収まり、その文書が最初の 3 つのヒットに入っていれば `_(cut here to keep this block within the context limit.)_` で終わる。

> 項目 1〜9 は、利用者の Redis を使う本物のインストールで行う（項目 1〜6 は Claude Code の MCP のツールで）。ただし項目 1 の Redis が止まったときの確かめと、項目 8 の失敗する保存の確認は、テスト用の Redis で行う。
>
> **自律評価は MCP で行い、フックを使う確かめ（項目 7〜9）は行わない。** 検証レポートでは項目 7〜9 を行わない。フックのコードは pytest（§9 の Layer 5）が確かめる。項目 7.1 の関門は、方法 A のセットアップの確かめ（§8）としてだけ使う。項目 10 と 11 は、一時的なデータフォルダと自分のポートを使う。

---

## 11. 保存の確実性と MCP プロトコルの限界

> **設計上の前提として明示しておく**：本サーバは MCP 経由で LLM に保存・検索を促せるが、**LLM が実際にツールを呼ぶかは MCP プロトコルでは強制できない**。これは本実装の不具合ではなく MCP の仕様レベルの限界である。

> **Claude Code は、この限界を受けない。** Claude Code では、モデルがどう決めても、フックがすべてのメッセージと応答を保存し、すべてのメッセージで検索する（§12）— この節が MCP には届かないと述べる、確実な保存である。この節は、フックのないアプリ（Claude デスクトップアプリとほかの MCP クライアント）に当てはまる。それらは §4.6 の薄いクライアントを使う。

### サーバが LLM にできる働きかけは 3 つだけ

1. **`tools/list` の各ツール `description`** — 毎ターン LLM の視野に入る
2. **`instructions` フィールド** — セッション開始時に 1 回だけクライアントに渡される
3. **`tools/call` 応答テキスト** — LLM がツールを呼んだ時に読む（§4.3 の各ツール応答末尾には auto-save を促す短い reminder を埋め込んでいる）

本仕様はこの 3 つすべてを利用するが、それでも **LLM がそれに従うかは非決定的**である。コンプライアンスは次に依存する：

- モデル自身の学習・ツール呼び出し傾向のバイアス
- MCP クライアントのプロンプト構築（`instructions` を要約・破棄するクライアントもある）
- ユーザのプロンプト、プロジェクトの `CLAUDE.md` など競合する別の指示

### 実運用で起きること

大半のターンでは正しく auto-save される。だが**短い返答・事実訂正のターン・LLM がユーザの質問に強く集中しているターン**では飛ぶ。観察可能な症状であっても自動評価は困難なので、§10 の検証レポートには含めない。「飛んだら飛んだまま」── 次セッションで失われていることに気づくまで分からない。

### 確実な保存が必要なときの 2 つの経路

LLM の自発性に依存せず保存を保証したい場合、**MCP の建付けの中ではこの 2 通りしかない**：

**経路 1：ユーザがプロンプトで明示する**（運用回避・即効）
- 「**N3MemoryCore に保存して**」「**メモリに記録して**」などをプロンプトに書く
- LLM はだいたいユーザの明示要求には応じる
- 利点: 何のインフラも要らない、すぐ効く、すべての MCP クライアントで動く
- 欠点: ユーザの認知負荷（毎回明示する必要、自動化されない）

**経路 2：first-party API（Anthropic Messages API）から自前オーケストレーション**（アーキテクチャ変更）
- MCP プロトコルを抜けて、`messages.create` の `tool_use` を自分のアプリで直接制御する
- 「LLM が呼ばなくても、コード側で毎ターン `save_memory` を確実に発火する」といった決定論的動作を組める
- 利点: コードが書いた通り動く、保存保証
- 欠点: アプリを 1 個書く労力。MCP クライアント（Claude Code 等）から外れる

要するに、**「MCP 経由で LLM に丸投げする利便性」と「保存保証」はトレードオフの両端**で、片方を取ったらもう片方は捨てる構造である。本サーバ側でできるのは「LLM に従いたいと思わせる nudge を最大限こめる」までで、それ以上の保証は仕様上ユーザ／クライアント実装側の選択になる。

---

## 12. Claude Code のフック

流れは N3MC-Free のもの（Free の仕様書の §4、§4.5、§5）で、名前は Lite のもの、保存の決まりは Lite のもの（§3.16）、検索は Lite のもの（§3.14）である。コードはこの文書だけから作るので、ここにすべて書き出す。

> **🛑 フックの登録は、必須の自動の手順である。** うまく入ったように見えるインストールの後で、セッションをまたいだ記憶が「動かない」いちばん多い理由は、実装した AI が CLI を試した後、`~/.claude/settings.json` のフックの塊を利用者に任せたことである。方法 A は `n3mc-lite --init` で終わる（§8）。セットアップを終わったと報告する前に、§10 項目 7.1 の関門が `OK` を出力しなければならない。（プラグインの利用者は、プラグインからフックを得る。）

### 12.1 フックがすること

| フック | 入口 | 手順（どの手順も同期の `subprocess.run` で、`Popen` は決して使わない） |
|---|---|---|
| `UserPromptSubmit` | `n3mc-lite-hook`（`n3mc_mcp/lite_hook.py`） | 手順 0：監査のレコードを追記する。次に、同じ stdin で `python -m n3mc_mcp.cli --hook-submit`。その stdout がフックの stdout である（Claude Code はそれをターンのコンテキストに入れる） |
| `Stop` | `n3mc-lite-stop-hook`（`n3mc_mcp/lite_stop_hook.py`） | 手順 0：監査のレコードを追記する。次に、同じ stdin で `python -m n3mc_mcp.cli --save-claude-turn`、その後、同じ stdin で（`cwd` のため、§12.4）`python -m n3mc_mcp.cli --stop` |

- ここでの `python` は、フック自身の `sys.executable` である。フックの入口が `--uvx` 付きで起動されたときは、どちらの `cli` の呼び出しにも `--uvx` を渡す（§8）。
- **同期にする理由**：`--repair` は検索の前に終わらなければならない。検索は Claude Code に制御が戻る前に出力し終えていなければならない。そうでないと Claude にそれが見えない（Free §5.2）。
- **時間の上限**：フックの入口は、`settings.json` / `hooks.json` の項目に `"timeout": 60`（秒）を付けて登録する。止まったフックは Claude Code が終わらせる。上の決まりなら、フックはふつう数秒で終わる。測っていないもの：`timeout` がないときの Claude Code 自身の既定。各手順は子プロセスのタイムアウトつきで動き、**1 つの入口の手順は合わせて `CHILD_TIMEOUT = 55` 秒を分け合う**（各手順は残りの時間を使う。最低 1 秒）。こうして Stop の入口の 2 つの手順も、登録した 60 秒の中で終わる。タイムアウトのとき、すでに出力した stdout は残す。
- **終了コード**：どちらの入口も常に exit 0 で終わる。エラーは stderr の 1 行である。失敗したフックは、Claude Code がメッセージのたびに利用者に表示する。非ゼロの `UserPromptSubmit` はプロンプトを止めることがある。文はすでに `audit.log` に無事に入っている。（Free は `--hook-submit` の終了コードで終わる。Lite はそうしない。Lite ではフックは決して失敗しないからである。）
- ターンごとのプロセスの数の合計：`n3mc-lite-hook` + 1（`--hook-submit`）、`n3mc-lite-stop-hook` + 2（`--save-claude-turn`、`--stop`）。
- §3.13 (1) の UTF-8 の再設定の塊は、どちらの入口と `cli.py` のモジュールの先頭でも、最初の `sys.stdin.read()` より前に動く。

### 12.2 `audit.log`（手順 0）

- フックの呼び出しごとに JSON の 1 行：`{"ts": <UTC ISO 8601>, "hook": "UserPromptSubmit" | "Stop", "raw": <the first 4,096 characters of stdin>, "payload": <the parsed JSON in full>}`。stdin が JSON でないときは、`payload` は `{"raw": <the whole stdin>}`。`ensure_ascii=False`、UTF-8、追記モードで、**ほかの何よりも先に**、入口自身が書く（サブプロセスではない）。
- 同じファイルのほかのレコード：`{"ts", "hook": "UserPromptSubmit", "stage": "skipped", "reason": "system_prompt"}`、`{"ts", "hook": "UserPromptSubmit", "stage": "transcript_unreadable", "reason": "<short reason>"}`、§3.15 の通知。
- 書き込みが失敗したら stderr に出し、フックは決して止めない。
- `audit.log` が 50 MB より大きければ、`--stop` がそれを `audit.log.1` に名前を変える（古いものは置き換える）。
- `audit.log` は、コードブロックや貼り付けた文も含めて、すべてのプロンプトと応答を丸ごと持つ。Redis に届かなかったメッセージの最後の控えである。これを読み戻して記憶に入れるものはない（§7）。

### 12.3 `--hook-submit`

入力：stdin の UserPromptSubmit の JSON（`session_id`、`transcript_path`、`cwd`、`permission_mode`、`hook_event_name`、`prompt`、と新しい Claude Code が足すキー。足されたキーは無視する）。`message` は `prompt` の別名として受け付ける。stdin が JSON でないときは、stdin 全体がプロンプトである。

1. **きれいにする**：パースした JSON 全体を `sanitize_surrogates`（§3.13 (2)）に通す。
2. **文**：`_extract_text(prompt)` — 文字列の `prompt` がマルチモーダルの配列になるのは、**空でない JSON の配列としてパースでき、その要素がすべて文字列の `type` キーを持つオブジェクトであるときだけ**である。そのときの文は、`type == "text"` の部分の `text` を `\n` でつないだものである。ほかの文字列は、利用者自身の文としてそのまま使う（`[1, 2, 3]` は `[1, 2, 3]` のまま）。リストの `prompt` も同じように読む。
3. **システムの入力**：文が `_SYSTEM_PROMPT_PREFIXES = ("<task-notification>", "<agent-message from=", "<system-reminder>")` のどれかで始まるときは、`skipped` の監査のレコードを書き、`# Recalled Memory Context\nSource: N3MC Lite (7-day working memory)\n\n_No memories found._\n` を出力して、やめる：修復も、保存も、検索もせず、**新しい `turn_id` も作らない**（その入力への Claude の応答は、今のやりとりに属する）。
4. **サーバー**：`ensure_server(cfg, max_wait=0)`（§3.15）。**フックはサーバーの起動を決して待たない**：答える健全なサーバーがなければ、裏で起動し（起動中のときや 5 分の待ちのときは起動しない）、すぐにサーバーなしで進む（直接の保存、代わりの検索の出力）。起動を最長 60 秒待つと（プラグインの最初の `uvx` の取得も加わる）、利用者を送信で待たせてしまう。`ensure_server` は `max_wait`（既定 60。CLI はこのまま）を受け取り、「PID ファイルの扱い」と「サーバーの起動」の手順 2 の待ちを、その秒数で打ち切る。Stop フック（`--save-claude-turn`）も同じにする。**フックはモデルも待たない**：フックの `/buffer` と `/search` の要求には `"no_model_wait": true` を付ける（§3.9、§4.5）。
5. **修復**：サーバーを使えるときは、`POST /repair` `{}`（1 回分：`ensure_index()` と、欠けているベクトル最大 200 件、§3.10）を、待ち時間 10 秒で送る。失敗や時間切れは stderr の 1 行。サーバーがなければ飛ばす。
6. **直前の応答**：`transcript_path` から Claude の直前の応答を読む（12.5）。あれば、`role = "claude"` と、`turn_id_<session>.txt` にある `turn_id`（直前のやりとり。ファイルがなければ新しい UUIDv4 を作り、そのファイルに書く）で保存する — §12.6。Stop フックがすでに保存していれば、保存は `duplicate` と答え、2 回は保存しない。
7. **新しいやりとり**：新しい `turn_id`（UUIDv4）を作り、`turn_id_<session>.txt` に書く — 画像だけのプロンプトでも同じ。
8. **検索**：文が空でなければ、`query` = 文、`session_id` = フックの `session_id`、`max_chars` = `context_max_chars` で `POST /search` を送り、応答の `text` を出力する。文が空（画像だけ）なら、`# Recalled Memory Context\nSource: N3MC Lite (7-day working memory)\n\n_No memories found._\n` を出力する。サーバーがないか、エラーのときは、12.7 の代わりの出力を出す。**検索と描画で例外が起きても、手順 9 は行う**：代わりの出力を作る途中（`notices.json` の読み書き）を含めて例外を捕まえ、`# Recalled Memory Context\nSource: N3MC Lite (7-day working memory)\n\n_(memory search unavailable: <例外の型>)_\n` を出力し、メッセージを保存する。そうしないと、メッセージが保存されない（新しい `turn_id` は手順 7 で書いてある）。
9. **メッセージを保存する**：文が空でなければ、`role = "user"` と新しい `turn_id` で保存する（§12.6）。

- このフックのレコードは、どれも `agent_name = "claude-code"` と、`session_id` = フックの `session_id` を持つ。
- 検索はメッセージを保存する**前に**行うので、メッセージが自分自身を見つけることはない。
- 画像だけのプロンプト：手順 5〜7 は動く。手順 8 と 9 は空の塊を出力し、何も保存しない。

### 12.4 `--save-claude-turn` と `--stop`

**`--save-claude-turn`** は Stop の JSON（`session_id`、`stop_hook_active`、`last_assistant_message`）を読み、`sanitize_surrogates` を当て、`last_assistant_message` が空でなければ、`role = "claude"` と `turn_id_<session>.txt` の `turn_id`（ファイルがなければ新しい UUIDv4 を作り、そのファイルに書く）で保存する — §12.6。ファイルは空にしない（次の `--hook-submit` が上書きする）。**`turn_id_<session>.txt` は、設定の読み込みとサーバーの確認（`ensure_server`）より前に読む**（ファイルがないときの新しい UUIDv4 も、このときに作って書く）。サーバーの起動や入れ替えには時間がかかることがあり、その間に次の発言の `--hook-submit` が同じファイルに新しい `turn_id` を書くと、この応答が次のやりとりの `turn_id` で保存される。**ファイルに書く理由**：フックの完全一致の重複の確認は同じ `turn_id` の中だけで働く（§3.16）。ファイルのないまま Stop が新しい UUID で応答を保存し、それを書かずにいると、次の `--hook-submit` が transcript から同じ応答を回収したとき（手順 6）に別の新しい UUID になり、同じ応答が 2 回保存される（たとえば `--init` の直後の最初の応答）。1 つのやりとりで Stop が 2 回動いたら、2 回目の保存は `duplicate` である。stdout には何も出力しない。

**`--stop`**（セッションの終わりの片付け。サーバーは止め**ない** — 止めるのは `--stop-server`、§13）：

1. `<project>/.claude/rules/n3mc-lite-behavior.md` を用意する（§12.9）。`<project>` は、環境変数 `CLAUDE_PROJECT_DIR` があり、実在するフォルダを指すときはその値（Claude Code が、自分の起動するフックのコマンドに渡す。プラグインのフックも同じ。セッションを始めたプロジェクトの根。Windows ではスラッシュで書かれる。例 `C:/Users/you/work`。フックの入口の子プロセスにも引き継がれる）。なければ `<cwd>`：Stop フックの JSON の `cwd` で、`n3mc-lite-stop-hook` が `--save-claude-turn` と同じく `--stop` にも stdin で渡す。それもなければ、プロセスの作業フォルダ。**`<project>` がホームフォルダのとき**は、この手順を飛ばす：`~/.claude/rules/` はすべてのプロジェクトに効いてしまう。**プロジェクトのフォルダにする理由**：フックの `cwd` はセッションの今の作業フォルダで、Claude が `cd` したあとはそのサブフォルダになる。そのため `cwd` だけを使うと、`--stop` はサブフォルダにも規則ファイルを作ってしまう。サブフォルダの規則ファイルは、Claude がそのフォルダのファイルを読むたびに、プロジェクトの規則ファイルに重ねて読み込まれ、この製品をアンインストールしたあとも残る。以前の版がすでにサブフォルダに作ったファイルには触れない（`--stop` は利用者のフォルダを探さない）。
2. データフォルダの `turn_id_*.txt` のうち、更新時刻が 30 日より前のファイルを消す。
3. `audit.log` が 50 MB を超えていればローテーションする。
4. exit 0 で終わり、何も出力しない。手順 1〜3 のエラーは、それぞれ stderr の 1 行である。

`turn_id_<session>.txt`：`<session>` は、フックの `session_id` の `[a-zA-Z0-9_-]` 以外の文字をすべて `_` に置き換え、64 文字で切ったものである。`session_id` がなければ `turn_id.txt`。この値はフックの JSON から来るので、そのままファイルシステムに届いてはならない。

### 12.5 transcript からの直前の応答

Free の決まりのまま。`transcript_path` はセッションの JSONL の transcript である。その最後の最大 5 MB を読み（末尾からシークする）、行を後ろから順にたどる。**行は LF だけで分ける**（`data.split(b"
")` のあと、末尾の CR を 1 つ外す）。`str.splitlines()` は使わない：U+2028、U+2029、U+0085 などの Unicode の改行でも切るためである。JSON は文字列の中のこれらの文字をエスケープしないので、それを含む応答が JSON でない行に切られ、回収できなくなる。5 MB のシークで途中から始まった最初の行と、JSON でないほかの行は飛ばす：

1. JSON のオブジェクトでない行、`isSidechain: true` の行、`type` が `user` でも `assistant` でもない行は飛ばす。
2. 本当のプロンプトでない `user` の行は飛ばす：`isMeta: true`。文字列も `type: "text"` の部分もない `message.content`（`tool_result` だけ）。`[Request interrupted by user`、`<command-`、`<local-command-`、または `_SYSTEM_PROMPT_PREFIXES` のどれかで始まる文。
3. 今のプロンプトそのものを飛ばす — **末尾の行だけ**：たどった中でいちばん新しい `user` か `assistant` の行は、その文が新しいプロンプトである本当のプロンプトなら飛ばす。それより古い本当のプロンプトの行は、文が同じでも、たどるのを終わらせる。
4. 空でない `type: "text"` の部分を持つ最初の `assistant` の行が答えを与える：その空でない文の部分の**最後のもの**（`thinking` と `tool_use` の部分は無視する）。先にほかの本当のプロンプトに着いたら、直前の応答はない。

どんな失敗（パスがない、ファイルが読めない、思わぬ構造）も「直前の応答はない」とする：`transcript_unreadable` の監査のレコードを書いて続ける。transcript は回収のためだけのもので、フックを失敗させることは決してない。**最後のテキストブロックにする理由**：Claude Code の Stop の入力は、応答の最後のテキストブロックだけを持つ。同じブロックにすれば、Stop がすでに保存したものを重複の決まりが飛ばす。

### 12.6 フックからの保存

1. **サーバーを通す**：`content`、`role`、`agent_name = "claude-code"`、`session_id`、`turn_id` を付けて `POST /buffer`（§4.5）。`saved`、`duplicate`、`skipped_code` はそれで終わりである。`error` の応答は stderr の 1 行（たとえば `empty content`）。
2. **Redis に届かない**（`/buffer` が 503 と答える）：直接保存は試さない。`redis_unreachable` を出す：`N3MC Lite could not reach Redis, so a message was kept only in <audit.log path>. ` + `backend_hint(redis_url)`（§3.9 項目 4）。
3. **サーバーがない、またはほかの失敗**（`ensure_server` の後に使えるサーバーがない、接続エラー、タイムアウト、ほかの HTTP のステータス）：§3.16 の**直接保存**（`embed=False`）。`role`、`agent_name`、`session_id`、`turn_id` は同じ。Redis に届かないので失敗したとき（`redis.ConnectionError` / `redis.TimeoutError`、または §3.9 の接続の確認が失敗する）は、手順 2 と同じく `redis_unreachable` を出す。ほかの例外：`save_failed`（`A record could not be saved (<ErrorType>: <message>). Current memories may be lost.`）を出す。
4. `saved` か `duplicate` で終わった保存は、`redis_unreachable` と `save_failed` を消す（後者を消さないと、1 回の保存の失敗で `save_failed` が 1 日 1 回ずっと出る）。
5. 送り直すためのキューは何も作らない。`audit.log` がすでに文を持っている。

### 12.7 検索の出力（`/search`、`--hook-submit`、`--search`）

サーバーが表示する（`context.py`）：

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

- 1 行目は `# Recalled Memory Context`、2 行目は `Source: N3MC Lite (7-day working memory)`（N3MC Free と Pro のフックも同じ見出しを出す。MCP の指示が探すのはこの行である、§5 (1)）、3 行目は `Search query: <query>`（検索したとおりのクエリ：`search_query_max_chars` で切り、改行は空白に置き換える）、次に空行、次に**§3.14 の `search_memory` の文そのまま** — 通知（§3.15「通知を表示する場所」）、版についての一文、行、弱い部分、または `_No memories found._` — で、リマインダは付けない。CLI は、塊の後に改行を 1 つ付けて出力する。
- `max_chars` があれば（フック）、§3.17 の上限が当てはまる。なければ（手の `--search`）、塊は完全である。
- **劣化時の代わりの出力**（CLI 自身が出力し、制限しない）：`# Recalled Memory Context` + `Source: N3MC Lite (7-day working memory)` + 空行 + 表示の時期が来た `notices.json` の通知（あれば）+ 次のどれか：
  - 使えるサーバーがない：`_(memory search unavailable: <reason>)_`。理由は §4.6 のもの（"the memory server is still starting (…)"、"the memory server could not start; see <server.log path>"）。
  - ポートにほかのフォルダのサーバーがいる：`_(memory search unavailable: another N3MC folder's server is using port <port>)_`。
  - `/search` が 503 と答えた（Redis に届かない）：`_(memory search unavailable: <hint>)_`。通知 `redis_unreachable` を出す。
  - ほかのエラー：`_(memory search unavailable: <ErrorType>: <message>)_`。
- CLI は**どの**呼び出しでも何かを出力する。そのため Claude には、記憶の状態が常に見える。

### 12.8 `--init`

`n3mc-lite --init`（pip のインストール。プラグインの利用者には要らない）：

1. データフォルダの `config.json` を読み込む（作る）（§6）。
2. `~/.claude/settings.json` を読む。**ないファイルは `{}` として読む**。手順 6 で作り、控えは取らない（控えるものがない）。要れば `~/.claude/` も作る。読めないファイル、または型の違う `hooks` / `permissions` / `permissions.allow`：何も変えず、理由を出力し、exit 1。
3. **プラグインは有効か？** `settings["enabledPlugins"]["n3mc-workingmemory@neuralnexusnote"]` が `true` なら、`The n3mc-workingmemory plugin is enabled: Claude Code also uses the MCP entry of the PyPI package (n3memorycore-mcp-lite==<version>). The hooks and the MCP tools work together when both run the same code.` を出力して続ける（プラグインが登録するのは MCP の入口で、フックではない）。
4. **フックを合わせる**：`UserPromptSubmit` と `Stop` について、自分のものでない項目はすべて残し（`n3mc` を含む残したコマンドは、それぞれ `Another N3MC hook is registered for <event> and was left in place: <command>` で知らせる）、項目 `{"hooks": [{"type": "command", "command": resolve_cmd(<entry point>), "timeout": 60}]}` を 1 つ足す。`command` が文字列でないフックの項目（null など）は、自分のものではないとして残す（知らせの対象にもしない）。
5. **許可ルール**：§8 の `PERMISSION_RULES`（`python -m n3mc_mcp.cli` と `n3mc-lite` の形）のうち欠けているものを足す。何も消さない。
6. **何かが変わったときだけ書く**：ファイルを `settings.json.bak-n3mc-lite-<YYYYmmddHHMMSS>` に写し、同じフォルダに一時ファイルを書いて、`os.replace` する。
7. **古い MCP の登録**：`~/.claude.json` か `./.mcp.json` にある `n3mc-workingmemory` の項目を、§8 のとおりに知らせる（読むだけ）。
8. `N3MC Lite hooks registered in <settings path>. Saving starts with the next Claude Code session.` を出力する（手順 3 のメッセージがあれば、その後に）。

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
    if not isinstance(command, str):                   # null or another type: not ours, keep it
        return False
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

# Characters that make bash (Git Bash on Windows) split or reinterpret an unquoted word. "~" counts only at
# the start of a path (the only place bash expands it); 8.3 short names (PROGRA~1) contain it mid-word.
SHELL_SPECIAL = frozenset(" \t'\"&|;<>()$`\\!#*?[]{}")

def _needs_quotes(s: str) -> bool:
    return s.startswith("~") or any(c in SHELL_SPECIAL for c in s)

def shell_path(p) -> str:
    s = pathlib.Path(p).as_posix()                     # forward slashes: bash reads "\" as an escape
    if _needs_quotes(s) and os.name == "nt":           # a command that starts with a quote
        short = _short_path(p)                         #       does not run as a hook on Windows:
        if short and not _needs_quotes(pathlib.Path(short).as_posix()):
            s = pathlib.Path(short).as_posix()         #       use the 8.3 short name when it needs no quotes
    if not _needs_quotes(s):
        return s
    for ch in ("\\", '"', "$", "`"):                   # still special inside double quotes
        s = s.replace(ch, "\\" + ch)
    return f'"{s}"'                                    # an unquoted ' or & is a bash syntax error

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

- `lite_hook.py` と `lite_stop_hook.py` は `main()` を持ち、`python -m` でも動く（フォールバックの形）。
- **この正確な照合にする理由**（Free と同じ）：フックの名前を*含む*だけのコマンドをすべて当てると、ほかの N3MC 製品のフックを消してしまう。1 つの形だけを当てると、重複が残る。Lite の名前（`n3mc-lite-hook`、`n3mc_mcp.lite_hook`）は、Free のもの（`n3mc-hook`、`n3memorycore.n3mc_hook`）とも Pro のものとも違うので、どちらの `--init` も相手の項目を消さない（§14）。
- **scripts フォルダを先に見て、引用符を付けない理由**（Free と同じ）：Python を入れた直後は、Claude Code の `PATH` に scripts フォルダがない。また、引用符で始まるフックのコマンドは、Windows で動かないことがある。空白や、`SHELL_SPECIAL` のほかの文字を含むパスには、それでも引用符が要る。引用符がないと、`'` や `&`（`C:/Users/O'Brien/...`）は bash の構文エラーになり、フックが毎回失敗する。そこで Windows では、`shell_path` がファイルの 8.3 形式の短い名前（`GetShortPathNameW`）を、それが引用符なしで済むときに使う。そうでなければパスを二重引用符で囲み、その中の `\`、`"`、`$`、`` ` `` をエスケープする（引用符の中でも特別な意味を持つ）。このとき `--init` は `The hook command for <event> starts with a quote because <path> contains a space or another character the shell treats specially; if the hook does not run, install Python into a folder without such characters.` を出す。Linux のパスも同じように引用符で囲む（そこで失敗するとは分かっていない）。`is_own_hook` は引用符で囲んだ形を `shlex` で読むので、`--init` をもう一度実行しても自分のものと分かり、2 つ目を足さない。

### 12.9 ルールファイル `.claude/rules/n3mc-lite-behavior.md`

Claude Code は、セッションの始まりにプロジェクトの `.claude/rules/*.md` を読み込む。`--stop` は `<project>/.claude/rules/n3mc-lite-behavior.md` を最新に保つ（`_ensure_rules_md`。`<project>` は §12.4 の手順 1 のプロジェクトのフォルダで、ホームフォルダにはしない）。Free は `n3mc-behavior.md` を書く。両方の製品を入れられるように、名前を変えてある（§14）。

**テンプレート**（`_RULES_MD_TEMPLATE`。この文そのままで、最後の行の後に改行を 1 つ付ける。言い換えない）。`{cli}` は、このインストールのコマンドの形に置き換える：`<python> -m n3mc_mcp.cli`（pip。`<python>` は `--stop` を動かすプロセスの `shell_path(sys.executable)` — パッケージを持つ Python の絶対パスを、スラッシュで、`shell_path` が引用符を要るとしたときだけ引用符で囲んだもの、§12.8）、または `--stop` が `--uvx` 付きで呼ばれたときは `uvx --from n3memorycore-mcp-lite==<version> n3mc-lite`（プラグイン）。（パッケージを venv に入れたとき、ただの `python` は別の Python である。）

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

**ファイルの扱い**：

| すでにあるファイル | 振る舞い |
|---|---|
| ない | 置き換えたテンプレートを書く（必要なら `.claude/rules/` を作る） |
| 内容が、このインストール用に置き換えたテンプレートと同じ | 何もしない。このパスの通知 `rules_md_edited:<hash>` を消す |
| **コマンドの形を取り除いた**内容（下記）が、今のテンプレートか前のテンプレート（`_KNOWN_RULES_MD_HASHES`）と同じ | バックアップし（`n3mc-lite-behavior.md.bak-n3mc-lite-<YYYYmmddHHMMSS>`）、置き換えたテンプレートを書く。Python のパスが違うとき、もう一方のインストールの形のとき、固定した版が古いときも、これに当たる |
| それ以外（利用者が直したもの） | **置き換えない。** `rules_md_edited:<hash>` を出す：`The rules file <absolute path> has been edited by hand, so it was not updated. Merge the new template manually if needed.` `<hash>` = ファイルの絶対パスの SHA-256 の、先頭 12 桁の 16 進数 |

- 内容は改行を LF にそろえて比べる。**コマンドの形を取り除く**：``(?:"[^"\n]*"|[^\s`]+) -m n3mc_mcp\.cli`` と `uvx --from n3memorycore-mcp-lite==\S+ n3mc-lite` に当たる部分をすべて `{cli}` に置き換える。その結果を `_RULES_MD_TEMPLATE` そのものと比べ、前のテンプレートとは、SHA-256 の先頭 16 桁の 16 進数を `_KNOWN_RULES_MD_HASHES` と比べる。v1 は最初のテンプレートなので、この集合は空から始まる。`uvx` の形の固定した版も取り除くので、新しいリリースのたびに、プラグインの利用者のファイルが直したもののように見えることはない。
- **テンプレートを変えるときは、いつも目印のバージョンを上げ、前の（置き換える前の）テンプレートのハッシュを `_KNOWN_RULES_MD_HASHES` に足す。**
- 同じフォルダの一時ファイルを通して書き、`os.replace` する。
- **コマンドの形を埋める理由**：プラグインのインストールには、`python -m` が見つけられるパッケージがない（コードは `uvx` の環境にある）。pip のインストールには、`uvx` を起動する理由がない。そのファイルを書いた形が、そのプロジェクトで動く形である。

---

## 13. CLI `n3mc-lite`

`n3mc-lite`（`n3mc_mcp.cli:main`。`python -m n3mc_mcp.cli` でも）は、Lite の機能を持つ Free の CLI である。Claude Code では、MCP の入口を使わないときも、これがツールと同じことをする：ツールのコマンドはどれも、常駐サーバーのツールのハンドラを呼び（`POST /tools/<name>?nudge=0`）、応答の文をそのまま、改行を 1 つ付けて出力する。1 回の呼び出しで、コマンドはちょうど 1 つ（argparse の、互いに排他的なグループ）。文字列の引数はすべて、どのコマンドが使うよりも前に `sanitize_surrogates`（§3.13）を通す。`--search` も同じ。

| コマンド | 呼ぶもの | 出力 |
|---|---|---|
| `--search "<keywords>" [--since S] [--until U] [--limit N]` | `query`、`since`、`until`、`limit` と、設定されていればこのプロセスの `N3MC_SESSION_ID` を `session_id` として付けた `POST /search`。`max_chars` なし | §12.7 の塊（制限なし） |
| `--buffer "<text>"` または `--buffer -`（stdin） `[--agent-name NAME] [--session-id S] [--turn-id T] [--importance X]` | `content`、`agent_name`（既定 `claude-code`）、`session_id`、`turn_id`、`importance`（それぞれ与えられたときだけ）を付けた `/tools/save_memory` | §4.3 の `save_memory` の JSON |
| `--list [--limit N]` | `/tools/list_memories` | `list_memories` の文 |
| `--recall-thread <turn_id> [--before N] [--after N] [--session-id S]` | `/tools/recall_thread`（`--session-id` は `session_id` として送る） | `recall_thread` の文 |
| `--delete <id>` | `/tools/delete_memory` | `delete_memory` の JSON |
| `--delete-session <session_id>` | `/tools/delete_memories_by_session` | §4.3 の JSON |
| `--repair` | `/tools/repair_memory`（1 回の呼び出し：最大 200 のベクトル。`remaining` > 0 の間はもう一度実行する） | `repair_memory` の JSON |
| `--stop-server` | 何も起動せずに `ensure_server` の同一性の確認を行う：`server_port` の `/health` が自分の `root` を返したら、それを付けて `POST /shutdown` を送り、ポートが空くまで待つ（§3.15） | `Stopped the N3MC Lite server (pid <pid>).`、または `No N3MC Lite server of this data folder is running.`（ほかのフォルダのサーバーがポートを使っているときも。それは決して止めない）。exit 0。`Stopped …` を出すのは、15 秒の待ちの中でポートが空いたときだけ。**停止を確かめられないとき**（停止の要求が失敗してポートがまだ使われている、または待ちがポートの使用中のまま終わった）は、stderr に `Could not stop the N3MC Lite server (pid <pid>): <reason>. End the process whose command line contains n3mc_mcp.cli and --run-server.` を出し、exit 1（失敗したのに成功を表示すると、アンインストールの手順がサーバーの動いたまま進んでしまう） |
| `--hook-submit`、`--save-claude-turn`、`--stop` | §12 | §12 |
| `--init` | §12.8 | §12.8 |
| `--run-server` | このプロセスで常駐サーバーを動かす（§3.15）：`config.json` を読み込み、`127.0.0.1` 以外の `bind_host` を警告し、§3.9 の手順 1〜3 とライブラリの import を行い、モデルのロードを始める lifespan 付きで、`bind_host:server_port` で uvicorn を動かす。終わるとき、PID ファイルが自分の PID を持っていれば消す | stdout には何も出さない |
| `--uvx` | `--hook-submit`、`--save-claude-turn`、`--stop` の修飾：このインストールは `uvx` で動く（§8、§12.9） | — |

- `--search`、`--list`、`--recall-thread`：使えるサーバーがないとき、`--search` は §12.7 の代わりの出力を出し、ツールのコマンドは §4.6 の表の文をリマインダなしで出力する。サーバーのない `--buffer` は直接保存し（§3.16、`role` なし）、§4.6 の `save_memory` の JSON をリマインダなしで出力する。
- **時刻の範囲**：`--since` / `--until` は、リクエストの前に §4.3.1 の決まりで読む。読めない値、または `since` が `until` より後なら、§4.3.1 の `Error: …` の文を stderr に出し、終了コード 2 で終わる。
- **終了コード**：0。ただし引数のエラーは 2、読めない `settings.json` での `--init` は 1、停止を確かめられない `--stop-server` は 1。ツール自身のエラーの文（`Error: …`、`"status": "error"`）は、ツールが返したとおり、終了コード 0 で出力する。
- **PID ファイル**：サーバーを起動するプロセス（`_start_server`）は、`Popen` の前に `open(path, "x")` で PID ファイルを作り、子プロセスの PID を書く。排他的な作成が失敗したら、ほかのプロセスがサーバーを起動している：「PID ファイルの扱い」（§3.15）に従う。**サーバーは自分の PID を書く**：サーバーは、自分の `os.getpid()` を PID ファイルに 2 回書く（`_write_own_pid`：ファイルがなければ排他的に作り、あれば上書きする。`OSError` は無視する）。1 回目は `_run_server` の始め（Redis とモデルより前）、2 回目は `uvicorn.run` の直前である。起動した側は `Popen` が返った直後に `Popen.pid` を書くので、最後に書くのがサーバーになるようにする。Free も同じことを 1 回行う。理由：Windows では venv の `python.exe`（標準の venv も uv の venv も。したがって `uvx` も）はランチャーで、本体の Python を子として起動する。そのため `Popen.pid` はランチャーの番号になる。ランチャーはどのポートでも待ち受けないので、その番号のままだと、`server_port` を変えたあと §3.15 の別ポートの確認がポートを見つけられず、古いサーバーが止まらず、CLI は呼び出しのたびに 60 秒待つ。`remove_pid_file_if_own()` の比較も一致しない。**空か読めない PID ファイル**（作ってから書くまでの間）は、ファイルの更新時刻から 60 秒の間「起動中」として扱い、死んだプロセスとは扱わない。消さず、2 つ目のサーバーも起動しない。
- **Claude が聞かずに動かしてよいコマンド**：§8「自動許可」。`--delete`、`--delete-session`、`--init` は、いつも利用者の許可が要る。
- 普通の出力に「run n3mc-lite --search …」のような案内を出さない。どのコマンドがあるかは、ルールファイルが Claude に伝える。

---

## 14. ほかの N3MC 製品との共存

Lite は、N3MC-Free、N3MC-Pro、MCP の Pro / PostgreSQL / Ollama 版と同じ PC に入れられる。共有するものはない：

| | N3MC-Free | N3MC-Pro | MCP-Lite（この製品） |
|---|---|---|---|
| データフォルダ | `~/.n3mc`（`N3MC_HOME`） | `~/.n3mc-pro`（`N3MC_PRO_HOME`） | `~/.n3mc-workingmemory`（`N3MC_DATA_DIR`） |
| データベース | データフォルダの SQLite | データフォルダの SQLite | Redis Stack（`redis_url`） |
| サーバーのポート | 18520 | 18521 | **18524**（PostgreSQL 版 18522、Ollama 版 18523） |
| フックのコマンド | `n3mc-hook`、`n3mc-stop-hook` | `n3mc-pro-hook`、`n3mc-pro-stop-hook` | `n3mc-lite-hook`、`n3mc-lite-stop-hook` |
| CLI | `n3mc` / `python -m n3memorycore.n3memory` | `n3mc-pro` / `python -m n3memorycore_pro.…` | `n3mc-lite` / `python -m n3mc_mcp.cli` |
| ルールファイル | `.claude/rules/n3mc-behavior.md` | `.claude/rules/n3mc-pro-behavior.md` | `.claude/rules/n3mc-lite-behavior.md` |
| MCP のサーバー名 | — | `n3mc-pro`（N3MC MCP Pro。PostgreSQL 版は `n3mc-pg`） | `n3mc-workingmemory` |

- **`--init` はほかの製品のフックを残し**（`is_own_hook`、§12.8）、それを知らせる。ほかの製品の `--init` も Lite のものを残す（それらの照合は `n3mc-lite-hook` にも `n3mc_mcp.lite_hook` にも当たらない）。
- **サーバーの識別**：どのサーバーも、自分のデータフォルダを `root` として知らせる。自分のポートにほかの製品のサーバーを見つけた CLI は、それを決して使わず、止めもしない（§3.15「ポートがほかのフォルダのサーバーのものだったとき」）。
- **2 つの製品のフックを一緒に**：Claude は毎ターン、それぞれの製品から、それぞれの記憶の `# Recalled Memory Context` の塊を 1 つずつ得る。それぞれのルールファイルは、自分のコマンドを示す。これは利用者の選択であり、どちらの製品も相手を消さない。
- **1 台の PC にプラグインと pip**：プラグインは MCP の入口を、`n3mc-lite --init` はフックを登録する。同じ版を動かす限り、両方一緒に動く（プラグインは `uvx` の版をパッケージの版に固定する、§2）。生成したインストールは `code_version` が違うので、プラグインが有効な間、方法 A は止める（§8）。
- **MCP の共存**：指示のルール 0（§5 (1)）：同じアプリに永続の N3MC の記憶が MCP でつながっているとき — `n3mc-pro`（N3MC MCP Pro）、`n3mc-pg`（その PostgreSQL 版）、`n3mc-longtermmemory`（以前の MCP Pro）— は、Lite はワーキングメモリである。

---

## 付録 A：オプション拡張（本 Lite 版には含めない）

Lite 版は §3.6 に記載したハイブリッド + 時間減衰ランカーで意図的に止めている。以下の拡張は **出荷仕様には含まれない** — 将来ユーザーや AI が「試してみたい」となった時に迷わないよう、拡張余地の見取り図として記す。いずれも Lite 版が正しく動作するために必須ではなく、各々が「精度 vs レイテンシ」のトレードである。

- **クロスエンコーダ・リランカー** — `hybrid_search` が返した上位 N 候補を、小型のクロスエンコーダ（例: `cross-encoder/ms-marco-MiniLM-L-12-v2`、~130 MB / `BAAI/bge-reranker-base`、~278 MB）で再ランキングする。現代的なノート PC で `search_memory` 1 回あたり **+100〜300 ms の CPU レイテンシ**（上位 50 件リランク）を加え、言い換えの多いクエリで概ね **精度 +1 ポイント** を得る見立て。差し込み位置は `processor.hybrid_search` の融合スコアソート後、`min_score` フィルタの前。リランカー無効時には従来スコアを素通しできるよう既定フォールバックを残すこと。なお本版には軽量な語彙リランカーが既にデフォルト有効で組み込まれており（`lexical_rerank_enabled` [§6](#6-設定)）、クロスエンコーダはその上位互換として差し替える形になる。
- **HyDE（Hypothetical Document Embeddings）** — ユーザーのクエリを埋め込む前に、小型 LLM で「仮の回答」を合成し、その回答をクエリの代わりに（または併用で）埋め込む。クエリが短く曖昧で、記憶側が長く具体的な場合に効く。検索ごとに LLM ホップが入るため、「外部 API コール無し」を謳う Lite 版とは相性が悪い — ローカル LLM が既に利用可能な場合に限り選択肢となる。
- **日本語形態素解析** — 本版はすでに [§3.7](#37-トークナイズと句読点処理) の通り CJK バイグラム展開で日本語・中国語 BM25 を補強している。さらに精度を求めるなら保存時に形態素解析器で本文（`content`）を事前分割する — 候補は `fugashi` + `unidic-lite`（MeCab ベース、~50 MB）、`SudachiPy` + `sudachidict-core`（~70 MB、A/B/C 三段の分割粒度）、バイナリ依存を避けたいなら純 Python の `Janome` — 表層形をスペース結合したものを並列 `text_tokens` TEXT フィールドに格納し、BM25 検索はこのフィールドを参照する。ベクトル検索側は影響なし（e5 埋め込みモデルは日本語を直接扱える）、表示用の本文（`content`）はそのまま保持する。見込みコストは `save_memory` 1 回あたり +5〜20 ms、バイグラム展開との差分は語境界の厳密性で、複合語・派生形の識別で差が出る。

3 案はいずれも加算的で、Redis スキーマ既存フィールドや TTL / 重複判定契約の変更を要求しない（日本語トークナイザは並列フィールドを**追加**するのみ）。将来の実装者は機能フラグとして独立に扱い（既定 OFF）、各々ベースラインランカーに対して単独でベンチマークすべきである。

---

## 付録 B：推奨 AI 支援ワークフロー

> **生成は本体のセッションで行い、サブエージェントに任せない。** この指示書を実装する AI は、コード・テストとそれらの変更を、利用者が見ているセッションで自分で書く。実装、作り直し、直し、テストを、サブエージェント（バックグラウンドのエージェント、Task / Agent ツール）に任せてはならない。サブエージェントは渡された説明しか知らず、この指示書の全体と利用者の意図を知らない。利用者に聞くこともできず、正しくても誤っていても同じ調子で報告を返す。サブエージェントは読むだけの作業（探す、比べる、レビューする）にだけ使い、その結果は、何かを変える前に本体のセッションで確かめる。

> **この付録は人間向けの操作ガイドです。** 各フェーズで ``` 内のプロンプトをコピーして Claude Code に貼り付けてください。AI が自動で次のフェーズに進むことはありません。方法 A は、Claude Code に**フック**と **MCP の入口**の両方を登録する（§8）。MCP のツールは、再起動したあとの Claude Code の中（§10 項目 1〜6）と、テスト用のフォルダで薄いクライアントを通して（§10 項目 10）試す。

| フェーズ | あなたがやること | 使うモデル |
|---|---|---|
| 1. 実装 | プロンプトを貼って実装を依頼 | **Sonnet**（高速） |
| 2. デバッグ | 3 つのプロンプトを**順番に**貼って検証 | **Sonnet** |
| 3. 自律評価 | **Claude Code を再起動**してからプロンプトを貼って §10 の Evidence Report 実行を依頼 | **Sonnet** または **Opus** |
| 4. 品質レビュー | **新しいセッションを始めてから**、プロンプトを貼って評価・改善を依頼 | **Opus**（深い推論） |

> **⚠️ フェーズ 3 の前に Claude Code の再起動が必須**
>
> フックとルールファイルは、Claude Code が起動時に読み込む。`n3mc-lite --init` を実行する前に始まったセッションには、フックがない。常駐サーバーはコードが変わると自分で置き換わる（`code_version`、§3.15）ので、手で再起動する必要はない。§10-1 はサーバー起動の後の最初の検索を計測するので、新しく起動した後がいちばん観察しやすい。
>
> 再起動前のチェックリスト：
> 1. `pip install -e .`（または再インストール）で最新コードがインポートパスに載っているか確認
> 2. フックが登録済みか確認：§10 項目 7.1 の関門が `OK` を出力する（[§8](#8-クライアント設定)、§12 参照）
> 3. `~/.claude/settings.json` に CLI の許可ルール（`Bash(python -m n3mc_mcp.cli --search *)` …。[§8 自動許可](#自動許可claude-code-固有) 参照）があり、`claude mcp get n3mc-workingmemory` が `Connected` を示すか確認
> 4. Redis Stack が起動していること（`docker ps` で確認、無ければ `docker start redis-stack` または `docker run -d --name redis-stack -p 127.0.0.1:6379:6379 redis/redis-stack-server:latest`）
> 5. Claude Code を**完全に終了 → 再起動**（**Windows**：右上 × ボタンや `/exit` だけでは残ることがあるため、**タスクマネージャ**で `Claude` / `claude` 関連プロセスをすべて終了させる。サーバーの新しい起動を計測するときは、コマンドラインに `n3mc_mcp.cli --run-server` を含む `python.exe` も終了させる）
> 6. サーバー起動の後の最初の検索は、multilingual-e5-base（~1.1 GB）の HF キャッシュ未生成なら 2〜10 分。その間、フックは「まだ起動中」の代わりの出力か、通知付きのキーワードだけの結果を出力する（§3.9）

---

### フェーズ 1：実装（Sonnet）

モデルを **Sonnet** に設定し、以下を貼り付けてください。

```
この指示書に従って n3mcmcp-lite を実装してください。
```

Sonnet がコード生成・フック設定・サーバー起動まで自動で行います。完了したらフェーズ 2 に進んでください（「完了した」≠「仕様通り」なので、ここで終わりにしないでください）。

> **⚠️ フェーズ 1 → フェーズ 2 の間に Claude Code を完全再起動してください**
>
> フェーズ 1 で Claude Code が起動した時点ではフックはまだ存在しないため、このセッションは何も保存せず、`# Recalled Memory Context` も得ません。フェーズ 2 のデバッグプロンプトを貼る前に、**Claude Code を完全終了して再起動**してください — そうすると `n3mc-lite --init` が登録したフックとルールファイルが効くようになります。
>
> **Windows で完全終了する手順**：右上 × ボタンや `/exit` だけでは、アプリが裏で動き続けていることがあります。
>
> 1. 開いている Claude Code をすべて閉じます（デスクトップアプリ、ターミナル、VS Code などの Claude Code の画面）。
> 2. タスクバーの右端（時計の近く）の「^」を開き、Claude のアイコンがあれば右クリックして終了します。
> 3. よくわからないときは、Windows から一度サインアウトして、サインインし直します。スタートメニュー → 左下の自分のアカウントのアイコン → **サインアウト**。PC の再起動でもかまいません。
> 4. Claude Code を開き直します。
>
> 再起動後、メッセージを 1 つ送り、それが保存されたことを確認してください（`python -m n3mc_mcp.cli --list` がその `[user]` のレコードを表示する）。

---

### フェーズ 2：デバッグ（Sonnet）

引き続き **Sonnet** で、以下の 3 つのプロンプトを**順番に 1 つずつ**貼り付けてください。

**① データフロートレース**（データが途中で失われていないか確認）
```
n3mcmcp-lite について：
コードを読んで、save_memory ツール呼び出し（MCP の薄いクライアントと
/tools/save_memory を通るもの）と、フックの保存（--hook-submit / --save-claude-turn と
/buffer、およびサーバーなしの直接保存）から Redis パイプライン EXECUTE まで、
および search_memory 呼び出しとフックの検索から出力までのエンドツーエンドの
データフローをトレースしてください。TTL が全書き込みで設定されているか、§3.16 の
役割の前置きが親ドキュメントとすべてのチャンクに付いているか、親ドキュメント
（§3.11）の fallback が途中で失われていないか確認し、必要に応じて修正してください。
```

**② 仕様との逐条比較**（指示書に書いてあるのに未実装の動作がないか確認）
```
n3mcmcp-lite について：
指示書 §4.3 の各 MCP ツールの入力スキーマ・振る舞いを 1 ツールずつ、続けて §4.5 の
エンドポイント、§4.6 の薄いクライアント、§12 のフック、§13 の CLI を、実際のコードと
比較してください。§3.11 の親-チャンク契約（verbatim 復元・親→チャンクカスケード削除・
[doc×N] 表示）も逐条で確認してください。
ドキュメントに記載されているが実装されていない動作を探し、必要に応じて修正してください。
```

**③ クロスセッションテスト**（サーバーを再起動してもデータが残るか実行確認）
```
n3mcmcp-lite について：
この Claude Code のセッションの MCP のツール（n3mc-workingmemory）を使ってください。
ツールがなければ止めて、Claude Code の再起動を私に頼んでください。
save_memory で 1 件保存し、search_memory で見つかることを確かめてください。
常駐サーバーを python -m n3mc_mcp.cli --stop-server で止め（Redis は止めない）、
もう一度 search_memory を呼んでください：サーバーがもう一度起動し、レコードが見つかること。
一時的なデータフォルダで、MCP の薄いクライアントを通した save_memory でも
同じことをしてください（§10 項目 10）。必要に応じて修正してください。
```

3 つとも完了したらフェーズ 3 に進んでください。

---

### フェーズ 3：自律評価（Evidence Report）

> **先に Claude Code を完全終了 → 再起動してから以下を実行**（上記 "⚠️ フェーズ 3 の前に Claude Code の再起動が必須" 参照）。再起動せずに実行すると、セッションに MCP のツールがなく（MCP の登録は起動のときに読まれる）、変更したインストールのフックも効いていないことがあるので、項目 1〜6 を MCP で行えず、§10-1 のサーバー起動の後の初回の計測もできません。

モデルは **Sonnet** または **Opus**（自律評価項目の検証は Sonnet で十分。Opus に渡せばよりシビアな採点になりやすい）。再起動後に以下を貼り付けてください。

```
n3mcmcp-lite について：
指示書 §10 の自律評価（Evidence Report）を実行してください。
項目 1〜6 にはこの Claude Code のセッションの MCP のツール（n3mc-workingmemory）を使い、
ツールがなければ止めて、Claude Code の再起動を私に頼んでください。項目 7〜9（フックを
使う確かめ）は行わないでください。項目 10 には MCP クライアントのテストを使い、
項目ごとに使った道（MCP か CLI か）を書いて、§10 の項目 1〜6、10、11 を順に検証し、各項目を
⭐⭐⭐⭐⭐ で採点した Evidence Report を生成してください。

特に以下を明示的に記録してください：
- §10-1: サーバー起動の後の初回の検索と、定常の検索（5 回計測の中央値）の実測値、Redis 停止時のヒント返却（クラッシュなし）
- §10-3: 400 字超テキストの一字一句復元（親ドキュメント verbatim 契約と [doc×N] タグ）
- §10-4: 括弧付き保存テキストを無括弧クエリで上位 3 件以内ヒット（CJK バイグラム）
- §10-6: 短／中／長（chunk_threshold 超）の混合投入を 1 つの session_id の下で → delete_memories_by_session
         → 投入数と deleted 合計の一致、他 session の無干渉、2 回目 not_found
- §10-10: 一時フォルダでの MCP の薄いクライアント（モデルなし、同じ文）
- §10-11: context_max_chars 4000 でのコンテキストの大きさの上限

失敗した項目があれば原因を特定し、必要に応じて修正後に再実行してください。
すべての項目が ⭐⭐⭐⭐⭐ を達成したらフェーズ 4 に進んでください。
```

Evidence Report が緑になったら、フェーズ 4（品質レビュー）に進みます。

---

### フェーズ 4：品質レビュー（Opus）

> **⚠️ レビューは新しいセッションで行う**: 実装とデバッグを行ったセッションでレビューしないでください。Claude Code を終了し、同じフォルダで起動し直して、モデルを **Opus** にした新しいセッションで行ってください。
> - 実装したセッションのコンテキストには、そのセッション自身の判断や説明が残っています。そこでレビューすると、コードと指示書を改めて突き合わせるより、それまでの判断を追認しがちになります。
> - 常駐サーバーはコードが変わると自分で置き換わります（`code_version`）。そのため、利用者が使うときと同じ状態でサーバーをレビューできます。
> - 新しいセッションでは、記憶の検索が実装中に保存したメモを返すことがあります。これは想定どおりです。レビューは、コード、この指示書、実際に実行したコマンドの結果にもとづいて行ってください。

モデルを **Opus** に切り替え、以下を貼り付けてください。

```
n3mcmcp-lite をレビューしてください。必要に応じて修正してください。

n3mcmcp-lite は記憶装置（Claude Code にはフック、ほかのアプリには MCP）として、また RAG として 10 点満点で何点ですか？
記憶装置としての評価（保存・7 日 TTL・重複排除・親ドキュメント verbatim 契約）と
RAG としての評価（ハイブリッド検索・CJK バイグラム・軽量リランカー・時間減衰・
自動 importance）を分けて、スコアカードを生成してください。
```

Opus が実際に CLI のコマンド（とテスト用フォルダでの MCP の薄いクライアント）を動かし、**記憶装置**（保存・TTL・重複排除・親ドキュメント）と **RAG**（検索精度・ランキング・ノイズ耐性）の 2 軸でスコアカードを生成します。

> **注意**：MCP Lite 版は軽量リランカーと CJK バイグラムを標準搭載しているため Free 版より RAG 側の天井は少し高いですが、それでも以下が未実装で **RAG 側は 8 点を超えることはまずありません：**
> - 形態素解析（MeCab / SudachiPy 等）による語境界の厳密化（付録 A）
> - クロスエンコーダによる本格的リランキング（付録 A）
> - 言語特化 embedding モデル（`multilingual-e5-large` 等）への切り替え
> - HyDE などクエリ拡張技法（付録 A）
>
> 甘い採点は改善機会を失わせるため、厳格に評価してください。**スコアに関わらず、Opus に何が足りないかを具体的に指摘してもらい、改善点を必ず相談してください。**

---
