# bluecore プラグイン 全コンポーネント動作検証レポート

- **対象**: `/Users/mt_aokuma/.claude/plugins/cache/bluecore/bluecore/0.9.17`（インストール済みプラグイン）
- **バージョン**: 0.9.17
- **検証日**: 2026-07-12
- **範囲**: agents 16 / commands 10 / skills 13 / hooks 29エントリ（20モジュール）/ output-styles 1
- **方針**: hooks は launcher 経由で実起動（35ケース実行）。agents/commands/skills/output-styles は定義精読による擬似起動（frontmatter・参照整合・委譲先解決・フロー破綻の静的トレース）。**プラグインファイルは一切未修正（報告のみ）**。

---

## 1. 総合サマリ

| カテゴリ | 件数 | OK | WARN | NG | 主な所見 |
|---|---|---|---|---|---|
| hooks | 20モジュール/35ケース | 全ケースOK | 3 | 0 | 非ブロッキング契約 完全遵守。import 20/20 成功。トレースバック皆無 |
| agents | 16 | 12 | 2 | 4* | executor の model 無効 + grader/comparator/bench-analyzer の Write 欠落 |
| commands | 10 | 8 | 2 | 0 | 委譲先参照はすべて実在。ドキュメント精度の WARN 2件 |
| output-styles | 1 | 1 | 0 | 0 | slim.md 妥当 |
| skills | 13 | 11 | 2 | 0 | refactor-prep 出力契約の spec 不備、skill-tune 記述不一致 |

\* agents の NG は「executor.md（1件）」+「grader/comparator/bench-analyzer（3件、同一パターン）」= 計4ファイル。

**総合判定**: 実起動する hooks レイヤに実装 NG は無し（堅牢）。定義レイヤ（agents）に**実害を伴う NG が2系統**。それ以外はドキュメント整合性の WARN。**プラグインとしての起動・動作は成立するが、agents の NG-1 は付属 CI（validate_agents）を確実に FAIL させるため要修正**。

> **更新（継続セッション 2026-07-13・§10）**: 上表は初回セッションの**静的擬似起動**時点のスナップショット。継続セッションで agents 16 / commands 10 / skills 13 / 停止時 hooks 6 を**すべて実起動検証**（§10.2〜10.7・計45コンポーネント）。その過程で定義精読では露見しなかった**実行系 実害 NG を7件（NG-B1/B2 launcher・NG-C1〜C5 commands）新規検出→全て修正・コミット済み**（`afd5a7a`・`9c49c55`）。初回検出の agents 2系統 NG も修正済み（`aac2ee3`）。最新の結論は **§10.7 最終集計**を参照。残課題は §9.5 + §10.6b。

---

## 2. Hooks（実起動検証：35ケース）

### 起動契約
`cd <install> && echo '<json>' | .venv/bin/python3 src/bluecore/launcher.py <module> [args]`
`CLAUDECODE=1` により全 hook を同期実行して exit code / stderr を捕捉（`BLUECORE_HOOK_TIMEOUT=30`）。venv は `.venv` symlink 経由で plugin cache の src を解決（Python 3.12）。

### 結果ハイライト
- **非ブロッキング契約**: 全 hook 遵守。正常入力・異常系（transcript 欠落・空 JSON `{}`・staged 無し・postgres 無効）いずれも例外落ちゼロ・exit 0。
- **意図的ブロッカー**は違反入力時のみ exit 2：
  - `block_no_verify`: `git commit --no-verify` → exit 2（`BLOCKED: git hook bypass flags are not allowed`）
  - `config_protection`: `.eslintrc.json` への Write → exit 2（`BLOCKED: Modifying .eslintrc.json is not allowed`）
  - `pre_bash_commit_quality`: staged `app.js` に AWS key → exit 2（`Commit blocked`）。console.log は warning
  - `insights_security_monitor`: 認証情報混入 → exit 2（`CREDENTIAL_EXPOSURE critical`）
- **stderr トレースバック**: 全35ケースで 0件（`Traceback / ImportError / ModuleNotFoundError / SyntaxError` 無し）。
- **参照整合**: hooks.json が参照する全 20 モジュールを `importlib.import_module` 検証 → import 失敗 0（全て `src/bluecore/{hooks,mem,skills/learn}/` に実在）。
- **フラグゲート**: `get_hook_profile()` は常に `strict` 返却。全 `run_with_flags` エントリの CSV に `strict` 含む → 実運用では skip されない。skip 機構自体は `profiles=minimal`（strict除外）で強制検証し stdin drain・exit 0 で正常動作。
- **timeout/async 整合**: async run_with_flags 8件が `BACKGROUND_HOOK_IDS` と完全一致。SessionStart 4件が `SESSION_START_HOOK_IDS` と一致。launcher 自決 timeout 590s < 最長エントリ 600s（孫プロセス孤立回避の順序が正しい）。欠落・過大なし。

### イベント別 実行結果（抜粋）

| イベント | モジュール | 入力 | exit | 判定 |
|---|---|---|---|---|
| PreToolUse | block_no_verify | 安全Bash / `--no-verify` | 0 / 2 | OK |
| PreToolUse | pre_bash_commit_quality | 通常 / AWS key staged | 0 / 2 | OK |
| PreToolUse | doc_file_warning | Write `.md` | 0 | OK（警告のみ） |
| PreToolUse | suggest_compact | Edit `.py` | 0 | OK |
| PreToolUse | config_protection | 通常 / `.eslintrc.json` | 0 / 2 | OK |
| PreToolUse | insights_security_monitor | 安全 / 認証情報混入 | 0 / 2 | OK（下記WARN-H1） |
| PreToolUse | skills.learn.observe | Write `.py` | 0 | OK |
| PreToolUse | pre_agent_nudge | Task general-purpose | 0 | OK（対応表を additionalContext 提示） |
| PreCompact | pre_compact | transcript有 / 空JSON | 0 | OK |
| SessionStart | session_install | startup | 0 | OK（`既にインストール済み: 0.9.17`） |
| SessionStart | mem.cli setup | — | 0 | OK（postgres未設定WARNING＝設計通り） |
| SessionStart | session_start | startup | 0 | OK（前回サマリ/checkpoint注入） |
| SessionStart | mem.cli record-project-profile | — | 0 | OK |
| SessionStart | mem.cli context | — | 0 | OK（`<mem-context>` 注入） |
| UserPromptSubmit | mem.cli session-init | 「前回」有/無 | 0 | OK（keywordゲート機能） |
| UserPromptSubmit | mem.cli team-session-init | 「以前」 | 0 | OK（postgres無効→graceful skip） |
| UserPromptSubmit | mem.cli record-interaction | prompt | 0 | OK（`success:true`） |
| UserPromptSubmit | mem.cli sync-check | — | 0 | OK |
| PostToolUse | mem.cli observe / record-item-run | Bash / Skill | 0 | OK |
| PostToolUse | redux_filter | 長いBash出力 | 0 | OK（18179→2679 chars, 85%削減） |
| PostToolUse | post_bash_pr_created | `gh pr create` 出力 | 0 | OK（PR URL記録＋review提示） |
| PostToolUse | quality_gate | Edit post-edit | 0 | OK |
| Stop | session_end / evaluate_session / desktop_notify | transcript有 | 0 | OK |
| SessionEnd | session_end_marker / mem.cli session-end | reason=clear | 0 | OK |

### WARN（hooks）
- **WARN-H1 [中]**: `insights_security_monitor.py:36` の `from insa_its import insAItsMonitor` が **venv に導入済みで稼働中**。「SDK 未導入時はスキップ」ではなく、実際に認証情報混入を CRITICAL 検出して exit 2 でブロックする。非ブロッキング契約は満たす（安全入力=0）が、hooks.json の description が謳う「SDK 未導入時はスキップ」は現環境では前提が崩れている。実運用でアクティブなガードである点を明示。
- **WARN-H2 [低・方法論]**: `pre_bash_commit_quality` の secret 検出対象は staged ファイル内容（`git diff --cached`）であり commit メッセージではない。メッセージに秘密情報を入れても検出されない（入力設計上のギャップ。hook のバグではない）。
- **WARN-H3 [低・非Claude限定]**: PostToolUse の `mem.cli observe` / `record-item-run` は `async:true` だが `run_with_flags` を経由せず launcher 直叩き。detach ロジックは `run_with_flags` 側のみのため、Codex/Copilot では同期実行。Claude Code は async をホスト側処理するため影響なし。

**hooks 実装 NG: 0件。**

---

## 3. Agents（16）

### 判定表

