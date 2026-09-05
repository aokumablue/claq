"""ple4フック実装の共通ユーティリティ。

フック用の入力読み込み、JSON解析、
出力書き込みの共有関数を提供します。
"""

from __future__ import annotations

import functools
import json
import os
import queue
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ple4.hooks.output_adapter import adapt_context_output, emit_block
from ple4.lib.core_utils import ensure_private_dir, get_ple4_dir

MAX_STDIN_BYTES = 1024 * 1024

# コマンド全体をセグメントに割るシェル区切りトークン。`block_no_verify` と
# `pre_bash_commit_quality` が共に shell 区切り文字密着トークン（例:
# ``status;echo``）を誤って 1 トークンとして扱わないよう、この定数と
# `tokenize`/`split_segments` を共有ヘルパとして 1 箇所に持つ（A-01 対応）。
_SHELL_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "(", ")"})


# ``sh -c`` 再帰と heredoc 本文判定で共有する既知シェル実行ファイル（basename 判定）。
# heredoc 側だけ別集合を持つと「本文が実行されるか」の判定軸が 2 つに割れるため、
# block_no_verify から本モジュールへ移して単一情報源にする。
SHELL_WRAPPER_EXECUTABLES = frozenset({"sh", "bash", "zsh", "dash"})

# 実行ファイルではないが、渡されたテキストを現在のシェルで実行するビルトイン。
# ``. /dev/stdin <<'EOF'`` / ``source /dev/stdin <<'EOF'`` / ``eval "$(cat <<'EOF'``
# はいずれも heredoc 本文をコマンドとして実行するため、本文をデータとして
# 剥がすと ADR-0017 が「除去しない条件 1」で禁じている誤通過になる（実測:
# 本文に `git commit --no-verify` を置くと exit 0 だった）。判定軸は
# 「本文が実行されうるか」であって「実行ファイルか」ではないので、
# `SHELL_WRAPPER_EXECUTABLES` とは別集合として持ち、両方を見る。
_BODY_EXECUTING_BUILTINS = frozenset({".", "source", "eval"})

# ``#`` を行コメントの開始として扱ってよい直前の文字。POSIX シェルは語の先頭に
# ある ``#`` だけをコメント開始とみなす（``echo a#b`` の ``#`` は語の一部）。
_COMMENT_PRECEDING_CHARS = frozenset(" \t\n;|&()<>")

# ``-c`` を含む結合短フラグ（``bash -lc 'cmd'``）。単独の ``-c`` だけを見ていた
# 頃は ``bash -lc`` でラッパー再帰が不発になり、保護フックが素通りしていた（実測）。
# 長オプション（``--``）は該当しないので先頭 1 文字のハイフンに限定する。
_SHELL_COMMAND_FLAG_RE = re.compile(r"-[A-Za-z]*c[A-Za-z]*")

# heredoc 演算子。``<<<``（herestring）を誤って heredoc と読まないよう、
# 前後に ``<`` が無いことを lookbehind / lookahead の両方で要求する
# （lookahead だけだと ``<<<EOF`` が offset 1 で再マッチする）。
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

    ``sh -c 'git commit --no-verify'`` のように basename が
    `SHELL_WRAPPER_EXECUTABLES` のいずれかであるトークンを探し、続くトークンに
    ``-c`` を含む短フラグがあれば、その次のトークン（シェルへ渡す文字列コマンド）
    を返す。``-c`` 単独だけを見ていた頃は ``bash -lc 'git commit --no-verify'``
    で再帰が不発になり、素通りしていた（実測）。

    `block_no_verify` と `bash_config_protection` が共有する。片方だけがラッパー
    再帰を持つと「Write なら止まるが Bash なら通る」と同型の非対称（A-06 で
    本モジュール群を分けた理由そのもの）が再発するため、単一情報源にする。
    再帰の段数は呼び出し側が決める（ADR-0002: 2 段以上のネストは非目標）。

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
        for offset, candidate in enumerate(segment[index + 1 :]):
            if not _SHELL_COMMAND_FLAG_RE.fullmatch(candidate):
                continue
            remaining = segment[index + 1 + offset + 1 :]
            return remaining[0] if remaining else None
    return None


