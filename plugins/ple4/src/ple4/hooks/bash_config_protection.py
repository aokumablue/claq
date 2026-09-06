"""Bash 経由の重要な設定ファイル直接書き換えを保護します（A-06 対応）。

トリガー: pre:bash
入力: bashコマンドを含むJSON
出力: 保護対象ファイルへの書き込みが検出された場合は host 非依存の合併出力でブロック
終了: 0 (許可) または 2 (ブロック)

`config_protection` は Edit/Write/MultiEdit 系 matcher にしか登録されておらず、
`printf x > pyproject.toml` のような Bash 経由の直接書き換えを検査しない
（監査 A-06）。本モジュールは同じ保護対象定義（大小無視で畳んだ basename 集合の
`PROTECTED_FILES_FOLDED` / `CONDITIONALLY_PROTECTED_FILES_FOLDED` と、ディレクトリ
単位の `protected_path_segment`。いずれも `config_protection` から import して共有）を
Bash コマンド文字列に対して適用する。保護対象の定義は `config_protection` を
単一情報源とし、本モジュール側で再定義しない。畳んだ側を import するのは、
`config_protection` が Write を deny する綴り（``Ruff.toml``）と本モジュールが
Bash を deny する綴りを必ず一致させるため（C-2: 片側だけ大小無視にすると、
Write は塞がるのに ``rm Ruff.toml`` は通る非対称が生まれる）。

`config_protection` を Bash matcher に相乗りさせない理由:
    `config_protection.main()` の fail 姿勢（空入力 0 / malformed JSON deny /
    truncated deny）は、コメントが明記するとおり「matcher が書込み系ツールに
    限定されている」ことを根拠に較正されている。Bash は最も広い matcher で
    あり、同じ matcher に入れると malformed/truncated な非設定ファイル操作の
    Bash 呼び出しまで deny することになり可用性が壊れる。そのため本モジュール
    は独立した hooks.json エントリとして追加し、判定ロジックだけを分ける。

判定方式:
    `hook_common.strip_data_heredoc_bodies` でデータ heredoc の本文を落としてから
    （ADR-0017。本文が実行されうる形は落とさない）、
    `hook_common.tokenize`/`split_segments`（block_no_verify と共有するトーク
    ナイザ）でセグメント分割し、各セグメント内で保護対象（basename 一致、
    または `.git/hooks/` のようなディレクトリ単位の保護 path 配下・保護 path 自身）
    が**書き込み先トークンとして現れた場合のみ** deny する:
        - `>` / `>>` / `&>` / `>|` リダイレクト先（`1>`/`2>`/`1>>`/`2>>` は
          `1`/`2` が別トークンになり `>`/`>>` に一致するため追加検出不要）
        - `tee` の出力先引数
        - `sed` / `perl` の in-place 編集の対象引数。フラグ語彙は
          `hook_common.is_inplace_edit_flag` が単一情報源で、`-i` 単独・
          `-0pi` 等の結合短形式・GNU 長形式 `--in-place[=SUFFIX]` とその
          非曖昧な短縮（`--i`）を含む
        - `ed`/`red`/`ex`/`sponge`（`hook_common.ALWAYS_MUTATING_EDIT_EXECUTABLES`）
          の引数。フラグ無しで書き換えるためフラグ判定の側では表現できない
        - `touch` の引数。内容は変えられないが、保護対象を**空で新規作成**すると
          linter の上位カスケード探索が止まり、実質的に設定の無効化になる
        - `cp`/`mv`/`install` の最終引数、`ln -f` の最終引数、`dd of=<path>`
    さらに、書き込み先ヒットがあった場合のみ `hook_common.resolve_repo_root`
    （`git rev-parse --show-toplevel`、プロセス内 1 回キャッシュ）でリポジトリ
    ルートを解決し、書き込み先を cwd 基準で解決したうえで**そのルート配下に
    ある場合のみ** deny する（A-06: basename だけの判定は別リポジトリ・別
    ディレクトリの同名ファイルを誤検出していた）。**リポジトリルートが決定
    できない場合は allow**（決定不能を deny に倒すと A-06 の false positive が
    残るため）。

    repo スコープ判定の適用範囲:
        この判定はシェル展開を経ていない生トークンに対して行うため、
        書き込み先に未展開の変数が含まれる場合（``printf x > $TMP/pyproject.toml``）
        は cwd 基準の結合がリポジトリ配下に着地し、実際の書き込み先が
        リポジトリ外でも deny になる。これは既知の false positive であり
        意図した姿勢である — POSIX シェル展開の模倣は原理的に不能で
        （``pre_bash_commit_quality`` の非目標と同じ理由）、変数を含むパスを
        「解決不能だから allow」に倒すと ``X=. ; printf y > $X/pyproject.toml``
        が確実なバイパスになる。したがって「リポジトリ配下のみ deny」という
        保証は変数を含まない書き込み先に限る。
    「検査不能なら deny」には倒さない（可用性が死ぬ）。JSON が壊れている場合
    のみ、`pre_bash_commit_quality.evaluate()` と同じ姿勢（生テキストに保護対象
    basename + 書き込み指示が両方見えるときだけ deny、それ以外は 0。この
    フォールバックはトークン化された経路を持たないため repo スコープ判定も
    ディレクトリ単位の保護 path 判定も適用されない）を採る。

非目標: `python -c`/`eval`/任意スクリプト経由の間接書き込み、`$(...)`・変数
    展開・パイプ越しの間接書き込み、シェルエイリアス・ラッパースクリプト
    経由の呼び出し。POSIX シェルの完全解釈は行わず、うっかり書き換えの抑止
    であって敵対的回避への防壁ではない（`block_no_verify` と同じ設計判断。
    詳細は `docs/adr/0002-*.md`）。
"""

