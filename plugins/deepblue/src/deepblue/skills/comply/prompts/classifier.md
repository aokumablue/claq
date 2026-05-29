<!-- markdownlint-disable MD007 -->
あなたは、コーディングエージェントのセッションから得た tool call を、期待される行動ステップと照合しています。

各 tool call について、それがどのステップに属するか（属さない場合はなし）を判断してください。1 つの tool call が一致できるステップは最大 1 つです。

Steps:
{steps_description}

Tool calls (numbered):
{tool_calls}

出力は JSON オブジェクトのみとしてください。step_id をキー、対応する tool call 番号の配列を値にします。
tool call が存在するステップだけを含めてください。どのステップにも一致しない tool call がある場合は、その step を省略してください。

Example response:
{"write_test": [0, 1], "run_test_red": [2], "write_impl": [3, 4]}

Rules:
- tool call の意味に基づいて一致させ、キーワードだけで判断しないでください
- `test_calculator.py` への Write は、内容が implementation っぽくても test file の書き込みです
- `calculator.py` への Write は、test helper を含んでいても implementation の書き込みです
- `pytest` を実行する Bash が "FAILED" を出力したら、RED フェーズのテスト実行です
- `pytest` を実行する Bash が "passed" を出力したら、GREEN フェーズのテスト実行です
- 各 tool call は最大 1 つのステップにだけ一致させてください（最適なものを選ぶ）
- 一致しない tool call は含めないでください