def _operator_start_offsets(line: str, quote: str | None) -> tuple[list[int], str | None]:
    """heredoc 演算子になりうる ``<`` の位置と、行末のクォート状態を返す。

    コメント内・クォート内・``\\`` エスケープ後の ``<`` は heredoc を開始しない。
    これらを演算子として採ると、実際には**実行される**後続行が「本文」として
    捨てられる（実測: ``# <<EOF`` 改行 ``git commit --no-verify`` 改行 ``EOF`` で
    保護フック 3 種が揃って exit 0 になった）。ADR-0017 が本文剥がしを正当化した
    前提「本文範囲は演算子・区切り語・終端行だけで決まる」は、そもそも heredoc が
    開始しないこの形では成立しない。

    クォート状態は行をまたぐため（シェルのクォートは改行を含む）、呼び出し元が
    **非本文行だけ**を鎖のように繋いで渡す。heredoc 本文行・終端行はシェルの
    語彙ではないので状態を更新してはならない — 本文中の ``#`` や閉じない
    アポストロフィ（``don't``）で以降の解析が壊れる。

    ``\\`` エスケープは行内で完結させる。行末の ``\\`` が消費するのは改行自体
    （行継続）であり、次行の先頭文字ではないため、状態として持ち越さない。

    コメント判定は `_strip_line_comments` と同じ POSIX 規則（語の先頭にある
    ``#`` だけ）で、`_COMMENT_PRECEDING_CHARS` を共有する。前段で
    `_strip_line_comments` を単純適用する形は採れない — heredoc 本文中の ``#``
    や閉じないアポストロフィまで巻き込んで壊すため。

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

    照合は `_operator_start_offsets` が返した位置からのみ行う。生の行へ
    `finditer` を当てていた頃は、コメント内・クォート内の ``<<`` を演算子と
    誤認していた。位置を絞ってから `re.Pattern.match` するため lookbehind
    （``(?<!<)``）は従来どおり手前の文字を見られる。

    採用した照合は `finditer` と同じく非重複にする（`consumed_to` で
    直前の照合範囲内の候補を飛ばす）。位置ごとに独立して照合すると、
    クォート内の ``<<`` を跨いだ重複照合が新たな区切り語を生み、本文を
    余計に剥がす＝誤通過側へ倒れうるため。

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
        word = next(
            value for value in (match.group("squote"), match.group("dquote"), match.group("bare")) if value is not None
        )
        delimiters.append((word, match.group(1) == "-"))
        consumed_to = match.end()
    return delimiters, next_quote


def _line_keeps_heredoc_bodies(line: str) -> bool:
    """演算子行を見て、本文を剥がさずに残すべきかを判定する。

    本文が実行されうる形（シェル起動・本文を実行するビルトイン・パイプや
    継続で次行へ繋がる形）はすべて残す側へ倒す。``bash <<'EOF'`` や
    ``. /dev/stdin <<'EOF'`` の本文は実際に実行されるため、データとして
    剥がすと ADR-0002 が禁じる誤通過になる（実測で確認済み）。

    Args:
        line: heredoc 演算子を含む物理行。

    Returns:
        本文を残すなら True、剥がしてよいなら False。

    Raises:
        例外は発生しません。
    """
    if line.rstrip().endswith(_HEREDOC_CONTINUATION_SUFFIXES):
        return True
    return any(
        is_shell_wrapper_token(token) or token in _BODY_EXECUTING_BUILTINS for token in tokenize(line)
    )


def _consume_heredoc_bodies(
    lines: list[str], start: int, delimiters: list[tuple[str, bool]]
) -> tuple[int, list[str]] | None:
    """区切り語の本文を読み飛ばし、終端行だけを残して返す。

    本文の走査中に ``<<`` を再検出しない。``<<'OUTER'`` の本文に
    ``<<'INNER'`` が現れる形で走査が同期ずれを起こすのを防ぐ。

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

    heredoc 本文はどのシェルでもコマンドの語彙に入らないため、そのまま
    トークン化すると散文が実行命令として読まれる（実測: ``cat > note.md
    <<'EOF'`` の本文に ``git commit --no-verify`` と書いただけで 3 つの保護
    フックが exit 2 になった）。ADR-0002 の「誤検出 > 誤通過」は解析できない
    構文についての規定であり、heredoc の本文範囲は演算子・区切り語・終端行
    だけで決まる環境非依存の構文なので、この規定は本 FP を正当化しない。

    ただし本文がデータだと**静的に確定できる場合だけ**剥がす。次のいずれかに
    当たれば剥がさず現状の挙動（＝検出側）を維持する:

    1. 演算子行に本文を実行するトークンがある（``bash <<'EOF'`` /
       ``cat <<'EOF' | bash`` / ``. /dev/stdin <<'EOF'`` / ``eval "$(cat <<'EOF'``）
    2. 演算子行が継続演算子で終わる（本文開始が次行とは限らない）
    3. 終端行が見つからない（未終端）
    4. ``<<`` がそもそも heredoc 演算子ではない（コメント内・クォート内・
       ``\\`` エスケープ後）。この場合 heredoc は開始せず、後続行は**実行される
       コマンド**なので、本文として剥がすと保護フックが素通りする（実測:
       ``# <<EOF`` 改行 ``git commit --no-verify`` 改行 ``EOF`` で 3 フックが
       揃って exit 0）。ADR-0017 の前提「本文範囲は演算子・区切り語・終端行だけ
       で決まる」はここでは成立しない。判定は `_operator_start_offsets` が持つ
       クォート／コメント状態で行い、状態は**非本文行だけ**を鎖にして更新する
       （本文行・終端行はシェルの語彙ではないため）。

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

    ``shlex`` にコメント処理を任せると、``#`` 以降が**入力末尾まで**捨てられる。
    `_replace_unquoted_newlines` が改行を ``;`` へ正規化した後の文字列を渡す
    ため、``shlex`` が探すコメント終端（改行）が 1 つも残っていないからである。
    その結果 ``git status #`` 改行 ``git commit --no-verify`` の 2 行目が丸ごと
    未検査になり、Bash 系フックが揃って exit 0 になっていた（実測）。
    ``echo a#b && git commit --no-verify`` も同様に素通りしていた（``shlex`` は
    語中の ``#`` もコメント開始として扱うため）。

    そこで正規化の前段でコメントを自分で落とし、``shlex`` 側のコメント処理は
    無効化する（`tokenize_with_status`）。POSIX シェルに合わせて、語の先頭に
    ある ``#`` だけをコメント開始とみなす（``echo a#b`` の ``#`` は語の一部）。
    クォート内・``\\`` エスケープ後の ``#`` はコメントにしない。

    クォートが閉じていない入力では「クォート内」と判定したまま末尾に達するため
    コメントは剥がされず、検査対象として残る（fail-closed 側）。

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


