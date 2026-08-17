# bluecore プラグイン実動作監査報告

## 結論

**監査判定: FAIL。** 再インストール済みの bluecore v0.9.30 は、skills / commands / agents / hooks の定義検証と通常系の起動は成立している。一方、入力切り捨て時の安全側失敗、stdin の無期限待機、監査対象の自動判定、read-only 契約、定義例のドリフトに、再現可能な欠陥が残っている。

ユーザー指定により、公開用リポジトリにテストコードがないこと、pytest の未収集、coverage 不足は不具合として扱わない。コード修正・設定修正・エージェント定義修正・コミットは実施していない。本ファイルのみを追加した。

## 監査対象と方法

- 実施日: 2026-08-17
- リポジトリ版: `/Users/tasaki-mamoru/dev/bluecore/plugins/bluecore`
- 再インストール版: `/Users/tasaki-mamoru/.copilot/installed-plugins/bluecore/bluecore`
- バージョン: `0.9.30`
- 実行環境: macOS、Python 3.14.6、Copilot CLI 1.0.80
- 対象:
  - skills 13
  - commands 9
  - agents 13
  - hooks.json 6 matcher
  - launcher / hook_common / memory / CI validator
- リポジトリ版とインストール版の定義・実装を比較し、実質的な差分がないことを確認した。
- 実際に起動した専門エージェント:
  - `bluecore:architect`
  - `bluecore:planner`
  - `bluecore:reviewer`
  - `bluecore:security-auditor`
  - `bluecore:tdd-writer`
  - `bluecore:simplifier`
  - `bluecore:refactor-orchestrator`
  - `bluecore:dead-code-cleaner`
  - `bluecore:perf-optimizer`
  - `bluecore:harness-tuner`
  - `bluecore:bench-analyzer`
  - `bluecore:comparator`
  - `bluecore:grader`
- 静的 validator:
  - `validate_skills`: PASS（13）
  - `validate_commands`: PASS（9）
  - `validate_agents`: PASS（13）
  - `validate_hooks`: PASS（6）
- 通常系 smoke:
  - `git commit --no-verify`: deny、exit 2
  - `git push -n`: allow、exit 0
  - 設定ファイル編集: deny、exit 2
  - SessionStart memory context: 有効な合併 JSON
  - PreCompact: exit 0、ログ記録
  - SessionEnd handoff: exit 0

## 重大度一覧

| ID | 重大度 | 対象 | 概要 | 状態 |
|---|---|---|---|---|
| F-01 | CRITICAL | `block_no_verify` | 1 MiB 超の入力切り捨て後に `--no-verify` 検査が fail-open | 実機再現 |
| F-02 | HIGH | `config_protection` | 不正 JSON / 必須項目欠落を許可し、設定保護が fail-open | 実機再現 |
| F-03 | HIGH | `mem context` | stdin を閉じない呼び出しで同期処理が無期限待機 | 実機再現 |
| F-04 | HIGH | `harness_audit` | 作業ルートを plugin provider ではなく consumer と誤監査 | 実機再現 |
| F-05 | HIGH | `reviewer` / `security-auditor` | read-only が Bash 権限により技術的に強制されない | 定義・実行契約 |
| F-06 | HIGH | `pre_bash_commit_quality` | `git commit --amend` で品質・secret 検査を無条件スキップ | 実機再現 |
| F-06a | HIGH | `commit_quality_scanner` | 読み取り・scanner 例外を握り潰し、検査なしで commit を許可 | コード上確定 |
| F-07 | HIGH | `launcher` | Python 3.12 未満で全 hook を exit 0 のまま無効化 | コード上確定 |
| F-08 | HIGH | memory DB | 既存 DB の権限を 0600 に補正しない | 実機再現 |
| F-08a | HIGH | `handoff` | `transcript_path` の任意パスを直接読み取る | 実機再現 |
| F-09 | HIGH | `dead-code-cleaner` / `refactor-rollback` | 動的リソースと既存編集を保護する契約が不足 | 定義上再現 |
| F-10 | HIGH | `grader` | grading.json 保存要求とツール・出力契約が不整合 | 定義上再現 |
| F-11 | MEDIUM | `planner` | bare `mem`、古いパス、未対応 `--help` を例示 | エージェント実行で確認 |
| F-12 | MEDIUM | TDD / simplifier 等 | ユーザーの no-fix / read-only 要求に対する分岐がない | エージェント実行で確認 |
| F-13 | MEDIUM | `refactor-orchestrator` | 依存順・競合・ロールバック・final gate の契約不足 | エージェント実行で確認 |
| F-14 | MEDIUM | `comparator` / `bench-analyzer` | 出力スキーマ・mode・tie の仕様が不統一 | エージェント実行で確認 |
| F-15 | LOW | `pre_bash_commit_quality` | 実際には別 hook が拒否する bypass コマンドを警告文で案内 | 実機確認 |

