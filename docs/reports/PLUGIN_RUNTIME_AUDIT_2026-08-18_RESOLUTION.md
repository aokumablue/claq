# bluecore プラグイン実機ランタイム監査 — 是正報告

実施日: 2026-08-18
対象文書: `docs/reports/PLUGIN_RUNTIME_AUDIT_2026-08-18.md`（以下「監査報告」）
方針: 監査報告の全指摘（R-01〜R-12、静的リスク 6.1〜6.4、§8）をべき論で再検証し、実欠陥のみを修正した。誤検知（実装済み・監査者が作った契約）と設計判断（仕様どおり）は理由を一次資料の行番号付きで示し、修正しない。

前提の確認: 監査報告は公開リポジトリ `6a40bd2`（本リポジトリの `8837d79` = v0.9.31）を対象にしている。`git diff 8837d79..HEAD -- plugins/bluecore/` は着手前時点で空であり、全指摘は着手時点の現行コードにそのまま該当していた（陳腐化なし）。

コミット: `c80dd10`〜`a188cc5`（7 コミット、フェーズ順）

---

## 1. 修正した内容

### 1.1 R-01 — commit 品質フックの検査不能時 fail-open（`c80dd10`, `65ed80d`）

対象: `plugins/bluecore/src/bluecore/hooks/pre_bash_commit_quality.py`, `commit_quality_scanner.py`

- **subprocess タイムアウト欠落**（監査 6.1 と同一根）: `_git_name_only`（`pre_bash_commit_quality.py`）と `get_staged_file_content`（`commit_quality_scanner.py`）の `subprocess.run` に `timeout` が無かった。`_resolve_repo_root` に既にあった 5 秒を基準に統一した。
- **R-01a（malformed JSON の fail-open）**: 同じ PreToolUse イベントで `config_protection.py` は parse 不能 JSON を exit 2 にする一方、本フックは exit 0 で素通ししていた。ただし単純に対称化はしなかった — `config_protection` の matcher は書込みツール限定だが本フックの matcher は `Bash|shell|run_terminal_command` 全体であり、malformed JSON というだけで一律 deny すると commit と無関係な全 Bash 呼び出しを塞ぐ。加えて `read_raw_stdin()` は 1MiB 超の無警告切り詰めや 5 秒デッドライン超過で部分データを返す経路があり、長い heredoc を含む正当な Bash 呼び出しが「parse 不能」に化けうる。そこで、parse 不能時は既存の regex フォールバック `\bgit\s+commit\b`（JSON を経由せず raw 文字列に直接効く）で commit 判定を試み、commit と判定できた場合のみ deny、それ以外は exit 0 + ログとした。
- **R-01b（裸の except による無言 fail-open）**: `evaluate()`/`main()` の `except Exception` が commit 確定後の例外もログなしで exit 0 にしていた。`_evaluate_confirmed_commit()` に分離し、commit と確定した入力に対する例外は fail-closed（exit 2 + 理由）にした。commit 確定前（JSON 抽出・判定段階）の例外は従来どおり非ブロッキングだが、ログは必ず出すようにした（`main()` の裸 except も同様）。
- **`get_staged_files()` の fail-open**: git 自体の失敗・timeout と「ステージ 0 件（`--allow-empty` 等の正常系）」を同じ `[]` に潰していた。`_git_name_only` の戻り値を `None`（git 失敗）と `[]`（確認済み 0 件）に区別し、`None` の場合は fail-closed にした。`get_unstaged_modified_files()`（HEAD 不在＝初回コミットの正常系失敗）は契約を変えず `[]` のまま維持。
- **R-01c 残余**: `find_file_issues()` の `content is None`（ファイル読み取り不能・symlink/traversal 拒否）を空 issue（無言許可）ではなく `scan_error`（severity=error）にした。既存の `scan_error` 機構自体は既に実装済みだった（1.7 節参照）。
- `_count_file_issues()` に防御的アクセス（`.get()`）を追加し、未知 severity を安全側の error 扱いにした（従来は total にのみ計上され error/warning/info いずれにも入らず見逃されていた）。

