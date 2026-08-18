#!/usr/bin/env python3
"""
コミット前にステージ済みファイルの品質を確認します。

pre:bash で `git commit` を検出したときだけ、lint や簡易静的チェックを実行します。
問題が見つかった場合はコミットを止め、それ以外は入力をそのまま通過させます。

commit 検出は `shlex` によるトークン化を用い、`git` グローバルオプション
（値の有無・既知/未知を問わずすべて読み飛ばします）や連続空白・改行・
`&&`/`;`/`|` 区切りの複合コマンドを考慮します。`git commit -a`/`--all`/
結合短形式（例: `-am`）を検出した場合は、未ステージ変更ファイルを
**作業ツリーから**読んでスキャン対象へ加えます（`git commit -a` は
作業ツリーの内容をコミットするため、INDEX ではなく作業ツリーを読む
必要があります）。ステージ済みファイルは従来どおり INDEX
（`git show :path`）から読みます。

シークレット検出はバイナリ判定（lint 抑制のみに使用）や nosec、
ファイルサイズに関わらず可能な限り実行します（大容量ファイルは
先頭 `_SECRET_SCAN_MAX_BYTES` バイトに切り詰めて継続します）。

非目標: ラッパースクリプトやシェルエイリアス経由の `git commit` 呼び出し検出、
`git commit <pathspec>` で明示指定された未ステージファイルの取り込み
（`-a`/`--all` を伴わない場合は対象外）、およびシェル展開・変数分割経由
（`git $(echo commit)` / `git${IFS}commit` 等）で `git` と `commit` が
生文字列上で隣接しない形の検出（POSIX シェル展開の模倣は原理的に不能で
あり、フェイルセーフ・ヒューリスティックの追加は過剰ブロックを招くため
行いません）。
"""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

from bluecore.hooks.commit_quality_scanner import (
    find_file_issues,
    should_lint_file,
    should_scan_secrets,
)
from bluecore.hooks.hook_common import parse_json_object
from bluecore.lib.core_utils import log
from bluecore.lib.harness import extract_bash_command

_SHELL_SEPARATORS = {"&&", "||", ";", "|"}

_CONVENTIONAL_COMMIT = re.compile(
    r"^(feat|fix|docs|style|refactor|test|chore|build|ci|perf|revert)(\(.+\))?:\s*.+"
)

# raw stdin が JSON として壊れているが、生文字列上に git commit らしき
# パターンが見える場合の deny 理由。JSON を経由せず raw_input に直接
# 正規表現を当てる（_is_git_commit_command の regex フォールバックと同じ
# パターン）。このフックの matcher は Bash 全体（commit と無関係な呼び出し
# も含む）なので、malformed JSON というだけで一律 deny すると commit と
# 無関係な Bash 呼び出しまで巻き込む。commit と判定できた場合のみ deny する。
_MALFORMED_COMMIT_MESSAGE = (
    "[Hook] BLOCKED: hook input could not be parsed as JSON, but the raw "
    "command text matches `git commit`. Refusing to allow an unverifiable "
    "commit through rather than silently skipping the quality scan."
)

# commit と判定済みの入力に対して、判定後の処理中に想定外の例外が
# 発生した場合の deny 理由。ここに到達する時点で「commit である」ことは
# 確定しているため、検査未完了のまま通すのではなく fail-closed にする。
_SCAN_FAILURE_MESSAGE = (
    "[Hook] BLOCKED: the commit quality scan failed unexpectedly for a "
    "confirmed `git commit` call. Refusing to allow an unverifiable commit "
    "through."
)

# get_staged_files() が git 自体の失敗・timeout で None を返した場合の
# deny 理由。「ステージ済みファイル 0 件」（正常系。--allow-empty 等）とは
# 区別する（0 件は exitCode=0 のまま）。
_STAGED_FILES_UNAVAILABLE_MESSAGE = (
    "[Hook] BLOCKED: could not determine staged files for a confirmed "
    "`git commit` call (git itself failed or timed out). Refusing to allow "
    "an unverifiable commit through."
)


