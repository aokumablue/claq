---
name: refactor
description: コードを一気通貫でリファクタリング。差分・指定パスの両方に対応。性能劣化防止・デッドコード排除・可読性改善・レビューを実行。
command: /refactor
---

<!-- DRY: grillme 前段（発火〜他処理に進まない）は全コマンド共通。終了条件・永続メモリ・引数は固有 -->

# 統合リファクタリング

## grillme 強制起動（必須）

開始直後に grillme スキルで共通理解を固め、完了まで他処理に進まない。完了時は合意方針を1行サマリで確認する。

## 永続メモリ

- context: SessionStart で `<mem-context>` 自動注入
- search: `refactor clean simplify perf review {対象ファイルパス}` / `critical high blocker`
- record: `{"event_type": "refactor", "content": "Scope: {scope}. Clean: {cleaned}. Simplify: {simplified}. Perf: {perf_fixed}. Blockers: {blockers}"}`

## skill 起動メカニズム

`refactor-prep` / `refactor-rollback` は `user-invocable: false` の skill。description マッチで Claude Code が Skill ツール経由で fork 実行する。本コマンドのステップ1で「refactor-prep skill を起動」「refactor-rollback skill を起動」と明示することで発火する。

## ステップ1: preflight（スコープ確定 + 実行準備）

スコープ確定（優先順）: 引数パス（ディレクトリ=配下全ファイル/ファイル=そのファイル） → `git diff --name-only HEAD`

着手前に実行:

- `refactor-prep` skill 起動（必須）: 対象分割・依存可視化・テストセット確定
- `refactor-rollback` skill 起動（必須）: ファイル単位リバート計画（Rollback Blueprint）生成
- `bluecore:refactor-orchestrator` 起動（必須）: clean/simplify/perf/review の実行順・並列制御

`deps.from` / `deps.to` は `groups` 配列のインデックスを指す。

`refactor-rollback` 運用規約:
- `CAUTION` ファイルは自動適用せず最終要約に記録
- `Skip Rules` は `{file, reason, required_action}` で出力し処理対象から除外
- `deps_order` はトポロジカル順で解決し、復旧時は逆順で適用

## ステップ2: baseline

1. テスト・linter を実行し基準を取得
2. 既存失敗を記録し新規失敗判定に使用
3. 基準取得不能なら実装を止め、原因解消後に再開

## ステップ3: clean（`refactor-orchestrator` → `bluecore:dead-code-cleaner`）

デッドコード削除。各ファイル適用ごとにテスト実行→失敗時は `git checkout -- <file>` で単ファイルリバートして継続。

`--mode=clean` 指定時はステップ3のみ実行して終了。

## ステップ4: simplify（並列, `refactor-orchestrator` → `bluecore:simplifier`）

グループ化して**同時起動**。可読性・一貫性・保守性を改善（機能保持前提）。グループ完了ごとにテスト→失敗時はファイル単位リバート。

`--mode=simplify` 指定時はステップ4のみ実行して終了（その後ステップ7 final gate へ直行）。

## ステップ5: perf（`refactor-orchestrator` → `bluecore:perf-optimizer`）

simplify 全グループ完了後に開始。不要計算・重複I/O・N+1・過剰メモリアロケーションを優先改善。変更ごとにテスト→失敗時はファイル単位リバート。

## ステップ6: review + secure（並列, `bluecore:refactor-orchestrator` から委譲）

以下を**同時起動**し結果を統合:
- `bluecore:reviewer`: 品質・設計・保守性
- `bluecore:security-auditor`: セキュリティ・脆弱性

## ステップ7: final gate

1. テストと linter を再実行
2. **CRITICAL または HIGH** が1件でもあればブロック
3. 失敗変更はファイル単位リバートし再検証
4. 全通過のみ完了

## ステップ8: 要約

orchestrator の出力テンプレート（`../agents/refactor-orchestrator.md` 参照）をそのまま提示する。

Issues は `bluecore:reviewer` と `bluecore:security-auditor` の統合件数。

## ルール

- 既定スコープは変更差分。パス/ディレクトリ指定で任意ファイルにも対応
- 失敗時は必ずファイル単位リバート
- CRITICAL/HIGH が残る状態では承認・コミットしない
- 機能変更禁止（WHAT不変）。挙動変更の疑義がある変更は要確認として報告
- 安全性に疑義がある変更はスキップし最終要約に記載
- サブエージェント委譲必須（`bluecore:refactor-orchestrator` 統括 → `bluecore:dead-code-cleaner` / `bluecore:simplifier` / `bluecore:perf-optimizer` / `bluecore:reviewer` / `bluecore:security-auditor`）

## 引数

- 位置 #1: `[ファイルパス or ディレクトリ]`（省略時: 変更差分）
- `--mode=clean|simplify`: 部分モード指定（省略時: clean→simplify→perf→review 全段階実行）
