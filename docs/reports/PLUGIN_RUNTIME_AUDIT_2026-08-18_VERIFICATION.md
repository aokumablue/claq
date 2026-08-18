# bluecore プラグイン実機ランタイム監査 — Copilot 再検証報告

実施日: 2026-08-18  
対象: GitHub Copilot CLI にインストールされた bluecore v0.9.32  
目的: `docs/reports/PLUGIN_RUNTIME_AUDIT_2026-08-18.md` の指摘と、`PLUGIN_RUNTIME_AUDIT_2026-08-18_RESOLUTION.md` が報告した是正内容を、Copilot CLI 上で実際にエージェント・スキル・コマンド・フックを動かして再検証すること。  
変更方針: **プラグイン本体は変更しない。問題、再現方法、影響、修正案だけを記録する。**

## 1. 結論

是正報告に記載された主要な修正のうち、R-07 の基本経路、R-08、R-09、R-10、R-11、R-12、scanner の読み取りエラー処理、subprocess timeout、dead code 整理は、インストール済み plugin を Copilot から実行して解消を確認した。

ただし、**監査報告の問題がすべて解消したとは判定できない**。次の残存問題を実測した。

- commit 品質フックは、commit ではない malformed JSON を exit 0 で通し、標準入力読み取り例外も exit 0 で通す。
- commit 検出器が `git status;echo commit` のような shell 区切り文字がトークンに密着した非 commit コマンドを commit と誤認する。
- secret scan はファイル先頭 1 MiB のみを検査し、1 MiB より後ろに置いた secret を検出しない。
- knowledge の本文・表示用 title は redact されるが、redact 前 title から生成した key に secret が残り、`learned:` 出力と key 参照で漏れる。
- `tox.ini` の `[testenv] commands = pytest --no-cov` 変更を config protection が許可する。
- config protection は Edit/Write 系 matcher のみで、Bash による設定ファイル直接書き換えを保護しない。
- Python 3.12 未満の fail-open、detached child の親 exit 0、reviewer の Bash 権限など、是正報告で設計維持とされた問題は実測上も残っている。

従って最終判定は、**主要修正は動作しているが、全件解消ではなく、残存欠陥と設計上の未解決境界がある**である。

## 2. 実行環境とインストール実体

| 項目 | 実測値 |
|---|---|
| Copilot CLI | 1.0.80 |
| リポジトリ HEAD | `89fa15aee72e1231b877aca17d3d118c028d39d7` |
| リポジトリ版 | v0.9.32 |
| インストール先 | `~/.copilot/installed-plugins/bluecore/bluecore` |
| インストール版 | v0.9.32 |
| 実行 OS | macOS |
| 検証 fixture | `/tmp/bluecore-resolution-audit` |
| ソース・インストール差分 | キャッシュ・bytecode を除き path/content とも一致 |

pytest、coverage、公開テストコードの有無は判定対象外とした。検証は validator の存在確認だけで済ませず、インストール済み plugin の agent、skill、command、hook を実際に起動した結果を主判定に用いた。

## 3. 実際に動かした plugin surface

### 3.1 Agent

インストール済みの 13 agent を no-fix/read-only 条件で起動した。

`architect`、`planner`、`reviewer`、`security-auditor`、`tdd-writer`、`simplifier`、`dead-code-cleaner`、`refactor-orchestrator`、`perf-optimizer`、`harness-tuner`、`bench-analyzer`、`comparator`、`grader`

重点確認結果:

- `bluecore:planner` と `bluecore:architect` は、編集せず計画本文を出力した。
- `bluecore:reviewer` は no-fix 指示下で fixture を変更しなかった。
- `bluecore:security-auditor` は Read/Grep/Glob のみで実行され、変更操作を行わなかった。
- `bluecore:grader` は正常系と混在 PASS/FAIL 系の両方を実行した。
- `bluecore:maintain` はインストール済み root を明示すると全 surface を発見し、root のない consumer fixture では `BLOCKED` を返した。
- `bluecore:refactor-rollback` は空の `tests.group` に対して架空の検証コマンドを生成せず、`verify="NOT_AVAILABLE"` と `manual_review` を出力した。

