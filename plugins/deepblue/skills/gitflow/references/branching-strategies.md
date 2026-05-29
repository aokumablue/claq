# ブランチ戦略の詳細

## Pull Requestベースのフロー（GitHub / GitLab）

継続的デプロイと小〜中規模チームに最適。

```
main (protected, always deployable)
  │
  ├── feature/user-auth      → PR → merge to main
  ├── feature/payment-flow   → PR → merge to main
  └── fix/login-bug          → PR → merge to main
```

**ルール:**

- `main` は常にデプロイ可能に保つ
- `main` からfeatureブランチを作成
- レビュー準備ができたらPull Request / Merge Requestを作成
- 承認され、CIが通ったら `main` にマージ
- マージ後すぐにデプロイ

## トランクベース開発（高速開発チーム向け）

強力なCI/CDと機能フラグを備えたチームに最適。

```
main (trunk)
  │
  ├── short-lived feature (1-2 days max)
  ├── short-lived feature
  └── short-lived feature
```

**ルール:**

- 全員が `main` か非常に短命なブランチにコミット
- 未完成の機能は機能フラグで隠す
- マージ前にCIを通す
- 1日に複数回デプロイ

## GitFlow（複雑でリリースサイクル駆動）

定期リリースやエンタープライズ向けプロジェクトに最適。

```
main (production releases)
  │
  └── develop (integration branch)
        │
        ├── feature/user-auth
        ├── feature/payment
        │
        ├── release/0.0.1    → merge to main and develop
        │
        └── hotfix/critical  → merge to main and develop
```

**ルール:**

- `main` には本番投入可能なコードのみを置く
- `develop` は統合ブランチ
- featureブランチは `develop` から切り、`develop` に戻してマージ
- releaseブランチは `develop` から切り、`main` と `develop` の両方にマージ
- hotfixブランチは `main` から切り、`main` と `develop` の両方にマージ

## マージとリベース

### マージ（履歴を保持する）

```bash
git checkout main
git merge feature/user-auth
```

**使う場面:** featureブランチを `main` にマージ・正確な履歴を残す・複数人がブランチで作業・push済み

### リベース（直線的な履歴）

```bash
git checkout feature/user-auth
git rebase main
```

**使う場面:** ローカルfeatureブランチは最新mainに追従・自分だけのブランチ

**リベースしない方がよい場合:** push済み・他の人が基に作業中・保護ブランチ

## コンフリクト解決

```bash
# コンフリクトしているファイルを確認
git status

# 方法 1: 手動で解決（マーカーを削除し正しい内容を残す）
# 方法 2: git mergetool
# 方法 3: git checkout --ours / --theirs

# 解決後
git add src/auth/login.ts
git commit
```

**防止策:** ブランチは小さく短命に・mainに頻繁にリベース・PRは迅速にマージ

## ブランチ管理

### 命名規則

```
feature/user-authentication
feature/JIRA-123-payment-integration
fix/login-redirect-loop
hotfix/critical-security-patch
release/1.2.0
experiment/new-caching-strategy
```

### ブランチの整理

```bash
git branch --merged main | grep -v "^\*\|main" | xargs -n 1 git branch -d
git fetch -p
```
