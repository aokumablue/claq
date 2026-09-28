---
name: refactor
description: コードを一気通貫でリファクタリング。差分・指定パスの両方に対応。性能劣化防止・デッドコード排除・可読性改善・レビューを実行。
command: /refactor
---

# 統合リファクタリング

## 停止点

止まるのは、ステップ1 の `BLOCKED`（effective scope が空）と、ステップ2 で基準を取れないときだけ。それ以外では、次の手順を予告して終わる要約・続けてよいかの伺い・作業を止めない判断事項の列挙・区切りや長さを理由にした報告で応答を終えず、ステップ9 の要約まで進む。報告や未決事項への推奨は次のツール呼び出しと同じ応答に書く。

## 永続メモリ

- 参照: SessionStart が `<claq-memory>`（`status='active'` の知識）を注入済み。追加で要るときは `. "$HOME/.claq/env.sh"` のあと `claq_run claq.mem.cli search "..."`（クエリ例 `refactor clean simplify perf review {対象ファイルパス}` / `critical high blocker`）→ 本文が要る key だけ `claq_run claq.mem.cli show <key>`
- 記録: ステップ8 の基準で `claq_mem_learn` を使う

## ステップ1: preflight（スコープ確定 + 実行準備）

スコープは次の優先順で決める: 引数パス（ディレクトリ = 配下の全ファイル / ファイル = そのファイル）→ `git diff --name-only HEAD`。

着手前に Skill ツールで次の 2 つを起動する。どちらも省かない。

- `refactor-prep` skill: 対象分割・依存可視化・テストセットを確定する（fork 実行。結果は報告で受け取る）
- `refactor-rollback` skill: ファイル単位のリバート計画（Rollback Blueprint）を作る（inline 実行で本セッションに展開される）。ステップ3 以降の `git checkout -- <file>` は処理開始前の未コミット編集も区別なく破棄するため、その退避（worktree clean の確認と baseline patch の保存）もここで行われる

`deps.from` / `deps.to` は `groups` 配列のインデックスを指す。`deps_order` はトポロジカル順に解決し、復旧時は逆順で適用する。`refactor-rollback` が `CAUTION` としたファイルは自動適用せず最終要約に記録し、`Skip Rules`（`{file, reason, required_action}`）のファイルは処理対象から外す。

`refactor-rollback` の出力を受け取った直後に、実効スコープを確定する（precondition gate）。

1. `effective_scope = スコープ確定の結果 − Skip Rules の file 集合 − CAUTION の file 集合`
2. `effective_scope` が空なら、1 件も編集せずに `BLOCKED: effective scope が空です（Skip Rules: {file 一覧}）` として終える（リバートできない問題を final gate まで持ち越すと、変更が残ったまま止まるため）
3. ステップ3〜5 の各委譲の依頼文へ、`effective_scope` をファイルパス一覧として明示して渡す（この手順書に書いた除外は子エージェントへ届かないため）

## ステップ2: baseline

1. テスト・linter を実行して基準を取る
2. 既存の失敗を記録し、新規失敗の判定に使う
3. 基準を取れなければ実装を止め、原因を解消してから再開する

## ステップ3: clean（`claq:code-refiner`）

デッドコードを削除する。依頼文に `mode: clean` と `effective_scope` を明示する（モードも依頼文で渡さないと子へ届かない）。ファイルを 1 つ適用するごとにテストし、失敗したら `git checkout -- <file>` でそのファイルだけ戻して続ける。

## ステップ4: simplify（並列, `claq:code-refiner`）

グループ単位で同時に起動する（上限 4）。依頼文に `mode: simplify` と `effective_scope` を明示する。起動の直前に各グループの対象ファイル集合を突き合わせ、重複があるグループは同時に起動せず直列にする（同じファイルを並列に編集すると片方の変更が失われるため）。機能を保ったまま可読性・一貫性・保守性を改善し、グループが終わるごとにテストし、失敗したらファイル単位で戻す。

## ステップ5: perf（`claq:code-refiner`）