from __future__ import annotations

import re
from pathlib import Path

from ple4.hooks.config_protection import (
    CONDITIONALLY_PROTECTED_FILES_FOLDED,
    PROTECTED_FILES_FOLDED,
    blocked_message_for_file,
    protected_path_segment,
)
from ple4.hooks.hook_common import (
    ALWAYS_MUTATING_EDIT_EXECUTABLES,
    MAX_STDIN_BYTES,
    StdinUnavailableError,
    basename,
    command_dialect_variants,
    emit_block_output,
    extract_shell_wrapper_command,
    is_inplace_edit_flag,
    normalize_executable_name,
    normalize_protected_name,
    parse_json_object,
    read_raw_stdin_with_truncation,
    resolve_effective_target,
    resolve_repo_root,
    split_segments,
    stdin_unreadable_message,
    strip_data_heredoc_bodies,
    tokenize,
)
from ple4.lib.harness import (
    extract_raw_tool_name,
    is_unidentifiable_tool,
    iter_bash_commands,
    normalize_tool_name,
)

# matcher（hooks.json）は Bash 系エイリアスにアンカーされた正規表現。matcher の
# 綴りが将来ズレても本体側で対象外ツールを確実に早期 return するための多重防御。
_BASH_TOOL_NAMES = frozenset({"bash"})

# リダイレクト演算子。`hook_common.tokenize` は shlex の既定 punctuation_chars
# （``();<>|&``）を使うため、``>`` / ``>>`` / ``&>`` / ``>|`` は密着していても
# 独立トークンになる。``1>``/``2>``/``1>>``/``2>>`` は数字が別トークンに
# 分かれ ``>``/``>>`` そのものに一致するため、ここへ追加する必要はない
# （数字プレフィックス自体を明示しているのは意図の記録目的）。
_REDIRECT_OPERATORS = frozenset({">", ">>", "&>", ">|", "1>", "2>", "1>>", "2>>"})

# 最終引数が書き込み先になるコマンド群（A-02）。
# 末尾引数が書き込み先になるコマンド。PowerShell の長形式 cmdlet を併記するのは、
# Windows のシェルツールが PowerShell だからである（`cp`/`mv`/`rm` は
# PowerShell の別名として同じ cmdlet に解決されるので既に効くが、長形式で
# 書かれると語彙から外れていた。release-verify 2026-09-03 の再レビュー）。
_LAST_ARG_WRITE_COMMANDS = frozenset(
    {"cp", "mv", "install", "copy-item", "move-item", "copy", "move", "cpi", "mi"}
)

# ファイルを消す／空にするコマンド群。書き込みではないが、リンタ設定を消せば
# ルールごと無効化できるため、上書きと同じ強さの弱体化として扱う。`mv` は
# 移動元も対象にする（`mv ruff.toml /tmp/backup` は実質削除）。`cp` の複製元は
# 元ファイルが残るため対象にしない。
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

# ファイルの中身を変えずに検査を無効化できるコマンド群。`chmod -x
# .git/hooks/pre-commit` は 1 コマンドでフックを実行不能にし、`chmod 000
# .eslintrc.json` は linter を読めなくする。中身を書き換える経路だけを塞ぐと
# 「塞いだ経路の存在が誤った安心になる」（config_protection の同趣旨コメント
# 参照）ため、削除と同じ強さの弱体化として扱う。引数の解釈はモード文字列と
# パスを区別せず、保護対象に一致したトークンがあれば deny する
# （ADR-0002: 誤検出 > 誤通過）。
_MODE_COMMANDS = frozenset({"chmod", "chown", "chgrp", "chflags"})

