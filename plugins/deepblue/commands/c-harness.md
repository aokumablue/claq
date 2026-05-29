---
name: c-harness
description: ハーネス監査→改善を一気通貫で実行。スコアカード取得→トップ3改善適用→改善後スコア報告。
command: /c-harness
---

# ハーネス管理

## s-grillme 強制起動（必須）

開始直後に s-grillme を必ず起動し、完了まで他の処理に進まない。

## 永続メモリ

`<mem-context>` 注入で起動。
search: `harness audit score` / `harness config optimization audit` (days: 90)
record: `{"event_type": "harness-run", "content": "Harness: Score {before} -> {after}. Changes: {changes}"}`

## 使い方

```bash
/c-harness
/c-harness --audit-only       # スコアカード出力のみ
/c-harness skills --format json
/c-harness --scope hooks --root /path/to/repo
```

`scope` は位置引数でも `--scope` でも指定可。既定値は `repo`。

## 実行フロー

1. `devgear_run devgear.ci.harness_audit` でベースラインスコア取得・出力
2. トップ3アクションを特定
3. 最小限・元に戻せる設定変更を提案・適用・検証
4. 再度 `devgear_run devgear.ci.harness_audit` で改善スコア報告
5. 変更前後の差分を出力

`--audit-only` はステップ1のみで終了。

## 実行エンジン

```bash
source "${CLAUDE_PLUGIN_ROOT}/runtime/devgear-helpers.sh"
devgear_run devgear.ci.harness_audit <scope> --format <text|json> [--root <path>]
```

スコアリングはこのスクリプトのみを根拠とし、手動採点は行わない。

ルーブリック版: `2026-03-30` — 固定カテゴリ7個（各0〜10に正規化）:
ツール網羅性 / 文脈効率 / 品質ゲート / メモリ永続性 / 評価網羅性 / セキュリティガードレール / コスト効率

## 制約

- 測定可能効果を持つ小変更優先
- クロスプラットフォーム動作保持・脆弱シェルクォーティング導入禁止
- `checks[]` と `top_actions[]` に含まれる正確なファイルパスを残す
- スクリプト出力をそのまま使い、手動で再採点しない

## 出力仕様

1. ベースライン `overall_score` と `max_score`（`repo` では70）
2. カテゴリ別スコアと指摘
3. 失敗チェックと正確なファイルパス
4. 上位3件のアクション（`top_actions`）と適用内容
5. 改善後スコアカード（`--audit-only` 以外）
6. 変更前後の差分サマリー

## 引数

- `[scope]`: `repo|hooks|skills|commands|agents`（既定: `repo`）
- `--format text|json` / `--root <path>` / `--audit-only`
