# CLAUDE.md

## コードベース分析

必ずサブエージェントに依頼する

## コーディングルール

- `Python` コードに `docstring` を付ける
- 後方互換フォールバックは実装しない（古いコードは必ず削除）
- カバレッジ `100%`
- テーブル定義変更は `CREATE TABLE` を直接修正（リリース前のためマイグレーション不要）

## 作業ルール

- Python は `python3` を使う
- 変更後は `.venv` を有効化して `python3 -m pytest -q` と `ruff check plugins/deepblue/src` が成功することを確認（警告なし）
- venv は 2 種類。両方とも `~/.deepblue/` 配下に置く
  - `~/.deepblue/.venv` — 本体ランタイム用（`install.sh` が作成）。Claude/Copilot 各キャッシュフォルダには symlink を張る
  - `~/.deepblue/.venv-modelbuild` — ONNX ビルド専用（初回のみ自動作成）。torch pickle RCE リスクと 5GB 配布回避のため本体 venv とは分離する

## スコープ規律

- 明示的に言及されたファイル・ディレクトリのみ変更する
- レビュー時は読み取り専用（REVIEW ONLY — NO EDITS）
- 曖昧な数値・フォーマット（例：「3桁」→ 33桁と解釈しない）は実行前に解釈を確認する
- 変更対象ファイルが 5 件以上のリファクタリングは `/plan` で変更ファイル一覧を確定してから着手する

## 永続メモリ

- `SessionStart`: `deepblue.mem.cli context` が `<mem-context>` を注入
- `PreToolUse` / `PostToolUse`: ツール操作を記録
- `SessionEnd`: 埋め込み生成と learn ブリッジ
- DB: `~/.deepblue/mem.db`
- 実装起点: `plugins/deepblue/src/deepblue/mem/{cli,search,context,bridge}.py`
