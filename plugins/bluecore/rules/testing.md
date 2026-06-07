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

- Arrange / Act / Assert の3段に分ける
- 1 テストで 1 つの振る舞いだけ検証する

## テスト命名

- 「何を・どの条件で・どうなるか」を名前に含める
- 実装詳細でなく振る舞いを表現する
