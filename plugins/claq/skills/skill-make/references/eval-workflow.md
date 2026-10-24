# テストケース実行と評価

`skill-make` の eval ワークフローの詳細。

## 全体の流れ

1 つの流れとして続けて進める（止まってよい条件は SKILL.md の「停止点」）。他の testing skill には委ねない。

結果は `<skill-name>-workspace/` に、スキルディレクトリの兄弟として保存する。ワークスペース内はイテレーション単位（`iteration-1/` / `iteration-2/`）に分け、その中に eval ごとのディレクトリを作る。ディレクトリ名には番号だけでなく何を試しているかが分かる名前を付ける（`eval-0-table-extraction` 等。benchmark.json の `eval_name` にも使う）。最初から全部は作らず、必要になった順に作る。

## 1. スキルありとベースラインを同じターンで全部起動する

各テストケースについて、スキルありとベースラインの 2 つのサブエージェントを同じターンで起動する（スキルありを全部終えてからベースラインに戻る方式にしない。同時に走らせたほうが条件がそろう）。

スキルあり:

```text
Execute this task:
- Skill path: <path-to-skill>
- Task: <eval prompt>
- Input files: <eval files if any, or "none">
- Save outputs to: <workspace>/iteration-<N>/eval-<ID>/with_skill/outputs/
- Outputs to save: <what the user cares about — e.g., "the .docx file", "the final CSV">
- Final response: list the steps you took, the tools/scripts you used, and any errors and how you handled them
```

ベースライン（プロンプトは同じ。ベースラインの種類は状況で決まる）:

- 新しいスキルを作る場合: スキルなし。`without_skill/outputs/` に保存する
- 既存スキルを改善する場合: 旧版をベースラインにする。編集前に `cp -r <skill-path> <workspace>/skill-snapshot/` でスナップショットを取り、そのコピーを使う。保存先は `old_skill/outputs/`

## 2. 期待値（expectations）を書く

各テストケースの定量的な期待値を下書きする。`evals/evals.json` に既に `expectations` があれば見直し、何を検証しているかを最終報告に書く。

良い期待値は、客観的に検証できて、名前だけで内容が分かるもの。文章の味やデザインの良し悪しのような主観的なものは無理に定量化せず、定性的に評価する。

下書きができたら `evals/evals.json` を更新する。

## 3. 完了した run から timing を記録する

各サブエージェントのタスクが終わると、通知に `total_tokens`・`tool_uses`・`duration_ms` が含まれる。この情報はその通知でしか取れないので、届いたらすぐ run ディレクトリの `timing.json` に保存する（形式は `schemas.md`。後でまとめて処理しない）。サブエージェントの最終応答も同じ run ディレクトリの `transcript.md` へ保存する（grader の `transcript_path` に渡す）。

## 4. grading して集計し、分析する

すべての run が終わったら:

grader / comparator / bench-analyzer の返値が検証を通らなければ保存せず、推測で補った JSON も書かない。その run（比較）は判定欠落として skill-make の呼び出し元へ伝える。

1. 各 run を grading する
   - grader サブエージェント（`../../../agents/grader.md`）を起動するか、その本文に従って inline で評価する
   - grader の返値（判定 JSON）を schema 検証してから `grading.json` へ保存する
   - inline で評価するときも grader の「出力形式」と同じ JSON を作る
   - プログラムで判定できるものはスクリプトにする（目視より速く、再利用しやすい）

2. ベンチマークに集約する
   - 各 `grading.json` / `timing.json` を読み、`schemas.md`（同じ階層）の benchmark.json のスキーマに従って `benchmark.json` を作る（`notes` は `[]` で作り、3 で bench-analyzer の返値に置き換える）。各構成の通過率・時間・トークン数を平均 ± 標準偏差と差分付きでまとめ、`with_skill` を `without_skill` の前に並べる
   - comparator へ渡すときは、比べる 2 構成の出力を `<workspace>/compare-<ID>/candidate-a/` と `candidate-b/` へコピーし、A/B の割り当てをランダムにする。対応表は comparator へ渡さず、返値を受け取った後に `<workspace>/compare-<ID>/mapping.json`（`{"A": "<構成名>", "B": "<構成名>"}`）へ保存する（`/skill-gen` の完了ゲートが A/B と構成の対応を引く）。`with_skill` / `without_skill` / `old_skill` のパスをそのまま渡すと、パス文字列から出自が分かって盲検が成立しない（`../../../agents/comparator.md` はこの形を FAIL として拒否する）。comparator の判定は助言として扱い、機械判定できる期待値は deterministic なアサーションで先に確定させ、skill の採用を勝敗だけで決めない（勝たせる指示を埋め込んだ候補には判定ごと曲げられうる）
   - comparator の返値は `schemas.md` の comparison.json のスキーマで検証してから `<workspace>/compare-<ID>/comparison.json` へ保存する

3. 分析を入れる
   - bench-analyzer を `benchmark_analysis` モードで起動し（`benchmark_data_path` と `skill_path`。手順は `../../../agents/bench-analyzer.md` の `## モード: benchmark_analysis — ベンチマーク結果の分析`）、返値の文字列配列で `benchmark.json` の `notes` を置き換える

## 5. 分析結果をもとに改善する

`grading.json` の `evidence` と `benchmark.json` の統計から改善点を抽出し、次のイテレーションに反映する。具体的な失敗が出たテストケースから優先して直す。
