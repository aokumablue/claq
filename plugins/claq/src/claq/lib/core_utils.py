"""フックとスクリプト向けクロスプラットフォームユーティリティ関数。

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

# CSI（ESC[ … <letter>）・OSC（ESC] … BEL/ST）・文字セット選択（ESC(B）・単独 ESC + 1 文字。
_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-9;?]*[A-Za-z]|\][^\x07\x1b]*(?:\x07|\x1b\\)|\([A-Z]|[A-Z])")


def get_home_dir() -> Path:
    """ユーザーのホームディレクトリを取得する（``CLAQ_HOME`` → ``HOME`` → ``USERPROFILE`` → 既定）。

    Raises:
        OSError: ホームを決められない場合。cwd へ逃げると、リポジトリに置かれた
            ``.claq/mem.db`` や transcript を自分のものとして読んでしまう。``OSError`` にするのは、
            書き込み先を扱う呼び出し元が既に ``OSError`` を「書けない」として扱っているため。
    """
    for env_name in ("CLAQ_HOME", "HOME", "USERPROFILE"):
        raw = os.environ.get(env_name)
        if raw:
            return Path(raw).expanduser()
    try:
        return Path.home()
    except RuntimeError as exc:
        raise OSError(f"ホームディレクトリを決められません: {exc}") from exc


def get_plugin_root() -> Path:
    """このプラグインのソースルート（``<root>/src/claq/lib`` の 3 つ上）を返す。

    ``CLAUDE_PLUGIN_ROOT`` は Bash tool の環境変数に乗らないため md 側からは参照できないが、
    hook プロセスの中では自分自身のファイル位置から確実に導ける。``launcher.REPO_ROOT``
    と同じ値になる。
    """
    return Path(__file__).resolve().parents[3]


def get_claq_dir() -> Path:
    """claq の保存ディレクトリを取得する。"""
    return get_home_dir() / BASE_DIR_NAME


def get_sessions_dir() -> Path:
    """セッションディレクトリを取得する。"""
    return get_claq_dir() / SESSION_DATA_DIR_NAME


def ensure_dir(dir_path: str | Path) -> Path:
    """ディレクトリを親ごと作成して Path を返す。

    既にディレクトリ（ディレクトリへの symlink を含む）なら何もしない。他プロセスとの
    作成競合は ``mkdir(exist_ok=True)`` が吸収する（競合に負けた側の
    ``FileExistsError`` は、対象がディレクトリなら pathlib が握る）ので、ここでは扱わない。

    Raises:
        FileExistsError: ``dir_path`` が通常ファイル・ファイルへの symlink・壊れた symlink
            など、ディレクトリ以外として存在する場合。
        NotADirectoryError: 祖先が通常ファイルの場合。
        OSError: ディレクトリを作成できない場合（例: 権限不足）
    """
    path = Path(dir_path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_private_dir(dir_path: str | Path) -> Path:
    """ディレクトリを作成し、パーミッションを 0700 に絞る。

    **作成そのものを、欠けている祖先ごと根から順に 0700 で行う**。作成と ``chmod`` を
    分けると、その間 ``~/.claq`` は umask 既定（通常 0755）で他 OS ユーザーから
    ``opendir`` でき、この窓の間に取得された fd は後続の ``chmod`` では失効しない。
    ``mkdir(mode=0o700, parents=True)`` は親に mode を適用しない（pathlib の仕様）ため、
    1 段ずつ作る。既存の祖先は ``chmod`` しない（``/`` や ``$HOME`` を 0700 にしない）。
    0700 は group/other のビットを立てないので umask で緩まない。

    ``chmod`` は既に 0755 のディレクトリとして存在する対象を締め直す是正用。
    ``dir_path`` が通常ファイルなど非ディレクトリなら、``chmod`` に進む前に
    ``FileExistsError`` で止まる（ファイルの mode を 0700 に変えない）。
    作成競合は ``mkdir(exist_ok=True)`` が吸収するため、ここでは握らない。``dir_path`` が
    ``get_claq_dir()`` 配下なら ``~/.claq`` 自身も 0700 にする。

    Windows では ``chmod`` が読み取り専用属性しか動かさず、DACL は変わらない。
    ``%USERPROFILE%`` 配下は既定でそのユーザー（と SYSTEM / Administrators）だけに
    許可されており、docs/adr/shell-analysis-boundary.md の脅威モデル（同一 OS ユーザーの
    敵対的回避は非対象）に対して POSIX の 0700 と同水準になるため、``icacls`` 等は呼ばない。

    Raises:
        FileExistsError: ``dir_path`` またはその祖先が通常ファイル・ファイルへの symlink・
            壊れた symlink など、ディレクトリ以外として存在する場合（``chmod`` しない）。
            祖先も 1 段ずつ ``mkdir`` するため、``ensure_dir`` と違い祖先が
            ファイルでも ``NotADirectoryError`` ではなくこちらになる。
        OSError: 作成も chmod もできない場合。
    """
    path = Path(dir_path)
    for target in (*reversed(path.parents), path):
        target.mkdir(mode=0o700, exist_ok=True)
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

    ``os.getuid()`` は Windows に無く、``mem promote`` が「active にしたのに監査ログを
    書けず非 0 終了」という部分成功になりうるため ``getpass.getuser()`` を使う。
    どこからも解決できない環境（Python 3.13+ は ``OSError``）では ``"unknown"`` を返す
    — 監査ログ用の識別子が取れないことを理由に本処理を失敗させない。
    """
    try:
        return getpass.getuser()
    except (OSError, KeyError):
        return "unknown"


def get_datetime_string() -> str:
    """現在日時を YYYY-MM-DD HH:MM:SS 形式で取得する。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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
    """文字列からすべての ANSI エスケープシーケンスを除去する。文字列以外は空文字列を返す。"""
    if not isinstance(text, str):
        return ""
    return _ANSI_ESCAPE.sub("", text)
