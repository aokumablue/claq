# 計画: loop-dev ループエンジニアリング品質昇格

## Context

loop-engineering（Zenn 記事 + cobusgreyling/loop-engineering、5.9k stars）の知見を bluecore ハーネスに取り込み、loop-dev 開発サイクル（plan→generate→evaluate 最大 2 反復）のループエンジニアリング品質を昇格させる。記事・リポジトリの docs（failure-modes / anti-patterns / loop-design-checklist / primitives）を精読した結果、現行 loop-dev はゴール条件・maker/checker 分離・checkpoint 状態永続化・レート制限ガード等の中核を実装済みで、不足は **Verifier Theater 対策（検証者の一次実行）/ Infinite Fix Loop 対策（circuit breaker + 根本原因診断）/ Flake 分類 / Over-Reach 対策（scope guard）/ Run log（反復履歴）/ Escalation 品質 / Human gate 明文化 / Loop Readiness 評価機構**の 8 点。

Claude Code / Copilot CLI 両対応のため **markdown 指示レベルのみで実装**（Python 変更・新規 hook・JSON 変更ゼロ）。

## grillme 合意事項（確定・変更不可）

1. スコープ = loop-dev 品質昇格に限定。スケジュール駆動ループ（PR Babysitter 等 7 パターン）の新設は非目標
2. circuit breaker 導入 + checkpoint フォーマット拡張（skills/checkpoint/SKILL.md 変更可）
3. worktree 隔離は見送り（Claude 固有機能で Copilot に相当なし、Bash 自作方式も利益薄と判断）
4. verifier 独立性 = reviewer に generate 自己申告を渡さず一次情報から独立検証（モデル分離なし）
5. 自律度はパラメータ化せず human gate として明文化。ユーザー指定パラメータを増やさない
6. 観測用の新規 hook なし（既存 mem observe で足りる）
7. 開発サイクル専用 before/after 評価機構を新設（skill-tune は skills 専用のため転用不可）
8. release コミット不要（ユーザー実施）

## 設計裁定（planner + architect 並列評価のマージ結果）

- **circuit breaker は決定論化して縮退**（architect 案採用）: LLM の意味判断でなく文字列 exact match。反復1 の失敗シグネチャを checkpoint 反復履歴に**正規化済み literal で記録**し、反復2 は文字列一致で比較。①テスト失敗 = pytest **nodeid**（ハードトリガー: 反復2 の generate 自己検証で同一 nodeid が再 red → evaluate をスキップして即エスカレーション）②blocker = `正規化パス~指摘要旨`（行番号除去。ソフトトリガー: エスカレーション材料に使い、単独でハード停止しない）。「反復2 を待たず即停止」の文字通り実装は論理的に不可能（再発は反復2 で初観測）のため、この縮退が正確な定義
- **reviewer 変更は verify_mode 条件分岐で loop-dev 文脈に限定**（重大リスク回避）: reviewer は /review 等でも共用されるため、無条件の「常にテスト再実行 + 拒否デフォルトスタンス」化は全 reviewer 起動を重く・厳しくする。呼び出し元が `verify_mode: reexecute` + 失敗 nodeid を渡したときのみ一次再実行
- **pytest full run の二重化はしない**: full run は generate 自己検証に一元化。reviewer の再実行は「ruff フル + 記録済み失敗 nodeid の再実行 + 変更ファイル関連サブセット」に限定（3023 テストの full 二重実行を回避）
- **scope guard は approved_plan 内の変更ファイル一覧の存在で gating**（重大リスク回避）: approved_plan の意味は呼び出し元でバラバラ（plan.md=計画+ファイル一覧 / test-gen=デシジョンテーブル / refactor=blocker 一覧 / feat-dev・bugfix=なし）。ファイル一覧が識別できる場合のみ活性化 → 5 コマンド無変更で波及ゼロ
- **反復履歴は独立 `## 反復履歴` トップレベル見出し**（重大リスク回避）: session_end.py の `_auto_save_checkpoint` は正規表現 `(?m)^## 変更済みファイル\n.*?(?=\n## |\Z)` で該当セクションのみ置換するため、独立 `## ` 見出しなら非破壊（実コード確認済み）。`###` サブセクションで入れると消失するため禁止
- **評価機構 = 新規 skill `loop-audit`（user-invocable: true）**: skill-tune（skills 用診断）と対称の配置。command 新設は grillme 前段規約等の登録面が増え過剰。metrics 母数は **git log + checkpoint 反復履歴**（全件走査可・決定論）を一次源とし、mem `search-structured`（tool_name=loop-dev。ただし limit 上限ありの近似）は二次源と明記 — CLI に event_type 全件列挙がないため（正確な mem 集計を必須化するなら Python CLI 追加が要るが本計画では見送り）
- **loop-dev 本体の増分は +25〜30 行以内**: シグネチャ定義・反復履歴フォーマットは checkpoint SKILL.md に単一情報源化し loop-dev からは参照のみ。新設節は circuit breaker / Human Gate の 2 つに限定、他はワンライナー差し込み。既存ルール節のレート制限 90%・gate 最終権限は Human Gate 節へ移動して重複相殺

