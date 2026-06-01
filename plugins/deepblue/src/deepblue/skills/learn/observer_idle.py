"""learn observer のユーザー操作アイドル秒数を OS 別に検出する。"""

from __future__ import annotations

import platform
import shutil
import subprocess


def _get_idle_seconds_darwin() -> int:
    """macOS の ioreg から HIDIdleTime を取得してアイドル秒数を返す。"""
    try:
        result = subprocess.run(
            ["/usr/sbin/ioreg", "-c", "IOHIDSystem"],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in result.stdout.splitlines():
            if "HIDIdleTime" in line:
                value = int(line.split()[-1])
                return max(0, value // 1_000_000_000)
    except (OSError, ValueError):
        pass
    return 0


def _get_idle_seconds_linux() -> int:
    """Linux の xprintidle コマンドからアイドル秒数を返す。"""
    if shutil.which("xprintidle") is None:
        return 0
    try:
        result = subprocess.run(["xprintidle"], capture_output=True, text=True, check=False)
        return max(0, int(result.stdout.strip() or "0") // 1000)
    except (OSError, ValueError):
        return 0


def _get_idle_seconds_windows() -> int:
    """Windows の GetLastInputInfo API からアイドル秒数を返す。"""
    _PS_CMD = (
        "try { "
        "Add-Type -MemberDefinition '[DllImport(\"user32.dll\")] public static extern bool GetLastInputInfo(ref LASTINPUTINFO p); "
        "[StructLayout(LayoutKind.Sequential)] public struct LASTINPUTINFO { public uint cbSize; public int dwTime; }' "
        "-Name WinAPI -Namespace PInvoke; "
        "$l = New-Object PInvoke.WinAPI+LASTINPUTINFO; $l.cbSize = 8; "
        "[PInvoke.WinAPI]::GetLastInputInfo([ref]$l) | Out-Null; "
        "[int][Math]::Max(0, [long]([Environment]::TickCount - [long]$l.dwTime) / 1000) "
        "} catch { 0 }"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _PS_CMD],
            capture_output=True,
            text=True,
            check=False,
        )
        return max(0, int((result.stdout or "0").strip().replace("\r", "")))
    except (OSError, ValueError):
        return 0


def _get_idle_seconds() -> int:
    """OS ごとの方法でユーザー操作のアイドル秒数を返す。

    取得できない場合や未対応 OS では 0 を返す。
    """
    system = platform.system()
    if system == "Darwin":
        return _get_idle_seconds_darwin()
    if system == "Linux":
        return _get_idle_seconds_linux()
    if system.startswith("MINGW") or system.startswith("MSYS") or system.startswith("CYGWIN"):
        return _get_idle_seconds_windows()
    return 0
