---
paths:
  - "**/*.test.ts"
  - "**/*.test.tsx"
  - "**/*.test.js"
  - "**/*.spec.ts"
  - "**/*.spec.tsx"
  - "**/*.spec.js"
  - "**/test_*.py"
  - "**/*_test.py"
  - "**/*_spec.rb"
  - "**/*_test.rb"
  - "**/*.test.coffee"
  - "**/*.spec.coffee"
---

# テスト標準

## テスト構造

- Arrange / Act / Assert を分ける
- 1テストで1つの振る舞いを検証
- 前提と検証を分かりやすく分ける

## テスト命名

- 何を・どの条件で・どうなるかを明確に
- 実装詳細ではなく振る舞いを表現
- 失敗時に原因が読み取れる名前に
