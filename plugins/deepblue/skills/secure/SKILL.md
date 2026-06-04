---
name: secure
description: 認証追加/ユーザー入力処理/シークレット管理/APIエンドポイント作成/支払い・機微情報実装時に使用。包括的セキュリティチェックリスト・パターン提供。
context: fork
user-invocable: false
---

# セキュリティレビュー

## 発動タイミング

認証/認可実装・ユーザー入力/ファイルアップロード・新APIエンドポイント・シークレット/認証情報・支払い機能・機微データ保存/送信・サードパーティAPI連携

## チェックリスト

### 1. シークレット管理
- [ ] APIキー・トークン・パスワードがハードコードされていない
- [ ] 全シークレットが環境変数
- [ ] `.env.local` が .gitignore に含まれている

### 2. 入力検証
- [ ] 全ユーザー入力をスキーマで検証
- [ ] ファイルアップロード制限（サイズ・MIME・拡張子）
- [ ] ホワイトリスト検証（ブラックリストでない）
- [ ] エラーメッセージで機微情報を漏らしていない

### 3. SQLインジェクション
- [ ] 全DBクエリでパラメータ化クエリまたはプリペアドステートメント使用
- [ ] SQL に文字列連結を使っていない

### 4. 認証と認可
- [ ] トークンを httpOnly クッキーに保存（localStorage 禁止）
- [ ] 機微な操作の前に認可チェック
- [ ] RLS 有効化 / RBAC 実装

### 5. XSS
- [ ] ユーザー提供 HTML をサニタイズ
- [ ] CSP ヘッダー設定
- [ ] フレームワーク標準 XSS 保護を使用

### 6. CSRF
- [ ] 状態変更操作に CSRF トークン
- [ ] クッキーに `SameSite=Strict`

### 7. レート制限
- [ ] 全 API エンドポイントにレート制限
- [ ] 高負荷処理（検索・アップロード）には厳しい制限

### 8. 機微情報露出
- [ ] ログにパスワード・トークン・シークレットを出していない
- [ ] ユーザー向けエラーメッセージは汎用的

### 9. 依存関係
- [ ] 既知の脆弱性がない
- [ ] ロックファイルをコミット済み
- [ ] Dependabot 有効化

## テスト

- 未認証→401 / 無効トークン→401
- 権限なし→403 / 自分のリソースのみアクセス
- 無効入力→400 / 制限超過→429

詳細: `references/cloud-infrastructure-security.md`

## 永続メモリ

search: `security vulnerability violation {category}` / `{vulnerability_type} fix repair patch`
record: `{"event_type": "security-review", "content": "Security review: {pass}/{total} checks passed. Issues: {issues}"}`