def _git_name_only(git_args: list[str]) -> list[str] | None:
    """`git ... --name-only` の出力を非空行リストにする。

    「git は成功したがファイル 0 件」（`None` ではなく `[]`）と「git 自体が
    失敗・timeout した」（`None`）を区別する。両者を同じ `[]` に潰すと、
    呼び出し側が「対象ファイルなし」と「検査不能」を見分けられず、後者を
    無言で見逃す（R-01 残余）。

    Args:
        git_args: subprocess に渡す git コマンド列。

    Returns:
        ファイルパスのリスト。git 自体の失敗・timeout 時は None。

    Raises:
        例外は発生しません。
    """
    try:
        result = subprocess.run(
            git_args,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        return [f for f in result.stdout.strip().split("\n") if f]
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None


def get_staged_files() -> list[str] | None:
    """ステージング済みファイルの一覧を取得します。

    Returns:
        ステージングされたファイルパスのリストを返します。git 自体の
        失敗・timeout で取得できない場合は None（「0 件」とは区別する）。

    Args:
        引数はありません。

    Raises:
        例外は発生しません。
    """
    return _git_name_only(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"])


def get_unstaged_modified_files() -> list[str]:
    """`git commit -a` 相当で追加取り込む作業ツリーの変更ファイル一覧を取得します。

    `git diff HEAD --name-only --diff-filter=ACMR` の結果を返します。
    HEAD が無い（初回コミット）等で git が失敗した場合は空リストです
    （初回コミットは正常系のため、ここは `get_staged_files` と異なり
    失敗を fail-closed にしません）。

    Returns:
        変更されている作業ツリーファイルパスのリストを返します。

    Args:
        引数はありません。

    Raises:
        例外は発生しません。
    """
    result = _git_name_only(["git", "diff", "HEAD", "--name-only", "--diff-filter=ACMR"])
    return result if result is not None else []


def _resolve_repo_root() -> Path | None:
    """`git rev-parse --show-toplevel` でリポジトリルートの絶対パスを解決します。

    `git commit -a` の未ステージ変更を作業ツリーから読むために使います。
    タイムアウト・失敗時は None を返し、呼び出し側で非ブロッキングに
    フォールバック（従来どおり INDEX のみ・作業ツリー分はスキップ）
    できるようにします。

    Returns:
        リポジトリルートの絶対パス。解決できなければ None を返します。

    Args:
        引数はありません。

    Raises:
        例外は発生しません。
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            return None
        top = result.stdout.strip()
        return Path(top) if top else None
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _is_git_executable_token(token: str) -> bool:
    """トークンが git 実行ファイルを指すかを判定します（basename 化・大小無視・.exe 許容）。

    `/usr/bin/git`（絶対パス）・`git.exe`（Windows）・
    `C:\\Program Files\\Git\\bin\\git.exe`（Windows 絶対パス）・`GIT`（大文字）を
    いずれも同一視します。basename 化せず完全一致だけで判定すると、絶対パス
    や `.exe` サフィックスを持つ実行ファイル指定が素通りしていました
    （`git-git-subcommand` の知見にある「shlex トークン走査で最初の
    非オプション語を取る」方式の延長で、実行ファイル名の表記ゆれも
    正規化します）。

    Args:
        token: `shlex` 等でトークン化された1トークンです。

    Returns:
        git 実行ファイルとみなせるなら True。

    Raises:
        例外は発生しません。
    """
    basename = token.replace("\\", "/").rsplit("/", 1)[-1]
    name = basename.lower()
    if name.endswith(".exe"):
        name = name[: -len(".exe")]
    return name == "git"


def _find_git_commit_args(tokens: list[str]) -> list[str] | None:
    """トークン列内の `git commit` 呼び出しを探し、commit 直後の引数トークンを返します。

    `git` トークン（`_is_git_executable_token` で絶対パス・`.exe`・大小を
    正規化して判定）の後は、既知/未知を問わずグローバルオプション・その値
    トークンを区別せず単純に読み飛ばし、`commit` サブコマンドに到達するかを
    判定します（allowlist に無い `--exec-path <path>` / `--super-prefix <path>`
    等の値トークンで走査が打ち切られ検出漏れになる問題を避けるため、過剰
    検出側に倒しています）。`&&`/`;`/`|` 等のシェル区切りトークンに達した
    場合はその `git` 呼び出しは commit ではないとみなし、次の `git` トークン
    を探します。見つかった場合、`commit` 以降シェル区切りトークンが現れる
    までを引数リストとして返します。

    Args:
        tokens: `shlex` 等でトークン化されたコマンド列です。

    Returns:
        commit 呼び出しの引数トークンリスト。見つからなければ None を返します。

    Raises:
        例外は発生しません。
    """
    for i, token in enumerate(tokens):
        if not _is_git_executable_token(token):
            continue
        rest_start = i + 1
        for offset, tok in enumerate(tokens[rest_start:]):
            if tok == "commit":
                return _collect_args_until_separator(tokens, rest_start + offset + 1)
            if tok in _SHELL_SEPARATORS:
                break
    return None


def _collect_args_until_separator(tokens: list[str], start: int) -> list[str]:
    """`start` からシェル区切りトークンが現れるまでの引数トークンを収集します。

    `git commit` の直後（`start`）から `&&`/`;`/`|` 等のシェル区切りに達する
    直前までを commit 引数として返します。

    Args:
        tokens: `shlex` 等でトークン化されたコマンド列です。
        start: 収集を開始するインデックス（`commit` トークンの次）です。

    Returns:
        収集した引数トークンのリストを返します。

    Raises:
        例外は発生しません。
    """
    args: list[str] = []
    for tok in tokens[start:]:
        if tok in _SHELL_SEPARATORS:
            break
        args.append(tok)
    return args


def _is_git_commit_command(command: str) -> tuple[bool, list[str]]:
    """コマンド文字列が `git commit` 呼び出しかを判定し、commit 引数トークンを返します。

    `shlex.split` でトークン化し、`git` → グローバルオプション → `commit`
    の並びを検出します。連続空白・改行・`&&`/`;`/`|` 区切りの複合コマンドは
    トークン走査で自然に扱えます。

    `shlex.split` がクォート不整合（heredoc 等）で `ValueError` を送出した
    場合は、空白による簡易分割へフォールバックしてトークン走査を継続し、
    それでも判定できなければ `re.search(r"\\bgit\\s+commit\\b", command)` で
    最終判定します。過剰検出側に倒すフェイルセーフ設計です。

    非目標: シェル展開・変数分割経由（`git $(echo commit)` / `git${IFS}commit`
    等）で `git` と `commit` が生文字列上で隣接しない形の検出。POSIX シェル
    展開を文字列解析だけで模倣するのは原理的に不能であり、無理に検出しようと
    するとヒューリスティックが過剰ブロックを招くため対応しません。

    Args:
        command: 検査対象のコマンド文字列です。

    Returns:
        (is_commit, commit_args) のタプル。is_commit が False の場合、
        commit_args は空リストです。

    Raises:
        例外は発生しません。
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    commit_args = _find_git_commit_args(tokens)
    if commit_args is not None:
        return True, commit_args

    if re.search(r"\bgit\s+commit\b", command):
        return True, []
    return False, []


def _is_commit_all_flag(commit_args: list[str]) -> bool:
    """commit 引数トークンに `-a`/`--all`（結合短形式含む）が含まれるかを判定します。

    `-am` のような結合短形式は、`-` 始まり・`--` ではない・`=` を含まない
    短形式トークンを文字単位に展開して `a` を探す最小実装です。

    Args:
        commit_args: `git commit` 呼び出しの引数トークン列です。

    Returns:
        `-a` 相当のフラグが含まれるなら True を返します。

    Raises:
        例外は発生しません。
    """
    for tok in commit_args:
        if tok == "--all":
            return True
        if tok.startswith("-") and not tok.startswith("--") and "=" not in tok and "a" in tok[1:]:
            return True
    return False


def _message_issue(kind: str, message: str, suggestion: str) -> dict:
    """コミットメッセージの問題 1 件を表す辞書を作る。

    Args:
        kind: 問題種別（format / length / capitalization / punctuation）。
        message: 問題の説明。
        suggestion: 修正提案。

    Returns:
        type / message / suggestion を持つ辞書。

    Raises:
        例外は発生しません。
    """
    return {"type": kind, "message": message, "suggestion": suggestion}


def _commit_message_issues(message: str) -> list[dict]:
    """コミットメッセージ本文の形式問題を列挙する。

    Args:
        message: 抽出済みのコミットメッセージ本文。

    Returns:
        問題辞書のリスト。問題がなければ空リスト。

    Raises:
        例外は発生しません。
    """
    issues: list[dict] = []
    if not _CONVENTIONAL_COMMIT.match(message):
        issues.append(
            _message_issue(
                "format",
                "Commit message does not follow conventional commit format",
                'Use format: type(scope): description (e.g., "feat(auth): add login flow")',
            )
        )
    if len(message) > 72:
        issues.append(
            _message_issue(
                "length",
                f"Commit message too long ({len(message)} chars, max 72)",
                "Keep the first line under 72 characters",
            )
        )
    if _CONVENTIONAL_COMMIT.match(message):
        after_colon = message.split(":", 1)[1] if ":" in message else ""
        if after_colon and re.match(r"^[A-Z]", after_colon.strip()):
            issues.append(
                _message_issue(
                    "capitalization",
                    "Subject should start with lowercase after type",
                    "Use lowercase for the first letter of the subject",
                )
            )
    if message.endswith("."):
        issues.append(
            _message_issue(
                "punctuation",
                "Commit message should not end with a period",
                "Remove the trailing period",
            )
        )
    return issues


def validate_commit_message(command: str) -> dict | None:
    """コミットメッセージの形式を検証します。

    Args:
        command: `git commit` コマンド文字列です。

    Returns:
        メッセージと問題一覧を含む辞書、またはメッセージがない場合は None を返します。

    Raises:
        例外は発生しません。
    """
    message_match = re.search(r"(?:-m|--message)[=\s]+[\"']?([^\"']+)[\"']?", command)
    if not message_match:
        return None
    message = message_match.group(1)
    return {"message": message, "issues": _commit_message_issues(message)}


def _scan_targets(files: list[str]) -> list[str]:
    """lint または secret スキャン対象のファイルだけを残す。

    Args:
        files: 候補ファイルパス。

    Returns:
        スキャン対象のファイルパス。

    Raises:
        例外は発生しません。
    """
    return [f for f in files if should_lint_file(f) or should_scan_secrets(f)]


def _partition_commit_all_files(
    staged_files: list[str], commit_args: list[str]
) -> tuple[list[str], list[str]]:
    """`-a`/`--all` 指定時に、INDEX から読む対象と作業ツリーから読む対象に分割します。

    `git commit -a` は作業ツリーの現在の内容をコミットするため、未ステージ
    変更ファイル（`get_unstaged_modified_files` 由来）は作業ツリー優先で
    読みます。ステージ済みかつ未ステージ変更もある（ステージ後にさらに
    作業ツリーで変更された）ファイルも、`-a` の場合は作業ツリー優先とします
    （`git commit -a` の実際の挙動と一致させるためです）。

    Args:
        staged_files: `get_staged_files()` によるステージ済みファイル一覧です。
        commit_args: `git commit` 呼び出しの引数トークン列です。

    Returns:
        (index_files, worktree_files) のタプルです。`-a`/`--all` が無ければ
        `worktree_files` は空リストです。

    Raises:
        例外は発生しません。
    """
    if not _is_commit_all_flag(commit_args):
        return list(staged_files), []

    worktree_files = sorted(set(get_unstaged_modified_files()))
    index_files = sorted(set(staged_files) - set(worktree_files))
    return index_files, worktree_files


def _count_file_issues(files_to_check: list[str], repo_root: Path | None = None) -> tuple[int, int, int, int]:
    """チェック対象ファイルの問題数を集計します。

    各ファイルに対して find_file_issues を呼び出し、severity 別に問題数を返します。
    ログ出力も行います。

    Args:
        files_to_check: チェック対象のファイルパスリストです。
        repo_root: 指定すると各ファイルを作業ツリーから読みます
            （`git commit -a` の未ステージ変更用）。None なら INDEX から
            読みます。

    Returns:
        (total_issues, error_count, warning_count, info_count) のタプルを返します。

    Raises:
        例外は発生しません。
    """
    total_issues = 0
    error_count = 0
    warning_count = 0
    info_count = 0
    severity_label = {"error": "ERROR", "warning": "WARNING", "info": "INFO"}

    for file_path in files_to_check:
        file_issues = find_file_issues(file_path, repo_root=repo_root)
        if not file_issues:
            continue
        log(f"\n[FILE] {file_path}")
        for issue in file_issues:
            # issue は find_file_issues の内部契約（dict にキー欠落なし）に
            # 依存せず .get() で防御する。未知 severity は「検査したが
            # 分類できない」ことを示すため、見逃す（黙って info/合計のみ加算）
            # のではなく安全側の error 扱いにする。
            severity = issue.get("severity")
            label = severity_label.get(severity, "INFO")
            log(f"  {label} Line {issue.get('line', 0)}: {issue.get('message', '')}")
            total_issues += 1
            if severity == "error":
                error_count += 1
            elif severity == "warning":
                warning_count += 1
            elif severity == "info":
                info_count += 1
            else:
                error_count += 1

    return total_issues, error_count, warning_count, info_count


def _apply_commit_message_issues(
    command: str,
    total_issues: int,
    warning_count: int,
) -> tuple[int, int]:
    """コミットメッセージの問題を検証してカウントに加算します。

    validate_commit_message を呼び出し、問題があればログ出力して
    更新後の (total_issues, warning_count) を返します。

    Args:
        command: `git commit` コマンド文字列です。
        total_issues: 現在の問題総数です。
        warning_count: 現在の警告数です。

    Returns:
        (total_issues, warning_count) の更新後タプルを返します。

    Raises:
        例外は発生しません。
    """
    message_validation = validate_commit_message(command)
    if not (message_validation and message_validation["issues"]):
        return total_issues, warning_count

    log("\nCommit Message Issues:")
    for issue in message_validation["issues"]:
        log(f"  WARNING {issue['message']}")
        if issue.get("suggestion"):
            log(f"     TIP {issue['suggestion']}")
        total_issues += 1
        warning_count += 1

    return total_issues, warning_count


def _finalize_result(
    total_issues: int,
    error_count: int,
    warning_count: int,
    info_count: int,
    raw_input: str,
) -> dict:
    """問題集計結果をログに記録し、終了コードを含む結果辞書を返します。

    error_count > 0 の場合は exitCode=2（コミットブロック）＋ reason（
    emit_block_output に渡すブロック理由）、それ以外は exitCode=0 を返します。

    Args:
        total_issues: 検出された問題の総数です。
        error_count: エラー severity の問題数です。
        warning_count: 警告 severity の問題数です。
        info_count: info severity の問題数です。
        raw_input: そのまま output に返す生の入力文字列です。

    Returns:
        output と exitCode（exitCode=2 の場合は reason も）を含む辞書を返します。

    Raises:
        例外は発生しません。
    """
    if total_issues > 0:
        log(
            f"\nSummary: {total_issues} issue(s) found "
            f"({error_count} error(s), {warning_count} warning(s), {info_count} info)"
        )
        if error_count > 0:
            log("\n[Hook] ERROR: Commit blocked due to critical issues. Fix them before committing.")
            reason = (
                f"[Hook] BLOCKED: {error_count} error(s) found in staged files. "
                "Fix them before committing."
            )
            return {"output": raw_input, "exitCode": 2, "reason": reason}
        log("\n[Hook] WARNING: Warnings found. Consider fixing them, but commit is allowed.")
    else:
        log("\n[Hook] PASS: All checks passed!")

    return {"output": raw_input, "exitCode": 0}


def _collect_worktree_issues(
    worktree_targets: list[str],
    total_issues: int,
    error_count: int,
    warning_count: int,
    info_count: int,
) -> tuple[int, int, int, int]:
    """`git commit -a` の作業ツリー対象ファイルの問題数を集計に加算します。

    リポジトリルートを解決し、作業ツリー（`repo_root` 経由）から各ファイルを
    読んで `_count_file_issues` で集計し、既存のカウントに加算した結果を
    返します。作業ツリー対象が無い、または repo root が解決できない場合は
    非ブロッキングで作業ツリー分をスキップし、入力のカウントをそのまま
    返します。

    Args:
        worktree_targets: 作業ツリーから読むチェック対象ファイルパスです。
        total_issues: 現在の問題総数です。
        error_count: 現在のエラー数です。
        warning_count: 現在の警告数です。
        info_count: 現在の info 数です。

    Returns:
        (total_issues, error_count, warning_count, info_count) の更新後
        タプルを返します。

    Raises:
        例外は発生しません。
    """
    if not worktree_targets:
        return total_issues, error_count, warning_count, info_count

    repo_root = _resolve_repo_root()
    if repo_root is None:
        # repo root が解決できない場合は非ブロッキングで作業ツリー分をスキップする
        return total_issues, error_count, warning_count, info_count

    wt_total, wt_error, wt_warning, wt_info = _count_file_issues(worktree_targets, repo_root=repo_root)
    return (
        total_issues + wt_total,
        error_count + wt_error,
        warning_count + wt_warning,
        info_count + wt_info,
    )


def _evaluate_confirmed_commit(raw_input: str, command: str, commit_args: list[str]) -> dict:
    """`git commit` と判定済みの入力を検査し、結果を返します。

    ここに到達した時点で「これは commit である」ことは確定しているため、
    処理中に想定外の例外が起きても exitCode=0（非ブロッキング）へは
    倒さない。検査未完了のまま通すのは「検査したが問題なし」と区別が
    付かなくなり、品質・secret 検査を無言で迂回できてしまうため。

    Args:
        raw_input: フックに渡された生の入力文字列です。
        command: `git commit` を含む bash コマンド文字列です。
        commit_args: commit 呼び出しの引数トークン列です。

    Returns:
        output と exitCode を含む辞書を返します。

    Raises:
        例外は発生しません。
    """
    try:
        # ステージングされたファイルを取得（-a/--all の場合は未ステージの変更も加える。
        # -a の未ステージ分は作業ツリーの内容がコミットされるため作業ツリーから読む）
        staged_files = get_staged_files()
        if staged_files is None:
            log("[Hook] ERROR: could not determine staged files (git itself failed or timed out).")
            return {"output": raw_input, "exitCode": 2, "reason": _STAGED_FILES_UNAVAILABLE_MESSAGE}

        index_files, worktree_files = _partition_commit_all_files(staged_files, commit_args)
        all_files = sorted(set(index_files) | set(worktree_files))
        if not all_files:
            log('[Hook] No staged files found. Use "git add" to stage files first.')
            return {"output": raw_input, "exitCode": 0}

        log(f"[Hook] Checking {len(all_files)} staged file(s)...")

        index_targets = _scan_targets(index_files)
        worktree_targets = _scan_targets(worktree_files)

        total_issues, error_count, warning_count, info_count = _count_file_issues(index_targets)
        total_issues, error_count, warning_count, info_count = _collect_worktree_issues(
            worktree_targets, total_issues, error_count, warning_count, info_count
        )

        total_issues, warning_count = _apply_commit_message_issues(command, total_issues, warning_count)

        return _finalize_result(total_issues, error_count, warning_count, info_count, raw_input)

    except Exception as err:
        log(f"[Hook] Error: {err}")
        log("[Hook] BLOCKED: quality scan failed for a confirmed git commit; refusing an unverifiable commit.")
        return {"output": raw_input, "exitCode": 2, "reason": _SCAN_FAILURE_MESSAGE}


def evaluate(raw_input: str) -> dict:
    """入力を評価し、出力内容と終了コードを返します。

    JSON が壊れている・commit と無関係な Bash 呼び出しは非ブロッキング
    （exitCode=0）。JSON が壊れていても raw 文字列上に `git commit` が
    見える場合は deny する（malformed JSON を理由に品質・secret 検査を
    迂回させないため）。`git commit` と確定した入力は
    `_evaluate_confirmed_commit` に委譲し、以降は fail-closed で扱う。

    Args:
        raw_input: フックに渡された生の入力文字列です。

    Returns:
        output と exitCode を含む辞書を返します。

    Raises:
        例外は発生しません。
    """
    try:
        input_data = parse_json_object(raw_input)
        if input_data is None:
            # 空/空白入力、または JSON として壊れている。matcher が Bash 全体
            # （commit と無関係な呼び出しも含む）のため、malformed というだけで
            # 一律 deny すると無関係な Bash 呼び出しまで巻き込む。raw 文字列上に
            # `git commit` が見える場合のみ deny する。
            if raw_input.strip() and re.search(r"\bgit\s+commit\b", raw_input):
                log("[Hook] ERROR: malformed JSON input, but raw command text matches `git commit`.")
                return {"output": raw_input, "exitCode": 2, "reason": _MALFORMED_COMMIT_MESSAGE}
            if raw_input.strip():
                log("[Hook] WARNING: could not parse hook input as JSON; not a git commit, passing through.")
            return {"output": raw_input, "exitCode": 0}

        if not input_data:
            return {"output": raw_input, "exitCode": 0}

        command = extract_bash_command(input_data)

        # git commit コマンドの場合のみ実行（トークン化して堅牢に判定）
        is_commit, commit_args = _is_git_commit_command(command)
        if not is_commit:
            return {"output": raw_input, "exitCode": 0}

        return _evaluate_confirmed_commit(raw_input, command, commit_args)

    except Exception as err:
        log(f"[Hook] Error: {err}")
        # commit と確定する前の例外（JSON 抽出・コマンド判定段階）は
        # 非ブロッキング。判定済みの例外は _evaluate_confirmed_commit 側で
        # fail-closed にする。

    return {"output": raw_input, "exitCode": 0}


def run(raw_input: str) -> dict:
    """フックを実行し、output と exitCode を含む結果を返します。

    Args:
        raw_input: フックに渡された生の入力文字列です。

    Returns:
        output と exitCode を含む辞書を返します。

    Raises:
        例外は発生しません。
    """
    return evaluate(raw_input)


def main() -> int:
    """スクリプト実行時に入力を読み取り、品質チェックを行います。

    ブロック時（exitCode == 2）は emit_block_output で host 非依存の合併出力
    （stderr の理由 + stdout の permissionDecision: deny JSON、exit 2）に変換する。

    Returns:
        コミットを許可する場合は 0、ブロックする場合は 2 を返します。

    Args:
        引数はありません。

    Raises:
        例外は発生しません。
    """
    from bluecore.hooks.hook_common import emit_block_output, read_raw_stdin

    try:
        raw = read_raw_stdin()
        result = evaluate(raw)
        if result["exitCode"] == 2:
            return emit_block_output(result["reason"])
        return result["exitCode"]
    except Exception as err:
        # read_raw_stdin / emit_block_output 自体が失敗した場合。evaluate()
        # は例外を投げない契約のためここに到達するのは入出力層のみ。
        # raw 入力が確保できていないため commit 判定自体ができず
        # fail-open にせざるを得ないが、無言にはしない。
        log(f"[Hook] Error: {err}")
        return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
