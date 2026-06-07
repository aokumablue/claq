#!/usr/bin/env python3
"""
デスクトップ通知フック (Stop)。

Claudeが応答を完了したときにタスクサマリーを含むネイティブ
デスクトップ通知を送信します。サポート環境:
  - macOS: osascript (ネイティブ)
  - WSL: PowerShell 7またはWindows PowerShell + BurntToastモジュール
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from bluecore.hooks.hook_common import parse_json_object, read_raw_stdin
from bluecore.lib.core_utils import IS_LINUX, IS_MACOS, log
from bluecore.lib.slim_text import compact_line, first_meaningful_line

TITLE = "通知"
MAX_BODY_LENGTH = 100

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


def find_powershell() -> str | None:
    """WSL上で利用可能なPowerShell実行ファイルを探します。"""

    candidates = [
        "pwsh.exe",  # WSL interopがWindows PATHから解決
        "powershell.exe",  # Windows PowerShell用のWSL interop
        "/mnt/c/Program Files/PowerShell/7/pwsh.exe",  # PowerShell 7 (デフォルトインストール)
        "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",  # Windows PowerShell
    ]

    for path in candidates:
        try:
            result = subprocess.run(
                [path, "-Command", "exit 0"],
                capture_output=True,
                timeout=3,
            )
            if result.returncode == 0:
                return path
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue

    return None


def notify_windows(pwsh_path: str, title: str, body: str) -> dict:
    """PowerShell BurntToast経由でWindowsトースト通知を送信します。

    'success' (bool)と'reason' (str|None)を含む辞書を返します。
    """
    safe_body = body.replace("'", "''")
    safe_title = title.replace("'", "''")
    command = f"Import-Module BurntToast; New-BurntToastNotification -Text '{safe_title}', '{safe_body}'"

    try:
        result = subprocess.run(
            [pwsh_path, "-Command", command],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return {"success": True, "reason": None}

        error_msg = result.stderr or f"exit {result.returncode}"
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


def notify_macos(title: str, body: str) -> None:
    """osascript経由でmacOS通知を送信します。
    AppleScript文字列はバックスラッシュエスケープをサポートしないため、
    埋め込み前にダブルクォートをカーリークォートに置換し、バックスラッシュを削除します。
    """
    safe_body = body.replace("\\", "").replace('"', "\u201c")
    safe_title = title.replace("\\", "").replace('"', "\u201c")
    script = f'display notification "{safe_body}" with title "{safe_title}"'

    try:
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log(f"[DesktopNotify] osascript failed: {e}")


def run(raw_input: str) -> str:
    """デスクトップ通知フックを実行します。生入力をそのまま返します (パススルー)。"""
    try:
        input_data = (parse_json_object(raw_input.strip()) if raw_input.strip() else None) or {}
        summary = extract_summary(input_data.get("last_assistant_message"))

        if IS_MACOS:
            notify_macos(TITLE, summary)
        elif is_wsl():
            ps = find_powershell()
            if ps:
                result = notify_windows(ps, TITLE, summary)
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