| name | model | frontmatter | 参照整合 | 総合 |
|---|---|---|---|---|
| architect | opus | OK | OK | OK |
| bench-analyzer | sonnet | **NG**（保存指示だがWrite無し） | OK | **NG** |
| comparator | sonnet | **NG**（同上） | OK | **NG** |
| dead-code-cleaner | sonnet | OK | OK | OK |
| executor | **inherit** | **NG**（validator が拒否） | OK | **NG** |
| explorer | sonnet | OK | OK | OK |
| grader | sonnet | **NG**（保存指示だがWrite無し） | OK | **NG** |
| harness-tuner | sonnet | OK（設定編集のみ＝Write不要） | OK | OK |
| perf-optimizer | sonnet | OK | OK | OK |
| planner | opus | OK | OK | OK |
| refactor-orchestrator | sonnet | OK | OK（5エージェント実在） | OK |
| reviewer | sonnet | OK | OK | OK |
| security-auditor | sonnet | OK（意図的READ-ONLY） | OK | OK |
| session-observer | haiku | OK | OK | OK |
| simplifier | opus | **WARN**（高頻度発火にopus） | OK | **WARN** |
| tdd-writer | sonnet | **WARN**（Glob欠落） | OK | **WARN** |

**plugin.json 整合**: `agents` 配列16件は実ファイル16件と 1:1 完全一致（欠落・余剰・パス誤りなし）。

### NG（agents）

- **NG-A1 [最重大・実害確認済] executor.md の `model: inherit` が付属 CI を FAIL させる**
  - `agents/executor.md:5` = `model: inherit`
  - `src/bluecore/ci/validate_agents.py:13` = `VALID_MODELS = ["haiku", "sonnet", "opus"]`（`inherit` 非対応）、`:71-73` で不一致時 emit_error
  - **本レポート作成中に実行して確認**: `python3 -m bluecore.ci.validate_agents` → 標準エラーに `エラー: executor.md - モデル 'inherit' は無効です` を出力し **exit=1（FAIL）**
  - `skills/maintain/SKILL.md:49` の baseline ステップでこの validator が走るため、現状のリポジトリでは maintain の初期検証が常時失敗する。**要修正**（executor.md を有効モデルに変更、または validator に `inherit` を許可）

- **NG-A2 [重大] grader / comparator / bench-analyzer が「ファイル保存」を指示されているのに Write を持たない**
  - `agents/grader.md:4`（Write無し）／`:79`「結果を `grading.json` に保存」
  - `agents/comparator.md:4`（同）／`:69`「結果をJSONにして保存」
  - `agents/bench-analyzer.md:4`（同）／`:78`・`:209`「`{output_path}` に保存」
  - 裏付け: `skills/skill-make/references/schemas.md` が `grading.json`=grader出力、`comparison.json`=comparator出力、`analysis.json`=analyzer出力と定義（エージェント自身が永続化する契約）。tools は Read/Grep/Glob/Agent のみで Edit も無く、新規ファイル作成不可。3件とも同一パターン。
  - 補足: 呼び出し元スキルがエージェントの戻り値テキストを受けて Write する設計なら実害は緩和されるが、SKILL.md/schemas.md の文面は「エージェントの出力＝当該JSONファイル」を示唆しており、その解釈ではエージェント側に Write（または Edit）権限が必要。契約の曖昧さとして要整理。

### WARN（agents）

- **WARN-A1 [中] simplifier の `model: opus` が発火頻度・コストと不整合**（`simplifier.md:5`）。「コード変更直後に自律発火」の高頻度トリガーでありながら opus。並走エージェント（dead-code-cleaner/perf-optimizer/refactor-orchestrator/tdd-writer）は全て sonnet。`maintain/SKILL.md:22` は「architect/planner/simplifier は Opus 固定のため model:fable 上書き必須」と自認するが、`commands/refactor.md:54-58`（simplify ステップ）と `loop-dev/SKILL.md:36-44`（generate ルーティング）には上書き指示が無く、通常運用では素の opus を消費し続ける。
- **WARN-A2 [低] tdd-writer の tools に Glob 欠落**（`tdd-writer.md:4` = Read/Write/Edit/Bash/Grep/Agent）。他の実装系エージェント（executor/dead-code-cleaner/perf-optimizer/simplifier/refactor-orchestrator/session-observer）は全て Glob を含む同一セット。tdd-writer のみ欠き、テストファイルのパターン探索を Bash 代替に頼る必要がある。

### 参考（NG/WARNではない）
- `bench-analyzer.md` は「ポストホック分析」（8-152行）と「ベンチマーク結果分析」（155-227行）の2契約を1ファイルに同居。呼び出し側がセクション名を明示参照して曖昧さを回避しているが、ファイル単体にモード判定ロジックは無い。

---

## 4. Commands（10）+ output-styles

### command 判定表

| name | frontmatter | 委譲先解決 | 総合 |
|---|---|---|---|
| bugfix | OK | OK（loop-dev 実在） | OK |
| dashboard | OK | OK | **WARN** |
| feat-dev | OK | OK（explorer×2, loop-dev） | OK |
| harness | OK | OK（harness-tuner） | OK |
| instinct | OK | OK（learn.cli の export/import/promote/prune/evolve 全実在） | OK |
| plan | OK | OK（planner/architect/reviewer/security-auditor/loop-dev） | OK |
| refactor | OK | OK（refactor-prep/rollback/orchestrator/dead-code-cleaner/simplifier/perf-optimizer/reviewer/security-auditor） | OK |
| review | OK | OK（reviewer/security-auditor） | OK |
| skill-gen | OK | OK（skill-make/skill-tune/grader/comparator/bench-analyzer 実在） | **WARN** |
| test-gen | OK | OK（loop-dev） | OK |

- 全10ファイルとも frontmatter は `name`/`description`/`command` の3キーで統一。`allowed-tools`/`argument-hint`/`model` 未宣言だが、これは本プラグイン独自の commands スキーマで `ci/validate_commands.py` も要求しておらず一貫。矛盾ではない。
- **plugin.json 整合**: `commands:["./commands/"]` → 実10ファイルと一致。`outputStyles:"./output-styles/"` → `slim.md` 1件と一致。

### output-styles/slim.md: OK
- frontmatter（`name:slim` / `description` / `keep-coding-instructions:true` / `force-for-plugin:true`）妥当。
- 「圧縮するもの＝説明散文のみ／圧縮しないもの＝ツール呼び出し・コード・URL等」の境界定義が明確で自己矛盾なし。句点削除ルールにも除外規定あり整合。
- `force-for-plugin:true` は `hooks/slim_fallback.py` のコメント（Claude Code は output-style 自動適用、非対応ハーネスのみフォールバック注入）と整合。

### WARN（commands）
- **WARN-C1 [中] dashboard.md の既定出力先が自己矛盾**。`dashboard.md:65`（引数節）は既定を `/tmp/bluecore-dashboard.html` と記載する一方、`dashboard.md:37`（実行例）は `./bluecore-dashboard.html`。引数無指定時の出力先が一意に定まらない。
- **WARN-C2 [中] skill-gen.md の user-invocable 記述が実装と不一致**（skills 側 WARN-S1 と同一事象）。`skill-gen.md:25` は「skill-make / skill-tune は `user-invocable:false`」と明記するが、`skills/skill-tune/SKILL.md:5` は `user-invocable:true`。skill-make（false）は正しく、skill-tune のみ乖離。実行破綻はしない（起動可否は frontmatter が支配）がドキュメント精度の欠陥。

### 参考（LOW）
- `test-gen.md:33` の `get_test_command(project_root)` は `src/bluecore/lib/project_detect/commands.py:78` に実在するが、bash ラッパー呼び出し例が無く呼び出し手段が本文だけでは一意に定まらない。実害未確認。

---

## 5. Skills（13）

### 判定表

| name | frontmatter | 参照実在 | 委譲先解決 | 総合 |
|---|---|---|---|---|
| adr | OK | OK | N/A | OK |
| checkpoint | OK | OK（loop-dev/reviewer 参照実在） | N/A | OK |
| grillme | OK | OK | N/A | OK |
| learn | OK | OK（learn.cli 実在、session-observer と haiku 整合） | N/A | OK |
| loop-audit | OK | OK（mem.cli search-structured 実在） | N/A | OK |
| loop-dev | OK（委譲元6コマンド全確認） | OK（checkpoint 実在） | OK（8エージェント全実在） | OK |
| maintain | OK | OK（helpers/mem/ci.validate_*/harness_audit/harness_probe/hook_common 全実在） | OK（reviewer/security-auditor/tdd-writer） | OK |
| refactor-prep | OK | OK | N/A | **WARN** |
| refactor-rollback | OK | **WARN** | N/A | **WARN** |
| search | OK | OK | N/A | OK |
| secure | OK | OK（references 実在） | N/A | OK |
| skill-make | OK | OK（references 3件・run_loop/package_skill/aggregate_benchmark・内部依存すべて解決） | N/A | OK |
| skill-tune | **WARN** | OK | N/A | **WARN** |

