"""git のフックバイパスフラグ（--no-verify / commit の -n）をブロックします。

トリガー: pre:bash
入力: bashコマンドを含むJSON
出力: ブロック時はハーネス別のブロック出力（Claude/Codex: stderr、Copilot: deny JSON）
終了: バイパス検出時はハーネス別のブロックコード、それ以外は0

判定方式:
    正規表現ではなく ``shlex`` によるトークン走査を行います。``git`` と
    サブコマンドの隣接を前提にすると、``git --no-pager commit --no-verify``
    や ``git -C <path> commit --no-verify``、``git -c k=v commit --no-verify``
    のようにグローバルオプションを挟む形で容易にバイパスできるためです。
    トークン列は ``&&`` / ``||`` / ``;`` / ``|`` / ``&`` / ``(`` / ``)`` で
    セグメントに分割し（``git add -A && git commit --no-verify`` を検出する
    ため）、各セグメントの**全トークン**から git 起動トークンを探索します。

git 起動トークンの探索:
    セグメント先頭が ``git`` であることは前提にしません。先頭固定にすると
    ``sudo git commit --no-verify`` / ``env git ...`` / ``GIT_DIR=.git git ...``
    / ``xargs -I% git ...`` / ``/usr/bin/git ...`` がいずれも素通りします。
    ラッパー名（``env`` / ``sudo`` / ``nice`` …）のホワイトリスト方式は将来の
    ラッパーを取りこぼすため採らず、**トークンの basename が ``git``** なら
    git 起動とみなす方式にしています。1 セグメント内に複数の git 起動が
    あり得る（``sudo -u git git commit -n``）ため、全ての git 起動トークンを
    検査します。解析は git 起動トークン以降のみを対象とするので、
    ``grep -rn git .`` のように ``-n`` が git より前にある形は誤検知しません。

``-n`` の扱い:
    ``-n`` は ``git commit`` では ``--no-verify`` の別名ですが、``git push``
    では ``--dry-run`` の別名でありフックバイパスではありません。そのため
    ``-n`` はサブコマンドが ``commit`` と確定できた場合にブロックし、
    ``git push -n`` や ``git log -n 5`` は通します。加えて、既知でない
    グローバルオプション（将来の git が追加するもの）が値を取るかどうかは
    判定できずサブコマンド解決がずれるため、**サブコマンドを確定できない
    場合は fail-closed でブロック**します（``git --future-option value
    commit -n`` を塞ぐ）。``--no-verify`` は ``commit`` / ``push`` いずれでも
    フックバイパスなので、サブコマンドの確定を待たず git 起動トークン以降に
    現れた時点でブロックします。

クォート内の誤検知:
    ``shlex`` はクォート内をひとつのトークンとして保持するため、
    ``git commit -m "use -n flag"`` のメッセージ本文はフラグとして
    解釈されず、``echo "git commit --no-verify"`` の中身も git 起動トークン
    になりません。加えて値を取るオプション（``-m`` / ``-F`` など）の
    値トークンはフラグ走査から除外します。

非目標（原理的に検出不能なため検出しません）:
    - シェルエイリアス・シェル関数経由の呼び出し（``alias g=git`` の ``g``、
      ``commit() { git commit --no-verify; }`` の ``commit``）
    - ``git $(echo commit) --no-verify`` のようなコマンド置換・変数展開
      （``$VAR`` / ``$(...)`` / バッククォート）を経由した組み立て
    - ``git`` 以外の名前を持つラッパースクリプト（``mygit`` / ``./deploy.sh``）
      の内部で行われる git 呼び出し
    - ``sh -c '...'`` / ``eval`` に渡す文字列の再帰的な解釈
    POSIX シェルのセマンティクス全体を模倣することは不可能であり、本フックは
    「うっかりバイパス」の抑止であって敵対的回避への防壁ではありません。
    git 自体が大文字小文字を区別するため、トークン比較も区別します。
"""

from __future__ import annotations

import shlex
from typing import NamedTuple

from bluecore.hooks.hook_common import emit_block_output, parse_json_object, read_raw_stdin
from bluecore.lib.harness import extract_bash_command

# コマンド全体をセグメントに割るシェル区切りトークン。
_SHELL_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "(", ")"})

