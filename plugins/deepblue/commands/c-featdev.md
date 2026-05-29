---
name: c-featdev
description: 新機能開発の7段階ワークフロー統括。発見→探索→質問→設計→実装→レビュー→サマリー。devgear:a-explore/a-arch/a-tdd/a-review 連携で一気通貫。新機能実装・機能拡張・中規模リファクタリング時に使用。
command: /c-featdev
---

# 機能開発フロー

新機能を発見から納品サマリーまで直線遂行。各段階で専門エージェント起動。

## 7段階

1. **発見** — 要求抽出・成功条件明確化。曖昧 → 利用側確認
2. **探索（並行）** — 以下3つを同時起動し、結果マージ後に次段階へ:
   - `devgear:a-explore` A: 既存構造・命名規約・類似実装調査
   - `devgear:a-explore` B: 影響範囲・依存関係・破壊リスク調査
   - `devgear:a-explore` C: 現行テストカバレッジ・テストパターン調査
3. **質問** — 探索マージ結果を元に未解決分岐を `s-grillme` スタイルで徹底質問（推奨回答付き）
4. **設計（並行）** — 以下2つを同時起動し、結果マージ後に次段階へ:
   - `devgear:a-arch` A: 決定モード → ブループリント取得
   - `devgear:a-perf` B: パフォーマンス要件・ボトルネック予測（読み取り専用）
5. **実装** — `devgear:a-tdd` RED→GREEN→REFACTOR 遵守
6. **レビュー（並行）** — 以下2つを同時起動し、両結果が Approve または Warning のみのとき採用:
   - `devgear:a-review` A: 品質・設計・保守性
   - `devgear:a-secure` B: セキュリティ・脆弱性
7. **サマリー** — 変更ファイル/追加テスト/残課題を一覧化

**段階飛ばし禁止**: 探索スキップ → 既存パターン無視 → 重複実装発生。

## 出力形式

```
### 変更ファイル
- path — 変更内容

### 追加テスト
- path:fn — カバー範囲

### 残課題
- ...
```

## 制約

- 既存拡張 > 新規作成
- テスト実行・緑必須（pytest / jest / go test 等）
- 後方互換フォールバック禁止 → 古コード削除

## 永続メモリ

`<mem-context>` 注入で起動。
search: `feature-dev workflow {feature}` / `phase blocker feature`
record: `{"event_type": "feature-dev", "content": "Feature: {name}. Phases: {done}/7. Files: {n}. Tests: {n}"}`
