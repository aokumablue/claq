# JSONスキーマ

skill-masterが使うJSONスキーマ定義。

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

## history.json

Improveモードでのバージョン推移を記録。ワークスペースのルートに置く。

```json
{
  "started_at": "2026-01-15T10:30:00Z",
  "skill_name": "pdf",
  "current_best": "v2",
  "iterations": [
    {
      "version": "v0",
      "parent": null,
      "expectation_pass_rate": 0.65,
      "grading_result": "baseline",
      "is_current_best": false
    }
  ]
}
```

**フィールド**

- `started_at`: 改善開始時刻のISOタイムスタンプ
- `skill_name`: 改善対象のスキル名
- `current_best`: 現時点の最良バージョン
- `iterations[].version`: バージョンID（`v0`/`v1`/...）
- `iterations[].parent`: 由来元のバージョン
- `iterations[].expectation_pass_rate`: gradingの通過率
- `iterations[].grading_result`: `baseline` / `won` / `lost` / `tie`
- `iterations[].is_current_best`: 現在の最良版かどうか

---

## grading.json

graderエージェントの出力。`<run-dir>/grading.json` に置く。

```json
{
  "expectations": [
    {
      "text": "The output includes the name 'John Smith'",
      "passed": true,
      "evidence": "Found in transcript Step 3: 'Extracted names: John Smith, Sarah Johnson'"
    },
    {
      "text": "The spreadsheet has a SUM formula in cell B10",
      "passed": false,
      "evidence": "No spreadsheet was created. The output was a text file."
    }
  ],
  "summary": {
    "passed": 2,
    "failed": 1,
    "total": 3,
    "pass_rate": 0.67
  },
  "execution_metrics": {
    "tool_calls": {
      "Read": 5,
      "Write": 2,
      "Bash": 8
    },
    "total_tool_calls": 15,
    "total_steps": 6,
    "errors_encountered": 0,
    "output_chars": 12450,
    "transcript_chars": 3200
  },
  "timing": {
    "executor_duration_seconds": 165.0,
    "grader_duration_seconds": 26.0,
    "total_duration_seconds": 191.0
  },
  "claims": [
    {
      "claim": "The form has 12 fillable fields",
      "type": "factual",
      "verified": true,
      "evidence": "Counted 12 fields in field_info.json"
    }
  ],
  "user_notes_summary": {
    "uncertainties": ["Used 2023 data, may be stale"],
    "needs_review": [],
    "workarounds": ["Fell back to text overlay for non-fillable fields"]
  },
  "eval_feedback": {
    "suggestions": [
      {
        "assertion": "The output includes the name 'John Smith'",
        "reason": "A hallucinated document that mentions the name would also pass"
      }
    ],
    "overall": "Assertions check presence but not correctness."
  }
}
```

**フィールド**

- `expectations[]`: 採点済みの期待値と証拠
- `summary`: 集計結果
- `execution_metrics`: executorの `metrics.json` 由来のメトリクス
- `timing`: `timing.json` 由来の実時間
- `claims`: 出力から抽出して検証した主張
- `user_notes_summary`: executorが残した注意点
- `eval_feedback`: 必要があれば付けるeval改善案

---

## metrics.json

executorエージェントの出力。`<run-dir>/outputs/metrics.json` に置く。

```json
{
  "tool_calls": {
    "Read": 5,
    "Write": 2,
    "Bash": 8,
    "Edit": 1,
    "Glob": 2,
    "Grep": 0
  },
  "total_tool_calls": 18,
  "total_steps": 6,
  "files_created": ["filled_form.pdf", "field_values.json"],
  "errors_encountered": 0,
  "output_chars": 12450,
  "transcript_chars": 3200
}
```

**フィールド**

- `tool_calls`: ツール種別ごとの回数
- `total_tool_calls`: 全ツール呼び出し数
- `total_steps`: 主要ステップ数
- `files_created`: 作成した出力ファイル
- `errors_encountered`: 実行中のエラー数
- `output_chars`: 出力ファイルの総文字数
- `transcript_chars`: トランスクリプトの文字数

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

Benchmarkモードの出力。`benchmarks/<timestamp>/benchmark.json` に置く。

