---
name: tdd
description: 新機能実装/バグ修正/リファクタリング時に使用。ユニット・統合テスト中心のTDD徹底。
context: fork
user-invocable: false
---

# テスト駆動開発

## 発動タイミング

新機能実装・バグ修正（まず再現テスト）・リファクタリング・重要ビジネスロジック

## TDD サイクル

`RED → GREEN → REFACTOR → REPEAT`

1. **SCAFFOLD** — インターフェイス定義（関数シグネチャ・型・契約）
2. **RED** — 失敗するテストを書く（失敗を確認）
3. テスト実行・失敗確認
4. **GREEN** — テストを通す最小限の実装
5. テスト実行・合格確認
6. **REFACTOR** — グリーンを保ちながら改善
7. テスト再実行でグリーン確認
8. カバレッジ確認

## テスト種別

- ユニット: 独立した個別 fn
- 統合: APIエンドポイント・DB操作

## エッジケース

Null/Undefined・空配列/文字列・無効型・境界値・エラーパス・競合状態・大規模データ・特殊文字（Unicode・SQL）

## カバレッジ要件

- リポジトリ全体: CLAUDE.md の目標に従う
- クリティカルコード 100% 必須: 金融計算・認証/認可・セキュリティ・コアビジネスロジック（1行の未テストコードも本番バグになりうる）

## 永続メモリ

search: `test {機能ドメイン}` (file_pattern: *test*.py) / `bug fix error {関連キーワード}`
record: `{"event_type": "tdd", "content": "{テストサマリー: X tests, Y% coverage}"}`

詳細な実装支援が必要な場合は `bluecore:tdd-writer` を明示起動する。
