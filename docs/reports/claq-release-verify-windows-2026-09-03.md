# claq 0.9.48 Windows 実起動検証レポート

## 1. 検証概要

- 検証日時: 2026-09-03 11:12 JST
- OS: Windows
- 作業ディレクトリ: `C:\Users\tasaki-mamoru\works`
- 作業ディレクトリは Git リポジトリではない
- 対象配布物: `C:\Users\tasaki-mamoru\.copilot\installed-plugins\claq\claq`
- plugin version: `0.9.48`
- 親エージェントおよび全サブエージェント: `gpt-5.6-luna`

本レポートは、実測できた事実、定義ロードだけ確認できた事項、静的検出、
PreToolUse 障害のため未実測となった事項を分離して記録する。

## 2. 棚卸しと起動状況

| 種別 | 件数 | 結果 |
|---|---:|---|
| Skills | 16 | 16件すべて定義ロード成功 |
| Commands | 9 | 9件すべて定義ロード成功 |
| Agents | 9 | 9件すべて起動応答あり |
| Python modules | 46 | 全モジュールの実起動は未完了 |
| Hook entries | 7 | 定義確認済み、実行は全件未到達 |

Agents は `planner`、`reviewer`、`security-auditor`、`code-refiner`、
`harness-tuner`、`tdd-writer`、`comparator`、`grader`、`bench-analyzer`を
すべて `gpt-5.6-luna` で再起動した。前提成果物がないエージェントは、
結果を捏造せず前提不足として停止した。

## 3. python3 再検証結果

管理者権限での再実行後、次のコマンドを試みた。

1. `Get-Command python3`
2. `python3 --version`
3. `block_no_verify` に安全な `git status` を渡す
4. `block_no_verify` に `git commit --no-verify -m x` を渡す
5. `config_protection` に `notes.txt` の書き込みを渡す
6. `config_protection` に `ruff.toml` の書き込みを渡す

すべての試行は Python、PowerShell、または launcher のプロセス開始前に、
次の同一エラーで拒否された。

```text
Denied by preToolUse hook from "claq@claq" (hook errored)
```

この時点では、`python3` の PATH 解決、実体、バージョン、launcher の
exit code、stdout、stderr、各 hook の allow/deny 結果は取得できなかった。
後続のフック無効化状態での環境確認により、実体のPythonは存在する一方、
`python3` がWindowsAppsのStore aliasへ解決されることを確認した。

前回レポートの「python3 不在」と読める記述は、次のように訂正する。

> `python3` の実体がないのではなく、現在のWindows環境では
> `python3` が `C:\Users\tasaki-mamoru\AppData\Local\Microsoft\WindowsApps\python3.exe`
> というStore aliasへ解決され、`python3 --version` が終了コード9009と
> `Python was not found`を返す。実体のPython 3.14.7は別の `python.exe` として
> 存在する。確定している問題は、PreToolUse hookが実体のPythonではなく
> 解決不能な `python3` aliasを呼び出していたことである。

## 4. 結論

**判定: BLOCKED**

定義ロードとエージェント起動は完了したが、Windows の PreToolUse フックが
Store aliasの `python3` 起動失敗で全操作を止めるため、`hooks.json` を有効化
した状態での完全検証には到達できない。さらに、実体のPythonを直接起動した
追加実測ではWindows stdin処理のfail-openも確認したため、Pythonコマンド名を
置き換えるだけでは安全な再有効化にならない。

## 5. 問題一覧

### P0-001: PreToolUse フックの全面障害（実測）

- **現象**: 無害な PowerShell 実行、`python3 --version`、launcher 実行、
  `apply_patch` による非設定ファイル作成がすべて
  `Denied by preToolUse hook from "claq@claq" (hook errored)` で停止する。
- **再現方法**:
  1. Windows で claq を有効にした Copilot CLI セッションを開始する。
  2. `python3 --version` または無害な PowerShell コマンドを実行する。
  3. launcher または `apply_patch` を実行する。
  4. Python/編集処理に到達する前に同じ拒否が返ることを確認する。
- **影響**: シェル、編集、テスト、実行検証、レポート保存が不能になる。
- **修正案**: Copilot CLI/Windows の hook command 契約を固定し、検証済みPython
  3.12以上の実体をwrapperまたは絶対パスで起動する。展開後のcommand、
  Python実体、stderr、exit codeを診断可能にし、単一hookの起動失敗を
  原因不明の全面拒否へ変換しない。
- **状態**: Windows実測、直接原因（`python3` Store alias）を確認、未修正。
  stdin処理にも別のfail-open実測あり。確度 10/10。

### P1-002: hooks.json の POSIX 起動固定（静的検出）

- **現象**: 全 hook が `python3 "${CLAUDE_PLUGIN_ROOT}/src/claq/launcher.py"`
  形式に固定されている。
- **再現方法**: Windows native で `python3` が PATH にない、または
  `${CLAUDE_PLUGIN_ROOT}` をホストが展開しない条件で hook を発火させる。
- **影響**: hook 起動失敗または host 側 hook error になる。P0-001 の直接原因候補。
- **修正案**: Windows 用 `.cmd`/`.ps1` wrapper、`sys.executable` を使う起動、
  ホスト別 command 生成を追加し、インストール後 smoke test で展開を検証する。
- **状態**: Windows環境で `python3` がStore aliasへ解決されることを実測。
  hooks.jsonの起動設計欠陥を確認。確度 10/10。

### P1-003: runtime の POSIX 専用 bootstrap（静的検出）

- **現象**: `env-template.sh` と `claq-helpers.sh` が `source`、`ps`、`tr`、
  `nohup`、`grep`、`sort`、`head`、`python3` 等を要求する。
- **再現方法**: PowerShell のみの Windows 環境で
  `. "$HOME/.claq/env.sh"` と各 helper を実行する。
