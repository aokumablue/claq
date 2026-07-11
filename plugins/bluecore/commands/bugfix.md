---
name: bugfix
description: バグを再現→原因分析→最小修正→回帰防止→レビューまで一気通貫で進める。
command: /bugfix
---

<!-- DRY: grillme 前段（発火〜他処理に進まない）は全コマンド共通。終了条件・永続メモリ・引数は固有 -->

# バグ修正フロー

## grillme 強制起動（必須）

開始直後に grillme スキルで共通理解を固め、完了まで他処理に進まない。完了時は合意方針を1行サマリで確認する。

## 永続メモリ

- context: SessionStart で `<mem-context>` 自動注入
- search: `bug fix regression repro root cause verify` / `{対象ファイルパス}` / `{症状キーワード}`
- record: `{"event_type": "bugfix", "content": "Scope: {scope}. Repro: {repro}. Root cause: {root_cause}. Fix: {fix}. Tests: {tests}. Prevention: {prevention}"}`

## skill 起動メカニズム

`loop-dev` は `user-invocable: false` の skill。本文で「loop-dev skill を起動」と明示することで Skill ツール経由の fork 実行で発火する。

## ステップ1: 要件整理

1. 症状・期待動作・実際の動作を分ける
2. 再現条件・入力・環境差分・影響範囲を確認
3. 仕様バグ・設計欠陥の疑いがあれば修正前に切り分ける

## ステップ2: 再現テスト確立

1. 再現テストまたは再現手順を先に作る
2. 既存テストで失敗を確認
3. 再現できない場合は不足情報を明示して止める

## ステップ3: loop-dev 反復修正

`loop-dev` skill を起動（必須）。plan→generate→evaluate を最大 2 反復で収束させる。

入力:

- `task` = 再現テスト・原因候補を含む修正要件
- `task_type` = `bugfix`
- `converge_extra` = 「再現テスト green + 回帰テスト追加済み」

loop-dev から収束 or 停止報告を受領して記録へ進む。

## 記録テンプレート

記録対象は出所別に分離する。Tests と Loop（反復数）は loop-dev の Loop-Dev Result からの転記。Review は loop-dev の `Blockers: {n} remaining` から導出する（0 件 → PASS / 1 件以上 → BLOCKED）— Loop-Dev Result に `Review` フィールドは存在しないため転記ではなく導出。Repro/Root cause/Fix はステップ1-2（要件整理・再現テスト確立）での自己記録に基づく（loop-dev の出力契約には存在しない）。未受領項目を PASS と書かない。

```
Bug Fix
──────────────────────────────
Scope:      {scope}
Repro:      PASS / FAIL
Root cause: {root_cause}
Fix:        {fix}
Tests:      {tests}
Loop:       {n}/2
Review:     PASS / BLOCKED
──────────────────────────────
```

## ルール

- 再現テストを先に作る / 最小修正 / 回帰確認を省略しない
- 仕様バグ・設計欠陥・品質改善は `/refactor` / `/plan` / `/review` に切り分ける

## 引数

- 位置 #1: `[バグ説明 or 症状]`（省略可）
- 位置 #2: `[ファイルパス or ディレクトリ]`（省略可）
