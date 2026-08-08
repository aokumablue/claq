"""
フックとスクリプト向けクロスプラットフォームユーティリティ関数。
Windows・macOS・Linux で動作する。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from bluecore.lib.constants import BASE_DIR_NAME

# プラットフォーム検出
IS_WINDOWS = platform.system() == "Windows"
IS_MACOS = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"

SESSION_DATA_DIR_NAME = "session-data"

WINDOWS_RESERVED_SESSION_IDS = frozenset(
    [
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
    ]
)


def get_home_dir() -> Path:
    """ユーザーのホームディレクトリを取得する（クロスプラットフォーム）。"""
    for env_name in ("BLUECORE_HOME", "HOME", "USERPROFILE"):
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


def get_bluecore_dir() -> Path:
    """bluecore の保存ディレクトリを取得する。"""
    return get_home_dir() / BASE_DIR_NAME


def get_projects_dir() -> Path:
    """プロジェクト保存ディレクトリを取得する。"""
    return get_bluecore_dir() / "projects"


def get_registry_file() -> Path:
    """プロジェクトレジストリファイルを取得する。"""
    return get_bluecore_dir() / "projects.json"


def get_sessions_dir() -> Path:
    """セッションディレクトリを取得する。"""
    return get_bluecore_dir() / SESSION_DATA_DIR_NAME


def get_session_search_dirs() -> list[Path]:
    """セッション検索対象ディレクトリのリストを返す。

    現在は sessions_dir のみだが、将来的に複数パスに拡張できるよう
    リストで返す。
    """
    return [get_sessions_dir()]


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
    try:
        path.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        # 他プロセスとの競合によるレースコンディションは許容
        pass
    return path


def get_date_string() -> str:
    """現在日付を YYYY-MM-DD 形式で取得する。"""
    return datetime.now().strftime("%Y-%m-%d")


def get_time_string() -> str:
    """現在時刻を HH:MM 形式で取得する。"""
    return datetime.now().strftime("%H:%M")


def get_datetime_string() -> str:
    """現在日時を YYYY-MM-DD HH:MM:SS 形式で取得する。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_git_repo_name() -> str | None:
    """git リポジトリ名を取得する。"""
    result = run_command("git rev-parse --show-toplevel")
    if not result["success"]:
        return None
    return Path(result["output"]).name


def get_git_user_name() -> str:
    """git config の user.name を取得する。

    Returns:
        取得できれば user.name の値、未設定または失敗時は空文字列。
    """
    result = run_command("git config --get user.name")
    if not result["success"]:
        return ""
    return result["output"].strip()


def get_project_name() -> str | None:
    """git リポジトリまたは現在ディレクトリからプロジェクト名を取得する。"""
    repo_name = get_git_repo_name()
    if repo_name:
        return repo_name
    cwd = Path.cwd()
    return cwd.name if cwd.name else None


def sanitize_session_id(raw: str | None) -> str | None:
    """
    セッション用ファイル名セグメントとして使えるよう文字列をサニタイズする。

    不正文字をハイフンに置換し、連続記号を圧縮し、
    先頭/末尾のハイフンを除去し、先頭ドットも取り除いて隠しディレクトリ名が
    「.claude」のような名前を「claude」に適切に変換する。

    非ASCIIのみの入力には安定した8文字ハッシュを付与し、異なる名前が
    同じフォールバックセッションIDに潰れないようにする。混在文字種の入力は
    ASCII部分を保持しつつ短いハッシュ接尾辞で識別可能にする。
    """
    if not raw or not isinstance(raw, str):
        return None

    has_non_ascii = any(ord(char) > 0x7F for char in raw)
    normalized = raw.lstrip(".")

    # 不正文字をハイフンに置換
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "-", normalized)
    # 連続するハイフンを圧縮
    sanitized = re.sub(r"-{2,}", "-", sanitized)
    # 先頭/末尾のハイフンを除去
    sanitized = sanitized.strip("-")

    if sanitized:
        suffix = hashlib.sha256(normalized.encode()).hexdigest()[:6]
        if sanitized.upper() in WINDOWS_RESERVED_SESSION_IDS:
            return f"{sanitized}-{suffix}"
        if not has_non_ascii:
            return sanitized
        return f"{sanitized}-{suffix}"

    # 非ASCIIのみ、または記号/空白のみの場合
    # Python の re は \p{P} をサポートしないため、明示的な句読点判定を使う
    import unicodedata

    meaningful = "".join(c for c in normalized if not c.isspace() and unicodedata.category(c)[0] != "P")
    if not meaningful:
        return None

    return hashlib.sha256(normalized.encode()).hexdigest()[:8]


def get_session_id_short(fallback: str = "default") -> str:
    """
    CLAUDE_SESSION_ID 環境変数から短いセッションIDを取得する。
    末尾8文字を返し、取得できない場合はサニタイズ済みプロジェクト名、最後に「default」を使う。
    """
    session_id = os.environ.get("CLAUDE_SESSION_ID", "")
    if session_id:
        sanitized = sanitize_session_id(session_id[-8:])
        if sanitized:
            return sanitized

    return sanitize_session_id(get_project_name()) or sanitize_session_id(fallback) or "default"


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """グロブパターンを正規表現オブジェクトに変換する。"""
    regex_pattern = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
    return re.compile(f"^{regex_pattern}$")


def _is_within_max_age(mtime_ms: float, max_age: float) -> bool:
    """mtime（ミリ秒）が max_age 日以内かどうかを判定する。"""
    import time

    age_in_days = (time.time() * 1000 - mtime_ms) / (1000 * 60 * 60 * 24)
    return age_in_days <= max_age


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

    regex = _glob_to_regex(pattern)
    results: list[dict[str, Any]] = []

    def search_dir(current_dir: Path) -> None:
        """ディレクトリを走査し、条件に合致するファイルを results に追加する。"""
        try:
            for entry in current_dir.iterdir():
                if entry.is_file() and regex.match(entry.name):
                    try:
                        mtime = entry.stat().st_mtime * 1000  # JS と同様にミリ秒へ変換
                        if max_age is not None and not _is_within_max_age(mtime, max_age):
                            continue
                        results.append({"path": str(entry), "mtime": mtime})
                    except OSError:
                        continue  # iterdir と stat の間でファイルが削除された
                elif entry.is_dir() and recursive:
                    search_dir(entry)
        except PermissionError:
            pass  # 権限エラーは無視

    search_dir(dir_path)
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

        if data.strip():
            return json.loads(data)
        return {}
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
    # コマンド名を検証
    if not re.match(r"^[a-zA-Z0-9_.-]+$", cmd):
        return False

    try:
        if IS_WINDOWS:
            result = subprocess.run(
                ["where", cmd],
                capture_output=True,
                check=False,
            )
        else:
            result = subprocess.run(
                ["which", cmd],
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
    # 各種 ANSI エスケープシーケンスに一致させる
    return re.sub(
        r"\x1b(?:\[[0-9;?]*[A-Za-z]|\][^\x07\x1b]*(?:\x07|\x1b\\)|\([A-Z]|[A-Z])",
        "",
        text,
    )


