"""コミットメッセージの形式検証（conventional commit・件名行の長さ・大文字・末尾句点）。

``git commit`` のコマンド文字列をトークン化し、``-m`` / ``--message`` の値（結合短形式の ``-am`` /
``-mmsg`` を含む）を取り出して、件名行の形式問題を列挙するだけの葉モジュール。評価の制御は
``pre_bash_commit_quality`` が持ち、このモジュールは ``pre_bash_commit_quality`` を import しない。
"""

from __future__ import annotations

import re

from claq.hooks.commit_risk import short_option_flags

# Conventional Commits 1.0.0: type・任意の scope・破壊的変更を表す任意の `!`・コロン。
_CONVENTIONAL_COMMIT = re.compile(
    r"^(feat|fix|docs|style|refactor|test|chore|build|ci|perf|revert)(\(.+\))?!?:\s*.+"
)
_MAX_SUBJECT_CHARS = 72
# 件名の末尾に置かない文の終わりの記号。日本語の件名は句点（`。`）と全角ピリオド（`．`）で文を終える。
_SUBJECT_TERMINATORS = (".", "。", "．")
# git が件名を生成する形（`git merge` / `git revert` の既定）。利用者が書いた件名ではないので検査しない。
_GIT_GENERATED_SUBJECT = re.compile(r'^(?:Merge (?:branch|remote-tracking branch|tag|pull request|commit) |Revert ")')
# `git rebase --autosquash` が後で畳む印。印の後ろが本来の件名なので、印を外して検査する。
_AUTOSQUASH_PREFIX = re.compile(r"^(?:(?:fixup|squash|amend)! )+")

# 説明部の先頭語（英数字の連なり）と、それが大文字 1 字＋小文字だけの語（``Add``）か。
_LEADING_WORD = re.compile(r"[A-Za-z0-9]+")
_SENTENCE_CASE_WORD = re.compile(r"[A-Z][a-z]*")


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


def _is_capitalized_description(description: str) -> bool:
    """件名の説明部が、小文字で始めるべき英語の語を大文字で始めているかを返す。

    小文字始まりは英語の件名の慣習なので、英語（ASCII）だけの説明部の先頭語が ``Add`` のように
    大文字 1 字＋小文字の形のときだけ真にする。略語（``API``）や大文字を内に含む名前（``GitHub``）は
    小文字にできない。日本語の件名の先頭に置いた英単語（``Windows の…``）は固有名か識別子で、
    小文字にすると誤記になる。

    Args:
        description: conventional 形式の ``type(scope):`` より後ろの、前後の空白を除いた説明部。

    Returns:
        大文字始まりを咎めるべきなら True。
    """
    first_word = _LEADING_WORD.match(description)
    return description.isascii() and first_word is not None and _SENTENCE_CASE_WORD.fullmatch(first_word[0]) is not None


def _commit_message_issues(subject: str) -> list[dict]:
    """コミットメッセージの件名行の形式問題を列挙する。

    git が生成する件名（``Merge branch …`` ``Revert "…"``）は検査しない。autosquash の印
    （``fixup!`` ``squash!`` ``amend!``）は外し、後ろの本来の件名を検査する。

    Args:
        subject: 抽出済みのコミットメッセージの 1 行目。

    Returns:
        問題辞書のリスト。問題がなければ空リスト。
    """
    if _GIT_GENERATED_SUBJECT.match(subject):
        return []
    subject = _AUTOSQUASH_PREFIX.sub("", subject, count=1)
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
    if conventional and _is_capitalized_description(subject.split(":", 1)[1].strip()):
        issues.append(
            _message_issue(
                "capitalization",
                "Subject should start with lowercase after type",
                "Use lowercase for the first letter of the subject",
            )
        )
    if subject.endswith(_SUBJECT_TERMINATORS):
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
        ``m`` より後ろの文字列（``-m`` 単独や ``-am`` は空文字）。フラグ（`short_option_flags`）が
        ``m`` で終わらない（``m`` が無い、または値を取る別の短形式が先に現れた）場合は None。
    """
    flags = short_option_flags(token)
    if not flags.endswith("m"):
        return None
    return token[1 + len(flags) :]


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


def validate_commit_message(commit_args: list[str]) -> list[dict]:
    """コミットメッセージの件名行の形式を検証し、問題の一覧を返します。

    メッセージはシェルのトークン化を通した commit 引数から読む（引用符の中の ``'``・結合短形式の
    ``-am`` も値どおりに読める）。検査は件名行（1 行目）だけに当てる。本文の長さや本文末尾の句点は
    件名の形式とは関係が無い。

    Args:
        commit_args: `parse_git_commit_command` が返した commit の引数トークン列です。

    Returns:
        問題の一覧。メッセージがない・本文が実行時にしか決まらない場合は空リストを返します。
    """
    message = _message_from_args(commit_args)
    if not message:
        return []
    # `-m "$(cat <<'EOF' ...)"` のようなコマンド置換は、本文が実行時にしか決まらない。字面を本文として
    # 検査すると置換の書き出し（`$(cat <<`）を本文と取り違え、毎回の commit で形式違反を警告する。
    if message.startswith(("$(", "`")):
        return []
    return _commit_message_issues(message.splitlines()[0])
