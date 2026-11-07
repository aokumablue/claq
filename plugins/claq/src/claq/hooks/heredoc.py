"""heredoc のデータ本文の除去。

保護フックはコマンド文字列をトークン化して判定するが、``shlex`` は heredoc を解釈しない。本文が
データだと静的に確定できる場合だけ、トークン化の前段で本文を取り除く（docs/adr/shell-analysis-boundary.md）。
"""

from __future__ import annotations

import re

from claq.hooks.shell_tokens import (
    COMMENT_PRECEDING_CHARS,
    CONTINUED_WORD,
    SHELL_SEPARATORS,
    QuoteState,
    command_dialect_variants,
    is_shell_wrapper_token,
    strip_line_comments,
    tokenize,
    tokenize_with_status,
)

# 実行ファイルではないが、渡されたテキストを現在のシェルで実行するビルトイン。
# ``. /dev/stdin <<'EOF'`` 等の本文はコマンドとして実行されるため、データとして
# 剥がすと誤通過になる（docs/adr/shell-analysis-boundary.md の除去しない条件 1）。
_BODY_EXECUTING_BUILTINS = frozenset({".", "source", "eval"})

# heredoc 演算子。``<<<``（herestring）を heredoc と読まないよう、前後に ``<`` が
# 無いことを要求する（lookahead だけだと ``<<<EOF`` が offset 1 で再マッチする）。
# 区切り語の直後は語の終わりに限る。``<<E"O"F`` の区切り語はシェルでは ``EOF`` で、途中の ``E`` までを
# 読むと本文の後ろの実行行まで剥がすため、読み切れない区切り語の行以降は本文を剥がさない。
# クォート付きの語の ``\\`` も同じ（``"E\\\\F"`` の実体は ``E\\F``。``'E\\'`` の終端行は残すと次行と連結する）。
_HEREDOC_OPERATOR_RE = re.compile(
    r"(?<!<)<<(?!<)(-?)[ \t]*"
    r"(?:'(?P<squote>[^'\\]*)'|\"(?P<dquote>[^\"\\$`]*)\"|\\?(?P<bare>[A-Za-z_][A-Za-z0-9_.-]*))"
    r"(?=[ \t;|&()<>]|$)"
)

# heredoc 演算子の開始。`_HEREDOC_OPERATOR_RE` と同じ条件で、区切り語を読めたかは問わない。
_HEREDOC_START_RE = re.compile(r"(?<!<)<<(?!<)")

# 演算子行がこれらで終わる場合、本文の開始位置が次行とは限らないか、本文の行き先が次行以降にある
# （``cat <<'EOF' |`` 改行 ``bash``、``cat <<'EOF' | {`` 改行 ``bash; }``）。剥がさない側へ倒す。
_HEREDOC_CONTINUATION_SUFFIXES = ("\\", "|", "&", "{", "(")


def _operator_start_offsets(line: str, quote: str | None) -> tuple[list[int], str | None]:
    """heredoc 演算子になりうる ``<`` の位置と、行末のクォート状態を返す。

    コメント内・クォート内・``\\`` エスケープ後の ``<`` は heredoc を開始しない。
    これらを演算子として採ると、実際には実行される後続行が「本文」として捨てられ、
    保護フックが素通りする。

    クォート状態は行をまたぐため、呼び出し元が**非本文行だけ**を鎖のように繋いで
    渡す。本文行・終端行はシェルの語彙ではないので、本文中の ``#`` や閉じない
    アポストロフィ（``don't``）で以降の解析を壊さないよう状態を更新しない。
    行末の ``\\`` が消費するのは改行自体なので、エスケープ状態は持ち越さない。ただし語の
    途中の行継続は語が次行へ続くので `CONTINUED_WORD` を渡す（次行の先頭の ``#`` は語の一部）。

    `strip_line_comments` を前段で単純適用する形は採れない。heredoc 本文中の
    ``#`` や閉じないアポストロフィまで巻き込んで壊すため。

    Args:
        line: 走査対象の 1 物理行（非本文行）。
        quote: 直前の非本文行から持ち越した未閉鎖クォート文字か `CONTINUED_WORD`。無ければ None。

    Returns:
        (演算子候補となる ``<`` の位置リスト, 行末時点の未閉鎖クォート文字か `CONTINUED_WORD`)。

    Raises:
        例外は発生しません。
    """
    offsets: list[int] = []
    state = QuoteState(quote)
    for index, char in enumerate(line):
        at_word_start = state.at_word_start
        if state.advance(char):
            if char == "#" and at_word_start:
                break
            if char == "<":
                offsets.append(index)
    if state.escaped and state.quote is None and not state.at_word_start:
        return offsets, CONTINUED_WORD
    return offsets, state.quote