## 実装内容

### 1. `plugins/bluecore/skills/checkpoint/SKILL.md` — 反復履歴 + シグネチャ定義 + State Rot 対策

- ファイルフォーマットに**任意セクション** `## 反復履歴`（append-only、`## 再開コンテキスト` の後 = 最終配置）を追加。1 反復 1 行:
  `- iter{n} | blockers=[{blocker シグネチャ, ...}] | tests={PASS|FAIL:{nodeid, ...}} | lint={PASS|FAIL} | scope={OK|VIOLATION} | result={converged|not-converged|circuit-break|stopped} | rootcause={1 行 or -}`
- シグネチャ定義（circuit breaker の単一情報源）: テスト失敗 = pytest nodeid そのまま / blocker = `正規化相対パス~指摘要旨先頭 N 語`（小文字化・行番号と数値マスク）。**記録時に正規化を確定し、比較は文字列 equality のみ**（再正規化のブレを排除）
- session_end.py の自動保存テンプレートには含まれない「任意セクション」であること、自動保存は `## 変更済みファイル` のみ更新するため反復履歴は非破壊であることを明記
- State Rot 対策を操作節に追記: 更新時に解決済みステップを `[x]` 確定し進行中/残りから掃除、`completed: true` 化時は進行中を空にする。反復履歴は掃除対象外（append-only）

### 2. `plugins/bluecore/agents/reviewer.md` — verify_mode 条件付き一次検証

- 新節: 呼び出し元が **`verify_mode: reexecute`**（+ 失敗 nodeid 一覧）を指定した場合のみ、①`ruff check` フル ②渡された失敗 nodeid の pytest 再実行（RED→GREEN 遷移の独立確認）③変更ファイル関連テストのサブセット、を Bash で一次実行し、**実行出力を根拠に** PASS/FAIL を報告。実装者の自己申告・会話上の報告は検証入力として扱わない
- verify_mode 指定時のデフォルトスタンス「拒否理由を探す」（Confidence 80 未満黙殺の既存基準は維持）。無指定時（/review 等）の挙動は現状不変
- `Blockers: {n}` 集計行契約は不変（loop-dev の機械判定が依存）

### 3. `plugins/bluecore/skills/loop-dev/SKILL.md` — 品質昇格（正味 +25〜30 行、2 コミット分割）

**コミット A（circuit breaker + 根本原因 + 反復履歴連携）:**
- 新節 `## circuit breaker`: 反復1 の checkpoint 反復履歴に記録した失敗シグネチャ（定義は checkpoint SKILL.md 参照）と反復2 の generate 自己検証結果を**文字列一致**で比較。同一 nodeid 再 red → evaluate をスキップし即エスカレーション停止（result=circuit-break、green コミットは行わない）。blocker シグネチャ一致はエスカレーション材料（単独でハード停止しない）
- 反復2 に差し込み: blocker 修正前に**根本原因を 1 行明記**（症状のみのパッチ禁止）、根本原因は反復履歴の rootcause に記録
- checkpoint 連携に差し込み: 各反復の収束判定後に `## 反復履歴` へ 1 行 append + State Rot 掃除

**コミット B（verifier 配線 + scope guard + flake + escalation + Human Gate + record 反復単位化）:**
- evaluate 差し込み: reviewer 起動時に `verify_mode: reexecute` + 反復履歴の失敗 nodeid を渡す。generate の自己申告（「テスト通過した」等の要約）は渡さない — diff とテスト結果は reviewer が一次取得
- evaluate 差し込み（scope guard）: approved_plan に変更ファイル一覧が識別できる場合のみ、編集ファイルが一覧内かを確認。逸脱 = BLOCKER 扱い。一覧なし（feat-dev/bugfix/test-gen/refactor 経由）は不活性
- 収束判定差し込み（flake 分類）: テスト失敗時、同一 nodeid を最大 2 回 rerun し結果が不安定なら flake と分類。**flake をプロダクトコード修正で隠蔽しない**。flake はエスカレーション事項として報告（隔離を報告した上で収束判定から除外可）
- 上限超過時の強化: エスカレーション出力に「残 blocker + 根本原因 + 推奨次アクション + 反復履歴全文」を含める
- 新節 `## Human Gate`: ①計画承認（呼び出し元）②上限超過・circuit break エスカレーション ③レート制限 90% 停止 ④コミット禁止規約 — の 4 ゲートを集約（既存ルール節の該当 2 項を移動して重複削除）。自律度パラメータは設けない
- 永続メモリ record を反復単位・機械可読固定フォーマットに変更: `{"event_type":"loop-dev","content":"Task:{task}. Iter:{n}/2. Result:{converged|circuit-break|stopped}. Blockers:{n}. Flake:{n}. Commits:{hash}"}`（loop-audit の二次データ源）
- 出力箱形に `Circuit-Break: {YES|NO}` 行を追加（既存行は不変）