### 3.2 Command

次の 9 command を no-fix/plan 相当で起動した。

`/bugfix`、`/feat-dev`、`/harness`、`/instinct`、`/plan`、`/refactor`、`/review`、`/skill-gen`、`/test-gen`

fixture、plugin 定義、memory DB は変更されなかった。

### 3.3 Skill

次の 13 skill を起動した。

`adr`、`checkpoint`、`grillme`、`learn`、`loop-audit`、`loop-dev`、`maintain`、`refactor-prep`、`refactor-rollback`、`search`、`secure`、`skill-make`、`skill-tune`

`maintain`、`learn`、`refactor-rollback` については、対象なし、secret、空 verification などの境界条件も確認した。

### 3.4 Hook

`hooks/hooks.json` の 6 経路を実行した。

1. `PreToolUse` — `block_no_verify`
2. `PreToolUse` — `pre_bash_commit_quality`
3. `PreToolUse` — `config_protection`
4. `PreCompact` — `pre_compact`
5. `SessionStart` — memory context
6. `SessionEnd` — detached handoff

Copilot CLI から Bash を実行した際にインストール済み `pre_bash_commit_quality` が実際に deny を返したため、少なくとも PreToolUse の hook 実行経路は Copilot 上で動作している。SessionStart の context 注入、SessionEnd child の診断ログ出力も確認した。

## 4. 既知 R-01〜R-12 の再検証

| 指摘 | 判定 | 実測結果 |
|---|---|---|
| R-01 commit 品質フックの検査不能時 fail-open | **部分解消** | 確定した commit の git failure、scanner exception、repo root 解決失敗、読み取り不能ファイルは exit 2。だが非 commit malformed JSON と I/O exception は exit 0。shell 区切り誤認と 1 MiB scan 境界も残る。 |
| R-02 Python 3.12 未満で保護 hook が exit 0 | **未解決・設計維持** | simulated 3.11.9 で `bluecoreProtectionDisabled` と exit 0。対応 Python 3.12+ 前提を文書化した設計判断。 |
| R-03 `--bg` child failure の親への伝播 | **部分解消** | child 失敗は `~/.bluecore/logs/bg-YYYY-MM-DD.log` に記録される。しかし `--bg nonexistent.module` の親は stdout/stderr なし、exit 0。起動受付と処理成功の区別は呼び出し元に伝わらない。 |
| R-04 knowledge の secret 無検査 | **部分解消** | title/body/source_ref は `[REDACTED]` 化され、pending knowledge は context に入らない。ただし raw title 由来 key に secret が残る。 |
| R-05 handoff transcript の trust boundary | **条件付き未解決・設計維持** | symlink、non-regular file、所有者不一致は拒否。所有者一致の任意 regular file は path allowlist なしで読まれる。host 非依存性質検査を採用する設計判断。 |
| R-06 reviewer read-only の技術的強制 | **部分解消** | security-auditor は Read/Grep/Glob のみ。reviewer は実行時に no-fix を守ったが Bash 権限を保持し、技術的強制ではない。 |
| R-07 config protection の設定ファイル網羅 | **部分解消** | `pyproject.toml`、`setup.cfg`、`package.json` の代表的な lint/config 変更は deny。`tox.ini` の test command 変更は allow。Bash 直接書き換えも別の残存経路。 |
| R-08 grader output contract | **解消確認** | 正常系は `summary.total=2, passed=2, failed=0, pass_rate=1.0`。混在系は `0/2/2/0.0` で、証拠も指定された transcript/output file に紐付けて FAIL を返した。 |
| R-09 maintain の consumer cwd 依存 | **解消確認** | consumer cwd で scope なしの場合は `BLOCKED`。インストール済み root を明示した場合は commands/skills/agents/hooks を発見した。 |
| R-10 refactor-rollback の架空 verify | **解消確認** | 空の `tests.group` に対して `verify="NOT_AVAILABLE"`、`required_action=manual_review`。存在しない test command は生成しない。 |
| R-11 planner の read-only 解釈 | **解消確認** | planner/architect は編集せず計画本文を出力。Bash を持たない agent に実行不能な `bluecore_run` を要求しない定義になった。 |
| R-12 `git.exe commit` 検出漏れ | **解消確認** | `git commit`、`/usr/bin/git commit`、`git.exe commit`、`GIT commit` を検出した。 |