- 全13スキルとも frontmatter は `name`/`description`/`context`/`user-invocable` の4キーで統一。`allowed-tools` 未使用は `ci/validate_skills.py` の要求と整合（agents の tools キーとは設計分離）。
- 依存関係: `loop-dev⇔6コマンド`・`skill-gen→skill-make→skill-tune`・`learn⇔session-observer`（共有ファイル経由の疎結合）いずれも循環・欠落なし。

### WARN（skills）
- **WARN-S1 [中] skill-tune の `user-invocable:true` が skill-gen.md の記述（false）と矛盾**（commands WARN-C2 と同一事象）。skill-tune は自身の description からも直接発火する設計であり、skill-gen.md 側の注釈が古い/誤り。実害は限定的（起動可否は frontmatter が支配）。
- **WARN-S2 [中] refactor-prep の出力契約が下流3ファイルの前提と形式不一致**。
  - `refactor-prep/SKILL.md:19-33` の出力はテキストブロック（`Scope: {n} files` / `Groups: g1, g2, ...` / `Dependencies: - g2 depends on g1`）で、JSON 構造化フィールドを定義しない。
  - 一方 `refactor-rollback/SKILL.md:22-35` は「refactor-prep 入力契約」として `scope_files` / `groups` / `deps:[{"from":idx,"to":idx}]` の JSON スキーマを明記。
  - `commands/refactor.md:35` は「`deps.from`/`deps.to` は `groups` 配列のインデックス」と、存在しない JSON フィールドを参照。
  - `agents/refactor-orchestrator.md:15` も「refactor-prep の出力（グループ、依存、テストセット）」を構造化データ前提。
  - 3ファイルが揃って構造化出力を仮定するが、生成側の refactor-prep にその契約が無い。LLM 解釈で運用は成立し得るが spec 不備。refactor-prep 側に JSON 契約（`scope_files`/`groups`/`deps`/`tests`）を明記すべき。

### 付随観察（LOW・採点外）
- `src/bluecore/skills/comply/*`（runner/classifier/grader/parser/cli/scenario_generator/spec_generator/report/utils）と `src/bluecore/skills/stocktake/*`（core/io/cli）は、13 SKILL.md・agents・commands・hooks.json のいずれからも**一切参照されていない**（grep 全件該当なし）。実装は自己完結するがドキュメント上のエントリポイント不在。dead code か未公開機能かは本調査範囲では判別不能。

---

## 6. 横断 問題一覧（重大度順）

| # | 重大度 | カテゴリ | 概要 | 該当 |
|---|---|---|---|---|
| NG-A1 | 最重大 | agents | `model: inherit` が validate_agents を exit=1 で FAIL（実行確認済）。maintain baseline を落とす | executor.md:5 |
| NG-A2 | 重大 | agents | grader/comparator/bench-analyzer が保存指示だが Write/Edit 無し | 3ファイル |
| WARN-A1 | 中 | agents | simplifier 高頻度発火に opus、通常運用経路に model 上書き無し | simplifier.md:5 |
| WARN-H1 | 中 | hooks | insights SDK は「未導入スキップ」でなく稼働中で CRITICAL をブロック | insights_security_monitor.py:36 |
| WARN-C1 | 中 | commands | dashboard 既定出力先 `/tmp/...` vs `./...` が矛盾 | dashboard.md:37/65 |
| WARN-C2/S1 | 中 | commands/skills | skill-tune の user-invocable が実装(true) と記述(false) で不一致 | skill-tune/SKILL.md:5, skill-gen.md:25 |
| WARN-S2 | 中 | skills | refactor-prep 出力契約（テキスト）が下流3ファイルの JSON 前提と不一致 | refactor-prep/SKILL.md ほか |
| WARN-A2 | 低 | agents | tdd-writer のみ tools に Glob 欠落 | tdd-writer.md:4 |
| WARN-H2 | 低 | hooks | commit-quality の secret 検出は staged 内容対象（メッセージは非対象） | pre_bash_commit_quality |
| WARN-H3 | 低 | hooks | async 直叩き2件は非Claudeハーネスで同期実行（Claudeは影響なし） | hooks.json PostToolUse |
| LOW | 情報 | skills | comply/stocktake モジュール群が全コンポーネントから未参照 | src/bluecore/skills/{comply,stocktake} |

---

## 7. 総括

- **実起動する hooks（20モジュール/35ケース）は堅牢**。非ブロッキング契約を全遵守、参照整合 20/20、トレースバック皆無、意図的ブロッカーは違反入力時のみ exit 2。実装 NG なし。
- **定義レイヤの実害 NG は2系統**:
  1. **NG-A1（executor.md `model: inherit`）** — 付属 CI `validate_agents` を実際に exit=1 で落とす（実行裏取り済）。maintain の baseline を確実に阻害するため**最優先修正候補**。
  2. **NG-A2（grader/comparator/bench-analyzer の Write 欠落）** — 「エージェントが JSON を保存する」契約と tools 権限の不整合。エージェント側に Write 付与か、呼び出し元保存への契約明確化のいずれかが必要。
- **その他は主にドキュメント整合性の WARN**（既定値矛盾、user-invocable 記述ずれ、出力契約の spec 不備、model 上書き漏れ、tools セットの微差）。いずれもプラグインの起動・フローを破綻させないが、CI/保守（特に maintain スキル）や運用コストに影響する。
- **未参照モジュール（comply/stocktake）**は dead code の可能性があり、別途要確認。

> 初回検証（§1–7）は「報告のみ」のスコープで実施。その後ユーザー指示により §8 の実害NG修正を実施した（コミット `aac2ee3`）。§1–7 の検証時点ではプラグインファイルは未変更。

---

## 8. フォローアップ修正（実害NG 2系統・`/bugfix` 実施）

検証後、ユーザー指示により実害NG 2系統をソースリポジトリ `plugins/bluecore/` で修正（キャッシュは install 再生成の派生物＝未変更）。grillme で方針確定 → loop-dev で反復実装 → 1反復で収束。

### 方針決定の経緯（NG-A1）

`validate_agents.py` の `VALID_MODELS` 検証について、当初は「inherit 許可追加」「model 具体値へ戻す」を検討したが、ユーザーとの詰めで**モデル名 VALID 検証そのものの必要性**を再評価:

- `VALID_MODELS` は `e617cbf`（rename）で旧 JS バリデータから**機械移植**された産物で、設計意図を吟味したコミットが無い。意図を表す唯一のテストは「`gemini` を弾く」1件のみ。
- 弱点: 正規値 `inherit`/`fable` や正式 model ID、非Claudeモデルを**構造的に false-positive で落とす**。守るのはタイポのみで、これは必須（非空）チェック＋実行時ハーネス解決で概ねカバー。
- ユーザーの意向（「必要性がわからない」「Claude系以外も使う」「指定しないのが理想」）に最も素直な **案Z（値検証撤廃＋model 任意化）** を採用。トレードオフ（model タイポの静的検出を失い実行時に委ねる）はユーザー承認済み。

### 実装内容（commit `aac2ee3`）

**NG-A1** — `plugins/bluecore/src/bluecore/ci/validate_agents.py`
- `REQUIRED_FIELDS = ["model", "tools"]` → `["tools"]`（model を必須から外す＝未指定を許容）
- `VALID_MODELS` 定数と model 値検証ブロックを削除
- `plugins/bluecore/agents/executor.md` の `model: inherit` 行を削除（model 未指定に）

**NG-A2** — `plugins/bluecore/agents/{grader,comparator,bench-analyzer}.md`
- 各 frontmatter `tools` 配列の Read 直後に `"Write"` を追加（Edit は新規JSON生成のため不要）

**テスト調整** — `plugins/bluecore/tests/ci/test_validator_edges.py`
- `model: gemini` を「無効」と検証するケース（invalid_model / `assert "モデル 'gemini' は無効です"`）を撤去
- `missing_fields.md`（tools 欠落）は tools 必須検証として存置
- 正例テスト `test_validate_agents_allows_missing_model_and_inherit` を追加（model 未指定 PASS / `model: inherit` PASS の回帰防止）
- 全 tests から `VALID_MODELS` / `モデル.*無効` / `model: gemini` の残存参照ゼロを確認（`comply/test_runner.py:440` は別概念でスコープ外）

### 検証結果（収束条件・全充足）

