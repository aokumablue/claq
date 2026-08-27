---
name: skill-gen
description: リポジトリ固有入力収集→skill-make スキルに SKILL.md 生成委譲→skill-tune スキルに改善委譲→grader/comparator/bench-analyzer による評価。
command: /skill-gen
---

<!-- DRY: grillme 前段（発火条件〜他処理に進まない）は全コマンド共通。終了条件・永続メモリ・引数は固有 -->

# スキル生成入力収集

リポジトリ固有入力を集めて整理し、SKILL.md 生成は `skill-make` skill に、生成後の empirical 改善は `skill-tune` skill に委譲。改善の各反復で `grader` / `comparator` / `bench-analyzer` の3エージェントが評価を担う。

## grillme 起動（条件付き）

要件が曖昧で複数の読み方が成立するときのみ、開始直後に grillme スキルで共通理解を固める。依頼が明確なら省略して着手する。起動した場合は完了まで他処理に進まず、完了時は合意方針を1行サマリで確認する。

## 永続メモリ

- 注入: SessionStart の `mem context` が `<bluecore-memory>` を自動投入（`status='active'` のみ）
- 参照: `bluecore_run bluecore.mem.cli search "..."`（`. "$HOME/.bluecore/env.sh"` 前提。クエリ例 `skill-gen pattern repository workflow`）→ 本文が要る key だけ `bluecore_run bluecore.mem.cli show <key>`
- 記録: 再利用可能な学びだけ `bluecore_mem_learn` で登録する。基準は `../skills/learn/SKILL.md` の「記録する / しない」

## skill 起動メカニズム

`skill-make` は `user-invocable: false` の skill で、description マッチにより Claude Code が Skill ツール経由で fork 実行する。`skill-tune` は `user-invocable: true` のため同様の自動発火に加えユーザーが直接呼び出すことも可能。本コマンドのステップ3 / ステップ4で「skill-make skill を起動」「skill-tune skill を起動」と明示することで発火する。

## ステップ1: 入力候補収集

```bash
. "$HOME/.bluecore/env.sh" || exit 127
collect_skill_create_inputs "${COMMITS:-200}"
```

## ステップ2: パターン検出

コミット規約（feat:/fix:/chore:）・ファイル同時変更パターン・繰り返しワークフロー・フォルダ構造/命名規則・テストパターンを抽出。

## ステップ3: skill-make 起動 → SKILL.md 生成

`skill-make` skill を起動。ステップ1〜2の入力を渡し、SKILL.md 下書きを作成。

## ステップ4: skill-tune 起動 + 評価エージェント連鎖

`skill-tune` skill を起動。empirical 評価と反復改善ループに入る。各反復内で以下の3エージェントを連鎖呼び出し:

1. **`bluecore:grader`**: 実行トランスクリプトと出力を期待値と照合し合否と根拠を整理
2. **`bluecore:comparator`**: 改善前後（または2候補）の出力をブラインド比較しどちらが課題達成度が高いか判定
3. **`bluecore:bench-analyzer`**: ベンチマーク結果と比較結果を要約し勝因・性能傾向を抽出

3 エージェントはいずれも結果を**返値**で返す（ファイルは書かない）。本コマンドが schema 検証のうえ固定の保存先へ書き、次のゲートを通す。

### 完了ゲート（1 件でも満たさなければ完了報告せず BLOCKED）

散文で「実行した」と書くことと、実際に実行されたことは別である。実機監査では、空トランスクリプト・候補と成果物の対応不一致・全 run が `0` のベンチマークを含む評価が「skill-make→skill-tune→grader→comparator→bench-analyzer を実行済み」として完了報告された。

1. **実行証跡**: 各 run について transcript・実行コマンド・exit code・入力 artifact の hash が揃っていること。`transcript_chars: 0` や `total_tool_calls: 0` は「実行した」の証跡にならない
2. **未計測は `null`**: 計測できなかった値は理由付きの `null` にする。`0` を代用しない（`0` が実測値と欠測の両方を意味すると、比較が成立しているように見えてしまう）
3. **候補と成果物の対応**: comparator が候補 A/B について述べた内容が、その候補の実際の artifact に含まれること。引用が実在しない、または対応が入れ替わっている場合はその判定を破棄する
4. **相互整合**: grader と comparator の期待値判定が食い違う場合は完了せず BLOCKED。どちらかの誤りが確定するまで採用判断に進まない
5. **表現**: 実行していない eval を「実行済み」と書かない

収束条件: 連続2回の反復で grader の新規不明瞭点ゼロ、または comparator の判定が連続2回同一勝者。ただし上記ゲートを満たさない反復は収束のカウント対象にしない。

## ステップ5: 知識カード生成（`--knowledge` 時のみ）

ステップ1の入力収集で見つかったリポジトリ規約・繰り返しワークフローのうち、
SKILL.md に落とし込めなかったものを知識カードとして登録する。

```bash
. "$HOME/.bluecore/env.sh" || exit 127
bluecore_mem_learn --key <slug> --kind convention --scope repo \
  --title "<1 行要約>" --domain <domain> --body "<根拠>"
```

常に `status=pending` で登録され、採否は人間が `/instinct` でレビューして決める
（自動生成の候補をそのまま注入枠に載せない）。記録基準は `../skills/learn/SKILL.md` の
「記録する / しない」に従い、リポジトリを読めば分かることは登録しない。

## 役割分担

| ステップ | 担当 | 種別 | 役割 |
|---|---|---|---|
| 入力収集 | skill-gen | command | リポジトリ分析・パターン検出 |
| SKILL.md 生成 | skill-make | skill | 下書き作成・構造化 |
| 品質改善 | skill-tune | skill | empirical 評価・反復改善 |
| 合否判定 | grader | agent | 期待値照合 |
| 盲検比較 | comparator | agent | 改善前後の優劣判定 |
| ベンチ要約 | bench-analyzer | agent | 勝因・性能傾向抽出 |

## 関連

- `/instinct` — 生成した知識カード（`status='pending'`）のレビューと昇格

## 引数

- `--commits=<n>` — 直近コミット件数（既定: 200）
- `--output=<path>` — 生成先（既定: `skills/`）
- `--knowledge` — 知識カード生成も依頼（ステップ5）