### 4.1 R-01 の解消部分

次の probe はすべて installed plugin の launcher 経由で実行した。

- malformed JSON でも raw command に `git commit` が含まれる場合: exit 2。
- staged file 取得失敗: exit 2、`could not determine staged files`。
- scanner 例外: exit 2、`quality scan failed for a confirmed git commit`。
- `git commit -a` の repo root 解決失敗: exit 2。
- 読み取り不能 file: `scan_error`、severity `error`。
- hook subprocess と `hooks.json` の timeout: 設定済み。

一方、`not-json` のように commit 文字列を含まない malformed JSON は、Bash matcher 全体を止めないという理由で exit 0 のままである。この設計は「commit と無関係な Bash を malformed というだけで止めない」点では一貫しているが、入力を検査できない状態を許可するため、監査報告の fail-closed 要件を全て満たしてはいない。

## 5. 実測で追加発見した未解決問題

### A-01 — shell 区切り文字が密着した非 commit を commit と誤認する

対象: `src/bluecore/hooks/pre_bash_commit_quality.py:_is_git_commit_command`

#### 再現

以下の Python probe は `PYTHONPATH="$HOME/.copilot/installed-plugins/bluecore/bluecore/src"` を設定して実行した。

```python
from bluecore.hooks.pre_bash_commit_quality import _is_git_commit_command

for command in (
    "git status;echo commit",
    "git status&&echo commit",
    "git status||echo commit",
    "git status|echo commit",
):
    print(command, _is_git_commit_command(command))
```

実測結果はいずれも commit 判定 `True` だった。`shlex.split()` が `status;echo` を shell separator として分割しない一方、後続の `commit` を同じ token 列の commit subcommand と解釈するためである。

#### 影響

commit ではない Bash が品質 scanner に送られ、staged file 取得や scanner 実行により不要な deny・遅延が発生する。これは bypass ではなく false positive だが、PreToolUse 全体を対象にする hook では可用性問題になる。

#### 修正案

`block_no_verify` と同じ separator-aware tokenizer を共有し、`&&`、`||`、`;`、`|`、`&`、括弧を token 境界として扱う。shell の完全解釈は行わず、少なくとも separator 密着による誤認だけを防ぐ。

### A-02 — 1 MiB より後ろの secret が検出されない

対象: `src/bluecore/hooks/commit_quality_scanner.py:_scan_secret_issues`

#### 再現

以下の Python probe は `PYTHONPATH="$HOME/.copilot/installed-plugins/bluecore/bluecore/src"` を設定して実行した。

```python
from bluecore.hooks.commit_quality_scanner import (
    _SECRET_SCAN_MAX_BYTES,
    _scan_secret_issues,
)

content = "A" * (_SECRET_SCAN_MAX_BYTES + 100) + "ghp_" + ("Z" * 36)
print(_scan_secret_issues(content, content.split("\n")))
```

実測結果:

```text
limit=1048576 bytes=1048716 issues=[]
```

同じ synthetic token を先頭に置いた場合は GitHub PAT として検出された。

#### 影響

1 MiB を超えるファイルの末尾へ secret を置くと、commit 品質フックの secret scan を回避できる。先頭だけの水増しで全面スキップされる旧問題は改善したが、末尾回避は残っている。

#### 修正案

