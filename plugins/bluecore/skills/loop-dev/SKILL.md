---
name: loop-dev
description: 合意済み要件を受けて plan→generate→evaluate を最大2反復で収束させる実装ループ。feat-dev/bugfix/refactor/test-gen/plan からの委譲専用。
context: fork
user-invocable: false
---

# 実装反復ループ

合意済み要件を入力に plan→generate→evaluate を最大 2 反復で回し、テスト green + blocker ゼロで収束させる。

## 前提（grillme 済み入力契約）

再 grillme 禁止。要件は呼び出し元コマンドで合意済みであり、fork のため対話コストが高い。

入力契約:

- `task`: 合意済み要件（1〜3 文）
- `task_type`: `feature` | `bugfix` | `test` | `refactor-fix`
- `approved_plan`（任意）: 渡された場合は反復1の plan 段を縮退し planner/architect 起動を省略、タスク割当のみ行う
- `converge_extra`（任意）: 追加の収束条件
- `commit`: 既定 `true`

入力に不明点があっても質問で停止しない。反復1 plan で仮決定し、出力の Assumptions に記載する。

## 手順

### 反復1（重量反復: 一発収束を狙う）

1. **plan（並列）**: `bluecore:planner` と `bluecore:architect` 決定モードを同時起動し、結果をマージ。分業: planner = 手順分解・依存関係・複雑度見積もり / architect = 構造影響・技術リスク・単一ブループリント確定。`approved_plan` があれば両者省略
2. **generate**: 下表でエージェントをルーティング

   | 作業内容 | 担当エージェント |
   |---|---|
   | コード追加を伴う feature/bugfix/test | `bluecore:tdd-writer` |
   | 可読性・重複整理 | `bluecore:simplifier` |
   | 未使用コード削除 | `bluecore:dead-code-cleaner` |
   | 性能改善 | `bluecore:perf-optimizer` |

   生成直後に自己検証必須: 検出済みテストコマンド + linter（本リポジトリなら `python3 -m pytest -q` + `ruff check plugins/bluecore/src`）を実行し、red なら evaluate に進む前に同一 generate 内で修正
3. **evaluate（条件付き並列）**: `bluecore:reviewer` 必須。認証/ユーザー入力/シークレット/API エンドポイント/支払いに触れる変更のみ `bluecore:security-auditor` を並列追加
4. **収束判定**: テスト+lint green かつ evaluate blocker（CRITICAL/HIGH）ゼロ かつ `converge_extra` 充足 → 収束
5. checkpoint 更新 + green コミット

### 反復2（修正専用: スコープ拡張禁止）

1. plan 省略。反復1の evaluate blocker を修正タスクへ直変換
2. **generate**: 各 blocker の修正前に根本原因を 1 行で明記（対症療法パッチ禁止。反復履歴行の rootcause に記録）。blocker 該当箇所のみ修正（新機能追加・リファクタ拡大禁止）+ 自己検証
3. **circuit breaker 判定**: 自己検証結果を反復1のシグネチャと照合（`## circuit breaker` 参照）。hard trigger 時は evaluate をスキップ
4. **evaluate**: reviewer 再実行 — 前回 blocker の解消確認のみに限定（新規指摘の掘り起こし禁止）
5. 収束判定 → checkpoint 更新 → green コミット

### 上限超過時

反復2 で未収束なら checkpoint を `completed: false` で保存し、残 blocker 一覧 + 推奨次アクションを出力してユーザー報告・停止。

## circuit breaker

- 照合: 反復1の checkpoint 反復履歴に記録済みのシグネチャ（定義は `../checkpoint/SKILL.md` が単一情報源。本ファイルで再定義しない）と反復2 generate 自己検証結果を文字列完全一致で比較
- hard trigger: 同一 pytest nodeid が反復2 の自己検証でも red → evaluate をスキップし即エスカレーション・停止（反復履歴に `result=circuit-break` を記録、green コミットしない）
- soft trigger: blocker シグネチャ一致はエスカレーション材料のみ（それ単独では停止しない）

## checkpoint 連携

Skill ネスト発火は使わない。checkpoint skill のフォーマット（`skills/checkpoint/SKILL.md` 参照）に従い loop-dev 自身が直接読み書きする。

- 保存先: `~/.bluecore/session-data/checkpoint-<YYYY-MM-DD>-<task-slug>.md`
- 反復1 開始前に新規作成（`completed: false`）
- 各反復の収束判定後に完了ステップ・変更済みファイルを更新し、`## 反復履歴` へ 1 行追記 + State Rot 除去（フォーマット・ルールは `../checkpoint/SKILL.md`）
- 収束時 `completed: true`（次セッション自動注入を停止）
- 上限超過停止時は `completed: false` のまま「再開コンテキスト」に残 blocker を記載

## コミット方針

- 各反復の収束判定 green 後に自動コミット（メッセージ: 変更要約 1 行 + 反復番号）
- コミット前に対象リポジトリの CLAUDE.md / AGENTS.md / CONTRIBUTING.md のコミット禁止・ブランチ規約を確認。禁止時はコミットせず出力に「未コミット（理由）」
- `--no-verify` 等のフックバイパス禁止
- コミット失敗（品質ガード reject 等）は未収束扱いにしない（収束条件はテスト green、コミットは付帯動作）
- `commit: false` 指定時はスキップ

## 出力

```
Loop-Dev Result
──────────────────────────────
Task:        {task}
Iterations:  {1|2} / 2
Converged:   YES / NO (stopped)
Tests:       PASS / FAIL
Lint:        PASS / FAIL
Blockers:    {n} remaining
Commits:     {hashes or "none (理由)"}
Checkpoint:  {path} (completed: {true|false})
Assumptions: {仮決定事項 or "-"}
──────────────────────────────
```

## ルール

- 再 grillme 禁止 / 質問で停止しない
- 3 反復目突入禁止（「あと少しで直る」判断でも上限厳守）
- 反復2 のスコープは反復1 の blocker のみ
- 自己検証（テスト+lint）を evaluate より前に必ず実行（evaluate に red コードを渡さない）
- 後方互換フォールバック禁止・古いコード削除
- レート制限 90% 超で次反復に進まず checkpoint 保存してユーザー確認
- 収束 gate の最終権限は loop-dev の evaluate。委譲先エージェントが独自 gate を持つ場合（例: refactor-orchestrator の final gate）も loop-dev 判定を正とする
- generate 委譲先エージェントの Agent 再委譲は 1 段まで（多層ネストによるコンテキスト消費と収束遅延の防止）

## 永続メモリ

search: `loop-dev iteration blocker converge {task キーワード}`
record: `{"event_type":"loop-dev","content":"Task:{task}. Iter:{n}/2. Converged:{y/n}. Blockers:{n}. Commits:{n}"}`