def _heredoc_delimiters(line: str, quote: str | None) -> tuple[list[tuple[str, bool]], str | None]:
    """行に現れる heredoc の区切り語を出現順に返し、行末のクォート状態も返す。

    照合は `_operator_start_offsets` が返した位置からのみ行う。照合は非重複にする
    （直前の照合範囲内の候補を飛ばす）。位置ごとに独立して照合すると、クォート内の
    ``<<`` を跨いだ重複照合が新たな区切り語を生み、本文を余計に剥がして誤通過側へ
    倒れうるため。

    Args:
        line: 走査対象の 1 物理行（非本文行）。
        quote: 直前の非本文行から持ち越した未閉鎖クォート文字か `CONTINUED_WORD`。無ければ None。

    Returns:
        ((区切り語, タブ剥がし可（``<<-``）) のリスト, 行末時点の未閉鎖クォート文字か `CONTINUED_WORD`)。

    Raises:
        例外は発生しません。
    """
    offsets, next_quote = _operator_start_offsets(line, quote)
    delimiters = []
    consumed_to = 0
    for offset in offsets:
        if offset < consumed_to:
            continue
        match = _HEREDOC_OPERATOR_RE.match(line, offset)
        if match is None:
            continue
        # ``<<''`` の空文字列区切りを落とさないため or 連結にしない。
        word = next(value for value in match.group("squote", "dquote", "bare") if value is not None)
        delimiters.append((word, match.group(1) == "-"))
        consumed_to = match.end()
    return delimiters, next_quote


def _unescaped_reading_comment_start(line: str, quote: str | None) -> int:
    """``\\`` をエスケープにしない読み（PowerShell）で、コメントが始まる位置を返す。

    POSIX 読みで語の途中の ``#``（``\\ #``）も、この読みでは区切り文字の直後なのでコメントになる。
    クォート内の ``#`` はどちらの読みでもコメントではない。

    Args:
        line: 走査対象の 1 物理行（非本文行）。
        quote: 直前の非本文行から持ち越した未閉鎖クォート文字か `CONTINUED_WORD`。無ければ None。

    Returns:
        コメントの開始位置。無ければ行の長さ。

    Raises:
        例外は発生しません。
    """
    open_quote = None if quote == CONTINUED_WORD else quote
    previous = "\n"
    for index, char in enumerate(line):
        if open_quote is not None:
            open_quote = None if char == open_quote else open_quote
        elif char in ("'", '"'):
            open_quote = char
        elif char == "#" and previous in COMMENT_PRECEDING_CHARS:
            return index
        previous = char
    return len(line)