全体を chunk/stream scan して上限を設けない。メモリ上限が必要な場合は、上限超過を `scan_error` として扱い commit を deny する。単純な先頭切り捨てを成功扱いにしない。

### A-03 — knowledge key が redact 前 title から生成される

対象: `src/bluecore/mem/knowledge_input.py:parse_knowledge_payload`

#### 再現

隔離 DB で secret 風 title を `learn` し、`show` と stdout を確認した。

```text
learned: keep-sk-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-out-of-titles
title: Keep [REDACTED] out of titles
body: Do not persist [REDACTED] or [REDACTED]
```

本文、title、body、source_ref の表示は redact されるが、`learned:` に出た key と key の DB 識別子には title 中の secret 断片が残った。

#### 影響

redaction の修正で DB 本文からは消えた secret が、key、learn の標準出力、`show <key>` の入力履歴、ログや次の agent prompt へ残る。是正報告は「redact 後 title では key collision が起きる」ことを理由に raw title を使っているが、秘密を key に残す別の情報漏えいを導入している。

#### 修正案

key は secret-free な正規化 title から生成するだけでなく、衝突回避用の安定 hash を使う。raw title を出力・DB key・ログに使用しない。既存 key の migration と旧ログの扱いも運用手順に含める。

### A-04 — `tox.ini` の test command 変更が config protection を通る

対象: `src/bluecore/hooks/config_protection.py`

#### 再現

`PLUGIN="$HOME/.copilot/installed-plugins/bluecore/bluecore"` を設定し、次を launcher 経由で実行した。

```bash
printf '%s' \
  '{"tool_name":"Write","tool_input":{"file_path":"tox.ini","content":"[testenv]\ncommands = pytest --no-cov\n"}}' |
  python3 "$PLUGIN/src/bluecore/launcher.py" bluecore.hooks.config_protection
```

実測結果:

```text
exit=0
```

`pyproject.toml` の lint/coverage/pytest 設定、`setup.cfg` の flake8、`package.json` の eslint 設定は deny されるが、`[testenv] commands` は保護対象キーに入っていない。

#### 影響

tox の実行コマンドを `--no-cov`、別 runner、成功扱いの wrapper へ変更し、品質ゲートを弱める経路が残る。

#### 修正案

`tox.ini` の `[testenv]`、`[testenv:*]` にある `commands`、`commands_pre`、`commands_post` の変更を section-aware に検出する。version、説明、環境変数などの正当な編集は許可し、実行コマンドの変更だけを deny する。

### A-05 — commit 品質フックの標準入力例外が exit 0

対象: `src/bluecore/hooks/pre_bash_commit_quality.py:main`

#### 再現

`PLUGIN="$HOME/.copilot/installed-plugins/bluecore/bluecore"` と
`PYTHONPATH="$PLUGIN/src"` を設定して実行した。

実際の hook module の `read_raw_stdin` を例外化して main の入出力境界を確認した。

```python
import bluecore.hooks.pre_bash_commit_quality as hook

hook.read_raw_stdin = lambda: (_ for _ in ()).throw(
    RuntimeError("stdin failure")
)
print(hook.main())
```

実測結果:

```text
main_exit= 0
```

コードはエラーを log するが、raw input が無いことを理由に fail-open している。

#### 影響

host/stdin の I/O 障害、decode 失敗、output adapter 失敗時に、commit かどうかを判定できないまま品質検査を通す。

#### 修正案

hook input を読めない場合は exit 2 の host-independent deny を返す。Bash matcher が広いことによる false positive は、入力不能時だけ deny する別の host error 契約で扱うべきで、成功扱いにしない。

### A-06 — config protection は Bash による直接編集を保護しない

対象: `hooks/hooks.json` の `config_protection` matcher

#### 再現

`PLUGIN="$HOME/.copilot/installed-plugins/bluecore/bluecore"` を設定し、次を launcher 経由で実行した。

`config_protection` は `Edit|Write|MultiEdit|...` の matcher にだけ登録されている。Bash payload を module に渡しても許可された。

