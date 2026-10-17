"""git のフックバイパスフラグ（--no-verify / commit の -n）をブロックします。

トリガー: pre:bash
入力: bashコマンドを含むJSON
出力: ブロック時は host 非依存の合併ブロック出力（stderr の理由 + stdout の deny JSON）
終了: バイパス検出時は 2、それ以外は 0

判定方式:
    正規表現ではなく ``shlex`` によるトークン走査を行います。``git`` とサブコマンドの
    隣接を前提にすると、``git --no-pager commit --no-verify`` や ``git -C <path> commit
    --no-verify`` のようにグローバルオプションを挟む形で容易にバイパスできるためです。
    トークン列はシェル区切りでセグメントに分割し、各セグメントの**全トークン**から
    git 起動トークンを探索します。

git 起動トークンの探索:
    セグメント先頭が ``git`` であることは前提にしません（``sudo git commit --no-verify`` /
    ``GIT_DIR=.git git ...`` / ``xargs -I% git ...`` / ``/usr/bin/git ...`` を通さないため）。
    ラッパー名のホワイトリストは将来のラッパーを取りこぼすため採らず、**トークンの
    basename が ``git``** なら git 起動とみなします。1 セグメント内の全ての git 起動
    （``sudo -u git git commit -n``）を検査し、解析は git 起動トークン以降だけを対象に
    するので ``grep -rn git .`` は誤検知しません。

    兄弟の ``bash_config_protection`` が wrapper の allowlist を持つのとの非対称は意図的
    です。本モジュールは「git が起動されるか」だけを見れば足り、実行位置を必要としない
    ため名前集合を持たずに済みます（docs/adr/shell-analysis-boundary.md）。

``-n`` の扱い:
    ``-n`` は ``git commit`` では ``--no-verify`` の別名ですが、``git push`` では
    ``--dry-run`` です。そのため ``-n`` はサブコマンドが ``commit`` と確定できた場合に
    ブロックし、``git push -n`` や ``git log -n 5`` は通します。既知でないグローバル
    オプションが値を取るかは判定できずサブコマンド解決がずれるため、**サブコマンドを
    確定できない場合は fail-closed でブロック**します（``git --future-option value
    commit -n``）。``--no-verify`` はサブコマンドを問わずブロックします。

heredoc 本文:
    データとして書かれた heredoc 本文はトークン化の前に落とします
    （docs/adr/shell-analysis-boundary.md）。本文が実行されうる形は落としません。

解析できない入力（クォート不整合）:
    ``echo $'\\'';git commit`` のように bash では有効でも ``shlex`` が解析できない入力は、
    区切り演算子を割った空白分割（`shell_tokens.tokenize_with_status`）で走査する。語に
    引用符が残るため（``"git" commit "--no-verify"``）それでも読めない形が残る。この
    **解析できなかったときに限り**、生文字列に ``--no-verify`` / ``hooksPath`` が見えたら
    ブロックする。解析できた入力には当てない（``grep -- '--no-verify' README.md`` を通す）。
    解析不能な入力の引用文に危険語があれば誤ってブロックされるが、回避は引用符を整合させる
    ことで、誤通過より小さい代償として許容する。``-n`` と ``--no-verify`` の前置は生文字列では
    判定できず、この経路では検出しない。

クォート内の誤検知:
    ``shlex`` はクォート内をひとつのトークンとして保持するため、``git commit -m "use -n
    flag"`` のメッセージ本文や ``echo "git commit --no-verify"`` の中身はフラグや git 起動に
    なりません。値を取るオプション（``-m`` / ``-F`` など）の値トークンもフラグ走査から
    除外します。

long オプションの短縮形:
    git は曖昧でない限り long オプションの**前置**を受理するため（``git commit
    --no-veri`` は実際に通る）、``--no-verify`` の前置（``--n`` 以上の長さ）もブロックします。

config 経由のフック無効化（config key は大小文字を区別せず判定）:
    - ``-c`` / ``--config-env`` / ``git config`` の書込みで ``core.hooksPath`` を差し替える
    - 同じ経路で ``include.path`` / ``includeIf.*`` を書き、取り込んだ先で
      ``core.hooksPath`` を書く
    - 同じ経路で ``alias.*`` を定義し、展開先に ``--no-verify`` を隠す
    - ``GIT_CONFIG_PARAMETERS`` / ``GIT_CONFIG_GLOBAL`` / ``GIT_CONFIG_SYSTEM`` /
      ``GIT_CONFIG_NOSYSTEM`` / ``GIT_CONFIG_KEY_<n>`` の literal 環境変数代入

``sh -c`` ラッパー（1 段のみ）:
    既知シェルの ``-c``（結合短フラグ ``bash -lc`` を含む）へ渡された文字列引数は、1 段
    だけ再帰的に同じ判定へ通します。2 段以上のネストや ``eval``・任意ラッパー
    スクリプトへは再帰しません。

非目標（原理的に検出不能、または意図的に対象外とするため検出しません）:
    - シェルエイリアス・シェル関数経由の呼び出し（コマンドラインに現れないため。
      config の ``alias.*`` は literal に現れるので対象内）
    - ``git --git-dir=<other>`` / ``GIT_DIR=<other>``（そのリポジトリのフックは通常どおり
      走るためバイパスではない）
    - コマンド置換・変数展開（``$VAR`` / ``$(...)`` / バッククォート）を経由した組み立て
    - ``git`` 以外の名前を持つラッパースクリプト内部の git 呼び出し
    - ``eval`` に渡す文字列の解釈、および ``sh -c`` の 2 段以上のネスト
    本フックは「うっかりバイパス」の抑止であって敵対的回避への防壁ではありません。
    git 自体が大文字小文字を区別するため、トークン比較も区別します（config key を除く）。
"""