検証: `printf 'not-json' | ...pre_bash_commit_quality` は exit 0 のまま（従来どおり、ただしログが出る）だが、`{"tool_input":{"command":"git commit -m x"` （閉じ括弧欠落）は exit 2 に変わった。テスト 100% カバレッジ。

### 1.2 R-12 — Windows 形式 `git.exe commit` の検出漏れ（`43c94ee`）

対象: `pre_bash_commit_quality.py`

`_find_git_commit_args` のトークン比較が `token != "git"` の完全一致で、`/usr/bin/git` は regex フォールバックにのみ救われ、`git.exe` は `.exe` が `\bgit\s+commit\b` の `\s` を破るため regex フォールバックからもすり抜けていた。`_is_git_executable_token()` を追加し、basename 化・`.exe` サフィックス除去・大小無視で比較するようにした（memory カード `git-git-subcommand` の「shlex トークン走査で最初の非オプション語」方式の延長）。`/usr/bin/git`・`git.exe`・`GIT`・`C:\...\git.exe` いずれも検出することを確認。

### 1.3 R-07 — `pyproject.toml` 等の Ruff/coverage/pytest 設定が config_protection の対象外（`bc52ab4`）

対象: `plugins/bluecore/src/bluecore/hooks/config_protection.py`

`.ruff.toml`/`ruff.toml` は保護するのに、同じ設定を書ける `pyproject.toml`（本リポジトリ自身が `[tool.ruff]`/`[tool.coverage.*]`/`[tool.pytest.ini_options]` を持つ）は無防備だった。`pyproject.toml` は version bump 等の正当な編集が頻繁なため全面ブロックはせず、`CONDITIONALLY_PROTECTED_FILES`（`pyproject.toml`/`setup.cfg`/`tox.ini`/`package.json`）を新設し、書き込み内容が lint/format/coverage 設定を弱めうる場合のみブロックするようにした。

判定は二段構え:
1. **主**: 対象ファイルをディスクから読み、Edit の `old_string` が属するセクション見出しを解決する（フックは素の Python でツール制限を受けないため実行可能）
2. **副**: 主が使えない場合、書き込みテキストにセクション見出し（`[tool.ruff`/`[tool.coverage`/`[tool.pytest`/`[flake8]`/`[mypy]`/`[pycodestyle]`）または lint 系キー（`ignore`/`select`/`per-file-ignores`/`exclude`/`fail_under`/`addopts`）が直接現れるかを照合する

見出しだけの照合だと、既存 `pyproject.toml` への Edit（`fail_under = 100` → `80` のような値行編集で見出しが `old_string`/`new_string` に現れない）という最も自然な迂回路を素通りさせてしまうため、副判定のキー照合を必須にした（レビュー時に指摘され修正）。内容が一切取得できない入力は fail-closed で deny する。

あわせて `PROTECTED_FILES` の非対称（`.stylelintrc.yaml` と `.markdownlint.yml` の欠落。eslint/prettier は `.yml`/`.yaml` 両方持つのに stylelint/markdownlint だけ片方欠落）を解消し、docstring 2 箇所の実装との乖離（`:32` の「matcher が全ツール」という誤記、`:207-208` の「Copilot で exit 0 に変換される」という誤記 — 実際は `output_adapter.py:106` が無条件 `return 2`）を修正した。

検証: 見出しを含まない `fail_under` 値行 Edit・`pyproject.toml` への `[tool.ruff]` セクション追加・`setup.cfg` の `[flake8]`・`package.json` の `eslintConfig` はいずれも exit 2。version bump のみの編集は exit 0 のまま。

### 1.4 R-04 — knowledge の secret 無検査（`e474a0b`）

