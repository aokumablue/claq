"""claq フック実装の共通ユーティリティ。

フック用の stdin 読み込み、シェルコマンドの解析、JSON 解析、出力書き込みの
共有関数を提供する。
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from claq.hooks.output_adapter import adapt_context_output, emit_block
from claq.lib.core_utils import get_home_dir
from claq.lib.harness import JSON_PARSE_FAILURES

MAX_STDIN_BYTES = 1024 * 1024

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


@functools.lru_cache(maxsize=1)
def resolve_repo_root() -> Path | None:
    """`git rev-parse --show-toplevel` でリポジトリルートの絶対パスを解決しキャッシュする。

    `bash_config_protection` と `pre_bash_commit_quality` が共有する。呼び出し側は
    必要なときだけ呼び、`lru_cache` で同一プロセス内の `git` 起動を最大 1 回にする。
    テストでキャッシュを跨がせない場合は `resolve_repo_root.cache_clear()` を呼ぶこと。

    Returns:
        リポジトリルートの絶対パス。タイムアウト・失敗時は None（呼び出し側が
        非ブロッキングにフォールバックする）。

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
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    top = result.stdout.strip()
    return Path(top) if result.returncode == 0 and top else None


class StdinUnavailableError(RuntimeError):
    """stdin に payload があるはずなのに読み取れなかったことを表す例外。

    「payload が無い」（tty 起動・stdin 未接続・即 EOF）とは区別する。前者で保護
    hook が許可へ倒すと、守るべきコマンドが素通りする。呼び出し側に 2 つを取り違え
    させないため、後者だけを例外にする（docs/adr/hook-failure-direction.md）。
    """


# ホストは spawn 直後に stdin へ JSON を書き込むため、最初のバイト到着まで 2 秒
# あれば十分。stdin リダイレクト漏れ（パイプ未接続のまま open）での無期限ブロックを防ぐ。
STDIN_FIRST_BYTE_TIMEOUT = 2.0

# 最初のチャンク受信後のチャンク読み取りループ全体に許す壁時計予算（秒）。
# payload は KB オーダーの JSON であり、正常系では触れない余裕として取る。
STDIN_READ_DEADLINE_SECONDS = 3.0

# 1 回の read 呼び出しで要求する最大バイト数（チャンクサイズ）。
STDIN_CHUNK_BYTES = 65536


def _stdin_is_absent() -> bool:
    """stdin がそもそも存在しない（読む対象が無い）かを判定します。

    ``sys.stdin`` が None か TTY に繋がっている場合を「payload なし」とする。
    ``isatty()`` が例外化する場合は判定できないため False を返し、実際の読み取り側
    （`_read_stdin_bytes`）に判断を委ねる。

    Returns:
        読む対象が無いと確定できれば True。

    Raises:
        例外は発生しません。
    """
    if sys.stdin is None:
        return True
    try:
        return bool(sys.stdin.isatty())
    except (OSError, ValueError, AttributeError):
        return False


def _stdin_chunk_reader(stdin_buffer: Any) -> Any:
    """ワーカースレッドが使う「n バイト読む」関数を選ぶ。

    実 fd があるときは ``os.read()`` を使う。``read1()`` は BufferedReader のロックを
    保持したままブロックするため、本スレッドがタイムアウトで先に進むと、終了時の
    stdin 後始末がロックを取れず ``Fatal Python error: _enter_buffered_busy`` で
    abort し、deny の exit code 2 が失われる。fd を持たないオブジェクト（テストの
    フェイク・io.BytesIO 等）では ``read1()`` へ落とす。

    Args:
        stdin_buffer: 読み取り対象のバイナリストリーム。

    Returns:
        ``reader(n) -> bytes`` の callable。

    Raises:
        例外は発生しません。
    """
    fileno = getattr(stdin_buffer, "fileno", None)
    if fileno is None:
        return stdin_buffer.read1
    try:
        fd = fileno()
    except (OSError, ValueError):
        return stdin_buffer.read1
    return lambda size: os.read(fd, size)