from __future__ import annotations

import re
from typing import NamedTuple

from claq.hooks.hook_common import (
    emit_block_output,
    is_git_executable_token,
)
from claq.hooks.shell_tokens import (
    MAX_COMMAND_TOKENS,
    command_dialect_variants,
    command_exceeds_scan_budget,
    extract_shell_wrapper_command,
    split_segments,
    strip_data_heredoc_bodies,
    tokenize_with_status,
)
from claq.hooks.stdin_io import (
    MAX_STDIN_BYTES,
    StdinUnavailableError,
    parse_json_object,
    read_raw_stdin_with_truncation,
    stdin_unreadable_message,
)
from claq.lib.harness import INPUT_CONTAINER_KEYS, extract_tool_input, iter_bash_commands

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

# long オプションの短縮形を受け付ける最短長。``--n`` / ``--no`` は git 側では曖昧で
# エラーになるが、deny 側に倒す（誤検出 > 誤通過）。
_MIN_ABBREVIATED_LONG_OPTION_LENGTH = 3

# ``git commit`` でのみ ``--no-verify`` の別名になる short フラグ。
_BYPASS_SHORT_FLAG = "-n"

# ``-c`` / ``--config-env`` の値が指す config key。git の config key 名は
# 大小文字を区別しないため、比較は小文字化した上で行います。
_HOOKS_PATH_CONFIG_KEY = "core.hookspath"

# フック実行を左右しうる config key の前置。いずれも小文字で比較します。
#
# - ``core.hookspath``: フック置き場そのものの差し替え。
# - ``include.path`` / ``includeif.``: 取り込んだ先で ``core.hooksPath`` を書けるため、
#   直接指定と等価。
# - ``alias.``: ``git -c alias.ci='commit --no-verify' ci`` は ``--no-verify`` が config
#   **値**の中にあるためフラグ走査に掛からない。展開結果を解釈する術が無い以上、
#   alias の定義そのものを deny する（誤検出 > 誤通過）。
#
# `is_sensitive_config_key` は `git config` / ``-c`` の key だけでなく、`config_protection` が
# config ファイルの本文を解析して得た key の判定にも使う（key 集合を 1 本にするため）。
_SENSITIVE_CONFIG_KEY_PREFIXES = (
    _HOOKS_PATH_CONFIG_KEY,
    "include.path",
    "includeif.",
    "alias.",
)


