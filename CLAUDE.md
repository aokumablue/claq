# CLAUDE.md

## コーディングルール

- `Python` コードに `docstring` を付ける
- 後方互換フォールバックは実装しない（古いコードは必ず削除）
- カバレッジ `100%`
- テーブル定義変更は `CREATE TABLE` を直接修正（リリース前のためマイグレーション不要）

## 作業ルール

- Python は `python3` を使う
- 変更後は `.venv` を有効化して `python3 -m pytest -q` と `ruff check plugins/bluecore/src` が成功することを確認（警告なし）
- venv は `~/.bluecore/.venv` の 1 つのみ — 本体ランタイム用（配布時は `install.sh` が作成。この開発環境では `install.sh` 未実行・手動構築）。Claude/Copilot 各キャッシュフォルダには symlink を張る
- 埋め込みモデルは静的テーブル（`~/.bluecore/models/embeddings.npy`）。`model.json` の URL から DL し `bluecore.model_build` が抽出する（torch / onnxruntime 不要）
- 新規 hook・外部呼び出し（DB/ネットワーク/DL）は非ブロッキング + ハードタイムアウト必須
- pytest をパイプする際は `set -o pipefail` 必須
- 開発中コードの CLI/モジュール実行は `PYTHONPATH=plugins/bluecore/src` を付与（venv の bluecore はプラグインキャッシュ側を解決するため）

## データモデルの前提

- PostgreSQL（`sql/pg_setup.sql`）は**チーム共有の蓄積メモリ**。全メンバーが全行を READ できるのが設計意図（`team_search` / ダッシュボード集計は他者行の横断 READ に依存）。メンバー間に機密境界は無い
- したがって RLS の目的は**機密性（READ 隔離）ではなく完全性（WRITE 所有）**。守るのは「他者の origin_user を詐称した書き込み」「他者行の上書き・削除」の防止のみ。READ を `origin_user` で絞るポリシー・提案は設計意図に反する（監査で「個人データが全員に見える」を漏洩と見なすのは誤り＝仕様）
- `origin_user` は `git config user.name`（`core_utils.py` `get_git_user_name`、自己申告）。ハードな認証境界ではなく多層防御

