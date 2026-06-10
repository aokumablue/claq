---
name: feat-dev
description: 新機能開発の7段階ワークフロー統括。発見→探索→質問→設計→実装→レビュー→サマリー。専門エージェント連携で一気通貫。新機能実装・機能拡張・中規模リファクタリング時に使用。
command: /feat-dev
---

<!-- DRY: grillme 前段（発火〜他処理に進まない）は全コマンド共通。終了条件・永続メモリ・引数は固有 -->

# 機能開発フロー

新機能を発見から納品サマリーまで直線遂行。各段階で専門エージェント起動。

## grillme 強制起動（必須）

開始直後に grillme スキルで共通理解を固め、完了まで他処理に進まない。完了時は合意方針を1行サマリで確認する。

## 永続メモリ

- context: SessionStart で `<mem-context>` 自動注入
- search: `feat-dev workflow {feature}` / `phase blocker feature`
- record: `{"event_type": "feat-dev", "content": "Feature: {name}. Phases: {done}/7. Files: {n}. Tests: {n}"}`

## ステップ1: 発見

要求抽出・成功条件明確化。曖昧 → 利用側確認。

## ステップ2: 探索（並列）

以下3エージェントを**同時起動**し、結果マージ後に次段階へ:

- `bluecore:explorer` A: 既存構造・命名規約・類似実装調査
- `bluecore:explorer` B: 影響範囲・依存関係・破壊リスク調査
- `bluecore:explorer` C: 現行テストカバレッジ・テストパターン調査

## ステップ3: 質問

探索マージ結果を元に未解決分岐を grillme スタイルで徹底質問（推奨回答付き）。

## ステップ4: 設計（並列）

以下2エージェントを**同時起動**し、結果マージ後に次段階へ:

- `bluecore:architect` A: 決定モード → 単一ブループリント確定
- `bluecore:perf-optimizer` B: パフォーマンス要件・ボトルネック予測（読み取り専用）

## ステップ5: 実装

`tdd` skill 自動発火（`user-invocable: false`、description マッチで起動）または `bluecore:tdd-writer` 明示起動。RED→GREEN→REFACTOR 遵守。

## ステップ6: レビュー（並列）

以下2エージェントを**同時起動**し、両結果が Approve または Warning のみのとき採用:

- `bluecore:reviewer` A: 品質・設計・保守性
- `bluecore:security-auditor` B: セキュリティ・脆弱性

## ステップ7: サマリー

変更ファイル/追加テスト/残課題を一覧化:

```
### 変更ファイル
- path — 変更内容

### 追加テスト
- path:fn — カバー範囲

### 残課題
- ...
```

**段階飛ばし禁止**: 探索スキップ → 既存パターン無視 → 重複実装発生。

## 制約

- 既存拡張 > 新規作成
- テスト実行・緑必須（pytest / jest / go test 等）
- 後方互換フォールバック禁止 → 古コード削除

## 引数

- 位置 #1: `[機能説明]`（省略時: 直前の会話文脈から要件抽出）
