"""claq フック実装の共通ユーティリティ。

フック出力の書き込み、保護対象名の正規化と判定、1 回の判定の中だけのメモ化、
リポジトリルートの解決を兄弟フックで共有する。シェルコマンドの解析と実行トークン名の
正規化は ``shell_tokens``、stdin の読み取りは ``stdin_io`` が持つ。
"""

from __future__ import annotations

import contextlib
import functools
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

from claq.hooks.output_adapter import adapt_context_output, adapt_deny_output
from claq.hooks.shell_tokens import basename_any_separator, normalize_executable_name
from claq.lib.core_utils import get_home_dir

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

    Raises:
        例外は発生しません。
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    top = os.fsdecode(result.stdout).strip()
    return Path(top) if result.returncode == 0 and top else None


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
    name = basename_any_separator(path).split(":", 1)[0]
    return name.rstrip(". \t").casefold()


def fold_path_parts(parts: tuple[str, ...]) -> tuple[str, ...]:
    """パス要素列の各要素を `normalize_protected_name` で畳み、大小を無視して比べられる形にする。

    Args:
        parts: 畳むパス要素列。

    Returns:
        畳んだ要素列。

    Raises:
        例外は発生しません。
    """
    return tuple(normalize_protected_name(part) for part in parts)


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

    Raises:
        例外は発生しません。
    """
    folded_parts = fold_path_parts(parts)
    for segments in candidates:
        folded_segments = fold_path_parts(segments)
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

# in-place 編集フラグ（`is_inplace_edit_flag`）を**伴うときだけ**引数のファイルを書き換えるコマンド。
# `ruby -pi` は `perl -pi` と同じ in-place 編集。`python3 -c` のような任意インタプリタは非目標で、
# ここには入れない。フラグ無しでも書き込むコマンド（`ed` 等）をここへ置くとフラグ判定に落ちて
# allow になるため、`ALWAYS_MUTATING_EDIT_EXECUTABLES` に置く。
# `bash_config_protection` と `pre_bash_commit_quality` が共有する。
INPLACE_EDIT_EXECUTABLES = frozenset({"sed", "perl", "ruby"})

# カレントディレクトリを移動するコマンド。これらが現れたコマンドでは、cwd 基準の
# 相対パス解決が実行時の位置とずれるため repo スコープ判定を信用しない。
# `bash_config_protection` と `pre_bash_commit_quality` が共有する。
DIRECTORY_CHANGE_COMMANDS = frozenset({"cd", "pushd", "popd", "chdir"})

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


def effective_basename(raw_path: str) -> str:
    """symlink を解決した実体 basename を返します。

    raw path の basename だけを見ると ``alias -> pyproject.toml`` のような symlink 経由の
    書込みがすり抜ける。解決不能（壊れた・循環した symlink 等）な場合は raw path の
    basename にフォールバックする。解決不能を deny に倒すと正当な編集まで巻き込むため。

    Args:
        raw_path: 検査対象の生パス文字列。

    Returns:
        判定に使う basename。

    Raises:
        例外は発生しません。
    """
    resolved = resolve_effective_target(raw_path)
    return resolved.name if resolved is not None else basename(raw_path)


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
    with contextlib.suppress(Exception):
        write_stdout(adapt_deny_output(reason))
        write_stderr(reason + "\n")
    return 2


def print_session_start_output(additional_context: str) -> None:
    """SessionStart 用のフック出力を stdout に書き出す。

    Args:
        additional_context: コンテキストに注入する追加文字列。

    Raises:
        OSError: パイプが閉じている場合（BrokenPipeError を含む）。
        UnicodeEncodeError: 出力先のエンコーディングで表現できない場合。
    """
    print(adapt_context_output("SessionStart", additional_context))
