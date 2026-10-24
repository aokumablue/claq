---
name: refactor
description: 機能を変えずにコードをリファクタリングする（デッドコード削除・可読性・性能。差分または指定パス）。新機能は /feat-dev。
command: /refactor
---

# 統合リファクタリング

## 停止点

止まるのは、ステップ1 の `BLOCKED`（effective scope が空）と、ステップ2 で基準を取れないときだけ。それ以外では、次の手順を予告して終わる要約・続けてよいかの伺い・作業を止めない判断事項の列挙・区切りや長さを理由にした報告で応答を終えず、ステップ9 の要約まで進む。報告や未決事項への推奨は次のツール呼び出しと同じ応答に書く。

## 永続メモリ

- 参照: SessionStart が `<claq-memory>`（`status='active'` の知識）を注入済み。追加で要るときは `. "$HOME/.claq/env.sh"` のあと `claq_run claq.mem.cli search "..."`（クエリ例 `refactor clean simplify perf review {対象ファイルパス}` / `critical high blocker`）→ 本文が要る key だけ `claq_run claq.mem.cli show <key>`
- 記録: ステップ8 の基準で `claq_mem_learn` を使う

## ステップ1: preflight（スコープ確定 + 実行準備）

スコープは `refactor-prep` が確定する（引数パスがあれば渡す。結果は `scope_files`）。

着手前に Skill ツールで次の 2 つを起動する。どちらも省かない。

- `refactor-prep` skill: 対象分割・依存可視化・テストセットを確定する（fork 実行。結果は報告で受け取る）
- `refactor-rollback` skill: ファイル単位のリバート計画（Rollback Blueprint）を作る（inline 実行で本セッションに展開される）

複数ファイルを戻すときは Rollback Blueprint の `Order` に従う。

`refactor-rollback` の出力を受け取った直後に、実効スコープを確定する（precondition gate）。

1. `effective_scope = scope_files − Skip Rules の file 集合 − CAUTION の file 集合`
2. `effective_scope` が空なら、1 件も編集せずに `BLOCKED: effective scope が空です（Skip Rules: {file 一覧}）` として終える（リバートできない問題を final gate まで持ち越すと、変更が残ったまま止まるため）

## ステップ2: baseline

1. `refactor-prep` の `tests.baseline` でテスト・linter を実行して基準を取る
2. 既存の失敗を記録し、新規失敗の判定に使う
3. 基準を取れなければ編集を始めず、取れない原因を報告して止まる（基準が無いと新規失敗を判定できない）

## ステップ3: clean（`claq:code-refiner`）

デッドコードを削除する。依頼文に `mode: clean` と `effective_scope` を明示する（モードも依頼文で渡さないと子へ届かない）。ファイルを 1 つ適用するごとにテストし、失敗したら `git checkout -- <file>` でそのファイルだけ戻して続ける。

## ステップ4: simplify（並列, `claq:code-refiner`）

グループ単位で同時に起動する（上限 4）。依頼文に `mode: simplify` と `effective_scope` を明示する。起動の直前に各グループの対象ファイル集合を突き合わせ、重複があるグループは同時に起動せず直列にする（同じファイルを並列に編集すると片方の変更が失われるため）。グループが終わるごとにテストし、失敗したらファイル単位で戻す。

## ステップ5: perf（`claq:code-refiner`）

simplify の全グループが終わってから始める。依頼文に `mode: perf` と `effective_scope` を明示し、計測データは渡さない。変更ごとにテストし、失敗したらファイル単位で戻す。

## ステップ6: review + secure（並列）

`claq:reviewer`（品質・設計・保守性）と `claq:security-auditor`（セキュリティ・脆弱性）を同時に起動し、結果を統合する。両方の依頼文に `effective_scope` を書き、reviewer の依頼文には security-auditor と並列であることも書く。

## 部分モード

`--mode=clean` はステップ1 → 2 → 3 → 6 → 7 → 8 → 9、`--mode=simplify` はステップ1 → 2 → 4 → 6 → 7 → 8 → 9 を実行する。