def _pump_stdin(reader: Any, max_bytes: int, sink: queue.Queue) -> None:
    """stdin をチャンク単位で読み、結果を `sink` へ流す（ワーカースレッド本体）。

    ``select.select`` は使わない。Windows では通常のパイプに ``OSError`` を投げ、
    それを「入力なし」と読むと保護 hook が許可側へ抜ける。ブロッキング read を
    ワーカースレッドへ隔離し、呼び出し側が queue のタイムアウトで待つ形にすると、
    3 プラットフォームで同一のロジックになる。各チャンクは ``n`` バイト揃うまで
    待たない読み方で読み、書き手が途中で止まってもデッドライン判定へ戻れるようにする。

    Args:
        reader: ``reader(n) -> bytes`` の読み取り関数（`_stdin_chunk_reader`）。
        max_bytes: 読み取る最大バイト数。
        sink: ``("data", bytes)`` / ``("error", Exception)`` を受け取るキュー。

    Raises:
        例外は発生しません（読み取り例外は sink へ載せます）。
    """
    collected = 0
    try:
        while collected < max_bytes:
            chunk = reader(min(STDIN_CHUNK_BYTES, max_bytes - collected))
            sink.put(("data", chunk))
            if not chunk:
                return
            collected += len(chunk)
    except (OSError, ValueError, AttributeError) as exc:
        # AttributeError は `.buffer` があるのに `.read1` を持たない stdin
        # （pytest の DontReadFromInput 等）。捕らえないとワーカー内で例外が消える。
        sink.put(("error", exc))


def _read_stdin_bytes(max_bytes: int) -> bytes:
    """stdin から最大 `max_bytes` 分をバイト列として読みます。

    ``.buffer`` があればワーカースレッド（`_pump_stdin`）へ read を隔離し、最初の
    チャンクは STDIN_FIRST_BYTE_TIMEOUT 秒、以降は STDIN_READ_DEADLINE_SECONDS 秒の
    予算で待つ。ワーカーは daemon なので、残ってもプロセス終了を妨げない。

    ``.buffer`` が無い場合（io.StringIO 等）は文字数で読んで UTF-8 へ再エンコード
    する。バイト上限を超えうるため、呼び出し側でバイト換算の切り詰めを行う。

    Args:
        max_bytes: 読み取る最大バイト数です（buffer 無し時は最大文字数）。

    Returns:
        読み取ったバイト列。1 バイト以上受け取った後にデッドライン超過・読み取り
        例外・EOF で打ち切った場合は、その時点までの部分データ（壊れた JSON として
        後段が fail-closed に倒す）。

    Raises:
        StdinUnavailableError: 1 バイトも受け取れないまま、読み取りが例外化
            したか最初のバイトが時間内に届かなかった場合。
    """
    stdin_buffer = getattr(sys.stdin, "buffer", None)
    if stdin_buffer is None:
        try:
            return sys.stdin.read(max_bytes).encode("utf-8", errors="replace")
        except (OSError, ValueError, AttributeError) as exc:
            raise StdinUnavailableError(f"stdin の読み取りに失敗しました: {exc}") from exc

    sink: queue.Queue = queue.Queue()
    threading.Thread(
        target=_pump_stdin, args=(_stdin_chunk_reader(stdin_buffer), max_bytes, sink), daemon=True
    ).start()

    collected = b""
    deadline: float | None = None
    while len(collected) < max_bytes:
        timeout = STDIN_FIRST_BYTE_TIMEOUT if deadline is None else max(0.0, deadline - time.monotonic())
        try:
            kind, payload = sink.get(timeout=timeout)
        except queue.Empty:
            if not collected:
                raise StdinUnavailableError(
                    f"stdin の最初のバイトが {STDIN_FIRST_BYTE_TIMEOUT} 秒以内に届きませんでした"
                ) from None
            write_stderr(
                "WARNING: stdin 読み取りが上限時間に達したため、"
                "受信済みの部分データで打ち切ります（stdin の書き手が"
                "応答しない可能性）\n"
            )
            break
        if kind == "error":
            if not collected:
                raise StdinUnavailableError(f"stdin の読み取りに失敗しました: {payload}")
            break
        if payload == b"":
            break
        collected += payload
        if deadline is None:
            deadline = time.monotonic() + STDIN_READ_DEADLINE_SECONDS
    return collected


def read_raw_stdin(max_bytes: int = MAX_STDIN_BYTES) -> str:
    """標準入力から生のテキストをバイト単位の上限つきで読み取ります。

    読み取れなかった場合も空文字列を返す。非保護経路（launcher の ``--bg`` 中継・
    ``mem.cli`` の payload 読み取り）専用で、これらは「入力が無い」と「読めない」を
    区別しても採れる別の行動が無い。保護 hook は `read_raw_stdin_with_truncation` を
    使い、`StdinUnavailableError` を捕捉して fail-closed に倒すこと。

    Args:
        max_bytes: 読み取る最大バイト数です。

    Returns:
        読み取られた文字列（max_bytes バイトで切り捨て済み）。読む対象が
        無い場合・読み取れなかった場合は空文字列。

    Raises:
        例外は発生しません。
    """
    if _stdin_is_absent():
        return ""
    try:
        raw_bytes = _read_stdin_bytes(max_bytes)
    except StdinUnavailableError:
        return ""
    return raw_bytes[:max_bytes].decode("utf-8", errors="replace")


