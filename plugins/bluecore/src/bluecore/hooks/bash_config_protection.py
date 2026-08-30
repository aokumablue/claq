"""Bash 経由の重要な設定ファイル直接書き換えを保護します（A-06 対応）。

トリガー: pre:bash
入力: bashコマンドを含むJSON
出力: 保護対象ファイルへの書き込みが検出された場合は host 非依存の合併出力でブロック
終了: 0 (許可) または 2 (ブロック)

`config_protection` は Edit/Write/MultiEdit 系 matcher にしか登録されておらず、
`printf x > pyproject.toml` のような Bash 経由の直接書き換えを検査しない
（監査 A-06）。本モジュールは同じ保護対象定義（basename 集合の
`PROTECTED_FILES` / `CONDITIONALLY_PROTECTED_FILES` と、ディレクトリ単位の
`protected_path_segment`。いずれも `config_protection` から import して共有）を
Bash コマンド文字列に対して適用する。保護対象の定義は `config_protection` を
単一情報源とし、本モジュール側で再定義しない。

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
        - `sed -i` / `perl -i`（`-0pi` 等の結合短形式含む）の対象引数（in-place 編集）
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

from bluecore.hooks.config_protection import (
    CONDITIONALLY_PROTECTED_FILES,
    PROTECTED_FILES,
    blocked_message_for_file,
    protected_path_segment,
)
from bluecore.hooks.hook_common import (
    MAX_STDIN_BYTES,
    basename,
    emit_block_output,
    parse_json_object,
    read_raw_stdin_with_truncation,
    resolve_effective_target,
    resolve_repo_root,
    split_segments,
    strip_data_heredoc_bodies,
    tokenize,
)
from bluecore.lib.harness import extract_raw_tool_name, iter_bash_commands, normalize_tool_name

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
_LAST_ARG_WRITE_COMMANDS = frozenset({"cp", "mv", "install"})

# ファイルを消す／空にするコマンド群。書き込みではないが、リンタ設定を消せば
# ルールごと無効化できるため、上書きと同じ強さの弱体化として扱う。`mv` は
# 移動元も対象にする（`mv ruff.toml /tmp/backup` は実質削除）。`cp` の複製元は
# 元ファイルが残るため対象にしない。
_REMOVE_COMMANDS = frozenset({"rm", "unlink", "shred", "truncate", "mv"})

# ファイルの中身を変えずに検査を無効化できるコマンド群。`chmod -x
# .git/hooks/pre-commit` は 1 コマンドでフックを実行不能にし、`chmod 000
# .eslintrc.json` は linter を読めなくする。中身を書き換える経路だけを塞ぐと
# 「塞いだ経路の存在が誤った安心になる」（config_protection の同趣旨コメント
# 参照）ため、削除と同じ強さの弱体化として扱う。引数の解釈はモード文字列と
# パスを区別せず、保護対象に一致したトークンがあれば deny する
# （ADR-0002: 誤検出 > 誤通過）。
_MODE_COMMANDS = frozenset({"chmod", "chown", "chgrp", "chflags"})

_ALL_PROTECTED_BASENAMES = PROTECTED_FILES | CONDITIONALLY_PROTECTED_FILES

# `NAME=value` 形式の literal 環境変数代入（M-01: 実行 executable 位置の特定に使う）。
# malformed JSON fallback で「破壊的操作の指示」とみなす部分文字列。書き込み系
# （`>`/`tee`/`-i`）に加えて、トークン化経路が既に見ている削除・リンク系の verb を
# 含める。部分一致なので過剰検出側に倒れるが、malformed 入力に対しては
# ADR-0002（誤検出 > 誤通過）どおりそれでよい。
_RAW_TEXT_RISK_INDICATORS = (
    (">", "tee", "-i") + tuple(sorted(_REMOVE_COMMANDS)) + tuple(sorted(_MODE_COMMANDS)) + ("ln",)
)

_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# `_command_index` が読み飛ばす実行 wrapper（basename 判定）。
_COMMAND_POSITION_WRAPPERS = frozenset({"env", "command", "sudo"})

# wrapper 自身が値を取る short オプション（`env -u NAME` / `sudo -u user`）。
_WRAPPER_VALUE_SHORT_OPTIONS = frozenset({"-u"})


def _command_index(segment: list[str]) -> int | None:
    """セグメント内で実際に実行される executable トークンの index を返す（M-01）。

    `tee pyproject.toml` の実行位置と `echo tee pyproject.toml` の
    非実行位置を区別するために使う。先頭から連続する literal 環境変数代入
    （``NAME=value``）と、``env``/``command``/``sudo`` の実行 wrapper（basename
    判定。``-u NAME`` のような値を取る wrapper 自身のオプションは値ごと
    読み飛ばす）を消費し、最初にそれ以外の形になったトークンの index を返す。

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
    while index < len(segment):
        token = segment[index]
        if _ENV_ASSIGNMENT_RE.match(token):
            index += 1
            continue
        if token.rsplit("/", 1)[-1] in _COMMAND_POSITION_WRAPPERS:
            index += 1
            continue
        if token in _WRAPPER_VALUE_SHORT_OPTIONS:
            index += 2
            continue
        if token.startswith("-"):
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
    if name in _ALL_PROTECTED_BASENAMES:
        return name
    segment = protected_path_segment(token)
    return f"{segment}/" if segment is not None else None


