"""git のフックバイパスフラグ（--no-verify / commit の -n）をブロックします。

トリガー: pre:bash
入力: bashコマンドを含むJSON
出力: ブロック時は host 非依存の合併ブロック出力（stderr の理由 + stdout の deny JSON）
終了: バイパス検出時は 2、それ以外は 0

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

long オプションの短縮形:
    git は曖昧でない限り long オプションの**前置**を受理します
    （``git commit --no-veri`` は実際に通る）。完全一致だけを見ると素通りする
    ため、``--no-verify`` の前置（``--n`` 以上の長さ）を同様にブロックします。

config 経由のフック無効化:
    ``--no-verify`` を使わずに git 自身のフック（`.git/hooks/pre-commit` 等）を
    無効化する経路をまとめてブロックします（A-07 / H-05 / H-06 とその拡張）。
    config key の大小文字は区別せず判定します（git の config key 名は大小文字を
    区別しないため）。

    - ``-c`` / ``--config-env`` / ``git config`` の書込みで
      ``core.hooksPath`` を差し替える
    - 同じ経路で ``include.path`` / ``includeIf.*`` を書き、取り込んだ先で
      ``core.hooksPath`` を書く
    - 同じ経路で ``alias.*`` を定義し、展開先に ``--no-verify`` を隠す
      （``git -c alias.ci='commit --no-verify' ci``）。展開結果を解釈する術が
      無いため、alias の定義そのものを deny します
    - ``GIT_CONFIG_PARAMETERS`` / ``GIT_CONFIG_GLOBAL`` / ``GIT_CONFIG_SYSTEM``
      / ``GIT_CONFIG_NOSYSTEM`` / ``GIT_CONFIG_KEY_<n>`` の literal 環境変数代入

``sh -c`` ラッパー（1 段のみ）:
    ``sh -c 'git commit --no-verify'`` のように既知シェル（``sh``/``bash``/
    ``zsh``/``dash``）の ``-c`` へ渡された文字列引数は、1 段だけ再帰的に
    同じ判定へ通します。2 段以上のネスト（``sh -c "sh -c 'git commit -n'"``）
    や ``eval``・任意ラッパースクリプトへは再帰しません（A-07 対応。深い
    再帰はコンテキスト消費と誤検知リスクの両方が増えるため、1 段に限定する
    設計判断です）。

非目標（原理的に検出不能、または意図的に対象外とするため検出しません）:
    - シェルエイリアス・シェル関数経由の呼び出し（``alias g=git`` の ``g``、
      ``commit() { git commit --no-verify; }`` の ``commit``）。コマンドラインに
      現れないため。config の ``alias.*`` は literal に現れるので対象内
    - ``git --git-dir=<other>`` / ``GIT_DIR=<other>``。別リポジトリを対象に
      するだけで、そのリポジトリのフックは通常どおり走るためバイパスではない
    - ``git $(echo commit) --no-verify`` のようなコマンド置換・変数展開
      （``$VAR`` / ``$(...)`` / バッククォート）を経由した組み立て
    - ``git`` 以外の名前を持つラッパースクリプト（``mygit`` / ``./deploy.sh``）
      の内部で行われる git 呼び出し
    - ``eval`` に渡す文字列の解釈、および ``sh -c`` の 2 段以上のネスト
    POSIX シェルのセマンティクス全体を模倣することは不可能であり、本フックは
    「うっかりバイパス」の抑止であって敵対的回避への防壁ではありません。
    git 自体が大文字小文字を区別するため、トークン比較も区別します
    （``core.hooksPath`` の config key を除く。git config key は大小文字を
    区別しないため）。
"""

from __future__ import annotations

import re
from typing import NamedTuple

from bluecore.hooks.hook_common import (
    MAX_STDIN_BYTES,
    emit_block_output,
    is_git_executable_token,
    parse_json_object,
    read_raw_stdin_with_truncation,
    split_segments,
    tokenize,
)
from bluecore.lib.harness import INPUT_CONTAINER_KEYS, extract_bash_command, extract_tool_input

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

