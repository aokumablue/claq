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


def _heredoc_matches(line: str, quote: str | None) -> tuple[list[re.Match[str]], str | None]:
    """行に現れる heredoc 演算子の一致を出現順に返し、行末のクォート状態も返す。

    照合は `_operator_start_offsets` が返した位置からのみ行う。照合は非重複にする
    （直前の照合範囲内の候補を飛ばす）。位置ごとに独立して照合すると、クォート内の
    ``<<`` を跨いだ重複照合が新たな区切り語を生み、本文を余計に剥がして誤通過側へ
    倒れうるため。

    Args:
        line: 走査対象の 1 物理行（非本文行）。
        quote: 直前の非本文行から持ち越した未閉鎖クォート文字か `CONTINUED_WORD`。無ければ None。

    Returns:
        (演算子の一致のリスト, 行末時点の未閉鎖クォート文字か `CONTINUED_WORD`)。

    Raises:
        例外は発生しません。
    """
    offsets, next_quote = _operator_start_offsets(line, quote)
    matches = []
    consumed_to = 0
    for offset in offsets:
        if offset < consumed_to:
            continue
        match = _HEREDOC_OPERATOR_RE.match(line, offset)
        if match is None:
            continue
        matches.append(match)
        consumed_to = match.end()
    return matches, next_quote


def _heredoc_delimiters(line: str, quote: str | None) -> tuple[list[tuple[str, bool]], str | None]:
    """行に現れる heredoc の区切り語を出現順に返し、行末のクォート状態も返す。

    Args:
        line: 走査対象の 1 物理行（非本文行）。
        quote: 直前の非本文行から持ち越した未閉鎖クォート文字か `CONTINUED_WORD`。無ければ None。

    Returns:
        ((区切り語, タブ剥がし可（``<<-``）) のリスト, 行末時点の未閉鎖クォート文字か `CONTINUED_WORD`)。

    Raises:
        例外は発生しません。
    """
    matches, next_quote = _heredoc_matches(line, quote)
    # ``<<''`` の空文字列区切りを落とさないため or 連結にしない。
    return [
        (next(value for value in match.group("squote", "dquote", "bare") if value is not None), match.group(1) == "-")
        for match in matches
    ], next_quote


def _has_unquoted_delimiter(line: str, quote: str | None) -> bool:
    """行の heredoc 区切り語に、クォートも ``\\`` 前置も無いものがあるかを判定する。

    シェルは区切り語のどこかにクォートか ``\\`` があれば本文を展開せず、``\\`` + 改行の連結もしない
    （``<<\\EOF`` もこちら）。何も付かない区切り語の本文だけが連結される。

    Args:
        line: 走査対象の 1 物理行（非本文行）。
        quote: 直前の非本文行から持ち越した未閉鎖クォート文字か `CONTINUED_WORD`。無ければ None。

    Returns:
        クォートも ``\\`` 前置も無い区切り語があれば True。

    Raises:
        例外は発生しません。
    """
    matches, _ = _heredoc_matches(line, quote)
    return any(
        match.group("bare") is not None and match.string[match.start("bare") - 1] != "\\" for match in matches
    )


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


