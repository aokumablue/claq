---
name: refactor-prep
description: リファクタ着手前に対象分割・依存可視化・実行前テストセットを最小コストで確定する事前準備スキル。
context: fork
user-invocable: false
---

# リファクタ事前準備

`refactor` 実行前に対象分割・依存関係・検証テストを固定し手戻りを減らす。

## 手順

1. **対象確定**（優先順）: 渡されたスコープリスト → 引数パス（ディレクトリ=配下全ファイル/ファイル=そのファイル） → `git diff --name-only HEAD`
2. **分割**: 同時変更が必要な塊でグループ化。依存が薄いグループを先行処理
3. **依存可視化**: import/参照関係確認→グループ間依存順明示。循環・高リスク境界（公開API・外部I/O）を先に記録
4. **テストセット確定**: baseline（全体）・グループ単位・final gate（テスト+lint）

## 出力

```
Refactor Preflight
──────────────────────────────
Scope:      {n} files
Groups:     {g1}, {g2}, ...
Dependencies:
  - {g2} depends on {g1}
Test Set:
  - baseline: {cmd}
  - group: {cmds}
  - final: {cmds}
──────────────────────────────
```

## ルール

- 既存テスト/lintのみ使用
- 依存不明は単独グループ化
- 公開APIを含む変更は最終グループ
- baseline/group/final に載せるコマンドは実在確認（テストファイル・ランナー設定を Grep/Read で確認）してから出力。確認できないコマンドは Test Set に載せず『検証手段なし』と明記

## 永続メモリ

search: `refactor preflight scope split dependency testset`
record: `{"event_type":"refprep","content":"Scope:{scope}. Groups:{groups}. TestSet:{tests}"}`
