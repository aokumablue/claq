"""シェルコマンド文字列の解析（トークン化・区切り・heredoc・ラッパー抽出・走査上限）。

保護フックが共有する、シェル文字列の解析だけを持つ葉モジュール。出力や保護対象の
判定は ``hook_common`` が持ち、このモジュールは ``hook_common`` を import しない。
"""

from __future__ import annotations

import re
import shlex

# 1 コマンドに許すシェルトークン数の上限。
# `block_no_verify` の走査はトークン数の二次で、上限が無いと stdin 上限の内側で
# hooks.json の timeout（15 秒）を超える入力を作れる。host が kill した hook は
# exit code を返さず silent fail-open になるため、超過は通さず BLOCKED で返す
# （docs/adr/hook-failure-direction.md）。5,000 は timeout に対し 20 倍以上の余裕があり、
# 正当なシェルコマンドがこの規模になることは実務上ない。
MAX_COMMAND_TOKENS = 5000

# 1 コマンドに許すバイト数の上限。
# 単一の巨大トークンはトークン数の上限を素通りするが、`shlex` の走査コストは
# バイト数の二次で、1MB で timeout に迫る。64KB は timeout に対し 75 倍以上の余裕が
# あり、トークン上限に収まる正当なコマンド（40KB 程度）を落とさない。
MAX_COMMAND_BYTES = 64 * 1024

# コマンド全体をセグメントに割るシェル区切りトークン。区切り文字に密着した
# トークン（``status;echo``）を全フックが同じ規則で割るため 1 箇所に持つ。
_SHELL_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "(", ")"})

# ``sh -c`` 再帰と heredoc 本文判定で共有する既知シェル実行ファイル（basename 判定）。
# 別集合を持つと「本文が実行されるか」の判定軸が 2 つに割れる。
SHELL_WRAPPER_EXECUTABLES = frozenset({"sh", "bash", "zsh", "dash"})

# 実行ファイルではないが、渡されたテキストを現在のシェルで実行するビルトイン。
# ``. /dev/stdin <<'EOF'`` 等の本文はコマンドとして実行されるため、データとして
# 剥がすと誤通過になる（docs/adr/shell-analysis-boundary.md の除去しない条件 1）。
_BODY_EXECUTING_BUILTINS = frozenset({".", "source", "eval"})

# ``#`` を行コメントの開始として扱ってよい直前の文字。POSIX シェルは語の先頭に
# ある ``#`` だけをコメント開始とみなす（``echo a#b`` の ``#`` は語の一部）。
_COMMENT_PRECEDING_CHARS = frozenset(" \t\n;|&()<>")

# ``-c`` を含む結合短フラグ（``bash -lc 'cmd'``）。単独の ``-c`` だけを見ると
# ``bash -lc`` でラッパー再帰が不発になり、保護フックが素通りする。
_SHELL_COMMAND_FLAG_RE = re.compile(r"-[A-Za-z]*c[A-Za-z]*")

# heredoc 演算子。``<<<``（herestring）を heredoc と読まないよう、前後に ``<`` が
# 無いことを要求する（lookahead だけだと ``<<<EOF`` が offset 1 で再マッチする）。
_HEREDOC_OPERATOR_RE = re.compile(
    r"(?<!<)<<(?!<)(-?)[ \t]*"
    r"(?:'(?P<squote>[^']*)'|\"(?P<dquote>[^\"]*)\"|\\?(?P<bare>[A-Za-z_][A-Za-z0-9_.-]*))"
)

# 演算子行がこれらで終わる場合、本文の開始位置が次行とは限らない
# （``cat <<'EOF' |`` 改行 ``bash``）。剥がさない側へ倒す。
_HEREDOC_CONTINUATION_SUFFIXES = ("\\", "|", "&")


def is_shell_wrapper_token(token: str) -> bool:
    """トークンが既知シェル実行ファイルかを basename で判定する。

    Args:
        token: 判定対象のトークン。

    Returns:
        basename が `SHELL_WRAPPER_EXECUTABLES` に属するなら True。

    Raises:
        例外は発生しません。
    """
    return token.rsplit("/", 1)[-1] in SHELL_WRAPPER_EXECUTABLES


