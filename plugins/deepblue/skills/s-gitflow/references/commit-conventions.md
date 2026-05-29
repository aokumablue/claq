# コミット規約とワークフロー詳細

## Conventional Commitsの形式

```
<type>(<scope>): <subject>

[optional body]

[optional footer(s)]
```

### 種類

- `feat` — 新機能: `feat(auth): add OAuth2 login`
- `fix` — バグ修正: `fix(api): handle null response in user endpoint`
- `docs` — ドキュメント: `docs(readme): update installation instructions`
- `style` — 形式のみ: `style: fix indentation in login component`
- `refactor` — リファクタリング: `refactor(db): extract connection pool to module`
- `test` — テスト追加/更新: `test(auth): add unit tests for token validation`
- `chore` — 保守作業: `chore(deps): update dependencies`
- `perf` — 性能改善: `perf(query): add index to users table`
- `ci` — CI/CD: `ci: add PostgreSQL service to test workflow`
- `revert` — 取り消し: `revert: revert "feat(auth): add OAuth2 login"`

### コミットメッセージテンプレート

`.gitmessage` を作成し `git config commit.template .gitmessage` で有効化。

## プルリクエストワークフロー

### タイトル形式

```
<type>(<scope>): <description>
```

### 説明文テンプレート

```md
## 概要
この PR が何をするか簡潔に説明。

## 背景
動機と文脈を説明。

## テスト
- [ ] ユニットテストを追加/更新
- [ ] 結合テストを追加/更新

## チェックリスト
- [ ] セルフレビュー完了
- [ ] テストがパス
- [ ] 関連 issue をリンク

Closes #123
```

## リリース管理

### セマンティックバージョニング（SemVer）

```
MAJOR.MINOR.PATCH
MAJOR: 破壊的変更
MINOR: 新機能、後方互換
PATCH: バグ修正、後方互換
```

### リリース作成

```bash
git tag -a v1.2.0 -m "Release v1.2.0: 概要"
git push origin v1.2.0
```

## Git設定

### 基本設定

```bash
git config --global init.defaultBranch main
git config --global pull.rebase true
git config --global push.default current
git config --global diff.algorithm histogram
```

### 便利なエイリアス

```bash
[alias]
    co = checkout
    br = branch
    ci = commit
    st = status
    last = log -1 HEAD
    visual = log --oneline --graph --all
    undo = reset --soft HEAD~1
```

## Gitフック

### コミット前フック

```bash
#!/bin/bash
# シークレットをチェック
if git diff --cached | grep -E '(password|api_key|secret)'; then
    echo "シークレットの可能性を検出しました。コミットを中止します。"
    exit 1
fi
```

## よくあるワークフロー

### 新機能を始める

```bash
git checkout main && git pull origin main
git checkout -b feature/user-auth
git add . && git commit -m "feat(auth): OAuth2 ログインを実装"
git push -u origin feature/user-auth
```

### 失敗を取り消す

```bash
git reset --soft HEAD~1    # コミット取り消し（変更保持）
git revert HEAD            # push 済みの取り消し
git checkout HEAD -- path  # 特定ファイルの変更取り消し
```
