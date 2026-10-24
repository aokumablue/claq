"""``git commit`` 呼び出しの検出と、commit 内容を検査不能にする複合コマンドの判定。

1 回の Bash 呼び出しの中に commit が複数ある、または commit より前に作業ツリー / index
を変更するセグメントがある場合を検出する葉モジュール。評価の制御は
``pre_bash_commit_quality`` が持ち、このモジュールは ``pre_bash_commit_quality`` を
import しない。
"""

from __future__ import annotations

import re

from claq.hooks.hook_common import (
    ALWAYS_MUTATING_EDIT_EXECUTABLES,
    is_git_executable_token,
    is_inplace_edit_flag,
    normalize_executable_name,
)
from claq.hooks.shell_tokens import split_segments, tokenize, tokenize_with_status

# PreToolUse フックは実行前の index/worktree しか観測できず、2 つ目以降の commit が
# 何をコミットするかを原理的に検査できない。
_MULTIPLE_COMMITS_MESSAGE = (
    "[Hook] BLOCKED: this command contains more than one `git commit`. A "
    "PreToolUse hook can only inspect the index as it is before the command "
    "runs, so any commit after the first one cannot be scanned. Split them "
    "into separate tool calls."
)

# commit より前のセグメントが作業ツリーまたは index を変更する場合。
# `printf ... > secret.py && git add secret.py && git commit -m x` は検査時点の
# index が空でも、実行時には secret.py が commit される。
_MUTATION_BEFORE_COMMIT_MESSAGE = (
    "[Hook] BLOCKED: this command modifies the worktree or the index before "
    "the `git commit` in the same call, so the quality scan would run against "
    "state that is not what gets committed. Run the file changes and "
    "`git add` in a separate tool call, then commit."
)


def _find_git_commit_args_in_segment(segment: list[str]) -> list[str] | None:
    """1 セグメント（シェル区切りを含まないトークン列）内の `git commit` 呼び出しを探します。

    `git` トークンの後は、既知/未知を問わずグローバルオプションとその値を区別せず
    読み飛ばし、`commit` に到達するかを見る。allowlist に無い `--exec-path <path>`
    等の値トークンで走査が打ち切られて検出漏れになるのを避け、過剰検出側へ倒す。

    Args:
        segment: `shell_tokens.tokenize` + `split_segments` で得た 1 セグメント分のトークン列です。

    Returns:
        commit 呼び出しの引数トークンリスト。見つからなければ None を返します。

    Raises:
        例外は発生しません。
    """
    for i, token in enumerate(segment):
        if is_git_executable_token(token) and "commit" in segment[i + 1 :]:
            return segment[segment.index("commit", i + 1) + 1 :]
    return None


def _is_git_commit_command(command: str) -> tuple[bool, list[str]]:
    """コマンド文字列が `git commit` 呼び出しかを判定し、commit 引数トークンを返します。

    トークン化してシェル区切りごとのセグメントに分け、セグメントごとに `git` →
    グローバルオプション → `commit` の並びを探す。セグメント分割により
    ``git status;echo commit`` のような非 commit コマンドを commit と判定しない。

    トークン化がクォート不整合で空白分割へフォールバックした場合**だけ**、生文字列の
    正規表現で最終判定する（解析できない入力に対するフェイルセーフ）。解析できた入力に
    まで当てると ``copilot -p 'Explain why a git commit command may fail'`` のような
    引用文まで commit と判定し、`block_no_verify` と「commit とは何か」で食い違う。

    Args:
        command: 検査対象のコマンド文字列です。

    Returns:
        (is_commit, commit_args) のタプル。is_commit が False の場合、
        commit_args は空リストです。

    Raises:
        例外は発生しません。
    """
    tokens, parsed_cleanly = tokenize_with_status(command)
    for segment in split_segments(tokens):
        commit_args = _find_git_commit_args_in_segment(segment)
        if commit_args is not None:
            return True, commit_args

    if not parsed_cleanly and re.search(r"\bgit\s+commit\b", command):
        return True, []
    return False, []


# commit より前に実行されると検査結果を無効化する操作。判定に迷う構文は「変更あり」側へ
# 倒す（docs/adr/shell-analysis-boundary.md）。対象は書き込み先が引数位置に明示される
# コマンドと index 操作。
_INDEX_MUTATING_GIT_SUBCOMMANDS = frozenset(
    {
        "add",
        "am",
        "apply",
        "checkout",
        "cherry-pick",
        "merge",
        "mv",
        "pull",
        "rebase",
        "reset",
        "restore",
        "revert",
        "rm",
        "stash",
        "switch",
    }
)
_WORKTREE_MUTATING_EXECUTABLES = frozenset(
    {
        "chmod",
        "chown",
        "cp",
        "dd",
        "install",
        "ln",
        "mkdir",
        "mv",
        "patch",
        "rm",
        "shred",
        "tee",
        "touch",
        "truncate",
        "unlink",
    }
)
# `-i` を伴うときだけ書き込みになるコマンド。フラグ無しでも書き込むコマンド（`ed` 等）を
# ここへ置くとフラグ判定に落ちて allow になるため、`ALWAYS_MUTATING_EDIT_EXECUTABLES` に置く。
_INPLACE_EDIT_EXECUTABLES = frozenset({"sed", "perl"})
# ``<>`` は読み書きオープンで、対象が無ければ作成する（作業ツリーの変更）。
_MUTATING_REDIRECT_OPERATORS = frozenset({">", ">>", "&>", "&>>", ">|", "<>", "1>", "2>", "1>>", "2>>", ">&"})
# `>&` は直後が fd 番号（数字のみ）か `-` のとき、fd の複製・クローズでありファイルへ書かない。
# それ以外（`>&file`）は `&>file` の別綴りで書き込み。`2>&1` も `2`・`>&`・`1` に割れる。
_FD_DUP_OPERATOR = ">&"
_FD_CLOSE_TARGET = "-"