def read_raw_stdin_with_truncation(max_bytes: int = MAX_STDIN_BYTES) -> tuple[str, bool]:
    """標準入力を読み取り、切り捨ての有無を返します（保護 hook 用）。

    読む対象が無い場合（tty 起動・``sys.stdin`` が None・即 EOF）は ``("", False)``。
    呼び出し側の保護 hook は `StdinUnavailableError` を捕捉して deny に倒すこと
    （docs/adr/hook-failure-direction.md）。

    Args:
        max_bytes: 読み取る最大バイト数です。

    Returns:
        読み取った文字列と、切り捨てが発生したかどうかのタプル。

    Raises:
        StdinUnavailableError: payload があるはずなのに読み取れなかった場合。
    """
    if _stdin_is_absent():
        return "", False
    raw_bytes = _read_stdin_bytes(max_bytes + 1)
    return raw_bytes[:max_bytes].decode("utf-8", errors="replace"), len(raw_bytes) > max_bytes


def stdin_unreadable_message(hook_name: str, reason: object) -> str:
    """stdin 読み取り不能時の deny 理由を組み立てます。

    4 つの保護 hook が同じ文面と方針を使うための単一情報源です。

    Args:
        hook_name: 呼び出し元 hook の識別名。
        reason: 読み取りに失敗した理由（`StdinUnavailableError`）。

    Returns:
        deny 理由の文字列。

    Raises:
        例外は発生しません。
    """
    return (
        f"[Hook] BLOCKED: {hook_name} could not read its stdin payload ({reason}). "
        "The tool call cannot be verified, so it is refused rather than allowed "
        "through unchecked."
    )


def parse_json_object(raw: str) -> dict[str, Any] | None:
    """JSON 文字列を辞書としてパースします。

    パース不能は ``JSON_PARSE_FAILURES`` で一括して受ける。``json.JSONDecodeError``
    だけだと、深いネストで送出される ``RecursionError`` が貫通して保護フックが
    exit 1（PreToolUse では non-blocking error ＝ ツールが実行される）へ倒れる。
    None は呼び出し元で「判定不能 → deny」として扱われる fail-closed 側の値。

    Args:
        raw: パース対象の JSON 文字列です。

    Returns:
        パースされた辞書、または失敗時は None を返します。

    Raises:
        例外は発生せず、パースエラー時は None を返します。
    """
    if not raw or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except JSON_PARSE_FAILURES:
        return None
    return data if isinstance(data, dict) else None


def write_stdout(text: str) -> None:
    """標準出力にテキストを書き出します。

    Args:
        text: 出力するテキストです。

    Raises:
        OSError: パイプが閉じている場合（BrokenPipeError を含む）。
        UnicodeEncodeError: 出力先のエンコーディングで表現できない場合。
    """
    sys.stdout.write(text)


def write_stderr(text: str) -> None:
    """標準エラーにテキストを書き出します。

    Args:
        text: 出力するテキストです。

    Raises:
        OSError: パイプが閉じている場合（BrokenPipeError を含む）。
        UnicodeEncodeError: 出力先のエンコーディングで表現できない場合。
    """
    sys.stderr.write(text)


def is_truthy(value: str | None) -> bool:
    """文字列が真値を表すかどうかを判定します。

    Args:
        value: 判定対象の文字列です。

    Returns:
        '1', 'true', 'yes', 'on' の場合は True を返します。

    Raises:
        例外は発生しません。
    """
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def basename(path: str) -> str:
    """パスからファイル名を取得します。

    Args:
        path: ファイルパスです。

    Returns:
        ファイル名を返します。

    Raises:
        例外は発生しません。
    """
    return Path(path).name