def extract_shell_wrapper_command(segment: list[str]) -> str | None:
    """セグメント内の既知シェル ``-c`` 呼び出しから、ラップされた文字列コマンドを取り出す。

    既知シェルのトークンに続く ``-c`` を含む短フラグを探し、その次のトークンを返す。
    `block_no_verify` と `bash_config_protection` が共有する。片方だけがラッパー再帰を
    持つと、同じコマンドが片方のフックだけを素通りする。再帰の段数は呼び出し側が
    決める（docs/adr/shell-analysis-boundary.md: 2 段以上のネストは非目標）。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。

    Returns:
        ラップされた文字列コマンド。見つからなければ None。

    Raises:
        例外は発生しません。
    """
    for index, token in enumerate(segment):
        if not is_shell_wrapper_token(token):
            continue
        for position in range(index + 1, len(segment)):
            if _SHELL_COMMAND_FLAG_RE.fullmatch(segment[position]):
                return segment[position + 1] if position + 1 < len(segment) else None
    return None


def _operator_start_offsets(line: str, quote: str | None) -> tuple[list[int], str | None]:
    """heredoc 演算子になりうる ``<`` の位置と、行末のクォート状態を返す。

    コメント内・クォート内・``\\`` エスケープ後の ``<`` は heredoc を開始しない。
    これらを演算子として採ると、実際には実行される後続行が「本文」として捨てられ、
    保護フックが素通りする。

    クォート状態は行をまたぐため、呼び出し元が**非本文行だけ**を鎖のように繋いで
    渡す。本文行・終端行はシェルの語彙ではないので、本文中の ``#`` や閉じない
    アポストロフィ（``don't``）で以降の解析を壊さないよう状態を更新しない。
    行末の ``\\`` が消費するのは改行自体なので、エスケープ状態は持ち越さない。

    `_strip_line_comments` を前段で単純適用する形は採れない。heredoc 本文中の
    ``#`` や閉じないアポストロフィまで巻き込んで壊すため。

    Args:
        line: 走査対象の 1 物理行（非本文行）。
        quote: 直前の非本文行から持ち越した未閉鎖クォート文字。無ければ None。

    Returns:
        (演算子候補となる ``<`` の位置リスト, 行末時点の未閉鎖クォート文字)。

    Raises:
        例外は発生しません。
    """
    offsets: list[int] = []
    escaped = False
    in_comment = False
    previous = "\n"
    for index, char in enumerate(line):
        if in_comment:
            continue
        if escaped:
            escaped = False
            previous = char
            continue
        if char == "\\" and quote != "'":
            escaped = True
            previous = char
            continue
        if quote is not None:
            if char == quote:
                quote = None
            previous = char
            continue
        if char in ("'", '"'):
            quote = char
            previous = char
            continue
        if char == "#" and previous in _COMMENT_PRECEDING_CHARS:
            in_comment = True
            continue
        if char == "<":
            offsets.append(index)
        previous = char
    return offsets, quote


def _heredoc_delimiters(line: str, quote: str | None) -> tuple[list[tuple[str, bool]], str | None]:
    """行に現れる heredoc の区切り語を出現順に返し、行末のクォート状態も返す。

    照合は `_operator_start_offsets` が返した位置からのみ行う。照合は非重複にする
    （直前の照合範囲内の候補を飛ばす）。位置ごとに独立して照合すると、クォート内の
    ``<<`` を跨いだ重複照合が新たな区切り語を生み、本文を余計に剥がして誤通過側へ
    倒れうるため。

    Args:
        line: 走査対象の 1 物理行（非本文行）。
        quote: 直前の非本文行から持ち越した未閉鎖クォート文字。無ければ None。

    Returns:
        ((区切り語, タブ剥がし可（``<<-``）) のリスト, 行末時点の未閉鎖クォート文字)。

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
    if line.rstrip().endswith(_HEREDOC_CONTINUATION_SUFFIXES):
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
        (再開する index, 出力に残す行のリスト)。1 つでも終端行が見つからな
        ければ None（未終端として呼び出し側が剥がすのをやめる）。

    Raises:
        例外は発生しません。
    """
    index = start
    kept = []
    for word, strip_tabs in delimiters:
        while index < len(lines):
            candidate = lines[index]
            index += 1
            if (candidate.lstrip("\t") if strip_tabs else candidate) == word:
                kept.append(candidate)
                break
        else:
            return None
    return index, kept