simplify の全グループが終わってから始める。依頼文に `mode: perf` と `effective_scope` を明示する。計測データは渡さないので、code-refiner は明白なアルゴリズム欠陥（不要計算・重複 I/O・N+1・過剰なメモリアロケーション）の修正に限り、実施内容に「未計測」と書く（実測を伴う最適化が要るならプロファイル取得を先に行う）。変更ごとにテストし、失敗したらファイル単位で戻す。

## ステップ6: review + secure（並列）

`claq:reviewer`（品質・設計・保守性）と `claq:security-auditor`（セキュリティ・脆弱性）を同時に起動し、結果を統合する。両方の依頼文に `effective_scope` を書き、reviewer の依頼文には security-auditor と並列であることも書く。

## 部分モード

`--mode=clean` はステップ3 → 6 → 7、`--mode=simplify` はステップ4 → 6 → 7 だけを実行する。部分モードでもステップ6 と CRITICAL/HIGH のブロック判定は省かない。

## ステップ7: final gate

1. テストと linter を再実行する
2. CRITICAL または HIGH が 1 件でもあればブロックする
3. 失敗した変更はファイル単位で戻して再検証する
4. すべて通ったときだけ完了にする

`Final Gate: PASS` になるのは次をすべて満たすときだけで、1 つでも欠ければ `BLOCKED`。

- 実行対象の stage（全体 = clean/simplify/perf/review+secure、`--mode=clean` = clean/review+secure、`--mode=simplify` = simplify/review+secure）がすべて完了している。スキップ・未実行・リバートしたままの放置は完了ではなく、全ファイルがリバートされて実質変更ゼロになった stage も未完了とする
- CRITICAL/HIGH が 0 件
- ステップ6 の `reviewer` / `security-auditor` の出力に `Blockers: {n}` 行が実際にある。無ければ「指摘ゼロ」ではなく「判定を取れなかった」として `BLOCKED` にする

リバートできないファイル（Skip Rules / `revert=NOT_AVAILABLE`）はステップ1 の precondition gate で除外済みなので、ここでは判定材料にしない。収束 gate の最終権限は `loop-dev` の evaluate にあり、本 gate はその入力を作る。

CRITICAL/HIGH の blocker、またはテスト/lint の失敗が出たら、Skill ツールで `loop-dev` skill を起動する。入力:

- `task` = final gate の CRITICAL/HIGH 指摘の解消
- `approved_plan` = blocker 一覧（plan 段を縮退させる）
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

記録しないもの: リポジトリを読めば分かること、作業ログ（削除ファイル一覧・件数・スコアは要約に書けば足りる）、そのセッション限りの妥協、一般的なリファクタ知識。

```bash
. "$HOME/.claq/env.sh" || exit 127
claq_mem_learn --kind fact --scope repo --domain <domain> \
  --title "<構造上の事実を 1 行で>" \
  --body "<根拠と、次に触るときの注意>"
```

該当が無ければ 1 件も記録しない（0 件は正しい結果）。詳細基準は `../skills/learn/SKILL.md` の「記録する / しない」。

## ステップ9: 要約

次のテンプレートで示す。Issues は `claq:reviewer` と `claq:security-auditor` の統合件数。末尾にステップ8 で記録した key を 1 行で添える（記録が無ければ `Learned: なし`）。

```text
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

## ルール

- 各委譲の完了の主張はテスト/lint の出力で裏を取り、証跡の無い完了は未検証として扱う
- 機能を変えない（WHAT 不変）。挙動が変わる疑いのある変更は要確認として報告する
- 安全性に疑いがある変更は飛ばし、最終要約に書く
- 作業は `claq:code-refiner`（`mode` = clean / simplify / perf）・`claq:reviewer`・`claq:security-auditor` に委譲する。実行順・並列制御・ファイル単位のリバートは本コマンドが直接行う

## 引数

- 位置 #1: `[ファイルパス or ディレクトリ]`（省略時: 変更差分）
- `--mode=clean|simplify`: 部分モード（省略時: clean → simplify → perf → review の全段階）
