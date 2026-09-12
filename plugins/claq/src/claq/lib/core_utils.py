"""
フックとスクリプト向けクロスプラットフォームユーティリティ関数。
Windows・macOS・Linux で動作する。
"""

from __future__ import annotations

import getpass
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from claq.lib.constants import BASE_DIR_NAME

SESSION_DATA_DIR_NAME = "session-data"

def get_home_dir() -> Path:
    """ユーザーのホームディレクトリを取得する（クロスプラットフォーム）。"""
    for env_name in ("CLAQ_HOME", "HOME", "USERPROFILE"):
        raw = os.environ.get(env_name)
        if raw:
            return Path(raw).expanduser()

    try:
        return Path.home()
    except RuntimeError:
        return Path.cwd()


def get_plugin_root() -> Path:
    """このプラグインのソースルート（``<root>/src/claq/lib`` の 3 つ上）を返す。

    ``CLAUDE_PLUGIN_ROOT`` は Bash tool の環境変数に乗らない（docs/adr/plugin-root-resolution.md）ため、
    md 側からは参照できない。一方、hook プロセスの中では自分自身のファイル位置
    から確実に導ける。``launcher.REPO_ROOT`` と同じ値になる。

    Args:
        なし

    Returns:
        プラグインルートの絶対パス。

    Raises:
        例外は発生しません。
    """
    return Path(__file__).resolve().parents[3]


def get_claq_dir() -> Path:
    """claq の保存ディレクトリを取得する。"""
    return get_home_dir() / BASE_DIR_NAME


def get_sessions_dir() -> Path:
    """セッションディレクトリを取得する。"""
    return get_claq_dir() / SESSION_DATA_DIR_NAME


def ensure_dir(dir_path: str | Path) -> Path:
    """
    ディレクトリが存在することを保証する（なければ作成）。

    Args:
        dir_path: 作成するディレクトリパス

    Returns:
        Pathオブジェクトとしてのディレクトリパス

    Raises:
        OSError: ディレクトリを作成できない場合（例: 権限不足）
    """
    path = Path(dir_path)
    _mkdir_exist_ok(path)
    return path


def ensure_private_dir(dir_path: str | Path) -> Path:
    """ディレクトリを作成し、パーミッションを 0700 に絞る。

    ``dir_path`` が ``get_claq_dir()`` 配下なら ``~/.claq`` 自身も
    0700 にする。``mkdir(parents=True)`` だけだと親が umask 022 で 0755
    のまま残るため。既存の 0755 ディレクトリも締め直す。

    **作成そのものを 0700 で行う**（``_mkdir_private_exist_ok``）。作成と
    ``chmod`` を分けると、その間 ``~/.claq`` は umask 既定（通常 0755）で
    他 OS ユーザーから ``opendir`` できる。POSIX の権限検査はディレクトリを
    open した時点でしか行われないため、この窓の間に取得された fd は後続の
    ``chmod`` では失効しない。``chmod`` は「既に 0755 で存在するディレクトリ
    を締め直す」是正用として残す。

    Windows では ``chmod`` が読み取り専用属性しか動かさず、DACL は変わらない
    （release-verify 2026-09-03 の P1-008）。ここで ``icacls`` や
    ``SetNamedSecurityInfo`` を呼ぶことはしない: ``%USERPROFILE%`` 配下は
    既定でそのユーザー（と SYSTEM / Administrators）だけに許可されており、
    docs/adr/shell-analysis-boundary.md が定める脅威モデル（同一 OS ユーザーの敵対的回避は非対象）に
    対しては POSIX の 0700 と同水準になる。Administrators が読める点は、
    POSIX で root が 0700 を読めるのと対応する。外部プロセス起動または
    ctypes 依存を増やして得られる差が無いため、実装しない。

    Args:
        dir_path: 作成または権限を締めるディレクトリ。

    Returns:
        対象ディレクトリの Path。

    Raises:
        OSError: 作成も chmod もできない場合。
    """
    path = Path(dir_path)
    _mkdir_private_exist_ok(path)
    path.chmod(0o700)
    claq_dir = get_claq_dir()
    try:
        path.resolve().relative_to(claq_dir.resolve())
    except ValueError:
        return path
    claq_dir.chmod(0o700)
    return path


