"""claq フック実装の共通ユーティリティ。

フック出力の書き込み、保護対象名の正規化と判定、1 回の判定の中だけのメモ化、
リポジトリルートの解決を兄弟フックで共有する。シェルコマンドの解析と実行トークン名の
正規化は ``shell_tokens``、stdin の読み取りは ``stdin_io`` が持つ。
"""

from __future__ import annotations

import contextlib
import functools
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TextIO

from claq.hooks.shell_tokens import basename_any_separator, normalize_executable_name
from claq.lib.core_utils import get_home_dir
from claq.lib.output_adapter import adapt_deny_output
from claq.lib.subprocess_utils import SUBPROCESS_FAILURES

# `scan_memo` の with の中でだけ使う、引数ごとの結果の置き場。with の外では None（毎回計算する）。
_scan_memo: dict[tuple[object, ...], object] | None = None


@contextlib.contextmanager
def scan_memo() -> Iterator[None]:
    """with の間だけ、`memoized_in_scan` を付けた関数の結果を引数ごとに使い回す。

    書込み先 1 つの判定で、プロセス内で変わらない値（保護対象の置き場所・プラグインのルート）と
    同じトークンの実体 path を何度も計算し直すと、``sh -c`` を束ねた 58KB の入力で
    bash_config_protection が 18.7 秒かかり timeout（15 秒）を超えた（kill された hook は host に
    許可として扱われる）。1 回の判定の間は cwd・環境変数・ファイルシステムを変えないので、使い回しても
    結果は変わらない。テストが環境を変えても持ち越さないよう、with を出たら捨てる。

    Yields:
        None。
    """
    global _scan_memo
    _scan_memo = {}
    try:
        yield
    finally:
        _scan_memo = None


def memoized_in_scan[T](fn: Callable[..., T]) -> Callable[..., T]:
    """`scan_memo` の with の間だけ、引数ごとに結果を使い回す関数にする。

    例外は使い回さない（毎回呼び直して同じ例外を送る）。

    Args:
        fn: 引数がすべてハッシュ可能な関数。

    Returns:
        同じ呼び方の関数。
    """

    @functools.wraps(fn)
    def wrapper(*args: object, **kwargs: object) -> T:
        """with の中なら置き場を引き、無ければ計算して置く。"""
        if _scan_memo is None:
            return fn(*args, **kwargs)
        key = (fn, args, tuple(sorted(kwargs.items())))
        if key not in _scan_memo:
            _scan_memo[key] = fn(*args, **kwargs)
        return _scan_memo[key]  # type: ignore[return-value]

    return wrapper