# 単一コマンドの抽出関数が実行位置判定（`_command_index`）へ渡す名前集合。
# `_executed_command_args` の引数を集合で統一するため、1 要素でも集合で持つ。
# 非オプション引数がすべて書き込み先になるコマンド。`tee` と、同じ形をとる
# PowerShell の書き込み系 cmdlet。
_TEE_COMMANDS = frozenset(
    {"tee", "set-content", "add-content", "out-file", "new-item", "sc", "ac", "ni"}
)
# in-place 編集で**フラグを伴うときだけ**書き込みになるコマンド。フラグ判定は
# `is_inplace_edit_flag`（`hook_common`）が単一情報源で、`sed` と `perl` を別関数に
# 分けていた頃の「sed は `-i` 前方一致 / perl は `i` の包含」という食い違いは
# ここで消える。フラグ無しでも書き込むエディタは
# `ALWAYS_MUTATING_EDIT_EXECUTABLES` の側（M-8）。
_INPLACE_EDIT_COMMANDS = frozenset({"sed", "perl"})
_LN_COMMANDS = frozenset({"ln"})
_DD_COMMANDS = frozenset({"dd"})

# 存在しないファイルを空で新規作成するコマンド（M-9）。内容を書き換えられなくても、
# **保護対象の名前を空で置くだけで検査を無効化できる**: 多くの linter は設定ファイルを
# 見つけた時点で上位ディレクトリの探索を打ち切るため（ESLint の cascade、
# ``.eslintrc`` が典型）、空の設定を置くことは「上位のルール一式を無効化する」ことに
# 等しい。`touch` はどの語彙集合にも入っておらず、保護対象 36 ファイル全てで
# allow だった（実測 exit 0）。
#
# PowerShell の ``New-Item`` は同じ効果を持つが、既に `_TEE_COMMANDS`
# （非オプション引数がすべて書き込み先）に載っているのでここへは重複させない。
_TOUCH_COMMANDS = frozenset({"touch"})

# カレントディレクトリを移動するコマンド。これらが現れたコマンドでは、cwd 基準の
# 相対パス解決が実行時の位置とずれるため repo スコープ判定を信用しない。
_DIRECTORY_CHANGE_COMMANDS = frozenset({"cd", "pushd", "popd", "chdir"})

_ALL_PROTECTED_BASENAMES = PROTECTED_FILES_FOLDED | CONDITIONALLY_PROTECTED_FILES_FOLDED

# `NAME=value` 形式の literal 環境変数代入（M-01: 実行 executable 位置の特定に使う）。
# malformed JSON fallback で「破壊的操作の指示」とみなす部分文字列。書き込み系
# （`>`/`tee`/`-i`）に加えて、トークン化経路が既に見ている削除・リンク系の verb を
# 含める。部分一致なので過剰検出側に倒れるが、malformed 入力に対しては
# ADR-0002（誤検出 > 誤通過）どおりそれでよい。
#
# `ALWAYS_MUTATING_EDIT_EXECUTABLES`（`ed` / `ex` / `red`）はここへ入れない。
# 判定が部分一致なので 2〜3 文字の名前は ``used`` ``next`` ``required`` のような
# 通常の語へ一致し、指標としての情報量が消える（保護対象 basename が見えている
# 入力はほぼ常に deny になり、この tuple 自体が意味を失う）。`touch` は語として
# 十分に長く、この問題を起こさないので含める。
_RAW_TEXT_RISK_INDICATORS = (
    (">", "tee", "-i")
    + tuple(sorted(_REMOVE_COMMANDS))
    + tuple(sorted(_MODE_COMMANDS))
    + tuple(sorted(_TOUCH_COMMANDS))
    + ("ln",)
)