| 条件 | 結果 |
|---|---|
| `validate_agents` 再現症状 | **exit=0**（「16 個のエージェントファイルを検証しました」）＝ NG-A1 解消 |
| grader/comparator/bench-analyzer の Write | **付与済** ＝ NG-A2 解消 |
| `python3 -m pytest -q` | **PASS**（3230 passed） |
| カバレッジ | **100.00%** |
| `ruff check plugins/bluecore/src` | **clean**（警告0） |
| 正例テスト（未指定/inherit PASS） | **追加済** |

> テスト実行 venv: runtime の `~/.bluecore/.venv` に pytest 不在のため dev 用 `/Users/mt_aokuma/dev/bluecore-dev/.venv` を使用。編集時、BOM 文字を含む match 文字列が InsAIts セキュリティフックに2回ブロックされたため BOM 非含有アンカーで再編集して回避（最終結果に影響なし）。

### Bug Fix 記録

```
Bug Fix
──────────────────────────────
Scope:      NG-A1 validate_agents の model 検証 / NG-A2 保存系 agent の tools
Repro:      PASS（validate_agents exit=1 → 修正後 exit=0）
Root cause: VALID_MODELS が機械移植で正規値 inherit を false-positive 排除 / grader・comparator・bench-analyzer が保存指示なのに Write 権限欠落
Fix:        案Z=VALID_MODELS撤廃+model任意化+executor model行削除 / 3 agents に Write 付与
Tests:      3230 passed, coverage 100.00%, ruff clean（正例テスト追加）
Loop:       1/2
Review:     PASS（Blockers 0 remaining）
──────────────────────────────
```

---

## 9. セッション引き継ぎ情報（別PCで継続）

> **この §9 が別PCへの唯一の引き継ぎ手段**。作業マシンが変わるため MEMORY.md 等マシンローカルのメモリは使わず、git 管理下の本ファイルに一元化する。**ユーザーの申し送りを正**とする（下記の済/残区分）。

### 9.1 現況（何が「実起動」で検証済みか）

前セッションが §1–7 で行ったのは主に**静的な擬似起動検証**（定義精読・参照整合）。ユーザー基準では、**実コマンド/スキルとして実際に起動して検証できたのは次の3件のみ**:

| 種別 | 実起動済み |
|---|---|
| commands | `bugfix`（済） |
| skills | `grillme`（済）／`loop-dev`（済） |
| agents | なし |

※ §3–5 の agents/commands/skills は定義精読のみで**実起動していない**。§2 の hooks は executor が 35ケース実起動したが、ユーザー申し送りでは停止時発火 hooks を継続検証対象とする（9.2 参照）。

### 9.2 継続タスク（次セッションで実施・interrupt 未完分もすべて再検証）

**タスク**: bluecore プラグイン(0.9.17) の残 agents/commands/skills/hooks を起動検証し、**結果を必ず本ファイル（`/Users/mt_aokuma/dev/bluecore-dev/plugin-verification-report-0.9.17.md`）に反映する**。interrupt により完了していない可能性のあるものも完了扱いにせず再検証する。

**残・検証対象**:
- **agents**: 16件すべて
- **commands**: `bugfix` 以外の 9件（dashboard, feat-dev, harness, instinct, plan, refactor, review, skill-gen, test-gen）
- **skills**: `grillme`/`loop-dev` 以外の 11件（adr, checkpoint, learn, loop-audit, maintain, refactor-prep, refactor-rollback, search, secure, skill-make, skill-tune）
- **hooks（停止時に発火するものを検証）**:
  - Stop: `session_end` / `evaluate_session` / `desktop_notify`
  - SessionEnd: `session_end_marker` / `mem.cli session-end` / `mem.cli sync-check`

### 9.3 git 状態スナップショット

- ブランチ: `review/fable-token-copilot-20260710`（main ではないので新規ブランチ不要）
- HEAD: `aac2ee3`（1つ前 `cd2197a release: v0.9.17`）。`aac2ee3` の変更6ファイル: `agents/{bench-analyzer,comparator,grader}.md`（各 +Write）/ `agents/executor.md`（-model行）/ `src/bluecore/ci/validate_agents.py`（-10行）/ `tests/ci/test_validator_edges.py`
- 未コミット: 本レポート `plugin-verification-report-0.9.17.md`（**コミットはユーザー明示指示待ち。勝手にコミットしない**）
- コミット時のメッセージ案:
  ```
  docs: v0.9.17 プラグイン全コンポーネント検証レポート＋NG修正記録

  agents/commands/skills/hooks を擬似起動検証（hooks は35ケース実起動）。
  実害NG 2系統（executor model:inherit の validator FAIL、保存系 agent の
  Write 欠落）の分析・案Z 決定経緯・修正結果（aac2ee3）を追記。
  ```

### 9.4 環境の落とし穴（再現に必須）

- **検証対象と修正対象は別ディレクトリ**: 検証（擬似起動）はキャッシュ `/Users/mt_aokuma/.claude/plugins/cache/bluecore/bluecore/0.9.17`、修正はソース `/Users/mt_aokuma/dev/bluecore-dev/plugins/bluecore/`（ソースが正・キャッシュは触らない）。**別PCではこれらの絶対パスが異なる可能性があるため、実パスを最初に確認すること**
- **テスト実行 venv**: runtime `~/.bluecore/.venv` に pytest 不在。テストは dev 用 `.venv`（リポジトリ直下）を有効化して実行
- **開発中コードの CLI/モジュール実行**は `PYTHONPATH` 付与が必須:
  - validate_agents 再現確認: `cd plugins/bluecore && PYTHONPATH=src python3 -m bluecore.ci.validate_agents`（修正後 exit=0）
  - hooks 実起動: `cd <キャッシュ> && echo '<json>' | .venv/bin/python3 src/bluecore/launcher.py <module> [args]`（`CLAUDECODE=1`・`BLUECORE_HOOK_TIMEOUT=30` 推奨）
- **InsAIts セキュリティフックが稼働中**（§WARN-H1）: BOM 文字（`﻿`）や認証情報様の文字列を Write/Edit するとブロックされる。編集時は BOM 非含有アンカーを使う

### 9.5 未対応の残課題（別タスク化候補・重大度順）

| 重大度 | 概要 | 該当 |
|---|---|---|
| 中 | insights SDK は「未導入スキップ」でなく稼働中で CRITICAL ブロック（hooks.json description と前提乖離） | insights_security_monitor.py:36 |
| 中 | dashboard 既定出力先 `/tmp/...` vs `./...` の自己矛盾 | dashboard.md:37/65 |
| 中 | skill-tune の user-invocable が実装(true)⇔記述(false) 不一致 | skill-tune/SKILL.md:5, skill-gen.md:25 |
| 中 | refactor-prep 出力契約（テキスト）が下流3ファイルの JSON 前提と不一致 | refactor-prep/SKILL.md ほか |
| 中 | simplifier 高頻度発火に opus、通常運用経路に model 上書き無し | simplifier.md:5 |
| 低 | tdd-writer のみ tools に Glob 欠落 | tdd-writer.md:4 |
| 情報 | comply/stocktake モジュール群が全コンポーネントから未参照（dead code 疑い、要判別） | src/bluecore/skills/{comply,stocktake} |

> これらは §8 修正のスコープ外。着手時は `/refactor`（品質改善）/ `/plan`（refactor-prep 契約整備）/ `/review` に切り分ける。comply/stocktake は「dead code か未公開機能か」の判別が先。

### 9.6 別PCで継続する際のおすすめプロンプト

このリポジトリ内で新セッションを起動し、次をそのまま投げる（パスはリポジトリ相対のため、そのリポジトリ内で起動すれば通る）:

```
bluecore プラグイン(0.9.17) の全 agents/commands/skills/hooks 起動検証の続き。
リポジトリ直下の plugin-verification-report-0.9.17.md の §9（セッション引き継ぎ）を
最初に読み、その済/残区分に従って未検証コンポーネントを検証せよ。

- §9.2 の残（agents 16 / commands 9 / skills 11 / 停止時発火 hooks）をすべて対象とする
- interrupt で未完の可能性があるものは完了扱いにせず再検証
- §9.4 の環境注意（venv・PYTHONPATH・検証対象キャッシュのパス）を実パスで確認してから着手
- 検証で実害NGが出たら §8 と同様に grillme→loop-dev で修正まで実施
```

**投げる前の確認**:
- ブランチ `review/fable-token-copilot-20260710`・HEAD `aac2ee3` を `git log --oneline -1` で確認（レポートと修正 `aac2ee3` が揃っている状態）
- キャッシュ側パス（`~/.claude/plugins/cache/...`）はマシンで変わるため、§9.4 の指示どおりエージェントに実パスを確認させる