class GitInvocation(NamedTuple):
    """git 起動トークン以降を解析した結果。

    Attributes:
        subcommand: 解決できたサブコマンド名。見つからなければ None。
        flags: 検出したフラグ名のリスト（オプションの値トークンは含まない）。
        subcommand_certain: サブコマンドの解決結果を信用してよいか。未知の
            グローバルオプションが値を取る可能性で解決位置がずれ得る場合は
            False になり、``-n`` 判定を fail-closed に倒す材料になります。
        config_values: ``-c`` / ``--config-env`` に渡された値文字列
            （``key=value`` 形式）のリスト。``core.hooksPath`` オーバーライド
            検出に使います。
        subcommand_args: サブコマンド確定後に現れた非オプショントークン
            （値トークンとして消費されたものは含まない）。``git config
            core.hooksPath <path>`` のような位置引数形式の検出に使う。
    """

    subcommand: str | None
    flags: list[str]
    subcommand_certain: bool
    config_values: list[str]
    subcommand_args: list[str]


def is_git_invocation(token: str) -> bool:
    """トークンが git 実行ファイルの起動かどうかを判定する。

    ``/usr/bin/git``・``git.exe``・``GIT`` を拾い、``gitk`` / ``GIT_DIR=.git`` / ``git/`` は
    一致しない。正規化は兄弟フックと共有する ``hook_common.is_git_executable_token`` に
    委譲する。

    Args:
        token: 検査対象のトークン。

    Returns:
        git 起動トークンなら True。

    Raises:
        例外は発生しません。
    """
    return is_git_executable_token(token)


def _consume_short_cluster(token: str, flags: list[str]) -> tuple[int, bool, str | None]:
    """short オプションのクラスタを 1 文字ずつフラグへ展開する。

    ``-am`` のように複数の short オプションが結合したトークンを扱います。
    値を取る文字に到達した時点で以降は値なので走査を打ち切ります。

    Args:
        token: ``-`` で始まる（``--`` ではない）short オプショントークン。
        flags: 検出したフラグを追記するリスト。

    Returns:
        (値として追加で消費すべき後続トークン数（0 または 1）,
        git グローバルオプションとして未知の文字を含むか,
        ``-c`` が結合形（``-ccore.hooksPath=x``）で値を持つ場合はその値文字列、
        それ以外は None) の 3 要素タプル。値が次トークンにある場合
        （非結合形の ``-c``）は None を返し、呼び出し側が
        ``flags[-1] == "-c"`` を見て次トークンを取得します。

    Raises:
        例外は発生しません。
    """
    last_index = len(token) - 1
    unknown = False
    for index, char in enumerate(token[1:], start=1):
        flags.append(f"-{char}")
        if char in _OPTIONAL_VALUE_SHORT_OPTIONS:
            if index < last_index:
                return 0, unknown, None
            continue
        if char in _VALUE_SHORT_OPTIONS:
            if index < last_index:
                config_value = token[index + 1 :] if char == "c" else None
                return 0, unknown, config_value
            return 1, unknown, None
        if char not in _BOOLEAN_GLOBAL_SHORT_OPTIONS:
            unknown = True
    return 0, unknown, None


def _consume_long_option(
    token: str, next_token: str | None, flags: list[str], config_values: list[str]
) -> tuple[int, bool]:
    """``--`` で始まる long オプション 1 トークンをフラグへ記録し、値の消費と未知判定を返す。

    ``--name=value`` の結合形は次トークンを消費しません。値を別トークンで取る long オプション
    （`_VALUE_LONG_OPTIONS`）は次トークンを値として 1 つ消費します。``--config-env`` の値は
    結合形・分離形のどちらでも ``config_values`` へ集約します。

    Args:
        token: ``--`` で始まるトークン（``--`` 単独ではない）。
        next_token: 直後のトークン。無ければ None。
        flags: 検出したフラグを追記するリスト。
        config_values: ``--config-env`` の値を追記するリスト。

    Returns:
        (値として追加で消費すべき後続トークン数（0 または 1）,
        値を取るか判定できない未知の long オプションか) の 2 要素タプル。

    Raises:
        例外は発生しません。
    """
    name, has_value, inline_value = token.partition("=")
    flags.append(name)
    if has_value:
        if name == "--config-env":
            config_values.append(inline_value)
        return 0, False
    if name in _VALUE_LONG_OPTIONS:
        if name == "--config-env" and next_token is not None:
            config_values.append(next_token)
        return 1, False
    return 0, name not in _BOOLEAN_GLOBAL_LONG_OPTIONS