```bash
printf '%s' \
  '{"tool_name":"Bash","tool_input":{"command":"printf x > pyproject.toml"}}' |
  python3 "$PLUGIN/src/bluecore/launcher.py" bluecore.hooks.config_protection
```

実測結果:

```text
exit=0
```

#### 影響

エージェントが `printf`、`python -c`、heredoc、リダイレクトなどで設定ファイルを直接変更すると、config protection の edit hook を経由しない。commit 品質フックは設定弱体化そのものを完全には検査しないため、R-07 の保護範囲を迂回できる。

#### 修正案

Bash matcher でファイル書き込み先を安全に解析して deny するか、commit 前に protected configuration の差分を専用検査する。shell 完全解析が困難な場合は opaque wrapper、リダイレクト、heredoc を「検査不能」として deny する。

### A-07 — `block_no_verify` は opaque wrapper と hooksPath override を検出しない

対象: `src/bluecore/hooks/block_no_verify.py`

#### 再現

以下の Python probe は `PYTHONPATH="$HOME/.copilot/installed-plugins/bluecore/bluecore/src"` を設定して実行した。

```python
from bluecore.hooks.block_no_verify import has_bypass_flag

print(has_bypass_flag("git -c core.hooksPath=/dev/null commit"))
print(has_bypass_flag("sh -c 'git commit --no-verify'"))
print(has_bypass_flag("git $(echo commit) --no-verify"))
```

実測結果:

```text
False
False
False
```

直接の `git commit --no-verify` と `git $VAR --no-verify` は検出された。

#### 影響

`core.hooksPath` の override や `sh -c`、command substitution の内部で hook bypass を実行すると、文字列 token scanner を迂回できる。

#### 判定と修正案

これは現行 docstring が明記する非目標（shell alias、変数展開、wrapper の再帰解析）に該当するため、今回の是正報告の単純な修正漏れとは分ける。ただし敵対的 agent を想定する場合は高リスクである。commit 実行を server-side policy または実行レベルの allowlist に寄せ、少なくとも `core.hooksPath` override と opaque shell wrapper は検査不能として deny する案が必要である。

## 6. 設計維持・条件付き事項

### 6.1 Python 3.12 未満

simulated Python 3.11.9 では launcher が `bluecoreProtectionDisabled` を示して exit 0 になる。Copilot 実行環境は対応版で問題なく動いたが、未対応 Python では保護が無効になる契約は残っている。

是正報告の説明どおり、全 Edit/Bash を exit 2 にすると復旧手段自体を塞ぐという trade-off は理解できる。しかし「未対応 runtime でも安全に動く」とは判定できないため、配布時の Python 検証、明示的なインストール失敗、保護 hook だけの停止状態などを別途検討する必要がある。

### 6.2 detached background

`--bg nonexistent.module` の親 exit 0 は維持されたが、child の traceback/失敗は `~/.bluecore/logs/bg-YYYY-MM-DD.log` に記録された。SessionEnd が best-effort である限り、診断可能性の改善は確認できる。

ただし API の意味は「処理成功」ではなく「起動受付」に限定し、起動 ID または次回 context で失敗を明示しない限り、呼び出し元は handoff 完了と誤認する。

### 6.3 reviewer の権限

`security-auditor` は tool frontmatter により Read/Grep/Glob のみで技術的に read-only。`reviewer` は Ruff/test command を再実行する職務のため Bash を保持する。no-fix probe は変更なしで完了したが、prompt 上書きや将来の本文変更に対して Bash write capability は残る。

技術的強制を必要とするなら、validator runner とレビュー agent の権限を分離し、reviewer には write-capable Bash を渡さない構成が必要である。

### 6.4 handoff transcript

symlink、non-regular file、所有者不一致は拒否された。所有者一致の regular file は host ごとの transcript root allowlist なしで読まれ、ユーザー文が handoff に入る。この挙動は Claude/Copilot の transcript root が異なることを理由とした設計である。

