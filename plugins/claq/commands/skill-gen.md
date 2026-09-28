---
name: skill-gen
description: リポジトリ固有入力収集→skill-make スキルに SKILL.md 生成委譲→skill-tune スキルに改善委譲→grader/comparator/bench-analyzer による評価。
command: /skill-gen
---

# スキル生成入力収集

リポジトリ固有の入力を集めて整理し、SKILL.md の生成は `skill-make` skill に、生成後の実行評価による改善は `skill-tune` skill に委ねる。改善の各反復では `grader` / `comparator` / `bench-analyzer` が評価を担う。

## grillme（条件付き）

要件が曖昧で複数の読み方が成立するときだけ、開始直後に grillme スキルで共通理解を固める。依頼が明確なら省いて着手する。起動したら完了まで他の処理に進まず、合意した方針を 1 行で確認する。

## 永続メモリ

- 参照: SessionStart が `<claq-memory>`（`status='active'` の知識）を注入済み。追加で要るときは `. "$HOME/.claq/env.sh"` のあと `claq_run claq.mem.cli search "..."`（クエリ例 `skill-gen pattern repository workflow`）→ 本文が要る key だけ `claq_run claq.mem.cli show <key>`
- 記録: 再利用可能な学びだけ `claq_mem_learn` で登録する。基準は `../skills/learn/SKILL.md` の「記録する / しない」

## ステップ1: 入力候補収集

```bash
. "$HOME/.claq/env.sh" || exit 127
collect_skill_create_inputs "${COMMITS:-200}"
```

## ステップ2: パターン検出

コミット規約（feat:/fix:/chore:）・ファイルの同時変更パターン・繰り返しのワークフロー・フォルダ構造/命名規則・テストパターンを抽出する。

## ステップ3: SKILL.md 生成

Skill ツールで `skill-make` skill を起動し、ステップ1〜2 の入力を渡して SKILL.md の下書きを作らせる。

## ステップ4: 改善と評価

Skill ツールで `skill-tune` skill を起動し、実行評価と反復改善のループに入る。各反復では次の 3 エージェントを順に呼ぶ。

1. `claq:grader`: 実行トランスクリプトと出力を期待値と照合し、合否と根拠を出す
2. `claq:comparator`: 改善前後（または 2 候補）の出力を盲検で比べ、どちらが課題をよく達成したかを判定する
3. `claq:bench-analyzer`: ベンチマーク結果と比較結果から勝因・性能傾向を抽出する

3 エージェントは結果を返値で返す（ファイルは書かない）。本コマンドが schema を検証して固定の保存先へ書き、次の完了ゲートを通す。

comparator の判定は助言として扱う。機械判定できる期待値は deterministic なアサーションで先に確定させ、skill の採用のような後戻りしにくい判断を comparator の勝敗だけで決めない（勝たせる指示を埋め込んだ候補には判定ごと曲げられうる）。

### 完了ゲート（1 つでも満たさなければ完了と報告せず BLOCKED）

「実行した」と書くことと実際に実行されたことは別なので、次の証跡で確かめる。

1. 実行証跡: 各 run について transcript・実行コマンド・exit code・入力 artifact の hash が揃っている。`transcript_chars: 0` や `total_tool_calls: 0` は実行の証跡にならない
2. 未計測は `null`: 計測できなかった値は理由付きの `null` にし、`0` で代用しない（`0` が実測値と欠測の両方を意味すると、比較が成立しているように見えるため）
3. 候補と成果物の対応: comparator が候補 A/B について述べた内容が、その候補の実際の artifact に含まれている。引用が実在しない、または対応が入れ替わっている判定は破棄する
4. 相互整合: grader と comparator の期待値判定が食い違えば完了せず BLOCKED にし、どちらの誤りかが確定するまで採用判断に進まない
5. 表現: 実行していない eval を「実行済み」と書かない

収束条件は `../skills/skill-tune/SKILL.md` の Step 7 に従う（本コマンドで定義し直さない）。上のゲートを満たさない反復は収束の回数に数えない。`grader` の期待値合否（`summary.pass_rate`）と `comparator` の勝者は収束判定への入力であって、判定の主体ではない。

## ステップ5: 知識カード生成（`--knowledge` 時のみ）

ステップ1 で見つかったリポジトリの規約・繰り返しのワークフローのうち、SKILL.md に落とし込めなかったものを知識カードとして登録する。

```bash
. "$HOME/.claq/env.sh" || exit 127
claq_mem_learn --key <slug> --kind convention --scope repo \
  --title "<1 行要約>" --domain <domain> --body "<根拠>"
```

登録は常に `status=pending` になり、採否は人間が `/instinct` でレビューして決める（自動生成の候補をそのまま注入枠に載せない）。記録基準は `../skills/learn/SKILL.md` の「記録する / しない」に従い、リポジトリを読めば分かることは登録しない。

## 引数

- `--commits=<n>` — 直近コミット件数（既定: 200）
- `--output=<path>` — 生成先（既定: `skills/`）
- `--knowledge` — 知識カード生成も行う（ステップ5）