@functools.lru_cache(maxsize=1)
def resolve_repo_root() -> Path | None:
    """`git rev-parse --show-toplevel` でリポジトリルートの絶対パスを解決しキャッシュする。

    `bash_config_protection` と `pre_bash_commit_quality` が共有する。呼び出し側は
    必要なときだけ呼び、`lru_cache` で同一プロセス内の `git` 起動を最大 1 回にする。
    テストでキャッシュを跨がせない場合は `resolve_repo_root.cache_clear()` を呼ぶこと。

    出力はロケールの復号に頼らず bytes で受け、`os.fsdecode` でパスへ戻す。ASCII ロケールの非 ASCII
    リポジトリでは復号が例外になり、hook が exit 1（PreToolUse では許可）で落ちる。

    Returns:
        リポジトリルートの絶対パス。タイムアウト・失敗時は None（呼び出し側が
        非ブロッキングにフォールバックする）。
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except SUBPROCESS_FAILURES:
        return None
    top = os.fsdecode(result.stdout).strip()
    return Path(top) if result.returncode == 0 and top else None


def basename(path: str) -> str:
    """パスからファイル名を取得します。

    Args:
        path: ファイルパスです。

    Returns:
        ファイル名を返します。
    """
    return Path(path).name


def normalize_protected_name(path: str) -> str:
    """保護対象ファイル名の照合用に basename を大小無視へ畳みます。

    `normalize_executable_name` と同じ case 演算（`casefold`）と basename 規則を使う。
    `Path.resolve()` は APFS / NTFS で綴りを実体の大小へ正規化しないため、
    ``Write Ruff.toml`` が実体 ``ruff.toml`` へ着地しても素の比較では外れる。
    `.exe` は落とさない（保護対象は設定ファイルで、同一視する根拠が無い）。

    Args:
        path: 検査対象のパス文字列、またはファイル名です。

    Returns:
        大小無視へ畳んだ basename を返します。
    """
    return basename_any_separator(path).casefold()


def fold_path_parts(parts: tuple[str, ...]) -> tuple[str, ...]:
    """パス要素列の各要素を `normalize_protected_name` で畳み、大小を無視して比べられる形にする。

    Args:
        parts: 畳むパス要素列。

    Returns:
        畳んだ要素列。
    """
    return tuple(normalize_protected_name(part) for part in parts)


def is_path_prefix(prefix: tuple[str, ...], parts: tuple[str, ...]) -> bool:
    """畳んだパス要素列 ``parts`` が ``prefix`` で始まる（``prefix`` 自身か、その配下を指す）かを返す。

    Args:
        prefix: `fold_path_parts` で畳んだ親側の要素列。
        parts: `fold_path_parts` で畳んだ判定対象の要素列。

    Returns:
        ``parts`` の先頭が ``prefix`` と一致すれば True。
    """
    return parts[: len(prefix)] == prefix


@functools.cache
def _folded_candidates(candidates: tuple[tuple[str, ...], ...]) -> tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]:
    """定数の候補表を (畳む前, 畳んだ後) の組にして返す（候補表はモジュール定数なのでプロセス内で 1 回だけ畳む）。

    Args:
        candidates: `match_folded_path_window` に渡す候補表。

    Returns:
        候補ごとの (畳む前の綴り, 畳んだ要素列)。
    """
    return tuple((segments, fold_path_parts(segments)) for segments in candidates)


def match_folded_path_window(
    parts: tuple[str, ...], candidates: tuple[tuple[str, ...], ...]
) -> tuple[str, ...] | None:
    """パス要素の連続した並びが候補のいずれかに一致するかを、畳んだ名前で判定する。

    候補の要素 ``*`` は任意の 1 要素に一致する。

    Args:
        parts: 判定するパスの要素列。
        candidates: 連続一致を探す要素列の候補（畳む前の綴り）。

    Returns:
        一致した候補（畳む前の綴り）。一致しなければ None。
    """
    folded_parts = fold_path_parts(parts)
    for segments, folded_segments in _folded_candidates(candidates):
        width = len(folded_segments)
        for index in range(len(folded_parts) - width + 1):
            window = folded_parts[index : index + width]
            if all(want in ("*", got) for want, got in zip(folded_segments, window, strict=True)):
                return segments
    return None


def is_git_executable_token(token: str) -> bool:
    """トークンが git 実行ファイルを指すかを判定します。

    Args:
        token: `shlex` 等でトークン化された1トークンです。

    Returns:
        git 実行ファイルとみなせるなら True。
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

# in-place 編集フラグ（`is_inplace_edit_flag`）を**伴うときだけ**引数のファイルを書き換えるコマンド。
# `ruby -pi` は `perl -pi` と同じ in-place 編集。`python3 -c` のような任意インタプリタは非目標で、
# ここには入れない。フラグ無しでも書き込むコマンド（`ed` 等）をここへ置くとフラグ判定に落ちて
# allow になるため、`ALWAYS_MUTATING_EDIT_EXECUTABLES` に置く。
# `bash_config_protection` と `pre_bash_commit_quality` が共有する。
INPLACE_EDIT_EXECUTABLES = frozenset({"sed", "perl", "ruby"})

# PowerShell / cmd のファイル書込み・削除コマンドを、書き込み先の取り方が同じ POSIX 名へ写す表。
# Windows の Bash ツールは PowerShell で動くため、別名（``ri`` / ``mi`` / ``clc`` は PowerShell の既定
# エイリアス、``del`` / ``erase`` / ``rd`` / ``copy`` / ``move`` / ``rename`` は cmd 組み込み）と長形式 cmdlet を併記する。
# `bash_config_protection` と `commit_risk` が `write_command_name` を通して共有する。片方だけが
# PowerShell 名を知っていると、``Copy-Item evil.py app.py; git commit -am x`` が片方のフックだけを素通りする。
WINDOWS_WRITE_COMMAND_ALIASES = {
    "copy-item": "cp",
    "copy": "cp",
    "cpi": "cp",
    "move-item": "mv",
    "move": "mv",
    "mi": "mv",
    # 改名は元の名前を消して新しい名前で作る（保護対象を別名へ逃がすのは削除と同じ）。
    "rename-item": "mv",
    "rename": "mv",
    "ren": "mv",
    "rni": "mv",
    "remove-item": "rm",
    "del": "rm",
    "erase": "rm",
    "rd": "rm",
    "rmdir": "rm",
    "ri": "rm",
    "clear-content": "truncate",
    "clc": "truncate",
    "tee-object": "tee",
    "set-content": "tee",
    "add-content": "tee",
    "out-file": "tee",
    "sc": "tee",
    "ac": "tee",
    # 非オプション引数をすべて作成先として読む点で `tee` と同じ取り方になる。
    "new-item": "tee",
    "ni": "tee",
}