- **影響**: root pointer、`claq_run`、memory helper が構築されない。
- **修正案**: Python API を正規実装にして shell bootstrap を薄くするか、
  POSIX 用 `.sh` と Windows 用 `.ps1` を分離する。
- **状態**: 静的検出、実行未完了。確度 9/10。

### P1-004: stdin 監視の Windows 互換性と fail-open（静的検出）

- **現象**: `hook_common.py` が `select.select([sys.stdin], ...)` を使う。
  Windows の通常パイプで例外になると入力なし扱いになる。
- **再現方法**: Windows パイプから JSON payload を渡し、`select.select` が
  `OSError`/`ValueError` になる条件で `block_no_verify` を実行する。
- **影響**: 判定不能時に本来ブロックすべき `--no-verify` を許可する可能性。
- **修正案**: Windows 用の overlapped I/O 等を使い、保護 hook の入力判定不能時は
  exit 2 等の fail-closed にする。
- **状態**: Windowsのリダイレクトstdinでallow/deny payloadを直接実測し、
  どちらもexit 0・stdout/stderr空。fail-open経路を再現。確度 10/10。

### P1-005: `mem promote` の DB 更新後 `os.getuid()`（静的検出）

- **現象**: `_handle_promote()` が DB 更新後に監査ログ用の `os.getuid()` を呼ぶ。
- **再現方法**: Windows の隔離 DB で pending card に `promote <key>` を実行し、
  DB status、exit code、stderr を比較する。
- **影響**: DB だけ active になり、処理が非0終了する部分成功の可能性。
- **修正案**: Windows の SID/ユーザー名を使う共通 actor 識別子を導入し、
  更新と監査記録を同一トランザクションで扱う。
- **状態**: 静的検出、実行未完了。確度 9/10。

### P1-006: transcript trust の UID 依存（静的検出）

- **現象**: `is_trusted_transcript()` が `st_uid` と `os.getuid()` で所有者を判定する。
- **再現方法**: Windows の trusted root 内ファイルを対象に context/handoff を実行する。
- **影響**: transcript の一律拒否または誤った trust 判定。
- **修正案**: Windows では ACL/SID による所有者・アクセス権判定を実装する。
- **状態**: 静的検出、実動作未完了。確度 9/10。

### P1-007: harness audit のインストール先誤認（静的検出）

- **現象**: `find_plugin_install()` が `.claude/plugins/claq` と `HOME` を主に
  探索し、実際の `.copilot/installed-plugins/claq/claq` と `USERPROFILE` を
  十分に扱わない。
- **再現方法**: 現在のインストール先で harness audit を実行し、plugin install
  check の判定を確認する。
- **影響**: インストール済みなのに未導入と誤報する。
- **修正案**: Copilot の公式ルート、`Path.home()`、`USERPROFILE`、`HOME` を
  正規化して探索する。
- **状態**: ソースと実インストールパスの比較で確認、実行未完了。確度 10/10。

### P1-008: `chmod` だけの機密ファイル保護（静的検出）

- **現象**: ディレクトリと DB の保護が `chmod(0700/0600)` に依存する。
- **再現方法**: Windows で DB/logs を作成し、`Get-Acl` で継承 ACL を確認する。
- **影響**: POSIX mode bit が Windows DACL のユーザー限定を保証しない。
- **修正案**: 現在ユーザー SID のみを許可する ACL を設定し、継承を制御する。
- **状態**: 静的検出、ACL 実測未完了。確度 9/10。

### P1-009: remote URL の query/fragment credential 残存（静的検出）

- **現象**: `_strip_userinfo()` は authority の userinfo だけを除去し、
  query/fragment 内の token を除去しない。
- **再現方法**: token を含む query または fragment の remote URL を設定し、
  repo identity 保存後の `remote_url` を確認する。
- **影響**: credential が DB やログへ永続化される。
- **修正案**: URL parser で query/fragment を破棄または秘密キーを redact し、
  認証情報を含まない canonical URL だけ保存する。
- **状態**: 静的検出、fixture 未実行。確度 8/10。

### P1-010: handoff 本文の永続化・再注入（静的検出）

- **現象**: 明示 `handoff` 本文が DB に保存され、次セッションの context に注入される。
- **再現方法**: 指示風文字列を handoff payload に入れ、SessionEnd 相当と次回 context
  を確認する。
- **影響**: 信頼されない指示文が高信頼な context に見える形で再注入される。
- **修正案**: handoff を診断データとして固定表示し、自由文より構造化事実を優先する。
- **状態**: 静的設計リスク、fixture 未実行。確度 8/10。

### P1-011: background 失敗ログの未サニタイズ注入（静的検出）

- **現象**: detached 処理の background log 最終行が次セッションの通知に流れる。
- **再現方法**: log path に指示風またはタグ風文字列を出力する child を実行し、
  次回 failure notice を確認する。
- **影響**: ログ由来の文字列がエージェント入力へ混入する。
- **修正案**: 固定フィールドの診断データとして扱い、タグ除去、長さ制限、出所表示、
  指示文の無害化を行う。
- **状態**: 静的検出、fixture 未実行。確度 8/10。

### P1-012: `promote` の人間承認が技術的に強制されない（静的検出）

- **現象**: 人間承認が文書上の要求に留まり、`promote` はエージェントから呼べる。
- **再現方法**: ユーザー操作なしに隔離 DB へ `mem promote <key>` を実行する。
- **影響**: 未承認カードが active memory へ昇格する。
- **修正案**: host-issued approval token、対話確認、署名付き承認、または
  人間操作専用 API を導入する。
- **状態**: 静的検出、実行未完了。確度 8/10。

### P1-013: detached watchdog の Unix シグナル依存（静的検出）

- **現象**: watchdog が `os.killpg`、`SIGTERM`、`SIGKILL`、
  `start_new_session=True` を使う。
