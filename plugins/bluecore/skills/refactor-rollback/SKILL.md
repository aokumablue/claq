---
name: refactor-rollback
description: refactor-prep 実行後にその出力（対象分割・グループ）を受けてファイル単位ロールバック計画を確定し、失敗時の復旧を高速化する。refactor-prep 未実行の段階では発動しない。
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

1. 変更対象列挙→各ファイルの tracked/untracked を `git ls-files` で判定してから復旧コマンドを確定（tracked=`git checkout -- {file}` / untracked（新規作成）=`rm {file}`）。`git ls-files` 不一致だけで untracked 確定しない — 対象パスを canonicalize し、リポジトリルート配下の相対パスで `..` を含まないことを検証する。満たさないパス（`..`・絶対パス・リポジトリ外）は SAFE/CAUTION 判定せず `rm` を生成せず、Skip Rules（`required_action=manual_review`）へ回す（fail-safe）
2. 高リスク境界を `CAUTION` タグ付け
3. ファイルごとに検証コマンドを紐付け
4. グループ依存がある場合、復旧順序を依存逆順で定義。循環依存時（refactor-prep が記録しうる）は循環に属する全ファイルを1グループとして一括 revert 対象にし、Skip Rules に cyclic-dependency を `required_action=bulk_revert` で記録（確定的な一括 revert 対象であり、手動判断を要する不確実ケースとは区別する）
5. Rollback Blueprint 出力

`CAUTION` 判定: 公開API/外部I/O/永続化境界を含む・依存グループをまたぐ

復旧順: `deps` をトポロジカル順に解決し、rollback 時は逆順で処理

## 出力形式

```text
Rollback Blueprint
──────────────────────────────
Scope: {n} files
File Rules:
  - {file}: revert="{git checkout -- {file} | rm {file}}" verify="{cmd}" risk={SAFE|CAUTION}
Order:
  - revert group {g2} -> {g1}
Skip Rules:
  - {file}: {reason} (required_action={manual_review|extra_test|keep|bulk_revert})
──────────────────────────────
```

## ルール

- 復旧単位は**ファイル単位**（循環依存グループのみ一括）
- 復旧コマンドは tracked=`git checkout -- {file}` / untracked（新規作成）=`rm {file}`。`git ls-files` で判定してから確定。untracked と判定しても、canonicalize してリポジトリルート配下の相対パス（`..` 非含有）でなければ `rm` を生成せず Skip Rules（`required_action=manual_review`）に回す（範囲外パスの不可逆削除を防ぐ）
- 不確実な変更は `Skip Rules` に `required_action={manual_review|extra_test|keep}` で記録。循環依存で一括 revert が必要なグループも `Skip Rules` に記録するが、これは確定的な復旧対象のため `required_action=bulk_revert` で区別する
- 機能変更禁止（WHAT不変）

## 永続メモリ

search: `refactor rollback blueprint {file_path}` / `revert failure pattern`
record: `{"event_type":"refrb","content":"Scope:{scope}. RevertPlan:{n_files}. RiskFiles:{risk_files}"}`