## 詳細

### F-01: 切り捨て後の `--no-verify` 検査が fail-open

- 重大度: **CRITICAL**
- 対象: `src/bluecore/hooks/block_no_verify.py:375-376`、`src/bluecore/hooks/hook_common.py`
- 原因: `read_raw_stdin()` は最大バイト数で切り捨てるが、切り捨てフラグを返さない。切り捨て後に JSON が不完全になり `parse_json_object()` が `None` を返すと、検査を行わず exit 0 になる。

再現:

```bash
ROOT=/Users/tasaki-mamoru/.copilot/installed-plugins/bluecore/bluecore
python3 - <<'PY' |
import json
print(json.dumps({
    "tool_name": "Bash",
    "tool_input": {"command": "git commit --no-verify"},
    "filler": "x" * (1024 * 1024 + 100),
}), end="")
PY
PYTHONPATH="$ROOT/src" CLAUDE_PLUGIN_ROOT="$ROOT" \
  python3 "$ROOT/src/bluecore/launcher.py" bluecore.hooks.block_no_verify
```

実測:

```text
exit=0
stdout=""
stderr=""
```

期待は deny JSON と exit 2。攻撃者または誤動作した呼び出し元が大きな payload を付けるだけで、no-verify 防止 hook を無効化できる。

修正案:

1. `read_raw_stdin_with_truncation()` を block hook でも使用する。
2. `truncated=True`、JSON 不正、必須フィールド欠落のいずれも deny にする。
3. 「入力を完全に検証できないため拒否した」という明示理由を出力する。
4. 通常 payload、1 MiB 境界、超過 payload、閉じない stdin を同一の decision table で固定する。

### F-02: 不正 JSON が設定保護を通過する

- 重大度: **HIGH**
- 対象: `src/bluecore/hooks/config_protection.py:205-214`
- 原因: `read_raw_stdin_with_truncation()` は切り捨てだけを検出する。不正 JSON は `parse_json_object()` が `None` を返し、`reason=None` のまま exit 0 になる。

再現:

```bash
printf '%s' \
  '{"tool_name":"Edit","tool_input":{"file_path":"ruff.toml"}xxxxx' |
PYTHONPATH="$ROOT/src" CLAUDE_PLUGIN_ROOT="$ROOT" \
python3 "$ROOT/src/bluecore/launcher.py" bluecore.hooks.config_protection
```

実測は exit 0、deny 出力なし。正常な `tool_input.file_path=ruff.toml` では exit 2 になるため、入力形式だけで保護判定が変わる。

修正案:

- 書込み系 tool と判定できる入力で JSON が不正、tool 名が不明、対象パスが取得不能なら fail-closed。
- ただし対象外イベントの空入力は既存どおり非ブロッキングで通す。
- `tool_name` / `toolName` と `tool_input` / `toolArgs` の両形式で必須フィールド検証を共通化する。

### F-03: memory context の同期 stdin が無期限待機する