# long オプションの短縮形を受け付ける最短長。git は曖昧でない限り long
# オプションの**前置**を受理するため（``git commit --no-veri`` は実際に通る）、
# 完全一致だけを見ると素通りする。``--n`` / ``--no`` は git 側では曖昧で
# エラーになるが、ここでは deny 側に倒す（ADR-0002: 誤検出 > 誤通過）。
_MIN_ABBREVIATED_LONG_OPTION_LENGTH = 3

# ``git commit`` でのみ ``--no-verify`` の別名になる short フラグ。
_BYPASS_SHORT_FLAG = "-n"

# ``-c`` / ``--config-env`` の値が指す config key。git の config key 名は
# 大小文字を区別しないため、比較は小文字化した上で行います（A-07 対応）。
_HOOKS_PATH_CONFIG_KEY = "core.hookspath"

# フック実行を左右しうる config key の前置。いずれも小文字で比較します。
#
# - ``core.hookspath``: フック置き場そのものの差し替え。
# - ``include.path`` / ``includeif.``: 任意の config ファイルを取り込める。
#   取り込んだ先で ``core.hooksPath`` を書けるため、直接指定と等価。
# - ``alias.``: 別名の展開先を本フックは知らない。``git -c
#   alias.ci='commit --no-verify' ci`` は ``--no-verify`` が config **値**の
#   中にあるためフラグ走査に掛からない。展開結果を解釈する術が無い以上、
#   alias の定義そのものを deny する（ADR-0002 の false-positive 優先）。
#
# シェルエイリアス（``alias g=git``）は依然として非目標だが、それは
# コマンドラインに現れないため。``-c alias.X=`` は literal に現れる。
_SENSITIVE_CONFIG_KEY_PREFIXES = (
    _HOOKS_PATH_CONFIG_KEY,
    "include.path",
    "includeif.",
    "alias.",
)

# ``sh -c`` 再帰の対象とする既知シェル実行ファイル（basename 判定）。
_SHELL_WRAPPER_EXECUTABLES = frozenset({"sh", "bash", "zsh", "dash"})


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
            core.hooksPath <path>`` のような位置引数形式の検出（H-06）に使う。
    """

    subcommand: str | None
    flags: list[str]
    subcommand_certain: bool
    config_values: list[str]
    subcommand_args: list[str]


def is_git_invocation(token: str) -> bool:
    """トークンが git 実行ファイルの起動かどうかを判定する。

    ``git`` そのものに加え ``/usr/bin/git`` のようなパス付き起動、
    ``git.exe``（Windows）、``GIT``（大文字。macOS 既定の APFS は大小文字を
    区別しないため ``GIT commit --no-verify`` は実 git を起動する。H-04
    対応）を拾う。``gitk`` / ``GIT_DIR=.git`` / ``git/``（ディレクトリ）は
    一致しない。判定は ``pre_bash_commit_quality`` と共有する
    ``hook_common.is_git_executable_token`` に委譲し、2 箇所で正規化が
    分岐する事態を防ぐ。

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
    （``core.hooksPath`` オーバーライド検出用、A-07 対応）。サブコマンド
    確定後に現れた非オプショントークンは `GitInvocation.subcommand_args`
    に集約します（``git config core.hooksPath <path>`` の位置引数形式検出用、
    H-06 対応）。

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
        if token == "--":
            break
        if token.startswith("--"):
            name = token.split("=", 1)[0]
            flags.append(name)
            if "=" not in token:
                if name in _VALUE_LONG_OPTIONS:
                    if name == "--config-env" and index < len(segment):
                        config_values.append(segment[index])
                    index += 1
                elif subcommand is None and name not in _BOOLEAN_GLOBAL_LONG_OPTIONS:
                    certain = False
            elif name == "--config-env":
                config_values.append(token.split("=", 1)[1])
            continue
        if token.startswith("-") and len(token) > 1:
            consumed, unknown, config_value = _consume_short_cluster(token, flags)
            if config_value is None and consumed == 1 and flags[-1] == "-c" and index < len(segment):
                config_value = segment[index]
            if config_value is not None:
                config_values.append(config_value)
            index += consumed
            if unknown and subcommand is None:
                certain = False
            continue
        if subcommand is None:
            subcommand = token
        else:
            subcommand_args.append(token)
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