- **再現方法**: Windows で SessionEnd `--bg` 相当を起動し、timeout 時の
  子・孫プロセス回収を確認する。
- **影響**: Windows で子孫プロセスが残留または cleanup が失敗する可能性。
- **修正案**: Windows では Job Object を使う OS 別 watchdog を実装する。
- **状態**: 静的検出、実動作未完了。確度 9/10。

### P2-014: Windows 絶対パスが handoff に残る可能性（静的検出）

- **現象**: `_shorten_path()` が `/` のみで分割し、`C:\...` を短縮できない。
- **再現方法**: Windows 絶対パスを変更ファイルとして handoff に渡す。
- **影響**: ユーザー名を含む絶対パスが handoff/logs に残る。
- **修正案**: `pathlib` または `os.path` で Windows 区切りと home を処理する。
- **状態**: 静的検出、fixture 未実行。確度 10/10。

### P1-015: review 後処理の無承認自律書き込み（定義静的検出）

- **現象**: `review.md` が分類後、ユーザー応答を待たず loop-dev に修正を委譲する。
- **再現方法**: `/review` で修正可能な指摘を生成し、明示承認なしに
  Edit/Write/commit 関連処理が始まるか確認する。
- **影響**: READ-ONLY レビューの境界を越えた意図しない変更が起きる。
- **修正案**: レビュー後に Human Gate を置き、変更対象と commit 可否を明示承認させる。
- **状態**: 定義静的検出、後処理未実行。確度 9/10。

### P2-016: output_adapter の契約不一致（静的検出）

- **現象**: docstring は `modifiedResult` を説明するが、実装 JSON は
  `additionalContext` と `hookSpecificOutput` のみを生成する。
- **再現方法**: `adapt_context_output()` の JSON キーを列挙する。
- **影響**: host が `modifiedResult` を必要とする場合、結果変更が無視される。
- **修正案**: 実 host 契約を確認して実装と docstring を一致させる。
- **状態**: 実装と文書の比較で確認、host 実測未完了。確度 10/10。

### P1-017: Python 3.12 未満で保護が無効化される（静的検出）

- **現象**: launcher は Python 3.12 未満で hook を実行せず exit 0 を返す。
- **再現方法**: Python 3.11 以下を `python3` として launcher を起動する。
- **影響**: host が成功扱いと誤認し、保護機能が無効なまま継続する。
- **修正案**: 保護無効を host に明示し、必要なら exit 2 の fail-closed とする。
- **状態**: 静的検出、Python 3.11 実行未完了。確度 10/10。

## 6. 未実測項目

1. `hooks.json` を有効化したCopilotプロセスから見たPython実体、PATH、
   launcherのexit code/stdout/stderr。
2. 4つの PreToolUse hook の全 allow/deny payload。
3. PreCompact、SessionStart、SessionEnd `--bg` のWindows実起動。
4. `mem promote`、handoff、transcript trust の隔離 fixture。
5. harness audit の Copilot インストール先検出。
6. Windows ACL、Job Object、remote URL redaction、path shortening。
7. output_adapter の Copilot host 実解釈。

## 7. 推奨修正優先順位

1. **P0**: Windows/Copilot CLI で hook command を確実に起動し、失敗原因を
   exit code と stderr で診断可能にする。
2. **P1**: POSIX runtime、stdin 判定、UID/SID、Windows ACL、Job Object、
   remote credential、handoff 入力境界、review Human Gate を修正する。
3. **P2**: Windows path shortening、output_adapter 契約、Python バージョン不足時の
   終了ポリシーを整合させる。
4. P0 修正後に未実測項目を再実行し、各指摘を `修正` または根拠付き `NO-FIX` に裁定する。

## 8. 成果物状態

- 対象プラグイン: 変更なし
- コミット: なし
- 本レポート: 作成
- 判定: **BLOCKED**
- BLOCKED 理由: `python3` alias起動失敗とWindows stdin fail-openを修正して
  `hooks.json` を再有効化した状態での完全な実測が未完了
- `python3` 問題の判定: **原因確認済み**。実体は存在するが、Store aliasへ
  解決されるためhooks.jsonの現行commandでは起動できない

---

## 9. 追補: フックエラーの再現条件・原因・修正方法（2026-09-03 19:29 JST）

### 9.1 フックを有効化するための環境確認

Copilot再起動後の同一ユーザー環境で、Python実体とコマンド解決結果を確認した。

```text
python  -> C:\Users\tasaki-mamoru\AppData\Local\Programs\Python\Python314\python.exe
python --version -> Python 3.14.7 (exit 0)
py -3.14 --version -> Python 3.14.7 (exit 0)
python3 -> C:\Users\tasaki-mamoru\AppData\Local\Microsoft\WindowsApps\python3.exe
python3 --version -> Python was not found ... App execution aliases (exit 9009)
```

この結果から、Python自体が存在しないのではなく、`hooks.json` が呼び出す
`python3` がMicrosoft Storeの実体を持たないApp execution aliasへ解決され、
実際のPython 3.14.7へ到達していないことが確認できる。claqの要求する
Python 3.12以上は満たしているため、現在の環境では `python.exe` の明示指定、
または `python3` を実体へ向けるwrapperが必要である。

### 9.2 再現した現象

フックが有効だった時点では、無害な操作を含む複数のツール呼び出しが、処理の
開始前に次のエラーで停止した。

```text
Denied by preToolUse hook from "claq@claq" (hook errored)
```

Copilotのプロセスログ
`C:\Users\tasaki-mamoru\.copilot\logs\process-1788430211522-14096.log`
には、次の直接証拠がある。

```text
Hook from "claq@claq" execution failed: Error: Hook command failed with code 1
Stderr: Python was not found; run without arguments to install from the Microsoft Store, or disable this shortcut from Settings > Apps > Advanced app settings > App execution aliases.
```