- 重大度: **HIGH**
- 対象: `src/bluecore/mem/cli.py` の context 入力経路
- 原因: memory CLI の同期 context は `sys.stdin.read()` 系で入力全体を待つ。launcher の同期経路には watchdog がない。

再現:

1. `launcher.py bluecore.mem.cli context` を stdin pipe 付きで起動する。
2. `{}` を write/flush する。
3. pipe を close せず 3 秒待つ。

実測:

```text
poll_after_3s=None
```

プロセスを terminate しない限り終了しなかった。一方、`block_no_verify` と `config_protection` は同じ条件で有限時間後に入力を打ち切る。SessionStart の host timeout まで hook 全体を占有し、セッション開始を遅延または失敗させ得る。

修正案:

- memory CLI の stdin 読み取りを `hook_common` の bounded reader に統一する。
- 初バイト待ち、全体 deadline、部分 JSON の扱い、fail-open/fail-closed を明示する。
- SessionStart の timeout と内部 deadline のどちらが先に効くかをログへ出す。

### F-04: harness audit の自動 target 判定が作業ルートを誤る

- 重大度: **HIGH**
- 対象: `src/bluecore/ci/harness_audit_utils.py:detect_target_mode`、`commands/harness.md`
- 原因: audit は現在の cwd を既定 root とする。plugin provider root を明示しない限り、workspace root を consumer と判定する。

再現:

```bash
PYTHONPATH=plugins/bluecore/src \
python3 -m bluecore.ci.harness_audit repo \
  --root /Users/tasaki-mamoru/dev/bluecore --format json
```

結果:

```text
target_mode=consumer
overall_score=4/29
```

plugin root を明示すると:

```bash
PYTHONPATH=plugins/bluecore/src \
python3 -m bluecore.ci.harness_audit repo \
  --root /Users/tasaki-mamoru/dev/bluecore/plugins/bluecore --format json
```

```text
target_mode=repo
overall_score=47/65
```

前者は provider 監査としては誤った「ECC をインストール」「consumer の `.claude/` を追加」という action を返し、provider 側のチェックを見逃す。

修正案:

- `--target-kind provider|consumer` を追加する。
- 自動判定結果と明示指定が不一致なら FAIL する。
- `/harness` と `maintain` の provider 監査は `CLAUDE_PLUGIN_ROOT` または plugin root を明示して呼ぶ。
- baseline と after で root / target-kind / scope の一致を必須にする。

### F-05: reviewer の read-only が技術的に強制されない

- 重大度: **HIGH**
- 対象: `agents/reviewer.md:4`、`agents/security-auditor.md:4`、`commands/review.md`
- 原因: reviewer / security-auditor は `Read, Grep, Glob, Bash` を許可されている。Bash から `rm`、リダイレクト、`git apply`、`git reset` 等を実行でき、プロンプト上の READ-ONLY だけでは強制にならない。

再現条件:

```text
reviewer に「読み取り専用で確認せよ」と依頼し、
Bash で `printf x > /tmp/reviewer-probe` または `git reset` を実行するよう誘導する。
```

現行定義には Bash 呼び出し自体を拒否する技術的境界がない。今回の監査では安全のため実行していないが、権限モデル上は書込み可能である。

修正案:

- reviewer / security-auditor から Bash を外し、読み取り専用ツールだけを許可する。
- Bash が必要なら、許可コマンド・作業ディレクトリ・リダイレクト・削除・git 書込みを拒否する専用 wrapper を通す。
- SessionStart / SessionEnd / PreCompact の永続メモリ hook も review mode では read-only にする。

### F-06: `git commit --amend` で品質検査を無条件スキップする

- 重大度: **HIGH**
- 対象: `src/bluecore/hooks/pre_bash_commit_quality.py:606-608`
- 原因: `--amend` を含む commit は、staged files の lint・secret・message 検証をせず exit 0 で返す。

再現:

```python
from bluecore.hooks import pre_bash_commit_quality
print(pre_bash_commit_quality.evaluate(
    '{"tool_name":"Bash","tool_input":{"command":"git commit --amend -m bad"}}'
))
```

