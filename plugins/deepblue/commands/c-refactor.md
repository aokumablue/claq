---
name: c-refactor
description: コードを一気通貫でリファクタリング。差分・指定パスの両方に対応。性能劣化防止・デッドコード排除・可読性改善・レビューを実行。
command: /c-refactor
---

# 統合リファクタリング

## s-grillme 強制起動（必須）

開始直後に s-grillme を必ず起動し、完了まで他の処理に進まない。

## 永続メモリ

search: `refactor clean simplify perf review {対象ファイルパス}` / `critical high blocker`
record: `{"event_type": "refactor", "content": "Scope: {scope}. Clean: {cleaned}. Simplify: {simplified}. Perf: {perf_fixed}. Blockers: {blockers}"}`

## ステップ1: preflight（スコープ確定 + 実行準備）

スコープ確定（優先順）: 引数パス（ディレクトリ=配下全ファイル/ファイル=そのファイル） → `git diff --name-only HEAD`

着手前に実行:

- `s-refprep`（必須）: 対象分割・依存可視化・テストセット確定
- `s-refrb`（必須）: ファイル単位リバート計画（Rollback Blueprint）生成
- `devgear:a-reforch`（必須）: clean/simplify/perf/review の実行順・並列制御

`deps.from` / `deps.to` は `groups` 配列のインデックスを指す。

`s-refrb` 運用規約:
- `CAUTION` ファイルは自動適用せず最終要約に記録
- `Skip Rules` は `{file, reason, required_action}` で出力し処理対象から除外
- `deps_order` はトポロジカル順で解決し、復旧時は逆順で適用

## ステップ2: baseline

1. テスト・linter を実行し基準を取得
2. 既存失敗を記録し新規失敗判定に使用
3. 基準取得不能なら実装を止め、原因解消後に再開

## ステップ3: clean（`a-reforch` → `devgear:a-clean`）

デッドコード削除。各ファイル適用ごとにテスト実行→失敗時は `git checkout -- <file>` で単ファイルリバートして継続。

## ステップ4: simplify（並列, `a-reforch` → `devgear:a-simplify`）

グループ化して**同時起動**。可読性・一貫性・保守性を改善（機能保持前提）。グループ完了ごとにテスト→失敗時はファイル単位リバート。

## ステップ5: perf（`a-reforch` → `devgear:a-perf`）

simplify 全グループ完了後に開始。不要計算・重複I/O・N+1・過剰メモリアロケーションを優先改善。変更ごとにテスト→失敗時はリバート。

## ステップ6: review + secure（並列, `devgear:a-reforch` から委譲）

以下を**同時起動**し結果を統合:
- `devgear:a-review`: 品質・設計・保守性
- `devgear:a-secure`: セキュリティ・脆弱性

## ステップ7: final gate

1. テストと linter を再実行
2. **CRITICAL または HIGH** が1件でもあればブロック
3. 失敗変更はファイル単位リバートし再検証
4. 全通過のみ完了

## ステップ8: 要約

```
Unified Refactor
──────────────────────────────
Scope:      {n} files
Cleaned:    {cleaned} files
Simplified: {simplified} files
Perf fixed: {perf_fixed} files
Reverted:   {reverted} files
Issues:     CRITICAL {c} / HIGH {h} / MEDIUM {m} / LOW {l}
──────────────────────────────
Final Gate: PASS / BLOCKED
```

Issues は `devgear:a-review` と `devgear:a-secure` の統合件数。

## ルール

- 既定スコープは変更差分。パス/ディレクトリ指定で任意ファイルにも対応
- 失敗時は必ずファイル単位リバート
- CRITICAL/HIGH が残る状態では承認・コミットしない
- 機能変更禁止（WHAT不変）。挙動変更の疑義がある変更は要確認として報告
- 安全性に疑義がある変更はスキップし最終要約に記載
- サブエージェント委譲必須（`devgear:a-reforch` 統括 → `devgear:a-clean` / `devgear:a-simplify` / `devgear:a-perf` / `devgear:a-review` / `devgear:a-secure`）

## 引数

$ARGUMENTS: `[ファイルパス or ディレクトリ]`（省略時: 変更差分）