def _basename_any_separator(path: str) -> str:
    """`/` と `\\` の**両方**を区切りとみなして basename を取り出します。

    `basename` は POSIX ホストでは `\\` を区切りとみなさない。クォートで守られた
    Windows 絶対パス（``"C:\\bin\\rm.exe" ruff.toml``）は方言展開でも `\\` を保った
    まま 1 トークンで残るため、ここで両方の区切りを扱う。

    Args:
        path: パスまたは実行トークンの文字列です。

    Returns:
        両方の区切りで切り出した末尾要素を返します。

    Raises:
        例外は発生しません。
    """
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def normalize_executable_name(token: str) -> str:
    """実行トークンを比較用の名前（basename 化・大小無視・`.exe` 除去）へ正規化します。

    `/usr/bin/git`・`git.exe`・`C:\\Program Files\\Git\\bin\\git.exe`・`GIT` を
    いずれも `git` へ畳む。大小を無視するのは、APFS / NTFS が既定で大小を区別せず、
    PowerShell が cmdlet 名を大小無視で解決するため（Linux での誤検出は
    docs/adr/shell-analysis-boundary.md の範囲内）。保護 hook の実行名照合は
    この 1 関数だけを通し、兄弟フック間で正規化がずれないようにする。

    Args:
        token: `shlex` 等でトークン化された 1 トークンです。

    Returns:
        basename を大小無視へ畳み、末尾の `.exe` を除いた名前を返します。

    Raises:
        例外は発生しません。
    """
    name = _basename_any_separator(token).casefold()
    return name[: -len(".exe")] if name.endswith(".exe") else name


def normalize_protected_name(path: str) -> str:
    """保護対象ファイル名の照合用に basename を大小無視・Win32 正準化へ畳みます。

    `normalize_executable_name` と同じ case 演算（`casefold`）と basename 規則を使う。
    `Path.resolve()` は APFS / NTFS で綴りを実体の大小へ正規化しないため、
    ``Write Ruff.toml`` が実体 ``ruff.toml`` へ着地しても素の比較では外れる。
    `.exe` は落とさない（保護対象は設定ファイルで、同一視する根拠が無い）。

    Windows は末尾のドットと空白を切り捨て、``:`` 以降を代替データストリーム名と
    して扱うため、``ruff.toml.`` / ``ruff.toml`` +空白 / ``ruff.toml:x`` も
    ``ruff.toml`` へ畳む。

    Args:
        path: 検査対象のパス文字列、またはファイル名です。

    Returns:
        大小無視・Win32 正準化へ畳んだ basename を返します。

    Raises:
        例外は発生しません。
    """
    name = _basename_any_separator(path).split(":", 1)[0]
    return name.rstrip(". \t").casefold()


def is_git_executable_token(token: str) -> bool:
    """トークンが git 実行ファイルを指すかを判定します。

    Args:
        token: `shlex` 等でトークン化された1トークンです。

    Returns:
        git 実行ファイルとみなせるなら True。

    Raises:
        例外は発生しません。
    """
    return normalize_executable_name(token) == "git"


# **フラグの有無に関わらず**引数のファイルを書き換えるエディタ／フィルタ。
# `ed` に in-place フラグは無く、``ed FILE`` は stdin の編集コマンドでその場で書き戻す
# ため、`is_inplace_edit_flag` の「フラグが立っていれば書き込み」という軸では表せない。
# 基準は「フラグ 1 つ無しでも名前を挙げたファイルを書き換えうるか」:
#     - ``ed`` / ``red`` / ``ex``: POSIX の行エディタで、``w`` で引数のファイルへ書き戻す。
#     - ``sponge``（moreutils）: stdin を吸ってから引数のファイルへ書く。
#     `vi` / `vim` / `nvim` は無フラグでは書き込まず（`-c wq` が要る）、含めない。
# `bash_config_protection` と `pre_bash_commit_quality` が共有する。判定軸が兄弟ごとに
# 割れると、片方だけずれた語彙を持って静かなバイパスになる。
ALWAYS_MUTATING_EDIT_EXECUTABLES = frozenset({"ed", "red", "ex", "sponge"})

# sed の GNU 長形式 in-place フラグ。GNU getopt は曖昧でない長形式の短縮を受け付け、
# ``sed --i`` も in-place になるため、この名前の非空プレフィックスを一致条件にする。
_LONG_INPLACE_OPTION_NAME = "in-place"


