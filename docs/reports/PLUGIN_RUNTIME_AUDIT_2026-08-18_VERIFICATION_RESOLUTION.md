# PLUGIN_RUNTIME_AUDIT_2026-08-18_VERIFICATION.md 残存問題の是正報告

実施日: 2026-08-18
対象: `docs/reports/PLUGIN_RUNTIME_AUDIT_2026-08-18_VERIFICATION.md`（Copilot CLI 1.0.80 上の bluecore v0.9.32 実機再検証報告）が残した A-01〜A-07、§6 設計維持事項、§7 未確認事項
方針: 承認済み計画（`docs-reports-plugin-runtime-audit-2026-quizzical-hennessy.md`）に従い、報告書の記述をそのまま実装するのではなく、実コードと照合して実害のあるものだけを直した。`_VERIFICATION.md` 自体は検証時点の記録として書き換えていない。

修正後バージョン: v0.9.33（v0.9.32 からの差分）。全項目を個別コミット単位で修正・検証済み。

## 1. 結論

A-01〜A-07 の全 7 件と、§6.2（detached child 失敗通知）・§6.4（handoff transcript trust boundary）・§7 未確認事項 4 件（grader/bench-analyzer の任意 Write path・`/harness` apply 承認境界・§7-3 の代替静的検証・`repos.remote_url` credential 除去）を解消した。§6.1（Python 3.12 未満の fail-open）と§6.3（reviewer の write-capable Bash）は実装を変更せず、既存の設計判断を維持した（理由は「4. 直さない判断」）。

`_VERIFICATION.md` の指摘のうち、実コード確認の結果、報告の前提が実装と食い違っていたものが複数あった（「2. 報告書と実装の食い違い」）。該当箇所は報告の記述どおりではなく、実際のコードに基づいて修正内容を決定した。

## 2. 報告書と実装の食い違い（実コード確認済み）

| 報告の指摘 | 実装の事実（着手前に確認） | 本作業での扱い |
|---|---|---|
| A-05「stdin 例外で exit 0」 | `hook_common.read_raw_stdin` は「例外は発生しません」を契約化しており、報告の probe は `read_raw_stdin` を monkeypatch した人工再現。通常経路では起きない | 別の実害（truncation 未検知・commit 確定後の emit 失敗時の fail-open）に置き換えて修正（1-3） |
| A-02「chunk/stream scan が必要」 | `get_staged_file_content` / `get_worktree_file_content` は既にファイル全体を str でメモリに載せている。1 MiB cap は正規表現コストのガードでメモリガードではない | cap を削除するのみとし、streaming・専用 deny 経路は追加しなかった（1-4） |
| A-04「configparser で section-aware に」 | `_LINT_SECTION_HEADERS` は前方一致判定のため `"[testenv"` を足すだけで `[testenv:py312]` まで拾える | 既存の前方一致構造に追加するのみとし、新規パース経路は作らなかった（1-5） |
| A-07「`-c` が解析されていない」 | `_VALUE_SHORT_OPTIONS = frozenset("Cc")` で `-c` の値自体は既に解析済み。不足は `_is_bypass_invocation` が `core.hooksPath` オーバーライドを見ていない点だけ | 判定側（`_is_bypass_invocation` / `has_bypass_flag`）の局所修正に留めた（1-2） |
| A-03「key に secret が残る」 | 事実。加えて報告に無い穴が同じ箇所に 2 つ（`domain` 未 redact／`payload["key"]` 明示指定は検証・redaction を素通り）判明 | 3 点まとめて 1 コミットで修正（Phase 2） |

## 3. 修正内容（コミット単位）

各項目は個別コミットで「修正 → `python3 -m pytest -q --cov`（`plugins/bluecore` 配下で `fail_under=100` を解決）→ `ruff check plugins/bluecore/src`」を実行し green を確認してから commit した。