# 値を別トークンとして必ず取る long オプション（git グローバル + commit/push）。
# 値が ``=`` で結合されている場合は次トークンを消費しません。
_VALUE_LONG_OPTIONS = frozenset(
    {
        # git グローバルオプション
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--exec-path",
        "--super-prefix",
        "--config-env",
        "--attr-source",
        # git commit のオプション
        "--message",
        "--file",
        "--template",
        "--author",
        "--date",
        "--fixup",
        "--squash",
        "--reuse-message",
        "--reedit-message",
        "--cleanup",
        "--trailer",
        "--pathspec-from-file",
    }
)

# 値を取らないことが確定している git グローバル long オプション。
# ここに無い未知の long オプションはサブコマンド解決を信用できない印になります
# （``git --future-option value commit -n`` を塞ぐための fail-closed 判定材料）。
_BOOLEAN_GLOBAL_LONG_OPTIONS = frozenset(
    {
        "--version",
        "--help",
        "--html-path",
        "--man-path",
        "--info-path",
        "--paginate",
        "--no-pager",
        "--bare",
        "--no-replace-objects",
        "--no-lazy-fetch",
        "--no-optional-locks",
        "--no-advice",
        "--literal-pathspecs",
        "--glob-pathspecs",
        "--noglob-pathspecs",
        "--icase-pathspecs",
    }
)

# 値を必ず取る short オプション文字。結合形（``-mmsg``）なら残りが値、
# クラスタ末尾（``-am``）なら次トークンが値になります。
_VALUE_SHORT_OPTIONS = frozenset("CcmFt")

# 値を取らないことが確定している git グローバル short オプション文字
# （``git -p log -n 5``）。long 側と同じく、未知の文字はサブコマンド解決を
# 信用できない印として扱います。
_BOOLEAN_GLOBAL_SHORT_OPTIONS = frozenset("pPvh")

# 値が省略可能で、指定する場合は結合形のみ許される short オプション文字
# （``-uno`` / ``-Skeyid``）。次トークンは値として消費しません。
_OPTIONAL_VALUE_SHORT_OPTIONS = frozenset("uS")

# フックバイパスに直結する long フラグ。サブコマンドを問わずブロックします。
_BYPASS_LONG_FLAG = "--no-verify"

# ``git commit`` でのみ ``--no-verify`` の別名になる short フラグ。
_BYPASS_SHORT_FLAG = "-n"


class GitInvocation(NamedTuple):
    """git 起動トークン以降を解析した結果。

    Attributes:
        subcommand: 解決できたサブコマンド名。見つからなければ None。
        flags: 検出したフラグ名のリスト（オプションの値トークンは含まない）。
        subcommand_certain: サブコマンドの解決結果を信用してよいか。未知の
            グローバルオプションが値を取る可能性で解決位置がずれ得る場合は
            False になり、``-n`` 判定を fail-closed に倒す材料になります。
    """

    subcommand: str | None
    flags: list[str]
    subcommand_certain: bool