def _redirect_target(segment: list[str]) -> str | None:
    """セグメント内の `>` 系リダイレクト先が保護対象ならその生トークンを返す。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。

    Returns:
        書き込み先の生トークン（パス文字列）。該当しなければ None。

    Raises:
        例外は発生しません。
    """
    for index, token in enumerate(segment):
        if token in _REDIRECT_OPERATORS and index + 1 < len(segment):
            candidate = segment[index + 1]
            if _protected_target(candidate):
                return candidate
    return None


def _tee_target(segment: list[str]) -> str | None:
    """セグメント内の `tee` の出力先引数が保護対象ならその生トークンを返す。

    `tee` が実際に実行される位置（`_command_index`）にある場合のみ判定する
    （M-01: ``echo tee pyproject.toml`` のように `tee` が実行されない位置に
    現れるだけの誤検出を避けるため）。`-a`（追記）等のオプショントークンは
    読み飛ばし、非オプション引数を出力先候補として検査する。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。

    Returns:
        書き込み先の生トークン（パス文字列）。該当しなければ None。

    Raises:
        例外は発生しません。
    """
    index = _command_index(segment)
    if index is None or segment[index].rsplit("/", 1)[-1] != "tee":
        return None
    for token in segment[index + 1 :]:
        if token.startswith("-"):
            continue
        if _protected_target(token):
            return token
    return None


def _sed_inplace_target(segment: list[str]) -> str | None:
    """セグメント内の `sed -i`（in-place 編集）の対象引数が保護対象ならその生トークンを返す。

    `sed` が実際に実行される位置（`_command_index`）にある場合のみ判定する
    （M-01: ``echo sed -i pyproject.toml`` のような非実行位置での誤検出を避ける）。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。

    Returns:
        書き込み先の生トークン（パス文字列）。該当しなければ None。

    Raises:
        例外は発生しません。
    """
    index = _command_index(segment)
    if index is None or segment[index].rsplit("/", 1)[-1] != "sed":
        return None
    args = segment[index + 1 :]
    has_inplace = any(token == "-i" or token.startswith("-i") for token in args if token.startswith("-"))
    if not has_inplace:
        return None
    for token in args:
        if _protected_target(token):
            return token
    return None