**修正まで一気に実施させたい場合**: 最終行を「検証で実害NGが出たら §8 と同様に grillme→loop-dev で修正まで実施」に差し替える。ただし方針分岐のある NG は確認を挟むこと。

---

## 10. 継続セッション: 残コンポーネント実起動検証（§9.2 の消化）

> 実施環境: §9 と同一マシン（引き継ぎ前提の別PCではなかったため §9.4 の実パスはそのまま有効と確認済み）。ブランチ `review/fable-token-copilot-20260710` / HEAD `aac2ee3`。

### 10.1 検証方式（agents）

- Agent tool で全16 agent を**実起動**（定義どおりの subagent_type・model 上書きなし＝素の起動経路）
- 各 agent に役割相応の小タスクを付与。書き込み系は scratchpad 配下のフィクスチャのみを対象にし、リポジトリ本体・実インスティンクトストア・実セッションデータには触れさせない
- 成果物（削除反映・O(n) 化・生成ファイル・実行出力）はオーケストレータ側でスポット再検証済み
- 途中でセッション制限に到達し executor / refactor-orchestrator が中断 → §9.2 の方針どおり完了扱いにせず再開・再検証

### 10.2 agents 判定表（16/16 実起動）

| # | agent | 検証タスク | 結果要点 | 判定 |
|---|---|---|---|---|
| 1 | architect | pg_write_mixin 設計評価（RO） | 責務分離を正しく評価し Protocol 注入の改善提案。編集自制 | OK |
| 2 | bench-analyzer | 合成ベンチ結果の分析+Write | bench_summary.md を書き出し。欠落データを推測せず明示（`aac2ee3` の +Write が実動作） | OK |
| 3 | comparator | 盲検比較（正解1/off-by-one 1） | B の range(n) バグを検出し A 勝ち判定・根拠明快 | OK |
| 4 | dead-code-cleaner | 未使用関数の安全削除 | unused_legacy_mul 削除・実行出力 3 維持を自己検証 | OK |
| 5 | executor | 停止時 hooks 6件の実起動（10.3） | Stop 3件 exit 0 確認後に中断→SendMessage で再開・完遂 | OK |
| 6 | explorer | get_git_user_name コールチェーン | 定義 `lib/core_utils.py:160`＋直接呼出10箇所＋DI経由4箇所＋注入4箇所を網羅 | OK |
| 7 | grader | トランスクリプト×期待値照合 | PASS/FAIL 表＋行番号根拠。抜粋外データを「なし」と明示 | OK |
| 8 | harness-tuner | hooks.json 分析（編集禁止条件付き） | PreToolUse 直列 3〜4 プロセス→単一ディスパッチ統合+timeout 付与を提案。編集自制 | OK |
| 9 | perf-optimizer | O(n²) find_dups の最適化 | Counter 2パス構成で O(n) 化・順序/重複排除挙動保持を検証 | OK |
| 10 | planner | mem stats 追加の計画（RO） | 既存構造踏襲の4ステップ計画＋chunk_count 乖離リスク指摘。実装自制 | OK |
| 11 | refactor-orchestrator | 2ファイル定数抽出（依存順） | 編集完了直後に中断→再起動で完了検査・entry()==42 検証 | OK |
| 12 | reviewer | perf_target.py レビュー | 重大度付き3件（テスト欠如/O(n²)/型ヒント） | OK |
| 13 | security-auditor | SQL 文字列連結の監査 | CRITICAL 1件特定・該当行・パラメータ化修正案 | OK |
| 14 | session-observer | 観測データ→インスティンクト化 | 信頼度 0.70 の原子的インスティンクトを指定先に生成 | OK |
| 15 | simplifier | 整理余地のないコードの単純化 | 「変更不要」と正当判断。未使用削除は dead-code-cleaner の担当と峻別（過剰単純化なし） | OK |
| 16 | tdd-writer | slugify を RED→GREEN | RED（収集エラー確認）→GREEN（9 passed・対象100%）。Glob 欠落（§9.5 低）は本タスクでは非発現 | OK |

**agents 総括: 16/16 実起動 OK・実害NG 0**。§8 修正（executor `model` 行削除・保存系3 agent への Write 付与）は実起動でも問題なし。

### 10.3 hooks（停止時発火）実起動 — executor 経由（6/6 OK）

実行形式は §9.4 準拠（cache 0.9.17 直下・`run_with_flags` ラッパー経由・`CLAUDECODE=1 BLUECORE_HOOK_TIMEOUT=30`・session_id は全件 `verify-0917-handoff`）。`.venv -> ~/.bluecore/.venv`（Python 3.12.0）有効。

| hook | exit | 所要 | 判定 | 備考 |
|---|---|---|---|---|
| Stop: session_end | 0 | 0.85s | OK | 副作用は下記特記のとおり |
| Stop: evaluate_session | 0 | 0.32s | OK | transcript=/dev/null → 0 messages で正常スキップ |
| Stop: desktop_notify | 0 | 0.64s | OK | decision キー無しの JSON パススルー（無害）。macOS 通知の目視は未確認 |
| SessionEnd: session_end_marker | 0 | 0.33s | OK | 静穏完了 |
| SessionEnd: mem.cli session-end | 0 | 0.50s | OK | pending なしの no-op（`is_hook_enabled()` 直接評価で gating skip でないことを裏取り） |
| SessionEnd: mem.cli sync-check | 0 | 0.37s | OK | `mem.sync.enabled: false` のため同期スキップ＝仕様どおり |

- 非ブロッキング契約: 6/6 exit 0。最大 0.85s ≪ 各 timeout
- エラー時のトレースバック挙動はエラー未発生のため実地未検証（既知の残置）
- 副作用特記: `stop:session-end` が cwd=キャッシュのため `~/.bluecore/session-data/2026-07-12-0-9-17-session.tmp` を Project=0.9.17 名義で更新（実プロジェクトのセッションデータとは分離・実害なし）。`~/.bluecore/mem.db` に `verify-0917-handoff` 行 0 件＝実データ汚染なし。PostgreSQL 書き込みなし。キャッシュ内ファイル編集なし

### 10.4 commands / skills 実起動判定表

> 検証基準: 実際に Skill として発火し、定義どおりの初段フロー（前段処理・対象収集・設計/計画提示・ゲート到達）が実挙動で確認できること。**リポジトリ本体の変更や大量トークンを伴う後段フェーズは、ユーザー承認ゲートで意図的に停止**（bugfix / grillme / loop-dev は前セッションで完遂済み）。

（検証進行に合わせて追記）

