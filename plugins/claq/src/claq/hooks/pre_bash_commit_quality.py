"""コミット前にステージ済みファイルの品質を確認します。

pre:bash で `git commit` を検出したときだけ、lint や簡易静的チェックを実行します。
問題が見つかった場合はコミットを止め、それ以外は入力をそのまま通過させます。

commit 検出と複合コマンドの判定は `commit_risk`、メッセージ検証は `commit_message` が持ち、
検出は `block_no_verify` と共有する区切り記号対応トークナイザ
（`shell_tokens.tokenize` / `split_segments`）で行い、`git` のグローバルオプションは
値の有無・既知/未知を問わず読み飛ばします。`git commit -a` / `--all` / 結合短形式
（`-am`）では、未ステージ変更ファイルを**作業ツリーから**読んでスキャン対象へ
加えます（`-a` は作業ツリーの内容をコミットするため）。ステージ済みファイルは
INDEX（`git show :0:path`）から読みます。

走査はフック起動時の cwd の index に対して行うため、commit が別の index を記録しうる形
（commit セグメントの ``-C`` / ``--git-dir`` / ``--work-tree`` / ``core.worktree`` / wrapper の
``--chdir``、``GIT_DIR`` / ``GIT_WORK_TREE`` / ``GIT_INDEX_FILE`` / ``GIT_CONFIG*`` の指定、
commit より前の ``cd`` / ``pushd`` 等）は
実行時状態として判定を放棄して deny します（docs/adr/shell-analysis-boundary.md）。

シークレット検出はファイルサイズに関わらず全体を走査します。バイナリ判定された
ファイルは lint を抑制しますが secret scan は行います（docs/adr/shell-analysis-boundary.md。
詳細は `commit_quality_scanner` のモジュール docstring）。

データとして書かれた heredoc 本文は `evaluate()` のループで 1 回だけ
`heredoc.strip_data_heredoc_bodies` で落とし、下流の commit 判定・compound risk
判定・確定後の検査が必ず同じ文字列を見るようにします。

非目標: ラッパースクリプトやシェルエイリアス経由の `git commit` 呼び出し検出、
非シェルインタプリタ（`python3 - <<EOF`）の heredoc 本文からの間接実行、
`-a`/`--all` を伴わない `git commit <pathspec>` で明示指定された未ステージファイルの
取り込み、シェル展開・変数分割経由（`git $(echo commit)` / `git${IFS}commit` 等）で
`git` と `commit` が生文字列上で隣接しない形の検出（POSIX シェル展開の模倣は原理的に
不能で、ヒューリスティックの追加は過剰ブロックを招くため）。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from claq.hooks.commit_message import validate_commit_message
from claq.hooks.commit_quality_scanner import (
    find_file_issues,
    new_secret_scan_deadline,
    sanitize_untrusted_path,
    should_lint_file,
    should_scan_secrets,
)
from claq.hooks.commit_risk import (
    MULTIPLE_COMMITS_MESSAGE,
    compound_commit_risk,
    find_git_commit_args_in_segment,
    is_commit_all_flag,
    parse_git_commit_command,
    with_shell_wrapper_segments,
)
from claq.hooks.heredoc import command_readings, strip_data_heredoc_bodies
from claq.hooks.hook_common import (
    DIRECTORY_CHANGE_COMMANDS,
    emit_block_output,
    resolve_repo_root,
)
from claq.hooks.shell_tokens import (
    command_exceeds_scan_budget,
    extract_shell_wrapper_command,
    is_shell_wrapper_token,
    normalize_executable_name,
    scan_budget_message,
    split_segments,
    tokenize,
)
from claq.hooks.stdin_io import (
    StdinUnavailableError,
    parse_json_object,
    read_raw_stdin_with_truncation,
    stdin_unreadable_message,
    truncated_input_message,
)
from claq.lib.core_utils import log
from claq.lib.harness import iter_bash_command_groups
from claq.lib.subprocess_utils import run_text

# raw stdin が JSON として壊れているが、生文字列上に git commit が見える場合の
# deny 理由。matcher は Bash 全体なので、malformed というだけでは deny しない。
_MALFORMED_COMMIT_MESSAGE = (
    "[Hook] BLOCKED: hook input could not be parsed as JSON, but the raw "
    "command text matches `git commit`. Refusing to allow an unverifiable "
    "commit through rather than silently skipping the quality scan."
)

# commit と確定した後の想定外の例外。検査未完了のまま通さず fail-closed にする。
_SCAN_FAILURE_MESSAGE = (
    "[Hook] BLOCKED: the commit quality scan failed unexpectedly for a "
    "confirmed `git commit` call. Refusing to allow an unverifiable commit "
    "through."
)

# git 自体の失敗・timeout でステージ済みファイルを取得できなかった場合。
# 「0 件」（--allow-empty 等の正常系。exit 0）とは区別する。
_STAGED_FILES_UNAVAILABLE_MESSAGE = (
    "[Hook] BLOCKED: could not determine staged files for a confirmed "
    "`git commit` call (git itself failed or timed out). Refusing to allow "
    "an unverifiable commit through."
)

# `git commit -a` で作業ツリーを読む必要があるのに repo root を解決できなかった場合。
# 実際にコミットされる未ステージ変更を検査できないまま「問題なし」を返さない。
_REPO_ROOT_UNAVAILABLE_MESSAGE = (
    "[Hook] BLOCKED: could not resolve the repo root to scan worktree changes "
    "for a confirmed `git commit -a` call (git itself failed or timed out). "
    "Refusing to allow an unverifiable commit through."
)

# `git commit -a` の作業ツリー列挙が git 自体の失敗・timeout で完了しなかった場合。
# HEAD が無い初回コミット（正常系。空リストで続行）とは区別する
# （docs/adr/hook-failure-direction.md）。
_WORKTREE_FILES_UNAVAILABLE_MESSAGE = (
    "[Hook] BLOCKED: could not enumerate worktree changes for a confirmed "
    "`git commit -a` call (git itself failed or timed out). Refusing to allow "
    "an unverifiable commit through."
)

# 走査コストが hooks.json の timeout を超える入力（`shell_tokens.MAX_COMMAND_BYTES`）。
_SCAN_BUDGET_MESSAGE = scan_budget_message("pre:bash-commit-quality", "commit")

# stdin が MAX_STDIN_BYTES を超えて切り捨てられた場合。切り捨て後の JSON では
# commit かどうかの判定自体が信用できないため fail-closed にする。
_TRUNCATED_INPUT_MESSAGE = truncated_input_message("pre:bash-commit-quality", "payload for a git commit quality scan")

# commit が、フックの走査した cwd の index とは別の index を記録しうる場合。
# 実行時状態（cd 後の cwd・環境変数・git の設定）は追跡せず deny する。
_COMMIT_TARGET_UNVERIFIABLE_MESSAGE = (
    "[Hook] BLOCKED: this `git commit` may record a different repository or index than the "
    "one this hook scans (the current directory's index). It points git elsewhere (`git -C` / "
    "`--git-dir` / `--work-tree` / `core.worktree`, a GIT_DIR / GIT_WORK_TREE / GIT_INDEX_FILE / "
    "GIT_CONFIG* variable, or a wrapper's `--chdir`) or changes directory (`cd` / `pushd`) before "
    "the commit in the same call. Change into that directory first, then run the commit in a "
    "separate tool call without these options or variables."
)

# 別の index / 作業ツリー / 設定を指す環境変数。代入（``NAME=`` / ``NAME+=`` / PowerShell の
# ``$env:NAME=``）に加え、名前だけの指定（``export NAME`` / ``read NAME`` / ``printf -v NAME``）も
# 拾う。``GIT_CONFIG*``（``GIT_CONFIG_COUNT`` / ``GIT_CONFIG_KEY_n`` / ``GIT_CONFIG_PARAMETERS`` /
# ``GIT_CONFIG_GLOBAL`` 等）は ``core.worktree`` を外から与えられる。PowerShell は名前の大小を
# 区別しないため大小無視で照合する。
_REPOSITORY_ENV_RE = re.compile(
    r"^(?:\$env:)?(?:GIT_DIR|GIT_WORK_TREE|GIT_INDEX_FILE|GIT_CONFIG\w*)(?:\+?=|$)", re.IGNORECASE
)

# 別のリポジトリ・作業ツリーへ移る git のグローバルオプション（``-C <dir>`` /
# ``--git-dir[=]<dir>`` / ``--work-tree[=]<dir>``）と、wrapper の作業ディレクトリ指定
# （``env -C`` / ``env --chdir`` / ``sudo -D`` / ``sudo --chdir``）。commit セグメントの commit より
# 前だけで照合する（commit 自身の ``-C <commit>`` はメッセージの再利用で、対象 index を変えない）。
# 大小を区別する（``git -c`` は設定の指定で、別に ``core.worktree`` だけを見る）。
_REPOSITORY_OPTION_RE = re.compile(r"^(?:-C|-D|--chdir(?:=|$)|--git-dir(?:=|$)|--work-tree(?:=|$))")

# ``--work-tree`` と同じ効果を持つ設定 key。``git -c`` / ``--config-env`` の値や ``git config`` の
# 引数として現れる。git の key は大小無視なので畳んでから部分一致で見る。
_CORE_WORKTREE_KEY = "core.worktree"


def _points_git_elsewhere(token: str) -> bool:
    """トークンが別の index / 作業ツリーを指す環境変数か ``core.worktree`` の指定かを判定する。

    Args:
        token: 1 トークン。

    Returns:
        該当すれば True を返します。

    Raises:
        例外は発生しません。
    """
    return bool(_REPOSITORY_ENV_RE.match(token)) or _CORE_WORKTREE_KEY in token.casefold()


def _shell_wrapper_moves_repository(segment: list[str]) -> bool:
    """内側に commit を持つ ``sh -c`` のセグメントが、シェル起動より前で別の場所を指すかを判定する。

    ``env -C ../other bash -c 'git commit -m x'`` の ``-C`` や ``GIT_DIR=x bash -c ...`` の代入は内側の
    git の対象を変える。``sh -c`` でないセグメントの ``-C`` は git と無関係（``grep -C 3``）なので見ない。
    `with_shell_wrapper_segments` は内側を先に並べるので、外側はここで commit より先に見る。

    Args:
        segment: 1 セグメント分のトークン列。

    Returns:
        該当すれば True を返します。

    Raises:
        例外は発生しません。
    """
    wrapped = extract_shell_wrapper_command(segment)
    if wrapped is None or not parse_git_commit_command(wrapped, shell_wrappers=False)[0]:
        return False
    shell = next(index for index, token in enumerate(segment) if is_shell_wrapper_token(token))
    return any(_REPOSITORY_OPTION_RE.match(token) or _points_git_elsewhere(token) for token in segment[:shell])


def _commit_target_risk(command: str) -> str | None:
    """commit がフックの走査した index と別の index を記録しうるなら deny 理由を返す。

    commit セグメントでは ``commit`` より前のトークン（代入・wrapper・``git``・グローバル
    オプション）を見る。commit より前のセグメントでは全トークンを走査し、``cd`` 系の名前・
    環境変数・``core.worktree`` が現れたら deny する。実行位置で照合すると ``{ cd x; …; }`` /
    ``if cd x; then …`` / ``builtin cd x`` の ``cd`` を見落とすため、`commit_risk` の変更判定と
    同じく誤検出側の全トークン走査にする（``echo cd`` も deny する）。commit より後ろの
    セグメントは、その commit の対象を変えないため見ない。``sh -c`` の 1 段の内側のセグメントも
    commit の検出と同じ範囲で読む。

    Args:
        command: `git commit` を含む bash コマンド文字列（heredoc 本文は除去済み）です。

    Returns:
        deny すべき場合は `_COMMIT_TARGET_UNVERIFIABLE_MESSAGE`、問題なければ None を返します。

    Raises:
        例外は発生しません。
    """
    segments = split_segments(tokenize(command))
    if any(_shell_wrapper_moves_repository(segment) for segment in segments):
        return _COMMIT_TARGET_UNVERIFIABLE_MESSAGE
    for segment in with_shell_wrapper_segments(segments):
        commit_args = find_git_commit_args_in_segment(segment)
        if commit_args is None:
            if any(
                normalize_executable_name(token) in DIRECTORY_CHANGE_COMMANDS or _points_git_elsewhere(token)
                for token in segment
            ):
                return _COMMIT_TARGET_UNVERIFIABLE_MESSAGE
            continue
        before_commit = segment[: len(segment) - len(commit_args) - 1]
        if any(_points_git_elsewhere(token) or _REPOSITORY_OPTION_RE.match(token) for token in before_commit):
            return _COMMIT_TARGET_UNVERIFIABLE_MESSAGE
        return None
    return None


def _git_name_only(git_args: list[str]) -> list[str] | None:
    """`git ... --name-only` の出力を非空行リストにする。

    「ファイル 0 件」（`[]`）と「git 自体が失敗・timeout した」（`None`）を区別する。
    同じ `[]` に潰すと、呼び出し側が「検査不能」を無言で見逃す。

    `run_text` 経由で UTF-8（errors="replace"）にデコードする。`core.quotePath=false`
    の環境では git が非 ASCII のファイル名を生バイトで返し、locale 依存のデコードでは
    `UnicodeDecodeError` が except を貫通してフックが落ち、commit が無検査で通る。
    置換されたファイル名の 1 件はスキャンから外れるが、残りは検査される。

    `check=False` なので `CalledProcessError` は送出されない。`OSError` で `PermissionError`
    等の git を起動できない場合も捕まえる。`TimeoutExpired` は `OSError` の部分集合ではないため
    個別に受ける。

    Args:
        git_args: subprocess に渡す git コマンド列。

    Returns:
        ファイルパスのリスト。git 自体の失敗・timeout 時は None。

    Raises:
        例外は発生しません。
    """
    try:
        result = run_text(git_args, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return [path for path in result.stdout.strip().split("\n") if path]


def get_staged_files() -> list[str] | None:
    """ステージング済みファイルの一覧を取得します。

    Returns:
        ステージングされたファイルパスのリストを返します。git 自体の
        失敗・timeout で取得できない場合は None（「0 件」とは区別する）。

    Raises:
        例外は発生しません。
    """
    return _git_name_only(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMRT"])


def _head_exists() -> bool | None:
    """HEAD が解決可能か（＝初回コミットではないか）を判定します。

    `_git_name_only` と同じ理由で `run_text` 経由にする。returncode しか読まないが、
    stderr もデコードされるため、非 UTF-8 ロケールの翻訳済みエラーメッセージで
    同じ `UnicodeDecodeError` 貫通が起きる。

    Returns:
        HEAD があれば True、初回コミット等で無ければ False。git 自体が
        失敗・timeout して判定できなければ None。

    Raises:
        例外は発生しません。
    """
    try:
        result = run_text(["git", "rev-parse", "--verify", "--quiet", "HEAD"], timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        return True
    # --quiet 指定時、HEAD が単に存在しない場合の終了コードは 1。
    # それ以外（リポジトリ外・git 内部エラー等）は「判定不能」として扱う。
    return False if result.returncode == 1 else None


def get_unstaged_modified_files() -> list[str] | None:
    """`git commit -a` 相当で追加取り込む作業ツリーの変更ファイル一覧を取得します。

    `git diff HEAD --name-only --diff-filter=ACMRT` の結果を返す。列挙の失敗は
    「HEAD が無い（初回コミット）」なら正常系の空リスト、「git 自体が失敗した」なら
    None にして呼び出し元で fail-closed にする（docs/adr/hook-failure-direction.md）。

    Returns:
        変更されている作業ツリーファイルパスのリスト。初回コミットで HEAD が
        無い場合は空リスト。git 自体の失敗・timeout で取得できない場合は None。

    Raises:
        例外は発生しません。
    """
    result = _git_name_only(["git", "diff", "HEAD", "--name-only", "--diff-filter=ACMRT"])
    if result is not None:
        return result
    return [] if _head_exists() is False else None


def _detect_git_commit(command: str, *, shell_wrappers: bool = True) -> tuple[str, list[str]] | None:
    """`command_readings` の各読み方で `git commit` 起動を探す。

    POSIX 読みだけだと ``C:\\Git\\bin\\git.exe commit -m x`` が ``C:Gitbingit.exe`` に
    潰れ、`block_no_verify` では検出されるのに品質ゲートだけ素通りする
    （docs/adr/shell-analysis-boundary.md）。

    Args:
        command: 検査対象のコマンド文字列（heredoc 本文は除去済み）。
        shell_wrappers: ``sh -c`` の 1 段の内側も読むなら True。

    Returns:
        commit を検出した読み方と、その commit 引数のタプル。
        どちらの読み方でも commit でなければ None。

    Raises:
        例外は発生しません（`parse_git_commit_command` の契約に従う）。
    """
    for variant in command_readings(command):
        is_commit, commit_args = parse_git_commit_command(variant, shell_wrappers=shell_wrappers)
        if is_commit:
            return variant, commit_args
    return None


def _scan_targets(files: list[str]) -> list[str]:
    """lint または secret スキャン対象のファイルだけを残す。

    Args:
        files: 候補ファイルパス。

    Returns:
        スキャン対象のファイルパス。

    Raises:
        例外は発生しません。
    """
    return [path for path in files if should_lint_file(path) or should_scan_secrets(path)]


def _partition_commit_all_files(
    staged_files: list[str], commit_args: list[str]
) -> tuple[list[str], list[str]] | None:
    """`-a`/`--all` 指定時に、INDEX から読む対象と作業ツリーから読む対象に分割します。

    `git commit -a` は作業ツリーの現在の内容をコミットするため、ステージ後に作業ツリーで
    さらに変更されたファイルも含めて、未ステージ変更ファイルは作業ツリー優先で読む。

    Args:
        staged_files: `get_staged_files()` によるステージ済みファイル一覧です。
        commit_args: `git commit` 呼び出しの引数トークン列です。

    Returns:
        (index_files, worktree_files) のタプルです。`-a`/`--all` が無ければ
        `worktree_files` は空リストです。`-a` 指定時に作業ツリーの列挙自体が
        失敗した場合は None を返します（呼び出し元で fail-closed にする）。

    Raises:
        例外は発生しません。
    """
    if not is_commit_all_flag(commit_args):
        return list(staged_files), []

    unstaged = get_unstaged_modified_files()
    if unstaged is None:
        return None

    worktree_files = sorted(set(unstaged))
    return sorted(set(staged_files) - set(worktree_files)), worktree_files


def _count_file_issues(
    files_to_check: list[str], repo_root: Path | None = None, *, deadline: float
) -> tuple[int, int, int, int]:
    """チェック対象ファイルの問題数を severity 別に集計し、ログへ出します。

    Args:
        files_to_check: チェック対象のファイルパスリストです。
        repo_root: 指定すると各ファイルを作業ツリーから読みます
            （`git commit -a` の未ステージ変更用）。None なら INDEX から
            読みます。
        deadline: secret scan の実時間予算を表す時刻です。本関数は INDEX 用・
            作業ツリー用の 2 回呼ばれるため、ここで作ると予算が 2 本になる。
            `_evaluate_confirmed_commit` が起動ごとに 1 度だけ作った値を渡す。

    Returns:
        (total_issues, error_count, warning_count, info_count) のタプルを返します。

    Raises:
        例外は発生しません。
    """
    severity_label = {"error": "ERROR", "warning": "WARNING", "info": "INFO"}
    counts = dict.fromkeys(severity_label, 0)

    for file_path in files_to_check:
        file_issues = find_file_issues(file_path, repo_root=repo_root, deadline=deadline)
        if not file_issues:
            continue
        log(f"\n[FILE] {sanitize_untrusted_path(file_path)}")
        for issue in file_issues:
            severity = issue["severity"]
            log(f"  {severity_label[severity]} Line {issue['line']}: {issue['message']}")
            counts[severity] += 1

    return sum(counts.values()), counts["error"], counts["warning"], counts["info"]


def _log_commit_message_issues(command: str) -> int:
    """コミットメッセージの問題を検証してログへ出し、その件数を返します。

    Args:
        command: `git commit` コマンド文字列です。

    Returns:
        メッセージの問題の件数を返します。すべて警告として数える件数です。

    Raises:
        例外は発生しません。
    """
    message_validation = validate_commit_message(command)
    if not (message_validation and message_validation["issues"]):
        return 0

    issues = message_validation["issues"]
    log("\nCommit Message Issues:")
    for issue in issues:
        log(f"  WARNING {issue['message']}")
        if issue.get("suggestion"):
            log(f"     TIP {issue['suggestion']}")

    return len(issues)


def _finalize_result(
    total_issues: int,
    error_count: int,
    warning_count: int,
    info_count: int,
) -> str | None:
    """問題集計結果をログに記録し、ブロック理由を返します。

    Args:
        total_issues: 検出された問題の総数です。
        error_count: エラー severity の問題数です。
        warning_count: 警告 severity の問題数です。
        info_count: info severity の問題数です。

    Returns:
        error_count > 0 ならブロック理由、それ以外は None。

    Raises:
        例外は発生しません。
    """
    if total_issues == 0:
        log("\n[Hook] PASS: All checks passed!")
        return None

    log(
        f"\nSummary: {total_issues} issue(s) found "
        f"({error_count} error(s), {warning_count} warning(s), {info_count} info)"
    )
    if error_count > 0:
        log("\n[Hook] ERROR: Commit blocked due to critical issues. Fix them before committing.")
        return f"[Hook] BLOCKED: {error_count} error(s) found in staged files. Fix them before committing."
    log("\n[Hook] WARNING: Warnings found. Consider fixing them, but commit is allowed.")
    return None


def _collect_worktree_issues(worktree_targets: list[str], *, deadline: float) -> tuple[int, int, int, int] | None:
    """`git commit -a` の作業ツリー対象ファイルの問題数を集計します。

    作業ツリー対象があるのに repo root を解決できない場合は None を返し、呼び出し側で
    fail-closed にさせる。実際にコミットされる未ステージ変更を検査できないまま
    「問題なし」を返さないため（ステージ済み側の git 失敗と対称）。

    Args:
        worktree_targets: 作業ツリーから読むチェック対象ファイルパスです。
        deadline: INDEX 側の集計と共有する secret scan の実時間予算です。

    Returns:
        (total_issues, error_count, warning_count, info_count)。対象が無ければすべて 0。
        repo root が必要なのに解決できなければ None。

    Raises:
        例外は発生しません。
    """
    if not worktree_targets:
        return 0, 0, 0, 0

    repo_root = resolve_repo_root()
    if repo_root is None:
        return None
    return _count_file_issues(worktree_targets, repo_root=repo_root, deadline=deadline)


def _evaluate_confirmed_commit(command: str, commit_args: list[str]) -> str | None:
    """`git commit` と判定済みの入力を検査し、ブロック理由を返します。

    commit であることは確定しているため、想定外の例外が起きても非ブロッキングへは
    倒さない。検査未完了のまま通すと「検査したが問題なし」と区別が付かず、品質・
    secret 検査を無言で迂回できてしまう。

    Args:
        command: `git commit` を含む bash コマンド文字列です。
        commit_args: commit 呼び出しの引数トークン列です。

    Returns:
        ブロックする場合は理由、通す場合は None。

    Raises:
        例外は発生しません。
    """
    try:
        staged_files = get_staged_files()
        if staged_files is None:
            log("[Hook] ERROR: could not determine staged files (git itself failed or timed out).")
            return _STAGED_FILES_UNAVAILABLE_MESSAGE

        partitioned = _partition_commit_all_files(staged_files, commit_args)
        if partitioned is None:
            log("[Hook] ERROR: could not enumerate worktree changes for `git commit -a`.")
            return _WORKTREE_FILES_UNAVAILABLE_MESSAGE

        index_files, worktree_files = partitioned
        all_files = sorted(set(index_files) | set(worktree_files))
        if not all_files:
            log('[Hook] No staged files found. Use "git add" to stage files first.')
            return None

        log(f"[Hook] Checking {len(all_files)} staged file(s)...")

        index_targets = _scan_targets(index_files)
        worktree_targets = _scan_targets(worktree_files)

        # secret scan の実時間予算はフック 1 回の起動につき 1 本。ファイル単位や集計単位で
        # 作ると、その数だけの実時間を許して hook timeout に達しうる。
        secret_scan_deadline = new_secret_scan_deadline()

        index_counts = _count_file_issues(index_targets, deadline=secret_scan_deadline)
        worktree_counts = _collect_worktree_issues(worktree_targets, deadline=secret_scan_deadline)
        if worktree_counts is None:
            log("[Hook] ERROR: could not resolve repo root for `git commit -a` worktree scan.")
            return _REPO_ROOT_UNAVAILABLE_MESSAGE
        total_issues, error_count, warning_count, info_count = (
            a + b for a, b in zip(index_counts, worktree_counts, strict=True)
        )

        message_warnings = _log_commit_message_issues(command)

        return _finalize_result(
            total_issues + message_warnings, error_count, warning_count + message_warnings, info_count
        )

    except Exception as err:
        log(f"[Hook] Error: {err}")
        log("[Hook] BLOCKED: quality scan failed for a confirmed git commit; refusing an unverifiable commit.")
        return _SCAN_FAILURE_MESSAGE


def _detect_group_commits(group: tuple[str, ...]) -> list[tuple[str, list[str]]]:
    """1 グループ（1 回の呼び出しを表す候補文字列の組）から `git commit` 起動を集める。

    各候補を個別に検出する。argv 配列のグループ（長さ 2 以上。末尾が結合文字列）では、
    結合候補が commit を検出し、要素側の検出が 1 件以下なら結合候補 1 件へ畳む。
    ``["git", "commit", "-m", "fix: git commit handling"]`` は要素にも結合にも commit が
    見え、そのまま数えると 1 回の commit が「複数 commit」になる。要素側の検出が 2 件以上
    （``["git commit -m a", "git commit -m b"]`` のようなコマンド文字列の配列）なら別々の
    commit なので要素をそのまま数える。結合候補が検出しない場合も要素をそのまま数える
    （``["bash", "-lc", "git commit -m x"]`` の commit は要素側でしか見えない）。
    結合候補だけは ``sh -c`` の内側を読まない。``shlex.join`` が足した引用を経た内側の commit
    引数はメッセージの検証に使えず、同じ commit は要素側が元の文字列のまま検出する。

    Args:
        group: ``iter_bash_command_groups`` が返すグループの各候補から、heredoc のデータ本文を
            落とした組です。

    Returns:
        検出した commit のリスト。

    Raises:
        例外は発生しません（候補ごとの判定失敗は記録して読み飛ばす）。
    """
    has_joined = len(group) > 1
    detections: list[tuple[str, list[str]] | None] = []
    for index, command in enumerate(group):
        is_joined = has_joined and index == len(group) - 1
        try:
            detections.append(_detect_git_commit(command, shell_wrappers=not is_joined))
        except Exception as err:  # noqa: BLE001 - 1 候補の失敗で他を落とさない
            log(f"[Hook] Error: {err}")
            detections.append(None)
    joined = detections.pop() if has_joined else None
    elements = [detected for detected in detections if detected is not None]
    if joined is not None and len(elements) <= 1:
        return [joined]
    return elements


def _collect_commits(groups: list[tuple[str, ...]]) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """全グループの commit と、commit を含む argv グループの結合候補を集める。

    argv の要素で検出した commit の外側（``env GIT_DIR=x`` 等）は結合候補にしか見えない。

    Args:
        groups: heredoc のデータ本文を落としたグループのリスト。

    Returns:
        (検出した commit のリスト, commit を含む argv グループの結合候補のリスト)。

    Raises:
        例外は発生しません（`_detect_group_commits` の契約に従う）。
    """
    commits: list[tuple[str, list[str]]] = []
    joined_commands: list[str] = []
    for group in groups:
        detected = _detect_group_commits(group)
        commits.extend(detected)
        if detected and len(group) > 1:
            joined_commands.append(group[-1])
    return commits, joined_commands


def _malformed_input_reason(raw_input: str) -> str | None:
    """JSON として読めなかった入力のブロック理由を返します。

    matcher は Bash 全体なので、読めないというだけでは deny しない。生文字列上に `git commit` が
    見える場合だけ、malformed JSON を理由に検査を迂回させないため deny する。

    Args:
        raw_input: JSON object として読めなかった生の入力文字列です。

    Returns:
        生文字列上に `git commit` が見えれば `_MALFORMED_COMMIT_MESSAGE`、それ以外は None。

    Raises:
        例外は発生しません。
    """
    if not raw_input.strip():
        return None
    if re.search(r"\bgit\s+commit\b", raw_input):
        log("[Hook] ERROR: malformed JSON input, but raw command text matches `git commit`.")
        return _MALFORMED_COMMIT_MESSAGE
    log("[Hook] WARNING: could not parse hook input as JSON; not a git commit, passing through.")
    return None


def evaluate(raw_input: str) -> str | None:
    """入力を評価し、ブロック理由を返します。

    JSON が壊れている・commit と無関係な Bash 呼び出しは非ブロッキング（None）。
    JSON が壊れていても raw 文字列上に `git commit` が見える場合は、malformed JSON を
    理由に検査を迂回させないため deny する。commit と確定した入力は
    `_evaluate_confirmed_commit` に委譲し、以降は fail-closed で扱う。

    Args:
        raw_input: フックに渡された生の入力文字列です。

    Returns:
        ブロックする場合は理由、通す場合は None。

    Raises:
        例外は発生しません。
    """
    try:
        payload = parse_json_object(raw_input)
        if payload is None:
            return _malformed_input_reason(raw_input)

        # コンテナキーは 1 つも取りこぼさず走査し、commit を**全て**集める。先に判定できた
        # コンテナの結論を返すと、先頭が「通る commit」のときに後続キーの commit が
        # 検査されない。
        groups = [
            tuple(strip_data_heredoc_bodies(command) for command in group)
            for group in iter_bash_command_groups(payload)
        ]
        # 予算は全候補を走査する前に検査する。候補ごとに走査と交互に検査すると、上限内の要素を
        # 並べた argv 配列で要素の走査が積み上がり、上限を超える結合候補に着く前に timeout に
        # 近づく（4,999 トークンの要素 45 個で 7.45 秒）。
        if any(command_exceeds_scan_budget(command) for group in groups for command in group):
            return _SCAN_BUDGET_MESSAGE
        commits, joined_commands = _collect_commits(groups)
        if not commits:
            return None

        # 複数コンテナに commit が散っている場合、どれがどの順で実行されるかは実行前に
        # 確定できないため、1 コマンド内の複数 commit と同じく検査不能として deny する。
        if len(commits) > 1:
            log("[Hook] ERROR: commit content cannot be inspected before execution.")
            return MULTIPLE_COMMITS_MESSAGE

        command, commit_args = commits[0]
        compound_risk = next(filter(None, map(compound_commit_risk, command_readings(command))), None)
        if compound_risk is not None:
            log("[Hook] ERROR: commit content cannot be inspected before execution.")
            return compound_risk

        target_risk = next(filter(None, map(_commit_target_risk, (command, *joined_commands))), None)
        if target_risk is not None:
            log("[Hook] ERROR: the commit may record an index other than the one scanned.")
            return target_risk

        return _evaluate_confirmed_commit(command, commit_args)

    except Exception as err:
        # commit と確定する前の例外は非ブロッキング。確定後の例外は
        # _evaluate_confirmed_commit 側で fail-closed にする。
        log(f"[Hook] Error: {err}")

    return None


def main() -> int:
    """スクリプト実行時に入力を読み取り、品質チェックを行います。

    fail-open/fail-closed の境界は「commit と確定したか」で分ける
    （docs/adr/hook-failure-direction.md）:

    - stdin を読めなかった（`StdinUnavailableError`）・1 MiB 超で切り捨てられた場合は、
      commit かどうかを判定する材料が無いため他の保護 hook と同じく deny する。
      読む対象が無い場合（tty 起動・stdin 未接続・即 EOF）は空文字列として素通りする。
    - deny が決まった後に出力層が失敗しても exit 2 を返す。

    Returns:
        コミットを許可する場合は 0、ブロックする場合は 2 を返します。

    Raises:
        例外は発生しません。
    """
    try:
        raw, truncated = read_raw_stdin_with_truncation()
    except StdinUnavailableError as exc:
        return emit_block_output(stdin_unreadable_message("pre:bash-commit-quality", exc))

    if truncated:
        return emit_block_output(_TRUNCATED_INPUT_MESSAGE)

    reason = evaluate(raw)
    return emit_block_output(reason) if reason is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
