# CLAUDE.md

## コード

- Python に docstring を付ける
- 失敗はそのまま呼び出し元へ伝える。古いコードと後方互換の分岐は削除する
- 入力の拒否・上限の検査は、要件の文・攻撃者と入口・再現できる失敗を引用できるときに書く。失敗の後に続行する経路は、続行を決めた文（要件・利用者の指示・外部 API 契約）を引用できるときに書く。保護フックはエージェント自身の誤操作も止める対象に含める
- 要件の文は、依頼・仕様・ADR に依頼者の決定として書かれた文を指す。再現できる失敗は、要件に書かれた振る舞いが崩れることをテストか手順で示せるものを指す
- 消すのは、到達しない・入口で検証済みと示せたものだけ。通る経路のフォールバックを消すのは仕様変更として扱う
- 報告の「提案」は採否を利用者が決める項目で、既定では実施しない。「すべて対応」「ベキ論で対応」は、各項目に実施かクローズかを決めて返す指示として読み、上の根拠を引用できる項目を実施し、それ以外は理由を添えてクローズする
- レビューと監査の範囲は依頼が決め、利用者が指定したときに広げる
- カバレッジ 100%
- テーブル定義は `CREATE TABLE` を直接修正する（マイグレーションしない）

## 検証

- Python は `python3` を使う
- 変更後はリポジトリ直下の `.venv` を有効化し、リポジトリ直下で `python3 -m pytest -q --cov`（カバレッジ 100% もここで判定）と `ruff check .` を通す
- pytest をパイプするときは `set -o pipefail`

## ランタイム前提

- ランタイムは venv を作らない。開発用 venv はリポジトリ直下 `.venv`（`python3 -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"`）
- 配布物は `plugins/claq` をそのまま複製したもの。テスト（`tests/`）・開発ツール（`tools/claq_dev`）・開発用 `pyproject.toml` はリポジトリ直下に置き、配布物へ入れない
- 対応 Python は 3.12 以上。未満は保護フックを fail-open（exit 0）する
- `hooks.json` は裸の `python3` を呼ばない。全エントリが `runtime/claq-hook`。Windows は `command` と `powershell` を同一モジュール・同一引数で両方宣言する
- hook と hook が起動する外部プロセスは非ブロッキング + ハードタイムアウト
- ランタイム依存は標準ライブラリのみ
- `~/.claq/env.sh` は POSIX 専用。Windows では SessionStart が注入する `runtime/claq-hook.cmd` を使う

## データ

- 知識とセッションの永続化は `~/.claq/mem.db` のみ。テーブルは `repos` / `knowledge` / `sessions`。`~/.claq` は 0700、`mem.db` は 0600。Windows では DACL を変えない
- `~/.claq/session-data/` には checkpoint（`checkpoint-*.md`）と圧縮の記録（`compaction-log.txt`）をファイルで置く
- agent 由来カードは常に `source=agent` / `status=pending`。注入されるのは `active` のみ（人間が `promote`）
- `list` / `search` は 1 行。`body` は `show` のみ。0 件は無出力

## ADR

- `docs/adr/` には現行の状態と、現在も有効な判断基準だけを書く
- 「過去にこうだった」経緯・改訂履歴・バージョン番号・採番 ID は置かない
- ファイル名に連番は振らない。役割ごとに 1 ファイルとし、同じ役割の内容は既存ファイルへ追記・修正する
- 他のトピックファイルへの参照は作らない。1 ファイルだけで理解できる文章にする