def _perl_inplace_target(segment: list[str]) -> str | None:
    """セグメント内の `perl -i`（`-0pi` 等の結合短形式含む）の対象引数が保護対象ならその生トークンを返す。

    perl の in-place 編集フラグは `-i` 単独、または `-0pi`/`-pi.bak` の
    ように他の短形式オプションと結合できる。結合位置は問わず、`-` 始まりの
    単一ダッシュ・トークンに小文字 `i` が含まれるかで判定する
    （`sed -i` と同じ「敵対的回避への防壁ではない」設計判断）。`perl` が
    実際に実行される位置（`_command_index`）にある場合のみ判定する（M-01）。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。

    Returns:
        書き込み先の生トークン（パス文字列）。該当しなければ None。

    Raises:
        例外は発生しません。
    """
    index = _command_index(segment)
    if index is None or segment[index].rsplit("/", 1)[-1] != "perl":
        return None
    args = segment[index + 1 :]
    has_inplace = any(token.startswith("-") and not token.startswith("--") and "i" in token for token in args)
    if not has_inplace:
        return None
    for token in args:
        if _protected_target(token):
            return token
    return None


def _last_arg_write_target(segment: list[str]) -> str | None:
    """`cp`/`mv`/`install` の最終（非オプション）引数が保護対象ならその生トークンを返す。

    これらのコマンドは複数ソースを取りうるが、書き込み先は常に末尾の
    非オプション引数（`cp a b c dest` の `dest`）である。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。

    Returns:
        書き込み先の生トークン（パス文字列）。該当しなければ None。

    Raises:
        例外は発生しません。
    """
    index = _command_index(segment)
    if index is None or segment[index].rsplit("/", 1)[-1] not in _LAST_ARG_WRITE_COMMANDS:
        return None
    non_option_tokens = [token for token in segment[index + 1 :] if not token.startswith("-")]
    if not non_option_tokens:
        return None
    candidate = non_option_tokens[-1]
    return candidate if _protected_target(candidate) else None


def _remove_target(segment: list[str]) -> str | None:
    """`rm`/`unlink`/`shred`/`truncate`/`mv` の引数に保護対象があればその生トークンを返す。

    削除・切り詰め・移動は書き込み先トークンとして現れないが、リンタ設定を消せば
    ルールごと無効化できるため上書きと同じ扱いにする（ADR-0002: false negative
    より false positive を選ぶ）。`mv` は移動先を `_last_arg_write_target` が既に
    見ているため、ここでは移動元を含む全引数を対象にする。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。

    Returns:
        保護対象の生トークン（パス文字列）。該当しなければ None。

    Raises:
        例外は発生しません。
    """
    return _first_protected_argument(segment, _REMOVE_COMMANDS)


def _mode_target(segment: list[str]) -> str | None:
    """`chmod`/`chown`/`chgrp`/`chflags` の引数に保護対象があればその生トークンを返す。

    中身を書き換えなくても、実行ビットや読み取り権限を落とせば検査は無効化
    できる（`chmod -x .git/hooks/pre-commit` / `chmod 000 .eslintrc.json`）。
    `_remove_target` と同じ理由で上書きと同じ扱いにする。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。

    Returns:
        保護対象の生トークン（パス文字列）。該当しなければ None。

    Raises:
        例外は発生しません。
    """
    return _first_protected_argument(segment, _MODE_COMMANDS)


def _first_protected_argument(segment: list[str], commands: frozenset[str]) -> str | None:
    """指定コマンド群の非オプション引数から最初の保護対象トークンを返す。

    `_remove_target` と `_mode_target` は「コマンド名を確かめ、以降の非
    オプション引数を順に見る」という同じ形をしており、片方だけ保護対象の
    判定を変えると非対称が生まれる（本バグの発生経路そのもの）。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。
        commands: 対象とするコマンド名（basename 比較）の集合。

    Returns:
        保護対象の生トークン（パス文字列）。該当しなければ None。

    Raises:
        例外は発生しません。
    """
    index = _command_index(segment)
    if index is None or segment[index].rsplit("/", 1)[-1] not in commands:
        return None
    for token in segment[index + 1 :]:
        if token.startswith("-"):
            continue
        if _protected_target(token):
            return token
    return None