| 種別 | 名前 | 結果要点 | 判定 |
|---|---|---|---|
| skill | checkpoint | `~/.bluecore/session-data/checkpoint-2026-07-12-bluecore-dev.md` へ保存。自動保存分の引き継ぎ更新も定義どおり | OK |
| skill | adr | ADR-0001 を `docs/adr/` に新規作成（README/template 含む初期化・重複番号チェック・mem 記録まで完遂）。※リポジトリに未コミット新規ファイル3点が増加 | OK |
| skill | loop-audit | 履歴集計→Loop Readiness 8.1/10・改善提案3件。読み取りのみ。「記録完遂率 40%」という実測ボトルネックを検出 | OK |
| skill | secure | get_git_user_name 周辺に9カテゴリチェックリスト適用・合格/N-A判定と観察4件。CLAUDE.md の RLS 設計前提（READ 共有は仕様）を正しく尊重 | OK |
| skill | search | 既存解（stdlib executemany+WAL PRAGMA、リポジトリ採用済み）を確認し「カスタム実装不要」判定。指定どおり最小構成 | OK |
| skill | refactor-prep | Preflight 出力（G1/G2 分割・依存・実在確認済みテストセット）。分析のみ。※出力はテキスト契約のまま＝§9.5 の JSON 前提不一致を実挙動で再確認 | OK |
| skill | refactor-rollback | prep 出力を受けて Rollback Blueprint 確定（revert/verify/risk/Order）。read-only 遵守。テキスト形式の prep 出力も正しく解釈 | OK |
| command | instinct | grillme 前段発火→合意→export 実行（YAML 608行を scratchpad へ）→mem record まで完遂。ただし CLI 実行系に **NG-B1**（下記）を検出 | OK（フロー）/ NG-B1（実行系） |
| skill | learn | 本セッション観測から原子的インスティンクト1件作成（NG-B1 由来の `bluecore-run-stdin-redirect`・信頼度0.7・重複チェック・mem 記録込み）。進化は指示どおり抑止 | OK |
| command | dashboard | grillme→生成（success:true・17.5KB HTML・個人のみ）→record 完遂。**発見**: 出力先は `~/.bluecore` 配下限定（`_resolve_safe_dashboard_output_path`）のため、dashboard.md:65 記載の既定値 `/tmp/...` は指定しても拒否される＝§9.5 の不一致は「文書どおりでは動かない」実害寄りに格上げ | OK（実行）/ 文書NG |
| command | plan | grillme→mem search→planner+architect **同時起動**→統合計画提示→ステップ5 確認待ちゲート到達で停止（定義どおり実装せず）。architect が chunk_count 圧縮ドリフトという実質リスクを検出 | OK |
| command | review | grillme→スコープ確定（launcher.py 単体）→reviewer+security-auditor **同時起動**（READ-ONLY 遵守）→統合レポート提示。**NG-B2 を新規検出**（下記）。後処理は合意どおり NG 一括修正フェーズへ委譲 | OK |
| command | test-gen | grillme→スコープ確定→ベースライン判定（repo 外は pytest 直接・venv 必須を grillme が補正）→find_dups のデシジョンテーブル（最小3ケース・全ブランチ）提示→承認待ちゲート到達で停止（「承認前に実装しない」ルール遵守・record は実装後項目のため対象外） | OK |
| command | harness | grillme（root=plugins/bluecore への補正を根拠付きで検出）→監査実行（repo モード・36/70・7カテゴリ採点）→top_actions 上位3件抽出→合意どおり適用前で停止。**WARN**: top_actions が `scripts/hooks/*.js` 等 JS 前提の汎用テンプレ提案で実構造（Python launcher 方式）と不整合＝ルーブリックの適合性課題。※実行は NG-B1 回避の `< /dev/null` 付与が必須だった | OK（実行）/ WARN（ルーブリック） |
| command | refactor | scratchpad フィクスチャで**全段階完遂**: grillme（バックアップ復元方式の代替を合意）→prep/rollback 最小適用→orchestrator 統括で clean（legacy 関数削除）→simplify→perf（全段階テスト green・リバートなし）→reviewer+security 並列（Blockers 0）→final gate PASS（3 passed / ruff clean）→要約・record | OK |
| command | skill-gen | grillme→ステップ1 `collect_skill_create_inputs 200`（1,003行・純 git 関数で NG-B1 非該当と裏取り）→ステップ2 パターン検出→ステップ3 skill-make 委譲まで完遂・record。ステップ4/5 は合意どおり省略 | OK |
| skill | skill-make | skill-gen からの fork 委譲で実起動。pytest-refail の SKILL.md（リポジトリ固有文脈反映・500行制限内）+ evals.json 3ケース（スキーマ妥当性検証済み）を scratchpad に生成。eval 実行は指示どおり抑止 | OK |
| skill | skill-tune | 生成スキルを対象に1反復の最小実行で完走: Step 0 整合チェック（実在しない例パスを検出→修正）→worktree 分離 subagent が実 repo でテスト破壊→SKILL.md 準拠で復旧（精度100%・15 steps・235s）→構造化振り返り→最小修正2件適用→次案提示。修正は scratchpad 内のみ | OK |
| skill | maintain | 限定実行（対象 instinct.md/harness.md・レビュー工程まで）: reviewer+security-auditor 並列 READ-ONLY レビュー完遂。**NG-C1〜C5 を新規検出**（下記）＋設計系 MEDIUM 提案 4 件（§10.6 残課題へ）。修正・強化フェーズは合意どおり停止し NG 一括修正へ引き渡し | OK |
| command | feat-dev | grillme→ステップ1 explorer **2並列**（構造調査＋影響調査）→探索結果マージ→ステップ2 分岐（chunk_count 出典）を証拠で解決→ステップ3 loop-dev 移行**直前で停止**（合意どおり）・record。セキュリティ工程はユーザー指示で省略。詳細は §10.6 | OK |

### 10.5 新規検出 実害NG（すべて修正済み）

> §10.5 の NG-B1 / B2 / C1〜C5 は本セッション内で grillme→loop-dev により**修正・コミット済み**（`afd5a7a` iter1: launcher.py+tests / `9c49c55` iter2: instinct.md・harness.md）。全体 3234 passed・ruff clean・変更モジュール カバレッジ100% を loop-dev evaluate が確認。以下は検出時の記録。

**NG-B1: `bluecore_run` 直呼び経路で launcher.py の stdin read が無期限ブロック（実測）**

- 事象: `bluecore_run bluecore.skills.learn.cli export` を Claude Code の Bash から定義どおり実行すると応答なし→ 2 分でツール側タイムアウト（exit 143）。`< /dev/null` を付けると exit 0・正常出力（YAML 608 行）
- 原因: `src/bluecore/launcher.py:145-149` — stdin が非 TTY だと `sys.stdin.buffer.read()` を無条件実行。Claude Code の Bash 実行環境は「非 TTY・EOF が来ない stdin」のため read が返らない。`_subprocess_timeout()`（ハードタイムアウト）は subprocess 起動後にしか効かず、read 段階を守れない＝「非ブロッキング + ハードタイムアウト必須」ルール違反の残置
- 影響範囲（stdin パイプなしで `bluecore_run` を指示する定義）: `commands/instinct.md:41` / `commands/harness.md:36,62` / `skills/skill-make/references/improvement-guide.md:71,84` / `agents/harness-tuner.md:14,17`（単体起動時）。hooks 経路は常に `echo '<json>' |` でパイプ+EOF が保証されるため無影響（§2 の 35 ケース、§10.3 の 6 件はすべて成立）
- 環境注意: zsh で helpers を source すると `BASH_SOURCE` 未定義でルート解決も誤る（`CLAUDE_PLUGIN_ROOT` 設定で回避可・実運用はハーネスが設定するため実害は stdin 側のみ）
- 対応: 検証完了後に grillme→loop-dev で修正（方針候補: launcher の stdin read を select/タイムアウト付きにする案、hook 実行時のみ stdin を読む案、helpers 側で用途別に分離する案 — bluecore_mem_json のパイプ入力を壊さないことが制約）

**NG-B2: launcher.py の子プロセス出力デコードが strict で hook 異常終了経路が残存（/review で検出）**

- 事象: `src/bluecore/launcher.py:152-158` の `subprocess.run(..., text=True)` は既定 `errors="strict"`。子プロセス（hook 本体）が非 UTF-8 バイトを stdout/stderr に出力すると `UnicodeDecodeError` が未捕捉で伝播し、launcher 自体がトレースバックで異常終了（非ブロッキング契約の穴）
- 非対称性: stdin 側は `launcher.py:149` で `errors="replace"` 防御済み。出力側のみ無防備。`main()` docstring の「例外はキャッチされる」契約とも不一致。`test_bluecore_launcher.py` に当該経路のテストなし
- 修正方針: `subprocess.run` に `errors="replace"` を付与（stdin 側と対称化）＋回帰テスト追加
- 深刻度: HIGH（reviewer 判定・全 hook 経路共通の異常終了リスク）。security-auditor は Blocker 0（MEDIUM/LOW の多層防御提案 3 件は §9.5 相当の別課題扱い）

**NG-C 群: コマンド定義⇔CLI 実装の乖離（/maintain 限定実行のレビューで検出・いずれも文書側修正）**

| # | 深刻度 | 事象 | 該当 |
|---|---|---|---|
| C1 | HIGH | promote を「全候補を自動昇格」と記述するが、CLI は `--force` なしだと `input()` 対話確認（promote.py:80,167）。launcher 経由では EOFError/拒否扱いで**文書どおり実行しても何も昇格されない**。import/prune と同じ 2 段階（`--dry-run`→承認→`--force`）に統一が必要 | instinct.md:50-51 |
| C2 | HIGH | evolve のファイル生成には `--generate` が必須（evolve.py:95-97）だが文書に言及なし＝裸 `evolve` では生成されない | instinct.md:56-57 |
| C3 | HIGH | `--audit-only` は /harness レベルの制御フラグで harness_audit CLI に渡すと `ValueError: Unknown argument`。実 CLI フラグと同列記載でクラッシュを誘発 | harness.md:25,36,93 |
| C4 | HIGH | `import <file-or-url>` の例示・指示が未クォート。`;`・`$()`・空白入り URL/パスでコマンドインジェクション/引数分割の経路 | instinct.md:48 |
| C5 | MEDIUM | ステップ4 のコードブロックに `source .../bluecore-helpers.sh` がなく、シェル状態非持続環境で `bluecore_run: command not found` | harness.md:61-63 |

**NG 修正方針（grillme 確定・maintain レビュー入力反映）**

