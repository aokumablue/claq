# ple4 0.9.48 Windows 実起動検証レポート

## 1. 検証概要

- 検証日時: 2026-09-03 11:12 JST
- OS: Windows
- 作業ディレクトリ: `C:\Users\tasaki-mamoru\works`
- 作業ディレクトリは Git リポジトリではない
- 対象配布物: `C:\Users\tasaki-mamoru\.copilot\installed-plugins\ple4\ple4`
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
Denied by preToolUse hook from "ple4@ple4" (hook errored)
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
  `Denied by preToolUse hook from "ple4@ple4" (hook errored)` で停止する。
- **再現方法**:
  1. Windows で ple4 を有効にした Copilot CLI セッションを開始する。
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

- **現象**: 全 hook が `python3 "${CLAUDE_PLUGIN_ROOT}/src/ple4/launcher.py"`
  形式に固定されている。
- **再現方法**: Windows native で `python3` が PATH にない、または
  `${CLAUDE_PLUGIN_ROOT}` をホストが展開しない条件で hook を発火させる。
- **影響**: hook 起動失敗または host 側 hook error になる。P0-001 の直接原因候補。
- **修正案**: Windows 用 `.cmd`/`.ps1` wrapper、`sys.executable` を使う起動、
  ホスト別 command 生成を追加し、インストール後 smoke test で展開を検証する。
- **状態**: Windows環境で `python3` がStore aliasへ解決されることを実測。
  hooks.jsonの起動設計欠陥を確認。確度 10/10。

### P1-003: runtime の POSIX 専用 bootstrap（静的検出）

- **現象**: `env-template.sh` と `ple4-helpers.sh` が `source`、`ps`、`tr`、
  `nohup`、`grep`、`sort`、`head`、`python3` 等を要求する。
- **再現方法**: PowerShell のみの Windows 環境で
  `. "$HOME/.ple4/env.sh"` と各 helper を実行する。
- **影響**: root pointer、`ple4_run`、memory helper が構築されない。
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

- **現象**: `find_plugin_install()` が `.claude/plugins/ple4` と `HOME` を主に
  探索し、実際の `.copilot/installed-plugins/ple4/ple4` と `USERPROFILE` を
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
実際のPython 3.14.7へ到達していないことが確認できる。ple4の要求する
Python 3.12以上は満たしているため、現在の環境では `python.exe` の明示指定、
または `python3` を実体へ向けるwrapperが必要である。

### 9.2 再現した現象

フックが有効だった時点では、無害な操作を含む複数のツール呼び出しが、処理の
開始前に次のエラーで停止した。

```text
Denied by preToolUse hook from "ple4@ple4" (hook errored)
```

Copilotのプロセスログ
`C:\Users\tasaki-mamoru\.copilot\logs\process-1788430211522-14096.log`
には、次の直接証拠がある。

```text
Hook from "ple4@ple4" execution failed: Error: Hook command failed with code 1
Stderr: Python was not found; run without arguments to install from the Microsoft Store, or disable this shortcut from Settings > Apps > Advanced app settings > App execution aliases.
```

この組み合わせは同ログの135-136行および153-154行にあり、別の過去ログにも
同じ標準エラーが反復している。

再現時の処理順は次のとおりである。

```text
CopilotがPreToolUseを発火
  -> ple4のhooks.jsonからcommandを取得
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
| ple4 | 必須 | `ple4@ple4` が有効で、hook定義がロードされている |
| hook command | 必須 | `python3 "${CLAUDE_PLUGIN_ROOT}/src/ple4/launcher.py" ...` |
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
2. `hooks.json` が存在するple4を有効にして、Copilot CLIを起動する。
3. Bash/shell tool、またはhook matcherに該当するEdit/Write toolを呼び出す。
4. 次のcommandが実行されることを確認する。

   ```text
   python3 "C:\Users\<user>\.copilot\installed-plugins\ple4\ple4/src/ple4/launcher.py" <module>
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
preToolUse hook from "ple4@ple4" timed out; allowing the tool call to proceed:
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
2. **ple4側の原因（設計バグ）**: `hooks.json` の全7 hookが、検証済み絶対パス
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
`C:\Users\tasaki-mamoru\.copilot\installed-plugins\ple4\ple4/src/ple4/...`
のような展開済みパスが表示されている。そのため、今回の主原因は
`${CLAUDE_PLUGIN_ROOT}` の未展開ではない。`\`と`/`の混在は、Python解決後に
別のWindows互換性問題を起こす可能性があるため、独立して修正する。

### 9.6 修正方法

#### P0: Windows用の確実な起動経路を配布する

1. `hooks.json` へ `python3` を直接記述せず、Windows用
   `runtime\ple4-hook.cmd` または同等のwrapperを指定する。
2. wrapperは、次の順でPythonを解決する。
   - インストール時に検証・固定したPython 3.12以上の絶対パス
   - ple4専用venvの `python.exe`
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
    -> runtime\ple4-hook.cmd
      -> 検証済み Python 3.12+ の絶対パス
        -> src\ple4\launcher.py

POSIX:
  hooks.json
    -> 検証済み python3
      -> src/ple4/launcher.py
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
