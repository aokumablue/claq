---
name: harness
description: ハーネスを監査して改善案を示す。適用は `--apply` 指定時のみ。
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

`scope` は位置引数でも `--scope` でも指定できる（既定 `repo`）。`claq_run` には `--root` と `--target-kind repo|consumer` を常に渡す（ユーザーが省いたら監査対象のルートと種別を決めて渡す）。省くと cwd から自動判定され、marketplace レイアウトの provider リポジトリ直下を consumer と誤判定しうる。`--target-kind` が自動判定と食い違えば FAIL し、誤った root を監査したまま進むのを防ぐ。

## ステップ1: ベースライン取得

```bash
. "$HOME/.claq/env.sh" || exit 127
claq_run claq.ci.harness_audit <scope> --format <text|json> --root <path> --target-kind <repo|consumer>
```

スコアカードを出力する。`--apply` 指定時は harness-tuner へ生の JSON を渡すため `--format json` で実行し、表示はその JSON から整形する（text 出力に JSON は含まれない）。`--audit-only` は /harness の制御フラグで `claq_run` へは渡さず、指定時はここで終える。`--root` / `--target-kind` はステップ4 の比較まで同じ値を保つ。

採点はこのスクリプトの出力だけを根拠にし、手で採点しない。

## ステップ2: トップ3アクション特定

`top_actions[]`（スクリプトが points 降順の上位 3 件に絞り済み）をそのまま使う。各 `path` は失敗チェックのファイルパスである。

`--apply` が指定されていなければ、トップ3（提案内容・対象ファイル・想定効果）を提示してここで終える（harness-tuner へ適用を委ねない）。`--apply` の明示がこの先へ進む承認にあたる。

## ステップ3: harness-tuner による改善適用（`--apply` 指定時のみ）

`claq:harness-tuner` を起動し、ステップ1 の JSON 出力をそのまま渡す（トップ3はその `top_actions`）。

## ステップ4: 改善後スコア（`--apply` 指定時のみ）

```bash
. "$HOME/.claq/env.sh" || exit 127
claq_run claq.ci.harness_audit <scope> --format <text|json> --root <path> --target-kind <repo|consumer>
```

## ステップ5: 差分要約（`--apply` 指定時のみ）

変更前後の差分・カテゴリ別のスコア変化・harness-tuner が適用した変更を出力する。

## 引数

- 位置 #1: `[scope]` = `repo|hooks|skills|commands|agents`（既定: `repo`）
- `--scope=<scope>`: 位置引数の別名
- `--format=text|json`（既定: `text`）
- `--root=<path>`: 監査対象ルート
- `--target-kind=repo|consumer`: 期待する判定モード（自動判定と食い違えば FAIL）
- `--audit-only`: ステップ1 だけで終える
- `--apply`: トップ3を harness-tuner に適用させる（ステップ3〜5）
