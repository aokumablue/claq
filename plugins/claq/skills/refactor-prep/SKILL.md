---
name: refactor-prep
description: リファクタ着手前に対象分割・依存可視化・実行前テストセットを最小コストで確定する事前準備スキル。
context: fork
user-invocable: false
---

# リファクタ事前準備

`refactor` の実行前に、対象の分割・依存関係・検証テストを固定して手戻りを減らす。

## 手順

1. 対象の確定（優先順）: 渡されたスコープリスト → 引数パス（ディレクトリ = 配下の全ファイル / ファイル = そのファイル）→ `git diff --name-only HEAD`
2. 分割: 同時に変える必要がある塊でグループにする。依存の薄いグループを先に処理する
3. 依存の可視化: import・参照関係を確かめ、グループ間の依存順を明示する。循環と高リスクの境界（公開 API・外部 I/O）は先に記録する
4. テストセットの確定: baseline（全体）・グループ単位・final gate（テスト + lint）

## 出力

テキストのサマリーと、下流（`refactor-rollback` と `refactor` コマンド）が読む JSON 契約を両方返す。

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

JSON 契約（`deps.from` / `deps.to` は `groups` 配列のインデックス）:

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

- `scope_files` / `groups` / `deps` / `tests.baseline` / `tests.group` / `tests.final` のキーは必ず出す（値は空でもよい）
- baseline / group / final のカテゴリ全体で実在するコマンドを確認できなければ、その配列を空にする。空配列が JSON 契約上の「検証手段なし」で、JSON だけを読む `refactor-rollback` はそれで判定する（テキスト側の「検証手段なし」は人間向けの表記）
- `tests.group[i]` は `groups[i]` を検証するコマンドで、`len(tests.group) == len(groups)`（または全体が空配列）にする。`groups[i]` にだけ検証手段が無ければ、配列ごと空にせず `tests.group[i]` を空文字列 `""` にする。`refactor-rollback` は添字で引いて各ファイルの `verify` を決めるため、長さがずれると、リバートした対象を検証しないコマンドを検証として提示することになる。`0 < len(tests.group) < len(groups)` は契約違反で、下流は不足分を `NOT_AVAILABLE` として扱う

## ルール

- 既存のテスト/lint だけを使う
- 依存が不明なファイルは単独のグループにする
- 公開 API を含む変更は最後のグループにする
- Test Set に載せるコマンドは、テストファイルとランナー設定を Grep/Read で確かめてから出す。確かめられないコマンドは載せず「検証手段なし」と書く

## 永続メモリ

- search: `claq_run claq.mem.cli search "..."`（`. "$HOME/.claq/env.sh"` 前提）— クエリ例 `refactor preflight scope split dependency testset`。返るのは `- [kind] title (key)` の 1 行だけなので、本文が要る key だけ `claq_run claq.mem.cli show <key>` に渡す
- record: 再利用可能な学びだけ `claq_mem_learn` で登録する。基準は `../learn/SKILL.md` の「記録する / しない」