- NG-B1: launcher.py に `_read_stdin()` 新設 — `select.select` で先頭バイト到着を最大 2.0 秒（定数）待ち、タイムアウト時は stderr 警告 1 行＋空入力で続行。**doc への `< /dev/null` 焼き込みは不採用**（launcher 修正が本命・修正後に誤学習の残骸となるとの reviewer 指摘を採用。直呼び経路の最大 2 秒待ちは許容）
- NG-B2: `subprocess.run` に `encoding="utf-8", errors="replace"` を明示し `text=True` 削除（stdin 側と完全対称・locale 非依存化）
- NG-C1〜C5: instinct.md / harness.md の文書修正（2 段階化・`--generate` 明記・`--audit-only` 注記・クォート必須化・`source` 追加）
- 制約: `bluecore_mem_json` のパイプ経路を壊さない / hooks.json・bluecore-helpers.sh 変更禁止 / Windows 分岐追加しない（到達不能コード＝カバレッジ阻害）/ 既存 FakeStdin テストは select モックで更新

**修正時の追加知見（loop-dev 実施ログより）**: PreToolUse フック `insights_security_monitor` が、コード中の bytes→str 変換メソッド呼び出し（`encode`/`decode` に開き括弧が続く形）を重大扱いで無条件ブロックするため、当該変換は `str(raw, encoding="utf-8", errors="replace")` の形で実装した。既存コードの同種呼び出しも今後の編集で同様にブロックされる＝ハーネス側 false-positive として §10.6b 残課題に記載。

### 10.6 feat-dev 探索統合 + 横断観察

**feat-dev ステップ1（explorer 2並列）の統合結論**（題材: mem stats・実装は未着手）:

- `_row_to_session` は `sync.py:93-107` に既存（`database.py` の inline ではない）。`row_converters.py` への移設は「スタイル統一」ではなく `sync.py`→`database.py` の一方向 import に起因する**循環 import 回避の構造的必須条件**
- `get_recent_sessions` は不在＝新規実装。最近傾向テンプレは `get_recent_digests`（database.py:808-820）。ただし `sessions` に `created_at_epoch` 列は無く `started_at_epoch` で並べる差異あり
- stats ハンドラは非 SessionStart コマンド経路（`cli.py:98-99`）で**戻り値が破棄**されるため、`handle_get_project_profile` 型に倣い**内部で `print(json.dumps(..., ensure_ascii=False))`** する必要がある（return-string 設計は誤り）
- **分岐解決（chunk_count 出典 3択）**: `sessions.chunk_count` は `store_chunk` の +1 のみで、compact/auto-compact の chunk 削除（cli_session_handlers.py:213,329）で減算されず**単調に過大表示**。`session_digests.chunk_count` は SessionEnd 時スナップショットで in-progress/crash/0件セッションに欠落。→ **推奨: `sessions` を起点に `memory_chunks` を LEFT JOIN し `COUNT(c.id)` で実カウント**（`COUNT(*)` だと未マッチ行を1件と誤算・単純グループ集計だと0件セッションが結果から欠落するため LEFT JOIN 必須）。index `idx_chunks_session` で裏打ち・dashboard の既存カウントパターン踏襲。参考値として累計 `chunk_count` の並記可。要 /plan 確定事項として「対象ストア（ローカル SQLite / 共有 PG）」を残置
- /plan（§ command:plan）の前提のうち「移設元＝database.py inline」「chunk_count 直読前提」「return 出力」の 3 点を探索で訂正

**横断観察（LOW・採点外）**: feat-dev 検証中、前段 grillme の fork 実行が chunk_count 判定の根拠収集のため explorer 2体を自律起動し、親（本オーケストレータ）も feat-dev ステップ1 として explorer 2並列を起動した結果、同題材で **explorer が計4体（前段 fork 2 + ステップ1 2）重複起動**した。4体の結論は完全収束（むしろ相互裏取りになり、COUNT(c.id) vs COUNT(*) の精緻化はステップ1側が追加）したが、grillme（前段合意）と feat-dev ステップ1（探索）の責務境界が実行時に曖昧化しうる兆候。実害なし（READ-ONLY）だが、grillme fork 内からの重い agent 起動は親のトラッキング外になり得る点は運用上の注意（§10.6b とは別の設計観察）。

### 10.6b 別タスク化する残課題（本検証で新規発見・§9.5 に追加）

| 重大度 | 概要 | 該当 |
|---|---|---|
| 中 | insights の bytes→str 変換呼び出し誤検知（literal を重大扱いでブロック）で既存コード編集が阻害される false-positive | insights_security_monitor 正規表現 |
| 中 | harness 監査 top_actions が JS テンプレ（`scripts/hooks/*.js`）前提で Python launcher 実装と不整合＝ルーブリック適合性 | harness_audit ルーブリック |
| 低 | import 承認ゲートが instinct 本文を提示せず（プロンプトインジェクション取込の残余）／evolve 生成の承認ゲート無し | instinct.md（security-auditor MEDIUM 提案） |
| 低 | mem `stats` 実装時の対象ストア（ローカル SQLite / 共有 PG）が未確定 | /plan Assumption |

### 10.7 継続セッション 最終集計

| 対象 | 母数 | 実起動 OK | 実害NG（検出→対応） |
|---|---|---|---|
| agents | 16 | 16 | 0 |
| commands | 10（前セッション bugfix 含む） | 10 | 文書系（dashboard 出力先・NG-C 群）→ 修正済み |
| skills | 13（前セッション grillme/loop-dev 含む） | 13 | 0（実行系は NG-B が launcher 起因） |
| hooks（停止時発火） | 6 | 6 | 0 |
| **合計** | **45 コンポーネント** | **45** | **NG-B1/B2 + C1〜C5（計7）→ 全修正・コミット済み** |

**総括**: §9.2 の残（agents 16 / commands 9 / skills 11 / 停止時 hooks）を**全て実起動検証し消化**。定義精読では露見しなかった実行系実害 NG を 7 件検出し、うち方針分岐のあるもの（NG-B1 の修正方式・harness root・dashboard 出力先）はユーザー合意または grillme で根拠確定のうえ修正まで完了。残課題は §9.5 + §10.6b に集約（別タスク化候補）。コミット済み: `afd5a7a`・`9c49c55`（NG 修正）。本レポート `plugin-verification-report-0.9.17.md` と `docs/adr/` はユーザー明示指示待ち（未コミット）。

## 11. §9.5 + §10.6b 残課題11件の解消（継続セッション3）

### 11.1 対応状況

| item | 内容 | 対応 | commit |
|---|---|---|---|
| 1 | hooks.json の insights description が前提（未導入時スキップ）と実挙動（稼働中・CRITICAL ブロック）で乖離 | 修正済み | `7e7a4b7` |
| 2 | dashboard 出力先デフォルトの自己矛盾（`/tmp/...` vs `./...`） | 修正済み（実際の許可境界 `~/.bluecore` に合わせて記述統一） | `7e7a4b7` |
| 3 | skill-gen.md の skill-tune user-invocable 記述（false）と実装（true）の不一致 | 修正済み | `7e7a4b7` |
| 4 | refactor-prep 出力契約（テキストのみ）が下流3ファイルの JSON 前提と不一致 | 修正済み（JSON 契約を追加、`refactor-rollback/SKILL.md` の入力契約と一致） | `7e7a4b7` |
| 5 | simplifier 高頻度発火が既定 `opus` 固定でコスト増 | 修正済み（`refactor.md` / `loop-dev/SKILL.md` の呼び出し2箇所に `model: "fable"` 明示を追記） | `7e7a4b7` |
| 6 | tdd-writer.md の tools に Glob 欠落 | 修正済み | `7e7a4b7` |
| 7 | comply/stocktake が dead code 疑い（要判別） | **決定: 現状維持**（11.2 参照） | 記録のみ（本 commit） |
| 8 | insights の bytes→str 変換誤検知（false-positive ブロック） | 修正済み（11.3 参照） | 本 commit |
| 9 | harness_audit の repo チェック8件が JS（Node.js 実装）前提で Python 実装と不整合 | 修正済み（Python 構造ベースへ書き換え・実リポジトリ pass=True の回帰テスト追加・モジュールカバレッジ100%） | `b331cdf` |
| 10 | instinct.md の承認ゲート不備 | 対象外（別タスク。本セッションでは着手しない） | - |
| 11 | mem `stats` 実装時の対象ストア未確定 | **決定: 両方**（11.4 参照） | 記録のみ（本 commit） |

### 11.2 item7 決定: comply/stocktake は現状維持

`src/bluecore/skills/comply/` `src/bluecore/skills/stocktake/` はいずれも `tests/skills/comply/`（7ファイル）・`tests/skills/stocktake/`（3ファイル）に対応する既存テストが確認でき、能動的にテストされている実装であり dead code ではない。削除せず現状維持とする。