def write_command_name(token: str) -> str:
    """実行トークンを、書込みコマンドの照合に使う名前（`normalize_executable_name` + Windows 別名の POSIX 化）へ畳む。

    Args:
        token: 実行位置の 1 トークン。

    Returns:
        PowerShell / cmd の書込みコマンドなら対応する POSIX 名、それ以外は正規化した名前。
    """
    name = normalize_executable_name(token)
    return WINDOWS_WRITE_COMMAND_ALIASES.get(name, name)


# カレントディレクトリを移動するコマンド。これらが現れたコマンドでは、cwd 基準の
# 相対パス解決が実行時の位置とずれるため repo スコープ判定を信用しない。PowerShell の
# ``Set-Location`` / ``Push-Location`` / ``Pop-Location`` と別名 ``sl`` も同じ移動になる。
# `bash_config_protection` と `pre_bash_commit_quality` が共有する。
DIRECTORY_CHANGE_COMMANDS = frozenset(
    {"cd", "pushd", "popd", "chdir", "set-location", "sl", "push-location", "pop-location"}
)

# wrapper の作業ディレクトリ指定（``env -C <dir>`` / ``env --chdir[=]<dir>`` / ``sudo -D <dir>`` /
# ``sudo --chdir[=]<dir>``）。`DIRECTORY_CHANGE_COMMANDS` と同じく cwd を動かす。
# `bash_config_protection`（cwd 基準の解決を信用するか）と `pre_bash_commit_quality`（commit の対象
# index）が共有し、`bash_config_protection` は wrapper の実行位置を探すときに値を取るオプションとしても使う。
# 片方だけが知っていると、``env -C plugins rm ../ruff.toml`` が片方を素通りする。
# 短形式は結合形（``-Cplugins``）も指定になり、長形式は ``=`` か単独のときだけ指定になる。
WRAPPER_CHDIR_SHORT_OPTIONS = ("-C", "-D")
WRAPPER_CHDIR_LONG_OPTION = "--chdir"
WRAPPER_CHDIR_OPTION_RE = re.compile(rf"^(?:{'|'.join(WRAPPER_CHDIR_SHORT_OPTIONS)}|{WRAPPER_CHDIR_LONG_OPTION}(?:=|$))")

# git の値を必ず取る short オプション文字（global の ``-C <dir>`` / ``-c <key=value>`` と、commit の ``-m`` /
# ``-F`` / ``-C`` / ``-c`` / ``-t``）。結合形（``-mmsg``）なら残りが値、クラスタ末尾（``-am``）なら次トークンが値。
# `block_no_verify`（バイパスフラグの検出）と `commit_risk`（``-a`` の検出）が共有する。片方だけが値と読むと、
# ``-mAll`` の後ろをフラグと読むかがフック間でずれる。
GIT_VALUE_SHORT_OPTIONS = frozenset("CcmFt")

# git の値が省略可能で、指定するなら結合形だけの short オプション文字（``-uno`` / ``-Skeyid``）。
# 次トークンは値として消費しない。`GIT_VALUE_SHORT_OPTIONS` と同じく 2 つのフックが共有する。
GIT_OPTIONAL_VALUE_SHORT_OPTIONS = frozenset("uS")

# sed の GNU 長形式 in-place フラグの名前（長形式の短縮は意図して組み立てる回避形なので見ない）。
_LONG_INPLACE_OPTION_NAME = "in-place"


def is_inplace_edit_flag(token: str) -> bool:
    """トークンが in-place 編集フラグ（`sed -i` / `perl -0pi` / `sed --in-place`）かを判定します。

    単一ダッシュの短形式は結合位置を問わず `i` を含むかで判定する（`-i` / `-i.bak` /
    `-0pi` / `-ni`）。長形式は名前部分が `_LONG_INPLACE_OPTION_NAME` と一致するものに
    限る（`--in-place=.bak` は名前部分だけを見る）。

    `bash_config_protection` と `pre_bash_commit_quality` が共有する。どちらかが
    長形式を除外すると ``sed --in-place ... && git commit -am x`` でガードが不発になる。

    Args:
        token: 引数トークン列の 1 トークンです。

    Returns:
        in-place 編集を指示するフラグなら True。
    """
    if not token.startswith("-") or token == "-":
        return False
    if token.startswith("--"):
        return token[2:].split("=", 1)[0] == _LONG_INPLACE_OPTION_NAME
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