def _is_sensitive_config_key(key: str) -> bool:
    """config key がフック実行を左右しうるものかを判定する。

    Args:
        key: config key 名（大小文字不問）。

    Returns:
        `_SENSITIVE_CONFIG_KEY_PREFIXES` のいずれかに前方一致すれば True。

    Raises:
        例外は発生しません。
    """
    normalized = key.strip().lower()
    return any(normalized.startswith(prefix) for prefix in _SENSITIVE_CONFIG_KEY_PREFIXES)


def _is_sensitive_config_override(config_values: list[str]) -> bool:
    """``-c``/``--config-env`` の値に機微な config key の上書きが含まれるかを判定する。

    Args:
        config_values: `GitInvocation.config_values`。

    Returns:
        フック実行を左右しうる key（大小文字不問）を上書きする値があれば True。

    Raises:
        例外は発生しません。
    """
    return any(_is_sensitive_config_key(value.split("=", 1)[0]) for value in config_values)


_ENV_ASSIGNMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)

# GIT_CONFIG_KEY_<n> の index 部分を取り出す正規表現（H-05）。
_GIT_CONFIG_KEY_INDEX_RE = re.compile(r"^GIT_CONFIG_KEY_\d+$")

# git 自身が config を注入・差し替えるために読む literal 環境変数名。値を
# shell-eval せず、出現した時点で deny する（中身を解釈しても安全側の判定を
# 追加できないため）。``GIT_CONFIG_PARAMETERS`` は H-05 で追加。
# ``GIT_CONFIG_GLOBAL`` / ``GIT_CONFIG_SYSTEM`` は config ファイルそのものを
# 差し替えられ、差し替え先で ``core.hooksPath`` を書けるため同じ経路。
# ``GIT_CONFIG_NOSYSTEM`` は system config を無効化するので、system 側に
# 置かれた保護設定を落とせる。
_GIT_CONFIG_INJECTION_ENV_NAMES = frozenset(
    {
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_NOSYSTEM",
    }
)

# ``env`` の直後で消費するオプション（値を取らないもの）。
_ENV_BOOLEAN_OPTIONS = frozenset({"-i", "--ignore-environment"})


def _collect_literal_env_assignments(prefix_tokens: list[str]) -> dict[str, str]:
    """git 起動トークンより前にある literal 環境変数代入を集める（H-05）。

    セグメント先頭から連続する ``NAME=value`` 代入と、続く literal ``env``
    コマンド（``-i`` / ``--ignore-environment`` / ``-u NAME`` を消費した後）
    の ``NAME=value`` 引数を対象にする。シェルエイリアス・変数展開・
    コマンド置換・``eval`` 経由の間接的な代入は対象外（ADR-0002 の非目標）。

    Args:
        prefix_tokens: セグメント内で git 起動トークンより前にあるトークン列。

    Returns:
        変数名から値への写像。代入が無ければ空 dict。

    Raises:
        例外は発生しません。
    """
    assignments: dict[str, str] = {}
    index = 0
    while index < len(prefix_tokens):
        match = _ENV_ASSIGNMENT_RE.match(prefix_tokens[index])
        if not match:
            break
        assignments[match.group(1)] = match.group(2)
        index += 1

    if index >= len(prefix_tokens) or prefix_tokens[index].rsplit("/", 1)[-1] != "env":
        return assignments

    index += 1
    while index < len(prefix_tokens):
        token = prefix_tokens[index]
        if token in _ENV_BOOLEAN_OPTIONS:
            index += 1
            continue
        if token == "-u":
            index += 2
            continue
        if token.startswith("-u") and len(token) > 2:
            index += 1
            continue
        match = _ENV_ASSIGNMENT_RE.match(token)
        if not match:
            break
        assignments[match.group(1)] = match.group(2)
        index += 1
    return assignments


