"""
フックとスクリプト向けクロスプラットフォームユーティリティ関数。
Windows・macOS・Linux で動作する。
"""

from __future__ import annotations

import getpass
import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from ple4.lib.constants import BASE_DIR_NAME

IS_WINDOWS = platform.system() == "Windows"
IS_MACOS = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"

SESSION_DATA_DIR_NAME = "session-data"

def get_home_dir() -> Path:
    """ユーザーのホームディレクトリを取得する（クロスプラットフォーム）。"""
    for env_name in ("PLE4_HOME", "HOME", "USERPROFILE"):
        raw = os.environ.get(env_name)
        if raw:
            return Path(raw).expanduser()

    try:
        return Path.home()
    except RuntimeError:
        return Path.cwd()


def get_claude_dir() -> Path:
    """Claude の設定ディレクトリを取得する。"""
    return get_home_dir() / ".claude"


def get_ple4_dir() -> Path:
    """ple4 の保存ディレクトリを取得する。"""
    return get_home_dir() / BASE_DIR_NAME


def get_sessions_dir() -> Path:
    """セッションディレクトリを取得する。"""
    return get_ple4_dir() / SESSION_DATA_DIR_NAME


def get_learned_skills_dir() -> Path:
    """学習済みスキルのディレクトリを取得する。"""
    return get_claude_dir() / "skills" / "learned"


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

    ``dir_path`` が ``get_ple4_dir()`` 配下なら ``~/.ple4`` 自身も
    0700 にする。``mkdir(parents=True)`` だけだと親が umask 022 で 0755
    のまま残るため。既存の 0755 ディレクトリも締め直す。

    Args:
        dir_path: 作成または権限を締めるディレクトリ。

    Returns:
        対象ディレクトリの Path。

    Raises:
        OSError: 作成も chmod もできない場合。
    """
    path = Path(dir_path)
    _mkdir_exist_ok(path)
    path.chmod(0o700)
    ple4_dir = get_ple4_dir()
    try:
        path.resolve().relative_to(ple4_dir.resolve())
    except ValueError:
        return path
    ple4_dir.chmod(0o700)
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


def get_date_string() -> str:
    """現在日付を YYYY-MM-DD 形式で取得する。"""
    return datetime.now().strftime("%Y-%m-%d")


def get_datetime_string() -> str:
    """現在日時を YYYY-MM-DD HH:MM:SS 形式で取得する。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _mkdir_exist_ok(path: Path) -> None:
    """親ごとディレクトリを作る。他プロセスとの競合による FileExistsError は無視する。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        pass


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """グロブパターンを正規表現オブジェクトに変換する。"""
    regex_pattern = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
    return re.compile(f"^{regex_pattern}$")


def _is_within_max_age(mtime_ms: float, max_age: float) -> bool:
    """mtime（ミリ秒）が max_age 日以内かどうかを判定する。"""
    import time

    age_in_days = (time.time() * 1000 - mtime_ms) / (1000 * 60 * 60 * 24)
    return age_in_days <= max_age


def _append_if_fresh(entry: Path, max_age: float | None, results: list[dict[str, Any]]) -> None:
    """stat できたファイルを max_age 条件付きで results に追加する。"""
    try:
        mtime = entry.stat().st_mtime * 1000  # JS と同様にミリ秒へ変換
    except OSError:
        return
    if max_age is not None and not _is_within_max_age(mtime, max_age):
        return
    results.append({"path": str(entry), "mtime": mtime})


def _collect_matching_files(
    current_dir: Path,
    regex: re.Pattern[str],
    max_age: float | None,
    recursive: bool,
    results: list[dict[str, Any]],
) -> None:
    """ディレクトリを走査し、条件に合致するファイルを results に追加する。"""
    try:
        for entry in current_dir.iterdir():
            if entry.is_file() and regex.match(entry.name):
                _append_if_fresh(entry, max_age, results)
            elif entry.is_dir() and recursive:
                _collect_matching_files(entry, regex, max_age, recursive, results)
    except PermissionError:
        pass


def find_files(
    directory: str | Path,
    pattern: str,
    *,
    max_age: float | None = None,
    recursive: bool = False,
) -> list[dict[str, Any]]:
    """
    ディレクトリ内でパターンに一致するファイルを探す。

    Args:
        directory: 検索対象ディレクトリ
        pattern: ファイルパターン（例: "*.tmp", "*.md"）
        max_age: ファイルの最大経過日数（None は無制限）
        recursive: サブディレクトリも検索するか

    Returns:
        'path' と 'mtime' を持つ辞書のリスト（新しい順）
    """
    if not directory or not pattern:
        return []

    dir_path = Path(directory)
    if not dir_path.exists():
        return []

    results: list[dict[str, Any]] = []
    _collect_matching_files(dir_path, _glob_to_regex(pattern), max_age, recursive, results)
    results.sort(key=lambda x: x["mtime"], reverse=True)
    return results


async def read_stdin_json(*, timeout_ms: int = 5000, max_size: int = 1024 * 1024) -> dict[str, Any]:
    """
    stdin から JSON を読み込む（フック入力用）。

    Args:
        timeout_ms: タイムアウト（ミリ秒、デフォルト: 5000）
        max_size: 入力サイズ上限（バイト）

    Returns:
        解析済みJSONオブジェクト。stdin が空または不正なら空辞書
    """
    import asyncio

    try:
        # stdin に読み取り可能なデータがあるか確認
        if sys.stdin.isatty():
            return {}

        # run_in_executor は Future を返すので、タイムアウト時に coroutine を残さない。
        loop = asyncio.get_running_loop()
        data = await asyncio.wait_for(loop.run_in_executor(None, lambda: sys.stdin.read(max_size)), timeout=timeout_ms / 1000)

        if not data.strip():
            return {}
        return json.loads(data)
    except (TimeoutError, json.JSONDecodeError, OSError):
        return {}


def log(message: str) -> None:
    """stderr にログを出力する。"""
    print(message, file=sys.stderr)


def output(data: Any) -> None:
    """stdout に出力する（Claude に返される）。"""
    if isinstance(data, (dict, list)):
        print(json.dumps(data))
    else:
        print(data)


def read_file(file_path: str | Path) -> str | None:
    """テキストファイルを安全に読み込む。"""
    try:
        return Path(file_path).read_text(encoding="utf-8")
    except OSError:
        return None


def write_file(file_path: str | Path, content: str) -> None:
    """テキストファイルを書き込む。"""
    path = Path(file_path)
    ensure_dir(path.parent)
    path.write_text(content, encoding="utf-8")


def append_file(file_path: str | Path, content: str) -> None:
    """テキストファイルに追記する。"""
    path = Path(file_path)
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as f:
        f.write(content)


def command_exists(cmd: str) -> bool:
    """
    PATH 上にコマンドが存在するか確認する。

    Args:
        cmd: 確認するコマンド名（英数字・ハイフン・アンダースコア・ドットのみ）

    Returns:
        コマンドが存在すれば True、そうでなければ False
    """
    if not re.match(r"^[a-zA-Z0-9_.-]+$", cmd):
        return False

    lookup = "where" if IS_WINDOWS else "which"
    try:
        result = subprocess.run(
            [lookup, cmd],
            capture_output=True,
            check=False,
        )
        return result.returncode == 0
    except OSError:
        return False


# 安全なコマンド接頭辞の許可リスト
_ALLOWED_COMMAND_PREFIXES = ("git", "node", "npx", "which", "where")


def run_command(cmd: str | list[str], **kwargs: Any) -> dict[str, Any]:
    """
    コマンドを実行して出力を返す。

    Args:
        cmd: 実行するコマンド（文字列または引数リスト。信頼済み/ハードコード済みであるべき）
        **kwargs: subprocess.run に渡す追加引数

    Returns:
        'success'（bool）と'output'（str）を持つ辞書
    """
    if isinstance(cmd, str):
        # シェルのメタ文字を拒否（shell=False でも引数経由での注入を防ぐ）
        if re.search(r"[;|&\n`$]", cmd):
            return {"success": False, "output": "runCommand blocked: shell metacharacters not allowed"}
        cmd_list = cmd.split()
    else:
        cmd_list = list(cmd)

    if not cmd_list:
        return {"success": False, "output": "runCommand error: empty command"}

    if cmd_list[0] not in _ALLOWED_COMMAND_PREFIXES:
        return {"success": False, "output": "runCommand blocked: unrecognized command"}

    try:
        result = subprocess.run(
            cmd_list,
            shell=False,
            capture_output=True,
            text=True,
            **kwargs,
        )
        if result.returncode == 0:
            return {"success": True, "output": result.stdout.strip()}
        return {"success": False, "output": result.stderr or result.stdout}
    except OSError as e:
        return {"success": False, "output": str(e)}


def is_git_repo() -> bool:
    """現在ディレクトリが git リポジトリか確認する。"""
    return run_command("git rev-parse --git-dir")["success"]


def count_in_file(file_path: str | Path, pattern: str | re.Pattern[str]) -> int:
    """
    ファイル内のパターン出現回数を数える。

    Args:
        file_path: 対象ファイルのパス
        pattern: カウント対象パターン

    Returns:
        一致件数
    """
    content = read_file(file_path)
    if content is None:
        return 0

    try:
        if isinstance(pattern, re.Pattern):
            matches = pattern.findall(content)
        else:
            matches = re.findall(pattern, content)
        return len(matches)
    except re.error:
        return 0


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


