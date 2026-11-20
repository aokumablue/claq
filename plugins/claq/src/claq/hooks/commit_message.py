"""コミットメッセージの形式検証（conventional commit・件名行の長さ・大文字・末尾句点）。

``git commit`` のコマンド文字列をトークン化し、``-m`` / ``--message`` の値（結合短形式の ``-am`` /
``-mmsg`` を含む）を取り出して、件名行の形式問題を列挙するだけの葉モジュール。評価の制御は
``pre_bash_commit_quality`` が持ち、このモジュールは ``pre_bash_commit_quality`` を import しない。
"""

from __future__ import annotations

import re

from claq.hooks.commit_risk import parse_git_commit_command

# Conventional Commits 1.0.0: type・任意の scope・破壊的変更を表す任意の `!`・コロン。
_CONVENTIONAL_COMMIT = re.compile(
    r"^(feat|fix|docs|style|refactor|test|chore|build|ci|perf|revert)(\(.+\))?!?:\s*.+"
)
_MAX_SUBJECT_CHARS = 72

# `-m` 以外で値を取る git commit の短形式。結合短形式の中でこれらが先に現れたら、残りの文字は
# その値であってメッセージの指定ではない（`-Cmain` の `m` を `-m` と読まない）。
_OTHER_SHORT_OPTIONS_WITH_VALUE = frozenset("CcFStu")


def _message_issue(kind: str, message: str, suggestion: str) -> dict:
    """コミットメッセージの問題 1 件を表す辞書を作る。

    Args:
        kind: 問題種別（format / length / capitalization / punctuation）。
        message: 問題の説明。
        suggestion: 修正提案。

    Returns:
        type / message / suggestion を持つ辞書。
    """
    return {"type": kind, "message": message, "suggestion": suggestion}


def _commit_message_issues(subject: str) -> list[dict]:
    """コミットメッセージの件名行の形式問題を列挙する。

    Args:
        subject: 抽出済みのコミットメッセージの 1 行目。

    Returns:
        問題辞書のリスト。問題がなければ空リスト。
    """
    issues: list[dict] = []
    conventional = _CONVENTIONAL_COMMIT.match(subject)
    if not conventional:
        issues.append(
            _message_issue(
                "format",
                "Commit message does not follow conventional commit format",
                'Use format: type(scope): description (e.g., "feat(auth): add login flow")',
            )
        )
    if len(subject) > _MAX_SUBJECT_CHARS:
        issues.append(
            _message_issue(
                "length",
                f"Commit message too long ({len(subject)} chars, max {_MAX_SUBJECT_CHARS})",
                f"Keep the first line under {_MAX_SUBJECT_CHARS} characters",
            )
        )
    if conventional and re.match(r"^[A-Z]", subject.split(":", 1)[1].strip()):
        issues.append(
            _message_issue(
                "capitalization",
                "Subject should start with lowercase after type",
                "Use lowercase for the first letter of the subject",
            )
        )
    if subject.endswith("."):
        issues.append(
            _message_issue(
                "punctuation",
                "Commit message should not end with a period",
                "Remove the trailing period",
            )
        )
    return issues


def _short_cluster_message(token: str) -> str | None:
    """短形式のトークン（``-m`` / ``-am`` / ``-mmsg``）から ``-m`` に結合した値を返す。

    Args:
        token: ``-`` 1 つで始まる引数トークン。

    Returns:
        ``m`` より後ろの文字列（``-m`` 単独や ``-am`` は空文字）。``m`` が無い、または値を取る
        別の短形式が ``m`` より先に現れた場合は None。
    """
    for position, flag in enumerate(token[1:], start=2):
        if flag == "m":
            return token[position:]
        if flag in _OTHER_SHORT_OPTIONS_WITH_VALUE:
            return None
    return None


def _message_from_args(commit_args: list[str]) -> str | None:
    """commit の引数トークンから最初の ``-m`` / ``--message`` の値を取り出す。

    git は複数の ``-m`` を段落として連結するので、最初の値が件名行を含む。``--`` より後ろは
    パス指定なので見ない。

    Args:
        commit_args: `parse_git_commit_command` が返した commit の引数トークン列。

    Returns:
        メッセージの値。指定が無い、または値が欠けていれば None。
    """
    following: list[str | None] = [*commit_args[1:], None]
    for token, next_token in zip(commit_args, following, strict=False):
        if token == "--":
            return None
        if token == "--message":
            return next_token
        if token.startswith("--message="):
            return token.removeprefix("--message=")
        if token.startswith("-") and not token.startswith("--"):
            attached = _short_cluster_message(token)
            if attached is not None:
                return attached or next_token
    return None


def validate_commit_message(command: str) -> dict | None:
    """コミットメッセージの件名行の形式を検証します。

    メッセージはシェルのトークン化を通した commit 引数から読む（引用符の中の ``'``・結合短形式の
    ``-am`` も値どおりに読める）。検査は件名行（1 行目）だけに当てる。本文の長さや本文末尾の句点は
    件名の形式とは関係が無い。

    Args:
        command: `git commit` コマンド文字列です。

    Returns:
        メッセージと問題一覧を含む辞書、またはメッセージがない・本文が実行時にしか決まらない場合は
        None を返します。
    """
    message = _message_from_args(parse_git_commit_command(command)[1])
    if not message:
        return None
    # `-m "$(cat <<'EOF' ...)"` のようなコマンド置換は、本文が実行時にしか決まらない。字面を本文として
    # 検査すると置換の書き出し（`$(cat <<`）を本文と取り違え、毎回の commit で形式違反を警告する。
    if message.startswith(("$(", "`")):
        return None
    return {"message": message, "issues": _commit_message_issues(message.splitlines()[0])}