_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# `_command_index` が読み飛ばす実行 wrapper（`normalize_executable_name` で判定）。
#
# ホワイトリストである理由と、`block_no_verify` が全トークン走査を採る理由の非対称は
# **意図的な設計判断**であり `docs/adr/0021-command-position-detection-uses-a-wrapper-allowlist.md`
# に記録した（代替案の実測を含む）。要旨だけ再掲すると: 本モジュールは M-01 のため
# **実行位置の特定**が必要で、``tee pyproject.toml``（deny）と
# ``echo tee pyproject.toml``（allow）は構文上同じ「語 → 語 → パス」なので何らかの
# 名前集合が原理的に不可避になる。`block_no_verify` は実行位置を必要としないため
# 名前集合を持たずに済む。この集合を消して全トークン走査へ揃える変更は、ADR-0021 の
# 代替案 1 として却下済みである。
#
# 結果としてこの集合は部分緩和であり、未知の wrapper（``chrt`` 相当の新顔）は
# 取りこぼす。それは承知のうえの境界で、`_command_index` の numeric positional
# 消費（下記）だけが唯一の一般化である。
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
# ``nice -n 5 rm`` のレベル、``chrt 99 rm`` の優先度、``taskset 0x3 rm`` のマスク）。
# いずれも数字で始まり、実行ファイル名が数字で始まることはまず無いので、
# **wrapper を 1 つ以上通過したあとに限り** 数字始まりの非オプション語を
# wrapper 自身の値として読み飛ばす。これが無いと ``timeout 5 rm ruff.toml`` の
# 実行位置が `5` に着地し、wrapper を語彙へ足しても deny に届かない。
_WRAPPER_POSITIONAL_VALUE_RE = re.compile(r"^[0-9]")


def _command_index(segment: list[str]) -> int | None:
    """セグメント内で実際に実行される executable トークンの index を返す（M-01）。

    `tee pyproject.toml` の実行位置と `echo tee pyproject.toml` の
    非実行位置を区別するために使う。先頭から連続する literal 環境変数代入
    （``NAME=value``）と、`_COMMAND_POSITION_WRAPPERS` の実行 wrapper
    （`normalize_executable_name` 判定。``-u NAME`` のような値を取る wrapper
    自身のオプションは値ごと読み飛ばす）を消費し、最初にそれ以外の形になった
    トークンの index を返す。

    wrapper を 1 つ以上通過したあとは、数字で始まる非オプション語を wrapper
    自身の positional 値として読み飛ばす（``timeout 5 rm`` の ``5``）。
    wrapper 通過前には適用しないので、``5foo ruff.toml`` のような通常コマンドの
    実行位置はずれない。

    未知の wrapper オプション（値の有無を判定できないもの）は 1 トークンだけ
    読み飛ばす。これは `block_no_verify` と同じ「うっかりバイパスの抑止」
    という設計判断で、POSIX シェルの完全な引数解釈は行わない（ADR-0002）。

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

    `config_protection` は basename 集合（`PROTECTED_FILES` /
    `CONDITIONALLY_PROTECTED_FILES`）と path 集合（`protected_path_segment`）の
    2 系統で保護しているが、本モジュールは前者しか見ておらず、
    ``.git/hooks/`` 配下への Bash 経由の書込み・削除が素通りしていた。Write/Edit
    なら deny される同じ書込みが Bash なら通るという非対称は、A-06 で本モジュール
    を作った理由（`config_protection` の Edit/Write 限定を Bash 側で補完する）
    そのものに反するため、両系統をここで一本化する。判定の定義は
    `config_protection` を単一情報源とし、本モジュールでは再定義しない。

    basename 判定は symlink を解決してから行う（H-02 対応）。
    ``alias -> pyproject.toml`` のような symlink 経由の書込みは、raw token の
    basename（``alias``）だけを見ると保護対象と判定できずすり抜けていた。
    `resolve_effective_target` で実体 path を解決してから basename を取る。
    解決不能（壊れた・循環した symlink 等）な場合は raw token の basename に
    フォールバックする（`config_protection._effective_basename` と同じ理由）。
    path 判定側の symlink 解決は `protected_path_segment` の既存挙動に委ねる。

    Args:
        token: 検査対象のトークン（パスの可能性がある）。

    Returns:
        保護対象の表示名。basename 一致ならそのファイル名、path 一致なら
        末尾に ``/`` を付けた保護 path（例 ``.git/hooks/``）。
        該当しなければ None。

    Raises:
        例外は発生しません。
    """
    resolved = resolve_effective_target(token)
    name = resolved.name if resolved is not None else basename(token)
    # 判定は畳んだ名前で行い、返す表示名は観測した綴りのままにする（C-2）。
    # `Path.resolve()` は APFS / NTFS で綴りを実体の大小へ直さないため、素の
    # 比較では ``rm Ruff.toml`` が実体 ``ruff.toml`` を消すのに素通りしていた。
    if normalize_protected_name(name) in _ALL_PROTECTED_BASENAMES:
        return name
    segment = protected_path_segment(token)
    return f"{segment}/" if segment is not None else None


