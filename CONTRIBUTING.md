# Contributing to N3MemoryCore MCP Lite

Thanks for considering a contribution! This file is the practical guide
for forking the repo, running tests locally, and proposing changes.

> **日本語版は本ファイル末尾に併記しています。**

---

## 1. Fork & local setup (5 min)

```bash
# 1. Fork on GitHub, then:
git clone https://github.com/<YOUR_USERNAME>/n3mcmcp-lite
cd n3mcmcp-lite

# 2. Start Redis Stack (required — RediSearch only indexes DB 0):
docker run -d --name redis-stack -p 6379:6379 redis/redis-stack-server:latest

# 3. Create a venv and install with dev deps:
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows (PowerShell):
.venv\Scripts\Activate.ps1

pip install -e ".[dev]"
```

The first run of any test that touches the embedding model will download
~440 MB of `intfloat/e5-base-v2` weights into `~/.cache/huggingface/`.
This is a one-time cost.

---

## 2. Run the test suite

```bash
# All 105 tests (requires Redis Stack on localhost:6379):
pytest tests/ -q

# A single layer:
pytest tests/test_database.py -v
pytest tests/test_processor.py -v
pytest tests/test_server.py -v

# Skip the slow embedding tests:
pytest tests/ -q -k "not TestEmbedding"

# With a non-default test Redis:
N3MC_REDIS_TEST_URL=redis://localhost:6390/0 pytest tests/ -q
```

> **⚠️ Destructive test DB warning**: tests `FLUSHDB` index 0 before and
> after every test. RediSearch refuses to create indexes outside DB 0
> (`Cannot create index on db != 0`), so a separate test DB is not an
> option. **Run the suite against a dedicated Redis container** —
> never one holding data you care about.

CI runs the same matrix on Python 3.10 / 3.11 / 3.12 / 3.13 against a
fresh Redis Stack service container. See `.github/workflows/test.yml`.

---

## 3. Build the package locally

```bash
pip install build twine
python -m build --wheel --sdist
python -m twine check dist/*
```

Output goes to `dist/`. The wheel is `py3-none-any` (pure Python).

---

## 4. Coding conventions

- **Spec is the contract**: `N3MemoryCore_MCP_Spec_EN.md` (and JP) define
  the wire-level behavior. If you change observable behavior, update
  the spec in the same PR.
- **No silent embedding-model swaps**: `intfloat/e5-base-v2` and the
  768-dim FLAT vector index are pinned. Changing them requires a major
  version bump and a migration story.
- **Pipeline atomicity is non-negotiable**: every `save_memory` writes
  `HSET` + `EXPIRE` + sha-guard in a single Redis pipeline. Do not
  introduce code paths that can leave a record without a TTL.
- **Verbatim recall (§3.11)**: the raw `content` field on `mem:` and
  `doc:` hashes must never be rewritten by normalization, sanitization,
  or formatting. If you need a normalized form, store it in
  `content_ngram` or compute it ad-hoc in a side path.
- **Encoding safety (§3.13)**: any new tool input that calls
  `.encode("utf-8")` downstream MUST be passed through
  `processor.sanitize_surrogates()` first.

---

## 5. Pull request checklist

- [ ] `pytest tests/ -q` passes locally against Redis Stack.
- [ ] `python -m build` succeeds and `twine check dist/*` is clean.
- [ ] Spec sections affected by the change are updated (EN + JP).
- [ ] `CHANGELOG.md` has a new bullet under `## Unreleased`.
- [ ] Public API changes (tool names, response JSON keys, config keys)
      are documented in both READMEs.
- [ ] No new dependencies without justification — every dep is a Lite
      install-time cost for the user.

---

## 6. Reporting issues

Open an issue at
<https://github.com/NeuralNexusNote/n3mcmcp-lite/issues>. Useful info:

