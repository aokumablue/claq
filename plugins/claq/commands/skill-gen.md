---
name: skill-gen
description: リポジトリ固有入力収集→skill-make スキルに SKILL.md 生成と grader/comparator/bench-analyzer による評価を委譲→skill-tune スキルに改善委譲。
command: /skill-gen
---

# スキル生成入力収集

リポジトリ固有の入力を集めて整理し、SKILL.md の生成と eval は `skill-make` skill に、生成後の実行評価による改善は `skill-tune` skill に委ねる。eval では `grader` / `comparator` / `bench-analyzer` が評価を担う。

## 停止点

ユーザーの応答を待つのは grillme の質問だけ。完了ゲートを満たせなければ `BLOCKED` で止まる。それ以外では、次の手順を予告して終わる要約・続けてよいかの伺い・作業を止めない判断事項の列挙・区切りや長さを理由にした報告で応答を終えず、次のステップへ進む。報告や未決事項への推奨は次のツール呼び出しと同じ応答に書く。

## grillme（条件付き）

要件が曖昧で、読み方によって作業が大きく変わるときだけ、開始直後に grillme スキルで共通理解を固める。それ以外の判断は自分で決めて着手する。起動したら完了まで他の処理に進まず、合意した方針を 1 行で書き留めてから次へ進む（ここで再確認は求めない）。

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

## ステップ3: SKILL.md 生成と評価

Skill ツールで `skill-make` skill を起動し、ステップ1〜2 の入力を渡して SKILL.md の下書きを作らせる。

eval（`../skills/skill-make/references/eval-workflow.md` §4）では `claq:grader` / `claq:comparator` / `claq:bench-analyzer` が評価を担い、返値の schema 検証と保存も skill-make 側で行う。

### 完了ゲート（1 つでも満たさなければ完了と報告せず BLOCKED）

skill-make が戻った後、本コマンドがその戻り値と workspace に保存された `grading.json` / `comparison.json` / `benchmark.json` に対して次のゲートを適用する。

「実行した」と書くことと実際に実行されたことは別なので、次の証跡で確かめる。

1. 実行証跡: 各 run について transcript・`outputs/`・`timing.json`・`grading.json` が揃っている（検証を通らず保存されなかった判定は欠落として扱う）
2. 未計測は `null`: 計測できなかった値は理由付きの `null` にし、`0` で代用しない（`0` が実測値と欠測の両方を意味すると、比較が成立しているように見えるため）
3. 候補と成果物の対応: comparator が候補 A/B について述べた内容が、その候補の実際の artifact に含まれている。引用が実在しない、または対応が入れ替わっている判定は破棄する
4. 相互整合: grader と comparator の期待値判定が食い違えば完了せず BLOCKED にし、どちらの誤りかが確定するまで採用判断に進まない
5. 表現: 実行していない eval を「実行済み」と書かない

## ステップ4: 改善

Skill ツールで `skill-tune` skill を起動し、実行評価と反復改善のループに入る。収束条件は `../skills/skill-tune/SKILL.md` の Step 7 に従う（本コマンドで定義し直さない）。

## ステップ5: 知識カード生成（`--knowledge` 時のみ）

ステップ1 で見つかったリポジトリの規約・繰り返しのワークフローのうち、SKILL.md に落とし込めなかったものを知識カードとして登録する。

```bash
. "$HOME/.claq/env.sh" || exit 127
claq_mem_learn --key <slug> --kind convention --scope repo \
  --title "<1 行要約>" --domain <domain> --body "<根拠>"
```

記録基準は `../skills/learn/SKILL.md` の「記録する / しない」に従い、リポジトリを読めば分かることは登録しない。

## 引数

- `--commits=<n>` — 直近コミット件数（既定: 200）
- `--knowledge` — 知識カード生成も行う（ステップ5）
