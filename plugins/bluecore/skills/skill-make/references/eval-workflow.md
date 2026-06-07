# テストケース実行と評価

`skill-make` のevalワークフロー詳細。

## 全体の流れ

1つの流れとして続けて進める。途中で止めない。`/skill-test` や他のtesting skillは使わない。

結果は `<skill-name>-workspace/` に、スキルディレクトリの兄弟として保存。ワークスペース内では `iteration-1/`/`iteration-2/` のようにイテレーション単位で分け、さらに `eval-0/`/`eval-1/` のように個別evalごとのディレクトリを作る。最初から全部作らず、必要になったら順に作る。

## 1. スキルありとベースラインを同じターンで全部起動する

各テストケースについて、同じターンで2つのサブエージェントを起動（スキルありと ベースライン）。スキルありを先に全部やってからベースラインに戻る方式はしない→同時に走らせた方が結果がそろいやすい。

**スキルあり実行**

```text
Execute this task:
- Skill path: <path-to-skill>
- Task: <eval prompt>
- Input files: <eval files if any, or "none">
- Save outputs to: <workspace>/iteration-<N>/eval-<ID>/with_skill/outputs/
- Outputs to save: <what the user cares about — e.g., "the .docx file", "the final CSV">
```

**baseline run**（同じpromptだが、baselineの種類は状況による）

- **新しいスキルを作る場合**: スキルなし。`without_skill/outputs/` に保存
- **既存スキルを改善する場合**: 旧版をbaselineにする。編集前に `cp -r <skill-path> <workspace>/skill-snapshot/` でスナップショットを作り、そのコピーをbaselineに向ける。保存先は `old_skill/outputs/`

各テストケースに `eval_metadata.json` を書く（assertionsは最初は空でよい）。`eval-0` のような番号だけでなく、何を試しているか分かる名前にする→ディレクトリ名にも使う。新しいeval promptを使うイテレーションでは、各新規evalディレクトリにこのファイルを作る（前のイテレーションから勝手に引き継がれない）。

```json
{
  "eval_id": 0,
  "eval_name": "descriptive-name-here",
  "prompt": "The user's task prompt",
  "assertions": []
}
```

## 2. 実行中にassertionsを書く

待つだけでなく、実行中の時間を使って各テストケースの定量的なassertionsを下書き。すでに `evals/evals.json` にassertionsがあるなら見直して、何を検証しているかをユーザーに説明。

良いassertions: 客観的に検証できて、名前を見ただけで内容が分かるもの。benchmark viewerで並んだときに何を見ているか一目で分かるように。文章の味やデザインの良し悪しのような主観的なものは無理に定量化せず、定性的に評価。

下書きができたら `eval_metadata.json` と `evals/evals.json` を更新。viewerでユーザーが何を見るのか（定性的な出力と定量的なbenchmarkの両方）を説明。

## 3. 完了したrunからtimingを記録する

各subagentのタスクが終わると、通知に `total_tokens` と `duration_ms` が含まれる。すぐにrunディレクトリの `timing.json` に保存。

```json
{
  "total_tokens": 84852,
  "duration_ms": 23332,
  "total_duration_seconds": 23.3
}
```

この情報はその通知でしか取れない。後でまとめて処理しようとせず、届いたらすぐ保存。

## 4. gradingして集計し、分析する

すべてのrunが終わったら:

1. **各runをgradingする**
   - grader subagentを起動するか、本文に従ってinlineで評価
   - 各assertionを `grading.json` に保存
   - `expectations` 配列は `text`/`passed`/`evidence` の3フィールドを使う（`name`/`met`/`details` などは使わない）
   - プログラム的に判定できるものはスクリプトを書く（目視より速くて再利用しやすい）

2. **ベンチマークに集約する**

```bash
PYTHONPATH=src python -m bluecore.skills.aggregate_benchmark <workspace>/iteration-N --skill-name <name>
```

これで `benchmark.json` と `benchmark.md` が作られる。各構成の通過率・時間・トークン数が平均±標準偏差と差分付きでまとまる。手で `benchmark.json` を作る場合は `references/schemas.md` のビューアー用スキーマを参照。`with_skill` を `without_skill` の前に並べる。

3. **分析を入れる**
   - `benchmark.json` と各 `grading.json` を直接読んで分析する
   - `../../agents/bench-analyzer.md` の「ベンチマーク結果の分析」セクションを参照
   - 例: 常に通るだけで差が出ないassertions・ばらつきが大きいeval・時間/トークンのトレードオフ

## 5. 分析結果をもとに改善する

`grading.json` の `evidence` フィールドと `benchmark.json` の統計から改善点を抽出し、次のイテレーションに反映する。改善は具体的な失敗が出たテストケースを優先。