def strip_data_heredoc_bodies(command: str) -> str:
    """heredoc の**データ**本文をコマンド文字列から取り除く。

    heredoc 本文はコマンドの語彙に入らないため、そのままトークン化すると散文が
    実行命令として読まれ、フックの禁止事項を名指しする説明文書自体が書けなくなる。
    本文範囲は演算子・区切り語・終端行だけで決まる環境非依存の構文なので、本文が
    データだと**静的に確定できる場合だけ**剥がす。次のいずれかに当たれば剥がさず
    検出側を維持する（docs/adr/shell-analysis-boundary.md）:

    1. 演算子行に本文を実行するトークンがある（``bash <<'EOF'`` /
       ``cat <<'EOF' | bash`` / ``. /dev/stdin <<'EOF'`` / ``eval "$(cat <<'EOF'``）
    2. 演算子行が継続演算子で終わる（本文開始が次行とは限らない）
    3. 終端行が見つからない（未終端）
    4. ``<<`` がそもそも heredoc 演算子ではない（コメント内・クォート内・
       ``\\`` エスケープ後）。後続行は実行されるコマンドなので剥がすと素通りする。

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
        delimiters, quote = _heredoc_delimiters(line, quote)
        if not delimiters or _line_keeps_heredoc_bodies(line):
            continue
        consumed = _consume_heredoc_bodies(lines, index, delimiters)
        if consumed is None:
            # 未終端。以降は判断材料が無いのでそのまま残す。
            output.extend(lines[index:])
            return "\n".join(output)
        index, kept = consumed
        output.extend(kept)
    return "\n".join(output)


def _strip_line_comments(command: str) -> str:
    """クォート外の ``#`` 行コメントを行末まで取り除く。

    ``shlex`` にコメント処理を任せると、改行を ``;`` へ正規化した後の文字列では
    ``#`` 以降が**入力末尾まで**捨てられ、2 行目以降が未検査になる。``shlex`` は
    語中の ``#`` もコメント開始として扱うため ``echo a#b && git commit --no-verify``
    も素通りする。そこで正規化の前段で POSIX 規則（語の先頭の ``#`` だけ）に従って
    自分で落とし、``shlex`` 側のコメント処理は無効化する。

    クォートが閉じていない入力ではクォート内と判定したまま末尾に達するため、
    コメントは剥がされず検査対象に残る（fail-closed 側）。

    Args:
        command: 元のコマンド文字列。

    Returns:
        行コメントを除去した文字列。改行は保持する。

    Raises:
        例外は発生しません。
    """
    if "#" not in command:
        return command

    result: list[str] = []
    quote: str | None = None
    escaped = False
    in_comment = False
    previous = "\n"
    for char in command:
        if in_comment:
            # コメント中。区切りとして働く行末の改行だけを残す。
            if char == "\n":
                in_comment = False
                result.append(char)
                previous = char
            continue
        if escaped:
            result.append(char)
            escaped = False
            previous = char
            continue
        if char == "\\" and quote != "'":
            result.append(char)
            escaped = True
            previous = char
            continue
        if quote is not None:
            if char == quote:
                quote = None
            result.append(char)
            previous = char
            continue
        if char in ("'", '"'):
            quote = char
            result.append(char)
            previous = char
            continue
        if char == "#" and previous in _COMMENT_PRECEDING_CHARS:
            in_comment = True
            continue
        result.append(char)
        previous = char
    return "".join(result)


def _strip_line_continuations(command: str) -> str:
    """クォート外・二重クォート内の行継続（``\\`` + 改行）を除去する。

    除去しないと ``shlex(posix=True)`` が改行を次トークンの先頭文字として吸収し、
    継続行が 0 桁目から始まるときにその先頭語が別トークンへ化ける（``git commit \\``
    + 改行 + ``--no-verify`` が素通りする）。単一クォート内では行継続は成立しない
    ので触らない。

    `_strip_line_comments` の**後**に適用すること。前に置くと ``# foo \\`` + 改行 +
    ``git commit --no-verify`` が 1 行のコメントへ畳まれ、2 行目が消える（シェルは
    コメント中の ``\\`` を行継続として扱わない）。

    Args:
        command: 検査対象のコマンド文字列。

    Returns:
        行継続を連結した文字列。

    Raises:
        例外は発生しません。
    """
    if "\\\n" not in command:
        return command

    result: list[str] = []
    quote: str | None = None
    index = 0
    length = len(command)
    while index < length:
        char = command[index]
        if char == "\\" and quote != "'" and index + 1 < length:
            following = command[index + 1]
            if following != "\n":
                result += (char, following)
            index += 2
            continue
        if quote is not None:
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        result.append(char)
        index += 1
    return "".join(result)


def _replace_unquoted_newlines(command: str) -> str:
    """クォート外の改行をシェル区切り ``;`` へ置き換える。

    改行はシェルにとって ``;`` と等価だが、``shlex`` は whitespace として消費する
    ため、複数行コマンドが 1 セグメントへ融合してセグメント境界に依存する判定が
    すべて不発になる。クォート内の改行と ``\\`` でエスケープされた改行は置き換えない。

    Args:
        command: 元のコマンド文字列。

    Returns:
        クォート外の改行を ``;`` へ置き換えた文字列。

    Raises:
        例外は発生しません。
    """
    if "\n" not in command:
        return command

    result = []
    quote = None
    escaped = False
    for char in command:
        if escaped:
            result.append(char)
            escaped = False
            continue
        if char == "\\" and quote != "'":
            result.append(char)
            escaped = True
            continue
        if quote is not None:
            if char == quote:
                quote = None
            result.append(char)
            continue
        if char in ("'", '"'):
            quote = char
            result.append(char)
            continue
        result.append(";" if char == "\n" else char)
    return "".join(result)


# Windows 読みでパス区切り ``/`` へ変換するバックスラッシュ（直後が空白と ``/`` 以外）。
_WINDOWS_SEPARATOR_BACKSLASH_RE = re.compile(r"\\(?=[^\s/])")

# Windows 読みで除去するバックスラッシュ（直後が空白）。エスケープを持たない
# PowerShell / cmd では ``rm my\\ ruff.toml`` は 2 引数で ``ruff.toml`` が実際に
# 削除されるため、検出側へ倒す。``/`` へ置換しないのは実在しない ``my/`` を作らないため。
_WINDOWS_ESCAPED_SPACE_RE = re.compile(r"\\(?=[ \t])")


def command_dialect_variants(command: str) -> tuple[str, ...]:
    """1 つのコマンド文字列を、POSIX 読みと Windows 読みの 2 方言へ展開する。

    ``shlex(posix=True)`` はクォート外の ``\\`` をエスケープとして消費するが、
    Windows の実行シェルでは ``\\`` はパス区切りである。どちらの方言で実行されるかは
    hook の入力から決められないため、両方の読み方で検査し、どちらかが引っ掛かれば
    deny する（docs/adr/shell-analysis-boundary.md）。

    Windows 読みは ``\\`` の直後の文字で分ける: パス構成文字なら ``/`` へ置換、
    空白 / タブなら ``\\`` を除去、``/`` なら変換しない（``\\/`` は sed 等の POSIX
    エスケープで、Windows のパス区切りには現れない）。

    `block_no_verify` と `bash_config_protection` が共有する。「何をパス区切りと
    みなすか」を 2 箇所に持つと、片方だけ強化される非対称が生じる。

    Args:
        command: 検査対象のコマンド文字列。

    Returns:
        検査すべき読み方のタプル。Windows 読みが元と同じなら 1 つだけ。

    Raises:
        例外は発生しません。
    """
    windows_reading = _WINDOWS_ESCAPED_SPACE_RE.sub("", _WINDOWS_SEPARATOR_BACKSLASH_RE.sub("/", command))
    if windows_reading == command:
        return (command,)
    return (command, windows_reading)


def tokenize(command: str) -> list[str]:
    """シェルコマンドを区切り記号込みのトークン列へ分割する。

    詳細は `tokenize_with_status` を参照。

    Args:
        command: 対象のシェルコマンド文字列。

    Returns:
        トークンのリスト。

    Raises:
        例外は発生しません。
    """
    return tokenize_with_status(command)[0]


def command_exceeds_scan_budget(command: str) -> bool:
    """コマンドが検査予算（バイト数・トークン数）を超えるか判定する。

    呼び出し元は True を受けたら走査せず BLOCKED を返すこと（理由は
    `MAX_COMMAND_TOKENS` / `MAX_COMMAND_BYTES` のコメント）。`tokenize` 自身が
    走査コストの本体なので、バイト数を先に見る。

    Args:
        command: 検査対象のシェルコマンド文字列。

    Returns:
        バイト数またはトークン数が上限を超えるなら True。

    Raises:
        例外は発生しません。
    """
    if len(command.encode("utf-8", "surrogatepass")) > MAX_COMMAND_BYTES:
        return True
    return len(tokenize(command)) > MAX_COMMAND_TOKENS


# `shlex` が解析できなかった入力で、空白分割の前に前後へ空白を入れる区切り演算子。
# 長い演算子を先に置いて最長一致にする。1 文字ずつ割ると ``>>ruff.toml`` が
# ``> > ruff.toml`` になり、リダイレクト先の判定がずれて新しい迂回口になる。
# ``1>`` / ``2>`` / ``1>>`` / ``2>>`` は語頭の fd 指定だけを 1 単位にする（``a2>x`` の ``a2``
# は割らない）。``>&`` / ``<&``（fd 複製）は 1 単位で、``2>&1`` の ``&`` を区切りとして読むと
# 後続の語が別セグメントへ切り離される。
_FALLBACK_OPERATOR_RE = re.compile(
    r"(&>>|(?<!\S)[12]>>|&>|(?<!\S)[12]>(?!&)|>>|>\||>&|<&|&&|\|\||[;|&()<>])"
)


def tokenize_with_status(command: str) -> tuple[list[str], bool]:
    """シェルコマンドをトークン化し、``shlex`` で解析し切れたかどうかも返す。

    ``punctuation_chars=True`` で、空白なしで連結された区切り記号
    （``git add -A&&git commit``）も独立トークンにする。コメント除去・行継続の連結・
    クォート外改行の ``;`` 化を先に行う。

    クォート不整合で ``ValueError`` になる入力（bash では有効な ``$'\\''`` を含む）は、
    区切り演算子（`_FALLBACK_OPERATOR_RE`）の前後に空白を入れてから空白分割する。
    これで ``echo $'\\'';git commit`` の ``;`` や ``>ruff.toml`` の ``>`` が語に埋もれず、
    クォート内容もフラグとして走査される。ただしクォートの対応は取らないため、本物の
    クォート内にある区切り記号まで割る。割るのは区切りの前後だけで、危険語が見えた
    こと自体では deny しない（読み取りコマンドを巻き込むため）。解析できた入力のトークン列は
    このフォールバックの影響を受けない。

    解析できたかを返すのは、生文字列への正規表現フォールバックを「解析できなかった
    ときだけ」に限定するため。解析できた入力にまで当てると ``echo "git commit"``
    のような引用文まで実行命令と誤認する。

    Args:
        command: 対象のシェルコマンド文字列。

    Returns:
        (トークンのリスト, ``shlex`` が最後まで解析できたか) のタプル。

    Raises:
        例外は発生しません。
    """
    normalized = _replace_unquoted_newlines(_strip_line_continuations(_strip_line_comments(command)))
    lexer = shlex.shlex(normalized, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    # コメントは `_strip_line_comments` が落とし済み。既定の ``#`` のままだと、
    # 改行を失った文字列で ``#`` 以降が入力末尾まで捨てられる。
    lexer.commenters = ""
    try:
        return list(lexer), True
    except ValueError:
        return _FALLBACK_OPERATOR_RE.sub(r" \1 ", normalized).split(), False


def split_segments(tokens: list[str]) -> list[list[str]]:
    """トークン列をシェル区切りごとのセグメントへ分割する。

    Args:
        tokens: `tokenize` が返したトークン列。

    Returns:
        区切りトークンを含まないセグメント（トークンリスト）のリスト。
        空セグメントは除外します。

    Raises:
        例外は発生しません。
    """
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token not in _SHELL_SEPARATORS:
            current.append(token)
            continue
        if current:
            segments.append(current)
        current = []
    if current:
        segments.append(current)
    return segments
