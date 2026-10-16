"""コミットメッセージの形式検証（conventional commit・長さ・大文字・末尾句点）。

``git commit`` のコマンド文字列から ``-m`` / ``--message`` を取り出して形式問題を列挙する
だけの葉モジュール。評価の制御は ``pre_bash_commit_quality`` が持ち、このモジュールは
``pre_bash_commit_quality`` を import しない。
"""

from __future__ import annotations

import re

_CONVENTIONAL_COMMIT = re.compile(
    r"^(feat|fix|docs|style|refactor|test|chore|build|ci|perf|revert)(\(.+\))?:\s*.+"
)


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
    conventional = _CONVENTIONAL_COMMIT.match(message)
    if not conventional:
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
    if conventional and re.match(r"^[A-Z]", message.split(":", 1)[1].strip()):
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
