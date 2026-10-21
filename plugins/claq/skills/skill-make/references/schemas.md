# JSONスキーマ

## 目次

- evals.json
- grading.json
- timing.json
- benchmark.json
- comparison.json

`skill-make` が使う JSON スキーマ定義。

---

## evals.json

skillのeval定義。スキルディレクトリ内の `evals/evals.json` に置く。

```json
{
  "skill_name": "example-skill",
  "evals": [
    {
      "id": 1,
      "prompt": "User's example prompt",
      "expected_output": "Description of expected result",
      "files": ["evals/files/sample1.pdf"],
      "expectations": [
        "The output includes X",
        "The skill used script Y"
      ]
    }
  ]
}
```

**フィールド**

- `skill_name`: frontmatterのskill名と一致させる
- `evals[].id`: 一意な整数ID
- `evals[].prompt`: 実行するタスク
- `evals[].expected_output`: 成功時の人間向け説明
- `evals[].files`: 入力ファイルの相対パス（任意）
- `evals[].expectations`: 検証可能な期待値の配列


---

## grading.json

grader エージェントの返値。形式と不変条件は `../../../agents/grader.md` の「出力契約」「出力形式」が正。呼び出し元がそれで検証したうえで `<run-dir>/grading.json` へ保存する（grader 自身はファイルを書かない）。

---

## timing.json

1回の実行にかかった実時間。`<run-dir>/timing.json` に置く。

**取得方法**: サブエージェントのタスク完了時、通知に `total_tokens` と `duration_ms` が含まれる。その場で保存→後からは復元不可。

```json
{
  "total_tokens": 84852,
  "duration_ms": 23332,
  "total_duration_seconds": 23.3,
  "executor_start": "2026-01-15T10:30:00Z",
  "executor_end": "2026-01-15T10:32:45Z",
  "executor_duration_seconds": 165.0,
  "grader_start": "2026-01-15T10:32:46Z",
  "grader_end": "2026-01-15T10:33:12Z",
  "grader_duration_seconds": 26.0
}
```

---

## benchmark.json

eval-workflow §4 の集約結果。`<workspace>/iteration-<N>/benchmark.json` に置く。

```json
{
  "metadata": {
    "skill_name": "pdf",
    "skill_path": "/path/to/pdf",
    "executor_model": "executor-model",
    "analyzer_model": "most-capable-model",
    "timestamp": "2026-01-15T10:30:00Z",
    "evals_run": [1, 2, 3],
    "runs_per_configuration": 3
  },
  "runs": [
    {
      "eval_id": 1,
      "eval_name": "Ocean",
      "configuration": "with_skill",
      "run_number": 1,
      "result": {
        "pass_rate": 0.85,
        "passed": 6,
        "failed": 1,
        "total": 7,
        "time_seconds": 42.5,
        "tokens": 3800,
        "tool_calls": 18,
        "errors": 0
      },
      "expectations": [
        {"text": "...", "passed": true, "evidence": "..."}
      ],
      "notes": [
        "Used 2023 data, may be stale",
        "Fell back to text overlay for non-fillable fields"
      ]
    }
  ],
  "run_summary": {
    "with_skill": {
      "pass_rate": {"mean": 0.85, "stddev": 0.05, "min": 0.80, "max": 0.90},
      "time_seconds": {"mean": 45.0, "stddev": 12.0, "min": 32.0, "max": 58.0},
      "tokens": {"mean": 3800, "stddev": 400, "min": 3200, "max": 4100}
    },
    "without_skill": {
      "pass_rate": {"mean": 0.35, "stddev": 0.08, "min": 0.28, "max": 0.45},
      "time_seconds": {"mean": 32.0, "stddev": 8.0, "min": 24.0, "max": 42.0},
      "tokens": {"mean": 2100, "stddev": 300, "min": 1800, "max": 2500}
    },
    "delta": {
      "pass_rate": "+0.50",
      "time_seconds": "+13.0",
      "tokens": "+1700"
    }
  },
  "notes": [
    "Assertion 'Output is a PDF file' passes 100% in both configurations - may not differentiate skill value",
    "Eval 3 shows high variance (50% ± 40%) - may be flaky or model-dependent",
    "Without-skill runs consistently fail on table extraction expectations",
    "Skill adds 13s average execution time but improves pass rate by 50%"
  ]
}
```

**フィールド**

- `metadata`: ベンチマーク実行情報
  - `skill_name`: スキル名
  - `timestamp`: 実行時刻
  - `evals_run`: 実行したevalの一覧
  - `runs_per_configuration`: 構成ごとの実行回数（例: 3）
- `runs[]`: 個々の実行結果
  - `eval_id`: 数値ID
  - `eval_name`: 人間向けのeval名
  - `configuration`: `"with_skill"` か、比較ベースラインの `"without_skill"` / `"old_skill"`（既存スキル改善パスは `old_skill` を出力する）。ベースライン名は fixture 全体で一方だけに揃える
  - `run_number`: 実行番号（1, 2, 3...）
  - `result`: `pass_rate`/`passed`/`total`/`time_seconds`/`tokens`/`errors` を含む
  - `expectations`: 期待値ごとの判定。**欠けると `bench-analyzer` が FAIL する**
- `run_summary`: 構成ごとの統計
  - `with_skill` と使用したベースライン（`without_skill` または `old_skill`）: それぞれ `pass_rate`/`time_seconds`/`tokens` の `mean` と `stddev`
  - `delta`: `"+0.50"` などの差分文字列
- `notes`: bench-analyzer（`benchmark_analysis` モード）の返値（文字列配列）

**重要**: フィールド名はこのスキーマのとおりにする（`configuration` を `config` にしない、`pass_rate` を `result` の外に置かない）。benchmark.jsonを手で作る場合はこのスキーマを必ず参照。

---

## comparison.json

comparator エージェントの返値。形式は `../../../agents/comparator.md` の「出力契約」「出力形式」が正。呼び出し元がそれで検証したうえで `<workspace>/compare-<ID>/comparison.json` へ保存する（comparator 自身はファイルを書かない）。