def _consume_short_option(
    token: str, next_token: str | None, flags: list[str], config_values: list[str]
) -> tuple[int, bool]:
    """``-`` で始まる short オプション 1 トークンをフラグへ記録し、値の消費と未知判定を返す。

    結合形の ``-c`` の値（``-ccore.hooksPath=x``）と、非結合形の ``-c`` が次トークンに持つ値を、
    ともに ``config_values`` へ集約します。

    Args:
        token: ``-`` で始まる（``--`` ではなく、``-`` 単独でもない）トークン。
        next_token: 直後のトークン。無ければ None。
        flags: 検出したフラグを追記するリスト。
        config_values: ``-c`` の値を追記するリスト。

    Returns:
        (値として追加で消費すべき後続トークン数（0 または 1）,
        git グローバルオプションとして未知の文字を含むか) の 2 要素タプル。

    Raises:
        例外は発生しません。
    """
    consumed, unknown, config_value = _consume_short_cluster(token, flags)
    if config_value is None and consumed == 1 and flags[-1] == "-c":
        config_value = next_token
    if config_value is not None:
        config_values.append(config_value)
    return consumed, unknown


def parse_git_segment(segment: list[str]) -> GitInvocation:
    """``git`` で始まるトークン列からサブコマンドとフラグ一覧を取り出す。

    ``git`` の後ろに任意個現れるグローバルオプション（値を取るものを含む）を
    読み飛ばし、最初の非オプション語をサブコマンドとみなします。オプションの
    値トークンはフラグ一覧に含めません（``git commit -m "-n"`` のような
    値をフラグと誤認しないため）。``--`` 以降は pathspec とみなし走査を
    打ち切ります。

    サブコマンド解決前に既知でないグローバルオプションが現れた場合、それが
    値を取るかどうかを判定できずサブコマンド位置がずれるため
    ``subcommand_certain=False`` を返します。``-c`` / ``--config-env`` に
    渡された値は `GitInvocation.config_values` に集約します
    （``core.hooksPath`` オーバーライド検出用）。サブコマンド
    確定後に現れた非オプショントークンは `GitInvocation.subcommand_args`
    に集約します（``git config core.hooksPath <path>`` の位置引数形式検出用）。

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
    config_values: list[str] = []
    subcommand_args: list[str] = []
    certain = True
    index = 1
    while index < len(segment):
        token = segment[index]
        index += 1
        next_token = segment[index] if index < len(segment) else None
        if token == "--":
            break
        if token.startswith("--"):
            consumed, unknown = _consume_long_option(token, next_token, flags, config_values)
        elif token.startswith("-") and len(token) > 1:
            consumed, unknown = _consume_short_option(token, next_token, flags, config_values)
        else:
            consumed, unknown = 0, False
            if subcommand is None:
                subcommand = token
            else:
                subcommand_args.append(token)
        index += consumed
        if unknown and subcommand is None:
            certain = False
    return GitInvocation(
        subcommand=subcommand,
        flags=flags,
        subcommand_certain=certain,
        config_values=config_values,
        subcommand_args=subcommand_args,
    )


def _is_no_verify_flag(flag: str) -> bool:
    """フラグが ``--no-verify``（git が受理する短縮形を含む）かを判定する。

    Args:
        flag: ``--`` で始まるフラグ名（``=`` の左側）。

    Returns:
        ``--no-verify`` の前置で ``--n`` 以上の長さがあれば True。

    Raises:
        例外は発生しません。
    """
    return (
        len(flag) >= _MIN_ABBREVIATED_LONG_OPTION_LENGTH
        and len(flag) <= len(_BYPASS_LONG_FLAG)
        and _BYPASS_LONG_FLAG.startswith(flag)
    )


def is_sensitive_config_key(key: str) -> bool:
    """config key がフック実行を左右しうるものかを判定する。

    `config_protection` が config ファイルの本文から得た完全修飾 key（``section.sub.key``）にも
    使うため公開名にしている。

    Args:
        key: config key 名（大小文字不問）。

    Returns:
        `_SENSITIVE_CONFIG_KEY_PREFIXES` のいずれかに前方一致すれば True。

    Raises:
        例外は発生しません。
    """
    normalized = key.strip().lower()
    return any(normalized.startswith(prefix) for prefix in _SENSITIVE_CONFIG_KEY_PREFIXES)


_ENV_ASSIGNMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)

# GIT_CONFIG_KEY_<n> の index 部分を取り出す正規表現。
_GIT_CONFIG_KEY_INDEX_RE = re.compile(r"^GIT_CONFIG_KEY_\d+$")

# git 自身が config を注入・差し替えるために読む literal 環境変数名。値を解釈しても
# 安全側の判定を追加できないため、出現した時点で deny する。``GIT_CONFIG_GLOBAL`` /
# ``GIT_CONFIG_SYSTEM`` は差し替え先で ``core.hooksPath`` を書け、``GIT_CONFIG_NOSYSTEM``
# は system 側に置かれた保護設定を落とせる。
_GIT_CONFIG_INJECTION_ENV_NAMES = frozenset(
    {
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_NOSYSTEM",
    }
)


def _is_literal_env_hooks_path_override(prefix_tokens: list[str]) -> bool:
    """git 起動前の literal 環境変数代入に ``core.hooksPath`` オーバーライドがあるか判定する。

    **前置トークンを全部走査する。** 既知のラッパ（`sudo` / `env` 等）とそのオプション
    だけを読み飛ばす形にすると、``env -C /tmp GIT_CONFIG_GLOBAL=/tmp/e git commit`` の
    ように列挙が漏れた瞬間に素通り側へ倒れる。git 起動より前の ``NAME=value`` は実質
    すべて環境変数代入であり、危険な名前に一致したときだけ deny するので拾いすぎても
    実害は無い（git のオプション ``--author=Foo`` は識別子で始まらず一致しない）。

    ``GIT_CONFIG_KEY_<n>`` は key が機微なら ``GIT_CONFIG_COUNT`` / ``GIT_CONFIG_VALUE_<n>``
    との整合を問わず deny する（不完全・矛盾する triplet を安全とみなさない）。同名の
    代入が複数あれば後のものを採る。

    Args:
        prefix_tokens: セグメント内で git 起動トークンより前にあるトークン列。

    Returns:
        ``core.hooksPath`` を上書きしうる literal 代入があれば True。

    Raises:
        例外は発生しません。
    """
    assignments = dict(match.groups() for token in prefix_tokens if (match := _ENV_ASSIGNMENT_RE.match(token)))
    if _GIT_CONFIG_INJECTION_ENV_NAMES & assignments.keys():
        return True
    return any(
        _GIT_CONFIG_KEY_INDEX_RE.match(name) and is_sensitive_config_key(value) for name, value in assignments.items()
    )


# `git config` の read-only であることが確定しているフラグ。
_GIT_CONFIG_READ_ONLY_FLAGS = frozenset({"--get", "--get-all", "--get-regexp", "--list", "--show-origin"})

# `git config` の書込み・削除であることが確定しているフラグ。
_GIT_CONFIG_WRITE_FLAGS = frozenset({"--add", "--replace-all", "--unset", "--unset-all"})

# git 2.46+ の `git config <op> <key>` 新構文。
_GIT_CONFIG_NEW_READ_OPS = frozenset({"get", "list"})
_GIT_CONFIG_NEW_WRITE_OPS = frozenset({"set", "unset", "add"})


def _is_config_sensitive_key_mutation(invocation: GitInvocation) -> bool:
    """``git config`` 呼び出しが機微な config key への書込み・削除操作かを判定する。

    ``git config core.hooksPath <path>`` のような通常の subcommand 呼び出しを検査する。
    読み取り専用と確定できる形（``--get`` 系、新構文の ``get``/``list``、値を伴わない
    legacy query）だけを allow し、それ以外は deny する（誤検出 > 誤通過）。

    Args:
        invocation: git 起動トークン以降の解析結果。

    Returns:
        書込み・削除・曖昧な操作なら True。

    Raises:
        例外は発生しません。
    """
    if invocation.subcommand != "config":
        return False

    args = invocation.subcommand_args
    new_op: str | None = None
    if args and args[0] in _GIT_CONFIG_NEW_READ_OPS | _GIT_CONFIG_NEW_WRITE_OPS:
        new_op, args = args[0], args[1:]

    if not any(is_sensitive_config_key(arg) for arg in args):
        return False

    if new_op is not None:
        return new_op in _GIT_CONFIG_NEW_WRITE_OPS

    flags = invocation.flags
    if any(flag in flags for flag in _GIT_CONFIG_READ_ONLY_FLAGS):
        return False
    if any(flag in flags for flag in _GIT_CONFIG_WRITE_FLAGS):
        return True

    # legacy syntax: `git config core.hooksPath` （値なし = query）は allow、
    # `git config core.hooksPath <value>` （値あり = set）は deny。
    return len(args) >= 2


def _is_bypass_invocation(invocation: GitInvocation) -> bool:
    """解析済み git 起動がフックバイパスかを判定する。

    Args:
        invocation: git 起動トークン以降の解析結果。

    Returns:
        ``--no-verify``、``core.hooksPath`` オーバーライド（``-c``/
        ``--config-env`` 経由、または ``git config`` の書込み操作経由）、
        ``git commit`` の ``-n``、またはサブコマンド未確定時の ``-n`` なら
        True。

    Raises:
        例外は発生しません。
    """
    if any(_is_no_verify_flag(flag) for flag in invocation.flags):
        return True
    if any(is_sensitive_config_key(value.split("=", 1)[0]) for value in invocation.config_values):
        return True
    if _is_config_sensitive_key_mutation(invocation):
        return True
    if _BYPASS_SHORT_FLAG not in invocation.flags:
        return False
    return invocation.subcommand == "commit" or not invocation.subcommand_certain


def has_bypass_flag(command: str, *, _recursed: bool = False) -> bool:
    """コマンド文字列に git フックバイパスフラグが含まれるかを判定する。

    全トークンから git 起動トークンを探し、見つかった位置以降を `parse_git_segment` で
    解析する。既知シェルの ``-c`` に渡された文字列引数へは 1 段だけ再帰する。

    Args:
        command: 検査対象のシェルコマンド文字列。
        _recursed: 内部再帰用フラグ。True の場合、これ以上 ``sh -c`` へは
            再帰しません（2 段以上のネストは非目標）。

    Returns:
        ``--no-verify``（サブコマンド不問）、``core.hooksPath`` オーバーライド
        （``-c``/``--config-env``、``git config`` の書込み操作、または
        literal ``GIT_CONFIG_*`` 環境変数代入のいずれか経由）、``git commit``
        の ``-n``、またはサブコマンドを確定できない状態での ``-n`` を
        検出したら True。

    Raises:
        例外は発生しません。
    """
    # heredoc のデータ本文はトークン化の前に落とす。入口で正規化するので `sh -c` の
    # 再帰先にも同じ規則が効く。
    command = strip_data_heredoc_bodies(command)
    return any(_has_bypass_flag_in_dialect(variant, _recursed=_recursed) for variant in command_dialect_variants(command))


# `shlex` が解析できなかった入力にだけ当てる、生文字列のバイパス兆候。`hooksPath` は
# git の config key が大小無視なので IGNORECASE。
_UNPARSED_RAW_BYPASS_RE = re.compile(r"--no-verify|hookspath", re.IGNORECASE)


def _has_bypass_flag_in_dialect(command: str, *, _recursed: bool) -> bool:
    """1 つのシェル方言の読み方で `has_bypass_flag` の判定を行う。

    Args:
        command: `command_dialect_variants` が返した 1 通りの読み方。
        _recursed: ラッパー再帰済みかどうか。

    Returns:
        バイパスフラグを検出したら True。

    Raises:
        例外は発生しません。
    """
    tokens, parsed_cleanly = tokenize_with_status(command)
    if not parsed_cleanly and _UNPARSED_RAW_BYPASS_RE.search(command):
        return True
    for segment in split_segments(tokens):
        for index, token in enumerate(segment):
            if not is_git_invocation(token):
                continue
            if _is_bypass_invocation(parse_git_segment(segment[index:])):
                return True
            if _is_literal_env_hooks_path_override(segment[:index]):
                return True
        if _recursed:
            continue
        wrapper_command = extract_shell_wrapper_command(segment)
        # `sh -c '<巨大なコマンド>'` は外側が数トークンしか無く main() の予算検査を通過する。
        # 再帰先を無予算で走らせると timeout で host に kill され silent fail-open になる
        # ため、境界でも同じ上限を当て、超過は BLOCK へ倒す。
        if wrapper_command is not None and (
            command_exceeds_scan_budget(wrapper_command) or has_bypass_flag(wrapper_command, _recursed=True)
        ):
            return True
    return False


_TOKEN_BUDGET_MESSAGE = (
    f"[Hook] BLOCKED: command exceeds {MAX_COMMAND_TOKENS} shell tokens for pre:block-no-verify. "
    "A command this large cannot be inspected within the hook timeout, and an un-inspected "
    "command must not be allowed. Split it into smaller commands."
)

_TRUNCATED_INPUT_MESSAGE = (
    f"[Hook] BLOCKED: input exceeded {MAX_STDIN_BYTES} bytes for pre:block-no-verify. "
    "Refusing to evaluate a possibly-truncated bash command for hook bypass flags. "
    "Retry with a smaller command."
)

_UNPARSEABLE_INPUT_MESSAGE = (
    "[Hook] BLOCKED: could not parse hook input for pre:block-no-verify. "
    "A non-empty payload that fails to parse as JSON cannot be checked for "
    "git hook bypass flags. Refusing rather than allowing an unverifiable "
    "command through."
)

_MISSING_FIELDS_MESSAGE = (
    "[Hook] BLOCKED: hook input for pre:block-no-verify has no recognizable "
    f"tool_input ({'|'.join(INPUT_CONTAINER_KEYS)}). Cannot verify the bash "
    "command is free of git hook bypass flags."
)


def main() -> int:
    """git コマンドでフックバイパスフラグの使用をブロックする。

    入力が壊れている（切り捨て・不正 JSON・必須フィールド欠落）場合は
    判定不能として fail-closed でブロックする。空入力（tty・stdin 未接続）
    だけは非ブロッキングで許可する。

    Returns:
        終了コード（0: 許可、ブロック時は 2）

    Raises:
        例外は発生しません。
    """
    try:
        raw, truncated = read_raw_stdin_with_truncation()
    except StdinUnavailableError as exc:
        return emit_block_output(stdin_unreadable_message("pre:block-no-verify", exc))
    if truncated:
        return emit_block_output(_TRUNCATED_INPUT_MESSAGE)

    if not raw:
        return 0

    data = parse_json_object(raw)
    if data is None:
        return emit_block_output(_UNPARSEABLE_INPUT_MESSAGE)

    if extract_tool_input(data) is None:
        return emit_block_output(_MISSING_FIELDS_MESSAGE)

    # 先勝ちで 1 キーだけ見ると、複数のコンテナキーを持つ payload で無害な側だけを
    # 検査して素通りさせうるため、全キーを走査する。
    for command in iter_bash_commands(data):
        # 走査は git トークン数の二次。上限超過を走らせると timeout で host に kill され
        # silent fail-open になるため、検査しきれない入力は通さず止める。
        if command_exceeds_scan_budget(command):
            return emit_block_output(_TOKEN_BUDGET_MESSAGE)
        if has_bypass_flag(command):
            return emit_block_output("[Hook] BLOCKED: git hook bypass flags are not allowed")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
