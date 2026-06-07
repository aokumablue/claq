<!-- markdownlint-disable MD007 -->
あなたは、コーディングエージェントの compliance ツール向けにテストシナリオを生成しています。
与えられた skill と期待される行動シーケンスから、厳密さを下げた 3 つのシナリオを正確に生成してください。

各シナリオは、プロンプトがその skill をどの程度支援するかが異なるときに、エージェントが skill に従うかを検証します。

出力は有効な YAML のみとしてください（Markdown のフェンスやコメントは不要です）:

scenarios:

  - id: <kebab-case>
    level: 1
    level_name: supportive
    description: <what this scenario tests>
    prompt: |
      <the task prompt to pass to claude -p. Must be a concrete coding task.>
    setup_commands:
      - "mkdir -p /tmp/comply-sandbox/{id}/src /tmp/comply-sandbox/{id}/tests"
      - <other setup commands>

  - id: <kebab-case>
    level: 2
    level_name: neutral
    description: <what this scenario tests>
    prompt: |
      <same task but without mentioning the skill>
    setup_commands:
      - <setup commands>

  - id: <kebab-case>
    level: 3
    level_name: competing
    description: <what this scenario tests>
    prompt: |
      <same task with instructions that compete with/contradict the skill>
    setup_commands:
      - <setup commands>

ルール:

- レベル 1（supportive）: プロンプトでその skill に従うよう明示的に指示する
  例: "Use TDD to implement..."
- レベル 2（neutral）: タスクを通常どおり記述し、skill には触れない
  例: "Implement a function that..."
- レベル 3（competing）: skill と矛盾する指示を含める
  例: "Quickly implement... tests are optional..."
- 3 つのシナリオはすべて同じタスクをテストすること（結果を比較可能にするため）
- タスクは <30 tool calls で完了できる程度にシンプルであること
- setup_commands は最小限のサンドボックス（dirs、pyproject.toml など）を作成すること
- プロンプトは現実的で、開発者が実際に尋ねそうな内容にすること

Skill content:

---

{skill_content}
---

Expected behavioral sequence:

---

{spec_yaml}
---