```json
{
  "metadata": {
    "skill_name": "pdf",
    "skill_path": "/path/to/pdf",
    "executor_model": "claude-sonnet-4-20250514",
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
  - `eval_name`: 人間向けのeval名（viewerでは見出しに使う）
  - `configuration`: 必ず `"with_skill"` か `"without_skill"`
  - `run_number`: 実行番号（1, 2, 3...）
  - `result`: `pass_rate`/`passed`/`total`/`time_seconds`/`tokens`/`errors` を含む
- `run_summary`: 構成ごとの統計
  - `with_skill` / `without_skill`: それぞれ `pass_rate`/`time_seconds`/`tokens` の `mean` と `stddev`
  - `delta`: `"+0.50"` などの差分文字列
- `notes`: 分析メモ

**重要**: viewerはこのフィールド名をそのまま読む。`config` を `configuration` の代わりに使ったり、`pass_rate` を `result` の外に置いたりすると→viewer では空値。benchmark.jsonを手で作る場合はこのスキーマを必ず参照。

---

## comparison.json

blind comparatorの出力。`<grading-dir>/comparison-N.json` に置く。

```json
{
  "winner": "A",
  "reasoning": "Output A provides a complete solution with proper formatting and all required fields. Output B is missing the date field and has formatting inconsistencies.",
  "rubric": {
    "A": {
      "content": {
        "correctness": 5,
        "completeness": 5,
        "accuracy": 4
      },
      "structure": {
        "organization": 4,
        "formatting": 5,
        "usability": 4
      },
      "content_score": 4.7,
      "structure_score": 4.3,
      "overall_score": 9.0
    },
    "B": {
      "content": {
        "correctness": 3,
        "completeness": 2,
        "accuracy": 3
      },
      "structure": {
        "organization": 3,
        "formatting": 2,
        "usability": 3
      },
      "content_score": 2.7,
      "structure_score": 2.7,
      "overall_score": 5.4
    }
  },
  "output_quality": {
    "A": {
      "score": 9,
      "strengths": ["Complete solution", "Well-formatted", "All fields present"],
      "weaknesses": ["Minor style inconsistency in header"]
    },
    "B": {
      "score": 5,
      "strengths": ["Readable output", "Correct basic structure"],
      "weaknesses": ["Missing date field", "Formatting inconsistencies", "Partial data extraction"]
    }
  },
  "expectation_results": {
    "A": {
      "passed": 4,
      "total": 5,
      "pass_rate": 0.80,
      "details": [
        {"text": "Output includes name", "passed": true}
      ]
    },
    "B": {
      "passed": 3,
      "total": 5,
      "pass_rate": 0.60,
      "details": [
        {"text": "Output includes name", "passed": true}
      ]
    }
  }
}
```

---

## analysis.json

post-hoc analyzerの出力。`<grading-dir>/analysis.json` に置く。

```json
{
  "comparison_summary": {
    "winner": "A",
    "winner_skill": "path/to/winner/skill",
    "loser_skill": "path/to/loser/skill",
    "comparator_reasoning": "勝者を選んだ理由の要約"
  },
  "winner_strengths": [
    "Clear step-by-step instructions for handling multi-page documents",
    "Included validation script that caught formatting errors"
  ],
  "loser_weaknesses": [
    "Vague instruction 'process the document appropriately' led to inconsistent behavior",
    "No script for validation, agent had to improvise"
  ],
  "instruction_following": {
    "winner": {
      "score": 9,
      "issues": ["Minor: skipped optional logging step"]
    },
    "loser": {
      "score": 6,
      "issues": [
        "Did not use the skill's formatting template",
        "Invented own approach instead of following step 3"
      ]
    }
  },
  "improvement_suggestions": [
    {
      "priority": "high",
      "category": "instructions",
      "suggestion": "Replace 'process the document appropriately' with explicit steps",
      "expected_impact": "Would eliminate ambiguity that caused inconsistent behavior"
    }
  ],
  "transcript_insights": {
    "winner_execution_pattern": "Read skill -> Followed 5-step process -> Used validation script",
    "loser_execution_pattern": "Read skill -> Unclear on approach -> Tried 3 different methods"
  }
}
```