def _replace_unquoted_newlines(command: str) -> str:
    """クォート外の改行をシェル区切り ``;`` へ置き換える。

    改行はシェルにとって ``;`` と等価なコマンド区切りだが、``shlex`` は
    whitespace として消費するため区切りトークンを出さない。その結果
    ``split_segments`` が複数行コマンドを 1 セグメントへ融合し、セグメント
    境界に依存する判定がすべて不発になる（実測: ``printf x > s.py`` 改行
    ``git add s.py`` 改行 ``git commit -m x`` が exit 0。``;`` 区切りの同内容は
    exit 2。``bash_config_protection`` も 2 行目以降の ``tee`` / ``sed -i`` /
    ``cp`` を実行位置と認識できていなかった）。

    クォート内の改行は置き換えない（``echo 'a`` 改行 ``b'`` は 1 トークンの
    まま）。``\\`` でエスケープされた改行は行継続なので区切りにしない。

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


# Windows 読みでパス区切りへ変換するバックスラッシュ。直後が空白と ``/`` の
# ものは除く（下 2 つの正規表現がそれぞれ別扱いする）。
_WINDOWS_SEPARATOR_BACKSLASH_RE = re.compile(r"\\(?=[^\s/])")

# Windows 読みで**除去**するバックスラッシュ（直後が空白）。POSIX では
# ``rm my\\ ruff.toml`` は 1 トークン ``my ruff.toml`` だが、エスケープを持たない
# PowerShell / cmd では ``my\\`` と ``ruff.toml`` の 2 引数であり、``ruff.toml`` が
# 実際に削除される。``git commit -m fix\\ --no-verify`` も同様に ``--no-verify``
# が独立した引数になり、フックがバイパスされる。したがってこの形は
# 「POSIX の誤検出」ではなく「Windows の真陽性」であり、検出side へ倒す
# （ADR-0002: 誤検出 > 誤通過）。``/`` へ置換するのではなく除去するのは、
# 実在しない ``my/`` のようなパスを作らないため。
_WINDOWS_ESCAPED_SPACE_RE = re.compile(r"\\(?=[ \t])")


def command_dialect_variants(command: str) -> tuple[str, ...]:
    """1 つのコマンド文字列を、2 つのシェル方言の読み方へ展開する。

    ``tokenize`` は ``shlex(posix=True)`` を使うため、クォート外の ``\\`` を
    「次の 1 文字をエスケープする記号」として消費する。POSIX シェルでは
    正しいが、Windows の実行シェル（PowerShell / cmd）では ``\\`` は
    ただのパス区切りである。その結果 ``rm .\\ruff.toml`` は
    ``['rm', '.ruff.toml']`` に、``C:\\Git\\git.exe commit --no-verify`` は
    ``['C:Gitgit.exe', ...]`` になり、保護 hook が basename も git 起動も
    見失う（実測。release-verify 2026-09-03 の再レビュー）。

    どちらの方言で解釈されるかは実行前には決められない（hook の入力に
    シェル種別は載らない）。そこで**両方の読み方で検査し、どちらかが
    引っ掛かれば deny する**。ADR-0002 は保護 hook の検出境界を
    「誤検出を誤通過より選ぶ」と定めており、この非対称はその規定の
    範囲内である。

    Windows 読みの作り方は、``\\`` の直後の文字で 3 通りに分ける:

    - **パス構成文字** → ``/`` へ置換（``.\\ruff.toml`` → ``./ruff.toml``）。
    - **空白 / タブ** → ``\\`` を除去（``my\\ ruff.toml`` → ``my ruff.toml``）。
      PowerShell / cmd に単語結合のエスケープは無いので、この形は Windows では
      2 引数であり ``ruff.toml`` が実際に書き込み対象になる。POSIX 読みでは
      1 トークンの非保護ファイル名なので両立しないが、ADR-0002 に従い検出側へ
      倒す。``/`` へ置換せず除去するのは、実在しない ``my/`` を作らないため。
    - **``/``** → 変換しない。``\\/`` は sed スクリプト等の POSIX エスケープで、
      Windows のパス区切りには現れない。POSIX 読みでも Windows 読みでも ``/`` は
      区切りのまま残るので、除外しても検出力は落ちない
      （``sed -i 's/\\.git\\/hooks\\/pre-commit//' notes.md`` の誤検出はこれで消える）。

    残る誤検出はある（例: ``rm a\\.eslintrc``）。これは ADR-0002 が受容する側の
    誤りであり、`tests/hooks/test_windows_shell_dialect.py` に characterization
    test として固定してある。

    `block_no_verify` と `bash_config_protection` が共有する。「何をパス区切りと
    みなすか」を 2 箇所へ別々に実装すると、片方だけ強化される非対称
    （``INPUT_CONTAINER_KEYS`` で実際に起きた失敗）を再生産するため。

    Args:
        command: 検査対象のコマンド文字列。

    Returns:
        検査すべき読み方のタプル。Windows 読みが元と同じなら 1 つだけ。

    Raises:
        例外は発生しません。
    """
    windows_reading = _WINDOWS_ESCAPED_SPACE_RE.sub(
        "", _WINDOWS_SEPARATOR_BACKSLASH_RE.sub("/", command)
    )
    if windows_reading == command:
        return (command,)
    return (command, windows_reading)


def tokenize(command: str) -> list[str]:
    """シェルコマンドを区切り記号込みのトークン列へ分割する。

    ``shlex`` を ``punctuation_chars=True`` で使い、``git add -A&&git commit``
    のように空白なしで連結された区切り記号も独立トークンにします。クォート外の
    改行は ``;`` へ正規化してから渡します（``_replace_unquoted_newlines``。
    ``shlex`` は改行を whitespace として消費し区切りトークンを出さないため、
    複数行コマンドが 1 セグメントへ融合していました）。クォート
    不整合で ``ValueError`` になる入力は空白分割へフォールバックします
    （クォートが閉じていない入力ではクォート内容もフラグとして走査され、
    ブロック側＝fail-closed に倒れます）。

    Args:
        command: 対象のシェルコマンド文字列。

    Returns:
        トークンのリスト。

    Raises:
        例外は発生しません。
    """
    tokens, _parsed_cleanly = tokenize_with_status(command)
    return tokens


def tokenize_with_status(command: str) -> tuple[list[str], bool]:
    """`tokenize` に加えて、``shlex`` で解析し切れたかどうかを返す。

    フォールバックしたかどうかを呼び出し側が知る必要があるのは、生文字列への
    正規表現フォールバックを「解析できなかったときだけ」に限定するため。
    解析できた入力にまで生文字列の正規表現を当てると、``echo "git commit"`` の
    ような引用文まで実行命令と誤認する（F-08）。

    Args:
        command: 対象のシェルコマンド文字列。

    Returns:
        (トークンのリスト, ``shlex`` が最後まで解析できたか) のタプル。

    Raises:
        例外は発生しません。
    """
    normalized = _replace_unquoted_newlines(_strip_line_comments(command))
    lexer = shlex.shlex(normalized, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    # コメントは `_strip_line_comments` が行単位で落とし済み。ここを既定
    # （``#``）のままにすると、正規化で改行を失った文字列に対して ``#`` 以降が
    # 入力末尾まで捨てられ、2 行目以降が未検査になる。
    lexer.commenters = ""
    try:
        return list(lexer), True
    except ValueError:
        return normalized.split(), False


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
        if token in _SHELL_SEPARATORS:
            if current:
                segments.append(current)
            current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


@functools.lru_cache(maxsize=1)
def resolve_repo_root() -> Path | None:
    """`git rev-parse --show-toplevel` でリポジトリルートの絶対パスを解決しキャッシュする。

    `bash_config_protection`（A-06: 保護対象パスがリポジトリ配下かの判定）と
    `pre_bash_commit_quality`（`git commit -a` の未ステージ変更を作業ツリーから
    読むため）が共有する。呼び出し側は「保護対象 basename のヒットがあった
    場合にのみ」呼ぶことを前提にしており（`git ls-files` 等が出現しない
    大多数の Bash 呼び出しでは subprocess を起動しない）、`lru_cache` で
    同一プロセス内では最大 1 回だけ実際に `git` を実行する（セグメントや
    呼び出し箇所ごとに再起動しない）。テストでキャッシュを跨がせない場合は
    `resolve_repo_root.cache_clear()` を呼ぶこと。

    タイムアウト・失敗時は None を返し、呼び出し側で非ブロッキングに
    フォールバック（deny を避ける、または INDEX のみ検査に留める）
    できるようにする。

    Args:
        引数はありません。

    Returns:
        リポジトリルートの絶対パス。解決できなければ None を返します。

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


