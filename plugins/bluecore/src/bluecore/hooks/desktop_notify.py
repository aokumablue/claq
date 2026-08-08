#!/usr/bin/env python3
"""
デスクトップ通知フック (Stop)。

Claudeが応答を完了したときにタスクサマリーを含むネイティブ
デスクトップ通知を送信します。サポート環境:
  - macOS: osascript (ネイティブ)
  - WSL: PowerShell 7またはWindows PowerShell + BurntToastモジュール
"""

from __future__ import annotations

import math
import os
import subprocess
import time
from pathlib import Path

from bluecore.hooks.hook_common import parse_json_object, read_raw_stdin
from bluecore.lib.core_utils import IS_LINUX, IS_MACOS, log
from bluecore.lib.slim_text import compact_line, first_meaningful_line

TITLE = "通知"
MAX_BODY_LENGTH = 100

# デスクトップ通知処理全体（PowerShell探索 + 通知送信）に許容する既定の予算秒数。
# 呼び出し元のhookタイムアウトをブロックしないよう、BLUECORE_HOOK_TIMEOUT（既定590秒）
# とは別に、通知処理専用の短い予算をここで管理する。
DEFAULT_NOTIFICATION_TIMEOUT = 10.0

# PowerShell候補1件のプローブに使う最大秒数。予算が十分残っていてもこれを超えない。
POWERSHELL_PROBE_TIMEOUT = 3.0

# メモ化されたWSL検出
_is_wsl: bool | None = None


def is_wsl() -> bool:
    """WSL上で実行されているかチェックします。"""
    global _is_wsl

    if _is_wsl is not None:
        return _is_wsl

    if not IS_LINUX:
        _is_wsl = False
        return _is_wsl

    try:
        version_content = Path("/proc/version").read_text(encoding="utf-8").lower()
        _is_wsl = "microsoft" in version_content
    except (OSError, UnicodeDecodeError):
        _is_wsl = False

    return _is_wsl


def _notification_timeout() -> float:
    """通知処理全体に許容する予算秒数を環境変数から解決します。

    Args:
        なし

    Returns:
        BLUECORE_DESKTOP_NOTIFY_TIMEOUT が正の有限数値ならその秒数、
        未設定・無効値なら既定の DEFAULT_NOTIFICATION_TIMEOUT 秒。

    Raises:
        例外は発生しません。
    """
    raw = os.environ.get("BLUECORE_DESKTOP_NOTIFY_TIMEOUT")
    if raw:
        try:
            value = float(raw)
        except ValueError:
            return DEFAULT_NOTIFICATION_TIMEOUT
        if value > 0 and math.isfinite(value):
            return value
    return DEFAULT_NOTIFICATION_TIMEOUT


def _remaining_timeout(deadline: float) -> float:
    """指定したdeadline（time.monotonic基準）までの残り秒数を返します。

    Args:
        deadline: time.monotonic()と同じ基準の締切時刻（秒）。

    Returns:
        残り秒数。deadlineを過ぎている場合は0.0。

    Raises:
        例外は発生しません。
    """
    return max(0.0, deadline - time.monotonic())


def find_powershell(deadline: float | None = None) -> str | None:
    """WSL上で利用可能なPowerShell実行ファイルをdeadline内で探します。

    候補への1回のプローブは最大でもPOWERSHELL_PROBE_TIMEOUT秒に制限され、
    かつdeadlineまでの残り時間を超えないよう動的に配分されます。
    残り時間が枯渇した時点で以降の候補プローブは打ち切られます。

    Args:
        deadline: time.monotonic()と同じ基準の締切時刻（秒）。Noneの場合は
            呼び出し時点からDEFAULT_NOTIFICATION_TIMEOUT秒後を締切とします。

    Returns:
        最初に応答したPowerShell実行ファイルのパス。全候補が失敗した場合はNone。

    Raises:
        例外は発生しません。
    """
    if deadline is None:
        deadline = time.monotonic() + DEFAULT_NOTIFICATION_TIMEOUT

    candidates = [
        "pwsh.exe",  # WSL interopがWindows PATHから解決
        "powershell.exe",  # Windows PowerShell用のWSL interop
        "/mnt/c/Program Files/PowerShell/7/pwsh.exe",  # PowerShell 7 (デフォルトインストール)
        "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",  # Windows PowerShell
    ]

    for path in candidates:
        timeout = min(POWERSHELL_PROBE_TIMEOUT, _remaining_timeout(deadline))
        if timeout <= 0:
            break
        try:
            result = subprocess.run(
                [path, "-Command", "exit 0"],
                capture_output=True,
                timeout=timeout,
            )
            if result.returncode == 0:
                return path
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue

    return None


