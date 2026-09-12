---
name: refactor-prep
description: リファクタ着手前に対象分割・依存可視化・実行前テストセットを最小コストで確定する事前準備スキル。
context: fork
user-invocable: false
---

# リファクタ事前準備

`refactor` 実行前に対象分割・依存関係・検証テストを固定し手戻りを減らす。

## 手順

1. **対象確定**（優先順）: 渡されたスコープリスト → 引数パス（ディレクトリ=配下全ファイル/ファイル=そのファイル） → `git diff --name-only HEAD`
2. **分割**: 同時変更が必要な塊でグループ化。依存が薄いグループを先行処理
3. **依存可視化**: import/参照関係確認→グループ間依存順明示。循環・高リスク境界（公開API・外部I/O）を先に記録
4. **テストセット確定**: baseline（全体）・グループ単位・final gate（テスト+lint）

## 出力

テキストサマリーに加え、下流（`refactor-rollback` / `refactor` コマンド）が参照する JSON 契約を同時に渡す。フィールド名・構造は `../refactor-rollback/SKILL.md` の入力契約と一致させる。

```
Refactor Preflight
──────────────────────────────
Scope:      {n} files
Groups:     {g1}, {g2}, ...
Dependencies:
  - {g2} depends on {g1}
Test Set:
  - baseline: {cmd}
  - group: {cmds}
  - final: {cmds}
──────────────────────────────
```

JSON 契約（下流3ファイルが前提とする形。`deps.from`/`deps.to` は `groups` 配列のインデックス）:

```json
{
  "scope_files": ["path/a.py", "path/b.py"],
  "groups": [["path/a.py"], ["path/b.py"]],
  "deps": [{"from": 1, "to": 0}],
  "tests": {
    "baseline": ["python3 -m pytest -q"],
    "group": ["python3 -m pytest -q tests/test_a.py", "python3 -m pytest -q tests/test_b.py"],
    "final": ["python3 -m pytest -q", "<プロジェクトの linter コマンド>"]
  }
}
```

必須: `scope_files` / `groups` / `deps` / `tests.baseline` / `tests.group` / `tests.final`（キーは必ず出力する。値は非空を要求しない）。baseline / group / final のカテゴリ全体で実在確認できない場合に限り、該当配列を**空配列**にする — これが JSON 契約上の「検証手段なし」の signal そのものであり、テキストサマリー側の「検証手段なし」表記は人間向けの重複表現にすぎない。JSON だけを読む下流（`refactor-rollback`）は空配列を「検証手段なし」として扱う契約になっているため、テキストの記述漏れがあっても JSON 側だけで判定できる。

**`tests.group` は `groups` と添字対応する。** `tests.group[i]` は `groups[i]` を検証するコマンドであり、長さは `groups` と一致させる（`len(tests.group) == len(groups)`、または全体が空配列）。`groups[i]` にだけ検証手段が無い場合は、配列ごと空にするのではなく **`tests.group[i]` を空文字列 `""`** にする。`refactor-rollback` は添字で引いて File Rule の `verify` を決めるため（`../refactor-rollback/SKILL.md` 手順3）、長さが揃わないと「revert した対象を検証しないコマンド」を実行可能な検証として提示することになる。`0 < len(tests.group) < len(groups)` は契約違反であり、下流は不足分を `NOT_AVAILABLE` として扱う。

## ルール

- 既存テスト/lintのみ使用
- 依存不明は単独グループ化
- 公開APIを含む変更は最終グループ
- baseline/group/final に載せるコマンドは実在確認（テストファイル・ランナー設定を Grep/Read で確認）してから出力。確認できないコマンドは Test Set に載せず『検証手段なし』と明記

## 永続メモリ

search: `claq_run claq.mem.cli search "..."`（`. "$HOME/.claq/env.sh"` 前提）— クエリ例 `refactor preflight scope split dependency testset`。返るのは `- [kind] title (key)` の 1 行だけなので、本文が要る key だけ `claq_run claq.mem.cli show <key>` に渡す
record: 再利用可能な学びだけ `claq_mem_learn` で登録する。基準は `../learn/SKILL.md` の「記録する / しない」