対象: `plugins/bluecore/src/bluecore/mem/knowledge_input.py`, `redaction.py`

`handoff`/`logger` は `redact()` を通すのに、`learn`（`mem.cli learn`）経路は無検査だった。secret らしい文字列を含む knowledge がそのまま DB に書かれ、次回 SessionStart の `<bluecore-memory>` へ本文ごと再注入されていた。

**既定 status は `active` のまま維持**（ユーザー判断。CLAUDE.md のデータモデル前提と README.md:373-375 に明記された仕様であり、`learn` はフック経由ではなくコマンド末尾でエージェントが明示的に叩く設計のため）。実害である redaction 欠落のみを塞いだ。

knowledge 本文には完全な commit SHA や長い識別子が正当に現れるため、既存 `redact()` の汎用エントロピーパターン（32 桁 hex・40 文字超 base64）をそのまま適用すると本文を破壊する（`handoff.py:66-69` がパスへの redact 適用を避けている理由と同じ罠）。`redact_knowledge_text()` を新設し、既知プレフィックス・キーワード系パターン（JWT/各種 API キー/bearer/password 代入/email/private IPv4）のみを適用する縮小版にした。

key は redact 前の生 title から生成するようにした（`generate_key` は title から key を導出するため、redact 後の title を使うと secret を含む複数カードが `[REDACTED]` 由来の同じ key に衝突し、`show <key>` も引けなくなる）。

あわせて `bluecore-helpers.sh` の `--status` doc に `archived` を追記（実装は受理するが doc に無かった乖離）。

検証: 隔離 DB で secret を含む `learn` → `show` に `[REDACTED]` が出ることを確認。同じ title を secret 有り/無しで登録しても key が一致すること、40 文字の commit SHA が保持されることを確認。

### 1.5 R-08 / R-09 / R-10 + 隣接欠陥 — 定義ファイルの契約強化（`a3cff53`）

- **R-08**（`agents/grader.md`）: `comparator.md` にある「最終出力は単一 JSON object 1 個のみ・前置き禁止」の `## 出力契約` セクションが grader 側に無かった。追加し、保存後に `summary` の不変条件（`total == len(expectations)` 等）を自己検証する手順を明示した。出力スキーマ自体（`total`/`passed`/`failed`/`pass_rate`）は既に `grader.md:179` にあり新設していない。
- **R-09**（`skills/maintain/SKILL.md`）: 対象 surfaces が 1 つも見つからない場合を PASS ではなく `BLOCKED` として停止する規則を追加した。監査報告の「`CLAUDE_PLUGIN_ROOT` を primary target にする」提案は不採用（1.9 参照）。
- **R-10**（`skills/refactor-rollback/SKILL.md`）: Blueprint の `verify="{cmd}"` が `tests.baseline`/`group`/`final` のどれ由来か記述が無かった。`tests.group` を割り当て、空配列（`refactor-prep` の「検証手段なし」判定）の場合は `verify="NOT_AVAILABLE"` + `required_action=manual_review` にする規則を追加。あわせて `refactor-prep`/`refactor-rollback` 間の「必須」表現の非対称（prep はキー必須・値は空配列許容、rollback は同じキーを単に「必須」とだけ列挙）を解消し、「検証手段なし」が JSON 契約自体（空配列という値そのもの）で下流に伝わることを明記した。
- **隣接欠陥（R-11 の実質的中身）**: `planner.md:104` と `architect.md:88` は `tools: Read, Grep, Glob`（Bash 非保持）なのに `bluecore_run` の実行を指示していた（`security-auditor.md:71` だけが正しく「Bash を持たないためできない」と明記）。両ファイルを security-auditor.md と同じ表現に統一した。あわせて `planner.md` に「編集禁止だが計画本文の出力は必須」という READ-ONLY 制約セクションを追加した（reviewer.md の同種セクションに倣う）。

