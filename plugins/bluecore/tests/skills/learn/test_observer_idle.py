"""observer_idle の OS 別アイドル秒数検出を検証するテスト。

対象:
  - _get_idle_seconds_darwin() — ioreg 出力パースと例外フォールバック
  - _get_idle_seconds_linux() — xprintidle 有無と例外フォールバック
  - _get_idle_seconds_windows() — PowerShell 出力パースと例外フォールバック
  - _get_idle_seconds() — OS ディスパッチ（Darwin/Linux/MINGW・MSYS・CYGWIN/その他）

subprocess・platform.system・shutil.which を全てモックし、実プロセスを起動しない。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from bluecore.skills.learn import observer_idle


def _completed(stdout: str) -> SimpleNamespace:
    """subprocess.run の戻り値を模した stdout 保持オブジェクトを返す。"""
    return SimpleNamespace(stdout=stdout)


# --- _get_idle_seconds_darwin ------------------------------------------------


def test_darwin_hid_line_returns_seconds() -> None:
    """HIDIdleTime 行があれば ナノ秒/1e9 の秒数を返す。"""
    stdout = '  "HIDIdleTime" = 5000000000\n  "OtherKey" = 1\n'
    with mock.patch.object(observer_idle.subprocess, "run", return_value=_completed(stdout)):
        assert observer_idle._get_idle_seconds_darwin() == 5


def test_darwin_no_hid_line_returns_zero() -> None:
    """HIDIdleTime 行が無ければ 0 を返す。"""
    with mock.patch.object(observer_idle.subprocess, "run", return_value=_completed("nothing here\n")):
        assert observer_idle._get_idle_seconds_darwin() == 0


def test_darwin_value_error_returns_zero() -> None:
    """値が非数値で int() が失敗すれば 0 を返す。"""
    stdout = '  "HIDIdleTime" = notanumber\n'
    with mock.patch.object(observer_idle.subprocess, "run", return_value=_completed(stdout)):
        assert observer_idle._get_idle_seconds_darwin() == 0


def test_darwin_os_error_returns_zero() -> None:
    """subprocess が OSError を送出すれば 0 を返す。"""
    with mock.patch.object(observer_idle.subprocess, "run", side_effect=OSError("boom")):
        assert observer_idle._get_idle_seconds_darwin() == 0


# --- _get_idle_seconds_linux -------------------------------------------------


def test_linux_no_xprintidle_returns_zero() -> None:
    """xprintidle が未インストールなら 0 を返す。"""
    with mock.patch.object(observer_idle.shutil, "which", return_value=None):
        assert observer_idle._get_idle_seconds_linux() == 0


def test_linux_returns_seconds() -> None:
    """xprintidle の ミリ秒出力を /1000 した秒数を返す。"""
    with (
        mock.patch.object(observer_idle.shutil, "which", return_value="/usr/bin/xprintidle"),
        mock.patch.object(observer_idle.subprocess, "run", return_value=_completed("5000\n")),
    ):
        assert observer_idle._get_idle_seconds_linux() == 5


def test_linux_value_error_returns_zero() -> None:
    """非数値出力で int() が失敗すれば 0 を返す。"""
    with (
        mock.patch.object(observer_idle.shutil, "which", return_value="/usr/bin/xprintidle"),
        mock.patch.object(observer_idle.subprocess, "run", return_value=_completed("oops\n")),
    ):
        assert observer_idle._get_idle_seconds_linux() == 0


def test_linux_os_error_returns_zero() -> None:
    """subprocess が OSError を送出すれば 0 を返す。"""
    with (
        mock.patch.object(observer_idle.shutil, "which", return_value="/usr/bin/xprintidle"),
        mock.patch.object(observer_idle.subprocess, "run", side_effect=OSError("boom")),
    ):
        assert observer_idle._get_idle_seconds_linux() == 0


# --- _get_idle_seconds_windows -----------------------------------------------


def test_windows_returns_seconds() -> None:
    """PowerShell の数値出力を int 秒として返す。"""
    with mock.patch.object(observer_idle.subprocess, "run", return_value=_completed("42\r\n")):
        assert observer_idle._get_idle_seconds_windows() == 42


def test_windows_value_error_returns_zero() -> None:
    """非数値出力で int() が失敗すれば 0 を返す。"""
    with mock.patch.object(observer_idle.subprocess, "run", return_value=_completed("nan\n")):
        assert observer_idle._get_idle_seconds_windows() == 0


def test_windows_os_error_returns_zero() -> None:
    """subprocess が OSError を送出すれば 0 を返す。"""
    with mock.patch.object(observer_idle.subprocess, "run", side_effect=OSError("boom")):
        assert observer_idle._get_idle_seconds_windows() == 0


# --- _get_idle_seconds（OS ディスパッチ）------------------------------------


def test_dispatch_darwin() -> None:
    """system が Darwin なら darwin 実装へ委譲する。"""
    with (
        mock.patch.object(observer_idle.platform, "system", return_value="Darwin"),
        mock.patch.object(observer_idle, "_get_idle_seconds_darwin", return_value=11) as m,
    ):
        assert observer_idle._get_idle_seconds() == 11
        m.assert_called_once_with()


def test_dispatch_linux() -> None:
    """system が Linux なら linux 実装へ委譲する。"""
    with (
        mock.patch.object(observer_idle.platform, "system", return_value="Linux"),
        mock.patch.object(observer_idle, "_get_idle_seconds_linux", return_value=22) as m,
    ):
        assert observer_idle._get_idle_seconds() == 22
        m.assert_called_once_with()


def test_dispatch_windows_mingw() -> None:
    """system が MINGW/MSYS/CYGWIN なら windows 実装へ委譲する。"""
    with (
        mock.patch.object(observer_idle.platform, "system", return_value="MINGW64_NT-10.0"),
        mock.patch.object(observer_idle, "_get_idle_seconds_windows", return_value=33) as m,
    ):
        assert observer_idle._get_idle_seconds() == 33
        m.assert_called_once_with()


def test_dispatch_unknown_returns_zero() -> None:
    """未対応 OS では 0 を返す。"""
    with mock.patch.object(observer_idle.platform, "system", return_value="Plan9"):
        assert observer_idle._get_idle_seconds() == 0