def _line_has_unreadable_heredoc(line: str, quote: str | None) -> bool:
    """行の ``<<`` に、本文の範囲を静的に確定できないものがあるかを判定する。

    区切り語を語の終わりまで読めない演算子、方言によってコメントの中になる演算子、算術の中の
    演算子（``((1<<x))``）が当たる。どれも本文の範囲が分からない。演算子ではないと読み進めると、本物の本文に書かれた ``<<`` を
    演算子と読み直し、本物の終端の後ろの実行行まで剥がす。

    Args:
        line: 走査対象の 1 物理行（非本文行）。
        quote: 直前の非本文行から持ち越した未閉鎖クォート文字か `CONTINUED_WORD`。無ければ None。

    Returns:
        確定できない ``<<`` があれば True。

    Raises:
        例外は発生しません。
    """
    offsets, _ = _operator_start_offsets(line, quote)
    comment_start = _unescaped_reading_comment_start(line, quote)
    return any(
        _HEREDOC_START_RE.match(line, offset) is not None
        and (offset > comment_start or "((" in line[:offset] or _HEREDOC_OPERATOR_RE.match(line, offset) is None)
        for offset in offsets
    )


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
    lines: list[str], start: int, delimiters: list[tuple[str, bool]], *, joins_continued_lines: bool
) -> tuple[int, list[str]] | None:
    """区切り語の本文を読み飛ばし、終端行だけを残して返す。

    本文の走査中に ``<<`` を再検出しない。``<<'OUTER'`` の本文に ``<<'INNER'`` が
    現れる形で走査が同期ずれを起こすのを防ぐ。

    Args:
        lines: コマンド全体の物理行リスト。
        start: 本文開始行の index。
        delimiters: 演算子行が宣言した (区切り語, タブ剥がし可) のリスト。
        joins_continued_lines: 未クォートの区切り語があり、本文の ``\\`` + 改行が連結するなら True。

    Returns:
        (再開する index, 出力に残す行のリスト)。1 つでも終端行が見つからない、または連結する本文に
        行継続がある（シェルは連結してから終端を探す）なら None（呼び出し側が剥がすのをやめる）。

    Raises:
        例外は発生しません。
    """
    index = start
    kept = []
    for word, strip_tabs in delimiters:
        while index < len(lines):
            candidate = lines[index]
            index += 1
            if joins_continued_lines and candidate.endswith("\\"):
                return None
            if (candidate.lstrip("\t") if strip_tabs else candidate) == word:
                kept.append(candidate)
                break
        else:
            return None
    return index, kept


def _reaches_a_shell(command: str) -> bool:
    """除去後のコマンドで、本文を受け取る位置に本文を実行しうるトークンがあるかを判定する。

    本文が届くのは heredoc 自身のコマンドとパイプの先だけで、改行で分かれた ``bash \\`` + ``<<'EOF'`` や
    ``(cat <<'EOF'`` + 本文 + ``) | bash`` は演算子行だけを見ても分からない。後ろの独立した文
    （``. .venv/bin/activate``）は本文を受け取らないので数えない。

    Args:
        command: 本文を除去した後のコマンド文字列。

    Returns:
        heredoc 演算子と同じセグメントかパイプの先に、シェルか本文実行ビルトインがあれば True。

    Raises:
        例外は発生しません。
    """
    receives_body = executes = False
    previous = ""
    # トークン化は ``<<`` を ``<`` 2 つに割るので、連続する ``<`` を演算子と読む。
    for token in [*tokenize(command), ";"]:
        if token in SHELL_SEPARATORS:
            if receives_body and executes:
                return True
            receives_body, executes = token == "|", False
        elif token == "<" == previous:
            receives_body = True
        elif is_shell_wrapper_token(token) or token in _BODY_EXECUTING_BUILTINS:
            executes = True
        previous = token
    return False