この組み合わせは同ログの135-136行および153-154行にあり、別の過去ログにも
同じ標準エラーが反復している。

再現時の処理順は次のとおりである。

```text
CopilotがPreToolUseを発火
  -> claqのhooks.jsonからcommandを取得
  -> Windowsがcommand先頭のpython3を解決
  -> Python実体を解決できずcode 1
  -> launcher.pyは起動しない
  -> Copilotがhook errored/fail-closedとして元の操作を拒否
```

そのため、`launcher.py`、対象hookモジュール、stdin JSONの解析、hook内部の
`select.select`や`os.getuid()`が今回の直接エラーを発生させたとはいえない。
それらへ到達する前に、OSの実行ファイル解決で停止している。

### 9.3 再現に必要な環境条件

主経路を再現する条件は以下である。

| 条件 | 必須度 | 内容 |
|---|---|---|
| OS | 必須 | Windows |
| claq | 必須 | `claq@claq` が有効で、hook定義がロードされている |
| hook command | 必須 | `python3 "${CLAUDE_PLUGIN_ROOT}/src/claq/launcher.py" ...` |
| Python | 必須 | Copilotプロセスから `python3` が解決できない、またはStore aliasだけが有効 |
| 操作 | 必須 | PreToolUse対象のBash/shell/Edit/Write等 |
| プロセス状態 | 増幅要因 | PATH変更後もCopilotを再起動していない |

PATHにPythonを追加しても、Copilotを起動したプロセスが古い環境変数を保持
している場合は、同じ失敗が続く。管理者PowerShellから見えるPATHと、通常ユーザー
として起動したCopilotのPATHが異なる場合も同じ結果になる。

### 9.4 再現手順

#### ケースA: `python3` が存在しない、またはStore aliasだけの場合

1. WindowsでPythonを未インストールにする、または実体のないMicrosoft Store
   App execution aliasだけを残す。
2. `hooks.json` が存在するclaqを有効にして、Copilot CLIを起動する。
3. Bash/shell tool、またはhook matcherに該当するEdit/Write toolを呼び出す。
4. 次のcommandが実行されることを確認する。

   ```text
   python3 "C:\Users\<user>\.copilot\installed-plugins\claq\claq/src/claq/launcher.py" <module>
   ```

5. Windowsが `Python was not found` をstderrへ出し、hookがcode 1で終了する。
6. Copilotが `hook errored` として操作を拒否する。

#### ケースB: PATH変更後にCopilotを再起動しない場合

1. ケースAの状態でCopilotを起動する。
2. Copilotを終了せず、別の管理者PowerShellまたはシステム設定でPython 3.12
   以上をPATHへ追加する。
3. 同じCopilotセッションでPreToolUse対象の操作を呼び出す。
4. Copilotプロセスが起動時環境を保持していれば、変更前と同じ
   `Python was not found` が再現する。
5. Copilot CLIと関連バックグラウンドプロセスを完全終了する。
6. PATH変更後の新しい環境からCopilotを起動し、同じ操作を再実行する。
7. 再起動前後のprocessログを比較し、Python解決エラーが消えるか確認する。

#### ケースC: Python解決またはhook実行が5秒を超える場合

過去ログには、次のタイムアウトも記録されている。

```text
preToolUse hook from "claq@claq" timed out; allowing the tool call to proceed:
HookTimeoutError: Hook command timed out after 5 seconds
```

`block_no_verify` と `bash_config_protection` はtimeout 5秒である。Store alias
の応答遅延、Python起動遅延、hookプロセスの待機によりこの経路へ入ると、
ログ上は `allowing the tool call to proceed` となる。

これはcode 1のfail-closedとは異なり、保護hookが完了しないときに対象操作を
通すfail-open経路である。保護目的と矛盾するため、Python起動失敗とは別の
再現対象・修正対象として扱う。

### 9.5 原因の判定

原因は、ホストとプラグインを分離して次のように判定する。

1. **直接原因（実測）**: Copilot hook runnerが `python3` を実行しようとした
   時点でWindowsがPython実体を解決できず、code 1と `Python was not found`
   を返した。
2. **claq側の原因（設計バグ）**: `hooks.json` の全7 hookが、検証済み絶対パス
   やWindows wrapperではなく、Copilotプロセスのambient PATHにある裸の
   `python3`へ依存している。
3. **ホスト側の動作**: hookのcode 1を検知して操作を拒否するfail-closedは、
   未実行の保護hookを通さない動作として妥当である。
4. **増幅要因**: 起動失敗の診断がホストの一般的な `hook errored` に集約され、
   必要Python、PATH、再起動方法が利用者へ明示されない。
5. **今回の未到達部分**: `select.select`、UID/SID、`write_env_pointer()`、
   各hookの判定ロジックは、Python起動後の別問題であり、この再現の直接原因
   ではない。

