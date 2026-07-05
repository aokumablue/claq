---
name: test-gen
description: テストコードを自動生成。デシジョンテーブル設計→ユーザー承認→実装を一気通貫で実行。デフォルトは差分ファイル、引数指定も対応。言語非依存。
command: /test-gen
---

<!-- DRY: grillme 前段（発火〜他処理に進まない）は全コマンド共通。終了条件・永続メモリ・引数は固有 -->

# テストコード生成フロー

## grillme 強制起動（必須）

開始直後に grillme スキルで共通理解を固め、完了まで他処理に進まない。完了時は合意方針を1行サマリで確認する。

## 永続メモリ

- context: SessionStart で `<mem-context>` 自動注入
- search: `test test-gen coverage decision-table {対象ファイルパス}`
- record: `{"event_type": "test-gen", "content": "Scope: {scope}. Lang: {language}. Tables: {table_count}. Tests added: {tests_added}. Coverage: before {cov_before}% → after {cov_after}%"}`

## skill 起動メカニズム

`loop-dev` は `user-invocable: false` の skill。本文で「loop-dev skill を起動」と明示することで Skill ツール経由の fork 実行で発火する。

## ステップ1: スコープ確定

スコープ確定（優先順）: 引数パス（ディレクトリ=配下全ソースファイル/ファイル=そのファイル） → `git diff --name-only HEAD`

除外: テストファイル自身、言語の設定ファイル

## ステップ2: プロジェクト検出 + ベースライン取得

1. `get_test_command(project_root)` でテストコマンドを検出
2. 言語に応じたカバレッジコマンドを選択
3. 未到達ブランチを記録
4. テスト失敗がある場合は内容を明示してユーザーに確認（修正後に続行）

## ステップ3: デシジョンテーブル設計（ファイル単位）

対象ファイルごとに:

1. ソースコードを読み込み、関数・メソッドを列挙
2. 各関数のブランチ条件（if/elif/else, 例外・エラー, ループ境界, null/None/Optional）を洗い出す
3. **関数単位**でデシジョンテーブルを作成:

```
関数: {function_name}
──────────────────────────────────────────
| # | 条件1 | 条件2 | ... | 期待値 | 備考 |
|---|-------|-------|-----|--------|------|
| 1 | ...   | ...   | ... | ...    |      |
...
```

- カバー対象: ベースラインで未到達のブランチを優先
- 目標: 最小テスト数で 100% ブランチカバレッジ達成
- 組み合わせ爆発が起きる場合は境界値分析で削減し、全ブランチをカバーする最小セットを選ぶ

4. 全関数のテーブルをまとめてユーザーに提示 → **承認を待つ**（「ok」「承認」「proceed」等で判断）

## ステップ4: loop-dev 反復実装

承認後、`loop-dev` skill を起動（必須）。`approved_plan` で plan 段を縮退（planner/architect 省略）しつつ、generate→evaluate を最大 2 反復で収束させる。

入力:

- `task` = テーブルの各行を 1 テストケースとして実装（下記の実装ルールを task に含めて引き継ぐ）
- `approved_plan` = 承認済みデシジョンテーブル（plan 段縮退で planner/architect 起動なし）
- `task_type` = `test`
- `converge_extra` = 「テーブル全行実装 + カバレッジ目標。生成テストの失敗はプロダクトコード修正で解消しない — 失敗はそのまま残しユーザー報告（収束条件から除外）」

task に含めて引き継ぐ実装ルール:

- 既存テストファイルがある場合: ベースラインの未到達ブランチに対応するテストのみ追記（重複しない）/ 存在しない場合: 言語の慣習に従ったパス・命名で新規作成
- 言語標準のモック・スタブ機能を優先（Python: unittest.mock/monkeypatch, JS: jest.mock, Go: interface-based, etc.）
- 外部ライブラリが必要な場合はユーザーに確認してから追加
- フィクスチャ・ヘルパーはテストファイル内ローカルで定義。共有が必要な場合のみ共有ファイルへ昇格
- テスト関数名はデシジョンテーブルの条件を反映した命名
- 言語に応じた linter を通すスタイル
- カバレッジ検証はテストファイルを個々に指定して実行（全テスト実行では時間がかかる）

loop-dev から収束 or 停止報告を受領して要約へ進む。

## ステップ5: 要約

```
Test Generation
──────────────────────────────
Scope:      {n} files  ({language})
Tables:     {table_count} (functions)
Tests added:{tests_added}
Coverage:   {cov_before}% → {cov_after}%
Iterations: {n}/2
Commits:    {hashes or "none (理由)"}
──────────────────────────────
Gate: PASS / BLOCKED ({reason})
```

Iterations / Commits は loop-dev の箱形出力（Loop-Dev Result）から転記する。

## ルール

- テストファイル・設定ファイルは対象外
- 重複テストを生成しない（既存カバー済みブランチはスキップ）
- デシジョンテーブル承認前に実装を始めない
- テスト失敗は自動修正しない（仕様の不明点を示すため）
- 外部ライブラリ追加はユーザー確認必須

## 引数

- 位置 #1: `[ファイルパス or ディレクトリ]`（省略時: 変更差分のソースファイル）