def _executed_command_args(segment: list[str], names: frozenset[str]) -> list[str] | None:
    """実行位置のコマンドが `names` のいずれかなら、その引数トークン列を返す。

    `tee pyproject.toml` の実行位置と `echo tee pyproject.toml` の非実行位置を
    区別する `_command_index` の判定を、コマンド別の抽出関数が写経せずに共有する
    ための helper（M-01 の判定軸を 1 か所に保つ）。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。
        names: 実行位置に来ていることを要求するコマンド名（basename 比較）の集合。

    Returns:
        コマンド名以降の引数トークン列（オプションを含む）。実行位置のコマンドが
        `names` に含まれない、または実行対象を特定できない場合は None。

    Raises:
        例外は発生しません。
    """
    index = _command_index(segment)
    if index is None or normalize_executable_name(segment[index]) not in names:
        return None
    return segment[index + 1 :]


def _non_option_args(args: list[str]) -> list[str]:
    """引数トークン列から `-` 始まりのオプションを除いたものを返す。

    Args:
        args: `_executed_command_args` が返した引数トークン列。

    Returns:
        オプションでない引数トークンのリスト。

    Raises:
        例外は発生しません。
    """
    return [token for token in args if not token.startswith("-")]


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


def _tee_targets(segment: list[str]) -> list[str]:
    """`tee` の出力先引数をすべて返す。

    `tee` が実際に実行される位置にある場合のみ判定する（M-01）。`-a`（追記）等の
    オプショントークンは出力先ではないので除く。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。

    Returns:
        出力先の生トークンのリスト。

    Raises:
        例外は発生しません。
    """
    args = _executed_command_args(segment, _TEE_COMMANDS)
    return _non_option_args(args) if args is not None else []


def _inplace_edit_targets(segment: list[str]) -> list[str]:
    """`sed` / `perl` の in-place 編集の対象になりうる引数をすべて返す。

    対象コマンドが実際に実行される位置にある場合のみ判定する（M-01）。
    スクリプト引数（``s/a/b/``）も含めて返すが、保護対象かどうかの判定は
    呼び出し側が `_protected_target` で行うため誤検出にはならない。

    フラグ判定は `hook_common.is_inplace_edit_flag` に委ねる。sed 用と perl 用を
    別関数に分けていた頃は sed 側だけが GNU 長形式を知らず、
    ``sed --in-place s/x/y/ ruff.toml`` が素通りしていた（H-5。実測で exit 0）。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。

    Returns:
        in-place 編集の対象になりうる生トークンのリスト。in-place フラグが
        無ければ空。

    Raises:
        例外は発生しません。
    """
    args = _executed_command_args(segment, _INPLACE_EDIT_COMMANDS)
    if args is None:
        return []
    return list(args) if any(is_inplace_edit_flag(token) for token in args) else []


def _always_mutating_edit_targets(segment: list[str]) -> list[str]:
    """`ed`/`red`/`ex`/`sponge` の編集対象になりうる引数をすべて返す。

    これらはフラグを 1 つも伴わずに引数のファイルを書き換えるため、
    `_inplace_edit_targets` のフラグ判定に掛けると常に空になる。実測では
    ``ed ruff.toml`` が exit 0 で素通りしていた（M-8）。語彙の根拠は
    `hook_common.ALWAYS_MUTATING_EDIT_EXECUTABLES` を単一情報源とする。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。

    Returns:
        編集対象になりうる非オプション引数の生トークンのリスト。

    Raises:
        例外は発生しません。
    """
    args = _executed_command_args(segment, ALWAYS_MUTATING_EDIT_EXECUTABLES)
    return _non_option_args(args) if args is not None else []


def _touch_targets(segment: list[str]) -> list[str]:
    """`touch` の対象引数をすべて返す。

    内容は書き換えられないが、保護対象の名前を**空で新規作成**できる。空の
    ``.eslintrc`` は ESLint の上位カスケード探索をそこで打ち切らせるため、実質的な
    lint 設定の無効化になる（M-9）。

    値を取るオプション（``-t <stamp>`` / ``-d <date>`` / ``-r <ref>``）の値は
    `_non_option_args` を素通りして候補に混じる。``touch -r ruff.toml app.py`` は
    読み取り参照でしかない `ruff.toml` を候補として deny するが、これは
    `_remove_targets` / `_mode_targets` と同じ姿勢であり ADR-0002（誤検出 >
    誤通過）の範囲内。オプションごとの arity 表を持ち込むと、`touch` の
    実装差（BSD / GNU / busybox）ごとに表が割れて取りこぼしが生まれる。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。

    Returns:
        作成・更新対象になりうる非オプション引数の生トークンのリスト。

    Raises:
        例外は発生しません。
    """
    args = _executed_command_args(segment, _TOUCH_COMMANDS)
    return _non_option_args(args) if args is not None else []