class StdinUnavailableError(RuntimeError):
    """stdin に payload があるはずなのに読み取れなかったことを表す例外。

    「payload が無い」（tty 起動・stdin 未接続・即 EOF）とは区別する。前者は
    ホストがそもそも入力を渡していない状態で、保護 hook が検査すべき対象自体
    が存在しない。後者は「入力はあるはずなのに取り出せなかった」状態であり、
    保護 hook が判定不能のまま許可へ倒すと、まさに守るべきコマンドが素通り
    する（実測: Windows で ``select.select`` が ``OSError`` になり、
    ``git commit --no-verify`` と ``git status`` が揃って exit 0 になった。
    release-verify 2026-09-03 の P1-004）。呼び出し側にこの 2 つを取り違え
    させないため、後者だけを例外として区別する（詳細は
    ``docs/adr/0019-*.md``）。
    """


# hooks は Claude Code が spawn 直後に stdin へ JSON を書き込むため、
# 最初のバイト到着まで 2 秒あれば十分な余裕がある。
# stdin リダイレクト漏れ（パイプ未接続のまま open）での無期限ブロックを防ぐ。
# launcher がインプロセス実行になったことで、この guard は各フックが
# 自分で stdin を読む read_raw_stdin* の先頭に置く（旧: launcher._read_stdin）。
STDIN_FIRST_BYTE_TIMEOUT = 2.0

# _read_stdin_bytes のチャンク読み取りループ全体に許す壁時計予算（秒）。
# 最初のバイト到着待ち（STDIN_FIRST_BYTE_TIMEOUT）とは別予算で、最初の
# チャンクを受け取った時点から計測する。hooks の stdin ペイロードは
# Claude Code から渡される KB オーダーの JSON であり、3 秒は正常系では絶対に
# 触れない余裕であって、正常系を制約する値ではない。
STDIN_READ_DEADLINE_SECONDS = 3.0

# 1 回の read 呼び出しで要求する最大バイト数（チャンクサイズ）。
STDIN_CHUNK_BYTES = 65536