### 1.6 デッドコード削除（`a4b0c8f`）

対象: `hook_common.py`, `output_adapter.py`, `lib/harness.py`, `lib/subprocess_utils.py`

`hooks.json` に PostToolUse/UserPromptSubmit の matcher は存在せず、`emit_post_tool_use_output`/`emit_user_prompt_submit_output`/`adapt_tool_output`/`extract_tool_result_text`/`check_output_text` の呼び出し元はテストのみだった（監査報告 6.3 と同一の指摘に加え、`emit_user_prompt_submit_output` と `check_output_text` は監査報告に記載が無いが調査で同種のデッドコードと判明したため合わせて削除）。テストごと削除し、カバレッジ 100% を維持した（コードとテストを対にして消したためカバレッジ稼ぎではない）。

### 1.7 前提の明文化（`a188cc5`）

- CLAUDE.md に Python 3.12+ 前提と R-02 の fail-open 判断根拠を追記（1.8 参照）
- README.md に対応 OS（macOS/Linux、Windows 未対応の理由）を明記

---

## 2. 修正しなかった内容とその理由

### 2.1 R-01c（一部）— scan_error 機構自体

**判定: 誤検知（実装済み）**。監査報告の修正案「scanner の読み取り不能・例外を `scan_error` として返し error severity は block にする」は、`commit_quality_scanner.py:341-382` に既に実装されていた（`get_staged_file_content`/`get_worktree_file_content` からの例外、scan target 判定の例外、lint/secret scan の例外はいずれも `_scan_error_issue()` で `scan_error` issue を積む）。未実装だったのは `content is None`（正常系のエラー戻り値）の 1 経路のみで、これは 1.1 節で対応済み。

### 2.2 R-02 — Python 3.12 未満で保護 hook が exit 0 のまま無効化

**判定: 設計判断（維持。ユーザー確認済み）**。`launcher.py:52-58` に「exit 2 にすると全 Edit/Bash が拒否されセッションが即死し、守るべき対象より被害が大きい」という設計判断が既に明記されている。かつ `hooks.json` は PATH 上の裸 `python3` を呼ぶため、fail-closed にすると 3.12 未満の環境ではセッション内から PATH を直す手段（Bash 自体）ごと塞がれ復旧不能になる。監査報告が提案する「インストール時に対応 Python を検証する」という逃げ道は、「ランタイムは venv も install.sh も持たない」という確立済み方針（memory カード `runtime-no-venv`）と衝突し採用できない。ユーザーへ選択肢を提示し、「3.12+ 前提のプラグインの旨、CLAUDE.md に追記して現状維持」の回答を得た。CLAUDE.md への追記のみ実施（1.7 節）。

### 2.3 R-03 — `launcher --bg` の子プロセス失敗が親に伝播しない

**判定: 誤検知（実装済み）**。監査報告は「`--bg nonexistent.module` は stdout/stderr なしで exit 0」を指摘するが、これは**親プロセスの**観測に限った話である。子（detach された対象プロセス）の stdout/stderr は `hook_common.py:312-320` の watchdog により `~/.bluecore/logs/bg-YYYY-MM-DD.log` へ記録される（`_detach_log_path()`。F-18 で対処済み）。SessionEnd は非同期 best-effort 処理であり、どのホストも `--bg` の exit code を消費しないため、親の exit code を「起動受付成功」と「処理成功」で分離する変更は実利が無い。診断ログという形で既に可観測化されている。

### 2.4 R-05 — handoff の transcript が不信データとして扱われない

**判定: 誤検知（設計判断）**。allowed-root allowlist が無いのは `handoff.py:104-127` に明記された意図的判断: Claude Code は `~/.claude/projects/...`、Copilot CLI は `~/.copilot/...` と host ごとに transcript の置き場所が異なり、allowed-root で封じ込めるとどちらかの host で壊れるため、host 非依存の性質検査（symlink 拒否・通常ファイルのみ・所有者一致）で防御している（F-08a 対応）。

