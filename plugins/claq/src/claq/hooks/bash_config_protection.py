"""Bash 経由の重要な設定ファイル直接書き換えを保護します。

トリガー: pre:bash
入力: bashコマンドを含むJSON
出力: 保護対象ファイルへの書き込みが検出された場合は host 非依存の合併出力でブロック
終了: 0 (許可) または 2 (ブロック)

`config_protection` は Edit/Write/MultiEdit 系 matcher にしか登録されておらず、
`printf x > pyproject.toml` のような Bash 経由の直接書き換えを検査しない。本モジュールは
同じ保護対象定義（`config_protection` から import する、大小無視で畳んだ basename 集合と
ディレクトリ単位の `protected_path_segment`）を Bash コマンド文字列に適用する。畳んだ側を
import するのは、Write を deny する綴りと Bash を deny する綴りを必ず一致させるため。

`config_protection` を Bash matcher に相乗りさせない理由:
    `config_protection.main()` の fail 姿勢（malformed JSON deny / truncated deny）は
    「matcher が書込み系ツールに限定されている」ことを根拠に較正されている。最も広い
    Bash matcher に入れると、設定ファイルと無関係な malformed/truncated の Bash 呼び出しまで
    deny して可用性が壊れるため、独立した hooks.json エントリとして判定ロジックだけを分ける。

判定方式:
    データ heredoc の本文を落としてから（docs/adr/shell-analysis-boundary.md）トークン化して
    セグメントに分け、保護対象（basename 一致、または `.git/hooks/` のような保護 path 配下・
    保護 path 自身）が**実行位置のコマンドの書き込み先トークンとして現れた場合のみ** deny する:
        - `>` / `>>` / `&>` / `>|` リダイレクト先
        - `tee` と PowerShell の書き込み系 cmdlet の出力先引数
        - `sed` / `perl` / `ruby` の in-place 編集の対象引数（フラグ語彙は
          `hook_common.is_inplace_edit_flag`）
        - `ed`/`red`/`ex`/`sponge`（`hook_common.ALWAYS_MUTATING_EDIT_EXECUTABLES`）の引数
        - `touch` の引数、削除・移動・権限変更コマンドの引数
        - `cp`/`mv`/`install` の最終引数、`ln` の最終引数、`dd of=<path>`
    git config（``.git/config``・``.git/config.worktree``・``.git/worktrees/*/config``、および
    ホーム側の ``~/.gitconfig`` / ``$XDG_CONFIG_HOME/git/config``）は `config_protection` では置換後の
    全文の前後比較で判定するが、Bash の書込み本文は解釈できないため、書き込み先位置に現れた時点で
    内容を問わず deny する（``git config <key> <value>`` は書き込み先トークンを持たないので影響を
    受けない）。ホーム側の設定はリポジトリ外の固定位置なので repo スコープ判定の前に deny する。
    書き込み先ヒットがあった場合のみリポジトリルートを解決し、書き込み先を cwd 基準で
    解決して**そのルート配下にある場合のみ** deny する（basename だけで判定すると別
    リポジトリの同名ファイルを誤検出する）。**リポジトリルートが決定できない場合は allow**。

    repo スコープ判定はシェル展開を経ていない生トークンに対して行うため、書き込み先に
    未展開の変数が含まれる場合（``printf x > $TMP/pyproject.toml``）は実際の書き込み先が
    リポジトリ外でも deny になる。変数を含むパスを「解決不能だから allow」に倒すと
    ``X=. ; printf y > $X/pyproject.toml`` が確実なバイパスになるため、この誤検出は意図した
    姿勢である。
    JSON が壊れている場合だけ、生テキストに保護対象 basename と書き込み指示が両方見えると
    きに deny する（repo スコープ判定・保護 path 判定は適用されない）。

非目標: `python -c`/`eval`/任意スクリプト経由の間接書き込み、`$(...)`・変数展開・
    パイプ越しの間接書き込み、シェルエイリアス・ラッパースクリプト経由の呼び出し。
    うっかり書き換えの抑止であって敵対的回避への防壁ではない
    （docs/adr/shell-analysis-boundary.md）。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from claq.hooks.config_protection import (
    CONDITIONALLY_PROTECTED_FILES_FOLDED,
    GIT_CONFIG_DISPLAY_NAMES,
    PROTECTED_FILES_FOLDED,
    blocked_message_for_file,
    protected_path_segment,
    repo_git_config_path,
)
from claq.hooks.git_config_text import (
    GLOBAL_GITCONFIG_DISPLAY_NAME,
    GLOBAL_GITCONFIG_NAME,
    global_git_config_path,
)
from claq.hooks.hook_common import (
    ALWAYS_MUTATING_EDIT_EXECUTABLES,
    basename,
    emit_block_output,
    is_inplace_edit_flag,
    normalize_executable_name,
    normalize_protected_name,
    resolve_effective_target,
    resolve_repo_root,
)
from claq.hooks.host_guard_targets import find_host_guard_match, match_folded_path_window
from claq.hooks.shell_tokens import (
    MAX_COMMAND_TOKENS,
    command_dialect_variants,
    command_exceeds_scan_budget,
    extract_shell_wrapper_command,
    split_segments,
    strip_data_heredoc_bodies,
    tokenize,
)
from claq.hooks.stdin_io import (
    MAX_STDIN_BYTES,
    StdinUnavailableError,
    parse_json_object,
    read_raw_stdin_with_truncation,
    stdin_unreadable_message,
)
from claq.lib.harness import (
    extract_raw_tool_name,
    is_unidentifiable_tool,
    iter_bash_commands,
    normalize_tool_name,
)

# matcher（hooks.json）の綴りがズレても、本体側で対象外ツールを早期 return する多重防御。
_BASH_TOOL_NAMES = frozenset({"bash"})

# リダイレクト演算子。``1>``/``2>`` 等は数字が別トークンに分かれ ``>``/``>>`` に一致する
# ため不要だが、意図の記録として明示している。``&>>`` は stdout+stderr の追記、``>&`` は
# ``&>`` の別綴り（``>&file``）で、どちらも `tokenize` が 1 トークンにする。``>&2`` / ``2>&1`` の
# ような fd 複製も同じ ``>&`` だが、後続が保護名でなければ書き込み先として一致せず allow になる。
# ``<>`` は読み書きオープンで、対象が無ければ作成する（保護名の空ファイル作成と同じ効果）。
_REDIRECT_OPERATORS = frozenset({">", ">>", "&>", "&>>", ">&", ">|", "<>", "1>", "2>", "1>>", "2>>"})

# 末尾引数が書き込み先になるコマンド。Windows のシェルツールは PowerShell なので、
# 別名だけでは拾えない長形式 cmdlet を併記する。
_LAST_ARG_WRITE_COMMANDS = frozenset(
    {"cp", "mv", "install", "copy-item", "move-item", "copy", "move", "cpi", "mi"}
)

# ファイルを消す／空にするコマンド群。リンタ設定を消せばルールごと無効化できるため、
# 上書きと同じ強さの弱体化として扱う。`mv` は移動元も対象にする（実質削除）。`cp` の
# 複製元は元ファイルが残るため対象にしない。
_REMOVE_COMMANDS = frozenset(
    {
        "rm",
        "unlink",
        "shred",
        "truncate",
        "mv",
        # PowerShell / cmd の長形式と別名（`del` / `erase` / `rd` は cmd 組み込み、
        # `ri` / `mi` / `clc` は PowerShell の既定エイリアス）。
        "remove-item",
        "move-item",
        "clear-content",
        "del",
        "erase",
        "rd",
        "ri",
        "mi",
        "clc",
    }
)

# 中身を変えずに検査を無効化できるコマンド群（`chmod -x .git/hooks/pre-commit` /
# `chmod 000 .eslintrc.json`）。モード文字列とパスを区別せず、保護対象に一致した
# トークンがあれば deny する（誤検出 > 誤通過）。
_MODE_COMMANDS = frozenset({"chmod", "chown", "chgrp", "chflags"})

# 非オプション引数がすべて書き込み先になるコマンド。`tee` と、同じ形をとる
# PowerShell の書き込み系 cmdlet。
_TEE_COMMANDS = frozenset(
    {"tee", "set-content", "add-content", "out-file", "new-item", "sc", "ac", "ni"}
)
# in-place 編集で**フラグを伴うときだけ**書き込みになるコマンド。フラグ無しでも書き込む
# エディタは `ALWAYS_MUTATING_EDIT_EXECUTABLES` の側。`ruby -pi` は `perl -pi` と同じ
# in-place 編集。`python3 -c` のような任意インタプリタは非目標で、ここには入れない。
_INPLACE_EDIT_COMMANDS = frozenset({"sed", "perl", "ruby"})
# `-f` の有無で区別しない。保護対象がまだ無い状態での `ln -s weak.toml ruff.toml` も、
# その名前を読む処理をリンク先の内容へ差し替える実質的な書き込みである。
_LN_COMMANDS = frozenset({"ln"})
_DD_COMMANDS = frozenset({"dd"})

# 保護対象の名前を空で新規作成するだけで検査を無効化できる（多くの linter は設定
# ファイルを見つけた時点で上位ディレクトリの探索を打ち切る）。値を取るオプション
# （``touch -r ruff.toml app.py``）の値も候補に混じるが、BSD / GNU / busybox で割れる
# arity 表を持つより誤検出側を選ぶ。``New-Item`` は `_TEE_COMMANDS` 側にある。
_TOUCH_COMMANDS = frozenset({"touch"})

# カレントディレクトリを移動するコマンド。これらが現れたコマンドでは、cwd 基準の
# 相対パス解決が実行時の位置とずれるため repo スコープ判定を信用しない。
_DIRECTORY_CHANGE_COMMANDS = frozenset({"cd", "pushd", "popd", "chdir"})

# ディレクトリ移動の判定で、コマンド名より前に来うるシェルの予約語・グループ化と `builtin`。
# ``{ cd x; …}`` / ``if cd x; then …`` / ``builtin cd x`` の ``cd`` を見落とすと、cwd 基準の解決を
# 信用したまま相対パスの書込み先を判定してしまう。
_LEADING_SHELL_KEYWORDS = frozenset({"{", "(", "!", "if", "then", "elif", "else", "while", "until", "do", "builtin"})

_ALL_PROTECTED_BASENAMES = PROTECTED_FILES_FOLDED | CONDITIONALLY_PROTECTED_FILES_FOLDED

# ~/.claq/logs/ 配下（bg 失敗ログ）をディレクトリ構造で保護する。`detach.recent_bg_failure_notice`
# がログ末尾行を次回 SessionStart のコンテキストへ注入するため、自由な追記はプロンプト
# 注入の永続化経路になる。保護の範囲を「Bash からの直接追記の拒否」に限るため、
# `config_protection` の保護 path（Edit/Write にも効く）へは足さず本モジュールに閉じる。
_HOME_LOG_PATH_SEGMENTS = ((".claq", "logs"),)


# 未展開のまま ``~/.gitconfig`` 等を指せる、書込み先トークン先頭の ``$HOME`` / ``$XDG_CONFIG_HOME``（``{}`` 付き可）。
_HOME_VARIABLE_PREFIX_RE = re.compile(r"^\$(?:\{HOME\}|HOME)(?=/|$)")
_XDG_VARIABLE_PREFIX_RE = re.compile(r"^\$(?:\{XDG_CONFIG_HOME\}|XDG_CONFIG_HOME)(?=/|$)")


def _expand_config_home_variables(token: str) -> str:
    """書込み先トークン先頭の ``$HOME`` / ``$XDG_CONFIG_HOME`` を展開する。

    ``$HOME`` はチルダに揃える。``$XDG_CONFIG_HOME`` は設定時だけ値へ展開する。

    Args:
        token: 検査対象の生トークン。

    Returns:
        先頭の変数を展開したトークン。変数で始まらなければそのまま。

    Raises:
        例外は発生しません。
    """
    token = _HOME_VARIABLE_PREFIX_RE.sub("~", token)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return _XDG_VARIABLE_PREFIX_RE.sub(lambda _match: xdg, token) if xdg else token


def _find_home_log_directory_match(token: str) -> str | None:
    """トークンが ``~/.claq/logs/`` のディレクトリ構造に一致するか、その
    ディレクトリ自身を指すかを判定する。

    symlink を解決し、畳んだ名前（`normalize_protected_name`）で構造的に一致を見る。
    ``~/.claq/logs/`` はどのリポジトリでも同じ固定位置を指すため、呼び出し側は
    repo スコープ判定を適用せず即座に deny する。

    解決先が実際に home 配下かまでは検証しない。解決できないトークンや `cd` を挟んだ
    相対パスでは構造一致でしか判定できないため、リポジトリ内に偶然 ``.claq/logs`` が
    あると deny しうるが、悪用経路にならない誤検出側の広さなので許容する。

    Args:
        token: 検査対象の生トークン（パスの可能性がある）。``~`` は
            `resolve_effective_target` が内部で展開する。

    Returns:
        一致すれば ``"~/.claq/logs"``。該当しなければ None。

    Raises:
        例外は発生しません。
    """
    resolved = resolve_effective_target(token)
    parts = resolved.parts if resolved is not None else Path(token).parts
    segments = match_folded_path_window(parts, _HOME_LOG_PATH_SEGMENTS)
    return None if segments is None else "~/" + "/".join(segments)


# malformed JSON fallback で「破壊的操作の指示」とみなす部分文字列。トークン化経路と
# 同じ削除・権限変更・作成・リンク系の verb を含め、JSON の壊れ方だけで判定が変わら
# ないようにする。`ed` / `ex` / `red` は部分一致で ``used`` 等の通常の語に一致し、
# 指標としての情報量が消えるため入れない。
_RAW_TEXT_RISK_INDICATORS = (
    (">", "tee", "-i")
    + tuple(sorted(_REMOVE_COMMANDS))
    + tuple(sorted(_MODE_COMMANDS))
    + tuple(sorted(_TOUCH_COMMANDS))
    + ("ln",)
)

# `NAME=value` 形式の literal 環境変数代入。
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# `_command_index` が読み飛ばす実行 wrapper。``tee pyproject.toml``（deny）と
# ``echo tee pyproject.toml``（allow）を分けるには実行位置の特定が要り、名前集合が
# 原理的に不可避になる。`block_no_verify` の全トークン走査との非対称は意図的で、
# 未知の wrapper の取りこぼしは承知のうえの境界である
# （docs/adr/shell-analysis-boundary.md）。
_COMMAND_POSITION_WRAPPERS = frozenset(
    {
        "env",
        "command",
        "sudo",
        "doas",
        "nohup",
        "nice",
        "ionice",
        "setsid",
        "stdbuf",
        "xargs",
        "time",
        "timeout",
        "chrt",
        "taskset",
    }
)

# wrapper 自身が値を取る short オプション（`env -u NAME` / `sudo -u user`）。
_WRAPPER_VALUE_SHORT_OPTIONS = frozenset({"-u"})

# wrapper がコマンド名より前に取る positional 引数（``timeout 5 rm`` の秒数、
# ``chrt 99 rm`` の優先度、``taskset 0x3 rm`` のマスク）。いずれも数字で始まり、実行
# ファイル名が数字で始まることはまず無いので、wrapper を 1 つ以上通過したあとに限り
# 読み飛ばす。
_WRAPPER_POSITIONAL_VALUE_RE = re.compile(r"^[0-9]")


def _command_index(segment: list[str]) -> int | None:
    """セグメント内で実際に実行される executable トークンの index を返す。

    先頭から連続する literal 環境変数代入（``NAME=value``）と `_COMMAND_POSITION_WRAPPERS`
    の実行 wrapper（値を取る wrapper 自身のオプションは値ごと）を消費し、最初にそれ以外の
    形になったトークンの index を返す。wrapper 通過後に限り、数字で始まる非オプション語を
    wrapper の positional 値として読み飛ばす（``5foo ruff.toml`` の実行位置はずらさない）。
    未知の wrapper オプションは 1 トークンだけ読み飛ばす。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。

    Returns:
        実行 executable の index。segment が空、または全トークンが代入/
        wrapper/オプションで実行対象が特定できない場合は None。

    Raises:
        例外は発生しません。
    """
    index = 0
    seen_wrapper = False
    while index < len(segment):
        token = segment[index]
        if _ENV_ASSIGNMENT_RE.match(token):
            index += 1
            continue
        if normalize_executable_name(token) in _COMMAND_POSITION_WRAPPERS:
            seen_wrapper = True
            index += 1
            continue
        if token in _WRAPPER_VALUE_SHORT_OPTIONS:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        if seen_wrapper and _WRAPPER_POSITIONAL_VALUE_RE.match(token):
            index += 1
            continue
        return index
    return None


def _protected_target(token: str) -> str | None:
    """トークンが `config_protection` の保護対象なら、その表示名を返す。

    basename 集合と保護 path（`protected_path_segment`）の両系統を見る。basename だけだと
    ``.git/hooks/`` 配下への Bash 経由の書込みが、Write/Edit なら deny されるのに素通りする。
    basename は symlink を解決した実体から取る（``alias -> pyproject.toml`` を通さない）。
    解決できなければ raw token の basename を使う。

    Args:
        token: 検査対象のトークン（パスの可能性がある）。

    Returns:
        保護対象の表示名。basename 一致ならそのファイル名、ディレクトリの path 一致なら
        末尾に ``/`` を付けた保護 path（例 ``.git/hooks/``）、内容次第で保護するファイルの
        path 一致なら ``.git/config``。該当しなければ None。

    Raises:
        例外は発生しません。
    """
    resolved = resolve_effective_target(token)
    name = resolved.name if resolved is not None else basename(token)
    # 判定は畳んだ名前で行い、表示名は観測した綴りのままにする（`Path.resolve()` は
    # APFS / NTFS で綴りを実体の大小へ直さず、``rm Ruff.toml`` が素通りするため）。
    if normalize_protected_name(name) in _ALL_PROTECTED_BASENAMES:
        return name
    segment = protected_path_segment(token)
    if segment is not None:
        return f"{segment}/"
    # ``.git/config`` は Edit/Write なら内容の兆候で判定するが、Bash の書込みは内容を解釈
    # できない（``printf … >> .git/config`` の本文は別のトークンで、素通りの余地が残る）ため、
    # 書込み先位置に現れた時点で deny する。
    return repo_git_config_path(token)


def _non_option_args(args: list[str]) -> list[str]:
    """引数トークン列から `-` 始まりのオプションを除いたものを返す。

    Args:
        args: 実行位置のコマンド名より後ろの引数トークン列。

    Returns:
        オプションでない引数トークンのリスト。

    Raises:
        例外は発生しません。
    """
    return [token for token in args if not token.startswith("-")]


def _last_non_option_arg(args: list[str]) -> list[str]:
    """最終の非オプション引数（`cp a b dest` の `dest`、`ln` のリンク名）を返す。

    Args:
        args: 実行位置のコマンド名より後ろの引数トークン列。

    Returns:
        最終の非オプション引数を高々 1 件含むリスト。

    Raises:
        例外は発生しません。
    """
    return _non_option_args(args)[-1:]


def _inplace_edit_args(args: list[str]) -> list[str]:
    """in-place フラグがあれば、編集対象になりうる引数をすべて返す。

    スクリプト引数（``s/a/b/``）も含めて返すが、保護対象かどうかは呼び出し側が
    `_protected_target` で判定するため誤検出にはならない。

    Args:
        args: 実行位置のコマンド名より後ろの引数トークン列。

    Returns:
        in-place フラグがあれば全引数、無ければ空リスト。

    Raises:
        例外は発生しません。
    """
    return list(args) if any(is_inplace_edit_flag(token) for token in args) else []


def _dd_of_args(args: list[str]) -> list[str]:
    """`dd of=<path>` の書き込み先をすべて返す。

    Args:
        args: 実行位置のコマンド名より後ろの引数トークン列。

    Returns:
        `of=` プレフィックスを除いた書き込み先パス文字列のリスト。

    Raises:
        例外は発生しません。
    """
    return [token[len("of=") :] for token in args if token.startswith("of=")]


def _redirect_targets(segment: list[str]) -> list[str]:
    """セグメント内の `>` 系リダイレクト先トークンをすべて返す。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。

    Returns:
        リダイレクト先の生トークン（パス文字列）のリスト。

    Raises:
        例外は発生しません。
    """
    return [
        segment[index + 1]
        for index, token in enumerate(segment)
        if token in _REDIRECT_OPERATORS and index + 1 < len(segment)
    ]


# 実行位置のコマンド名ごとの書き込み先候補の取り出し方。並び順が候補の順序
# （＝最初に一致した保護対象の表示名）を決める。名前は `_command_index` が求めた
# 実行位置だけで照合し、``echo tee pyproject.toml`` のような非実行位置の言及は見ない。
_EXECUTED_COMMAND_TARGET_RULES = (
    (_TEE_COMMANDS, _non_option_args),
    (_INPLACE_EDIT_COMMANDS, _inplace_edit_args),
    (ALWAYS_MUTATING_EDIT_EXECUTABLES, _non_option_args),
    (_TOUCH_COMMANDS, _non_option_args),
    (_LAST_ARG_WRITE_COMMANDS, _last_non_option_arg),
    (_REMOVE_COMMANDS, _non_option_args),
    (_MODE_COMMANDS, _non_option_args),
    (_LN_COMMANDS, _last_non_option_arg),
    (_DD_COMMANDS, _dd_of_args),
)


def _write_target_tokens_in_segment(segment: list[str]) -> list[str]:
    """セグメント内の書き込み先候補トークンを**すべて**返す。

    先勝ちで 1 件だけ返すと、repo 外の囮を先頭に置くだけで後続の保護対象
    書き込みが検査から外れる（``rm -f /tmp/ruff.toml ruff.toml``）。保護対象かどうかと
    repo スコープ内かどうかの判定は呼び出し側が候補ごとに行う。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。

    Returns:
        書き込み先候補の生トークン（パス文字列）のリスト。

    Raises:
        例外は発生しません。
    """
    targets = _redirect_targets(segment)
    index = _command_index(segment)
    if index is None:
        return targets
    name = normalize_executable_name(segment[index])
    args = segment[index + 1 :]
    for names, pick in _EXECUTED_COMMAND_TARGET_RULES:
        if name in names:
            targets += pick(args)
    return targets


def _changes_working_directory(segment: list[str]) -> bool:
    """セグメントがカレントディレクトリを移動するコマンドかを判定する。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。

    Returns:
        先頭の `_LEADING_SHELL_KEYWORDS` を除いた実行位置のコマンドが `_DIRECTORY_CHANGE_COMMANDS`
        に属するなら True。

    Raises:
        例外は発生しません。
    """
    start = 0
    while start < len(segment) and segment[start] in _LEADING_SHELL_KEYWORDS:
        start += 1
    index = _command_index(segment[start:])
    return index is not None and normalize_executable_name(segment[start + index]) in _DIRECTORY_CHANGE_COMMANDS


def _within_repo_root(resolved: Path, repo_root: Path) -> bool:
    """解決済みの書き込み先が `repo_root` 配下にあるかを判定する。

    Args:
        resolved: `resolve_effective_target` が返した、cwd 基準・symlink 解決済みの実体 path。
        repo_root: `resolve_repo_root` が返したリポジトリルート。

    Returns:
        `repo_root` 配下にあれば True。配下外、または `repo_root` 自身の解決に失敗した
        場合は False。

    Raises:
        例外は発生しません。
    """
    try:
        return resolved.is_relative_to(repo_root.resolve())
    except (OSError, RuntimeError):
        return False


class ScanBudgetExceededError(Exception):
    """`sh -c` の再帰先が検査予算（バイト数・トークン数）を超えたことを表す。

    入口の予算検査は外側のコマンドしか見ないため、外側が数トークンしか無いネスト payload
    を通す。再帰先を無予算で走らせると走査が `hooks.json` の timeout を超え、host に
    kill された hook は exit code を返さず silent fail-open になる。呼び出し側
    （`main`）は走査を打ち切って BLOCKED にする。
    """


def find_protected_write(command: str, *, _recursed: bool = False) -> str | None:
    """コマンド文字列内に保護対象への書き込みがあればその表示名を返す。

    保護対象ヒットがあった場合のみリポジトリルートを解決し、書き込み先がその配下に
    ある場合のみ deny する。ルートが決定できない場合は allow。ただし保護名に一致した
    トークン自体が解決できない場合（単独サロゲート・symlink ループ等）は、リポジトリ外と
    示せないため deny する。

    コマンドが `cd` 等でカレントディレクトリを移動する場合は、cwd 基準の相対パス解決が
    実行時の位置とずれるため、保護対象のヒットをそのまま deny する
    （``cd plugins && printf x > ../ruff.toml`` を通さない。docs/adr/shell-analysis-boundary.md）。

    既知シェルの ``-c`` へ渡された文字列コマンドへは 1 段だけ再帰する（`block_no_verify`
    と同じ。2 段以上のネストは非目標）。

    Args:
        command: 検査対象のシェルコマンド文字列。
        _recursed: 内部再帰用フラグ。True の場合、これ以上ラッパーへ再帰しない。

    Returns:
        `_protected_target` / `find_host_guard_match` の表示名（`_blocked_message` が文言を選ぶ鍵）、
        または ``~/.claq/logs``。該当しなければ None。

    Raises:
        ScanBudgetExceededError: `sh -c` の再帰先が検査予算を超えた。
    """
    # heredoc のデータ本文は方言展開の前に 1 回だけ落とす（`block_no_verify` と同じ順序に
    # しないと、同じ入力でも hook によって解析対象が変わる）。
    stripped = strip_data_heredoc_bodies(command)
    for variant in command_dialect_variants(stripped):
        found = _find_protected_write_in_dialect(variant, _recursed=_recursed)
        if found is not None:
            return found
    return None


def _find_protected_write_in_dialect(command: str, *, _recursed: bool) -> str | None:
    """1 つのシェル方言の読み方で保護対象書き込みを探す。

    ``~/.claq/logs/``・ホーム側の git 設定・`find_host_guard_match`（``hooks/hooks.json``・実行中の
    plugin の ``runtime/`` と ``src/claq/``・ホスト設定）への一致は repo スコープ判定より前で、無条件に deny する。
    保護名に一致したトークンの解決に失敗した場合も repo スコープ判定に進めないため deny する。

    Args:
        command: `command_dialect_variants` が返した 1 通りの読み方
            （heredoc のデータ本文は呼び出し元で除去済み）。
        _recursed: ラッパー再帰済みかどうか。

    Returns:
        保護対象の表示名。該当しなければ None。

    Raises:
        ScanBudgetExceededError: `sh -c` の再帰先が検査予算を超えた。
    """
    segments = split_segments(tokenize(command))
    scope_certain = not any(_changes_working_directory(segment) for segment in segments)
    for segment in segments:
        for token in _write_target_tokens_in_segment(segment):
            home_log_hit = _find_home_log_directory_match(token)
            if home_log_hit is not None:
                return home_log_hit
            # ホーム側の設定はリポジトリ外の固定位置。repo スコープ判定に掛けると allow になる。
            global_config_hit = global_git_config_path(_expand_config_home_variables(token))
            if global_config_hit is not None:
                return global_config_hit
            host_guard_hit = find_host_guard_match(token, scope_certain=scope_certain)
            if host_guard_hit is not None:
                return host_guard_hit
            # ``cd ~ && printf x >> .gitconfig``: `cd` を含むコマンドは basename で見る。
            if not scope_certain and normalize_protected_name(basename(token)) == GLOBAL_GITCONFIG_NAME:
                return GLOBAL_GITCONFIG_DISPLAY_NAME
            found = _protected_target(token)
            if found is None:
                continue
            if not scope_certain:
                return found
            resolved = resolve_effective_target(token)
            if resolved is None:
                # 保護名に一致したトークンが解決できない（単独サロゲート・symlink ループ等）。
                # 書込み先がリポジトリ外だと示せない以上、入力側が任意に作れる解決不能で
                # 保護を外せないよう deny する（docs/adr/hook-failure-direction.md）。
                return found
            repo_root = resolve_repo_root()
            if repo_root is None:
                # 「決定不能なら allow」はこの書込み先 1 つについての判断。return にすると
                # 残りの書込み先候補と `sh -c` 再帰まで捨てて allow が広がる。
                continue
            if _within_repo_root(resolved, repo_root):
                return found
        if not _recursed:
            wrapped = extract_shell_wrapper_command(segment)
            if wrapped is None:
                continue
            # 入口の予算検査は外側しか見ない。再帰先にも同じ上限を当てる。
            if command_exceeds_scan_budget(wrapped):
                raise ScanBudgetExceededError
            if (found := find_protected_write(wrapped, _recursed=True)) is not None:
                return found
    return None


def _raw_text_write_risk(raw_input: str) -> str | None:
    """JSON が壊れている場合の fallback: 生テキストに保護対象 basename と書き込み指示が両方見えるかを判定する。

    「壊れているというだけで deny」にはせず、保護対象ファイル名と破壊的操作の指示の
    両方が生文字列上に見える場合のみ deny する。保護 path（``.git/hooks/``）はここでは
    見ない。JSON が壊れるのはハーネス側の事故で agent が制御できるチャネルではなく、
    この縮退経路の攻撃面は無視できるため。

    Args:
        raw_input: フックへ渡された生の入力文字列。

    Returns:
        保護対象ファイル名。該当しなければ None。

    Raises:
        例外は発生しません。
    """
    # 指標には PowerShell の長形式 cmdlet（`Remove-Item`）が入るため、トークン化経路の
    # 名前比較と同じ casefold で大小を無視する。
    lowered = raw_input.casefold()
    if not any(indicator in lowered for indicator in _RAW_TEXT_RISK_INDICATORS):
        return None
    # `_ALL_PROTECTED_BASENAMES` は既に畳み済み。
    for name in sorted(_ALL_PROTECTED_BASENAMES):
        if name in lowered:
            return name
    return None


_TOKEN_BUDGET_MESSAGE = (
    f"[Hook] BLOCKED: command exceeds {MAX_COMMAND_TOKENS} shell tokens for pre:bash-config-protection. "
    "A command this large cannot be inspected within the hook timeout, and an un-inspected "
    "command must not be allowed. Split it into smaller commands."
)

_TRUNCATED_INPUT_MESSAGE = (
    f"[Hook] BLOCKED: input exceeded {MAX_STDIN_BYTES} bytes for pre:bash-config-protection. "
    "Refusing to evaluate a possibly-truncated bash command for protected config writes. "
    "Retry with a smaller command."
)

_MALFORMED_INPUT_MESSAGE = (
    "[Hook] BLOCKED: hook input could not be parsed as JSON, but the raw command "
    "text appears to write to a protected configuration file. Refusing to allow an "
    "unverifiable write through."
)


def _blocked_home_log_message(display_name: str) -> str:
    """``~/.claq/logs/`` への Bash 経由の書込みブロック時の理由メッセージを生成する。

    `blocked_message_for_file` はリンタ設定向けの文言で、ログディレクトリの保護理由
    （プロンプト注入の永続化経路を塞ぐこと）に合わないため専用の文言を持つ。

    Args:
        display_name: ブロックされたパスの表示名。

    Returns:
        ブロック理由の文字列。

    Raises:
        例外は発生しません。
    """
    return (
        f"[Hook] BLOCKED: Writing to {display_name} via Bash is not allowed. "
        "This directory's content is read back into every future session's "
        "context, so free-form writes here are a prompt-injection persistence "
        "vector. If this write is genuinely necessary, ask the user to make it — "
        "do not disable the protection hook yourself."
    )


def _blocked_git_config_message(display_name: str) -> str:
    """``.git/config`` への Bash 経由の書込みブロック時の理由メッセージを生成する。

    `blocked_message_for_file` は linter 設定向けの文言で、``.git/config`` の保護理由
    （``core.hooksPath`` 等でフックを無効化できること）に合わないため専用の文言を持つ。

    Args:
        display_name: ブロックされたパスの表示名。

    Returns:
        ブロック理由の文字列。

    Raises:
        例外は発生しません。
    """
    return (
        f"[Hook] BLOCKED: Writing to {display_name} via Bash is not allowed. "
        "The file can disable git hooks (core.hooksPath, include.path, alias) and the "
        "written content cannot be inspected here. Change ordinary settings with "
        "`git config <key> <value>` instead. If this write is genuinely necessary, ask the "
        "user to make it — do not disable the protection hook yourself."
    )


def _blocked_message(found: str) -> str:
    """保護対象の表示名に対応するブロック理由を選ぶ。

    Args:
        found: `find_protected_write` が返した表示名。

    Returns:
        ブロック理由の文字列。

    Raises:
        例外は発生しません。
    """
    if found in GIT_CONFIG_DISPLAY_NAMES:
        return _blocked_git_config_message(found)
    if found.startswith("~/"):
        return _blocked_home_log_message(found)
    return blocked_message_for_file(found)


def main() -> int:
    """Bash コマンドによる保護対象設定ファイルへの直接書き込みを検知してブロックする。

    Returns:
        終了コード（0: 許可、2: ブロック）

    Raises:
        例外は発生しません。
    """
    try:
        raw, truncated = read_raw_stdin_with_truncation()
    except StdinUnavailableError as exc:
        return emit_block_output(stdin_unreadable_message("pre:bash-config-protection", exc))
    if truncated:
        return emit_block_output(_TRUNCATED_INPUT_MESSAGE)

    if not raw:
        return 0

    data = parse_json_object(raw)
    if data is None:
        return emit_block_output(_MALFORMED_INPUT_MESSAGE) if _raw_text_write_risk(raw) else 0

    tool_name = extract_raw_tool_name(data)
    # 既知の「シェルではないツール」だけを skip する。ツール名を特定
    # できない payload を対象外へ倒すと保護が丸ごと無効になる。
    if normalize_tool_name(tool_name).lower() not in _BASH_TOOL_NAMES and not is_unidentifiable_tool(
        tool_name
    ):
        return 0

    # コンテナキーは 1 つも取りこぼさず走査する。先勝ちで 1 キーだけ見ると、無害な側だけを
    # 検査して後続キーの保護対象書き込みを素通りさせる。
    for command in iter_bash_commands(data):
        # 走査はトークン数・バイト数に対して重い。上限超過を走らせると timeout で host に
        # kill され silent fail-open になるため、検査しきれない入力は通さず止める。
        if command_exceeds_scan_budget(command):
            return emit_block_output(_TOKEN_BUDGET_MESSAGE)
        try:
            found = find_protected_write(command)
        except ScanBudgetExceededError:
            return emit_block_output(_TOKEN_BUDGET_MESSAGE)
        if found:
            return emit_block_output(_blocked_message(found))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
