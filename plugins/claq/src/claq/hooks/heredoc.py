"""heredoc のデータ本文の除去。

保護フックはコマンド文字列をトークン化して判定するが、``shlex`` は heredoc を解釈しない。本文が
データだと静的に確定できる場合だけ、トークン化の前段で本文を取り除く（docs/adr/shell-analysis-boundary.md）。
"""

from __future__ import annotations

import re

from claq.hooks.shell_tokens import QuoteState, is_shell_wrapper_token, strip_line_comments, tokenize

# 実行ファイルではないが、渡されたテキストを現在のシェルで実行するビルトイン。
# ``. /dev/stdin <<'EOF'`` 等の本文はコマンドとして実行されるため、データとして
# 剥がすと誤通過になる。
_BODY_EXECUTING_BUILTINS = frozenset({".", "source", "eval"})

# heredoc 演算子。``<<<``（herestring）を heredoc と読まないよう、前後に ``<`` が
# 無いことを要求する（lookahead だけだと ``<<<EOF`` が offset 1 で再マッチする）。
_HEREDOC_OPERATOR_RE = re.compile(
    r"(?<!<)<<(?!<)(-?)[ \t]*(?:'(?P<squote>[^']*)'|\"(?P<dquote>[^\"]*)\"|\\?(?P<bare>[A-Za-z_][A-Za-z0-9_.-]*))"
)

# 演算子行がこれらで終わる場合、本文の開始位置が次行とは限らないか、本文の行き先が次行以降にある
# （``cat <<'EOF' |`` 改行 ``bash``、``cat <<'EOF' | {`` 改行 ``bash; }``）。剥がさない側へ倒す。
_HEREDOC_CONTINUATION_SUFFIXES = ("\\", "|", "&", "{", "(")


def _heredoc_delimiters(line: str, quote: str | None) -> tuple[list[tuple[str, bool]], str | None]:
    """行に現れる heredoc の区切り語を出現順に返し、行末のクォート状態も返す。

    コメント内・クォート内・``\\`` エスケープ後の ``<<`` は heredoc を開始しない。これらを演算子と
    読むと、実行される後続行が「本文」として捨てられ、保護フックが素通りする。クォート状態は行を
    またぐため、呼び出し元が**非本文行だけ**を鎖のように繋いで渡す。本文行・終端行はシェルの語彙
    ではないので、本文中の ``#`` や閉じないアポストロフィ（``don't``）で以降の解析を壊さない。

    Args:
        line: 走査対象の 1 物理行（非本文行）。
        quote: 直前の非本文行から持ち越した未閉鎖クォート文字。無ければ None。

    Returns:
        ((区切り語, タブ剥がし可（``<<-``）) のリスト, 行末時点の未閉鎖クォート文字)。

    Raises:
        例外は発生しません。
    """
    delimiters: list[tuple[str, bool]] = []
    state = QuoteState(quote)
    for index, char in enumerate(line):
        at_word_start = state.at_word_start
        if not state.advance(char):
            continue
        if char == "#" and at_word_start:
            break
        match = _HEREDOC_OPERATOR_RE.match(line, index) if char == "<" else None
        if match is not None:
            # ``<<''`` の空文字列区切りを落とさないため or 連結にしない。
            word = next(value for value in match.group("squote", "dquote", "bare") if value is not None)
            delimiters.append((word, match.group(1) == "-"))
    return delimiters, state.quote


def _line_keeps_heredoc_bodies(line: str) -> bool:
    """演算子行を見て、本文を剥がさずに残すべきかを判定する。

    本文が実行されうる形（シェル起動・本文を実行するビルトイン・パイプや継続で
    次行へ繋がる形）はすべて残す側へ倒す。

    Args:
        line: heredoc 演算子を含む物理行。

    Returns:
        本文を残すなら True、剥がしてよいなら False。

    Raises:
        例外は発生しません。
    """
    if strip_line_comments(line).rstrip().endswith(_HEREDOC_CONTINUATION_SUFFIXES):
        return True
    return any(is_shell_wrapper_token(token) or token in _BODY_EXECUTING_BUILTINS for token in tokenize(line))


def _consume_heredoc_bodies(
    lines: list[str], start: int, delimiters: list[tuple[str, bool]]
) -> tuple[int, list[str]] | None:
    """区切り語の本文を読み飛ばし、終端行だけを残して返す。

    本文の走査中に ``<<`` を再検出しない。``<<'OUTER'`` の本文に ``<<'INNER'`` が
    現れる形で走査が同期ずれを起こすのを防ぐ。

    Args:
        lines: コマンド全体の物理行リスト。
        start: 本文開始行の index。
        delimiters: 演算子行が宣言した (区切り語, タブ剥がし可) のリスト。

    Returns:
        (再開する index, 出力に残す終端行のリスト)。1 つでも終端行が見つからなければ None
        （呼び出し側が剥がすのをやめる）。

    Raises:
        例外は発生しません。
    """
    index = start
    terminators: list[str] = []
    for word, strip_tabs in delimiters:
        while index < len(lines):
            line = lines[index]
            index += 1
            if (line.lstrip("\t") if strip_tabs else line) == word:
                terminators.append(line)
                break
        else:
            return None
    return index, terminators


def strip_data_heredoc_bodies(command: str) -> str:
    """heredoc の**データ**本文をコマンド文字列から取り除く。

    heredoc 本文はコマンドの語彙に入らないため、そのままトークン化すると散文が
    実行命令として読まれ、フックの禁止事項を名指しする説明文書自体が書けなくなる。
    本文が実行されうる場合は剥がさず検出側を維持する（docs/adr/shell-analysis-boundary.md）:

    1. 演算子行にシェルか本文実行ビルトインがある（``bash <<'EOF'`` / ``cat <<'EOF' | bash`` /
       ``. /dev/stdin <<'EOF'``）
    2. 演算子行が継続演算子（``\\`` ``|`` ``&``）か開いたグループ（``{`` ``(``）で終わる。行末コメントは
       除いて見る
    3. 終端行が見つからない（未終端）
    4. ``<<`` が heredoc 演算子ではない（コメント内・クォート内・``\\`` エスケープ後）。後続行は
       実行されるコマンドなので、剥がすと素通りする

    1〜3 に当たったら、その行以降は一切剥がさない。終端行は残すので冪等（``f(f(x)) == f(x)``）。

    Args:
        command: 元のコマンド文字列。

    Returns:
        データ本文を除去した文字列。剥がせないと判断した場合はその行以降を入力のまま残す。

    Raises:
        例外は発生しません。
    """
    if "<<" not in command:
        return command
    lines = command.split("\n")
    output: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        output.append(line)
        index += 1
        delimiters, quote = _heredoc_delimiters(line, quote)
        if not delimiters:
            continue
        consumed = None if _line_keeps_heredoc_bodies(line) else _consume_heredoc_bodies(lines, index, delimiters)
        if consumed is None:
            output.extend(lines[index:])
            break
        index, terminators = consumed
        output.extend(terminators)
    return "\n".join(output)