### 4. `plugins/bluecore/skills/loop-audit/SKILL.md` — 新規（Loop Readiness 評価機構）

- フロントマター: `name: loop-audit` / description「loop-dev 開発サイクルの収束品質を履歴から集計し Loop Readiness スコアと改善提案を出す診断」/ `context: fork` / `user-invocable: true`
- plugin.json 変更不要（skills はディレクトリ自動探索）
- データ源（信頼度階層を明記）: 一次 = `~/.bluecore/session-data/checkpoint-*.md` の反復履歴（Read/Bash grep で全件走査）+ `git log`（反復番号付きコミット）/ 二次 = mem `search-structured`（`tool_name=loop-dev`、limit 上限ありの**近似**と明記。実行は `PYTHONPATH=plugins/bluecore/src python3 -m bluecore.mem.cli ...` — 開発環境と配布ランタイムの両対応を記述）
- 指標: 収束率（converged / 全実行）・平均反復数・circuit break 率・blocker 再発率・flake 検出数・エスカレーション率
- 出力: Loop Readiness スコア（loop-engineering の checklist 10 領域に対する充足評価）+ before/after 比較（期間指定）+ 改善提案
- skill-tune との責務境界を明記（skill-tune = skills のプロンプト品質診断 / loop-audit = 開発サイクルの収束品質診断）

## コミット単位タスク分解

検証コマンド（全サイクル共通）: `set -o pipefail; source ~/.bluecore/.venv/bin/activate && python3 -m pytest -q && ruff check plugins/bluecore/src`

| # | コミット | 依存 |
|---|---|---|
| C1 | checkpoint SKILL.md: 反復履歴セクション + シグネチャ定義 + State Rot 対策 | なし |
| C2 | reviewer.md: verify_mode 条件付き一次検証 + 拒否スタンス | なし（C1 と独立） |
| C3 | loop-dev SKILL.md: circuit breaker + 根本原因診断 + 反復履歴連携 | C1 |
| C4 | loop-dev SKILL.md: verifier 配線 + scope guard + flake + escalation + Human Gate + record 反復単位化 | C1, C2, C3 |
| C5 | loop-audit skill 新規作成 | C1, C4（record フォーマット確定後） |

## 変更しないファイル（隣接するが対象外）

- `plugins/bluecore/commands/{feat-dev,bugfix,refactor,test-gen,plan}.md` — loop-dev 入力契約不変のため波及ゼロ（scope guard は approved_plan 内ファイル一覧の存在で自動 gating）
- `plugins/bluecore/src/bluecore/hooks/session_end.py` — 独立 `## ` 見出しなら正規表現非干渉（確認済み）
- `plugins/bluecore/hooks/hooks.json` / `.claude-plugin/plugin.json` / `src/bluecore/mem/**` / `tests/**` — 新規 hook・Python・JSON 変更なし

## リスクと対応

1. **markdown 指示の circuit breaker 実効性**: シグネチャを checkpoint に literal 記録 → 文字列 equality 比較に決定論化。nodeid のみハードトリガー、blocker 一致はソフト
2. **reviewer 変更の全域波及**: verify_mode 条件分岐で loop-dev 文脈限定。/review 等の既存挙動不変
3. **pytest 再実行コスト**: full run は generate 自己検証に一元化、reviewer は失敗 nodeid + サブセットのみ。flake rerun は上限 2 回
4. **loop-dev 肥大化による規律劣化**: 増分 +25〜30 行以内、定義は checkpoint に単一情報源化
5. **loop-audit の mem 集計限界**: 一次源を checkpoint + git log に置き、mem search は近似補助と明記
6. **Copilot 互換**: 全変更が markdown 指示 + Bash（python3/git/grep）のみでハーネス固有機能に不依存。PYTHONPATH 両対応を loop-audit に明記

## 検証方法

- 各コミット: pytest -q（3023 件 + test_md_references.py の md 参照整合 + validate_skills の非空検証）+ ruff 警告ゼロ。Python 非変更のためカバレッジ 100% は既存維持
- C1 後: session_end.py の更新正規表現が `## 反復履歴` を侵さないことをサンプル checkpoint ファイルで机上確認（正規表現を Python REPL で再現）
- C5 後: dry-run — loop-audit を起動し、既存 checkpoint / git log から指標が出力されることを確認
- before/after 評価（成功条件）: 次回以降の loop-dev 実運用で反復履歴が蓄積された後、loop-audit で収束率・blocker 再発率を測定（本計画のスコープは機構の実装まで）
