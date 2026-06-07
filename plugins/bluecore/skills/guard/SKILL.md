---
name: guard
description: 本番環境作業・エージェント自律実行時に破壊的操作を防ぐ。
context: fork
user-invocable: false
---

# Safety Guard

## 発動タイミング

- 本番環境作業・エージェント自律実行・特定ディレクトリ制限・機密性高い操作（マイグレーション/デプロイ/データ変更）

## 監視パターン

- `rm -rf` (/, ~, プロジェクトルート)
- `git push --force` / `git reset --hard` / `git checkout .`
- `DROP TABLE` / `DROP DATABASE`
- `docker system prune` / `kubectl delete`
- `chmod 777` / `sudo rm` / `npm publish`
- `--no-verify` を含む任意コマンド

検知時→コマンド内容提示・確認要求・安全な代替案提案。

## 実装

PreToolUse フックで Bash/Write/Edit/MultiEdit を検知。コマンド・パスを有効ルールと照合後、実行許可。

## 永続メモリ

search: `guard block prevent dangerous` / `{command_pattern} block risk`
record: `{"event_type": "guard-block", "content": "Blocked: {command}. Reason: {reason}. Alternative: {alternative}"}`
