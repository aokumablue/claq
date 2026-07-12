---
name: harness
description: ハーネス監査→改善を一気通貫で実行。スコアカード取得→トップ3改善適用→改善後スコア報告。
command: /harness
---

<!-- DRY: grillme 前段（発火〜他処理に進まない）は全コマンド共通。終了条件・永続メモリ・引数は固有 -->

# ハーネス管理

## grillme 強制起動（必須）

開始直後に grillme スキルで共通理解を固め、完了まで他処理に進まない。完了時は合意方針を1行サマリで確認する。

## 永続メモリ

- context: SessionStart で `<mem-context>` 自動注入
- search: `harness audit score` / `harness config optimization audit` (days: 90)
- record: `{"event_type": "harness-run", "content": "Harness: Score {before} -> {after}. Changes: {changes}"}`

## 使い方

```bash
/harness
/harness --audit-only       # スコアカード出力のみ
/harness skills --format json
/harness --scope hooks --root /path/to/repo
```

`scope` は位置引数でも `--scope` でも指定可。既定値は `repo`。

## ステップ1: ベースライン取得

```bash
source "${CLAUDE_PLUGIN_ROOT}/runtime/bluecore-helpers.sh"
bluecore_run bluecore.ci.harness_audit <scope> --format <text|json> [--root <path>]
```

スコアカードを出力。`--audit-only` は /harness レベルの制御フラグであり `bluecore_run` へ渡さない。指定時はここで終了。

スコアリングはこのスクリプトのみを根拠とし、手動採点は行わない。

ルーブリック版: `2026-03-30` — 固定カテゴリ7個（各0〜10に正規化）:
ツール網羅性 / 文脈効率 / 品質ゲート / メモリ永続性 / 評価網羅性 / セキュリティガードレール / コスト効率

## ステップ2: トップ3アクション特定

`top_actions[]` から最も効果が高い3件を抽出。各アクションは `checks[]` の失敗チェックに紐付く正確なファイルパス付き。

## ステップ3: harness-tuner による改善適用

`bluecore:harness-tuner` を起動。ベースラインJSONとトップ3アクションを渡し、信頼性・コスト・スループット最適化を委譲。

harness-tuner は:
- ハーネス設定（hooks.json / settings.json 等）の最小限の変更を提案
- 元に戻せる設定変更のみを適用
- 適用前後の影響範囲を要約

## ステップ4: 改善後スコア

```bash
source "${CLAUDE_PLUGIN_ROOT}/runtime/bluecore-helpers.sh"
bluecore_run bluecore.ci.harness_audit <scope> --format <text|json> [--root <path>]
```

ステップ1と同条件で再採点。

## ステップ5: 差分要約

変更前後の差分・カテゴリ別スコア変化・harness-tuner が適用した変更内容を出力。

## 制約

- 測定可能効果を持つ小変更優先
- クロスプラットフォーム動作保持・脆弱シェルクォーティング導入禁止
- `checks[]` と `top_actions[]` に含まれる正確なファイルパスを残す
- スクリプト出力をそのまま使い、手動で再採点しない

## 出力仕様

1. ベースライン `overall_score` と `max_score`（`repo` では70）
2. カテゴリ別スコアと指摘
3. 失敗チェックと正確なファイルパス
4. 上位3件のアクション（`top_actions`）と harness-tuner 適用内容
5. 改善後スコアカード（`--audit-only` 以外）
6. 変更前後の差分サマリー

## 引数

- 位置 #1: `[scope]` = `repo|hooks|skills|commands|agents`（既定: `repo`）
- `--scope=<scope>`: 位置引数の別名（互換維持）
- `--format=text|json`（既定: `text`）
- `--root=<path>`: ルートディレクトリ指定
- `--audit-only`: ステップ1のみで終了（/harness レベルの制御フラグ。`bluecore_run` へ渡さない）