def actor_identity() -> str:
    """監査ログ用の実行ユーザー識別子を返す（クロスプラットフォーム）。

    ``os.getuid()`` は POSIX にしか存在せず、Windows では ``AttributeError``
    になる。``mem promote`` は DB 更新の**後**にこれを呼んでいたため、
    Windows では「カードは active になったのに監査ログを書けず非 0 終了」と
    いう部分成功になりえた（release-verify 2026-09-03 の P1-005）。

    ``getpass.getuser()`` は POSIX では ``LOGNAME``/``USER``/``LNAME``/
    ``USERNAME`` と ``pwd`` を、Windows では ``USERNAME`` を見るため、
    3 プラットフォームで同じ 1 本の実装になる。どこからも解決できない環境
    （Python 3.13+ は ``OSError``）では ``"unknown"`` を返す — 監査ログの
    ための識別子であって、これが取れないことを理由に本処理を失敗させない。

    Args:
        なし

    Returns:
        ユーザー名。解決できない場合は ``"unknown"``。

    Raises:
        例外は発生しません。
    """
    try:
        return getpass.getuser()
    except (OSError, KeyError):
        return "unknown"


def get_datetime_string() -> str:
    """現在日時を YYYY-MM-DD HH:MM:SS 形式で取得する。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _mkdir_exist_ok(path: Path) -> None:
    """親ごとディレクトリを作る。他プロセスとの競合による FileExistsError は無視する。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        pass


def _mkdir_private_exist_ok(path: Path) -> None:
    """欠けている祖先ごと、ディレクトリを最初から 0700 で作る。

    ``mkdir(mode=0o700, parents=True)`` では**親に mode が適用されない** —
    pathlib は欠けている親を既定モード（umask 適用後は通常 0755）で作る仕様で、
    ``mode`` は末端にしか効かない（実測: umask 022 で ``a/b`` を作ると
    ``a`` が 0755、``b`` が 0700）。呼び出しの大半は ``~/.claq/<name>`` の形
    なので、末端だけ 0700 にしても ``~/.claq`` 自身に窓が残る。そこが
    ``mem.db`` を持つディレクトリなので、根から順に 0700 で作る。

    既存のディレクトリに対しては ``exist_ok=True`` が no-op になるだけで
    ``chmod`` はしない（``/`` や ``$HOME`` を 0700 にはしない）。0700 は
    group/other のビットを 1 つも立てないため、``mode & ~umask`` はどの
    umask でも 0700 のまま — umask による緩みは起きない。

    Args:
        path: 作成するディレクトリ。

    Returns:
        なし。

    Raises:
        OSError: 作成に失敗した場合（``FileExistsError`` は競合として無視）。
    """
    for target in (*reversed(path.parents), path):
        try:
            target.mkdir(mode=0o700, exist_ok=True)
        except FileExistsError:
            pass


def log(message: str) -> None:
    """stderr にログを出力する。"""
    print(message, file=sys.stderr)


def append_file(file_path: str | Path, content: str) -> None:
    """テキストファイルに追記する。"""
    path = Path(file_path)
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as f:
        f.write(content)


def strip_ansi(text: str) -> str:
    """
    文字列からすべての ANSI エスケープシーケンスを除去する。

    対応対象:
    - CSI シーケンス: ESC[ … <letter>（色、カーソル移動、消去など）
    - OSC シーケンス: ESC] … BEL/ST（ウィンドウタイトル、ハイパーリンク）
    - 文字セット選択: ESC(B
    - 単独 ESC + 1文字: ESC <letter>（例: 逆インデックスの ESC M）

    Args:
        text: ANSIコードを含む可能性のある入力文字列

    Returns:
        すべてのエスケープシーケンスを除去した文字列
    """
    if not isinstance(text, str):
        return ""
    return re.sub(
        r"\x1b(?:\[[0-9;?]*[A-Za-z]|\][^\x07\x1b]*(?:\x07|\x1b\\)|\([A-Z]|[A-Z])",
        "",
        text,
    )