def _stdin_is_absent() -> bool:
    """stdin がそもそも存在しない（読む対象が無い）かを判定します。

    ``sys.stdin`` が None（detach された子プロセス等）か、TTY に繋がって
    いる（人手による直接起動）場合を「payload なし」として扱います。
    ``isatty()`` が例外化する場合は「判定できない」ため False を返し、
    実際の読み取り側（`_read_stdin_bytes`）に判断を委ねます。

    Args:
        なし

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

    実 fd があるときは ``BufferedReader.read1()`` ではなく ``os.read()`` を
    使う。``read1()`` は BufferedReader のロックを保持したままブロックする
    ため、書き手が止まったまま本スレッドがタイムアウトで先に進むと、
    インタプリタ終了時の stdin 後始末がそのロックを取れず
    ``Fatal Python error: _enter_buffered_busy`` でプロセスが abort する
    （実測: 開いたまま何も書かれないパイプを渡すと、deny 出力の直後に abort し
    exit code が 2 ではなくなった）。``os.read()`` は Python レベルのロックを
    握らないので、ブロックしたワーカーが残っても終了処理を妨げない。

    fd を持たないオブジェクト（テストのフェイク・io.BytesIO 等）では
    ``read1()`` へ落とす。

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

    ``select.select`` は使いません。Windows では select が socket にしか
    使えず、通常のパイプに対して ``OSError`` を投げます。その例外を
    「入力なし」と読み替えていたため、保護 hook が payload を 1 バイトも
    読まずに許可側へ抜けていました（P1-004）。ブロッキング read を
    ワーカースレッドへ隔離し、呼び出し側が queue のタイムアウトで待つ形に
    すると、readiness 判定に OS 固有 API が要らなくなり、3 プラットフォーム
    で同一のロジックになります。

    各チャンクは ``.read()`` ではなく ``.read1()`` で読みます。``.read(n)``
    は ``n`` バイト届くか EOF まで待ち続けるため、書き手が途中で止まると
    デッドライン判定の外側でブロックし続けます。

    Args:
        reader: ``reader(n) -> bytes`` の読み取り関数（`_stdin_chunk_reader`）。
        max_bytes: 読み取る最大バイト数。
        sink: ``("data", bytes)`` / ``("error", Exception)`` を受け取るキュー。

    Returns:
        なし

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
        # （pytest の DontReadFromInput 等）。読めない点は OSError と同じなので
        # 同じ扱いにする。ここで捕らえないとワーカースレッド内で例外が消える。
        sink.put(("error", exc))


def _read_stdin_bytes(max_bytes: int) -> bytes:
    """stdin から最大 `max_bytes` 分をバイト列として読みます。

    ``.buffer`` があればワーカースレッド（`_pump_stdin`）へブロッキング
    read を隔離し、本スレッドは queue のタイムアウトで待ちます。最初の
    チャンクは STDIN_FIRST_BYTE_TIMEOUT 秒、以降はそこから
    STDIN_READ_DEADLINE_SECONDS 秒の予算で待ちます。ワーカーは daemon
    なので、待ち切れずに残ってもプロセス終了を妨げません。

    ``.buffer`` が無い場合（io.StringIO 等）は文字数で読んだあと UTF-8 に
    再エンコードします。文字数 read ではバイト上限を最大 4 倍超過しうる
    ため、呼び出し側でバイト換算の切り詰めを行います。

    Args:
        max_bytes: 読み取る最大バイト数です（buffer 無し時は最大文字数）。

    Returns:
        読み取ったバイト列。1 バイト以上受け取った後にデッドライン超過・
        読み取り例外・EOF で打ち切った場合は、その時点までの部分データを
        返します（壊れた JSON として後段が fail-closed に倒します）。

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
        timeout = (
            STDIN_FIRST_BYTE_TIMEOUT if deadline is None else max(0.0, deadline - time.monotonic())
        )
        try:
            kind, payload = sink.get(timeout=timeout)
        except queue.Empty:
            if not collected:
                raise StdinUnavailableError(
                    "stdin の最初のバイトが "
                    f"{STDIN_FIRST_BYTE_TIMEOUT} 秒以内に届きませんでした"
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

    読み取れなかった場合も空文字列を返します。非保護経路（launcher の
    ``--bg`` 中継・``mem.cli`` の payload 読み取り）専用で、これらは
    「入力が無い」と「読めない」を区別しても採れる別の行動が無いためです
    （どちらでも記録すべき材料が無い）。保護 hook は
    `read_raw_stdin_with_truncation` を使い、`StdinUnavailableError` を
    自分で捕捉して fail-closed に倒すこと。

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

    読む対象が無い場合（tty 起動・``sys.stdin`` が None・即 EOF）は
    ``("", False)`` を返します。読み取り自体に失敗した場合は
    `StdinUnavailableError` を送出します — 呼び出し側の保護 hook は
    これを捕捉して deny に倒すこと（ADR-0019）。

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
    truncated = len(raw_bytes) > max_bytes
    if truncated:
        raw_bytes = raw_bytes[:max_bytes]
    return raw_bytes.decode("utf-8", errors="replace"), truncated


def stdin_unreadable_message(hook_name: str, reason: object) -> str:
    """stdin 読み取り不能時の deny 理由を組み立てます。

    4 つの保護 hook が同じ文面を使うための単一情報源です。片方だけ文面や
    方針がずれると「Write なら止まるが Bash なら通る」型の非対称が再発
    します。

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
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def write_stdout(text: str) -> None:
    """標準出力にテキストを書き出します。

    Args:
        text: 出力するテキストです。

    Returns:
        なし

    Raises:
        例外は発生しません。
    """
    sys.stdout.write(text)


def write_stderr(text: str) -> None:
    """標準エラーにテキストを書き出します。

    Args:
        text: 出力するテキストです。

    Returns:
        なし

    Raises:
        例外は発生しません。
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

    `basename`（`Path(path).name`）は POSIX ホストでは `\\` を区切りとみなさない
    ため、クォートで守られた Windows 絶対パス（``"C:\\bin\\rm.exe" ruff.toml``）が
    `shlex(posix=True)` を通っても `\\` を保ったまま 1 トークンで残り、名前照合が
    パス全体と比較されて外れます。`command_dialect_variants` は**クォート外**の
    `\\` しか `/` へ読み替えないため、この経路は方言展開では閉じません。

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

    `/usr/bin/git`（絶対パス）・`git.exe`（Windows）・
    `C:\\Program Files\\Git\\bin\\git.exe`（Windows 絶対パス）・`GIT`（大文字）を
    いずれも `git` へ畳みます。大小を無視するのは、macOS 既定の APFS と Windows の
    NTFS が既定で大小を区別せず（``CP foo bar`` は実 `cp` を起動し、``RM`` は実 `rm`
    を起動する）、PowerShell が cmdlet 名を大小無視で解決するためです。Linux では
    `RM` が `rm` に一致する誤検出側へ倒れますが、ADR-0002 の範囲内です。

    保護 hook の実行名照合は**この 1 関数だけ**を通します。かつて
    `is_git_executable_token`（`hook_common`）・`bash_config_protection._command_name`・
    `pre_bash_commit_quality._segment_mutates_worktree_or_index` が同じ正規化を
    別々に持ち、大小無視と `.exe` 除去が兄弟ごとに 1 世代ずつずれた結果、
    ``git.exe commit --no-verify`` は捕まるのに ``CP evil.py app.py && git commit``
    や ``rm.exe ruff.toml`` は語彙から外れる非対称が出荷されました（H-04 / H-6）。

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
    """保護対象ファイル名の照合用に basename を大小無視へ畳みます。

    `normalize_executable_name` と**同じ case 演算（`casefold`）と同じ basename
    規則**を使います。片方を `lower` / もう片方を `casefold` にすると、閉じたはず
    の非対称（実行名は大小無視なのにファイル名は大小区別、の逆）をそのまま
    再生産するためです。実行名と違い `.exe` は落としません — 保護対象は設定
    ファイルであり、`ruff.toml.exe` を `ruff.toml` と同一視する根拠が無いためです。

    `Path.resolve()` は APFS / NTFS で綴りを実体の大小へ正規化しないため、
    解決後の basename を素で比較すると ``Write Ruff.toml`` が実体 ``ruff.toml``
    へ着地するのに保護判定は外れます（C-2。実測で exit 0）。

    Args:
        path: 検査対象のパス文字列、またはファイル名です。

    Returns:
        大小無視へ畳んだ basename を返します。

    Raises:
        例外は発生しません。
    """
    return _basename_any_separator(path).casefold()


def is_git_executable_token(token: str) -> bool:
    """トークンが git 実行ファイルを指すかを判定します。

    正規化は `normalize_executable_name` へ委譲します（`block_no_verify` /
    `pre_bash_commit_quality` / `bash_config_protection` で共有する単一情報源）。

    Args:
        token: `shlex` 等でトークン化された1トークンです。

    Returns:
        git 実行ファイルとみなせるなら True。

    Raises:
        例外は発生しません。
    """
    return normalize_executable_name(token) == "git"


# sed の GNU 長形式 in-place フラグ。GNU getopt は曖昧でない限り長形式の短縮を
# 受け付け、sed の長形式で `--i` から始まるのは `--in-place` だけなので、
# ``sed --i s/x/y/ ruff.toml`` も実際に in-place 編集になる。したがって
# 「`in-place` の非空プレフィックス」を一致条件にする（`--in-place=.bak` の
# サフィックス指定も名前部分だけを見て拾う）。
_LONG_INPLACE_OPTION_NAME = "in-place"


def is_inplace_edit_flag(token: str) -> bool:
    """トークンが in-place 編集フラグ（`sed -i` / `perl -0pi` / `sed --in-place`）かを判定します。

    単一ダッシュの短形式は**結合位置を問わず** `i` を含むかで判定します
    （`-i` / `-i.bak` / `-0pi` / `-ni` はいずれも in-place）。`sed` 側だけが
    `-i` 前方一致で、`perl` 側だけが `i` の包含判定という食い違いがあったため、
    両者の和集合をこの 1 関数へ集約します。

    長形式は `_LONG_INPLACE_OPTION_NAME` の非空プレフィックスに限ります。
    区切りの `--` は名前部分が空になるため in-place とみなしません
    （``sed -- s/x/y/ ruff.toml`` は標準出力へ流すだけで書き込まない）。
    `--expression` のような別の長形式も、`in-place` のプレフィックスでは
    ないので一致しません。

    `bash_config_protection`（保護対象への in-place 書き込み）と
    `pre_bash_commit_quality`（commit 前の作業ツリー変更）が共有します。
    後者は `not arg.startswith("--")` で長形式を**明示的に**除外していたため、
    ``sed --in-place ... && git commit -am x`` で両方のガードが同時に不発に
    なっていました（H-5）。

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


def resolve_effective_target(raw_path: str) -> Path | None:
    """cwd 基準で解決し、symlink を辿った実体 path を返します（H-02 対応）。

    `config_protection` / `bash_config_protection` が basename だけで保護対象
    判定していたため、`alias -> pyproject.toml` のような symlink 経由の
    書き込みが判定をすり抜けていた。両モジュールがこの共有 helper で解決後の
    実体 path を得てから basename 判定することで、判定基準を一本化する。

    Args:
        raw_path: 検査対象の生パス文字列（相対 / 絶対 / symlink いずれも可）。

    Returns:
        解決できた実体 Path。壊れた・循環した symlink 等で解決できない場合は
        None（呼び出し側はこの場合を fail-closed/fail-open どちらに倒すか
        自身の文脈で決める）。

    Raises:
        例外は発生しません。
    """
    try:
        return (Path.cwd() / raw_path).resolve(strict=False)
    except (OSError, RuntimeError):
        return None


# detach 起動した子の実行時間上限（秒）。start_new_session=True の子はハーネスの
# timeout で kill されないため、自前の watchdog で自決させる。
#
# この値は hooks.json の timeout とは無関係に決める。detach 後の子はハーネスの
# 管轄外であり、hooks.json の値（最大は mem.cli context の 60 秒だが、これは
# detach しない同期エントリ）と紐付ける論拠がないため。
#
# detach 対象（launcher --bg: mem.cli handoff）は正常系ではローカル I/O 数秒で
# 終わる。したがって
# 上限は「正常系を絶対に切らない」ことを優先した安全網の閾値であり、
# 10 分走り続けていれば確実に異常（ハング・暴走）と断定できる 600 秒を採る。
DETACH_TIMEOUT_SECONDS = 600
# SIGTERM を無視して詰まったプロセスを SIGKILL で確実に回収するまでの猶予（秒）。
_DETACH_KILL_AFTER_SECONDS = 30

# detach した子を DETACH_TIMEOUT_SECONDS で穏当に停止（POSIX: SIGTERM、
# Windows: TerminateProcess 相当）し、応答が無ければ _DETACH_KILL_AFTER_SECONDS
# 後に強制終了する watchdog。coreutils の `timeout`/`gtimeout` は BSD/macOS に
# 標準で存在せず（--kill-after は GNU 固有）、ランタイム依存ゼロの方針にも
# 反するため、既に起動に使っている sys.executable 自身で実装し外部コマンドへの
# 依存をなくす。
#
# POSIX では停止シグナルを「プロセスグループ」へ送る。子を
# start_new_session=True で新しいセッション（= 新しいプロセスグループ）の
# リーダーにし、os.killpg で子と孫をまとめて回収する。Popen.terminate()/kill()
# は直接の子 1 プロセスにしか届かないため、孫プロセスの無期限残留を防げない。
#
# Windows には killpg / SIGKILL が無い（`os.killpg` 不在、`signal.SIGKILL`
# 未定義、`start_new_session` は ValueError）。ここは Windows 用の分岐を置く
# （release-verify 2026-09-03 の P1-013）。分岐はプラットフォーム名ではなく
# `hasattr(os, "killpg")` という capability で行い、Windows では
# CREATE_NEW_PROCESS_GROUP|DETACHED_PROCESS で起動して
# Popen.terminate()/kill() で回収する。
#
# 受容する差: Windows では孫プロセスが回収されない（Job Object を使えば
# 回収できるが、ctypes で書く 100 行超が macOS/Linux では 1 行も実行されず
# 検証もできない）。現在の --bg 対象は SessionEnd の `mem.cli handoff` の
# 1 つで、その孫は `git` 呼び出しのみ。git 側にも GIT_TIMEOUT_SECONDS の
# ハードタイムアウトがあり、無期限残留は起きない。対象が孫を長時間持つ
# 処理へ広がった時点で Job Object を再検討する。
#
# watchdog 自身が SIGTERM を受けた場合も、そのまま終了すると孫が残るため、
# ハンドラで子へ停止を cascade し、猶予後に強制終了してから抜ける
# （ハンドラ内で proc.wait() を再入させないよう time.sleep で待つ）。
#
# コスト: detach 1 回につき watchdog + 対象の 2 プロセスが起動する。現在の --bg
# 対象（SessionEnd の mem.cli handoff）はセッション終了イベントでのみ発火する
# ため、ツールコールごとの頻度ではない。それでも意図的なコストであり、
# 削減目的で watchdog を外してはならない:
#   - watchdog を消すと、detach 済みの子と孫を kill する主体が消滅する。子は
#     ハーネス timeout の管轄外なので、ハングした子と孫が無制限に残留する。
#   - 子プロセス内の `signal.alarm` では代替できない。alarm は自プロセスにしか
#     届かず、子が孫プロセスを起動する構成になった場合に回収できないため
#     等価ではない。Windows には alarm 自体が無い。
#   - watchdog は sys.executable の `-c` 実行で、対象モジュールを import せず
#     待つだけなので、追加コストは Python インタプリタ起動 1 回分に留まる。
# すなわち「毎回 1 プロセス分の起動コスト」と「孫プロセスの無制限残留を防ぐ
# kill 保証」のトレードオフであり、後者を採る。
_WATCHDOG_SCRIPT = """
import os, signal, subprocess, sys, time

timeout, kill_after = float(sys.argv[1]), float(sys.argv[2])
log_path = os.environ.get("PLE4_BG_LOG_PATH")
log_file = open(log_path, "ab") if log_path else subprocess.DEVNULL
has_process_groups = hasattr(os, "killpg")
if has_process_groups:
    spawn_kwargs = {"start_new_session": True}
else:
    spawn_kwargs = {
        "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0)
    }
proc = subprocess.Popen(
    sys.argv[3:],
    stdin=sys.stdin,
    stdout=log_file,
    stderr=log_file,
    **spawn_kwargs,
)

def stop_child(hard):
    try:
        if has_process_groups:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL if hard else signal.SIGTERM)
        elif hard:
            proc.kill()
        else:
            proc.terminate()
    except (ProcessLookupError, OSError):
        pass

