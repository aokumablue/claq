# スキル改善ガイド

`skill-make` のスキル改善・高度な機能の詳細。

## 改善の考え方

1. **フィードバックを一般化する**
   - 何度でも使えるスキルを作る
   - 数個の例を何度も回して速く改善しても、その例だけに最適化されたスキルは価値がない
   - きつすぎるMUSTや過剰に狭い制約ではなく、別の比喩や別のやり方を提案する方がよいことがある

2. **プロンプトを軽く保つ**
   - 効いていない説明は削る
   - 最終出力だけでなくトランスクリプトも見る
   - モデルが無駄な作業をしているなら、その原因になっている指示を減らす

3. **なぜを説明する**
   - モデルに何をさせるかだけでなく、なぜ必要かも説明する
   - 今のLLMは賢いので、良い足場があれば単なる手順以上のことができる
   - 大文字のALWAYS/NEVERばかりになるなら黄色信号→可能なら、理由を説明して自然に書き換える

4. **テストケース間の重複を探す**
   - 複数の実行で同じヘルパースクリプトや同じ多段手順が繰り返されていないかを確認
   - 3つとも `create_docx.py` や `build_chart.py` を作っているなら、そのスクリプトはスキルに同梱した方がよい
   - 一度書いて `src/bluecore/skills/` に置けば毎回の再発明を防げる

この作業は重要。考える時間がボトルネックではないので、時間をかけて見直す。下書きを作ってから、もう一度眺め直して改善するのがおすすめ。

## 反復ループ

改善後は次を繰り返す:

1. 変更をスキルに反映
2. すべてのテストケースを新しい `iteration-<N+1>/` に再実行（ベースラインも含む）
3. `benchmark.json` / `grading.json` を読んで結果を分析する
4. 新たな改善点を抽出してさらに改善して繰り返す

続ける条件:

- ユーザーが満足した
- フィードバックがすべて空になった
- これ以上有意な進展がない

## 上級編: 盲検比較

スキルの2版をより厳密に比べたいとき、盲検比較システムを使える。詳細は `../../../agents/comparator.md` と `../../../agents/bench-analyzer.md` を参照。基本は、どちらがどちらかを明かさずに2つの出力を独立したエージェントに渡し、品質を判定させ、その勝因を分析する流れ。

任意でサブエージェントが必要。多くのユーザーには不要で、通常は人間レビューのループで十分。

## 説明文の最適化

SKILL.mdの前置きにある `description` は、Claudeがスキルを呼ぶかどうかを左右する主な要素。スキルを作成・改善した後、トリガー精度を上げるために説明文の最適化を提案。

### 1. トリガー用evalクエリを作る

20個の評価クエリを作る。`トリガー対象` と `トリガー対象外` を混ぜてJSONで保存。

```json
[
  {"query": "the user prompt", "should_trigger": true},
  {"query": "another prompt", "should_trigger": false}
]
```

クエリは実際のユーザーが打ちそうな具体的で詳細なもの。パス・個人的な状況・列名・会社名・URLなど、少し背景のある実例。小文字・略語・タイポ・くだけた言い方が混ざっていても構わない。長さにばらつきを持たせ、エッジケースを重視。

**should-trigger**（8〜10件）: 同じ意図の別表現を広く集める。
**should-not-trigger**（8〜10件）: 近接しつつも別タスクが必要なものを選ぶ。

### 2. 最適化ループを回す

```bash
source "${CLAUDE_PLUGIN_ROOT}/runtime/bluecore-helpers.sh"
bluecore_run bluecore.skills.run_loop --eval-set <path-to-trigger-eval.json> --skill-path <path-to-skill> --model <model-id-powering-this-session> --max-iterations 5 --verbose
```

現セッションを動かしているmodel IDを使う。evalセットをtrain 60% / holdout test 40%に分け、反復改善。`best_description` はtestスコアで選ぶ。

### 3. 結果を反映する

JSONの `best_description` を取り出し、SKILL.mdのfrontmatterを更新。

## パッケージ化して渡す（`present_files` がある場合のみ）

`present_files` ツールにアクセスできるか確認。使えないなら飛ばす。

```bash
source "${CLAUDE_PLUGIN_ROOT}/runtime/bluecore-helpers.sh"
bluecore_run bluecore.skills.package_skill <path/to/skill-folder>
```

## 環境別の注意

### Claude.ai

- subagentがないので並列実行はせず1件ずつ進める
- baseline比較に依存する定量ベンチマークは省略し、定性的フィードバックを重視
- 説明文最適化（eval スクリプト使用）は飛ばす
- 盲検比較は飛ばす
- 既存スキル更新時は元の名前を保持し、`/tmp/` にコピーしてから編集

### Cowork

- subagentは使えるので基本フローはそのまま
- 既存スキル更新時はClaude.aiのセクションの手順に従う