def is_inplace_edit_flag(token: str) -> bool:
    """トークンが in-place 編集フラグ（`sed -i` / `perl -0pi` / `sed --in-place`）かを判定します。

    単一ダッシュの短形式は結合位置を問わず `i` を含むかで判定する（`-i` / `-i.bak` /
    `-0pi` / `-ni`）。長形式は `_LONG_INPLACE_OPTION_NAME` の非空プレフィックスに
    限る（`--in-place=.bak` は名前部分だけを見る）。区切りの `--` は名前部分が空に
    なるため in-place とみなさない。

    `bash_config_protection` と `pre_bash_commit_quality` が共有する。どちらかが
    長形式を除外すると ``sed --in-place ... && git commit -am x`` でガードが不発になる。

    Args:
        token: 引数トークン列の 1 トークンです。

    Returns:
        in-place 編集を指示するフラグなら True。

    Raises:
        例外は発生しません。
    """
    if not token.startswith("-") or token == "-":
        return False
    if token.startswith("--"):
        name = token[2:].split("=", 1)[0]
        return bool(name) and _LONG_INPLACE_OPTION_NAME.startswith(name)
    return "i" in token[1:]


def _expand_leading_tilde(raw_path: str) -> str:
    """先頭の ``~`` をホームディレクトリへ展開する（シェルのチルダ展開に相当）。

    展開しないと ``~`` がカレントディレクトリ配下のフォルダ名として扱われる。
    展開先は `get_home_dir()` に揃える。``~otheruser`` 形式は標準ライブラリに対応
    API が無く、うっかりバイパスの抑止という対象範囲の外なので展開しない。

    Args:
        raw_path: 検査対象の生パス文字列。

    Returns:
        先頭が ``~`` または ``~/`` ならホームディレクトリへ展開した文字列。
        それ以外はそのまま返す。

    Raises:
        OSError: 先頭が ``~`` で、ホームディレクトリを決定できないとき（``get_home_dir`` 由来）。
    """
    if raw_path == "~" or raw_path.startswith("~/"):
        return str(get_home_dir()) + raw_path[1:]
    return raw_path


def resolve_effective_target(raw_path: str) -> Path | None:
    """cwd 基準で解決し、symlink を辿った実体 path を返します。

    basename だけで保護対象を判定すると `alias -> pyproject.toml` のような symlink
    経由の書き込みがすり抜けるため、`config_protection` / `bash_config_protection`
    はこの関数で実体 path を得てから判定する。先頭の ``~`` は展開してから解決する。

    Args:
        raw_path: 検査対象の生パス文字列（相対 / 絶対 / symlink いずれも可）。

    Returns:
        解決できた実体 Path。解決できない場合は None（fail-closed / fail-open の
        どちらに倒すかは呼び出し側が決める）。

    Raises:
        例外は発生しません。
    """
    try:
        return (Path.cwd() / _expand_leading_tilde(raw_path)).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        # ValueError は単独サロゲート（JSON の \ud800）の ``UnicodeEncodeError`` と
        # NUL 混入。入力側が任意に作れるため、貫通させると呼び出し側の保護フックが
        # exit 1（＝ PreToolUse では non-blocking でツール実行）で落ち、後続トークンの
        # 保護対象書き込みの検査ごと失われる。
        return None


def emit_block_output(reason: str) -> int:
    """ツール実行ブロックを host 非依存の合併出力で stdout/stderr に書き出す。

    ツール実行をブロックするフックは、このヘルパの戻り値（常に 2）を終了コードとして
    返すこと。出力層の例外（host がパイプを閉じたときの BrokenPipeError 等）が抜けると
    exit 1 になり、PreToolUse では non-blocking error ＝ ツールが実行されて deny が
    allow へ反転する。出力の唯一の口であるここで握り潰し、判定は exit code で運ぶ
    （docs/adr/hook-failure-direction.md）。

    Args:
        reason: ブロック理由（ユーザー / エージェントに提示される）。

    Returns:
        フックが返すべき終了コード。

    Raises:
        例外は発生しません。
    """
    exit_code, deny_out, reason_err = emit_block(reason)
    with contextlib.suppress(Exception):
        write_stdout(deny_out)
        write_stderr(reason_err + "\n")
    return exit_code


def emit_session_start_output(additional_context: str = "") -> str:
    """SessionStart 用のフック出力 JSON 文字列を返す（stdout へは書かない）。

    Args:
        additional_context: コンテキストに注入する追加文字列。

    Returns:
        additionalContext（トップレベル）と hookSpecificOutput を同時に
        含む合併 JSON 文字列。

    Raises:
        例外は発生しません。
    """
    return adapt_context_output("SessionStart", additional_context)


def print_session_start_output(additional_context: str = "") -> None:
    """SessionStart 用のフック出力を stdout に書き出す。

    Args:
        additional_context: コンテキストに注入する追加文字列。

    Raises:
        例外は発生しません。
    """
    print(emit_session_start_output(additional_context))