def _last_arg_write_targets(segment: list[str]) -> list[str]:
    """`cp`/`mv`/`install` の書き込み先（最終の非オプション引数）を返す。

    これらのコマンドは複数ソースを取りうるが、書き込み先は常に末尾の非オプション
    引数（`cp a b c dest` の `dest`）である。先行する引数は読み取り元なので
    含めない。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。

    Returns:
        書き込み先の生トークンを高々 1 件含むリスト。

    Raises:
        例外は発生しません。
    """
    args = _executed_command_args(segment, _LAST_ARG_WRITE_COMMANDS)
    return _non_option_args(args)[-1:] if args is not None else []


def _remove_targets(segment: list[str]) -> list[str]:
    """`rm`/`unlink`/`shred`/`truncate`/`mv` の引数をすべて返す。

    削除・切り詰め・移動は書き込み先トークンとして現れないが、リンタ設定を消せば
    ルールごと無効化できるため上書きと同じ扱いにする（ADR-0002: false negative
    より false positive を選ぶ）。`mv` は移動先を `_last_arg_write_targets` が既に
    見ているため、ここでは移動元を含む全引数を対象にする。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。

    Returns:
        非オプション引数の生トークンのリスト。

    Raises:
        例外は発生しません。
    """
    args = _executed_command_args(segment, _REMOVE_COMMANDS)
    return _non_option_args(args) if args is not None else []


def _mode_targets(segment: list[str]) -> list[str]:
    """`chmod`/`chown`/`chgrp`/`chflags` の引数をすべて返す。

    中身を書き換えなくても、実行ビットや読み取り権限を落とせば検査は無効化
    できる（`chmod -x .git/hooks/pre-commit` / `chmod 000 .eslintrc.json`）。
    `_remove_targets` と同じ理由で上書きと同じ扱いにする。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。

    Returns:
        非オプション引数の生トークンのリスト。

    Raises:
        例外は発生しません。
    """
    args = _executed_command_args(segment, _MODE_COMMANDS)
    return _non_option_args(args) if args is not None else []


def _ln_targets(segment: list[str]) -> list[str]:
    """`ln` のリンク名（最終の非オプション引数）を返す。

    `-f` の有無で区別しない。既存ファイルを置き換える `ln -f` だけでなく、
    保護対象がまだ存在しない状態での `ln -s weak.toml ruff.toml` も、以後
    その名前を読む処理をリンク先の内容へ差し替えるため実質的な書き込みである。
    `-f` 付きだけを見ていた頃は、後者が素通りしていた（F-07）。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。

    Returns:
        リンク名の生トークンを高々 1 件含むリスト。

    Raises:
        例外は発生しません。
    """
    args = _executed_command_args(segment, _LN_COMMANDS)
    return _non_option_args(args)[-1:] if args is not None else []


def _dd_of_targets(segment: list[str]) -> list[str]:
    """`dd of=<path>` の書き込み先をすべて返す。

    `of=` トークンだけでは write command とみなさず、`dd` が実際に実行される
    位置にある場合のみ判定する（M-01: ``echo of=pyproject.toml`` のような
    非実行位置での誤検出を避ける）。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。

    Returns:
        `of=` プレフィックスを除いた書き込み先パス文字列のリスト。

    Raises:
        例外は発生しません。
    """
    args = _executed_command_args(segment, _DD_COMMANDS)
    if args is None:
        return []
    return [token[len("of=") :] for token in args if token.startswith("of=")]


def _write_target_tokens_in_segment(segment: list[str]) -> list[str]:
    """セグメント内の書き込み先候補トークンを**すべて**返す。

    先勝ちで 1 件だけ返していた頃は、repo 外の囮を先頭に置くだけで後続の
    保護対象書き込みが検査から外れた（実測: ``rm -f /tmp/ruff.toml ruff.toml``
    が exit 0。囮なしの ``rm -f ruff.toml`` は exit 2）。保護対象かどうかと
    repo スコープ内かどうかの判定は呼び出し側が候補ごとに行う。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。

    Returns:
        書き込み先候補の生トークン（パス文字列）のリスト。

    Raises:
        例外は発生しません。
    """
    return [
        token
        for extract in (
            _redirect_targets,
            _tee_targets,
            _inplace_edit_targets,
            _always_mutating_edit_targets,
            _touch_targets,
            _last_arg_write_targets,
            _remove_targets,
            _mode_targets,
            _ln_targets,
            _dd_of_targets,
        )
        for token in extract(segment)
    ]