また監査報告の「ユーザー文を信頼境界なしで次セッションへ渡す」という記述も事実誤認: `handoff.py:239` は既に `compact_line(redact(strip_tags(strip_ansi(text))), 200)` を適用しており、ANSI 除去・タグ除去・secret redaction を経てから DB に入る。攻撃者が所有者一致の任意ファイルを home 配下に書ける状況を仮定するなら、transcript 経由の injection より直接的な攻撃経路（他の設定ファイル改ざん等）が既に成立しており、この経路だけを塞ぐ実益は薄い。

### 2.5 R-06 — reviewer の read-only が技術的に強制されない

**判定: 設計判断（維持）**。`reviewer.md:11` に「`ruff check` と渡された `test_cmd` の再実行（RED→GREEN 独立検証）が職務のため、`tools` から Bash を外せない（security-auditor と異なる点）」と明記されている。Bash 権限がある限り「技術的強制」は原理的に不可能であり、監査報告の代替案（read-only validator 専用ツールへの分離）は「渡された任意の `test_cmd` を実行する」という reviewer の職務要件そのものと両立しない。現状は散文規約 + 明示的な自己申告（tools 権限で防げない旨をエージェント自身が認識している）で運用しており、これ以上の変更は職務を損なう。

### 2.6 R-11 — planner agent が read-only を「計画を出さない」と解釈する

**判定: 誤検知（監査者が作った契約）**。監査報告が引用する `PLAN_PRESENT=no`/`APPROVAL_STATE=pending` という契約キーワードは、リポジトリ全体を検索しても `plugins/bluecore/` 配下（commands/skills/agents/hooks/src すべて）に一切存在しない。唯一のヒットは監査報告自身の該当行だけである。つまり planner が違反したとされる契約は、bluecore 側が定義したものではなく監査プローブが仮定した契約であり、これを「bluecore 側の契約違反」として扱うのは誤り。

ただし、この所見の**背後にある実質的な原因**（`planner.md` に read-only/出力必須の規定が 1 行も無く、`tools: Read, Grep, Glob` なのに `bluecore_run` の実行を指示する矛盾がある）は実在する欠陥として特定し、1.5 節で修正済み。

### 2.7 §8 — Claude 側のインストール差分（v0.9.29 / Agents 0）

**判定: 既に解消（確認済み）**。監査報告が観測した時点では Claude marketplace が v0.9.29 のまま stale だったが、本作業時点で `claude plugin list` は `bluecore@bluecore Version: 0.9.31` を返し、`~/.claude/plugins/marketplaces/bluecore` の `plugin.json` も `version: 0.9.31` / `agents: 13 件` を正しく列挙していることを確認した。追加対応不要。

### 2.8 静的リスク 6.2 — `select.select` の Windows portability

**判定: 対応 OS の明示で決着**。移植する（Windows 対応を実装する）のではなく、README.md/CLAUDE.md に「対応 OS は macOS/Linux」と明記することで解決した（1.7 節）。Windows 対応の実装は本監査のスコープを超える新機能追加に相当する。

### 2.9 静的リスク 6.4 — Copilot hook registration の静的確認限界

**判定: 修正対象なし**。これは実装の欠陥ではなく、検証手段（配布・アップデート時の integration check）の話であり、監査報告自身も「絶対に登録されないとは判定しなかった」としている。今回のスコープ（べき論による欠陥修正）には該当しない。

### 2.10 R-09 の代替案（`CLAUDE_PLUGIN_ROOT` を primary target にする）

**判定: 不採用**。`maintain/SKILL.md:29`（旧行番号）に「開発リポジトリでは repo 版を source する（プラグインキャッシュ版は stale の可能性）」という相対パス採用の意図が明記されている。これを反転して `CLAUDE_PLUGIN_ROOT` を primary にすると、開発リポジトリ（本リポジトリ）でのメンテ実行そのものが壊れる。採用した対応（対象なし → PASS ではなく BLOCKED で停止）は、consumer fixture から誤って起動した場合の誤認だけを解消し、開発リポジトリでの正常動作を維持する。