def _is_literal_env_hooks_path_override(prefix_tokens: list[str]) -> bool:
    """git 起動前の literal 環境変数代入に ``core.hooksPath`` オーバーライドがあるか判定する（H-05）。

    ``GIT_CONFIG_COUNT`` / ``GIT_CONFIG_KEY_<n>`` / ``GIT_CONFIG_VALUE_<n>`` は
    index ごとに検証し、key が ``core.hooksPath``（大小文字不問）なら value
    の有無や ``GIT_CONFIG_COUNT`` との整合性を問わず deny する（不完全・
    矛盾する triplet を安全とみなさない）。``GIT_CONFIG_PARAMETERS`` は git
    自身が使う config 注入 literal であり、値を解釈せず出現時点で deny する。

    Args:
        prefix_tokens: セグメント内で git 起動トークンより前にあるトークン列。

    Returns:
        ``core.hooksPath`` を上書きしうる literal 代入があれば True。

    Raises:
        例外は発生しません。
    """
    assignments = _collect_literal_env_assignments(prefix_tokens)
    if _GIT_CONFIG_INJECTION_ENV_NAMES & assignments.keys():
        return True
    for name, value in assignments.items():
        if _GIT_CONFIG_KEY_INDEX_RE.match(name) and _is_sensitive_config_key(value):
            return True
    return False


# `git config` の read-only であることが確定しているフラグ（H-06）。
_GIT_CONFIG_READ_ONLY_FLAGS = frozenset({"--get", "--get-all", "--get-regexp", "--list", "--show-origin"})

# `git config` の書込み・削除であることが確定しているフラグ（H-06）。
_GIT_CONFIG_WRITE_FLAGS = frozenset({"--add", "--replace-all", "--unset", "--unset-all"})

# git 2.46+ の `git config <op> <key>` 新構文（H-06）。
_GIT_CONFIG_NEW_READ_OPS = frozenset({"get", "list"})
_GIT_CONFIG_NEW_WRITE_OPS = frozenset({"set", "unset", "add"})


def _config_args_touch_sensitive_key(args: list[str]) -> bool:
    """`git config` の位置引数に機微な config key（大小文字不問）が含まれるか判定する。

    Args:
        args: `GitInvocation.subcommand_args`（新構文の op トークンを除いたもの）。

    Returns:
        含まれていれば True。

    Raises:
        例外は発生しません。
    """
    return any(_is_sensitive_config_key(arg) for arg in args)


def _is_config_sensitive_key_mutation(invocation: GitInvocation) -> bool:
    """``git config`` 呼び出しが機微な config key への書込み・削除操作かを判定する（H-06）。

    ``-c`` / ``--config-env`` によるオーバーライド（`_is_sensitive_config_override`）
    とは別に、``git config core.hooksPath <path>`` のような通常の subcommand
    呼び出しを検査する。読み取り専用と確定できる形（``--get`` 系、新構文の
    ``get``/``list``、値を伴わない legacy query）だけを allow し、それ以外の
    ``core.hooksPath`` に触れる呼び出しは deny する（ADR-0002 の
    false-positive 優先方針）。

    Args:
        invocation: git 起動トークン以降の解析結果。

    Returns:
        書込み・削除・曖昧な操作なら True。

    Raises:
        例外は発生しません。
    """
    if invocation.subcommand != "config":
        return False

    args = list(invocation.subcommand_args)
    new_op: str | None = None
    if args and args[0] in _GIT_CONFIG_NEW_READ_OPS | _GIT_CONFIG_NEW_WRITE_OPS:
        new_op, args = args[0], args[1:]

    if not _config_args_touch_sensitive_key(args):
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
    if _is_sensitive_config_override(invocation.config_values):
        return True
    if _is_config_sensitive_key_mutation(invocation):
        return True
    if _BYPASS_SHORT_FLAG not in invocation.flags:
        return False
    return invocation.subcommand == "commit" or not invocation.subcommand_certain