def _ln_target(segment: list[str]) -> str | None:
    """`ln` の最終（非オプション）引数が保護対象ならその生トークンを返す。

    `-f` の有無で区別しない。既存ファイルを置き換える `ln -f` だけでなく、
    保護対象がまだ存在しない状態での `ln -s weak.toml ruff.toml` も、以後
    その名前を読む処理をリンク先の内容へ差し替えるため実質的な書き込みである。
    `-f` 付きだけを見ていた頃は、後者が素通りしていた（F-07）。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。

    Returns:
        書き込み先の生トークン（パス文字列）。該当しなければ None。

    Raises:
        例外は発生しません。
    """
    index = _command_index(segment)
    if index is None or segment[index].rsplit("/", 1)[-1] != "ln":
        return None
    non_option_tokens = [token for token in segment[index + 1 :] if not token.startswith("-")]
    if not non_option_tokens:
        return None
    candidate = non_option_tokens[-1]
    return candidate if _protected_target(candidate) else None


def _dd_of_target(segment: list[str]) -> str | None:
    """セグメント内の `dd of=<path>` の書き込み先が保護対象ならその生パス文字列を返す。

    `of=` トークンだけでは write command とみなさず、`dd` が実際に実行される
    位置（`_command_index`）にある場合のみ判定する（M-01: ``echo of=pyproject.toml``
    のような非実行位置での誤検出を避ける）。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。

    Returns:
        書き込み先の生パス文字列（`of=` プレフィックス除去済み）。
        該当しなければ None。

    Raises:
        例外は発生しません。
    """
    index = _command_index(segment)
    if index is None or segment[index].rsplit("/", 1)[-1] != "dd":
        return None
    for token in segment[index + 1 :]:
        if token.startswith("of="):
            candidate = token[len("of=") :]
            if _protected_target(candidate):
                return candidate
    return None


def _write_target_token_in_segment(segment: list[str]) -> str | None:
    """セグメント内の書き込み先トークン（保護対象ヒット時）を返す。

    Args:
        segment: 区切りトークンを含まない 1 セグメント分のトークン列。

    Returns:
        書き込み先の生トークン（パス文字列）。該当しなければ None。

    Raises:
        例外は発生しません。
    """
    return (
        _redirect_target(segment)
        or _tee_target(segment)
        or _sed_inplace_target(segment)
        or _perl_inplace_target(segment)
        or _last_arg_write_target(segment)
        or _remove_target(segment)
        or _mode_target(segment)
        or _ln_target(segment)
        or _dd_of_target(segment)
    )


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


def find_protected_write(command: str) -> str | None:
    """コマンド文字列内に保護対象への書き込みがあればその表示名を返す。

    書き込み先ヒットがあった場合のみ `resolve_repo_root` を呼び、書き込み先が
    現在のリポジトリルート配下にある場合のみ deny する（A-06）。リポジトリ
    ルートが決定できない場合は allow（決定不能を deny に倒すと false
    positive が残るため）。

    Args:
        command: 検査対象のシェルコマンド文字列。

    Returns:
        `_protected_target` の表示名（symlink 解決後の実体 basename、または
        ``.git/hooks/`` のような保護 path）。そのまま
        `blocked_message_for_file` に渡せる。該当しなければ None。

    Raises:
        例外は発生しません。
    """
    command = strip_data_heredoc_bodies(command)
    for segment in split_segments(tokenize(command)):
        token = _write_target_token_in_segment(segment)
        if token is None:
            continue
        repo_root = resolve_repo_root()
        if repo_root is None:
            return None
        if _within_repo_root(token, repo_root):
            return _protected_target(token)
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
    if not any(indicator in raw_input for indicator in _RAW_TEXT_RISK_INDICATORS):
        return None
    for name in sorted(_ALL_PROTECTED_BASENAMES):
        if name in raw_input:
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
    raw, truncated = read_raw_stdin_with_truncation()
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
    if normalize_tool_name(tool_name).lower() not in _BASH_TOOL_NAMES:
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
