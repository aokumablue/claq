---
name: dashboard
description: スキル/コマンド/エージェント使用率を個人（SQLite）とチーム（PostgreSQL）で比較する静的 HTML ダッシュボード生成。
command: /dashboard
---

<!-- DRY: grillme 前段（発火〜他処理に進まない）は全コマンド共通。終了条件・永続メモリ・引数は固有 -->

# ダッシュボード生成

個人データ（SQLite）常時収集・PostgreSQL設定時はチームデータも収集→個人 vs チーム比較表示の静的HTMLダッシュボード生成。スキル健全性・成長候補・プロジェクト登録もここに集約する。

## grillme 強制起動（必須）

開始直後に grillme スキルで共通理解を固め、完了まで他処理に進まない。完了時は合意方針を1行サマリで確認する。

## 永続メモリ

- context: SessionStart で `<mem-context>` 自動注入
- search: `dashboard usage skill-health growth`
- record: `{"event_type": "dashboard", "content": "Period: {days}d. Output: {output}. Format: {format}"}`

## 前提条件

- ローカルSQLite（`~/.bluecore/mem.db`）初期化済み
- チーム比較: `settings.json` で `mem.sync.enabled: true` かつ `postgres_url` 設定済み

## ステップ1: 前提確認

- `~/.bluecore/mem.db` の存在確認（未初期化なら SessionStart hook を1度走らせる）
- PostgreSQL 設定確認（任意、`mem.sync.enabled` 確認）

## ステップ2: データ収集 + HTML 生成

```bash
source "${CLAUDE_PLUGIN_ROOT}/runtime/bluecore-helpers.sh"
bluecore_mem_json dashboard '{"days": 30, "output": "./bluecore-dashboard.html", "format": "html"}'
```

## ステップ3: 出力提示

生成されたファイルパス・期間・集計指標サマリーを 1 行で報告。

## ダッシュボード内容

### アイテム使用率（`mem_item_runs` 実行記録ありのみ）

- スキル/コマンド/エージェント使用回数ランキング: グループ横棒（個人 vs チーム）
- 日次実行トレンド: 2系列折れ線
- アウトカム分布: ドーナツ（success/partial/failure/unknown）

### Skill Health / Growth / Projects

- スキル健全性: 7日/30日成功率、低下トレンド、保留修正
- 成長候補: 繰り返しパターン、ギャップ候補
- プロジェクト登録: インスティンクト数・観測数・最終検出時刻

### メモリ統計（PostgreSQL設定時のみ）

- ユーザー/プロジェクト別アクティビティ・ツール使用分布・日次推移・ファイル変更頻度・インスティンクト成長

## 引数

- `--days=<n>` — 集計期間（既定: 30）
- `--output=<path>` — 出力先（既定: /tmp/bluecore-dashboard.html）
- `--format=html|json` — 出力形式（既定: html）
