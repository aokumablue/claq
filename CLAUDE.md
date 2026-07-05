# CLAUDE.md

## コードベース分析

必ずサブエージェントに依頼する

## コーディングルール

- `Python` コードに `docstring` を付ける
- 後方互換フォールバックは実装しない（古いコードは必ず削除）
- カバレッジ `100%`
- テーブル定義変更は `CREATE TABLE` を直接修正（リリース前のためマイグレーション不要）
- タスクを作業内容に応じて細分化して、その粒度で修正→検証→コミットのサイクルを繰り返す

## 作業ルール

- Python は `python3` を使う
- 変更後は `.venv` を有効化して `python3 -m pytest -q` と `ruff check plugins/bluecore/src` が成功することを確認（警告なし）
- venv は `~/.bluecore/.venv` の 1 つのみ — 本体ランタイム用（配布時は `install.sh` が作成。この開発環境では `install.sh` 未実行・手動構築）。Claude/Copilot 各キャッシュフォルダには symlink を張る
- 埋め込みモデルは静的テーブル（`~/.bluecore/models/embeddings.npy`）。`model.json` の URL から DL し `bluecore.model_build` が抽出する（torch / onnxruntime 不要）
- 新規 hook・外部呼び出し（DB/ネットワーク/DL）は非ブロッキング + ハードタイムアウト必須
- pytest をパイプする際は `set -o pipefail` 必須
- 開発中コードの CLI/モジュール実行は `PYTHONPATH=plugins/bluecore/src` を付与（venv の bluecore はプラグインキャッシュ側を解決するため）

## スコープ規律

- 明示的に言及されたファイル・ディレクトリのみ変更する
- レビュー時は読み取り専用（REVIEW ONLY — NO EDITS）
- 曖昧な数値・フォーマット（例：「3桁」→ 33桁と解釈しない）は実行前に解釈を確認する
- 変更対象ファイルが 5 件以上のリファクタリングは `/plan` で変更ファイル一覧を確定してから着手する

## 永続メモリ

- `SessionStart`: `bluecore.mem.cli context` が `<mem-context>` を注入
- `PreToolUse` / `PostToolUse`: ツール操作を記録
- `SessionEnd`: 埋め込み生成と learn ブリッジ
- DB: `~/.bluecore/mem.db`
- 実装起点: `plugins/bluecore/src/bluecore/mem/{cli,search,context,bridge}.py`