def _is_fd_duplication(segment: list[str], index: int) -> bool:
    """`segment[index]` が fd の複製・クローズ（`>&2` / `>&-`）かを判定します。

    直後のトークンだけを見る。`echo 1 >&f` の `1` のように別位置の数字は対象にしない。
    全角数字などは fd 番号ではないため ASCII の数字に限る。

    Args:
        segment: `shell_tokens.split_segments` で得た 1 セグメント分のトークン列です。
        index: 判定する `>&` のセグメント内位置です。

    Returns:
        `>&` で、直後が ASCII 数字のみのトークンか `-` なら True を返します。

    Raises:
        例外は発生しません。
    """
    if segment[index] != _FD_DUP_OPERATOR or index + 1 >= len(segment):
        return False
    target = segment[index + 1]
    return target == _FD_CLOSE_TARGET or (target.isascii() and target.isdigit())


def _segment_mutates_worktree_or_index(segment: list[str]) -> bool:
    """1 セグメントが作業ツリーまたは git index を変更しうるかを判定します。

    誤検出側へ倒す。誤検出のコストは「commit を別の tool call へ分けてもらう」ことで、
    誤通過のコスト（未検査の内容が commit される）より小さい。

    Args:
        segment: `shell_tokens.split_segments` で得た 1 セグメント分のトークン列です。

    Returns:
        変更しうるなら True を返します。

    Raises:
        例外は発生しません。
    """
    if any(
        token in _MUTATING_REDIRECT_OPERATORS and not _is_fd_duplication(segment, i) for i, token in enumerate(segment)
    ):
        return True

    for i, token in enumerate(segment):
        if is_git_executable_token(token):
            # 「最初の非 `-` トークン」で決めると `git -C . add x` の値 `.` がサブコマンドと
            # 読まれて不発になるため全トークンを走査する。後続の書込み実行ファイルも
            # 見るため早期 return しない。
            if any(sub in _INDEX_MUTATING_GIT_SUBCOMMANDS for sub in segment[i + 1 :]):
                return True
            continue

        # 素の basename（大小区別・`.exe` 残し）で照合すると ``CP evil.py app.py`` や
        # ``cp.exe`` が語彙から外れ、未検査の内容が commit されるため共有の正規化を通す。
        name = normalize_executable_name(token)
        if name in _WORKTREE_MUTATING_EXECUTABLES or name in ALWAYS_MUTATING_EDIT_EXECUTABLES:
            return True
        if name in _INPLACE_EDIT_EXECUTABLES:
            return any(is_inplace_edit_flag(arg) for arg in segment[i + 1 :])

    return False


def _compound_commit_risk(command: str) -> str | None:
    """1 回の Bash 呼び出しの中で commit 内容を検査できなくする構造を検出します。

    PreToolUse フックが観測できるのはコマンド実行前の index/worktree だけなので、
    次の 2 つは原理的に検査不能として deny する（docs/adr/hook-failure-direction.md、
    docs/adr/shell-analysis-boundary.md）。

    1. 複数の `git commit` — 2 つ目以降がコミットする内容は実行前状態に現れない。
    2. commit より前のセグメントによる作業ツリー / index の変更。

    ``git add . && git commit -m x`` というイディオムも塞ぐ。通すと「品質フックが
    commit 内容を検査している」という契約が成立せず、誤った安心感になるため。

    Args:
        command: 検査対象のコマンド文字列です。

    Returns:
        deny すべき場合はその理由文字列、問題なければ None を返します。

    Raises:
        例外は発生しません。
    """
    segments = split_segments(tokenize(command))
    commit_indices = [i for i, segment in enumerate(segments) if _find_git_commit_args_in_segment(segment) is not None]
    if not commit_indices:
        return None
    if len(commit_indices) > 1:
        return _MULTIPLE_COMMITS_MESSAGE
    if any(_segment_mutates_worktree_or_index(segment) for segment in segments[: commit_indices[0]]):
        return _MUTATION_BEFORE_COMMIT_MESSAGE
    return None


def _is_commit_all_flag(commit_args: list[str]) -> bool:
    """commit 引数トークンに `-a`/`--all`（結合短形式含む）が含まれるかを判定します。

    `-am` のような結合短形式は、`-` 始まり・`--` ではない・`=` を含まない短形式
    トークンの中に `a` があるかで見る。

    Args:
        commit_args: `git commit` 呼び出しの引数トークン列です。

    Returns:
        `-a` 相当のフラグが含まれるなら True を返します。

    Raises:
        例外は発生しません。
    """
    return any(
        tok == "--all" or (tok.startswith("-") and not tok.startswith("--") and "=" not in tok and "a" in tok[1:])
        for tok in commit_args
    )