def tokenize(command: str) -> list[str]:
    """シェルコマンドを区切り記号込みのトークン列へ分割する。

    ``shlex`` を ``punctuation_chars=True`` で使い、``git add -A&&git commit``
    のように空白なしで連結された区切り記号も独立トークンにします。クォート
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
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return command.split()


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


def is_git_invocation(token: str) -> bool:
    """トークンが git 実行ファイルの起動かどうかを判定する。

    ``git`` そのものに加え ``/usr/bin/git`` のようなパス付き起動を拾うため、
    ``/`` で区切った最後の要素（basename）が ``git`` かどうかで判定します。
    ``gitk`` / ``GIT_DIR=.git`` / ``git/``（ディレクトリ）は一致しません。

    Args:
        token: 検査対象のトークン。

    Returns:
        git 起動トークンなら True。

    Raises:
        例外は発生しません。
    """
    return token.rsplit("/", 1)[-1] == "git"


def _consume_short_cluster(token: str, flags: list[str]) -> tuple[int, bool]:
    """short オプションのクラスタを 1 文字ずつフラグへ展開する。

    ``-am`` のように複数の short オプションが結合したトークンを扱います。
    値を取る文字に到達した時点で以降は値なので走査を打ち切ります。

    Args:
        token: ``-`` で始まる（``--`` ではない）short オプショントークン。
        flags: 検出したフラグを追記するリスト。

    Returns:
        (値として追加で消費すべき後続トークン数（0 または 1）,
        git グローバルオプションとして未知の文字を含むか) のタプル。
        後者はサブコマンド未解決時のみ意味を持ちます。

    Raises:
        例外は発生しません。
    """
    last_index = len(token) - 1
    unknown = False
    for index, char in enumerate(token[1:], start=1):
        flags.append(f"-{char}")
        if char in _OPTIONAL_VALUE_SHORT_OPTIONS:
            if index < last_index:
                return 0, unknown
            continue
        if char in _VALUE_SHORT_OPTIONS:
            return (0 if index < last_index else 1), unknown
        if char not in _BOOLEAN_GLOBAL_SHORT_OPTIONS:
            unknown = True
    return 0, unknown


def parse_git_segment(segment: list[str]) -> GitInvocation:
    """``git`` で始まるトークン列からサブコマンドとフラグ一覧を取り出す。

    ``git`` の後ろに任意個現れるグローバルオプション（値を取るものを含む）を
    読み飛ばし、最初の非オプション語をサブコマンドとみなします。オプションの
    値トークンはフラグ一覧に含めません（``git commit -m "-n"`` のような
    値をフラグと誤認しないため）。``--`` 以降は pathspec とみなし走査を
    打ち切ります。

    サブコマンド解決前に既知でないグローバルオプションが現れた場合、それが
    値を取るかどうかを判定できずサブコマンド位置がずれるため
    ``subcommand_certain=False`` を返します。

    Args:
        segment: 先頭要素が ``git`` 起動トークンであるトークンリスト。

    Returns:
        解析結果の `GitInvocation`。サブコマンドが見つからない場合
        `GitInvocation.subcommand` は None です。

    Raises:
        例外は発生しません。
    """
    subcommand: str | None = None
    flags: list[str] = []
    certain = True
    index = 1
    while index < len(segment):
        token = segment[index]
        index += 1
        if token == "--":
            break
        if token.startswith("--"):
            name = token.split("=", 1)[0]
            flags.append(name)
            if "=" not in token:
                if name in _VALUE_LONG_OPTIONS:
                    index += 1
                elif subcommand is None and name not in _BOOLEAN_GLOBAL_LONG_OPTIONS:
                    certain = False
            continue
        if token.startswith("-") and len(token) > 1:
            consumed, unknown = _consume_short_cluster(token, flags)
            index += consumed
            if unknown and subcommand is None:
                certain = False
            continue
        if subcommand is None:
            subcommand = token
    return GitInvocation(subcommand=subcommand, flags=flags, subcommand_certain=certain)


def has_bypass_flag(command: str) -> bool:
    """コマンド文字列に git フックバイパスフラグが含まれるかを判定する。

    セグメント先頭に限らず全トークンから git 起動トークンを探し、見つかった
    位置以降を `parse_git_segment` で解析します（``sudo git commit -n`` や
    ``/usr/bin/git commit --no-verify`` を捕捉するため）。1 セグメントに
    複数の git 起動があれば全て検査します。

    Args:
        command: 検査対象のシェルコマンド文字列。

    Returns:
        ``--no-verify``（サブコマンド不問）、``git commit`` の ``-n``、または
        サブコマンドを確定できない状態での ``-n`` を検出したら True。

    Raises:
        例外は発生しません。
    """
    for segment in split_segments(tokenize(command)):
        for index, token in enumerate(segment):
            if not is_git_invocation(token):
                continue
            invocation = parse_git_segment(segment[index:])
            if _BYPASS_LONG_FLAG in invocation.flags:
                return True
            if _BYPASS_SHORT_FLAG in invocation.flags and (
                invocation.subcommand == "commit" or not invocation.subcommand_certain
            ):
                return True
    return False


def main() -> int:
    """git コマンドでフックバイパスフラグの使用をブロックする。

    Args:
        引数はありません（標準入力から読み取る）。

    Returns:
        終了コード（0: 許可、ブロック時はハーネス別コード）

    Raises:
        例外は発生しません。
    """
    raw = read_raw_stdin()
    data = parse_json_object(raw)

    if data:
        command = extract_bash_command(data)
        if has_bypass_flag(command):
            return emit_block_output("[Hook] BLOCKED: git hook bypass flags are not allowed")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