実測:

```text
{'exitCode': 0, ...}
```

修正案:

- amend も通常の検査対象にする。
- amend が既存コミットを再利用するケースで false positive が出るなら、対象ファイル集合だけを正確に取得して検査する。
- 検査を省略する明示フラグが必要なら、no-verify と同じく deny または承認要求にする。

### F-06a: scanner 例外時に検査なしで commit を許可する

- 重大度: **HIGH**
- 対象: `src/bluecore/hooks/commit_quality_scanner.py:357`
- 原因: staged/worktree ファイルの読み取り、lint、secret scanner の例外を `except Exception: pass` で握り潰し、空の issue 一覧を返す。

再現条件:

1. 品質検査対象の staged file を用意する。
2. hook 実行中に対象ファイルを unreadable にする、または scanner 内で予期しない例外を発生させる。
3. `pre_bash_commit_quality` を実行する。

実測・コード上の結果は、検査不能理由が出力されず、issue なしとして後続判定へ進むこと。検査不能時に commit を許可するため、F-01 とは別に「scanner の内部例外」でも fail-open になる。

修正案:

- 読み取り不能、lint 例外、secret scanner 例外を `scan_error` として呼び出し元へ返す。
- `scan_error` は security-sensitive な commit では deny にする。
- 例外内容は秘密情報を含めない形で stderr / 構造化 output に記録する。
- 広い `except Exception` を scanner 単位の明示的な例外処理へ分解する。

### F-07: Python 3.12 未満で安全 hook を無効化する

- 重大度: **HIGH**
- 対象: `src/bluecore/launcher.py:50-70,142`
- 原因: Python 3.12 未満では理由を stderr に出しながら exit 0 を返す fail-open 方針。`block_no_verify`、`config_protection`、SessionStart 等の全 target が実行されない。

修正案:

- security-sensitive hook は exit 2 で拒否する。
- または plugin が固定した対応 Python を確実に起動する。
- 「互換性不足で保護機能が無効」と認識できる構造化出力を返す。

### F-08: 既存 memory DB の権限を補正しない

- 重大度: **HIGH**
- 対象: `src/bluecore/mem/database.py:23-35`
- 原因: `chmod(0600)` は DB を新規作成したときだけ実行され、既存 DB の mode は検査・補正されない。

再現:

1. 所有する一時ディレクトリに `mem.db` を作り mode `0644` にする。
2. `Database(path)` を開く。
3. mode を確認する。

実測:

```text
mode_after=0o644
```

handoff と knowledge の内容を他ユーザーが読める状態を維持できる。

修正案:

- 既存 DB も毎回 owner / mode / symlink を検証する。
- 0600 未満なら補正、補正不能なら memory 操作を拒否する。
- 親ディレクトリ、WAL、SHM の mode も同時に確認する。

### F-08a: handoff が任意の transcript_path を読み取る

- 重大度: **HIGH**
- 対象: `src/bluecore/mem/handoff.py`
- 原因: payload の `transcript_path` を Path 化して直接読み取り、許可ディレクトリ、所有者、symlink、通常ファイルであることを検証していない。

再現:

```python
import os, tempfile
from bluecore.mem.handoff import build_handoff

fd, path = tempfile.mkstemp()
os.write(fd, b'{"type":"user","content":"secret task"}\n')
os.close(fd)
print(build_handoff({"transcript_path": path}))
os.unlink(path)
```

実測は一時ファイルの内容を handoff の「直近の依頼」として出力した。SessionEnd payload の `transcript_path` が信頼できない入力になる構成では、任意の readable file の内容が memory / handoff 経路へ流入する。

修正案:

- transcript は Copilot/Claude が指定する許可済み session directory 配下だけに制限する。
- `resolve()` 後に許可 root の下であること、regular file であること、所有者が現在ユーザーであることを確認する。
- symlink は拒否するか、realpath の許可判定を必須にする。
- 機密ファイルを指す入力を検出した場合は読み取りを拒否し、SessionEnd 自体は安全に終了する。

