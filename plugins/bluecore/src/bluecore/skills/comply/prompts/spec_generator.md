<!-- markdownlint-disable MD007 -->
あなたは、コーディングエージェント向けの skill / rule ファイルを分析しています。
あなたのタスクは、この skill が有効なときにエージェントが辿るべき**観測可能な行動シーケンス**を抽出することです。

各ステップは自然言語で記述してください。正規表現パターンは使わないでください。

出力は、次の形式の有効な YAML のみとしてください（Markdown のフェンスやコメントは不要です）:

id: <kebab-case-id>
name: <Human readable name>
source_rule: <file path provided>
version: "1.0"

steps:
  - id: <snake_case>
    description: <what the agent should do>
    required: true|false
    detector:
      description: <natural language description of what tool call to look for>
      after_step: <step_id this must come after, optional — omit if not needed>
      before_step: <step_id this must come before, optional — omit if not needed>

scoring:
  threshold_promote_to_hook: 0.6

ルール:
- detector.description は、tool call のパターンではなく意味を説明してください
  良い例: "Write or Edit a test file (not an implementation file)"
  悪い例: "Write|Edit with input matching test.*\.py"
- 順序が重要な skill では before_step/after_step を使ってください（例: TDD: test before impl）
- 順序ではなく存在だけが重要な skill では ordering constraints を省略してください
- skill が「任意」または「if applicable」と言っている場合にのみ required: false にしてください
- 3〜7 ステップが理想です。細かく分けすぎないでください
- 重要: コロンを含む YAML 文字列値はすべてダブルクォートで囲んでください
  良い例: description: "Use conventional commit format (type: description)"
  悪い例: description: Use conventional commit format (type: description)

解析する skill ファイル:

---
{skill_content}
---
