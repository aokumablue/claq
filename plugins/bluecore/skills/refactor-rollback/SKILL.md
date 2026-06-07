---
name: refactor-rollback
description: refactor前にファイル単位ロールバック計画を確定し、失敗時の復旧を高速化する。
context: fork
user-invocable: false
---

# リファクタ ロールバック設計

## 発動タイミング

- `/refactor` preflight・複数ファイルにまたがる変更・並列サブエージェント実行前

失敗時に迷わず復旧できるよう、**ファイル単位**の Rollback Blueprint を事前に固定する。

## 入力

- 変更対象: `scope_files` → 引数パス → `git diff --name-only HEAD`
- `refactor-prep` のグループ/依存関係/テストセット
- 高リスク境界（公開API・外部I/O・永続化境界）

`refactor-prep` 入力契約:

```json
{
  "scope_files": ["path/a.py", "path/b.py"],
  "groups": [["path/a.py"], ["path/b.py"]],
  "deps": [{"from": 1, "to": 0}],
  "tests": {
    "baseline": ["python3 -m pytest -q"],
    "group": ["python3 -m pytest -q tests/test_a.py"],
    "final": ["python3 -m pytest -q", "ruff check plugins/bluecore/src plugins/bluecore/tests"]
  }
}
```

必須: `scope_files` / `groups` / `deps` / `tests.baseline` / `tests.group` / `tests.final`

## 手順

1. 変更対象列挙→各ファイルの復旧コマンド定義
2. 高リスク境界を `CAUTION` タグ付け
3. ファイルごとに検証コマンドを紐付け
4. グループ依存がある場合、復旧順序を依存逆順で定義
5. Rollback Blueprint 出力

`CAUTION` 判定: 公開API/外部I/O/永続化境界を含む・依存グループをまたぐ

復旧順: `deps` をトポロジカル順に解決し、rollback 時は逆順で処理

## 出力形式

```text
Rollback Blueprint
──────────────────────────────
Scope: {n} files
File Rules:
  - {file}: revert="git checkout -- {file}" verify="{cmd}" risk={SAFE|CAUTION}
Order:
  - revert group {g2} -> {g1}
Skip Rules:
  - {file}: {reason} (required_action={manual_review|extra_test|keep})
──────────────────────────────
```

必須キー:

```json
{
  "file_rules": [{"file": "path/a.py", "revert": "git checkout -- path/a.py", "verify": "python3 -m pytest -q tests/test_a.py", "risk": "CAUTION"}],
  "revert_files": ["path/a.py"],
  "deps_order": [{"from": 1, "to": 0}],
  "risk_files": [{"file": "path/a.py", "risk": "CAUTION", "reason": "public_api", "action": "manual_review"}],
  "skip_rules": [{"file": "path/a.py", "reason": "dynamic_reference", "required_action": "extra_test"}]
}
```

## ルール

- 復旧単位は**ファイル単位**
- 復旧コマンドは `git checkout -- <file>`
- 不確実な変更は `Skip Rules` に記録
- 機能変更禁止（WHAT不変）

## 永続メモリ

search: `refactor rollback blueprint {file_path}` / `revert failure pattern`
record: `{"event_type":"refrb","content":"Scope:{scope}. RevertPlan:{n_files}. RiskFiles:{risk_files}"}`