- OS + Python version (`python --version`).
- Redis Stack version (`docker exec redis-stack redis-server --version`).
- The exact failing command and a copy of `stderr` from the MCP server
  (Claude Code shows this in the MCP panel).
- For search-quality issues: a minimal reproducer (`save_memory`
  payload + `search_memory` query + observed top-K + expected top-K).

---

## 日本語版

### 1. フォークとローカルセットアップ（5 分）

```bash
# 1. GitHub でフォークしてから：
git clone https://github.com/<YOUR_USERNAME>/n3mcmcp-lite
cd n3mcmcp-lite

# 2. Redis Stack を起動（必須 — RediSearch は DB 0 にしかインデックスを作れない）：
docker run -d --name redis-stack -p 6379:6379 redis/redis-stack-server:latest

# 3. venv を作って dev 依存込みでインストール：
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows (PowerShell):
.venv\Scripts\Activate.ps1

pip install -e ".[dev]"
```

初回の埋め込みモデル絡みのテストで `intfloat/e5-base-v2` の重み (~440 MB)
が `~/.cache/huggingface/` にダウンロードされます。

### 2. テスト実行

```bash
# 全 105 テスト：
pytest tests/ -q

# 個別レイヤ：
pytest tests/test_database.py -v
pytest tests/test_processor.py -v
pytest tests/test_server.py -v

# 重い埋め込みテストを除外：
pytest tests/ -q -k "not TestEmbedding"
```

> **⚠️ テストは DB 0 を毎回 FLUSHDB します**。残したいデータが入っている
> Redis をテストに使わないでください。CI は GitHub Actions で
> Python 3.10–3.13 × Redis Stack を毎 push 走らせます
> （`.github/workflows/test.yml`）。

### 3. パッケージビルド

```bash
pip install build twine
python -m build --wheel --sdist
python -m twine check dist/*
```

### 4. コーディング規約

- **仕様書が契約**：`N3MemoryCore_MCP_Spec_JP.md`（および EN）が外向き
  挙動を定義する。観測可能な挙動を変えたら同じ PR で仕様書も更新。
- **埋め込みモデルの暗黙差し替え禁止**：`intfloat/e5-base-v2` と
  768 次元 FLAT ベクトルインデックスは pin。変更にはメジャー版バンプと
  マイグレーション計画が必要。
- **パイプラインのアトミック性は絶対**：`save_memory` の HSET + EXPIRE
  + sha-guard は単一パイプラインで発行。TTL 無しのレコードを残しうる
  コード経路を導入しない。
- **Verbatim 復元（§3.11）**：`mem:` および `doc:` の生 `content`
  フィールドは正規化・サニタイズ・整形で書き換えない。正規化形が必要
  なら `content_ngram` を使うか、用途ごとにアドホック計算する。
- **エンコーディング安全策（§3.13）**：新しいツール入力で下流が
  `.encode("utf-8")` する経路があるなら、必ず
  `processor.sanitize_surrogates()` を通す。

### 5. PR チェックリスト

- [ ] ローカルで `pytest tests/ -q` が通る（Redis Stack 起動済み）
- [ ] `python -m build` 成功＆ `twine check dist/*` がクリーン
- [ ] 影響する仕様書セクションを EN/JP 両方更新
- [ ] `CHANGELOG.md` の `## Unreleased` に行を追加
- [ ] 公開 API（ツール名・レスポンス JSON キー・設定キー）の変更を
      両 README に記載
- [ ] 正当な理由なく新しい依存を増やさない

### 6. Issue 報告

<https://github.com/NeuralNexusNote/n3mcmcp-lite/issues> に立ててください。
役立つ情報：

- OS と Python バージョン
- Redis Stack バージョン
- 失敗時のコマンドと MCP サーバの stderr（Claude Code の MCP パネルに出る）
- 検索品質の問題：再現可能な最小ペイロード（`save_memory` 入力 +
  `search_memory` クエリ + 実際の上位 K 件 + 期待した上位 K 件）