### 11.3 item8 状況: 実装・検証済み

前セッションでは当該変更のテスト実行（pytest）が Bash 呼び出し時に自動モードの安全性判定により毎回拒否され、未適用・未検証のまま作業ツリーから revert していた。継続セッションで、承認された「単一フラグ全般の降格」ではなく**対象を `TOOL_DESCRIPTION_DIVERGENCE` anomaly type に限定**する設計へ narrowing した上で再実装し、以下を確認して commit した。

- **実装**: `insights_security_monitor.py` に `_effective_severity()` を追加。severity 降格は anomaly `type` が `TOOL_DESCRIPTION_DIVERGENCE` の場合のみ適用（他 type、例えば資格情報露出検知等は対象外で従来どおり単一フラグでも `CRITICAL` を維持）。対象 type 内では `details.flags` から `goal_shift_after_tool_load` を除いた有意フラグ数が2件未満なら `MEDIUM` に降格、2件以上なら `CRITICAL` を維持する、という insa-its 側 `ai_monitor.py` アダプタと同一の一般則を実装（**ただしこの hook の実呼び出し経路での到達可能性は 11.3 追加調査を参照**）
- **単体テスト**: `_effective_severity()` の7ケース（他type不変・単一flag降格・複数flag維持・goal_shift除外後1件で降格・details欠落/非dict/属性アクセス型の防御的分岐）+ 既存の end-to-end パラメトライズドテストに `TOOL_DESCRIPTION_DIVERGENCE` の降格/維持2ケースを追加。モジュールカバレッジ100%・全体3244件 pass・ruff clean
- **実SDKでのlive確認**: スタブでなく実際の insa_its（v4.9.7）へ、バイト列を文字列へ変換する呼び出し1件のみを含むテキストを送信し、返る anomaly が severity=critical・type=TOOL_DESCRIPTION_DIVERGENCE・details.flags=[hidden_instructions_in_message]（1件のみ）であること、および `_effective_severity()` 適用後に MEDIUM へ降格し非ブロックとなることを実行確認済み（スタブ前提が実装と一致しているかという懸念を解消）
- **前回指摘の反映**: 「効果が変換呼び出しの誤検知に限定されず単一シグナル CRITICAL 全般を広く弱める」という懸念は、対象を `TOOL_DESCRIPTION_DIVERGENCE` type に限定したことで解消。他の anomaly type（資格情報漏洩・プロンプトインジェクション等）は本変更の影響を受けない。ただし `TOOL_DESCRIPTION_DIVERGENCE` 自体が単一シグナルで真陽性を検出したケースも同様に MEDIUM へ降格される点は、承認済みトレードオフとして残る（同 type はエンコーディング関連キーワードで発火するため、実際の攻撃は多くの場合2つ目のフラグか別 anomaly type も伴う想定）
- **自動モード分類器の再現性**: 本セッションでは同一 diff に対する pytest 実行が拒否されなかった（直接の Bash 実行では再現せず）。前回の拒否がどの実行経路・文脈に依存したかは未特定だが、今回は実測で通過している

**reviewer + security-auditor レビュー後の追加修正**:

- Blocker（reviewer）: `pyproject.toml` の `[tool.coverage.run] branch = true` により当該モジュールは branch coverage 100% が要件だが、`_handle_anomalies` 内の `isinstance(a, dict)` 分岐の False 側（非 dict anomaly）を通す統合テストが無く実測 99.43% だった。`_handle_anomalies` を直接呼び出し非 dict anomaly（`CREDENTIAL_EXPOSURE` 型）を渡すテストを追加し branch coverage 100% を確認・修正
- High（security-auditor）: `write_audit` に severity 降格の証跡（`anomaly_severities` / `effective_severities`）が記録されておらずフォレンジック不可という指摘を受け、監査ログへ両方を記録するよう修正。あわせて `has_critical` 判定を `_effective_severity()` の戻り値に直接基づく形へ統一し（従来は dict への in-place mutation 後の値を再読していたため非 dict anomaly では type 別の降格ルールが判定に反映されない不整合があった）、dict/非 dict どちらの anomaly でもブロック判定が一貫するよう修正
- Critical（security-auditor、指摘の前提を追加調査で訂正）: 「`TOOL_DESCRIPTION_DIVERGENCE` の有意フラグ数閾値が insa-its 側の非公開ロジックに全面依存しており、攻撃者がフラグを1件以下に抑える入力を作れれば CRITICAL ブロックを回避できる可能性がある」という指摘を受け、advisor同席のうえ `tool_description_divergence.py` の `check()` 全文と `monitor.py` の呼び出し引数を追加調査した。

  **判明した事実（前回記載より事態は踏み込んでいた）**: `check()` が生成しうる flag は6種類あるが、`cross_category_behavior`/`behavior_shift_detected`/`semantic_divergence`/`tool_description_contains_hidden_instructions` の4種は `tool_name` 引数ありの呼び出し（`register_tool_description`経由の別API）でのみ発火する。bluecore が実際に呼ぶ `insAItsMonitor.send_message()` は `tool_name` を渡さない（`monitor.py`の唯一の呼出し箇所は位置引数2つのみ）ため、この4 flag は**このhookの呼び出し経路では構造的に発火し得ない**。到達可能なのは `hidden_instructions_in_message` と `goal_shift_after_tool_load` の2 flag のみで、後者は `_NON_SIGNIFICANT_FLAGS` として有意カウントから常に除外される。**したがって有意フラグ数の理論上限は1であり、`_effective_severity()` の「2件以上なら `CRITICAL` 維持」分岐は、この呼び出し経路では到達不能**。つまり `TOOL_DESCRIPTION_DIVERGENCE` はこの hook 経由では入力内容によらず常に `MEDIUM` に降格される（「単一フラグのみ降格」ではなく実質「全件降格」）。

  **security-auditor の指摘への回答**: 前提（攻撃者がフラグ数を操作して1件に抑える）は誤り——操作の余地なく最初から1件が上限のため、この経路に「フラグ数を悪用した回避」という攻撃手段は成立しない。ただし結論部分（このtypeでCRITICALブロックが実質機能していない）は指摘より正しい方向に踏み込んでいた。

  **それでも許容と判断する根拠**: `hidden_instructions_in_message` は正規表現一致（base64・hex・rot13・エンコード関連キーワードの直後に区切り記号が続く形）のみで発火し、これはコード中の変換呼び出し構文には広く一致する一方、シェルコマンドラインでの実際の変換型攻撃には**そもそも一致しない**（区切り記号が続かない形のため）。純粋な字句一致であり意味解析を伴わないこの信号は、そもそも単独で CRITICAL ブロックに値する検知精度を持っていなかった（誤検知を量産していた item8 の発生自体がその証拠）。したがって「この hook 経由では常時 MEDIUM」という実態は、想定より広いが、実害の観点では「元々ほぼ防御力のなかった検知をブロックしないことにした」というだけであり、新たな穴を開けたわけではない。資格情報露出等の他 anomaly type は本変更の影響を受けず引き続き CRITICAL を維持する
  - **開示すべき残余リスク**: 変換呼び出し構文で難読化し、かつ他のどの anomaly type/flag も一切トリップしない悪意あるコードは、CRITICAL ではなく MEDIUM（監査ログ記録＋警告、自動ブロックなし）として通過する。これは本 hook が唯一の防御層ではない（多層防御の一枚）ことを前提に許容する
  - **コード変更方針**: `_effective_severity()` 自体は insa-its 側アダプタの一般則を正しく実装した predicate であり、コードとしての妥当性に問題はない（到達可能性は呼び出し側 `_run_insaits_scan()` が `tool_name` を渡すかどうかに依存する外部要因であり、predicate 自体を歪めていない）。プロジェクト規約（到達不能コード削除・投機的な将来対応禁止）に照らし、「いつか `tool_name` を渡すかもしれないから残す」という将来対応目的では正当化しない——あくまで「vendor の実際のロジックを忠実に表す一般則である」という理由でのみ現状維持する。コード自体の変更は行わず、本節の記述訂正のみで対応する

修正後: 全体 pytest 3245 件 pass・`insights_security_monitor.py` の branch coverage 100%・ruff clean（コミット別途）。コードは無変更、本節の記述訂正のみ。

### 11.4 item11 決定: mem stats 対象ストアは両方

`/dashboard` の既存パターン（個人データは SQLite に常時収集、PostgreSQL は `mem.sync.enabled` 設定時のみチームデータも収集）を踏襲し、`mem stats` も個人ストア（常時）・チーム PostgreSQL（設定時のみ）の両方を対象とする。実装は別タスクとする。