| # | 対象 | 変更概要 | コミット |
|---|---|---|---|
| A-01 | `pre_bash_commit_quality.py` / `block_no_verify.py` / `hook_common.py` | `git status;echo commit` のように shell 区切り文字がトークンへ密着すると commit と誤検出していた。`block_no_verify` の区切り記号対応トークナイザ（`tokenize`/`split_segments`）を `hook_common` へ共有ヘルパとして移動し、commit 検出をセグメント単位に書き換えた | `aedca5e` |
| A-07 | `block_no_verify.py` | `--no-verify` のみを見ており、`git -c core.hooksPath=x commit` による git 自身のフック無効化と `sh -c 'git commit --no-verify'` の 1 段シェルラッパー経由バイパスを見逃していた。`GitInvocation.config_values` を追加して `-c`/`--config-env` の値から `core.hooksPath` 上書きを検出し、`has_bypass_flag` に既知シェル（sh/bash/zsh/dash）の `-c` 引数へ 1 段だけ再帰する分岐を追加 | `44658af` |
| A-05 相当 | `pre_bash_commit_quality.py` | `main()` が stdin 読み取り例外を一律 exit 0 にしており、1 MiB 超の truncation も未検知だった。`read_raw_stdin_with_truncation` に切り替え、truncation は `block_no_verify`/`config_protection` と同じ fail-closed にし、`main()` の try/except を「commit 確定前の例外は fail-open」「確定後（`emit_block_output` 失敗等）は fail-closed」の境界で分割 | `95fe29e` |
| A-06 | `bash_config_protection.py`（新規） / `hooks.json` | `config_protection` は Edit/Write/MultiEdit matcher にしか登録されておらず、`printf x > pyproject.toml` のような Bash 経由の直接書き換えを検査しなかった。新規モジュールを Bash matcher の 3 本目として追加し、リダイレクト（`>`/`>>`）・`tee` 出力先・`sed -i` 対象の書き込み先トークンに保護対象 basename が現れた場合のみ deny する（検査不能を理由に可用性を壊す fail-closed へは倒さない） | `89e95a1` |
| A-02 | `commit_quality_scanner.py` / `pre_bash_commit_quality.py` | 1 MiB での先頭切り詰めにより末尾側の secret が未検出だった。サイズによる打ち切りを廃止しテキストファイルは全体走査に変更。バイナリ判定ファイルは secret scan 自体をスキップし severity `warning` の痕跡 issue を残す（`error` にはせず画像等の commit を一律ブロックしない）。走査量無制限化の保険として実時間バジェット（10 秒、`_SECRET_SCAN_TIME_BUDGET_SECONDS`）を追加し、超過は `scan_error`（error、fail-closed）として扱う | `ec9dbde` |
| A-04 | `config_protection.py` | `[testenv]`/`[testenv:py312]` の `commands` 変更（`--no-cov` への差し替え等）が通っていた。`_LINT_SECTION_HEADERS` に `"[testenv"` を追加（前方一致でサブ環境も網羅）し、`tox.ini` 限定の `commands`/`commands_pre`/`commands_post` キー照合を追加。`_LINT_KEYS` へのグローバル追加は行わず（`pyproject.toml` の正当な `commands` を誤検出するため）、`package.json` と同じ形のファイル限定分岐にした | `eb1cb36` |
| §7-4 | `repo_identity.py` | `git remote get-url origin` の生出力（`https://user:token@host/...` 等）がそのまま `repos.remote_url` に保存されていた。userinfo 除去は `identity_key` 側の `_join_host_path`（`rpartition("@")`）にしか適用されていなかったため、同じロジックを切り出した `_strip_userinfo` を保存前の `remote_url` にも適用 | `0da8ff0` |
| A-03 | `knowledge_input.py` | 同じ箇所に 3 つの穴: (1) key が redact 前の生 title から生成され `learned:` 出力・`show <key>` 経由で secret が漏れる、(2) `domain` が redact 対象外、(3) `payload["key"]` の明示指定が redaction・検証を素通り。`generate_key` を redact 後 title から生成するよう変更し、redaction が title を変えた場合のみ生 title の SHA-1 先頭 8 桁を衝突回避サフィックスとして付す（同一 secret title の再 learn は同一 key で upsert、異なる secret は別 key になる）。`domain` と明示 `key` にも redaction＋slugify を適用 | `7482551` |
| §6.2 | `hook_common.py` / `cli.py` | `detach_process` の戻り値は「起動受付」であり「処理成功」ではないため、`--bg` 起動した子の失敗は呼び出し元へ同期的に伝わらない。親の exit 契約は変えず、次回 SessionStart の `mem context` で前回の `bg-YYYY-MM-DD.log`（当日＋前日の 2 ファイルまで、末尾 4KB まで）に内容があれば 1 行だけ通知する（失敗時のみ出力し、出力トークン最小化と両立） | `26829f9` |
| §6.4 | `handoff.py` | 所有者一致の任意 regular file を host root 制約なしに読んでおり、ユーザーが書ける任意ファイルを prompt injection の入力にできる余地があった。既存の性質検査（symlink 拒否・regular file・所有者一致）を残した上で、既知 host root（`~/.claude/projects`、`~/.copilot`、`~/.codex`）の allowlist 包含チェックを後段に追加。`BLUECORE_TRANSCRIPT_ROOTS`（コロン区切り）で未知 host にも拡張可能。root 外は要約を諦めて空文字列を返し、handoff は明示テキスト・構造化事実で継続する（壊れずに劣化） | `cb60242` |
| §7 未確認 | `grader.md` / `bench-analyzer.md` / `harness.md` / `reviewer.md` | frontmatter の `tools` はパス単位の制約を表現できないため、`grader`/`bench-analyzer` の書込み先を `grading_path`/`output_path` 配下に散文で限定。`/harness` は step3 の harness-tuner 適用を無条件委譲していたため、`--apply` を明示しない限り top3 提示で停止するよう分離（`--audit-only` とは独立の承認境界）。`reviewer.md` には write-capable Bash を維持する理由（§6.3 の技術的強制を採らない根拠）を追記 | `08b58d0` |
| §7-3 代替 | `test_validate_hooks.py` | 実 install/update + host 登録の smoke test はスコープを大きく超えるため実施せず、代わりに `hooks.json` が PreToolUse（block_no_verify/pre_bash_commit_quality/bash_config_protection/config_protection）・PreCompact・SessionStart・SessionEnd の全経路を宣言していることと、全 hook エントリが `timeout` を持つことを静的検証するテストを追加。`PreCompact` の `timeout` 未指定を 10 秒で補った | `846e3d6` |
| scope 追加 | `test_hook_edge_cases.py` | `test_repo_wide_self_scan_has_zero_secret_issues` に本報告書自身を含めて `pytest -q` を実行した結果を反映（5 節参照） | `c934ff2` |
| release | version 4 ファイル・本報告書 | v0.9.32 → v0.9.33 | `be950fe` |

