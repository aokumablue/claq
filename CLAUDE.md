# CLAUDE.md

## コーディングルール

- `Python` コードに `docstring` を付ける
- 後方互換フォールバックは実装しない（古いコードは必ず削除）
- カバレッジ `100%`
- テーブル定義変更は `CREATE TABLE` を直接修正（リリース前のためマイグレーション不要）

## 作業ルール

- Python は `python3` を使う
- 変更後は `.venv` を有効化して `python3 -m pytest -q` と `ruff check plugins/bluecore/src` が成功することを確認（警告なし）
- venv は `~/.bluecore/.venv` の 1 つのみ — 本体ランタイム用（`install.sh` が作成）。Claude/Copilot 各キャッシュフォルダには symlink を張る
- 埋め込みモデルは静的テーブル（`~/.bluecore/models/embeddings.npy`）。`model.json` の URL から DL し `bluecore.model_build` が抽出する（torch / onnxruntime 不要）

