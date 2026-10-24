# スキル改善ガイド

`skill-make` のスキル改善と高度な機能の詳細。

## 改善の考え方

- きつい MUST や狭い制約を足しても直らないときは、別の比喩や別のやり方を示す
- 最終出力だけでなくトランスクリプトも読み、無駄な作業の原因になっている指示を減らす
- 複数の run が同じヘルパースクリプトや同じ多段手順を書いていたら、スキルの `scripts/` に同梱する

## 反復ループ

改善後は次を繰り返す。

1. 変更をスキルに反映する
2. すべてのテストケースを新しい `iteration-<N+1>/` で再実行する（ベースラインも含む）
3. `benchmark.json` / `grading.json` を読んで結果を分析する
4. 新しい改善点を抽出して、さらに改善する

反復を終える条件（満たしたら SKILL.md の全体の流れの 8 へ進む）:

- grader の `eval_feedback` と失敗した期待値に、前のイテレーションに無かったものが出なくなった
- with_skill の pass_rate が前のイテレーションから上がらなくなった

## 上級編: 盲検比較

スキルの 2 版をより厳密に比べたいときは、盲検比較を使える（`../../../agents/comparator.md` と `../../../agents/bench-analyzer.md`）。どちらがどちらかを明かさずに 2 つの出力を独立したエージェントに渡して品質を判定させ、その勝因を分析する。bench-analyzer（`posthoc_comparison`。両方に SKILL.md がある `old_skill` との比較に限る）へは、`compare-<ID>/comparison.json` と、`mapping.json` で引いた勝者・敗者それぞれの SKILL.md と run の `transcript.md` を渡す（`winner` が `TIE` の比較は渡さない）。返値の改善案は「5. 分析結果をもとに改善する」の材料にする。

## 説明文の最適化

SKILL.md の前置きの `description` は、Claude がスキルを呼ぶかどうかを左右する主要因である。作成・改善の後、次の手順で description のトリガー精度を上げる。train/holdout で過適合を避ける収束判断の考え方は `skill-tune`（Step 7 の hold-out 過適合チェック）に従う。

### 1. トリガー用 eval クエリを作る

`should_trigger` の true/false を混ぜた 20 件を JSON で保存する。should-trigger は 8〜10 件（同じ意図の別表現を広く）、should-not-trigger も 8〜10 件（近いが別のタスクが必要なもの）。クエリは実際のユーザーが打つ具体的な例にする（パス・列名・会社名・URL などの背景付き。小文字・略語・タイポ・口語も可。長さにばらつきを持たせ、エッジケースを重視する）。

```json
[
  {"query": "the user prompt", "should_trigger": true},
  {"query": "another prompt", "should_trigger": false}
]
```

### 2. 候補を反復評価する

eval セットを train 60% / holdout 40% に手で分け、description の候補ごとに train 側で `should_trigger` の判定精度を確かめ、holdout 側で過適合していないかを確かめる。

### 3. 結果を反映する

holdout のスコアが最も良かった description を採用し、SKILL.md の frontmatter を更新する。
