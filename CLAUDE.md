# CLAUDE.md

## コード

- Python に docstring を付ける
- 後方互換フォールバックは置かない。古いコードは削除する
- カバレッジ 100%
- テーブル定義は `CREATE TABLE` を直接修正する（マイグレーションしない）

## 検証

- Python は `python3` を使う
- 変更後はリポジトリ直下の `.venv` を有効化し、`python3 -m pytest -q` と `ruff check plugins/claq` を通す
- カバレッジ確認は `cd plugins/claq && python3 -m pytest -q --cov`（`fail_under=100` はこのディレクトリでのみ効く）
- pytest をパイプするときは `set -o pipefail`

## ランタイム

- ランタイムは venv を作らない。開発用 venv はリポジトリ直下 `.venv`（`scripts/install-dev.sh`）
- 対応 Python は 3.12 以上。未満は保護フックを fail-open（exit 0）する
- `hooks.json` は裸の `python3` を呼ばない。全エントリが `runtime/claq-hook`。Windows は `command` と `powershell` を同一モジュール・同一引数で両方宣言する
- 新規 hook / 外部呼び出しは非ブロッキング + ハードタイムアウト
- ランタイム依存は標準ライブラリのみ
- `~/.claq/env.sh` は POSIX 専用。Windows では SessionStart が注入する `runtime/claq-hook.cmd` を使う

## データ

- 永続化は `~/.claq/mem.db` のみ。テーブルは `repos` / `knowledge` / `sessions`。`~/.claq` は 0700、`mem.db` は 0600。Windows では DACL を変えない
- agent 由来カードは常に `source=agent` / `status=pending`。注入されるのは `active` のみ（人間が `promote`）
- `list` / `search` は 1 行。`body` は `show` のみ。0 件は無出力

## ADR

- `docs/adr/` には現行の状態と、現在も有効な判断基準だけを書く
- 「過去にこうだった」経緯・改訂履歴・バージョン番号・採番 ID は置かない
- ファイル名に連番は振らない。役割ごとに 1 ファイルとし、同じ役割の内容は既存ファイルへ追記・修正する
- 他のトピックファイルへの参照は作らない。1 ファイルだけで理解できる文章にする