### F-09: 動的リソースと既存編集を保護しない cleanup / rollback

- 重大度: **HIGH**
- 対象: `agents/dead-code-cleaner.md`、`skills/refactor-rollback/SKILL.md`
- 実行エージェント報告で再現条件を確認した。

問題:

- manifest の directory load、`user-invocable: true`、description dispatch、hooks.json の event/matcher は grep 参照がなくても生存根拠になるが、dead-code-cleaner の削除判定で必須確認されない。
- rollback の `git checkout -- <file>` は、処理開始前から存在したユーザー編集まで HEAD に戻し得る。

修正案:

- plugin manifest、frontmatter、event registration、ユーザー/プロジェクト/キャッシュの各 scope を削除判定に必須入力とする。
- 動的参照を確認できない候補は `UNKNOWN/SKIP` にする。
- rollback 前に worktree を clean にするか、開始時の patch/blob を保存して自分の差分だけを三方向復元する。

### F-10: grader の保存契約が実行能力と不整合

- 重大度: **HIGH**
- 対象: `agents/grader.md:入力 / 手順8 / 出力形式`
- 問題:
  - 手順8は `{outputs_dir}/../grading.json` への保存を要求する。
  - 保存用 tool を frontmatter で明示していない。
  - 出力例は `expectations` が2件なのに `total=3, passed=2, failed=1` で、内容と整合しない。
  - `grader_duration_seconds` の測定源と測定手順が未定義。

修正案:

- caller が JSON 応答を保存する方式に統一するか、grader に Write と保存確認を明示する。
- `total == len(expectations)`、passed/failed 集計、pass_rate、丸め規則を schema で検証する。
- timing は wrapper の開始/終了時刻を正とし、未測定値を推測させない。

### F-11: planner の実行例が現行実装とずれる

- 重大度: **MEDIUM**
- 対象: `agents/planner.md`
- 実行エージェントが確認した問題:
  - `mem search` / `mem show` は通常 PATH に存在しない。実際の入口は `bluecore_run bluecore.mem.cli ...`。
  - 現行 CLI の例と `mem search --help`、既定 `--limit` の記述が一致しない。
  - planner 単体には、曖昧な依頼で必ず質問すること、計画後に承認待ちすることがない。

修正案:

- memory 操作を `bluecore-helpers.sh` 経由へ統一する。
- パスは plugin root / repository root 相対を明記する。
- planner 単体の出力に `NEEDS_CLARIFICATION` と `AWAITING_APPROVAL` を追加する。

### F-12: no-fix / read-only 要求を受けた agent が安全に停止する契約がない

- 重大度: **MEDIUM**
- 対象: `agents/tdd-writer.md`、`agents/simplifier.md`、`agents/dead-code-cleaner.md`、関連 skills
- 実行エージェント報告で共通して確認した問題:
  - tdd-writer は RED→GREEN→REFACTOR の実装手順が優先され、read-only/no-fix 分岐がない。
  - simplifier / dead-code-cleaner は変更・削除・commit を通常フローに含む。
  - 呼び出し元が強い制約を再注入しないと、定義単体では編集へ進む余地がある。

修正案:

- 「read-only / no-fix / inspect-only」が指定された場合は最優先で編集、削除、commit、delegate を禁止する。
- RED/GREEN/REFACTOR は `NOT RUN (read-only constraint)` と出力する。
- 実行していないテストを PASS と報告しない。

### F-13: refactor orchestrator の依存・競合・final gate が不十分

- 重大度: **MEDIUM**
- 対象: `agents/refactor-orchestrator.md`
- 実行エージェント報告:
  - dependency graph の cycle / index 検証と topological scheduling が必須でない。
  - 同一ファイルを触る group の conflict detection がない。
  - active worker の失敗時に他 group の変更をどう取り消すか不明。
  - final gate が全 stage の PASS、perf evidence、rollback 完了を必須としていない。