@memoized_in_scan
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
    """
    try:
        return (Path.cwd() / _expand_leading_tilde(raw_path)).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        # ValueError は単独サロゲート（JSON の \ud800）の ``UnicodeEncodeError`` と
        # NUL 混入。入力側が任意に作れるため、貫通させると呼び出し側の保護フックが
        # exit 1（＝ PreToolUse では non-blocking でツール実行）で落ち、後続トークンの
        # 保護対象書き込みの検査ごと失われる。
        return None


@memoized_in_scan
def match_path_segments(file_path: str, candidates: tuple[tuple[str, ...], ...]) -> str | None:
    """パスの連続した要素が候補のいずれかに一致するか判定する。

    symlink は解決してから判定し、解決不能なら生パスで判定する。比較は大小無視で行い
    （`.GIT/HOOKS` は APFS / NTFS では `.git/hooks` そのものを指す）、返す表示名は畳む前の
    綴りを使う。候補の要素 ``*`` は任意の 1 要素に一致する。

    Args:
        file_path: 検査対象の生パス文字列。
        candidates: 連続一致を探す要素列の候補（畳む前の綴り）。

    Returns:
        一致した候補の表示名（``/`` 区切り）。一致しなければ None。
    """
    resolved = resolve_effective_target(file_path)
    parts = resolved.parts if resolved is not None else Path(file_path).parts
    segments = match_folded_path_window(parts, candidates)
    return None if segments is None else "/".join(segments)


def effective_basename(raw_path: str) -> str:
    """symlink を解決した実体 basename を返します。

    raw path の basename だけを見ると ``alias -> pyproject.toml`` のような symlink 経由の
    書込みがすり抜ける。解決不能（壊れた・循環した symlink 等）な場合は raw path の
    basename にフォールバックする。解決不能を deny に倒すと正当な編集まで巻き込むため。

    Args:
        raw_path: 検査対象の生パス文字列。

    Returns:
        判定に使う basename。
    """
    resolved = resolve_effective_target(raw_path)
    return resolved.name if resolved is not None else basename(raw_path)


def _flush_or_discard(stream: TextIO | None) -> None:
    """ストリームを今 flush し、書けなければ fd を devnull へ付け替えて残りを捨てる。

    パイプへの書込みはバッファに溜まるだけで、読み手が居なくても ``write`` は失敗しない。
    失敗はインタプリタ終了時の flush で起き、Python はそこで終了コードを 120 に書き換える。
    deny の exit 2 が 120 になると、PreToolUse では non-blocking error ＝ ツール実行へ反転する。
    ここで flush して失敗を握り、fd を devnull へ向ければ、終了時の flush は残りを devnull へ
    書いて成功し、終了コードは変わらない。

    Args:
        stream: flush する出力ストリーム。fd 1 / 2 が閉じた状態で起動したときは None。
    """
    try:
        stream.flush()
    except Exception:
        with contextlib.suppress(Exception):
            fd = stream.fileno()
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, fd)
            os.close(devnull)


def emit_block_output(reason: str) -> int:
    """ツール実行ブロックを host 非依存の合併出力で stdout/stderr に書き出す。

    ツール実行をブロックするフックは、このヘルパの戻り値（常に 2）を終了コードとして
    返すこと。出力層の例外（host がパイプを閉じたときの BrokenPipeError 等）が抜けると
    exit 1 になり、PreToolUse では non-blocking error ＝ ツールが実行されて deny が
    allow へ反転する。出力の唯一の口であるここで握り潰し、判定は exit code で運ぶ
    （docs/adr/hook-failure-direction.md）。書いた出力はここで flush する。終了時の flush に
    任せると、そこでの失敗が終了コードを 120 に変える（`_flush_or_discard`）。

    Args:
        reason: ブロック理由（ユーザー / エージェントに提示される）。

    Returns:
        フックが返すべき終了コード。
    """
    with contextlib.suppress(Exception):
        sys.stdout.write(adapt_deny_output(reason))
        sys.stderr.write(reason + "\n")
    _flush_or_discard(sys.stdout)
    _flush_or_discard(sys.stderr)
    return 2