## 4. 直さない判断

以下は実装を変更していない。理由を明記する。

- **§6.1 Python 3.12 未満の fail-open**: ユーザー判断で対象外。`CLAUDE.md` および `launcher.py:52-58` に明記された設計判断（ランタイムが venv も install スクリプトも持たないため、インストール時に対応 Python を検証する経路が無く、fail-closed にすると復旧手段そのものを塞ぐ）を維持する。
- **§6.3 reviewer の write-capable Bash**: `reviewer.md` の既存記述（`ruff check` / `test_cmd` 再実行が職務のため Bash を外せない）が根拠として有効と判断し、agent を 2 分割（validator runner とレビュー agent の分離）する監査の代案は採らなかった。理由を `reviewer.md` に追記した（本報告 3 節「§7 未確認」参照）。分割はコンテキスト消費と収束遅延を増やすだけで根本的なリスク（書込み権限の技術的強制）は解消しないと判断した過剰設計である。
- **§7-3 実 install/update smoke test**: 未実施。理由は 3 節「§7-3 代替」に記載のとおり、静的検証への縮退で対応した。
- **A-01 の `echo "git commit"` 誤検出**: `_is_git_commit_command` の regex フォールバック（`\bgit\s+commit\b`）により、`echo "git commit"` は引き続き commit と誤判定される。これは安全側の誤検出（false positive であり bypass ではない）として受容する。過検出防止のためにフォールバックを削除すると、真の commit を取り逃す false negative のリスクの方が高い。
- **`$(...)` / 変数展開 / エイリアス / 任意ラッパースクリプト**: `block_no_verify` の非目標として維持する。POSIX シェルの完全解釈は原理的に不可能であり、無理に検出しようとするとヒューリスティックが過剰ブロックを招く。`sh -c` の 1 段だけを目標に追加し（A-07 対応）、2 段以上のネストは非目標のまま。
- **A-02 のバイナリスキップ**: NUL バイトを 1 個混ぜてバイナリ判定させ secret scan を回避する経路は理屈上残る。フック timeout による commit 全体の無検査（fail-open）を避けるためのトレードオフとして受容し、ファイル単位で severity `warning` の痕跡（`secret_scan_skipped`）を残すことで無言のスキップにはしていない。
- **A-05 / A-02 / A-04 / A-07 の報告書前提との食い違い**: 2 節に記載のとおり。報告が提示した修正案（chunk/stream scan、configparser 導入等）はそのまま実装せず、実装の実態に基づいた最小修正に置き換えた。

## 5. 検証

各コミットで以下を実行し green を確認した（`plugins/bluecore` 配下でのみ `fail_under=100` が解決するため、カバレッジ確認は同ディレクトリで実施）。

