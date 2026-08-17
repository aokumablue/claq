---
name: bugfix
description: バグを再現→原因分析→最小修正→回帰防止→レビューまで一気通貫で進める。
command: /bugfix
---

<!-- DRY: grillme 前段（発火条件〜他処理に進まない）は全コマンド共通。終了条件・永続メモリ・引数は固有 -->

# バグ修正フロー

## grillme 起動（条件付き）

要件が曖昧で複数の読み方が成立するときのみ、開始直後に grillme スキルで共通理解を固める。依頼が明確なら省略して着手する。起動した場合は完了まで他処理に進まず、完了時は合意方針を1行サマリで確認する。

## 永続メモリ

- 注入: SessionStart の `mem context` が `<bluecore-memory>` を自動投入（`status='active'` のみ）
- 参照: `bluecore_run bluecore.mem.cli search "..."`（`source .../runtime/bluecore-helpers.sh` 前提。クエリ例 `bug fix regression repro root cause verify` / `{対象ファイルパス}` / `{症状キーワード}`）→ 本文が要る key だけ `bluecore_run bluecore.mem.cli show <key>`
- 記録: 再利用可能な学びだけ `bluecore_mem_learn` で登録する。基準は `../skills/learn/SKILL.md` の「記録する / しない」

## skill 起動メカニズム

`loop-dev` は `user-invocable: false` の skill。本文で「loop-dev skill を起動」と明示することで Skill ツール経由の fork 実行で発火する。

## ステップ1: 要件整理

1. 症状・期待動作・実際の動作を分ける
2. 再現条件・入力・環境差分・影響範囲を確認
3. 仕様バグ・設計欠陥の疑いがあれば修正前に切り分ける

## ステップ2: 再現テスト確立

1. 再現テストまたは再現手順を先に作る
2. 既存テストで失敗を確認
3. 再現できない場合は不足情報を明示して止める

## ステップ3: loop-dev 反復修正

`loop-dev` skill を起動（必須）。plan→generate→evaluate を最大 2 反復で収束させる。

入力:

- `task` = 再現テスト・原因候補を含む修正要件
- `task_type` = `bugfix`
- `converge_extra` = 「再現テスト green + 回帰テスト追加済み」

loop-dev から収束 or 停止報告を受領して記録へ進む。

## ステップ4: 学びの記録（必須実行・記録は該当時のみ）

ステップ2で特定した**根本原因**を、次に同じ状況へ来る自分が回り道せずに済む形で残す。
根本原因が「調べて初めて分かったこと」なら、それはほぼ確実に記録対象。

記録する:

| 見つけたもの | kind | title に書くこと |
|---|---|---|
| 原因が非自明で、同じ状況なら次も踏む罠 | `pitfall` | 症状ではなく**回避条件**（「X するときは Y が要る」） |
| 調べないと分からなかったこのリポジトリ固有の事実 | `fact` | 事実そのもの（「開発用 venv はリポジトリ直下 `.venv` のみ。ランタイムは venv を作らない」） |
| 再現手順が毎回同じで、次も同じ手順を踏む | `howto` | 手順の目的（「hook の再現は stdin に JSON を流す」） |
| 明文化されていなかった規約に反していたのが原因 | `convention` | 守るべきルール |

記録しない:

- **リポジトリを読めば分かること** — README・CLAUDE.md・型定義・docstring に既に書いてあること
- **作業ログ** — 「このバグを直した」「どのファイルを触った」。SessionEnd の `handoff` が自動で残す
- **diff の要約** — コードを読めば分かる修正内容
- **そのセッション限りの事情** — 「今回は別ブランチの変更が混ざっていた」
- **一般的なプログラミング知識** — off-by-one、null チェック漏れなど

```bash
source "${CLAUDE_PLUGIN_ROOT}/runtime/bluecore-helpers.sh"
bluecore_mem_learn --kind pitfall --scope repo --domain <domain> \
  --title "<回避条件を 1 行で>" \
  --body "<根本原因と、次に踏まないための具体策>"
```

該当ゼロなら 1 件も記録しない（0 件は正しい結果。埋め合わせで書かない）。
確信が持てないものは `--status pending` を付け、採否は `/instinct` のレビューに委ねる。
詳細基準は `../skills/learn/SKILL.md` の「記録する / しない」。

## 記録テンプレート

記録対象は出所別に分離する。Tests と Loop（反復数）は loop-dev の Loop-Dev Result からの転記。Review は loop-dev の `Blockers: {n} remaining` から導出する（0 件 → PASS / 1 件以上 → BLOCKED）— Loop-Dev Result に `Review` フィールドは存在しないため転記ではなく導出。Repro/Root cause/Fix はステップ1-2（要件整理・再現テスト確立）での自己記録に基づく（loop-dev の出力契約には存在しない）。未受領項目を PASS と書かない。

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

`Learned` はステップ4の実行結果。記録対象が無かった場合は `なし` と書く（欄ごと省略しない）。

## ルール

- 再現テストを先に作る / 最小修正 / 回帰確認を省略しない
- 仕様バグ・設計欠陥・品質改善は `/refactor` / `/plan` / `/review` に切り分ける

## 引数

- 位置 #1: `[バグ説明 or 症状]`（省略可）
- 位置 #2: `[ファイルパス or ディレクトリ]`（省略可）
