# N3MemoryCore MCP — Lite（揮発型）

**7 日たつと自分で忘れる、Claude の作業メモリ。**

Claude Code では、フックがすべてのメッセージと応答を保存し、関係のあるものを毎ターン呼び出します。ツールの呼び出しは要りません。
Claude デスクトップアプリと、ほかの MCP 対応アプリは、MCP で同じ記憶を使います。
記憶は、あなたが動かす Redis Stack に置きます。各エントリは、保存から 7 日で消えます。

```bash
docker run -d --name redis-stack -p 127.0.0.1:6379:6379 redis/redis-stack-server:latest
git clone https://github.com/NeuralNexusNote/n3mcmcp-lite.git
pip install -e ./n3mcmcp-lite
n3mc-lite --init
```

そのあと、Claude Code を再起動します。次のセッションから記憶が働きます。
手順の詳細、プラグインの経路、Claude デスクトップアプリの設定は、[クイックスタート](#step-1--redis-stack-を起動) にあります。

> ⚠️ **v2.0.0 は、まだ PyPI にありません。**
> この README は v2.0.0 を説明しています。v2.0.0 は、このリポジトリのコードです。指示書
> （[`N3MemoryCore_MCP_Spec_JP.md`](./N3MemoryCore_MCP_Spec_JP.md)）から生成しました。
> 上のコマンドがリポジトリから入れているのは、そのためです（clone したフォルダは、その後も使われます。消したり動かしたりすると、記憶が止まります）。
> PyPI のパッケージと、Claude Code のマーケットプレイスのプラグインは、まだ **v1.12.2** です。
> v1.12.2 は MCP サーバーだけです。Claude Code では、何を保存するかをモデルが決めます。
> v1.12.2 の設定は、PyPI で v1.12.2 と一緒に公開されている README のとおりです
> （<https://pypi.org/project/n3memorycore-mcp-lite/1.12.2/>）。
> v2.0.0 のリリースまでは、`pip install n3memorycore-mcp-lite` で v1.12.2 が入ります。
> 既存の利用者にとって何が変わるかは、[v2.0.0 で変わったこと](#v200-で変わったこと) にあります。

> NeuralNexusNote™ プロダクト — **無償 Lite** 版。
> Redis Stack を使う、揮発性のハイブリッド（ベクトル + BM25）メモリです。
> 各エントリは 7 日で消えます。
>
> 🇺🇸 **[English README](./README.md)**
> 🛡️ **[開発ポリシー](./PHILOSOPHY.md)**

**読む場所の案内**

| あなたは | 読む場所 |
|---|---|
| 初めて使う | [クイックスタート](#step-1--redis-stack-を起動)、そのあと [実際に Claude に届くもの](#実際に-claude-に届くもの) |
| v1.x から来た | [v2.0.0 で変わったこと](#v200-で変わったこと) |
| 無償の 2 つで迷っている | [Lite と Free、どちらを使うか](#lite-と-freeどちらを使うか) |
| データの置き場所を知りたい | [記憶はあなたが管理するマシンに残ります](#記憶はあなたが管理するマシンに残ります)、[データ保存先](#データ保存先) |
| 設定を変えたい | [設定](#設定)、[ランキング式](#ランキング式) |
| 困っている | [トラブルシューティング](#トラブルシューティング)、[制限](#制限) |

## 概要

`n3memorycore-mcp-lite` は、Claude に、会話をまたぐ短期の記憶を与えます。
文章は、利用者が動かす Redis Stack に保存します。
索引は 2 つです。BM25 の全文索引と、768 次元のベクトル索引
（[`intfloat/multilingual-e5-base`](https://huggingface.co/intfloat/multilingual-e5-base)）です。
検索の結果は、両方を合わせた順位で返します。
各エントリは、保存から 7 日で Redis が消します。

v2.0.0 からの MCP-Lite は「N3MC-Free + MCP で、データベースは Redis」です。
中身は次のとおりです。
Lite v1.12.2 の検索と保存の決まりに、[N3MC Free](https://github.com/NeuralNexusNote/n3mc-free) のフック・常駐サーバー・CLI を足しました。
さらに、フックのないアプリのために、薄い MCP の入口を足しました。
検索と保存の決まりは、Free と Lite で別々のままです。
詳しくは [MODIFICATIONS.md](./MODIFICATIONS.md) を見てください。

```
Claude Code ──hooks / CLI (HTTP)─────────────────────────┐
                                                         ├─▶ resident server (127.0.0.1:18524, model loaded once) ─▶ Redis Stack
Claude desktop app ─(MCP, stdio)─▶ n3mc-workingmemory ───┘
                                   (thin client, no model)
```

| アプリ | つながる経路 | 登録するもの |
|---|---|---|
| Claude Code | **フックだけ**（`UserPromptSubmit`、`Stop`）と CLI `n3mc-lite` | プラグイン、または `n3mc-lite --init` |
| Claude デスクトップアプリと、フックのないほかのアプリ | **MCP だけ**（薄いクライアント `n3mc-workingmemory`） | 利用者が、そのアプリの設定ファイルに書く |

- **Claude Code に MCP サーバーを登録しないでください。** Claude Code は、フックと CLI から記憶を使います。
- どちらの経路も、**同じ常駐サーバーと同じ Redis** に届きます。
  Claude Code で話したことは、デスクトップアプリから見つかります。
  デスクトップアプリが保存したことは、Claude Code で見つかります。

## Lite と Free、どちらを使うか

無償の N3MemoryCore は 2 つあります。
**Claude Code だけを使い、残る記憶がほしい方には、[N3MC Free](https://github.com/NeuralNexusNote/n3mc-free) のほうが簡単です。Docker が要りません。**

| | **N3MC MCP Lite**（このリポジトリ） | **[N3MC Free](https://github.com/NeuralNexusNote/n3mc-free)** |
|---|---|---|
| 使えるアプリ | Claude Code（フック）と、Claude デスクトップアプリやほかの MCP 対応アプリ（MCP） | Claude Code だけ |
| 保存されるもの | Claude Code：すべてのメッセージと応答を自動で（フック）。ほかのアプリ：AI が保存すると判断したもの（`save_memory`） | すべての発言を自動で（フック） |
| 残る期間 | 7 日。過ぎると自動で消える | 自分で消すまで |
| 必要なもの | Python 3.10 以上と Docker（Redis Stack） | Python 3.10 以上 |
| 導入 | Redis Stack を起動し、プラグインを入れる（または `pip install` と `n3mc-lite --init`）。デスクトップアプリは、設定に MCP の入口を書く | `pip install n3memorycore-free` の後に `n3mc --init` |
| 向いている使い方 | 短期の作業メモリ。Claude Code とデスクトップアプリで 1 つの記憶を共有する | Claude Code の長期記憶 |

---

## v2.0.0 で変わったこと

v2.0.0 はメジャーバージョンです。
Lite と Claude Code のつながり方が変わりました。
v1.12.2 までの Lite は MCP サーバーだけで、何を保存するかはモデルが決めていました。

| 以前（v1.12.2） | 以後（v2.0.0） |
|---|---|
| Claude Code に 7 つの `mcp__…n3mc-workingmemory__*` ツールが見えていた | **Claude Code に MCP のツールは見えなくなる。** プラグインはもう MCP サーバーを登録しない |
| モデルが `save_memory` に渡すと決めたものだけが保存された | **空でないすべてのメッセージと応答を、フックが保存する**（コードブロックは `[code omitted]` にする）。保存は `[user]` / `[claude]` のレコードになる |
| モデルがターンの最初に `search_memory` を呼んでいた | `UserPromptSubmit` フックが、メッセージのたびに検索する。結果は `# Recalled Memory Context` の見出しで Claude のコンテキストに入る |
| 検索・一覧・取り出し・削除は MCP のツールだった | Claude Code では CLI が同じことをする：`n3mc-lite --search`、`--list`、`--recall-thread`、`--delete`、`--delete-session`、`--buffer`、`--repair`。表示する文はツールの文と同じ |
| アプリごとに自分の MCP サーバーを起動し、モデル（約 1.1 GB）を読み込んでいた | 1 つの常駐サーバーが、モデルを 1 回だけ読み込む。MCP の入口は薄いクライアントで、モデルを読み込まない |
| セッションの合間に動くプロセスはなかった | 常駐サーバーは、一度起動するとバックグラウンドで動き続ける |
| pip の利用者は MCP サーバーを登録した | pip の利用者は `n3mc-lite --init` を 1 回実行して、フックを登録する。Claude Code に残った古い `n3mc-workingmemory` の登録はもう使われないので、消す |
| Redis のコマンドは `docker run … -p 6379:6379 …` だった | `docker run … -p 127.0.0.1:6379:6379 …` になる。記憶がネットワークから届かなくなる。すでに作ったコンテナは、その設定のまま。作り直す（`docker rm -f redis-stack`、そのあと新しいコマンド）と、Redis のほかの再起動と同じく、その記憶は消える |
| MCP の指示は、永続の記憶として `n3mc-longtermmemory` を挙げていた | `n3mc-pro`、`n3mc-pg`、`n3mc-longtermmemory`（N3MC MCP Pro、その PostgreSQL 版、以前の MCP Pro）を挙げる |
| 古いプラグインや `.mcp.json` が、版の指定のない `uvx --from n3memorycore-mcp-lite n3mc-workingmemory` を起動していた | プラグインを更新する。それまでは、その登録が v2.0.0 の MCP の入口を、フックなしで Claude Code に起動する |
| `N3MC_SESSION_ID` で、Claude Code のセッションの保存をまとめられた | フックのレコードは Claude Code 自身のセッション ID を持つ。`N3MC_SESSION_ID` は今も MCP に効く |
| MCP クライアントの `env` の `N3MC_REDIS_URL` で Redis を選べた | `redis_url` を `config.json` に書く。共用のサーバーは、それを起動したプロセスの環境を使う |
| `skip_code_blocks: true` は、コード付きの保存を断った | フックのメッセージや応答も、コードブロックを含めば保存しない |
| — | `search_memory` の検索結果は `context_max_chars`（既定 9,500 文字）に収まる |

**変わらないもの**：
PyPI の名前 `n3memorycore-mcp-lite`。
Python パッケージ `n3mc_mcp`。
MCP のコマンドとサーバー名 `n3mc-workingmemory`（別名 `n3mc-mcp-lite`）。
7 つのツールの名前・引数・出力の文（ただし `search_memory` の文は `context_max_chars` に収まるようになった）。
Redis のレイアウトと設定（`redis_url`、`N3MC_REDIS_URL`）。
7 日の TTL。
データフォルダ `~/.n3mc-workingmemory`（`N3MC_DATA_DIR`）。
プラグイン名 `n3mc-workingmemory`。
検索と保存のすべての決まり。
v1.12.2 が保存したレコードは、そのまま読めます（同じ索引、同じフィールド）。

**v2.0.0 に更新した後に、既存の利用者がすること**

- **プラグインの利用者**：プラグインは、MCP サーバーの代わりにフックを登録します。`uv` は今までどおり `PATH` に必要です。
- **pip の利用者**：`n3mc-lite --init` を 1 回実行し、Claude Code を再起動します。
  Claude Code から古い MCP の登録を消します。
  `claude mcp remove n3mc-workingmemory -s user` を実行します。
  プロジェクトの `.mcp.json` にあるときは、`n3mc-workingmemory` の項目を消します。
  `--init` はこの登録を知らせますが、これらのファイルは書き換えません。
  登録が残ると、Claude Code にフックと並んでツールがまた見えます。
- **Claude デスクトップアプリの利用者**：項目の名前は `n3mc-workingmemory` のままです。
  v2.0.0 では、コマンドを絶対パスで書き、`env` の項目を足します
  （[Claude デスクトップアプリ](#step-3--claude-デスクトップアプリ任意) を見てください）。
  セットアップは、書く項目を表示します。追加は自分で行います（セットアップは、アプリのファイルを書き換えません）。
- **データフォルダに新しいファイルが増えます**：`audit.log`（フックの入力の原文。TTL なし）、`server.log`、`notices.json` と、小さなプロセス用のファイルです。
  [データ保存先](#データ保存先) を見てください。

---

## 🚀 クイックスタート

### Step 1 — Redis Stack を起動

```bash
# 初回のみ（コンテナを作成）：
docker run -d --name redis-stack -p 127.0.0.1:6379:6379 redis/redis-stack-server:latest

# 2 回目以降（コンテナは既存なので start するだけ）：
docker start redis-stack
```

コンテナがあるときに `docker run` をもう一度実行すると、
`Conflict. The container name "/redis-stack" is already in use` で失敗します。
2 回目からは `docker start redis-stack` を使います。

### Step 2 — Claude Code（フック）

どちらか 1 つを選びます。
**Claude Code に MCP サーバーを登録しないでください**
（`claude mcp add`、`~/.claude.json`、`.mcp.json`）。

**(a) プラグインマーケットプレイス（推奨。設定ファイルの編集は不要）。**
[`uv`](https://docs.astral.sh/uv/) が `PATH` に必要です。
Claude Code の中で実行します。

```
/plugin marketplace add NeuralNexusNote/n3mcmcp-lite
/plugin install n3mc-workingmemory@neuralnexusnote
```

プラグインは 3 つのフックを登録します。
`SessionStart` は、CLI のコマンドを事前に許可します。
`UserPromptSubmit` と `Stop` は、`uvx --from n3memorycore-mcp-lite==2.0.0 …` で動きます。
この版の番号は、パッケージの版に合わせて固定されています。
v2.0.0 のリリースまでは、マーケットプレイスからは v1.12.2 のプラグインが入ります。これは MCP サーバーを登録し、上のフックは登録しません。

**(b) pip**

```bash
pip install n3memorycore-mcp-lite
n3mc-lite --init
```

v2.0.0 が PyPI に出るまでは、1 行目を、このリポジトリを clone したフォルダでの `pip install -e .` に替えてください。

`n3mc-lite --init` は、`~/.claude/settings.json` に 2 つのフックを登録します。
コマンドは、`n3mc-lite-hook` / `n3mc-lite-stop-hook` の絶対パスです。
ほかの項目は残します（ほかの N3MC 製品のフックも残します）。
書く前に、ファイルのバックアップを取ります。
CLI のための許可のルールも足します。
プラグインが有効なときは、フックを足しません（プラグインのフックが使われます）。

(a) と (b) は、どちらか 1 つだけにします。両方だと、メッセージのたびに検索が 2 回走ります。

### Step 3 — Claude デスクトップアプリ（任意）

設定ファイルは、アプリから開きます（Settings → Developer → Edit Config）。
Windows でのふつうの場所は `%APPDATA%\Claude\claude_desktop_config.json` です。
Microsoft Store 版のアプリは、別のフォルダに置くことがあります。
`mcpServers` の下に項目を足します。すでにある項目は残します。
保存したら、**アプリを完全に終了して、起動し直します**。

pip で入れた場合：

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

プラグインの利用者（pip で入れていない場合）：

```json
{
  "mcpServers": {
    "n3mc-workingmemory": {
      "command": "uvx",
      "args": ["--from", "n3memorycore-mcp-lite==2.0.0", "n3mc-workingmemory"],
      "env": {
        "N3MC_AGENT_NAME": "claude-desktop"
      }
    }
  }
}
```

- `command`：フックと同じ scripts フォルダにある `n3mc-workingmemory` の**絶対パス**を書きます
  （Windows：`where n3mc-workingmemory`、Linux：`which n3mc-workingmemory`）。
  アプリは、ターミナルの `PATH` を使いません。
  フックと MCP の入口は、同じコードを動かす必要があります（[制限](#制限) を見てください）。
- `env.N3MC_AGENT_NAME`：モデルが `agent_name` を渡さないときに、保存する名前です。アプリごとに 1 つの名前にします。
- フックが別のデータフォルダ（`N3MC_DATA_DIR`）を使うときは、同じ `N3MC_DATA_DIR` を `env` に足します。
- `N3MC_SESSION_ID` を足すと、このアプリの既定の `session_id` を固定できます。
- Windows と Linux のほかの MCP クライアントも、同じように登録します（command、args、env）。

### Step 4 — 再起動

Claude Code（とデスクトップアプリ）を再起動します。
記憶は、次のセッションから始まります。
最初の起動では、常駐サーバーが埋め込みモデル（約 1.1 GB）をダウンロードして読み込みます（2 回目からはキャッシュを使います）。
フックはこれを待ちません。モデルの準備ができるまで、記憶の検索はキーワードだけで行い、そのことを通知で知らせます。
メッセージはベクトルなしで保存し、ベクトルはあとで自動で足します。
MCP のツールと CLI は、モデルを最大 20 秒待ちます。

### 指示書からコードを作る

N3MC シリーズは「指示書が先」です。
指示書は、AI が実装を生成できるように書かれています。
生成するときは、**空のフォルダ**（たとえば `~/n3mcmcp-lite`）に `N3MemoryCore_MCP_Spec_JP.md`（または `_EN.md`）だけを入れます。
Redis Stack を起動し、そのフォルダで Claude Code を開きます。
「この指示書に従って n3mcmcp-lite を実装してください」と頼みます。
AI は、最初にフォルダ・Python・Redis を確かめます。
そのあと、パッケージを生成し、`pip install -e .` を実行します。
`n3mc-lite --init` でフックを登録します。
Claude デスクトップアプリが入っていれば、MCP の入口に書く項目を表示します（アプリのファイルへの追加は、自分で行います）。
最後に、すべてが起動することを確かめます。
フォルダはその後も使われます。消したり動かしたりすると、記憶が止まります。
4 段階の作業の流れは、指示書の付録 B にあります。

---

## ⚠️ 事前準備

| 項目 | 要件 |
|---|---|
| Python | 3.10 以上 |
| クライアント | Claude Code（フック）と、Claude デスクトップアプリまたはほかの MCP クライアント（MCP）のどちらか、または両方 |
| Redis Stack | `localhost:6379` で動いていること（`N3MC_REDIS_URL` で別の URL にできる） |
| pip パッケージ（自動で入る） | `mcp`（`>=1.0.0,<2.0.0`）、`redis`（`<8.0.0`）、`sentence-transformers`、`uuid-utils`、`platformdirs`、`numpy`。v2.0.0 からは `fastapi`、`uvicorn`、`pydantic`、`psutil` も |
| `uv` | プラグインと `uvx` の形を使うときだけ |
| OS | Windows 11 または Linux。macOS は対象外 |

> **docker のコマンドに永続化の引数を付けない理由**：この版は、*わざと揮発的*に作っています。
> 常駐サーバーは、Redis に接続するときに、自分で永続化を切ります（`CONFIG SET appendonly no` と `CONFIG SET save ""`）。
> 手で永続化を有効にしても、次に Lite が動くときに元に戻ります。
> `--save ""` は `docker run` の行に付けません。空の文字列が、Windows PowerShell と cmd.exe の引用符の処理で壊れるからです。
>
> **専用の Redis を使ってください。** サーバーがこの設定を変えるのは、Redis に N3MC のキーしか入っていないときだけです。
> ほかのデータがあれば、永続化の設定は変えません。そして `shared_redis` の通知を出します。
> N3MC Lite には **Redis Stack**（RediSearch）が必要です。
> ふつうの Redis では、どのツールも「Redis Stack を起動してください」という案内を返します。

Redis がなくても、フックは失敗しません。
文章は `audit.log` にだけ残ります。
「Redis Stack を起動してください」という通知が出ます。
あとで送り直すための待ち行列はありません。

---

## 特徴

- 💾 **自分の Redis** — 記憶は、利用者が動かす Redis（既定は自分の PC）に置きます。クラウドのサービスには置きません。AI のサービスに送られるのは、そのターンに取り出した記憶だけで、会話の一部として送られます（[詳しく](#記憶はあなたが管理するマシンに残ります)）。
- ⚡ **Claude Code では自動** — フックが、空でないすべてのメッセージと応答を保存します。メッセージのたびに検索します。モデルの判断は関係ありません。ツールの呼び出しは要りません。
- 🔗 **Claude Code とデスクトップアプリで 1 つの記憶** — どちらも、同じ常駐サーバーと同じ Redis に届きます。
- 🔍 **意味で探す検索** — 言葉が違っても、関係のある過去の会話を見つけます。
- 🌐 **はじめから多言語** — CPU だけで動きます。LLM も GPU も要りません。NFKC の正規化（`ｱﾙﾌｧ`↔`アルファ`、`１２３`↔`123`、合字）、日本語・中国語・韓国語・タイ語・ラオ語・ミャンマー語・クメール語のバイグラム、ラテン文字のダイアクリティカルマークの同一視（`café`↔`cafe`）があります。
- 🛡️ **文字コードの安全策** — Windows では、stdin/stdout/stderr を UTF-8 に切り替えます（cp932 → UTF-8）。すべての入力から、孤立サロゲートを取り除きます。
- 🔄 **セッションをまたぐ文脈** — **7 日**残る作業メモリです（Redis の TTL で自動で消えます）。
- 🧠 **モデルは 1 回だけ読み込む** — 1 つの常駐サーバーが、フック・CLI・すべての MCP クライアントのために、埋め込みモデルを読み込みます。
- 🏢 **1 人に 1 つの記憶** — 記憶は人の間で共有しません。`config.json` ごとに別の `owner_id` があります。検索は、その持ち主のレコードだけを表示します。インストールごとに 1 つの Redis を使ってください。
- 🧹 **揮発性は設計上の特長** — 7 日で消えるので、失敗した試みや捨てた設計が、次の作業に混ざりません。
- 💰 **トークンの無駄を減らす** — 記憶の検索は、ローカルの埋め込み（`intfloat/multilingual-e5-base`）で行います。Claude のトークンは使いません。

## 仕組み

**Claude Code（フック）**

```
User's message
    │
    ▼  UserPromptSubmit hook
    ├─ 1. append the raw input to audit.log
    ├─ 2. add missing vectors (repair, one batch)
    ├─ 3. save Claude's previous reply from the transcript, if the Stop hook missed it
    ├─ 4. search with the message → "# Recalled Memory Context" into Claude's context
    └─ 5. save the message as a [user] record
    │
    ▼
Claude responds
    │
    ▼  Stop hook
    ├─ 1. append the raw input to audit.log
    ├─ 2. save the reply as a [claude] record
    └─ 3. housekeeping: keep .claude/rules/n3mc-lite-behavior.md up to date, rotate audit.log
```

1. `UserPromptSubmit` フックは、まず入力の原文を `audit.log` に書きます。
2. ベクトルのないレコードに、ベクトルを足します（修復。1 回に 1 まとまり）。
3. Stop フックが前の応答を保存し損ねていれば、トランスクリプトから読んで保存します。
4. メッセージで検索し、`# Recalled Memory Context` を Claude のコンテキストに入れます。
5. メッセージを `[user]` のレコードとして保存します。
6. `Stop` フックは、入力の原文を `audit.log` に書き、応答を `[claude]` のレコードとして保存します。
7. 最後に後片付けをします。`.claude/rules/n3mc-lite-behavior.md` を新しく保ち、`audit.log` をローテーションします。

検索は、メッセージを保存する前に行います。だから、メッセージが自分自身を見つけることはありません。
フックは、いつも終了コード 0 で終わります。
サーバーや Redis が止まっていても、文章は `audit.log` に残ります。何をすればよいかは、通知が知らせます。
Claude Code への指示は、ルールファイル `.claude/rules/n3mc-lite-behavior.md` から届きます。
このファイルは、Stop フックがプロジェクトのフォルダに書きます。
手で編集したファイルは、置き換えません。
Claude Code 自身が差し込む入力（タスクの通知、エージェントのメッセージ、システムの注意書き）は、保存も検索もしません。

**Claude デスクトップアプリと、ほかの MCP 対応アプリ**

薄い MCP の入口は、`initialize` に**振る舞いの指示**を付けて答えます。
指示は「ターンの最初に検索する」「意味のあるやりとりのあとに保存する」です。
ツールの呼び出しは、すべて常駐サーバーに渡します。
これらのアプリでは、保存するかどうかはモデル次第です。
[コンプライアンスについて](#コンプライアンスについて--mcp-は促すことしかできない) を見てください。

### 3 つの場面

> **月曜** — Claude Code で、サイトのデータベースを PostgreSQL にすると決めます。理由も話します。フックが、そのやりとりを保存します。
>
> **火曜** — 新しいセッションで、「データベースは何にしたか」と聞きます。月曜の決定が、質問と一緒に Claude のコンテキストに届きます。
>
> **8 日後** — 月曜の記録は、もうありません。各エントリは、保存から 7 日で消えます。そういう設計です。残る記憶が要るなら、[N3MC Free](https://github.com/NeuralNexusNote/n3mc-free) か Pro 版を使ってください。

### 実際に Claude に届くもの

火曜の質問のときに、`UserPromptSubmit` フックが加えた塊です。フックを実際に動かして取ったものです（一時的なデータフォルダと、使い捨ての Redis Stack のコンテナを使い、2 つ目のセッションは 1 つ目と別のフォルダで始めました）。

```text
# Recalled Memory Context
Source: N3MC Lite (7-day working memory)
Search query: サイトのデータベースは、何にすると決めましたか。理由も教えてください。

_If several versions of the same content appear, the one with the newest `saved` time is the current one._

### [01a2020e-5bc8-7950-93b8-d79d3ef70cbb] turn=6ede374d-e530-4e02-a1fe-20b1a4be0038 score=0.491 saved=2026-11-22 16:00 (UTC+0900)
[user] サイトのデータベースは PostgreSQL にしましょう。複数の人が同時に編集する予定で、SQLite は書き込みのたびにファイル全体をロックするからです。

---

### [01a2020e-78d8-7990-b966-eae2d6e917a9] turn=6ede374d-e530-4e02-a1fe-20b1a4be0038 score=0.463 saved=2026-11-22 16:00 (UTC+0900)
[claude] 承知しました。データベースは PostgreSQL にします。理由は、同時に編集する人がいることと、SQLite は書き込みが 1 つずつになることです。次は、記事のテーブルの定義を書きます。
```

各ヒットは `### [<id>]` で始まり、`turn=`、`score=`、`saved=` の時刻が付きます。関連度の下限を通らなかった記録は、`Possibly related` という見出しの下に出ます（[設定](#設定) の `min_results`）。

### Claude 標準の auto-memory との違い

Claude Code には、標準の auto-memory（`~/.claude/projects/.../memory/`）があります。
N3MemoryCore は、それと**競合せず、補い合います**。

|            | Claude auto-memory                             | N3MemoryCore RAG                                   |
| ---------- | ---------------------------------------------- | -------------------------------------------------- |
| **強み**   | 確実。毎セッション読み込まれる。決まった事実に向く | 会話の文脈。詳しい履歴                             |
| **弱み**   | 会話の流れや文脈は残せない                     | 検索の質に左右される。必ず出てくるとは限らない     |
| **向く用途** | 利用者のプロフィール、フォルダのパス、変わらない設定 | 会話の流れ、過去の決定、理由                       |

- **毎セッション要る決まった情報**（フォルダのパス、好み）→ auto-memory
- **会話の文脈と履歴**（議論の流れ、過去の決定）→ N3MemoryCore（7 日の窓）

---

## 記憶はあなたが管理するマシンに残ります

N3MC Lite は、利用者が動かす Redis Stack に記憶を置きます。
既定は自分の PC の Redis です。自分で選んだ Redis サーバーにもできます。

- **N3MC のクラウドではなく、自分の Redis** — 記憶は `redis_url` の Redis に保存します（既定は `redis://localhost:6379/0`。環境変数 `N3MC_REDIS_URL` が優先）。N3MC のアカウントも、N3MC のクラウドサービスもありません。各エントリは、保存から 7 日で消えます（`ttl_seconds`）。
- **常駐サーバーは自分の PC だけで待ち受ける** — `127.0.0.1:18524` で待ち受けます（`bind_host`、`server_port`）。認証はありません。だから **`bind_host` は `127.0.0.1` のままにしてください**。ほかの値にすると、記憶がネットワークに見えてしまいます（サーバーは警告を出します）。`Host` ヘッダーが `127.0.0.1` / `localhost` でない要求は断ります。MCP の入口は、アプリと stdio で話し、ポートを開きません。CLI とフックは、プロキシを通しません。
- **ディスクに置くもの** — Redis のほかに、データフォルダに `config.json` を置きます。v2.0.0 からは **`audit.log` も置きます。これはフックの入力の原文で、すべてのプロンプトと応答を、コードブロックや貼り付けた文章も含めて、そのまま持ちます。TTL はありません**（50 MB でローテーションし、最大約 100 MB）。他人に見せない情報として扱ってください。`audit.log` は、Redis に届かなかったメッセージの最後の控えです。記憶に読み戻すものはありません。[データ保存先](#データ保存先) を見てください。
- **埋め込みは自分の PC で作る** — 埋め込みモデルは、ローカルの CPU で動きます。検索は自分の Redis の中で動きます。検索のために外部の API は呼びません。
- **テレメトリーなし** — N3MC は、利用の情報をどこにも送りません。コードは公開されているので、自分で確かめられます。
- **記憶を見て、消せる** — `n3mc-lite --list` / `list_memories` で新しい記憶を見られます。`--delete` / `delete_memory` と `--delete-session` / `delete_memories_by_session` で Redis から消せます。フックの呼び出しの原文は、データフォルダの `audit.log` にも残ります（TTL なし。50 MB でローテーション）。消すときは、このファイルを自分で消してください。

ネットワークを通るもの：

- **インストール** — `pip` と `uvx` は、PyPI からパッケージと依存を取ってきます。Claude Code のマーケットプレイスは、GitHub の `NeuralNexusNote/n3mcmcp-lite` からプラグインを取ってきます。
- **Redis Stack のイメージ** — 最初の `docker run` で、Docker Hub から `redis/redis-stack-server` を取ってきます。
- **埋め込みモデル** — 最初の起動で、Hugging Face から `intfloat/multilingual-e5-base`（約 1.1 GB）を取ってきます。
- **ほかのマシンの Redis** — `redis_url` / `N3MC_REDIS_URL` をほかのマシンにすると、保存と検索はすべてネットワークを通ります。
- **AI が読むもの** — `# Recalled Memory Context` の塊と `search_memory` の結果は、会話の一部になります。だから、メッセージと一緒に、アプリの裏にある AI のサービス（Claude なら Anthropic）に送られます。N3MC が Redis の中身をどこかに上げることはありません。

## ユースケース — ワーキングメモリが適している場面

7 日の TTL と揮発性の Redis は、**制限ではなく設計上の特長**です。
次のような使い方に向いています。

- **エージェントによるコード生成のループ** — 失敗した試みや捨てた設計が、次の作業に混ざりません。
- **実験・使い捨てのプロトタイプ** — 放っておけば 7 日で消えます。整理は要りません。
- **プロジェクトの間だけの作業メモリ** — 作業やプロジェクトごとに `session_id` を固定して、文脈を分けます。
- **1 つの作業の中で、Claude Code とデスクトップアプリを行き来する**。

**長く残る記憶**が要るなら、作業メモリは向いていません。
Pro 版（永続保存）を使うか、外部に控えを取ってください。
Lite では、TTL を避けるための「永続化の工夫」（RDB スナップショット、AOF、外部へのダンプ）を禁じています。

## ツールと CLI のコマンド

7 つのツール（MCP）と CLI のコマンド（Claude Code）は、常駐サーバーの同じ処理を呼びます。
返す文も同じです。
CLI は、自動保存を促す一文を付けずに表示します。Claude Code ではフックが保存するからです。
`--search` は、フックと同じ塊を、`# Recalled Memory Context` の見出しを付けて表示します。

| ツール（MCP） | CLI（`n3mc-lite`） | 目的 |
|---|---|---|
| `search_memory` | `--search "<keywords>" [--since S] [--until U] [--limit N]` | ハイブリッド（ベクトル + BM25）検索。時間で減衰する順位と `session_id` の加点 |
| `save_memory` | `--buffer "<text>"` または `--buffer -`（stdin） | エントリを保存（7 日の TTL。完全に同じものは断る） |
| `list_memories` | `--list [--limit N]` | 新しい順のエントリ |
| `recall_thread` | `--recall-thread <turn_id> [--before N] [--after N]` | ヒットの `turn_id` の前後のエントリ |
| `delete_memory` | `--delete <id>` | 1 件を消す（親文書の id ならチャンクも消す） |
| `delete_memories_by_session` | `--delete-session <session_id>` | ある `session_id` の記憶をまとめて消す |
| `repair_memory` | `--repair` | 索引がなければ作り直し、ベクトルのないレコードにベクトルを足す（1 回に 200 件まで） |

- Claude Code では、`--search`、`--buffer`、`--list`、`--recall-thread`、`--repair`、`--stop` を事前に許可します（`permissions.allow`）。形は、pip では `python -m n3mc_mcp.cli` と `n3mc-lite`、プラグインでは `uvx --from n3memorycore-mcp-lite==2.0.0 n3mc-lite` です。
- **`--delete` と `--delete-session` は、事前に許可しません。** 元に戻せないので、Claude Code が毎回たずねます。
- 手で実行する `--search` には、大きさの制限がありません。フックの塊と MCP の `search_memory` の結果は、`context_max_chars` に収めます。

## ID 階層構造

N3MemoryCore は、5 つの ID で、各レコードの出どころと文脈を表します。
ふつうの利用者が触るのは `session_id` だけです（`agent_name` はまれに）。
ほかは自動で入ります。

| ID                    | 保存場所          | 作られるとき                         | 単位                         | 目的 |
|-----------------------|-------------------|--------------------------------------|------------------------------|------|
| `id`（主キー）        | Redis のハッシュ  | レコードごと（UUIDv7。時刻順）        | **1 レコード**               | 各記憶の識別子。`delete_memory` と重複の判定に使う。 |
| `owner_id`            | `config.json`     | 最初の起動（UUIDv4）                  | **持ち主・インストール**     | 誰のデータかを表す。`save_memory` のたびに確かめ、合わなければ `owner_id mismatch` で断る。 |
| `local_id`（agent_id） | `config.json`     | 最初の起動（UUIDv4）                  | **エージェント・インストール** | 全行に保存するが、**Lite の `b_local` の順位には使わない**。`b_local` は `stored_importance + access_count` だけで決まる。 |
| `session_id`          | Redis のハッシュ  | 作業・プロジェクト・会話ごと（文字列） | **作業・プロジェクト・会話** | **`b_session` の順位の補正**（`b_session_match=1.0`、`b_session_mismatch=0.6`）に使う。`delete_memories_by_session` の対象を決める。フックのレコードは Claude Code の `session_id`。MCP は、呼び出しの引数 → `N3MC_SESSION_ID` → MCP の入口が起動したときに作る UUIDv4 の順。 |
| `agent_name`          | Redis のハッシュ  | 保存ごと（自由な文字列）              | **エージェントの表示名**     | フックのレコードは、いつも `"claude-code"`。MCP は、引数 → `N3MC_AGENT_NAME` → `initialize` でクライアントが送った名前の順。順位には使わない。 |

フックのレコードには `turn_id` もあります。メッセージと、その応答を結びます。
`--recall-thread` / `recall_thread` に渡すと、ヒットの前後の会話を読めます。

**使い方の目安：**

- MCP で名前のあるプロジェクトの作業をするときは、**`session_id` を固定します**。`save_memory` と `search_memory` に同じ文字列（例 `"proj-alpha"`）を渡すか、`N3MC_SESSION_ID` を設定します。
- **`owner_id` は渡さないでください。** 持ち主であることを示す必要があるときだけ渡します。
- **インストールごとに 1 つの Redis を使ってください。** 複数の PC や人が同じ Redis を使うと、次のことが起きます。`owner_id` を作り直して古いレコードが残っているときも同じです。ある持ち主が保存した本文は、もう一方には `duplicate` として断られます（消えるまで 7 日）。ある持ち主のレコードが検索の候補を埋め、もう一方のレコードが見つからなくなります。インストールごとに専用の Redis を用意してください。

## データ保存先

記憶は Redis にあり、自動で消えます。
データフォルダは `~/.n3mc-workingmemory/` です（Windows：`%USERPROFILE%\.n3mc-workingmemory\`）。
環境変数 `N3MC_DATA_DIR`（絶対パス）で変えられます。

| ファイル | 中身 |
|---|---|
| `config.json` | 設定（v1.12.2 までは、これだけ） |
| `audit.log`、`audit.log.1` | （v2.0.0）フックの呼び出しごとの JSONL の記録。何よりも先に書く。プロンプトと応答の原文で、**TTL はない**。50 MB でローテーションするので、最大約 100 MB |
| `n3mc-lite.pid` | （v2.0.0）常駐サーバーの PID ファイル（動いている間だけある） |
| `server.log` | （v2.0.0）常駐サーバーの stderr（起動時に 1 MB を超えていれば空にする） |
| `server_start_failed` | （v2.0.0）サーバーの起動に失敗した印（5 分は起動し直さない） |
| `notices.json` | （v2.0.0）利用者の対応が要る通知。1 日に 1 回表示する |
| `turn_id_<session>.txt` | （v2.0.0）`UserPromptSubmit` フックから `Stop` フックに渡す `turn_id`。30 日より古いものは消す |

v1.10.0 から、フォルダはホームフォルダにあります（以前はプラットフォームのアプリデータのフォルダでした）。
Microsoft Store 版の Claude デスクトップアプリは、自分が起動するプロセスの `%LOCALAPPDATA%` を別の場所に向けます。
そのため、1 台の PC に設定と `owner_id` が 2 つできていました。
最初の起動で、古い設定を 1 回だけコピーします（古いファイルは残します）。

バックアップは想定していません。エントリは 7 日の窓で消えていきます。
ID にかかわるのは `config.json`（`owner_id` / `local_id`）だけです。

## 設定

最初の起動で、`config.json` を自動で作ります。`owner_id` と `local_id` にはランダムな UUID が入ります。
変えられる既定値：

```json
{
  "owner_id":                 "<uuid>",
  "local_id":                 "<uuid>",
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

- `redis_url` — 接続先の URL。環境変数 `N3MC_REDIS_URL` が優先します。
- `ttl_seconds` — 新しい記憶とその重複防止の印に付ける TTL（既定 7 日）。
- `vector_min_cos` — ベクトルの候補になるための、問い合わせとの近さ（コサイン）。既定は `0.85` です。目安は 0.80–0.88 です。下げると、より多く拾い、関係の薄い行も増えます。上げると、結果が短くなります。0.85 は `multilingual-e5-base` で測った値です。`embedding_model` を変えたら、測り直してください。
- `relative_min_score_ratio` — 最高点に対してこの比より低い結果を外します（既定 `0.6`。`0.0` で無効）。
- `min_results` — しきいを通った結果がこれより少ないときは、外した行のうち近いものを、別の見出し `#### Possibly related (weak matches — may be unrelated)` の下に足します（既定 `3`。`0` で無効）。弱い一致の行は、`access_count` に数えません。
- `version_order_enabled` / `version_sim_threshold` / `version_min_chars` — 中身が 0.98 以上似ている結果は、同じものの版として扱い、新しい順に並べます。各ヒットには `saved=` の時刻が付きます。
- `chunk_threshold` / `chunk_overlap` — 区切りの大きさと重なり（文字数）。これより長い本文は、親文書とチャンクで保存します。ヒットすると全文が返ります。
- `access_count_*` — よく使われる記憶の加点。**2026-11-17 から既定は無効**です（`access_count_enabled: false`）。ヒットの回数は数えます。
- `ttl_refresh_top_k` — 検索のたびに `access_count` を増やす、上位の件数です。名前は以前の版のままですが、検索で TTL は戻しません。（`ttl_refresh_on_search` は 2026-11-22 になくしました。`config.json` に残っていても使いません。）
- `lexical_rerank_*` / `rerank_weight` / `rerank_phrase_weight` — 合算の後の、軽い語彙での並べ替え（CPU だけ）。
- `b_session_match` / `b_session_mismatch` — 保存された `session_id` が合う行（既定 `1.0`）と合わない行（`0.6`）に掛ける値。両方を `1.0` にすると、補正はなくなります。
- `skip_code_blocks` — `true` にすると、3 つのバッククォートの囲みを含む内容を保存しません（`status: "skipped_code"`）。フックの保存にも効きます。既定の `false` では、コードブロックを `[code omitted]` に置き換えて保存します。
- `embedding_model` / `embedding_dim` — 埋め込みモデルと、そのベクトルの次元。2 つは合っている必要があります。どちらかを変えたら、再起動と索引の消去が要ります。
- `server_port`（v2.0.0）— 常駐サーバーのポート（既定 `18524`）。N3MC-Free は 18520、Pro は 18521、PostgreSQL 版は 18522、Ollama 版は 18523 を使います。1 台の PC では、N3MC の製品ごとに別のポートにします。
- `bind_host`（v2.0.0）— 既定の `127.0.0.1` のままにします。
- `context_max_chars`（v2.0.0）— フックの `# Recalled Memory Context` の塊と、MCP の `search_memory` の結果の最大の大きさ（既定 `9500`。2000–10000）。Claude Code は、10,000 文字を超えるフックの出力をファイルに移し、2,000 文字の抜粋だけを見せます。塊が長すぎるときは、まず順位の低い行を外し、それでも長ければ文を切ります。手で実行する `--search` は全部を見せます。
- **変更が効くとき**：`config.json` を書き換えると、次のフック・CLI・MCP の呼び出しで常駐サーバーが起動し直します。`config.json` に書き間違いがあって読めないときは、ファイルには触らず、既定値で動きます。最初の検索の結果がそのことを知らせます。数値は引用符なしで書きます。

全項目は、指示書の §6 にあります。

## 多言語対応

はじめから入っています。CPU だけで動き、LLM も GPU も要りません。
同じ言葉をどう打っても、検索と重複の判定は同じように動きます。

| 層 | すること | 例 |
|---|---|---|
| **NFKC 正規化** | SHA・埋め込み・BM25 の前に、互換文字をそろえる | `ｱﾙﾌｧ` ↔ `アルファ`、`１２３` ↔ `123`、`ﬁ` ↔ `fi` |
| **バイグラムの BM25 補助** | 空白のない文字に、重なる 2 文字の組を作る | `記憶装置` → `記憶 憶装 装置`。韓国語（`안녕하세요`）、タイ語（`สวัสดี`）、ラオ語、ミャンマー語、クメール語も同じ |
| **ダイアクリティカルマークの同一視** | ラテン・ギリシャ・キリル文字の語を、結合記号なしでも索引に入れる | `café` が `cafe` に、`Ångström` が `Angstrom` に一致 |
| **multilingual-e5-base の埋め込み** | 100 以上の言語にまたがる意味の空間 | 言語をまたぐ言い換えの検索 |

これらは、保存と検索のたびに動きます。
保存する `content` は書き換えません。長い本文は、保存したとおりに全文が返ります。

## エンコーディング安全策

1. **UTF-8 への切り替え** — MCP の入口、フックの入口、CLI は、起動したときに `sys.stdin` / `sys.stdout` / `sys.stderr` を UTF-8 にします。日本語の Windows では、コンソールの既定のコードページが cp932 です。そのままだと、ASCII 以外のバイトがすべて壊れます。POSIX の環境はもともと UTF-8 なので、何も変わりません。
2. **孤立サロゲートの除去** — 保存するすべての文章、すべての問い合わせ、フックの入力の全体を `sanitize_surrogates()` に通します。Windows のパイプが、Python で読めないバイトを渡すと、UTF-16 の孤立サロゲート（`U+D800`–`U+DFFF`）が出ることがあります。この対策がないと、書き込みが失敗します。

空になった内容や、見える文字のない内容は、`empty content` / `empty content (no visible characters)` で断ります。

保存するすべてのものに、2 つの決まりがあります。

- **外側の空白を 1 回だけ取ります** — `' \nAAA\n '` は `AAA` として保存します。
- **コードブロックは `[code omitted]` にします** — 囲まれたブロック（```` ``` ```` … ```` ``` ````、または閉じていない囲みから最後まで）を、保存の前に置き換えます。行内のバッククォートは残します。N3MemoryCore が覚えるのは、話したことと決めたことです。コードは、プロジェクトのファイルにあるべきものです。

フックのレコードは、役割の印で始まります：`[user] ` または `[claude] `（長い文章のチャンクは `[user 2/5] ` など）。

## ランキング式

```
final_score = (0.7 * cosine_similarity + 0.3 * keyword_relevance) * time_decay * b_local * b_session

time_decay   = 2 ^ (-days_elapsed / half_life_days)       (default half-life: 3 days)
b_local      = clamp(0.5, 2.0, stored_importance + access_boost)
access_boost = min(0.5, access_count * 0.02)               (only when access_count_enabled)
b_session    = b_session_match (default 1.0)   if row.session_id == effective_session
             = b_session_mismatch (default 0.6) otherwise
```

半減期は 3 日で、7 日の TTL より短いです。だから Lite では `time_decay` がよく効きます。
保存したばかりの記憶は 1.0、3 日前のものは 0.5、7 日前のものは約 0.20 です。

よく使われる記憶の加点は、2026-11-17 から既定で無効です。
N3MC-Pro で測ったところ、recall@3 が下がったためです（よく返る記憶が、より関係のある記憶より上に来ました）。

## ほかの N3MC 製品との共存

Lite は、N3MC-Free、N3MC-Pro、MCP Pro / PostgreSQL / Ollama 版と同じ PC に入れられます。
共有するものはありません。
Lite には、専用のデータフォルダ（`~/.n3mc-workingmemory`）、データベース（Redis）、サーバーのポート（18524）、フックのコマンド（`n3mc-lite-hook`、`n3mc-lite-stop-hook`）、CLI（`n3mc-lite`）、ルールファイル（`.claude/rules/n3mc-lite-behavior.md`）があります。

- `n3mc-lite --init` は、ほかの製品のフックを残し、そのことを知らせます。
- 2 つの製品のフックが入っていると、Claude は製品ごとに 1 つずつ `# Recalled Memory Context` の塊を受け取ります。それぞれの記憶から取り出したものです。
- ほかの N3MC のサーバーが Lite のポートを使っているとき、Lite はそのサーバーを使わず、止めもしません。ベクトルなしで保存し、検索はしません。`port_conflict` の通知を出し、Lite に専用の `server_port` を与えるよう求めます。
- 同じアプリに永続の N3MC の記憶が MCP でつながっているとき — `n3mc-pro`（N3MC MCP Pro）、`n3mc-pg`（その PostgreSQL 版）、`n3mc-longtermmemory`（以前の MCP Pro）— は、MCP の指示が Lite を作業メモリとして扱います。

## 制限

- **7 日。** 各エントリは、保存から 7 日で消えます。検索しても延びません。`audit.log` は例外で、TTL がありません。
- **フックのないアプリでは、保存はモデル次第です**（下を見てください）。会話の全部が残るのは Claude Code だけです。
- **常駐サーバーが動ける必要があります。** 起動できないときは、検索はエラーか代わりの文を返します。保存は、ベクトルなしで Redis に入ります（キーワードではすぐ見つかり、ベクトルはあとで修復が足します）。
- **すべてのクライアントで 1 つのインストール。** フック・CLI・MCP の入口は、同じ版を動かす必要があります。違うと、呼び出しのたびに、互いのサーバーを置き換えてしまいます。デスクトップアプリは、フックと同じ scripts フォルダか、同じ版に固定した `uvx` の形で登録します。
- **プラグインと pip を一緒に使わないでください。** プラグインが有効なとき、`--init` はフックを登録しません。プラグインは、`--init` が書いたフックがあれば知らせます。
- **まだ PyPI にはありません。** このリポジトリの v2.0.0 のコードは指示書から生成したもので、テストに合格しています。ただし、PyPI の最新版はまだ 1.12.2 です。v2.0.0 が公開されるまでは、このリポジトリから入れてください（`pip install -e .`）。`pip install n3memorycore-mcp-lite` では 1.12.2 が入ります。この README の `uvx --from n3memorycore-mcp-lite==2.0.0 …` の形は、その版がまだ PyPI にないので動きません。プラグインのマーケットプレイスからは、v1.12.2 のプラグインが入ります。

## なぜ N3MemoryCore？（組込みメモリとの違い）

| 観点 | 組込みメモリ | N3MemoryCore（Lite） |
|---|---|---|
| **データの持ち主** | ベンダーが預かる | **自分のマシンの、自分の Redis Stack** |
| **Claude Code での保存** | モデル次第 | **フックが、すべてのメッセージと応答を保存** |
| **使えるクライアント** | そのベンダーの製品だけ | **Claude Code（フック）と、あらゆる MCP クライアント** |
| **原文の取り出し** | 見えない（要約されることも） | **親文書の約束。保存したとおりの全文を返す（コードブロックは `[code omitted]`）** |
| **検索の中身** | ブラックボックス | **BM25 + e5 ベクトル + CJK バイグラム + 時間減衰 + 軽い並べ替え。値はすべて見えて、変えられる** |
| **確認と操作** | 画面からだけ | **一覧・取り出し・削除・`session_id` でのまとめての削除を、生のレコードに対して行える** |
| **残る期間** | ベンダーのサービス次第 | **7 日の TTL の Redis。短命が設計だが、コンテナは自分のもの** |

1 つのベンダーの製品の中で「AI がセッションをまたいで何かを覚えていればよい」だけなら、組込みメモリのほうが簡単です。

## コンプライアンスについて — MCP は「促す」ことしかできない

**この節は、フックのないアプリ**（Claude デスクトップアプリと、ほかの MCP クライアント）**の話です。**
Claude Code では、モデルの判断にかかわらず、フックがすべてのメッセージと応答を保存し、メッセージのたびに検索します。

MCP サーバーは、LLM にツールを呼ばせることはできません。
MCP で使える手は 3 つです。`tools/list` のツールの説明、セッションの始めに送る `instructions`、ツールの応答の文です。
Lite は 3 つとも使います。ツールの応答の最後には、いつも短い念押しを付けます。
それでも、**LLM が従うかどうかは決まりません**。
モデル、クライアントのプロンプトの組み立て方（`instructions` を要約したり落としたりするクライアントもあります）、ほかの指示によって変わります。
ほとんどのターンは正しく保存されます。短い答えや、事実の訂正は、保存が抜けることがあります。

必ず保存したいとき：

- **はっきり頼む** — プロンプトに「これを N3MemoryCore に保存して」と書きます。LLM は、はっきりした頼みにはほぼ必ず従います。
- **Claude Code を使う** — フックが、すべてのやりとりを保存します。
- **自分のアプリから Anthropic の Messages API を呼ぶ** — `tool_use` を自分で動かし、毎ターン `save_memory` を呼びます。そのアプリは自分で書く必要があります。

## アンインストール

フックの登録も消えるように、AI に「N3MC Lite を削除してください」と頼みます。
自分で行うときは、次のとおりです。

1. プラグインの利用者は `/plugin uninstall n3mc-workingmemory@neuralnexusnote`。
   pip の利用者は、`~/.claude/settings.json` のうち、コマンドが `n3mc-lite-hook` / `n3mc-lite-stop-hook` を実行する 2 つの項目を消します。
2. 常駐サーバーを止めます：`n3mc-lite --stop-server`（または、コマンドラインに `n3mc_mcp.cli` と `--run-server` を含むプロセスを終わらせます）。
3. `pip uninstall n3memorycore-mcp-lite` でパッケージを消します。
4. `docker rm -f redis-stack` で Redis のコンテナを止めて消します。保存した記憶はすべて消えます。
5. `~/.n3mc-workingmemory/`（または `N3MC_DATA_DIR`）を消します。`config.json`、`audit.log` とほかのファイルが消えます。
6. Claude デスクトップアプリの設定ファイルから、`n3mc-workingmemory` の項目を消します。

プロジェクトのフォルダにある `.claude/rules/n3mc-lite-behavior.md` は、手で消してかまいません。

## 開発

```bash
# テストは使う Redis を FLUSHDB する。記憶用の Redis ではなく、テスト専用のコンテナを使う
docker run -d --name redis-stack-test --restart=no -p 127.0.0.1:6390:6379 redis/redis-stack-server:latest
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
N3MC_REDIS_TEST_URL=redis://localhost:6390/0 pytest tests/ -q
```

Windows の PowerShell:

```powershell
docker run -d --name redis-stack-test --restart=no -p 127.0.0.1:6390:6379 redis/redis-stack-server:latest
python -m venv .venv; .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
$env:N3MC_REDIS_TEST_URL = "redis://localhost:6390/0"; pytest tests/ -q
```

RediSearch は DB 0 にしか索引を作れません。だから、別の DB 番号でテストすることはできません。
テストは、**専用の** Redis コンテナで実行してください。
v2.0.0 のテストの層（フック、サーバーの API、CLI、MCP クライアント、大きさの制限、共存）は、指示書の §9 にあります。テストは `tests/` にあります。

CI は、`main` への push と、`main` 向けの PR のたびにテストを実行します。
[`.github/workflows/test.yml`](./.github/workflows/test.yml) を見てください。
開発者向けの案内（英語 + 日本語）は [`CONTRIBUTING.md`](./CONTRIBUTING.md) にあります。

## Lite 版の拡張・改造

このリポジトリは**公開されていて、Apache-2.0 ライセンス**です。
自由にフォークし、変え、動かしてかまいません。
動きを変えたいとき（順位の式、クロスエンコーダの並べ替え、日本語の形態素解析など）は、指示書から始めてください。

- [`N3MemoryCore_MCP_Spec_EN.md`](./N3MemoryCore_MCP_Spec_EN.md) — 設計の全文（英語）
- [`N3MemoryCore_MCP_Spec_JP.md`](./N3MemoryCore_MCP_Spec_JP.md) — 日本語版

指示書の付録 A に、追加できる機能と、その差し込み口があります。
フォークを別のパッケージ名で公開するときは、`pyproject.toml` の `name`、`[project.urls]`、コンソールスクリプトの名前も変えてください。

## トラブルシューティング

### Windows: `pip install --upgrade` が `WinError 32`（ファイル使用中）で失敗する

症状：
```
ERROR: Could not install packages due to an OSError: [WinError 32]
The process cannot access the file because it is being used by another process:
'...\Scripts\n3mc-workingmemory.exe' -> '...\Scripts\n3mc-workingmemory.exe.deleteme'
```

原因：動いているプロセスが、パッケージのファイルを開いたままにしています。
たとえば、Claude デスクトップアプリが `n3mc-workingmemory.exe` を動かしています。

対処：Claude デスクトップアプリと Claude Code を完全に終了します。
Windows では、ウィンドウを閉じるだけでは足りません。タスクマネージャーでプロセスを終わらせるか、一度サインアウトします。
常駐サーバーも止めます（`n3mc-lite --stop-server`）。
そのあと、もう一度 `pip install --upgrade` を実行します。

### `~3memorycore-mcp-lite` 警告が pip install 時に出る

次のような行が出ることがあります。
```
WARNING: Ignoring invalid distribution ~3memorycore-mcp-lite
```
これは、前のインストールが書き込みの途中で止まったことを、pip が知らせています（たいていは上のファイルロックが原因です）。
残ったフォルダは、名前が `~` で始まります。害はありませんが、警告が出続けます。手で消してください。

```bash
# Windows
rmdir /s "%LOCALAPPDATA%\Programs\Python\Python312\Lib\site-packages\~3memorycore_mcp_lite-<version>.dist-info"
```

（パスは、自分の Python のインストールに合わせてください。）

### `# Recalled Memory Context` の塊に、検索が使えないと出る

塊は、いつも記憶の状態を表示します。
`the memory server is still starting` は、常駐サーバーが起動中で、まだ答えないときに出ます。少し待ってから、次のメッセージを送ってください。
最初の起動でモデルをダウンロードしている間は、サーバーはもう答えます。検索はキーワードだけで行い、モデルが `is still loading` だという通知が出ます。ベクトルはあとで自動で足します。
ほかの理由のときは、原因が書かれています（`see <server.log path>`、ポートを使うほかの N3MC のサーバー、Redis に届かない、など）。
`> ⚠️ N3MC notice:` の行は、何をすればよいかを知らせます。たとえば `docker start redis-stack` です。

## ライセンス

Apache License 2.0 です。[LICENSE](./LICENSE) を見てください。
一部は N3MC-Free（Apache License 2.0、同じ作者）から作られています。
[NOTICE](./NOTICE) と [MODIFICATIONS.md](./MODIFICATIONS.md) を見てください。
