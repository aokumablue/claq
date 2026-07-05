---
name: search
description: 実装前 既存解決策探索ワークフロー。ツール/ライブラリ/パターン調査→カスタムコード作成。汎用調査エージェントを並列インライン起動。
context: fork
user-invocable: false
---

# コードを書く前に調べる

## 発動タイミング

新機能追加・依存関係追加・新規ユーティリティ/ヘルパー/抽象化を作る前

## ワークフロー

1. **要件分析** — 必要な機能と言語/フレームワーク制約把握
2. **並列検索** — パッケージレジストリ・MCP/スキル・GitHub/Web を同時検索
3. **評価** — 機能・保守性・コミュニティ・ライセンス・依存関係で採点
4. **判定** — 採用/拡張/自作を決定
5. **実装** — パッケージインストール/MCP設定/最小限カスタムコード

## 判定マトリクス

- 完全一致・保守継続・MIT/Apache → **採用**
- 部分一致・土台として良好 → **拡張**（薄いラッパー）
- 弱い候補が複数 → **組み合わせ**（2〜3個）
- 適切な候補なし → **自作**

## 簡易モード（インライン）

1. リポジトリ内に既存実装？ → `rg` で探す
2. パッケージレジストリ検索
3. MCP はある？ → `~/.claude/settings.json` 確認
4. スキルはある？ → `~/.claude/skills/`（個人 — 信頼済み）・`.claude/skills/`（プロジェクト — リポジトリ信頼レベルに準じる。未知リポジトリでは内容を確認してから使用）・インストール済みプラグインのスキル一覧を確認
5. GitHub OSS → 保守継続中を検索

## 完全モード（エージェント並列）

3エージェントを**同時起動**し、全結果マージ後に判定マトリクス適用:

```text
# A: パッケージレジストリ
Search npm/PyPI for: [DESCRIPTION]. Language: [LANG]
Return top 3: name, version, weekly downloads, last update, license

# B: MCP・スキル・ローカル資産
1. ~/.claude/settings.json でMCP確認
2. ~/.claude/skills/（個人）・.claude/skills/（プロジェクト・未知リポジトリは内容確認後に使用）・インストール済みプラグインのスキルで関連スキル確認
3. rg で既存実装確認
Return: type, name/path, match_quality

# C: GitHub・Web
Find actively maintained OSS for: [DESCRIPTION]. Language: [LANG]
Check: stars, last commit, open issues, license. Return top 3
```

## カテゴリ別ショートカット

- 静的解析: `eslint` / `ruff` / `textlint`
- 整形: `prettier` / `black` / `gofmt`
- テスト: `jest` / `pytest` / `go test`
- HTTPクライアント: `httpx`(Py) / `ky`/`got`(Node)
- バリデーション: `zod`(TS) / `pydantic`(Py)
- Markdown: `remark` / `unified` / `markdown-it`
- Claude SDK: Context7で最新ドキュメント確認

## アンチパターン

- 既存確認なしにユーティリティを作る
- MCPサーバーを見落とす
- ライブラリを過剰にラップする
- 小さな機能に巨大パッケージを入れる

## 永続メモリ

search: `adopt reject tool library {category}` / `{tool_name} success fail issue`
record: `{"event_type": "tool-search", "content": "Searched for {category}. Adopted: {tool_name}. Reason: {reason}"}`