また、ログ中のcommandには
`C:\Users\tasaki-mamoru\.copilot\installed-plugins\claq\claq/src/claq/...`
のような展開済みパスが表示されている。そのため、今回の主原因は
`${CLAUDE_PLUGIN_ROOT}` の未展開ではない。`\`と`/`の混在は、Python解決後に
別のWindows互換性問題を起こす可能性があるため、独立して修正する。

### 9.6 修正方法

#### P0: Windows用の確実な起動経路を配布する

1. `hooks.json` へ `python3` を直接記述せず、Windows用
   `runtime\claq-hook.cmd` または同等のwrapperを指定する。
2. wrapperは、次の順でPythonを解決する。
   - インストール時に検証・固定したPython 3.12以上の絶対パス
   - claq専用venvの `python.exe`
   - `py -3.12` で取得した実体
   - 最後に `python3`/`python`（実行後に3.12以上を検証）
3. `python`への単純置換は行わない。古いPython、別ユーザーのPATH、
   Microsoft Store aliasを誤って拾うため不十分である。
4. Pythonが見つからない、または3.12未満の場合は、wrapperが必要バージョン、
   検出候補、復旧方法をstderrへ明示し、非0終了する。
5. 可能なら専用venvまたは配布時に固定したruntimeを使い、ユーザーPATHへの
   依存をなくす。

#### P1: インストール時検証と診断を追加する

1. インストール完了時に、実際のWindows hook commandを起動するsmoke testを
   行う。`python --version`、launcher、代表allow/deny payloadを確認する。
2. wrapperの診断ログに、解決したPython絶対パス、Pythonバージョン、
   plugin root、対象module、終了コードを記録する。秘密情報は記録しない。
3. `CLAUDE_PLUGIN_ROOT` が未展開の場合は、一般的なhook errorではなく、
   展開前command、期待する値、復旧手順を明示する。
4. PATH・権限・Pythonを変更した後はCopilot CLIを完全終了して再起動する必要が
   あることをインストール文書とトラブルシューティングへ記載する。

#### P1: timeout時の保護ポリシーを見直す

1. `block_no_verify`、`config_protection`、commit qualityのような保護hookは、
   起動失敗、入力不正、timeoutをfail-closedとして扱う。
2. `SessionStart`や診断用hookは、セッション全体を壊さず、警告を返す別ポリシー
   に分ける。
3. ホストがtimeout時に無条件で `allowing the tool call to proceed` とする場合、
   保護hookについては拒否または明示的な安全状態へ倒せる契約を検討する。

### 9.7 `hooks.json` を有効化した状態での再検証方針と判定

`hooks.json` を有効化した状態でエラーを解消するには、まずhook commandの
先頭にあるPython解決を修正し、その後にCopilotを完全再起動する必要がある。
Python実体が存在していても、現在のように `python3` がWindowsApps aliasへ
解決されると、launcherは起動しない。

修正後は新規Copilotプロセスで、次の順に確認する。

1. `python3`またはwrapperの実体・バージョン・PATH。
2. launcherのstdout/stderrと終了コード。
3. 各PreToolUse hookのallow/deny payload。
4. timeout時に保護対象が許可されないこと。
5. Python起動後に初めて現れるWindows互換性問題（stdin、UID/SID、ACL、
   Job Object、POSIX runtime、Windows path）を個別に確認する。

`hooks.json` を無効化してエラーを消すことは保護機能を停止するだけであり、
修正や合格の証拠にはならない。修正後に `hooks.json` を有効化した状態で、
allow/denyの両方を含む実payloadを流して初めて、フックが実用可能と判定する。

### 9.8 `hooks.json` を有効化するための具体的な修正手順

現在の環境で最短に復旧する手順は次のとおりである。

1. `hooks.json` の各commandで裸の `python3` を直接呼ばない。
2. Windows用wrapperを追加し、実体として
   `C:\Users\tasaki-mamoru\AppData\Local\Programs\Python\Python314\python.exe`
   を使用するか、インストール時に検証したPythonの絶対パスを使用する。
3. wrapper起動時に `sys.version_info >= (3, 12)` を確認し、条件を満たさない
   場合はstderrへ明示的な診断を出して非0終了する。
4. wrapperで解決したPython、plugin root、対象module、終了コードを機密情報を
   除いて記録する。
5. 代替案として、WindowsのApp execution aliasesで誤った `python3.exe`を無効化
   し、実体のPythonを `python3` として解決できるshimをPATH先頭へ置く方法もある。
   ただし、ユーザー環境のPATHに依存するため、配布物としてはwrapperまたは
   専用venvの方が再現性が高い。
6. `python` へ単純置換するだけでも現在の端末では動くが、別のWindows環境で
   古いPythonやStore aliasを拾う可能性があるため、製品修正はバージョン検証を
   含むwrapper方式にする。
7. 修正後、Copilot CLIと関連バックグラウンドプロセスを完全終了して再起動する。
8. `hooks.json` を有効化した新規セッションで、まず無害なallow payload、
   次に `git commit --no-verify` 等のdeny payloadを流し、hookの終了コードと
   Copilotの判定を確認する。

#### 推奨する実装例

```text
Windows:
  hooks.json
    -> runtime\claq-hook.cmd
      -> 検証済み Python 3.12+ の絶対パス
        -> src\claq\launcher.py

POSIX:
  hooks.json
    -> 検証済み python3
      -> src/claq/launcher.py