## ステップ7: final gate

1. `refactor-prep` の `tests.final` を再実行する
2. 失敗した変更はファイル単位で戻して再検証する

`Final Gate: PASS` になるのは次をすべて満たすときだけで、1 つでも欠ければ `BLOCKED`。

- 実行対象の stage（全体 = clean/simplify/perf/review+secure、`--mode=clean` = clean/review+secure、`--mode=simplify` = simplify/review+secure）がすべて完了している。スキップ・未実行・リバートしたままの放置は完了ではなく、全ファイルがリバートされて実質変更ゼロになった stage も未完了とする
- CRITICAL/HIGH が 0 件
- テストと linter が green（ステップ2 で記録した既存の失敗を除く）
- ステップ6 の `reviewer` / `security-auditor` の出力に `Blockers: {n}` 行が実際にある。無ければ「指摘ゼロ」ではなく「判定を取れなかった」として `BLOCKED` にする

ファイル単位のリバートの後も CRITICAL/HIGH の blocker が残ったら、Skill ツールで `loop-dev` skill を起動する。入力:

- `task` = final gate の CRITICAL/HIGH 指摘の解消
- `approved_plan` = blocker 一覧
- `task_type` = `refactor-fix`

loop-dev が 2 反復で収束しなければ、ファイル単位のリバート方針に従い、未解消分を要約に書く。

## ステップ8: 学びの記録（毎回実行・記録は該当時のみ）

ファイル単位のリバートが起きた変更と、ステップ1 の依存可視化で分かった構造は、次のリファクタでも効く。要約の前に残す。

| 見つけたもの | kind | title に書くこと |
|---|---|---|
| リバートを引き起こした変更パターン | `pitfall` | 「X を Y にするとテストが落ちる」という回避条件 |
| モジュール間の隠れた依存・暗黙の契約 | `fact` | 依存の向きと理由 |
| 3 回以上繰り返した安全な手順 | `howto` | 手順の目的（「分割 → テスト → 適用の順で回す」） |
| 合意された、明文化されていないコーディング規約 | `convention` | 守るべきルール |
| 採用した設計と却下した案 | `decision` | 選択と理由 |

```bash
. "$HOME/.claq/env.sh" || exit 127
claq_mem_learn --kind fact --scope repo --domain <domain> \
  --title "<構造上の事実を 1 行で>" \
  --body "<根拠と、次に触るときの注意>"
```

該当が無ければ 1 件も記録しない（0 件は正しい結果）。詳細基準は `../skills/learn/SKILL.md` の「記録する / しない」。

## ステップ9: 要約

次のテンプレートで示す。Issues は `claq:reviewer` と `claq:security-auditor` の統合件数。部分モードで実行しなかった stage の行は件数の代わりに `skipped (--mode={mode})` と書く（0 と書くと「実行して変更なし」と区別できない）。

```text
Unified Refactor
──────────────────────────────
Scope:      {n} files
Cleaned:    {cleaned} files
Simplified: {simplified} files
Perf fixed: {perf_fixed} files
Reverted:   {reverted} files
Skipped:    {Skip Rules・CAUTION で除外したファイルと、code-refiner が UNKNOWN とした候補}
Issues:     CRITICAL {c} / HIGH {h} / MEDIUM {m} / LOW {l}
──────────────────────────────
Final Gate: PASS / BLOCKED
Learned:    {ステップ8 で記録した key / なし}
```

## ルール

- 各委譲の完了の主張はテスト/lint の出力で裏を取り、証跡の無い完了は未検証として扱う
- 機能を変えない（WHAT 不変）。挙動が変わる疑いのある変更は要確認として報告する
- 安全性に疑いがある変更は飛ばし、最終要約に書く

## 引数

- 位置 #1: `[ファイルパス or ディレクトリ]`（省略時: 変更差分）
- `--mode=clean|simplify`: 部分モード（省略時: clean → simplify → perf → review の全段階）