def _changes_working_directory(segment: list[str]) -> bool:
    """セグメントがカレントディレクトリを移動するコマンドかを判定する。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。

    Returns:
        実行位置のコマンドが `_DIRECTORY_CHANGE_COMMANDS` に属するなら True。

    Raises:
        例外は発生しません。
    """
    index = _command_index(segment)
    return index is not None and normalize_executable_name(segment[index]) in _DIRECTORY_CHANGE_COMMANDS


def _within_repo_root(token: str, repo_root: Path) -> bool:
    """書き込み先トークンを cwd 基準・symlink 解決済みで `repo_root` 配下にあるかを判定する。

    `token` が絶対パスなら cwd は無視される（pathlib の `/` 演算子の挙動）。
    symlink 解決は `_protected_target` と同じ `resolve_effective_target` を
    共有し、判定基準を一本化する（H-02）。

    Args:
        token: 書き込み先の生トークン（パス文字列）。
        repo_root: `resolve_repo_root` が返したリポジトリルート。

    Returns:
        `repo_root` 配下にあれば True。解決不能・配下外なら False。

    Raises:
        例外は発生しません。
    """
    resolved = resolve_effective_target(token)
    if resolved is None:
        return False
    try:
        root = repo_root.resolve()
    except (OSError, RuntimeError):
        return False
    try:
        resolved.relative_to(root)
    except ValueError:
        return False
    return True


def find_protected_write(command: str, *, _recursed: bool = False) -> str | None:
    """コマンド文字列内に保護対象への書き込みがあればその表示名を返す。

    保護対象ヒットがあった場合のみ `resolve_repo_root` を呼び、書き込み先が
    現在のリポジトリルート配下にある場合のみ deny する（A-06）。リポジトリ
    ルートが決定できない場合は allow（決定不能を deny に倒すと false
    positive が残るため）。

    ただしコマンドが `cd` 等でカレントディレクトリを移動する場合、cwd 基準の
    相対パス解決は実行時の位置とずれるため repo スコープ判定を信用できない。
    その場合は保護対象 basename のヒットをそのまま deny する（ADR-0018。
    実測: ``cd plugins && printf x > ../ruff.toml`` が exit 0 だった）。
    `cd` を跨いだ symlink 解決のずれは同じ理由で非目標。

    既知シェルの ``-c`` へ渡された文字列コマンドへは 1 段だけ再帰する
    （実測: ``bash -c 'printf x > ruff.toml'`` が exit 0 だった。`block_no_verify`
    は同じ再帰を持っており、片方だけ持たない非対称は A-06 で本モジュールを
    足した理由そのものに反する）。2 段以上のネストは ADR-0002 の非目標。

    Args:
        command: 検査対象のシェルコマンド文字列。
        _recursed: 内部再帰用フラグ。True の場合、これ以上ラッパーへ再帰しない。

    Returns:
        `_protected_target` の表示名（symlink 解決後の実体 basename、または
        ``.git/hooks/`` のような保護 path）。そのまま
        `blocked_message_for_file` に渡せる。該当しなければ None。

    Raises:
        例外は発生しません。
    """
    # heredoc のデータ本文はコマンドの語彙に入らないため、方言展開の前に 1 回だけ
    # 落とす（`block_no_verify` と同じ順序。片方が方言ごとに剥がす形だと、同じ
    # 入力でも hook によって解析対象が変わる非対称が生まれる）。
    stripped = strip_data_heredoc_bodies(command)
    for variant in command_dialect_variants(stripped):
        found = _find_protected_write_in_dialect(variant, _recursed=_recursed)
        if found is not None:
            return found
    return None


