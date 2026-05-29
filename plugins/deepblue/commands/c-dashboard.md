---
name: c-dashboard
description: スキル/コマンド/エージェント使用率を個人（SQLite）とチーム（PostgreSQL）で比較する静的 HTML ダッシュボード生成。
command: /c-dashboard
---

# ダッシュボード生成

個人データ（SQLite）常時収集・PostgreSQL設定時はチームデータも収集→個人 vs チーム比較表示の静的HTMLダッシュボード生成。スキル健全性・成長候補・プロジェクト登録もここに集約する。

## s-grillme 強制起動（必須）

開始直後に s-grillme を必ず起動し、完了まで他の処理に進まない。

## 前提条件

- ローカルSQLite（`~/.devgear/mem.db`）初期化済み
- チーム比較: `settings.json` で `mem.sync.enabled: true` かつ `postgres_url` 設定済み

## 使い方

```bash
/c-dashboard # 30日・HTML出力
/c-dashboard --days 90
/c-dashboard --output ~/reports/dashboard.html
/c-dashboard --format json
```

## 実装

```bash
source "${CLAUDE_PLUGIN_ROOT}/runtime/devgear-helpers.sh"
devgear_mem_json dashboard '{"days": 30, "output": "./devgear-dashboard.html", "format": "html"}'
```

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

- `--days <n>` — 集計期間（デフォルト: 30）
- `--output <path>` — 出力先（デフォルト: /tmp/devgear-dashboard.html）
- `--format <html|json>` — 出力形式（デフォルト: html）
