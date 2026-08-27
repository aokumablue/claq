# CLAUDE.md

## コーディングルール

- `Python` コードに `docstring` を付ける
- 後方互換フォールバックは実装しない（古いコードは必ず削除）
- カバレッジ `100%`
- テーブル定義変更は `CREATE TABLE` を直接修正（リリース前のためマイグレーション不要）

## 作業ルール

- Python は `python3` を使う
- 変更後はリポジトリ直下の `.venv` を有効化して `python3 -m pytest -q` と `ruff check plugins/bluecore/src` が成功することを確認（警告なし）。カバレッジ100%の確認は `cd plugins/bluecore && python3 -m pytest -q --cov` で行う（`pyproject.toml` の `fail_under=100` はこのディレクトリでのみ解決する。リポジトリ直下には coverage 設定が無く `--cov` を付けてもゲートが発火しないため注意）
- ランタイムは venv を作らない
- 開発用 venv は `bluecore-dev/.venv` のみで、`scripts/install-dev.sh` が作成する
- 新規 hook・外部呼び出し（DB/ネットワーク/DL）は非ブロッキング + ハードタイムアウト必須
- pytest をパイプする際は `set -o pipefail` 必須
- 開発中コードの CLI/モジュール実行に `PYTHONPATH=plugins/bluecore/src` は不要 — editable install が venv の `bluecore` をこのリポジトリの `plugins/bluecore/src` へ向ける。`.venv/lib/*/site-packages/_editable_impl_bluecore.pth` がプラグインキャッシュを指していたら `install-dev.sh` を再実行して直す

## ランタイム前提

- 対応 `python3` は 3.12 以上。`launcher.py` は 3.12 未満を検出すると保護フック（`block_no_verify` / `pre_bash_commit_quality` / `config_protection`）を stderr へ警告した上で **exit 0（fail-open）** にする。これは意図的な設計判断（`launcher.py:52-58` に理由を明記）— ランタイムは venv も install.sh も持たないため（[[runtime-no-venv]]）インストール時に対応 Python を検証する経路が無く、fail-closed（exit 2）にすると `hooks.json` が呼ぶ PATH 上の裸 `python3` を直す手段（Bash）ごとセッション内から塞がれ復旧不能になる。stderr の `bluecoreProtectionDisabled` 警告を監視しない環境では保護無効化を見落とすため、対応 Python の確保は利用者側の責務とする
- 対応 OS は macOS / Linux。`hooks/hook_common.py` の stdin 読み取りガードは `select.select` を使用しており、Windows の通常 console stdin では機能しない可能性がある（Windows 未対応）

## データモデルの前提

- 永続化は `~/.bluecore/mem.db`（SQLite）単独。チーム共有ストア（旧 PostgreSQL 同期）は全廃済み — 各メンバーのメモリは自身の SQLite に閉じる
- テーブルは `repos` / `knowledge` / `sessions` の 3 つだけ（`mem/schema.py`）。リポジトリ識別は `repos.id`（人間可読スラッグ）の 1 系統で、別台帳ファイルは持たない
- 記憶の単位は **知識カード**（`knowledge` の 1 行）。`kind` は `convention` / `decision` / `pitfall` / `howto` / `fact` / `preference` の 6 値、`scope` は `global` / `repo`。SessionStart に注入されるのは `status='active'` のみで、`pending` は人間が `/instinct promote` で昇格させるまで注入されない。`bluecore_mem_learn` / `mem learn` から入るカードは `source`/`status` を指定できず、常に `source=agent` / `status=pending` で登録される（H-01）。JSON に `status`/`source` を書くと usage error になり、`--status` フラグ自体が存在しない。したがって agent 由来カードは例外なく `/instinct promote` を経なければ注入されない
- 検索は埋め込みベクトルも FTS5 も使わない。知識カードは数百件オーダーに収まるため、全件をロードして Python 側でスコアリングする（`mem/cli.py`）。静的埋め込みテーブル・`bluecore.model_build`・numpy・sqlite-vec はいずれも全廃済み
- ランタイム依存はゼロ（標準ライブラリのみ）。frontmatter 解析は `lib/frontmatter.py` の自前パーサで、依存ゼロは `tests/lib/test_runtime_dependencies.py` が機械的に保証する
- 出力トークンの最小化が設計原則。`list` / `search` は `- [kind] title (key)` の 1 行だけを返し、`body` を返すのは `mem show <key>` だけ。0 件なら 1 文字も出力しない