---

## 3. 判定サマリー表

| # | 対象 | 判定 | 対応 |
|---|---|---|---|
| R-01a | pre_bash_commit_quality.py | 実欠陥 | 修正（`c80dd10`/`65ed80d`） |
| R-01b | 同上 | 実欠陥 | 修正 |
| R-01c | commit_quality_scanner.py | 一部誤検知（機構は実装済み）・残余は実欠陥 | 残余のみ修正 |
| R-02 | launcher.py | 設計判断 | 現状維持＋CLAUDE.md明文化（`a188cc5`） |
| R-03 | launcher.py / hook_common.py | 誤検知（実装済み） | 無変更 |
| R-04 | knowledge_input.py | 実欠陥（redaction）／設計判断（status） | redaction のみ修正（`e474a0b`） |
| R-05 | handoff.py | 誤検知（設計判断） | 無変更 |
| R-06 | reviewer.md | 設計判断 | 無変更 |
| R-07 | config_protection.py | 実欠陥 | 修正（`bc52ab4`） |
| R-08 | grader.md | 実欠陥 | 修正（`a3cff53`） |
| R-09 | maintain/SKILL.md | 実欠陥（代替案は不採用） | 修正（`a3cff53`） |
| R-10 | refactor-rollback/prep SKILL.md | 実欠陥 | 修正（`a3cff53`） |
| R-11 | planner.md | 誤検知（契約不在）／実質原因は実欠陥 | 実質原因のみ修正（`a3cff53`） |
| R-12 | pre_bash_commit_quality.py | 実欠陥 | 修正（`43c94ee`） |
| 6.1 | pre_bash_commit_quality.py / commit_quality_scanner.py | 実欠陥 | 修正（`c80dd10`） |
| 6.2 | hook_common.py | 対応OS明示で決着 | 文書化（`a188cc5`） |
| 6.3 | hook_common.py / output_adapter.py / harness.py / subprocess_utils.py | 実欠陥（デッドコード） | 削除（`a4b0c8f`） |
| 6.4 | hooks.json 配布 | 検証手段の話・欠陥ではない | 無変更 |
| §8 | Claude marketplace | 既に解消（確認済み） | 無変更 |
| 隣接(a) | planner.md / architect.md | 実欠陥 | 修正（`a3cff53`） |
| 隣接(b) | config_protection.py PROTECTED_FILES | 実欠陥 | 修正（`bc52ab4`） |
| 隣接(c) | config_protection.py docstring | 実欠陥（記述乖離） | 修正（`bc52ab4`） |
| 隣接(d) | bluecore-helpers.sh doc | 実欠陥（記述乖離） | 修正（`e474a0b`） |

---

## 4. 検証

全 7 フェーズで以下を実行し、警告なしで成功したことを確認済み:

```bash
source .venv/bin/activate
python3 -m pytest -q                                  # 1321 passed
ruff check plugins/bluecore/src                        # All checks passed
cd plugins/bluecore && python3 -m pytest -q --cov       # 100% coverage
python3 -m bluecore.ci.validate_{hooks,agents,skills,commands}  # 全通過
```

実機再現（launcher.py 経由のサブプロセス実行）で以下を確認:
- malformed JSON + `git commit` 含有 → exit 2（従来 exit 0）
- `pyproject.toml` への lint 設定編集（見出し有り/無し両方）→ exit 2、version bump のみ → exit 0（従来どちらも exit 0）
- `git.exe commit` / `/usr/bin/git commit` → 検出（従来 `git.exe` のみ未検出）
- secret を含む knowledge の `learn` → `show` で `[REDACTED]`（従来平文保存）