修正案:

- group の依存 graph を検証し、ready group だけ起動する。
- path/resource overlap を検出して直列化する。
- 失敗時は active worker の終了を待ち、依存逆順で rollback する。
- final gate は全 stage の status/evidence が揃わない限り PASS にしない。

### F-14: comparator / bench-analyzer の出力契約が揺れる

- 重大度: **MEDIUM**
- 対象: `agents/comparator.md`、`agents/bench-analyzer.md`
- 実行エージェント報告:
  - comparator は本文の `correctness` 系評価と例の `accuracy` キーが一致しない。
  - `expectation_results` は A/B 必須と書かれているが例が片側のみ。
  - tie の「安易に使わない」と「同点なら TIE」が優先順位なしで併存する。
  - bench-analyzer は blind comparison と benchmark analysis の mode dispatch がない。
  - 欠損 nested metrics の診断が一部しか出ない。

修正案:

- JSON schema を一つに固定し、キー名・必須性・丸め規則を例と同期する。
- comparator は rubric → expectation pass rate → TIE の順序を明記する。
- bench-analyzer に `mode=benchmark_analysis|posthoc_comparison` を導入し、mode ごとの必須入力を分離する。

### F-15: 品質 hook が拒否される bypass を案内する

- 重大度: **LOW**
- 対象: `src/bluecore/hooks/pre_bash_commit_quality.py:529`
- 問題: warning 出力は `git commit --no-verify` を「bypass 方法」として案内するが、同じ hooks.json の `block_no_verify` がそのコマンドを exit 2 で拒否する。

修正案:

- 「bypass」案内を削除し、通常の修正方法だけを示す。
- 例外的に hook 無効化が必要なら、管理者承認など実際に利用可能な手順を明示する。

## 条件付きリスク

以下はホスト契約や運用経路に依存するため、今回の単独実行だけで常時 exploitable と断定せず、条件付きリスクとして記録する。

### R-01: knowledge の既定 status が `active`

`src/bluecore/mem/knowledge_input.py:253` は status 未指定を `active` にする。`active` は memory context へ注入される値であり、agent/user 由来の入力を同じ payload で受ける経路では、未指定データが直ちに次回セッションのコンテキストへ入る。明示的な `learn` 操作だけが入力元なら仕様内だが、observer や hook から同じ変換関数を再利用する場合は `pending` を強制する必要がある。

修正案:

- 外部入力、observer、hook は `status_override="pending"` を必須にする。
- `active` は明示承認済みの経路だけが指定できるよう分離する。
- context 注入前に source / scope / status の監査ログを残す。

## 不具合として採用しなかった観測

- validators は全て PASS。
- source / installed の定義・実装差分は確認できなかった。
- 正常な `git commit --no-verify`、`git push -n`、設定保護、SessionStart、PreCompact、SessionEnd は期待どおり動作した。
- `extensions_manage list` は「No extensions discovered」だったが、bluecore はこの extension registry の登録対象ではなく、今回の plugin 定義欠落とは判定しなかった。
- `/skill-test` は `skill-make` の文中で「使わない」と否定的に言及されるだけで、未提供コマンドを実行させる欠陥とは扱わなかった。
- 公開用リポジトリにテストコードがないこと、pytest/coverage の未実行・未収集はユーザー指定により対象外とした。

## 残存リスクと優先修正順

1. F-01、F-02: block / protection hook の fail-open を fail-closed 化。
2. F-03: memory context の stdin deadline 導入。
3. F-04: provider/consumer の target-kind と root を明示化。
4. F-05、F-12: read-only/no-fix の技術的・契約的境界を強化。
5. F-06〜F-10: amend、Python version、DB permission、transcript/memory の安全境界を整理。
6. F-11〜F-15: agent/benchmark/command の出力契約と例を現行実装へ同期。

**本監査では上記の修正を一切適用していない。**