def _find_protected_write_in_dialect(command: str, *, _recursed: bool) -> str | None:
    """1 つのシェル方言の読み方で保護対象書き込みを探す。

    Args:
        command: `command_dialect_variants` が返した 1 通りの読み方
            （heredoc のデータ本文は呼び出し元で除去済み）。
        _recursed: ラッパー再帰済みかどうか。

    Returns:
        保護対象の表示名。該当しなければ None。

    Raises:
        例外は発生しません。
    """
    segments = split_segments(tokenize(command))
    scope_certain = not any(_changes_working_directory(segment) for segment in segments)
    for segment in segments:
        for token in _write_target_tokens_in_segment(segment):
            found = _protected_target(token)
            if found is None:
                continue
            if not scope_certain:
                return found
            repo_root = resolve_repo_root()
            if repo_root is None:
                # 「決定不能なら allow」は意図された fail-open（A-06）だが、
                # それはこの書込み先 1 つについての判断である。`return` にすると
                # 同一コマンドの残りの書込み先候補と `sh -c` 再帰まで捨てて
                # しまい、文書化されていない範囲まで allow が広がる。
                continue
            if _within_repo_root(token, repo_root):
                return found
        if not _recursed:
            wrapped = extract_shell_wrapper_command(segment)
            if wrapped is not None and (found := find_protected_write(wrapped, _recursed=True)) is not None:
                return found
    return None


def _raw_text_write_risk(raw_input: str) -> str | None:
    """JSON が壊れている場合の fallback: 生テキストに保護対象 basename と書き込み指示が両方見えるかを判定する。

    `pre_bash_commit_quality.evaluate()` の malformed JSON 姿勢と同じく、
    「壊れているというだけで deny」にはせず、保護対象ファイル名と破壊的操作の
    指示の両方が生文字列上に見える場合のみ deny する。

    破壊的操作には削除・リンク系（`rm`/`unlink`/`shred`/`truncate`/`mv`/`ln`）も
    含める。トークン化できる経路では `_remove_target` / `_ln_target` が既に
    これらを見ているのに、malformed fallback だけが書き込み指示（`>`/`tee`/`-i`）
    しか見ていなかった（F-07）。同じ入力が JSON の壊れ方だけで通ったり通らなく
    なったりするのは、境界としては説明できない。

    ディレクトリ単位の保護 path（`protected_path_segment`）はここでは見ない。
    理由は特異度ではなく露出の小ささである（``.git/hooks/`` は本関数が既に
    裸の部分一致で拾っている ``pyproject.toml`` / ``package.json`` より特異度が
    高く、「過剰 deny になるから」は本関数の既存挙動と両立しない）。JSON が
    壊れるのはハーネス側の事故であって agent が制御できるチャネルではないため、
    この縮退経路の攻撃面は無視できる。トークン化できる正常系は
    `_protected_target` が両系統を見ている。

    Args:
        raw_input: フックへ渡された生の入力文字列。

    Returns:
        保護対象ファイル名。該当しなければ None。

    Raises:
        例外は発生しません。
    """
    # 指標は畳んだ形で持つ（`_REMOVE_COMMANDS` 等に PowerShell の長形式 cmdlet が
    # 入っており、生テキストには `Remove-Item` と大文字混じりで現れる）。
    # トークン化経路の名前比較も大小無視なので、ここだけ大小を見ると
    # 「JSON が壊れているときだけ通る」非対称が生まれる。case 演算は
    # `normalize_protected_name` / `normalize_executable_name` と同じ casefold で
    # 揃える（片方だけ lower にすると、畳み方の違いが新しい非対称になる）。
    lowered = raw_input.casefold()
    if not any(indicator in lowered for indicator in _RAW_TEXT_RISK_INDICATORS):
        return None
    # `_ALL_PROTECTED_BASENAMES` は既に畳み済みなので、ここで再度畳まない。
    for name in sorted(_ALL_PROTECTED_BASENAMES):
        if name in lowered:
            return name
    return None


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


def main() -> int:
    """Bash コマンドによる保護対象設定ファイルへの直接書き込みを検知してブロックする。

    Args:
        引数はありません（標準入力から読み取る）。

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
        found = _raw_text_write_risk(raw)
        if found:
            return emit_block_output(_MALFORMED_INPUT_MESSAGE)
        return 0

    tool_name = extract_raw_tool_name(data)
    # 既知の「シェルではないツール」だけを skip する。ツール名を特定
    # できない payload を対象外へ倒すと保護が丸ごと無効になる（S-8）。
    if normalize_tool_name(tool_name).lower() not in _BASH_TOOL_NAMES and not is_unidentifiable_tool(
        tool_name
    ):
        return 0

    # コンテナキーは 1 つも取りこぼさず走査する（iter_bash_commands）。
    # 先勝ちで 1 キーだけ見ると、無害な側だけを検査して後続キーの保護対象
    # 書き込みを素通りさせる（実測: exit 0）。
    for command in iter_bash_commands(data):
        found = find_protected_write(command)
        if found:
            return emit_block_output(blocked_message_for_file(found))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
