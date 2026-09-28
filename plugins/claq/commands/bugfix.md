---
name: bugfix
description: バグを再現→原因分析→最小修正→回帰防止→レビューまで一気通貫で進める。
command: /bugfix
---

# バグ修正フロー

## grillme（条件付き）

要件が曖昧で複数の読み方が成立するときだけ、開始直後に grillme スキルで共通理解を固める。依頼が明確なら省いて着手する。起動したら完了まで他の処理に進まず、合意した方針を 1 行で確認する。

## 永続メモリ

- 参照: SessionStart が `<claq-memory>`（`status='active'` の知識）を注入済み。追加で要るときは `. "$HOME/.claq/env.sh"` のあと `claq_run claq.mem.cli search "..."`（クエリ例 `bug fix regression repro root cause verify` / `{対象ファイルパス}` / `{症状キーワード}`）→ 本文が要る key だけ `claq_run claq.mem.cli show <key>`
- 記録: ステップ4 の基準で `claq_mem_learn` を使う

## ステップ1: 要件整理

1. 症状・期待動作・実際の動作を分ける
2. 再現条件・入力・環境差分・影響範囲を確認する
3. 仕様バグ・設計欠陥の疑いがあれば、修正前に切り分ける

## ステップ2: 再現テスト確立

1. 再現テストまたは再現手順を先に作る
2. 既存テストで失敗を確認する
3. 再現できなければ、不足している情報を示して止める

## ステップ3: loop-dev 反復修正

Skill ツールで `loop-dev` skill を起動し、plan→generate→evaluate を最大 2 反復で収束させる。入力:

- `task` = 再現テスト・原因候補を含む修正要件
- `task_type` = `bugfix`
- `converge_extra` = 「再現テスト green + 回帰テスト追加済み」

loop-dev の収束または停止の報告を受け取ったらステップ4へ進む。

## ステップ4: 学びの記録（毎回実行・記録は該当時のみ）

ステップ3 で特定した根本原因を、次に同じ状況へ来たときに回り道せずに済む形で残す。調べて初めて分かった根本原因は、ほぼ確実に記録対象になる。

| 見つけたもの | kind | title に書くこと |
|---|---|---|
| 原因が非自明で、同じ状況なら次も踏む罠 | `pitfall` | 症状ではなく回避条件（「X するときは Y が要る」） |
| 調べないと分からなかったこのリポジトリ固有の事実 | `fact` | 事実そのもの（「開発用 venv はリポジトリ直下 `.venv` のみ。ランタイムは venv を作らない」） |
| 再現手順が毎回同じで、次も同じ手順を踏む | `howto` | 手順の目的（「hook の再現は stdin に JSON を流す」） |
| 明文化されていなかった規約に反していたのが原因 | `convention` | 守るべきルール |

記録しないもの: リポジトリを読めば分かること（README・CLAUDE.md・型定義・docstring）、作業ログ（SessionEnd の `handoff` が残す）、diff の要約、そのセッション限りの事情、一般的なプログラミング知識。

```bash
. "$HOME/.claq/env.sh" || exit 127
claq_mem_learn --kind pitfall --scope repo --domain <domain> \
  --title "<回避条件を 1 行で>" \
  --body "<根本原因と、次に踏まないための具体策>"
```

該当が無ければ 1 件も記録しない（0 件は正しい結果。埋め合わせで書かない）。登録は常に `status=pending` になり、採否は `/instinct` のレビューで決まる。重複確認とシークレットのマスクは `../skills/learn/SKILL.md` に従う。

## 記録テンプレート

未受領の項目を PASS と書かない。各欄の出所:

- Repro / Root cause / Fix: ステップ1-2 の自分の記録
- Tests / Loop: loop-dev の Loop-Dev Result から転記
- Review: loop-dev の `Blockers: {n} remaining` から導く（0 件 → PASS / 1 件以上 → BLOCKED）
- Learned: ステップ4 の結果（記録が無ければ `なし`。欄は省かない）

```
Bug Fix
──────────────────────────────
Scope:      {scope}
Repro:      PASS / FAIL
Root cause: {root_cause}
Fix:        {fix}
Tests:      {tests}
Loop:       {n}/2
Review:     PASS / BLOCKED
Learned:    {記録した key} / なし
──────────────────────────────
```

## ルール

- 再現テストを先に作る / 修正は最小にする / 回帰確認を省かない
- 仕様バグ・設計欠陥・品質改善は `/refactor` / `/plan` / `/review` に切り分ける

## 引数

- 位置 #1: `[バグ説明 or 症状]`（省略可）
- 位置 #2: `[ファイルパス or ディレクトリ]`（省略可）
