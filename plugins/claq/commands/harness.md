---
name: harness
description: ハーネス監査を実行しトップ3改善案を提示する。`--apply` 指定時のみ改善適用と改善後スコア報告まで進む。
command: /harness
---

# ハーネス管理

## 永続メモリ

- 参照: SessionStart が `<claq-memory>`（`status='active'` の知識）を注入済み。追加で要るときは `. "$HOME/.claq/env.sh"` のあと `claq_run claq.mem.cli search "..."`（クエリ例 `harness audit score` / `harness config optimization audit`）→ 本文が要る key だけ `claq_run claq.mem.cli show <key>`
- 記録: 再利用可能な学びだけ `claq_mem_learn` で登録する。基準は `../skills/learn/SKILL.md` の「記録する / しない」

## 使い方

```bash
/harness                    # ベースライン取得→トップ3提示までで停止（apply しない）
/harness --audit-only       # スコアカード出力のみ
/harness --apply            # トップ3を確認なしで harness-tuner に適用させる
/harness skills --format json
/harness --scope hooks --root /path/to/repo --target-kind repo
```

`scope` は位置引数でも `--scope` でも指定できる（既定 `repo`）。`--root` と `--target-kind repo|consumer` は必須。省くと cwd から自動判定され、marketplace レイアウトの provider リポジトリ直下を consumer と誤判定しうる。`--target-kind` が自動判定と食い違えば FAIL し、誤った root を監査したまま進むのを防ぐ。

## ステップ1: ベースライン取得

```bash
. "$HOME/.claq/env.sh" || exit 127
claq_run claq.ci.harness_audit <scope> --format <text|json> --root <path> --target-kind <repo|consumer>
```

スコアカードを出力する。`--audit-only` は /harness の制御フラグで `claq_run` へは渡さず、指定時はここで終える。`--root` / `--target-kind` はステップ4 の比較まで同じ値を保つ。

採点はこのスクリプトの出力だけを根拠にし、手で採点しない。

ルーブリック版: `2026-09-06` — 固定カテゴリ7個（各0〜10に正規化）:
ツール網羅性 / 文脈効率 / 品質ゲート / メモリ永続性 / 評価網羅性 / セキュリティガードレール / コスト効率

## ステップ2: トップ3アクション特定

`top_actions[]` から効果の高い 3 件を選ぶ。各アクションには `checks[]` の失敗チェックに紐付く正確なファイルパスが付いている。

`--apply` が指定されていなければ、トップ3（提案内容・対象ファイル・想定効果）を提示してここで終える（harness-tuner へ適用を委ねない）。`--apply` の明示がこの先へ進む承認にあたる。

## ステップ3: harness-tuner による改善適用（`--apply` 指定時のみ）

`claq:harness-tuner` を起動し、ベースライン JSON とトップ3アクションを渡して信頼性・コスト・スループットの最適化を委ねる。harness-tuner は、ハーネス設定（hooks.json / settings.json 等）への最小限で元に戻せる変更だけを適用し、影響範囲を要約する。

## ステップ4: 改善後スコア（`--apply` 指定時のみ）

```bash
. "$HOME/.claq/env.sh" || exit 127
claq_run claq.ci.harness_audit <scope> --format <text|json> --root <path> --target-kind <repo|consumer>
```

ステップ1 と同じ `--root` / `--target-kind` で採点し直す（異なる root・スケールのスコアを比べない）。

## ステップ5: 差分要約（`--apply` 指定時のみ）

変更前後の差分・カテゴリ別のスコア変化・harness-tuner が適用した変更を出力する。

## 制約

- 測定できる効果を持つ小さな変更を優先する
- クロスプラットフォームの動作を保ち、脆弱なシェルクォーティングを持ち込まない
- `checks[]` と `top_actions[]` にある正確なファイルパスを残す

## 出力仕様

1. ベースライン `overall_score` と `max_score`（`repo` では58）
2. カテゴリ別スコアと指摘
3. 失敗チェックと正確なファイルパス
4. 上位3件のアクション（`top_actions`）
5. （`--apply` 指定時）harness-tuner が適用した内容
6. （`--apply` 指定時）改善後スコアカード
7. （`--apply` 指定時）変更前後の差分サマリー

## 引数

- 位置 #1: `[scope]` = `repo|hooks|skills|commands|agents`（既定: `repo`）
- `--scope=<scope>`: 位置引数の別名
- `--format=text|json`（既定: `text`）
- `--root=<path>`: 監査対象ルート（必須）
- `--target-kind=repo|consumer`: 期待する判定モード（必須。自動判定と食い違えば FAIL）
- `--audit-only`: ステップ1 だけで終える（トップ3も出さない）。/harness の制御フラグで `claq_run` へは渡さない
- `--apply`: トップ3を harness-tuner に適用させる（ステップ3〜5）。引数によるスコープ指定であって承認待ちではないので、`--apply` があれば途中で止まらない