def strip_data_heredoc_bodies(command: str) -> str:
    """heredoc の**データ**本文をコマンド文字列から取り除く。

    heredoc 本文はコマンドの語彙に入らないため、そのままトークン化すると散文が
    実行命令として読まれ、フックの禁止事項を名指しする説明文書自体が書けなくなる。
    本文範囲は演算子・区切り語・終端行だけで決まる環境非依存の構文なので、本文が
    データだと**静的に確定できる場合だけ**剥がす。次のいずれかに当たれば剥がさず
    検出側を維持する（docs/adr/shell-analysis-boundary.md）:

    1. 演算子行に本文を実行するトークンがある（``bash <<'EOF'`` /
       ``cat <<'EOF' | bash`` / ``. /dev/stdin <<'EOF'`` / ``eval "$(cat <<'EOF'``）。
       その行以降は一切剥がさない。除去後に本文を受け取る位置（演算子と同じセグメント・パイプの先）に
       シェルが残るときも除去を取り消す
    2. 演算子行が継続演算子で終わる（本文開始が次行とは限らない）。その行以降は一切剥がさない
    3. 終端行が見つからない（未終端）
    4. ``<<`` がそもそも heredoc 演算子ではない（コメント内・クォート内・
       ``\\`` エスケープ後）。後続行は実行されるコマンドなので剥がすと素通りする。
    5. 本文の範囲を確定できない ``<<`` がある（`_line_has_unreadable_heredoc`）、演算子行の行頭か
       行末でクォートが開いている、未クォートの本文が行継続で終わる。その行以降は一切剥がさない。

    終端行を残すため冪等（``f(f(x)) == f(x)``）。

    Args:
        command: 元のコマンド文字列。

    Returns:
        データ本文を除去した文字列。剥がせないと判断した場合は入力そのまま。

    Raises:
        例外は発生しません。
    """
    if "<<" not in command:
        return command

    lines = command.split("\n")
    output = []
    index = 0
    # 非本文行だけを繋いだクォート状態。本文行・終端行では更新しない。
    quote: str | None = None
    while index < len(lines):
        line = lines[index]
        output.append(line)
        index += 1
        if "<<" in line and _line_has_unreadable_heredoc(line, quote):
            return "\n".join(output + lines[index:])
        delimiters, next_quote = _heredoc_delimiters(line, quote)
        if delimiters and (quote is not None or next_quote is not None or _line_keeps_heredoc_bodies(line)):
            return "\n".join(output + lines[index:])
        quote = next_quote
        if not delimiters:
            continue
        unquoted = any(match.group("bare") for match in _HEREDOC_OPERATOR_RE.finditer(line))
        consumed = _consume_heredoc_bodies(lines, index, delimiters, joins_continued_lines=unquoted)
        if consumed is None:
            return "\n".join(output + lines[index:])
        index, kept = consumed
        output.extend(kept)
    stripped = "\n".join(output)
    return command if _reaches_a_shell(stripped) else stripped


def _has_heredoc_operator(command: str) -> bool:
    """引用の外に heredoc 演算子があるかを判定する。

    ``git commit -m "$(cat <<'EOF'`` の ``<<`` は引用の中で、本文はこの引用の中身として読める。

    Args:
        command: 検査対象のコマンド文字列。

    Returns:
        引用・コメントの外に ``<<`` があれば True。

    Raises:
        例外は発生しません。
    """
    quote: str | None = None
    for line in command.split("\n"):
        offsets, quote = _operator_start_offsets(line, quote)
        if any(_HEREDOC_START_RE.match(line, offset) for offset in offsets):
            return True
    return False


def command_readings(command: str) -> tuple[str, ...]:
    """保護フックが検査する読み方を返す。方言ごとの読みに、必要なら物理行ごとの読みを足す。

    残した本文・終端行の引用符（``don't``）は、シェルでは本文の中でも ``shlex`` では引用の開始になり、
    後ろの実行行を飲み込む。解析できない入力では改行が区切りにならない。どちらも行全体の読みでは
    実行行が見えない。

    Args:
        command: データ本文を除去した後のコマンド文字列。

    Returns:
        検査すべき読み方のタプル。

    Raises:
        例外は発生しません。
    """
    readings = command_dialect_variants(command)
    if "\n" in command and (_has_heredoc_operator(command) or not tokenize_with_status(command)[1]):
        return (*readings, *(reading for line in command.split("\n") for reading in command_dialect_variants(line)))
    return readings