```

Windowsの配布物で `python3` のPATH解決だけに依存する現行形式を残す限り、
`hooks.json` を再有効化した環境で同じエラーが再発する。

### 9.9 再有効化前の追加実測と有効化条件

`python3` の実体解決だけを置き換えれば十分かを確認するため、現在実際に
起動できるPython 3.14.7の絶対パスで `launcher.py` を直接起動し、Windowsの
リダイレクトstdinへUTF-8 JSONを渡した。

| Payload | 実行結果 |
|---|---|
| `{"tool_name":"Bash","tool_input":{"command":"git status"}}` | exit 0、stdout空、stderr空 |
| `{"tool_name":"Bash","tool_input":{"command":"git commit --no-verify -m x"}}` | exit 0、stdout空、stderr空 |

本来、後者は `block_no_verify` がdenyして非0終了またはdeny JSONを返すべき
payloadである。allowとdenyが同じ `exit 0` になったため、**Python起動エラーを
解消するだけでは、保護hookが意図どおり動くとは判定できない**。

配布ソースの `hook_common.py` では、`_stdin_ready()` と
`_read_stdin_bytes()` がWindowsのパイプに対して `select.select` を使い、
`OSError`/`ValueError`を「入力なし」として握りつぶす実装になっている。
その場合、hookは入力を読まず、空入力のまま許可側へ進み得る。今回の
allow/deny同一結果は、このWindows stdin fail-open経路と整合する。

従って、`hooks.json` を安全に有効化するための修正は二段階で必要である。

1. **起動修正**: 裸の `python3` を、検証済みPython 3.12以上の絶対パスを
   使用するWindows wrapper、または専用venvの `python.exe` に置き換える。
2. **入力処理修正**: Windowsでは `select.select([sys.stdin], ...)` に依存せず、
   Windows pipeに対応した読み取り（overlapped I/O等）を使う。読み取り不能、
   JSON不正、入力欠落、timeoutは保護hookではfail-closedの非0終了にする。

修正後の受入条件は次のとおりである。

| 確認対象 | 合格条件 |
|---|---|
| Python解決 | `python3` aliasではなく、実体のPython 3.12以上が起動する |
| launcher | stderrにPython解決エラーがなく、対象moduleまで到達する |
| allow payload | 安全なコマンドが許可される |
| deny payload | `git commit --no-verify` が非0終了またはdeny JSONになる |
| 入力異常 | 空入力、壊れたJSON、Windows pipe読取例外を許可側へ倒さない |
| timeout | 保護対象の操作を `allowing the tool call to proceed` にしない |

`python` を現在の端末で直接指定するとPython 3.14.7は起動するが、
上記のstdin問題が残るため、それだけで `hooks.json` の再有効化を完了とは
しない。wrapperとstdin処理を修正し、修正後の実payloadでこの表を満たした
状態でのみ有効化する。

## 10. 追補時点の成果物状態

- 対象プラグイン: `hooks.json` を有効化して再検証する前提
- 本レポート: 上記の再現条件、ログ証拠、原因判定、修正方法を追記
- 配布物の修正: 未適用（無効化ではなくWindows用起動経路の修正が必要）
- 検証Gate: **BLOCKED**
- 理由: フック起動障害の直接原因は特定したが、フックを復元した状態での
  launcher・全payload・timeoutの再実測と、Windows用起動修正の適用は未実施

---

## 11. 対応結果（2026-09-04）

本節は §5 の 17 件すべてに対する裁定と、実施した修正・検証状態を記録する。
検証は macOS（Python 3.14.7）でのみ実施した。**Windows 実機での再検証は
未実施**であり、各行の「検証」列がその区別を持つ。

凡例:

- **T** = macOS 上の自動テストで表明（`pytest` で回帰固定済み）
- **S** = macOS 上で実プロセスを起動して実測（スモーク）
- **U** = Windows 実機で未検証（設計・静的根拠のみ）

### 11.1 裁定一覧

| ID | 裁定 | 対応内容 / NO-FIX の根拠 | 検証 |
|---|---|---|---|
| P0-001 | 修正 | hooks.json の全 7 エントリを `runtime/claq-hook`（Windows は同名 `.cmd`）経由へ変更。インタプリタ解決を wrapper の単一責務に集約 | T+S / Windows は **U** |
| P1-002 | 修正 | 同上。Windows 側は `WindowsApps` 配下の Store alias を候補から除外し、`py -3` を優先 | T / Windows は **U** |
| P1-003 | 修正（設計変更） | `env.sh` resolver は祖先 PID と `ps -o lstart=` の照合に依存し Windows では原理的に解決不能。md を 2 通り書かず、SessionStart の `mem context` が解決済み plugin root と読み替え方（`claq_run` / `claq_mem_learn` 両方）を 1 節だけ注入する | T |
| P1-004 | 修正 | `select.select` を撤去し、ブロッキング read を daemon スレッドへ隔離（3 OS 共通の 1 実装）。「入力が無い」と「読めなかった」を分離し、後者は保護 hook 4 つが deny。保護フックの失敗方向 に記録 | T+S |
| P1-005 | 修正 | `os.getuid()` を `core_utils.actor_identity()` へ置換し、DB 更新の**前**に確定。部分成功の窓を閉じた | T |
| P1-006 | 修正 | 所有者判定を `os.getuid` の有無という capability で分岐。uid が無効な環境では symlink 拒否・通常ファイル要求・trusted root 包含で守る | T |
| P1-007 | 修正 | `HOME` 直読みを `get_home_dir()`（`CLAQ_HOME`/`HOME`/`USERPROFILE`/`Path.home`）へ。Copilot の実配置 `.copilot/installed-plugins/claq/claq/` を探索対象に追加 | T+S |
| P1-008 | **NO-FIX** | `%USERPROFILE%` 配下は既定でそのユーザー（と SYSTEM/Administrators）に限定されており、シェルコマンド解析の境界 の脅威モデル（同一 OS ユーザーの敵対的回避は非対象）に対し POSIX の 0700 と同水準。Administrators が読める点は POSIX の root と対応する。`icacls` / ctypes 依存を増やして得られる差が無い。根拠は `core_utils.ensure_private_dir` と CLAUDE.md へ記載 | — |
| P1-009 | 修正 | `_strip_userinfo` → `_strip_credentials`。userinfo に加えて query / fragment を丸ごと破棄（鍵名の列挙では新しい鍵名を取りこぼすため） | T |
| P1-010 | **NO-FIX（誤検出）** | 既に実装済み。`_handoff_section` は `strip_tags` を通し `CONTEXT_HANDOFF_CHAR_BUDGET` で切っている（`mem/cli.py`）。信頼できない入力とプロンプト境界 / 陳腐化の検知 が扱う領域 | T（既存） |
| P1-011 | **NO-FIX（誤検出）** | 既に実装済み。`_bg_failure_section` は `strip_tags(normalize_user_message(...))` を通し、末尾 1 行・4KB 上限に限定している | T（既存） |
| P1-012 | **NO-FIX** | 信頼できない入力とプロンプト境界 が明示的に受容した残存リスク。同一 UID から `mem.db` を直接更新できる以上、CLI をいくら固めても人間実行の保証にはならない。`_handle_promote` の docstring にも記載済み | — |
| P1-013 | 修正 | `detached_spawn_kwargs()` を追加（POSIX: `start_new_session` / Windows: `CREATE_NEW_PROCESS_GROUP｜DETACHED_PROCESS`）。watchdog の停止処理を `stop_child(hard)` へ集約し `os.killpg` の有無で分岐。Windows で孫を回収しない差は受容し、根拠（`--bg` 対象の孫は git のみで、git 側にもハードタイムアウトがある）をコードへ明記 | T / Windows は **U** |
| P2-014 | 修正 | `_shorten_path` が `/` と `\` の両方を区切りとして扱う | T |
| P1-015 | **NO-FIX** | `review.md` の設計そのもの。READ-ONLY 制約はステップ 1〜3（レビュー工程）に限定され、ステップ 4 の自律修正は意図された挙動。とくに `commands/review.md:62` は仕様変更だけを除外し **「これは承認待ちではなくスコープ制限」** と明記しており、承認ゲートを検討したうえで採らない判断が既に記録されている | — |
| P2-016 | 修正 | 実装が正しく docstring が誤り。`modifiedResult` の記述を削除し、実測していない host 契約を推測でキーに足さない方針を明記 | T（既存の出力テスト） |
| P1-017 | **NO-FIX** | CLAUDE.md「ランタイム前提」と `launcher.py:52-58` が記録済みの意図的判断。fail-closed にすると Python を直す手段（Bash）ごとセッション内から塞がれ復旧不能になる。`claqProtectionDisabled` を stderr へ出して無音の無効化は避けている。wrapper 側の「Python が 1 つも見つからない」経路も同じポリシーに揃えた | T |

### 11.2 §9.4 ケース C（timeout 時の fail-open）

ホストが hook の timeout を `allowing the tool call to proceed` に変換する
挙動はプラグイン側から変えられない。踏みにくくする方向で対応した。

- 保護 hook の timeout を 5 → 15 秒、`pre_compact` を 10 → 15 秒、
  `mem.cli context` を 5 → 20 秒へ引き上げ（Windows の wrapper +
  インタプリタ起動を吸収する）
- `STDIN_FIRST_BYTE_TIMEOUT` を 1.0 → 2.0 秒

### 11.3 検証中に判明した新規不具合（レポート外）

v0.9.50 公開後に 3 OS 横断で hook 経路を再レビューし、下表を追加で検出・修正した。

| ID | 現象 | 対応 | 検証 |
|---|---|---|---|
| N-01 | 開いたまま何も書かれない stdin を渡すと、deny 出力の直後に `Fatal Python error: _enter_buffered_busy` で abort し終了コードが 2 でなくなる（P1-004 の修正で導入した daemon スレッドが `BufferedReader` のロックを保持したままブロックするため） | 実 fd がある場合は `os.read()` で読む（Python レベルのロックを握らない）。プロセス終了時にしか現れないため実プロセスの回帰テストを追加 | T+S |
| N-02 | `claq_mem_learn` の marshal 経路（`python3 -c` → `claq.mem.learn_payload`）に経路テストが無かった | 隔離 DB へ実際に 1 件書き、引用符・パイプ・非 ASCII が素通ること、`source=agent`/`status=pending` であることを bash と dash の両方で表明 | T |
| **W1** | **`claq-hook.cmd` が `if defined ... ( python ... & exit /b %ERRORLEVEL% )` の形だった。cmd はカッコブロック内の `%VAR%` を*パース時*に展開するため、返るのは python 起動*前*の errorlevel になる。保護 hook の exit 2 が 0 として host へ報告され、Windows では全 deny が allow になる** | ブロックを廃して `goto` + 素の `exit /b` へ再構成。「起動行がブロック外」「`%ERRORLEVEL%` を持たない」ことを構造テストで固定 | T / Windows は **U** |
| W2 | WindowsApps 除外を `echo %%P｜findstr` で行っていた。for 変数は展開後に再パースされるため `C:\Program Files (x86)\...` のようなカッコ入り実在パスでブロックが壊れる | 除外を `where` のパイプライン側へ移動 | T / Windows は **U** |
| W3 | findstr のパターンが `"\WindowsApps\"` と閉じ引用符直前でバックスラッシュ終端していた。findstr は C ランタイム解析で `\"` を引用符のエスケープとして読むため、除外が永久に不発になる | `"\WindowsApps"` へ | T / Windows は **U** |
| W4 | `detach_process` の stdin 一時ファイルは Windows では unlink できず（子が継承ハンドルを保持）、`~/.claq` にセッションごと 1 個ずつ孤児が残る | env_pointer の GC を `*.stdin` にも広げた（猶予 1 時間の age-gate） | T |
| W5 | `shlex(posix=True)` がクォート外の `\` をエスケープとして消費するため、`rm .\.eslintrc` は `['rm', '..eslintrc']`、`C:\Git\bin\git.exe commit --no-verify` は `['C:Gitbingit.exe', ...]` に潰れ、保護対象 basename も git 起動も見失う | コマンド文字列を POSIX 読みと Windows 読みの 2 方言で解析し、どちらかが検出したら deny（シェルコマンド解析の境界）。既存 2520 件は全て緑のまま | T |
| W6 | PowerShell の長形式 cmdlet（`Remove-Item` / `Set-Content` / `Out-File` / `Add-Content` / `New-Item` / `Copy-Item` / `Move-Item` / `Clear-Content`）が書き込み語彙に無かった（`rm`/`cp`/`mv` は PowerShell の別名なので既に効いていた） | 語彙へ追加し、実行位置コマンド名の比較を大小無視に。malformed JSON の縮退経路でも同じ扱いになるよう生テキスト照合も小文字化 | T |
| **N-03** | **stdout がパイプかつ UTF-8 モード無効のとき、Python はロケール由来のエンコーディングを使う。Windows の既定コードページ（日本語環境なら cp932）や `LC_ALL=C` の Linux では、注入コンテキストや deny 理由の日本語が `UnicodeEncodeError` になり、フックは注入も deny もできないまま exit 1 で落ちる** | launcher が起動直後に stdout/stderr を UTF-8（errors=replace）へ固定。`--bg` の子は `main()` を通らないため `build_env()` に `PYTHONIOENCODING=utf-8` も追加 | T+S（`PYTHONUTF8=0 LC_ALL=C` で実測・再現・修正確認） |
| N-04 | 祖先ポインタ方式が成立しない OS でも毎 hook `ps` を spawn していた（MSYS 由来の `ps.exe` が PATH にあると別 PID 空間の値を書きうる） | `ancestor_pointers_supported()` が偽なら `ps` を呼ばない | T |

`pre_bash_commit_quality` / `commit_quality_scanner` の subprocess は全て `git` の
argv 直呼びで、`sh -c` もハードコード絶対パスも無いことを確認した（対応不要）。

### 11.4 §9.9 受入条件表の再測定

| 確認対象 | 合格条件 | macOS 実測 | Windows |
|---|---|---|---|
| Python 解決 | alias ではなく実体の 3.12+ が起動する | 合格（`CLAQ_PYTHON` / `python3` / `python`(3.12+ 検証) の順で解決） | **U** |
| launcher | stderr に解決エラーが無く対象 module まで到達する | 合格 | **U** |
| allow payload | `git status` が許可される | 合格（exit 0、出力なし） | **U** |
| deny payload | `git commit --no-verify` が非 0 または deny JSON | 合格（exit 2 + `permissionDecision: deny`） | **U** |
| 入力異常 | 空入力・壊れた JSON・pipe 読取例外を許可側へ倒さない | 読取例外／到着なしは deny（実測）。**壊れた JSON は従来どおり deny。ただし「payload が無い」（tty・stdin 未接続・即 EOF）は素通りのまま**（保護フックの失敗方向 の判断。payload を渡さない host を全面拒否すると復旧不能になるため） | **U** |
| timeout | 保護対象を `allowing the tool call to proceed` にしない | ホスト側挙動のため不可。11.2 の緩和のみ | **U** |

### 11.5 残る「未検証だが全体が依存する」前提

**cmd.exe が、引用符付き・拡張子なし・区切り混在のパス
（`"C:\Users\...\claq\claq/runtime/claq-hook"`）を PATHEXT で
`claq-hook.cmd` へ解決すること。** hooks.json は 1 エントリにつき 1 つの
コマンド文字列しか持てず、3 OS で正しい裸のインタプリタ名は存在しないため、
この解決に賭けている（POSIX 側は拡張子なしファイルを直接 exec するので
確実。この非対称のため、拡張子なしを hooks.json に書く方が
`.cmd` を書くより POSIX 側で安全と判断した）。

**この前提が誤っていた場合の症状は、修正前とバイト単位で同一**
（`Denied by preToolUse hook from "claq@claq" (hook errored)`）になる。
「修正が効かなかった」ではなく「この 1 つの前提が外れた」と切り分けられる
よう、復旧手順を明記する:

1. `hooks.json` の 7 エントリのパスを `.../runtime/claq-hook` から
   `.../runtime/claq-hook.cmd` へ変える。
2. その場合 POSIX 側も同じ名前を exec することになるため、
   `runtime/claq-hook.cmd` を POSIX からも実行できる形（sh/batch
   ポリグロット、実行ビット付き）にするか、host ごとに別 `hooks.json` を
   配布する必要がある。
3. 切り分けだけなら、Windows で `CLAQ_PYTHON` に実体 Python の絶対パスを
   設定しても解決しない（wrapper 自体が起動していないため）。
   ホストのプロセスログに `'...claq-hook' is not recognized` 系の
   stderr が出ていれば、この前提が外れたと確定できる。

### 11.6 成果物

| 項目 | 内容 |
|---|---|
| コミット | v0.9.50 まで: `872ac87` / `c875903` / `1509c6a` / `11655bc` / `fd43487` / `6e0599f` / `81143e9`。再レビュー分: `71b33a1`（W1〜W6） / `1e34db8`（N-03・N-04） / `753e24f`（detach 子の UTF-8・縮退経路の大小無視） |
| 新規 ADR | 保護フックの失敗方向（保護フックは stdin を「読めなかった」場合に fail-closed する）・シェルコマンド解析の境界（シェル保護フックは 1 つのコマンド文字列を 2 つのシェル方言で解析する） |
| 新規配布物 | `runtime/claq-hook`（100755）・`runtime/claq-hook.cmd`。publish は git filter-repo の除外方式なので自動的に配布ツリーへ載り、実行ビットも保持される |
| テスト | 2546 件成功、`ruff check plugins/claq` 警告なし、カバレッジ 100% |
| 判定 | **macOS/Linux: 回帰なし。Windows: 実機再検証待ち（11.5 の前提を最初に確認すること）** |

### 11.7 公開済み v0.9.50 の位置づけ（訂正）

v0.9.50 を公開したあとの再レビューで **W1** を検出した。したがって v0.9.50 の
状態は「Windows 未検証」ではなく、**特定の形で Windows において壊れている**:

- `claq-hook.cmd` が python の終了コードではなく起動前の errorlevel を返すため、
  **保護 hook の deny（exit 2）が全て allow（0）として host へ報告される**。
  `block_no_verify` / `config_protection` / `bash_config_protection` /
  `pre_bash_commit_quality` の 4 つが Windows で無効化される。
- macOS / Linux は影響を受けない（`.cmd` は実行されない）。
- SessionStart の記憶注入・handoff など、deny を返さない hook は v0.9.50 でも
  意図どおり動く（N-03 の非 UTF-8 ロケール条件を除く）。

v0.9.50 を Windows で導入した環境は、W1・N-03 を含む次版へ更新すること。