host-independent な性質検査としては機能しているが、現在ユーザーが書ける任意 regular file を prompt injection の入力にできる点は残る。構造化された事実だけを handoff へ渡すか、host ごとの trusted root を設定可能にする案が必要である。

## 7. Agent 実行から得た追加の注意事項

以下は maintain/reviewer/security-auditor の実行結果で強く指摘されたが、今回の no-fix 実機検証では独立した exploit probe まで行っていないため、確認済み runtime bug とは分ける。

| 項目 | 根拠 | 追加確認・修正案 |
|---|---|---|
| `grader`/`bench-analyzer` の任意 `Write` path | agent frontmatter が `Write` を持ち、`grading_path`/`output_path` を呼び出し側から受け取る | artifact root、canonical path、symlink、親ディレクトリ制約を共通化する。呼び出し側指定外への書き込み probe を追加する。 |
| `/harness` の apply 承認境界 | `harness.md` は `harness-tuner` に変更適用を委譲する | `--apply` 明示または変更前のユーザー承認を必須化し、audit-only と apply を完全分離する。今回 apply mode は実行していない。 |
| Copilot native registration の配布差分 | installed plugin の直接 hook/agent runtime は動いたが、配布 validator だけで update 後の host 登録を保証できない | update/install smoke test で PreToolUse、SessionStart、SessionEnd の実登録を毎回確認する。 |
| Git remote credential の保存 | security reviewer がリスクを指摘したが、この環境の `git remote get-url` は credential を mask して返した | credential-bearing remote を返す Git/host 環境で DB の `repos.remote_url` を再確認し、保存前に userinfo を除去する。 |

## 8. 解消確認できた項目の証跡

- `block_no_verify` の oversized payload: fail-closed、exit 2。
- `config_protection` の malformed/truncated JSON: fail-closed、exit 2。
- `pyproject.toml`、`setup.cfg`、`package.json` の代表的な lint/config 弱体化編集: deny。
- scanner の読み取り不能 file: `scan_error`/error severity。
- `git commit --amend`: quality scanner へ到達。
- `git.exe`、絶対 path、大小文字違いの git executable: commit 検出。
- knowledge の title/body/source_ref: 本文出力では `[REDACTED]`。
- pending knowledge: SessionStart context に自動注入されない。
- trust-boundary tags: context 出力から除去。
- 既存 memory DB mode: 0600 へ補正。
- memory stdin: bounded wait で open pipe が無期限待機しない。
- planner/architect: read-only でも計画本文を出力。
- grader: summary invariant と file-bound evidence を満たす。
- maintain:対象なしを `BLOCKED` とし、installed root の明示時は対象を発見。
- refactor-rollback: verification 不在時に架空コマンドを生成しない。
- hooks の subprocess timeout: 設定済み。
- dead API: 現行 manifest/hooks から参照されない旧 output adapter 経路を削除済み。

## 9. 最終判定

`PLUGIN_RUNTIME_AUDIT_2026-08-18_RESOLUTION.md` の修正は、主要な正常経路と複数の fail-closed 境界で実際に効果を確認できた。一方、以下は未解決として残る。

1. commit 品質・設定保護の検査不能時 fail-open と shell parsing 境界。
2. 1 MiB 後方の secret scan 回避。
3. knowledge key への secret 残存。
4. `tox.ini` および Bash 直接編集による config protection 迂回。
5. Python 3.12 未満の保護無効化と detached child failure の親 exit 0。
6. reviewer の write-capable Bash と handoff の任意 regular file trust boundary。

したがって、**Copilot CLI 1.0.80 上の bluecore v0.9.32 は「修正内容が全て解消した」とは判定しない。修正済み項目は確認できたが、上記の残存問題には再現方法と修正案が存在する。**

本検証ではプラグイン本体、定義、設定、fixture を修正していない。追加したリポジトリ成果物は本報告書のみである。
