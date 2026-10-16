"""claq フック実装の共通ユーティリティ。

出力書き込み、保護対象の判定の共有関数を提供する。シェルコマンドの解析は ``shell_tokens`` が持つ。
stdin の読み取りは ``stdin_io`` が持つ。
"""

from __future__ import annotations

import contextlib
import functools
import subprocess
import sys
from pathlib import Path

from claq.hooks.output_adapter import adapt_context_output, emit_block
from claq.lib.core_utils import get_home_dir


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