def _ends_with_continuation(line: str) -> bool:
    """行末が行継続（奇数個の ``\\``）かを判定する。偶数個は ``\\`` 自身のエスケープで継続ではない。

    Args:
        line: 1 物理行。

    Returns:
        行末が ``\\`` + 改行の継続なら True。

    Raises:
        例外は発生しません。
    """
    return (len(line) - len(line.rstrip("\\"))) % 2 == 1


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
        joins_continued_lines: 何も付かない区切り語があり、本文の ``\\`` + 改行が連結するなら True。
            シェルは連結してから終端を探すので、連結した行を区切り語と照合する。

    Returns:
        (再開する index, 出力に残す行のリスト)。1 つでも終端行が見つからない、または ``<<-`` の本文で
        連結が起きる（タブ剥がしと連結の順序を静的に確定しない）なら None（呼び出し側が剥がすのをやめる）。
        終端が連結した行なら、その物理行をすべて残す。

    Raises:
        例外は発生しません。
    """
    index = start
    kept: list[str] = []
    for word, strip_tabs in delimiters:
        while index < len(lines):
            first = index
            candidate = lines[index]
            index += 1
            while joins_continued_lines and _ends_with_continuation(candidate) and index < len(lines):
                if strip_tabs:
                    return None
                candidate = candidate[:-1] + lines[index]
                index += 1
            if (candidate.lstrip("\t") if strip_tabs else candidate) == word:
                kept.extend(lines[first:index])
                break
        else:
            return None
    return index, kept


# 文の内側へ深さを 1 段下げる予約語（コマンド位置のときだけ）と、戻す予約語。
_GROUP_OPENERS = frozenset({"{", "if", "case", "for", "while", "until", "select"})
_GROUP_CLOSERS = frozenset({"}", "fi", "esac", "done"})

# 区切り（`SHELL_SEPARATORS`）のほかに、直後のトークンをコマンド位置にする予約語。
_COMMAND_POSITION_KEYWORDS = frozenset({"{", "then", "do", "else", "elif", "!"})

# 深さ 0 で文を分ける区切り。``|`` はパイプラインを繋ぐので含めない。
_STATEMENT_SEPARATORS = frozenset({";", "&&", "||", "&"})


def _is_command_position(previous: str) -> bool:
    """直前のトークンを見て、次のトークンがコマンド位置にあるかを判定する。

    Args:
        previous: 直前のトークン。先頭なら ``;`` を渡す。

    Returns:
        区切りか `_COMMAND_POSITION_KEYWORDS` の直後なら True。

    Raises:
        例外は発生しません。
    """
    return previous in SHELL_SEPARATORS or previous in _COMMAND_POSITION_KEYWORDS


def _top_level_statements(tokens: list[str]) -> list[list[str]]:
    """トークン列を、グループ・サブシェル・複合コマンドの外側にある文ごとに分ける。

    heredoc の本文が届きうるのは、heredoc と同じトップレベルの文の中だけ。``|`` の直後に続く ``;``（改行。
    空行・コメント行で何個でも続く）と ``&``（``|&``）はパイプラインの続きとして扱う。深さが負になるか最後に 0 へ戻らないなら、文の境界を
    確定できないのでコマンド全体を 1 文とする。

    Args:
        tokens: `tokenize` が返したトークン列。

    Returns:
        文ごとのトークンリスト。

    Raises:
        例外は発生しません。
    """
    statements: list[list[str]] = [[]]
    depth = 0
    previous = ";"
    for token in tokens:
        at_command_position = _is_command_position(previous)
        if token == "(" or (at_command_position and token in _GROUP_OPENERS):
            depth += 1
        elif token == ")" or (at_command_position and token in _GROUP_CLOSERS):
            depth -= 1
            if depth < 0:
                return [tokens]
        if depth == 0 and token in _STATEMENT_SEPARATORS and previous != "|":
            statements.append([])
        else:
            statements[-1].append(token)
        previous = "|" if previous == "|" and token in (";", "&") else token
    return statements if depth == 0 else [tokens]


def _statement_feeds_interpreter(lines: list[str], terminators: set[int]) -> bool:
    """heredoc を含むトップレベルの文に、本文を実行しうるトークンがあるかを判定する。

    本文はその文の中のどのコマンドへも、パイプ・グループ・複合コマンドの stdin・``exec`` を経て届きうる。
    シェルと本文実行ビルトインは文の中の位置を問わない（``sudo bash``・``env sh``・``xargs sh`` を含める）。
    ``exec`` はビルトインでラッパから起動できないので、コマンド位置だけを数える（``docker exec`` は数えない）。
    終端行は区切り語でシェルの構文ではないので、トークン化の前に空行へ置き換える。

    Args:
        lines: 本文を除去した後の物理行。
        terminators: ``lines`` のうち残した終端行の index。

    Returns:
        本文を実行しうる文があれば True。

    Raises:
        例外は発生しません。
    """
    text = "\n".join("" if index in terminators else line for index, line in enumerate(lines))
    for statement in _top_level_statements(tokenize(text)):
        has_heredoc = any(token == "<" == following for token, following in zip(statement, statement[1:], strict=False))
        if has_heredoc and any(
            is_shell_wrapper_token(token)
            or token in _BODY_EXECUTING_BUILTINS
            or (token == "exec" and _is_command_position(previous))
            for previous, token in zip([";", *statement], statement, strict=False)
        ):
            return True
    return False


def _walk_heredoc_bodies(command: str) -> tuple[list[str], set[int], int | None]:
    """物理行を走査し、データ本文を除いた行・残した終端行・除去を打ち切った位置を返す。

    条件 1（演算子行）・2・3・4・5 は `strip_data_heredoc_bodies` を参照。

    Args:
        command: 元のコマンド文字列。

    Returns:
        (出力する物理行, 出力のうち残した終端行の index, 除去を打ち切った出力上の行 index か None)。
        打ち切った行以降は入力のまま出力に入る。

    Raises:
        例外は発生しません。
    """
    lines = command.split("\n")
    output: list[str] = []
    terminators: set[int] = set()
    index = 0
    # 非本文行だけを繋いだクォート状態。本文行・終端行では更新しない。
    quote: str | None = None
    while index < len(lines):
        line = lines[index]
        output.append(line)
        index += 1
        if "<<" in line and _line_has_unreadable_heredoc(line, quote):
            return output + lines[index:], terminators, len(output) - 1
        delimiters, next_quote = _heredoc_delimiters(line, quote)
        if delimiters and (quote is not None or next_quote is not None or _line_keeps_heredoc_bodies(line)):
            return output + lines[index:], terminators, len(output) - 1
        previous_quote, quote = quote, next_quote
        if not delimiters:
            continue
        consumed = _consume_heredoc_bodies(
            lines, index, delimiters, joins_continued_lines=_has_unquoted_delimiter(line, previous_quote)
        )
        if consumed is None:
            return output + lines[index:], terminators, len(output) - 1
        index, kept = consumed
        terminators.update(range(len(output), len(output) + len(kept)))
        output.extend(kept)
    return output, terminators, None


def strip_data_heredoc_bodies(command: str) -> str:
    """heredoc の**データ**本文をコマンド文字列から取り除く。

    heredoc 本文はコマンドの語彙に入らないため、そのままトークン化すると散文が
    実行命令として読まれ、フックの禁止事項を名指しする説明文書自体が書けなくなる。
    本文範囲は演算子・区切り語・終端行だけで決まる環境非依存の構文なので、本文が
    データだと**静的に確定できる場合だけ**剥がす。次のいずれかに当たれば剥がさず
    検出側を維持する（docs/adr/shell-analysis-boundary.md）:

    1. 本文を実行しうるトークンが本文の届く範囲にある。演算子行にシェルか本文実行ビルトインがあれば
       その行以降は一切剥がさない（``bash <<'EOF'`` / ``cat <<'EOF' | bash`` / ``. /dev/stdin <<'EOF'``）。
       除去後に heredoc を含むトップレベルの文にシェル・本文実行ビルトイン・``exec`` があれば
       除去を取り消す（`_statement_feeds_interpreter`）
    2. 演算子行が継続演算子（``\\`` ``|`` ``&``）か開いたグループ（``{`` ``(``）で終わる。行末コメントは
       除いて見る（本文開始が次行とは限らないか、本文の行き先が次行以降にある）。その行以降は一切剥がさない
    3. 終端行が見つからない（未終端）
    4. ``<<`` がそもそも heredoc 演算子ではない（コメント内・クォート内・
       ``\\`` エスケープ後）。後続行は実行されるコマンドなので剥がすと素通りする。
    5. 本文の範囲を確定できない ``<<`` がある（`_line_has_unreadable_heredoc`）、演算子行の行頭か
       行末でクォートが開いている。その行以降は一切剥がさない。

    何も付かない区切り語の本文は ``\\`` + 改行を連結してから終端を探す（シェルと同じ）。連結した行が
    区切り語なら、そこまでが本文になる。

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
    lines, terminators, _ = _walk_heredoc_bodies(command)
    return command if _statement_feeds_interpreter(lines, terminators) else "\n".join(lines)


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


def _line_reading_start(command: str) -> int | None:
    """物理行ごとの読みを足し始める行 index を返す。足さないなら None。

    行全体の読みで実行行が見えなくなるのは、``shlex`` がシェルと違う範囲を引用と読むときだけ。
    当たるのは、解析できない入力（先頭から）、本文を残した範囲（除去を打ち切った行か、除去を取り消した
    コマンドの先頭から）、引用符か ``\\`` を含む終端行（``<<"a'b"`` の終端行。その行から）。除去できた本文の
    後ろの、別の文の複数行クォート（PR 本文・コミットメッセージ）は行全体の読みで正しく読める。

    Args:
        command: データ本文を除去した後のコマンド文字列。

    Returns:
        物理行の読みを足し始める行 index か None。

    Raises:
        例外は発生しません。
    """
    if not tokenize_with_status(command)[1]:
        return 0
    if not _has_heredoc_operator(command):
        return None
    lines, terminators, kept_from = _walk_heredoc_bodies(command)
    if _statement_feeds_interpreter(lines, terminators):
        return 0
    quoted_terminators = [index for index in terminators if any(char in lines[index] for char in "'\"\\")]
    return min([*quoted_terminators, *([] if kept_from is None else [kept_from])], default=None)


def command_readings(command: str) -> tuple[str, ...]:
    """保護フックが検査する読み方を返す。方言ごとの読みに、必要なら物理行ごとの読みを足す。

    残した本文・終端行の引用符（``don't``）は、シェルでは本文の中でも ``shlex`` では引用の開始になり、
    後ろの実行行を飲み込む。解析できない入力では改行が区切りにならない。どちらも行全体の読みでは
    実行行が見えないので、`_line_reading_start` の行から後ろを 1 行ずつも読む。

    Args:
        command: データ本文を除去した後のコマンド文字列。

    Returns:
        検査すべき読み方のタプル。

    Raises:
        例外は発生しません。
    """
    readings = command_dialect_variants(command)
    if "\n" not in command:
        return readings
    start = _line_reading_start(command)
    if start is None:
        return readings
    return (*readings, *(reading for line in command.split("\n")[start:] for reading in command_dialect_variants(line)))
