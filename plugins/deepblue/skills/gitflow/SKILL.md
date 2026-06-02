---
name: gitflow
description: Gitワークフローパターン。ブランチ戦略/コミット規約/マージvsリベース/コンフリクト解決/全規模チーム向け共同開発ベストプラクティス。
context: fork
user-invocable: false
---

# Git ワークフロー

## 発動タイミング

- ブランチ戦略決定・コミットメッセージ/PR記述・マージコンフリクト解決・リリース管理

## ブランチ戦略

- PR ベース: 規模問わず・継続的デプロイ → SaaS・Webアプリ・スタートアップ
- トランクベース: 5名以上・1日複数回デプロイ → 高速開発・機能フラグ運用
- GitFlow: 10名以上・定期リリース → エンタープライズ・規制業界

詳細: `references/branching-strategies.md`

## コミットメッセージ

`<type>(<scope>): <subject>` — `feat` / `fix` / `docs` / `refactor` / `test` / `chore` / `perf`

詳細: `references/commit-conventions.md`

## マージ vs リベース

- feature → main: マージ
- ローカルfeatureをmainに追従: リベース
- push済み/他者作業中: マージ（リベース禁止）
- 直線履歴: リベース

## アンチパターン

- mainに直接コミット → featureブランチ+PR
- シークレットをコミット → `.gitignore`+環境変数
- 巨大PR（1000行超） → 小さく分割
- 公開履歴を書き換える → revert
- 長期featureブランチ → 頻繁にリベース

## 早見表

```
git checkout -b feature/name
git merge branch-name
git rebase main
git reset --soft HEAD~1
git revert HEAD
git stash push -m "message"
```

## 参照ファイル

- `references/branching-strategies.md` — ブランチ戦略詳細・コンフリクト解決
- `references/commit-conventions.md` — Conventional Commits・PRテンプレート・リリース管理

## 永続メモリ

search: `git commit branch merge rebase`
record: `{"event_type": "git-workflow", "content": "Merged {branch} to main. Conflicts: {n}. Strategy: {strategy}"}`
