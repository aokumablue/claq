# ADR-0004: reviewer agent は write-capable Bash を保持する

**日付**: 2026-08-18  **ステータス**: accepted

## コンテキスト

`security-auditor` エージェントは frontmatter の `tools` から Bash を
外し、Read/Grep/Glob のみで完結する（`tests/test_md_references.py
::test_security_auditor_has_no_bash_access` が技術的に強制）。一方
`reviewer` エージェント（`agents/reviewer.md`）は `tools: Read, Grep,
Glob, Bash` を維持しており、Bash を経由して任意の書き込み（`>`・`sed -i`
等）が理論上可能な状態が残る。

監査は複数ラウンドでこの非対称を指摘している:

- v0.9.32 検証 §6.3: reviewer が write-capable Bash を持つことを指摘。
  代案として「validator runner とレビュー agent を分離し、reviewer には
  Bash を渡さない構成」を提示。
- v0.9.33 検証: §6.3 が再提起された。

`reviewer.md` は `ruff check` の実行と、呼び出し元から渡された `test_cmd`
の再実行（RED→GREEN 遷移の独立検証、`verify_mode: reexecute`）を職務として
明記しており、これらはいずれも Bash を要する。

## 決定

**reviewer エージェントの `tools` から Bash を外さない。** 技術的な
書き込み禁止の強制は行わず、既存の散文制約（read-only の職務境界を明記し、
逸脱時はレビューを継続せず報告して停止する）を維持する。

`ruff check` の全体実行と、渡された `test_cmd` を基底コマンドとした失敗
テストの再実行（RED→GREEN 遷移の独立確認）はレビューの職務そのものであり、
これらを実行するには Bash が要る。security-auditor（10 項目チェック
リストが Read/Grep/Glob のみで完結する）とは職務の性質が異なるため、
同じ強制を適用しない。

## 検討した代替案

### 代替案 1: validator runner とレビュー agent を 2 分割する（§6.3 の代案）

- 長所: レビュー agent 自体からは Bash 権限が技術的に消え、
  security-auditor と同じ強制ができる。
- 短所:
  1. レビュー結果（reviewer の指摘）と検証結果（validator runner の
     PASS/FAIL）を突合する責務が、reviewer からその呼び出し元
     （orchestrator）へ押し戻されるだけで、「Bash を持つ主体が存在する」
     という根本的なリスクは消えない。
  2. agent 分割・呼び出し 1 段増によるコンテキスト消費と収束遅延を招く
     （`loop-dev` の evaluate ステップは既に条件付き並列
     `security-auditor` を持ち、これ以上の agent 増殖は turn cap との
     兼ね合いも悪化させる）。
- 却下理由: コストに見合うリスク低減が無い過剰設計と判断した。書込み権限
  の技術的強制という根本課題は分割しても解決しない。

### 代替案 2: `ruff check`/`test_cmd` の再実行を呼び出し元（loop-dev
orchestrator）に移し、reviewer には結果だけ渡す

- 長所: reviewer から Bash 実行そのものを完全に排除できる。
- 短所: `reviewer.md` の一次検証 step 0 が定める「`test_cmd` の三層検証
  （由来の明示・シグネチャの正規表現制約・引用符付け連結）」は、
  reviewer 自身が実行することで初めて検証結果の信頼性を担保している
  （実装者の自己申告コマンドは受け付けない設計）。呼び出し元が代わりに
  実行して結果だけ渡す構成にすると、「渡された結果が本当にそのコマンドの
  実行結果か」を reviewer が検証できなくなり、自己申告を信頼する経路が
  復活してしまう。
- 却下理由: 検証の独立性という reviewer の核心的な価値を失う。

## 結果

### 肯定的

- reviewer は `ruff check`/`test_cmd` の独立再実行という職務を、Bash 実行
  の主体を分割せずに一貫して担える。

### 否定的

- security-auditor と異なり、reviewer では frontmatter の `tools` だけで
  書き込み不能を技術的に保証できない。

### リスク

- reviewer の散文制約（read-only の職務境界の明記）は技術的強制ではない
  ため、プロンプトインジェクション等でこの制約が無視された場合、
  reviewer が実際に任意の書き込みを行う経路は理論上残る。この残存リスクを
  受容した上で、`agents/reviewer.md` に本 ADR への参照と却下理由を明記し、
  再監査のたびに同じ議論を繰り返さないようにする。