def _decode_stderr(stderr: bytes | str | None) -> str | None:
    """subprocessのstderrをUTF-8としてデコードします。

    Windows環境のPowerShell出力がUTF-8以外のコードページで書かれている場合でも
    デコード例外を送出しないよう、不正なバイト列は置換文字で継続します。
    bytes以外（str/None）はそのまま返します。
    """
    if isinstance(stderr, bytes):
        return stderr.decode("utf-8", errors="replace")
    return stderr


def notify_windows(pwsh_path: str, title: str, body: str, *, timeout: float = DEFAULT_NOTIFICATION_TIMEOUT) -> dict:
    """PowerShell BurntToast経由でWindowsトースト通知を送信します。

    Args:
        pwsh_path: 使用するPowerShell実行ファイルのパス。
        title: 通知タイトル。
        body: 通知本文。
        timeout: subprocess呼び出しの最大待機秒数。既定はDEFAULT_NOTIFICATION_TIMEOUT。

    Returns:
        'success' (bool)と'reason' (str|None)を含む辞書。

    Raises:
        例外は発生しません（内部でTimeoutExpired/FileNotFoundErrorを捕捉します）。
    """
    safe_body = body.replace("'", "''")
    safe_title = title.replace("'", "''")
    command = f"Import-Module BurntToast; New-BurntToastNotification -Text '{safe_title}', '{safe_body}'"

    try:
        result = subprocess.run(
            [pwsh_path, "-Command", command],
            capture_output=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return {"success": True, "reason": None}

        error_msg = _decode_stderr(result.stderr) or f"exit {result.returncode}"
        return {"success": False, "reason": error_msg}
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return {"success": False, "reason": str(e)}


def extract_summary(message: str | None) -> str:
    """最後のアシスタントメッセージから短いサマリーを抽出します。
    最初の非空行を取得し、MAX_BODY_LENGTH文字に切り詰めます。
    """
    if not message or not isinstance(message, str):
        return "Done"

    line = first_meaningful_line(message)
    if not line:
        return "Done"

    compacted = compact_line(line, MAX_BODY_LENGTH)
    return compacted or "Done"


def notify_macos(title: str, body: str, *, timeout: float = DEFAULT_NOTIFICATION_TIMEOUT) -> None:
    """osascript経由でmacOS通知を送信します。

    AppleScript文字列はバックスラッシュエスケープをサポートしないため、
    埋め込み前にダブルクォートをカーリークォートに置換し、バックスラッシュを削除します。

    Args:
        title: 通知タイトル。
        body: 通知本文。
        timeout: subprocess呼び出しの最大待機秒数。既定はDEFAULT_NOTIFICATION_TIMEOUT。

    Returns:
        なし。

    Raises:
        例外は発生しません（内部でTimeoutExpired/FileNotFoundErrorを捕捉します）。
    """
    safe_body = body.replace("\\", "").replace('"', "\u201c")
    safe_title = title.replace("\\", "").replace('"', "\u201c")
    script = f'display notification "{safe_body}" with title "{safe_title}"'

    try:
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log(f"[DesktopNotify] osascript failed: {e}")


def run(raw_input: str) -> str:
    """デスクトップ通知フックを実行します。生入力をそのまま返します (パススルー)。

    通知処理全体（PowerShell探索 + 通知送信）は_notification_timeout()秒の
    予算内で完結するようdeadlineベースで管理され、予算を使い切った時点で
    以降の処理（探索・送信）は打ち切られます。
    """
    try:
        input_data = (parse_json_object(raw_input.strip()) if raw_input.strip() else None) or {}
        summary = extract_summary(input_data.get("last_assistant_message"))
        deadline = time.monotonic() + _notification_timeout()

        if IS_MACOS:
            timeout = _remaining_timeout(deadline)
            if timeout > 0:
                notify_macos(TITLE, summary, timeout=timeout)
        elif is_wsl():
            ps = find_powershell(deadline)
            if ps:
                timeout = _remaining_timeout(deadline)
                if timeout <= 0:
                    return raw_input
                result = notify_windows(ps, TITLE, summary, timeout=timeout)
                if result.get("reason") and "burnttoast" in result["reason"].lower():
                    log("[DesktopNotify] Tip: Install BurntToast module to enable notifications")
                elif result.get("reason"):
                    log(f"[DesktopNotify] Notification failed: {result['reason']}")
            else:
                log("[DesktopNotify] Tip: Install BurntToast module in PowerShell for notifications")
    except Exception as err:
        log(f"[DesktopNotify] Error: {err}")

    return raw_input


def main() -> int:
    """スクリプトとして実行されたときのエントリーポイント。"""

    try:
        raw = read_raw_stdin()
        output = run(raw)
        print(output, end="")
        return 0
    except Exception:
        return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
