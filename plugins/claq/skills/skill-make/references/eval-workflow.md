# テストケース実行と評価

`skill-make` の eval ワークフローの詳細。

## 全体の流れ

1 つの流れとして続けて進める（止まってよい条件は SKILL.md の「停止点」）。他の testing skill には委ねない。

結果は `<skill-name>-workspace/` に、スキルディレクトリの兄弟として保存する。ワークスペース内はイテレーション単位（`iteration-1/` / `iteration-2/`）に分け、その中に eval ごとのディレクトリ（`eval-0/` / `eval-1/`）を作る。最初から全部は作らず、必要になった順に作る。

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
```

ベースライン（プロンプトは同じ。ベースラインの種類は状況で決まる）:

- 新しいスキルを作る場合: スキルなし。`without_skill/outputs/` に保存する
- 既存スキルを改善する場合: 旧版をベースラインにする。編集前に `cp -r <skill-path> <workspace>/skill-snapshot/` でスナップショットを取り、そのコピーを使う。保存先は `old_skill/outputs/`

各テストケースに `eval_metadata.json` を書く（assertions は最初は空でよい）。`eval-0` のような番号だけでなく、何を試しているか分かる名前を付け、ディレクトリ名にも使う。新しい eval プロンプトを使うイテレーションでは、新しい eval ディレクトリごとにこのファイルを作る（前のイテレーションからは引き継がれない）。

```json
{
  "eval_id": 0,
  "eval_name": "descriptive-name-here",
  "prompt": "The user's task prompt",
  "assertions": []
}
```

## 2. 実行中に assertions を書く

実行を待つ間に、各テストケースの定量的な assertions を下書きする。`evals/evals.json` に既に assertions があれば見直し、何を検証しているかをユーザーに説明する。

良い assertions は、客観的に検証できて、名前だけで内容が分かるもの（benchmark viewer で並んだときに何を見ているかが一目で分かる）。文章の味やデザインの良し悪しのような主観的なものは無理に定量化せず、定性的に評価する。

下書きができたら `eval_metadata.json` と `evals/evals.json` を更新し、viewer でユーザーが何を見るのか（定性的な出力と定量的な benchmark の両方）を説明する。

## 3. 完了した run から timing を記録する

各サブエージェントのタスクが終わると、通知に `total_tokens` と `duration_ms` が含まれる。この情報はその通知でしか取れないので、届いたらすぐ run ディレクトリの `timing.json` に保存する（後でまとめて処理しない）。

```json
{
  "total_tokens": 84852,
  "duration_ms": 23332,
  "total_duration_seconds": 23.3
}
```

## 4. grading して集計し、分析する

すべての run が終わったら:

1. 各 run を grading する
   - grader サブエージェントを起動するか、本文に従って inline で評価する
   - grader の返値（判定 JSON）を schema 検証してから `grading.json` へ保存する。保存するのは本ワークフロー側で、grader 自身はファイルを書かない（`../../../agents/grader.md`）
   - `expectations` 配列は `text` / `passed` / `evidence` の 3 フィールドを使う（`name` / `met` / `details` などは使わない）
   - プログラムで判定できるものはスクリプトにする（目視より速く、再利用しやすい）

2. ベンチマークに集約する
   - 各 `grading.json` / `timing.json` を読み、`schemas.md`（同じ階層）のビューアー用スキーマに従って `benchmark.json` を作る。各構成の通過率・時間・トークン数を平均 ± 標準偏差と差分付きでまとめ、`with_skill` を `without_skill` の前に並べる
   - comparator へ渡すときは、比べる 2 構成の出力を `<workspace>/compare-<ID>/candidate-a/` と `candidate-b/` へコピーし、A/B の割り当てをランダムにする。対応表は呼び出し元だけが持つ。`with_skill` / `without_skill` / `old_skill` のパスをそのまま渡すと、パス文字列から出自が分かって盲検が成立しない（`../../../agents/comparator.md` はこの形を FAIL として拒否する）。comparator の判定は助言として扱い、機械判定できる期待値は deterministic なアサーションで先に確定させ、skill の採用を勝敗だけで決めない（勝たせる指示を埋め込んだ候補には判定ごと曲げられうる）

3. 分析を入れる
   - `benchmark.json` と各 `grading.json` を直接読んで分析する
   - `../../../agents/bench-analyzer.md` の `## モード: benchmark_analysis — ベンチマーク結果の分析` を参照する
   - 例: 常に通るだけで差が出ない assertions・ばらつきが大きい eval・時間/トークンのトレードオフ

## 5. 分析結果をもとに改善する

`grading.json` の `evidence` と `benchmark.json` の統計から改善点を抽出し、次のイテレーションに反映する。具体的な失敗が出たテストケースから優先して直す。