def _extract_shell_wrapper_command(segment: list[str]) -> str | None:
    """セグメント内の既知シェル ``-c`` 呼び出しから、ラップされた文字列コマンドを取り出す。

    ``sh -c 'git commit --no-verify'`` のように basename が
    `_SHELL_WRAPPER_EXECUTABLES` のいずれかであるトークンを探し、続く
    トークンに ``-c`` があれば、その次のトークン（シェルへ渡す文字列
    コマンド）を返します（A-07 対応。1 段の再帰にのみ使う）。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。

    Returns:
        ラップされた文字列コマンド。見つからなければ None。

    Raises:
        例外は発生しません。
    """
    for i, token in enumerate(segment):
        basename = token.rsplit("/", 1)[-1]
        if basename not in _SHELL_WRAPPER_EXECUTABLES:
            continue
        for offset, tok in enumerate(segment[i + 1 :]):
            if tok != "-c":
                continue
            remaining = segment[i + 1 + offset + 1 :]
            return remaining[0] if remaining else None
    return None


def has_bypass_flag(command: str, *, _recursed: bool = False) -> bool:
    """コマンド文字列に git フックバイパスフラグが含まれるかを判定する。

    セグメント先頭に限らず全トークンから git 起動トークンを探し、見つかった
    位置以降を `parse_git_segment` で解析します（``sudo git commit -n`` や
    ``/usr/bin/git commit --no-verify`` を捕捉するため）。1 セグメントに
    複数の git 起動があれば全て検査します。加えて、``sh``/``bash``/``zsh``/
    ``dash`` の ``-c`` に渡された文字列引数へ 1 段だけ再帰します
    （``sh -c 'git commit --no-verify'`` を検出するため、A-07 対応。
    ``_recursed`` は内部再帰用の引数で外部から指定しません）。

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
    for segment in split_segments(tokenize(command)):
        for index, token in enumerate(segment):
            if not is_git_invocation(token):
                continue
            if _is_bypass_invocation(parse_git_segment(segment[index:])):
                return True
            if _is_literal_env_hooks_path_override(segment[:index]):
                return True
        if not _recursed:
            wrapper_command = _extract_shell_wrapper_command(segment)
            if wrapper_command is not None and has_bypass_flag(wrapper_command, _recursed=True):
                return True
    return False


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
    だけは従来通り非ブロッキングで許可する（F-01 対応）。

    Args:
        引数はありません（標準入力から読み取る）。

    Returns:
        終了コード（0: 許可、ブロック時は 2）

    Raises:
        例外は発生しません。
    """
    raw, truncated = read_raw_stdin_with_truncation()
    if truncated:
        return emit_block_output(_TRUNCATED_INPUT_MESSAGE)

    if not raw:
        return 0

    data = parse_json_object(raw)
    if data is None:
        return emit_block_output(_UNPARSEABLE_INPUT_MESSAGE)

    if extract_tool_input(data) is None:
        return emit_block_output(_MISSING_FIELDS_MESSAGE)

    # コンテナキーは全て走査する。先勝ちで 1 キーだけ見ると、payload が
    # 複数のコンテナキーを持つ host で無害な側だけを検査して素通りさせうる。
    # ADR-0002 は本フックの検出境界を「誤検出を誤通過より選ぶ」と定めており、
    # config_protection は既に全キー走査（_block_reason_for_container）。単一情報源
    # 化したのは定数だけで、走査の意味論が片側だけ緩いままだった。
    for key in INPUT_CONTAINER_KEYS:
        if key not in data:
            continue
        if has_bypass_flag(extract_bash_command({key: data[key]})):
            return emit_block_output("[Hook] BLOCKED: git hook bypass flags are not allowed")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
