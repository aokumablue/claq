---
name: c-bugfix
description: バグを再現→原因分析→最小修正→回帰防止→レビューまで一気通貫で進める。
command: /c-bugfix
---

# バグ修正フロー

## s-grillme 強制起動（必須）

開始直後に `s-grillme` を必ず起動し、共通理解が固まるまで次へ進まない。

## 永続メモリ

search: `bug fix regression repro root cause verify` / `{対象ファイルパス}` / `{症状キーワード}`
record: `{"event_type":"bugfix","content":"Scope:{scope}. Repro:{repro}. Root cause:{root_cause}. Fix:{fix}. Tests:{tests}. Prevention:{prevention}"}`

## ステップ1: 要件整理

1. 症状・期待動作・実際の動作を分ける
2. 再現条件・入力・環境差分・影響範囲を確認
3. 仕様バグ・設計欠陥の疑いがあれば修正前に切り分ける

## ステップ2: 再現テスト確立

1. 再現テストまたは再現手順を先に作る
2. 既存テストで失敗を確認
3. 再現できない場合は不足情報を明示して止める

## ステップ3: 原因分析と修正方針

1. `s-grillme` で症状を本質化し原因候補を絞る
2. 修正案を複数出し、メリット/デメリット/コストを比較
3. 最小修正を基本に、同類バグがあれば合わせて直す

## ステップ4: 修正

1. 根本原因を直接直す
2. 既存の正常系を壊さない
3. 振る舞い変更が必要なら理由と影響を明示

## ステップ5: 検証とレビュー

1. 再現テストを通す
2. 回帰テストを追加
3. 周辺の既存テストを再実行
4. `devgear:a-review` と `devgear:a-secure` で品質・安全性を確認

## 記録テンプレート

```
Bug Fix
──────────────────────────────
Scope:      {scope}
Repro:      PASS / FAIL
Root cause: {root_cause}
Fix:        {fix}
Tests:      {tests}
Review:     PASS / BLOCKED
──────────────────────────────
```

## ルール

- 再現テストを先に作る / 最小修正 / 回帰確認を省略しない
- 仕様バグ・設計欠陥・品質改善は `c-refactor` / `c-plan` / `c-review` に切り分ける

## 引数

$ARGUMENTS: `[バグ説明 or 症状] [ファイルパス or ディレクトリ]`