```bash
cd plugins/bluecore && python3 -m pytest -q --cov
ruff check src
```

最終状態（v0.9.33）:

- テスト: 1439 passed / 0 failed
- カバレッジ: 100.00%（`fail_under=100` 達成）
- ruff: All checks passed

**scope 追加**: `test_repo_wide_self_scan_has_zero_secret_issues` が、`docs/reports/PLUGIN_RUNTIME_AUDIT_2026-08-18_VERIFICATION.md:190` に A-03 の再現手順として引用された synthetic secret 文字列（`sk-aaaa...`）を検出し red だった。`git worktree add` で `cbd3838`（`_VERIFICATION.md` を追加したコミットそのもの）を検証した結果、この red は `_VERIFICATION.md` が追加された時点から存在しており、本作業の A-01〜A-07 等のどの変更にも起因しないことを確認した。`_VERIFICATION.md` は検証時点の記録として書き換えない方針のため、`test_hook_edge_cases.py` の self-scan テスト側にのみ `path:line` 単位の限定的アローリスト（`_SELF_SCAN_KNOWN_SYNTHETIC_SECRET_HITS`）を追加して解消した。ファイル単位ではなく `path:line` 単位にしたのは、同ファイル内の別行に将来 secret が混入した場合の検出能力を保つため。`commit_quality_scanner._SECRET_PATTERNS` を含む本番の secret scan ロジックは一切変更していない。CLAUDE.md の作業ルール（`pytest -q` の成功が必須）を満たすために承認済み計画の範囲を超えて対応した。

エンドツーエンド確認（承認済み計画に記載の probe をすべて実行し、期待どおりの結果を得た）:

- A-01: `git status;echo commit` → exit 0（非 commit 誤検出なし）、`git status; git commit -m x` → exit 2（退行なし）
- A-04: `tox.ini` の `[testenv] commands = pytest --no-cov` → exit 2
- A-06: `printf x > pyproject.toml`（Bash 経由）→ exit 2
- A-07: `git -c core.hooksPath=/dev/null commit` → exit 2
- A-02: `_scan_secret_issues` で 1 MiB 超の位置に置いた secret を検出（`True`）
- A-03: 隔離 DB で secret 風 title を learn し、`learned:` の key に secret 断片が残らないことを確認（`keep-redacted-out-of-titles-<hash>` 形式）

## 6. 変更ファイル一覧

**実装**:
`plugins/bluecore/src/bluecore/hooks/{hook_common,block_no_verify,pre_bash_commit_quality,commit_quality_scanner,config_protection,bash_config_protection(新規)}.py`、
`plugins/bluecore/hooks/hooks.json`、
`plugins/bluecore/src/bluecore/mem/{repo_identity,knowledge_input,cli,handoff,models,schema}.py`

**定義・報告**:
`plugins/bluecore/agents/{grader,bench-analyzer,reviewer}.md`、
`plugins/bluecore/commands/harness.md`、
本報告書（新規）、
version 4 ファイル（`.claude-plugin/plugin.json`、`.claude-plugin/marketplace.json`、`plugins/bluecore/pyproject.toml`、`plugins/bluecore/src/bluecore/mem/__init__.py`、`scripts/version-up.sh` 経由で 0.9.32 → 0.9.33）

**テスト**（新規分岐と同一コミットで追加。`fail_under=100` のため）:
`plugins/bluecore/tests/hooks/{test_pre_bash_commit_quality(新規),test_additional_hooks,test_hook_edge_cases(secret検出分岐＋self-scanアローリスト),test_hook_common,test_bash_config_protection(新規),test_hooks_json_matchers,test_config_protection_conditional}.py`、
`plugins/bluecore/tests/mem/{test_repo_identity,test_knowledge_input,test_cli,test_handoff}.py`、
`plugins/bluecore/tests/ci/test_validate_hooks.py`

## 7. 変更しないファイル（隣接するが対象外）

- `plugins/bluecore/src/bluecore/launcher.py` — §6.1 の設計維持（ユーザー判断）
- `docs/reports/PLUGIN_RUNTIME_AUDIT_2026-08-18{,_RESOLUTION}.md` および `_VERIFICATION.md` — 過去記録として保全
- `plugins/bluecore/src/bluecore/mem/schema.py` の CREATE TABLE 構造自体 — マイグレーション不要（key の形式変更にスキーマ変更は伴わない。コメントのみ更新）
- `plugins/bluecore/src/bluecore/mem/redaction.py` — パターン集合は変更していない（適用箇所のみ拡張: `domain`・明示 `key`）