def cascade(signum, frame):
    stop_child(False)
    time.sleep(kill_after)
    stop_child(True)
    os._exit(128 + signum)

signal.signal(signal.SIGTERM, cascade)
try:
    proc.wait(timeout=timeout)
except subprocess.TimeoutExpired:
    stop_child(False)
    try:
        proc.wait(timeout=kill_after)
    except subprocess.TimeoutExpired:
        stop_child(True)
        proc.wait()
"""


def detached_spawn_kwargs() -> dict[str, Any]:
    """親から独立したプロセスグループで子を起動するための Popen 引数を返す。

    POSIX は ``start_new_session=True``、Windows は
    ``CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS`` が同じ意図を表す
    （``start_new_session`` を Windows で渡すと ``ValueError`` になり、
    SessionEnd の handoff hook がそこで落ちていた。P1-013）。分岐は
    プラットフォーム名ではなく ``os.setsid`` の有無という capability で
    行う。

    Args:
        なし

    Returns:
        ``subprocess.Popen`` へ展開する追加キーワード引数。

    Raises:
        例外は発生しません。
    """
    if hasattr(os, "setsid"):
        return {"start_new_session": True}
    return {
        "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0)
    }


def _watchdog_argv(cmd: list[str]) -> list[str]:
    """detach 対象を watchdog 付きで起動する argv を組み立てます。

    Args:
        cmd: 監視対象のコマンドリストです。

    Returns:
        `sys.executable -c _WATCHDOG_SCRIPT` で対象を包んだ argv を返します。

    Raises:
        例外は発生しません。
    """
    return [
        sys.executable,
        "-c",
        _WATCHDOG_SCRIPT,
        str(DETACH_TIMEOUT_SECONDS),
        str(_DETACH_KILL_AFTER_SECONDS),
        *cmd,
    ]


def _detach_log_path() -> Path | None:
    """detach した子プロセスの stdout/stderr を追記するログファイルのパスを返す。

    以前は DEVNULL に捨てており、detach 対象（--bg 起動）の import 失敗
    等の出力が完全に消えていた（起動受付成功と処理完了・成功の区別が
    付かない一因。F-18）。日付ごとに 1 ファイルへ追記する
    （mem/logger.py の `mem-YYYY-MM-DD.log` と同じ命名規則）。
    best-effort の diagnostics であり、ログディレクトリを作れない場合は
    None を返して detach 自体は継続させる（ログが取れないことを理由に
    起動を諦めない）。

    Args:
        なし

    Returns:
        `~/.ple4/logs/bg-YYYY-MM-DD.log` の Path。作成失敗時は None。

    Raises:
        例外は発生しません。
    """
    try:
        log_dir = ensure_private_dir(get_ple4_dir() / "logs")
    except OSError:
        return None
    return log_dir / f"bg-{datetime.now():%Y-%m-%d}.log"


_DETACH_STDIN_SUFFIX = ".stdin"
"""detach 用 stdin 一時ファイルの拡張子。"""

_DETACH_STDIN_GRACE_SECONDS = 60 * 60
"""この秒数より古い ``*.stdin`` 孤児だけを回収する（実行中の detach を奪わない）。
detach の実行上限は DETACH_TIMEOUT_SECONDS（600 秒）なので、1 時間は十分な余裕。"""


def gc_detach_stdin_orphans() -> None:
    """``~/.ple4`` 直下に残った detach 用 stdin 一時ファイルを age-gate で回収する。

    POSIX では `detach_process` が起動直後に unlink するため孤児は生じない。
    Windows は「開いているファイルを削除できない」ため（子が継承ハンドルを
    保持している）unlink が必ず失敗し、SessionEnd ごとに 1 個ずつ積み上がる
    （release-verify 2026-09-03 の再レビュー W4）。

    回収を生成側と同じモジュールへ置くのは、走査先を生成側と同じ
    ``get_ple4_dir()``（``PLE4_HOME`` を見る）に揃えるため。``env_pointer`` の
    GC は ``$HOME`` 固定の別契約であり、そちらに置くと ``PLE4_HOME`` を使う
    環境で「作る場所と掃除する場所が違う」ことになる。

    Args:
        なし

    Returns:
        なし

    Raises:
        例外は発生しません（iterdir/stat/unlink の失敗は無視します）。
    """
    cutoff = time.time() - _DETACH_STDIN_GRACE_SECONDS
    try:
        entries = list(get_ple4_dir().iterdir())
    except OSError:
        return
    for entry in entries:
        if not entry.name.endswith(_DETACH_STDIN_SUFFIX):
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            continue


def detach_process(cmd: list[str], raw_stdin: str, *, env: dict[str, str] | None = None) -> bool:
    """コマンドを detached（新セッション）で起動し stdin を一時ファイル経由で渡す。

    親プロセスの終了に影響されず子を走らせ続けるために使う。起動のたびに
    `gc_detach_stdin_orphans` で過去の孤児（Windows では unlink できず必ず
    残る）を掃除してから作る。一時ファイルは
    world-writable な /tmp を避けて ~/.ple4 配下に作成し、close→reopen の
    TOCTOU 窓を作らないよう同一 fd を seek(0) して子へ継承する。起動直後に
    unlink する（継承済み fd は有効なまま）。

    detach 後の子はハーネスの timeout の管轄外になるため、_WATCHDOG_SCRIPT で
    ラップして DETACH_TIMEOUT_SECONDS で穏当に停止、さらに猶予後に強制終了し、
    暴走プロセスの無期限残留を防ぐ。POSIX では停止シグナルを子のプロセス
    グループへ送るため、子が起動した孫プロセスもまとめて回収される
    （Windows は直接の子のみ。理由は `_WATCHDOG_SCRIPT` のコメント参照）。

    対象の stdout/stderr は `PLE4_BG_LOG_PATH` 環境変数で
    _WATCHDOG_SCRIPT へ log ファイルパスを渡し、そこへ追記させる
    （`_detach_log_path` が None を返した場合のみ DEVNULL にフォールバック
    する）。起動受付の成否（この関数の戻り値）と、対象プロセスの実行結果
    は依然として別概念であり、後者を呼び出し元へ同期的に返す契約は無い
    （best-effort の非同期処理という host 契約は変えない）。診断が必要な
    場合は log ファイルを参照する。

    Args:
        cmd: subprocess に渡すコマンドリスト。
        raw_stdin: 子プロセスへ渡す stdin の内容。
        env: 子プロセスの環境変数。None なら親の環境を継承する。

    Returns:
        起動に成功した場合 True、OSError 時は False。

    Raises:
        例外は発生しません。
    """
    gc_detach_stdin_orphans()
    try:
        private_dir = ensure_private_dir(get_ple4_dir())
        tmp = tempfile.NamedTemporaryFile(
            mode="w+", encoding="utf-8", suffix=".stdin", dir=private_dir, delete=False
        )
    except OSError:
        return False
    try:
        tmp.write(raw_stdin)
        tmp.flush()
        tmp.seek(0)
        child_env = dict(env) if env is not None else dict(os.environ)
        log_path = _detach_log_path()
        if log_path is not None:
            child_env["PLE4_BG_LOG_PATH"] = str(log_path)
        subprocess.Popen(
            _watchdog_argv(cmd),
            stdin=tmp,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=child_env,
            **detached_spawn_kwargs(),
        )
        return True
    except OSError:
        return False
    finally:
        tmp.close()
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


# 前回セッション detach 失敗通知（recent_bg_failure_notice）の 1 ファイル
# あたり読み取り上限バイト数。1 行提示できれば十分なため小さく取る。
_BG_FAILURE_TAIL_MAX_BYTES = 4096


def _read_tail_bytes(path: Path, max_bytes: int) -> str:
    """ファイル末尾を上限バイトまで読む（seek-from-end）。

    `mem.handoff._read_tail` と同じ末尾限定パターンです。読めない場合は
    空文字列を返します（呼び出し元を落とさない）。

    Args:
        path: 読み取り対象のファイル。
        max_bytes: 末尾から読む最大バイト数。

    Returns:
        デコード済みの末尾テキスト。読めなければ空文字列。

    Raises:
        例外は発生しません。
    """
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - max_bytes))
            return stream.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def recent_bg_failure_notice() -> str:
    """前回セッションの detach 起動（``--bg``）に失敗の痕跡があれば 1 行の通知を返す。

    `detach_process` の戻り値は「起動受付」であり「処理成功」ではないため
    （docstring 参照）、detach した子の失敗は呼び出し元へ同期的に伝わらない
    （§6.2 対応）。そこで次回 SessionStart の ``mem context`` で、前回の
    `_detach_log_path` が書いたログファイル（``bg-YYYY-MM-DD.log``。
    正常系では対象プロセスが何も出力しないため中身は空のはず）に内容が
    あれば「起動受付後に何かが起きた」痕跡とみなし、末尾 1 行だけ提示する。
    失敗時のみ出力するため「出力トークン最小化」原則と両立する。

    読み取り範囲は当日＋前日の 2 ファイルまでに限定し、各ファイルは末尾
    `_BG_FAILURE_TAIL_MAX_BYTES` バイトまでしか読まない（SessionStart の
    hooks.json timeout 60 秒に対する境界を持たせるため）。

    Args:
        なし

    Returns:
        失敗の痕跡があれば人間可読の 1 行。無ければ空文字列。

    Raises:
        例外は発生しません。
    """
    log_dir = get_ple4_dir() / "logs"
    today = datetime.now()
    for offset in (0, 1):
        day = today - timedelta(days=offset)
        path = log_dir / f"bg-{day:%Y-%m-%d}.log"
        if not path.is_file():
            continue
        tail = _read_tail_bytes(path, _BG_FAILURE_TAIL_MAX_BYTES).strip()
        if not tail:
            continue
        last_line = tail.splitlines()[-1]
        return f"前回のバックグラウンド起動でエラーの痕跡があります（{path.name}）: {last_line}"
    return ""


def emit_block_output(reason: str) -> int:
    """ツール実行ブロックを host 非依存の合併出力で stdout/stderr に書き出す。

    ツール実行をブロックするフックは終了コードを直接返さず、このヘルパの
    戻り値を返すこと（stderr への理由書き出し + stdout への deny JSON を
    同時に出し、exit code は常に 2 を返す）。

    Args:
        reason: ブロック理由（ユーザー / エージェントに提示される）。

    Returns:
        フックが返すべき終了コード。

    Raises:
        例外は発生しません。
    """
    exit_code, deny_out, reason_err = emit_block(reason)
    write_stdout(deny_out)
    write_stderr(reason_err + "\n")
    return exit_code


def _emit_hook_specific_output(event_name: str, additional_context: str) -> str:
    """コンテキスト注入出力を host 非依存の合併 JSON で返す。

    Args:
        event_name: hookEventName に設定するイベント名。
        additional_context: コンテキストに注入する追加文字列。

    Returns:
        ハーネスのプロトコルに適合した JSON 文字列。

    Raises:
        例外は発生しません。
    """
    return adapt_context_output(event_name, additional_context)


def emit_session_start_output(additional_context: str = "") -> str:
    """SessionStart 用のフック出力 JSON 文字列を返す。

    host 非依存の合併出力を output_adapter 経由で生成する。
    stdout への書き込みは行わない純粋関数として使う。

    Args:
        additional_context: コンテキストに注入する追加文字列。

    Returns:
        additionalContext（トップレベル）と hookSpecificOutput を同時に
        含む合併 JSON 文字列。

    Raises:
        例外は発生しません。
    """
    return _emit_hook_specific_output("SessionStart", additional_context)


def print_session_start_output(additional_context: str = "") -> None:
    """SessionStart 用のフック出力を stdout に書き出す。

    Args:
        additional_context: コンテキストに注入する追加文字列。

    Returns:
        None

    Raises:
        例外は発生しません。
    """
    print(emit_session_start_output(additional_context))
