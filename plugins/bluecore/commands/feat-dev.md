---
name: feat-dev
description: 新機能開発統括。発見→探索→loop-dev 反復実装（plan/generate/evaluate 最大2反復）→サマリー。専門エージェント連携で一気通貫。新機能実装・機能拡張・中規模リファクタリング時に使用。
command: /feat-dev
---

<!-- DRY: grillme 前段（発火〜他処理に進まない）は全コマンド共通。終了条件・永続メモリ・引数は固有 -->

# 機能開発フロー

新機能を発見から納品サマリーまで直線遂行。実装は loop-dev skill に委譲する。

## grillme 強制起動（必須）

開始直後に grillme スキルで共通理解を固め、完了まで他処理に進まない。完了時は合意方針を1行サマリで確認する。

## 永続メモリ

- context: SessionStart で `<mem-context>` 自動注入
- search: `feat-dev workflow {feature}` / `phase blocker feature`
- record: `{"event_type": "feat-dev", "content": "Feature: {name}. Iter: {n}/2. Files: {n}. Tests: {n}"}`

## skill 起動メカニズム

`loop-dev` は `user-invocable: false` の skill。本文で「loop-dev skill を起動」と明示することで Skill ツール経由の fork 実行で発火する。

## ステップ1: 発見 + 探索（並列）

要求抽出・成功条件明確化（曖昧 → 利用側確認）後、`bluecore:explorer` を **2 並列同時起動**し、結果マージ後に次段階へ:

- `bluecore:explorer` A: 既存構造・命名規約・類似実装調査
- `bluecore:explorer` B: 影響範囲・依存・現行テスト実態調査

マージ済み探索結果は再取得しない（既取得情報の再探索・重複調査を避ける）。

## ステップ2: 残分岐確認

grillme 済み前提。探索結果により**新たに発生した**分岐のみ確認する（推奨回答付き）。新規分岐がゼロなら本ステップは省略可。

## ステップ3: loop-dev 反復実装

`loop-dev` skill を起動（必須）。plan→generate→evaluate を最大 2 反復で収束させる。

入力:

- `task` = 合意済み要件
- `task_type` = `feature`
- `converge_extra` = ステップ1 の成功条件から導出

loop-dev から収束 or 停止報告を受領して次段階へ。

## ステップ4: サマリー

変更ファイル/追加テスト/残課題を一覧化し、loop-dev の箱形出力（Loop-Dev Result）を転記する。報告には loop-dev の箱形出力等の一次証跡の転記のみを用い、未確認事項を完了として報告しない:

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
