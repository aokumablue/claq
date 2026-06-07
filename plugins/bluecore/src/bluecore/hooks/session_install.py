#!/usr/bin/env python3
"""
install.sh の自動実行を管理する SessionStart フック。

~/.bluecore/plugin_installed_version のバージョンと plugin.json のバージョンを比較し、
差異がある場合のみ install.sh を実行して仮想環境を再構築する。
ONNX モデルビルドは BLUECORE_INSTALL_ONNX_ASYNC=1 でバックグラウンド非同期実行に切り替わる。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from bluecore.hooks._install_lock import install_lock
from bluecore.hooks.hook_common import emit_session_start_output as _emit_session_start_output
from bluecore.lib.constants import BASE_DIR_NAME
from bluecore.lib.sanitize import sanitize_log_value
from bluecore.lib.subprocess_utils import run_text

_PLUGIN_ROOT = Path(__file__).resolve().parents[3]
_BLUECORE_DIR = Path.home() / BASE_DIR_NAME
_VERSION_FILE = _BLUECORE_DIR / "plugin_installed_version"
_VENV_DIR = Path.home() / BASE_DIR_NAME / ".venv"


def _session_start_output() -> str:
    """SessionStart 互換の hookSpecificOutput を返す。"""
    return _emit_session_start_output()


def _sanitize_exception(exc: BaseException) -> str:
    """例外メッセージをログ出力向けにサニタイズする。"""
    return sanitize_log_value(str(exc))


def _should_repair_venv_symlink(plugin_root: Path) -> bool:
    """version 一致時に .venv symlink が欠落・破損していれば True を返す。

    Args:
        plugin_root: プラグインルートディレクトリのパス。

    Returns:
        修復が必要であれば True。
    """
    if not _VENV_DIR.is_dir():
        return False  # 共有 venv 自体が無ければ修復不可
    link = plugin_root / ".venv"
    if not link.exists() and not link.is_symlink():
        return True  # symlink が存在しない
    if link.is_symlink():
        try:
            return link.resolve() != _VENV_DIR.resolve()
        except OSError:
            return True  # 解決不能な破損 symlink
    return True  # 実体ディレクトリやファイルなど予期しない種別


def _repair_venv_symlink(plugin_root: Path) -> None:
    """.venv symlink を共有 venv へ向け直す。

    実体ディレクトリが存在する場合は誤削除を避け警告のみ出す。

    Args:
        plugin_root: プラグインルートディレクトリのパス。

    Returns:
        None: 値を返しません。
    """
    link = plugin_root / ".venv"
    if link.exists() and not link.is_symlink():
        # 実体ディレクトリ / ファイルは破壊しない
        print(f"[SessionInstall] .venv が symlink ではないため自動修復を中止: {link}", file=sys.stderr)
        return
    try:
        link.unlink(missing_ok=True)
    except OSError as e:
        print(f"[SessionInstall] 破損 symlink 削除失敗: {_sanitize_exception(e)}", file=sys.stderr)
        return
    try:
        link.symlink_to(_VENV_DIR, target_is_directory=True)
        print(f"[SessionInstall] .venv symlink 修復: {link} -> {_VENV_DIR}", file=sys.stderr)
    except OSError as e:
        print(f"[SessionInstall] symlink 作成失敗: {_sanitize_exception(e)}", file=sys.stderr)


def _resolve_plugin_root() -> Path | None:
    """CLAUDE_PLUGIN_ROOT を検証してプラグインルートを返す。"""
    plugin_root_env = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not plugin_root_env:
        print("[SessionInstall] CLAUDE_PLUGIN_ROOT が設定されていません。スキップします。", file=sys.stderr)
        return None

    plugin_root = Path(plugin_root_env).resolve()
    if plugin_root != _PLUGIN_ROOT:
        print(
            f"[SessionInstall] 不正なプラグインルートです: {sanitize_log_value(plugin_root_env)}",
            file=sys.stderr,
        )
        return None

    plugin_json = plugin_root / ".claude-plugin" / "plugin.json"
    if not plugin_json.is_file():
        print(
            f"[SessionInstall] 不正なプラグインルートです: {sanitize_log_value(plugin_root_env)}",
            file=sys.stderr,
        )
        return None
    return plugin_root


def _get_plugin_version(plugin_root: Path) -> str | None:
    """plugin.json からプラグインバージョンを読み取る。

    Args:
        plugin_root: プラグインルートディレクトリのパス。

    Returns:
        バージョン文字列。取得できない場合は None。

    Raises:
        例外は発生しません。
    """
    plugin_json = plugin_root / ".claude-plugin" / "plugin.json"
    try:
        data = json.loads(plugin_json.read_text(encoding="utf-8"))
        return data.get("version")
    except (json.JSONDecodeError, OSError, AttributeError, TypeError) as e:
        print(f"[SessionInstall] plugin.json の読み込みに失敗しました: {_sanitize_exception(e)}", file=sys.stderr)
        return None


def _get_installed_version() -> str | None:
    """~/.bluecore/plugin_installed_version からインストール済みバージョンを読み取る。

    Args:
        引数はありません。

    Returns:
        インストール済みバージョン文字列。ファイルが存在しない場合は None。

    Raises:
        例外は発生しません。
    """
    if not _VERSION_FILE.exists():
        return None
    return _VERSION_FILE.read_text(encoding="utf-8").strip()


def _precheck_install_target(plugin_root: Path) -> Path | None:
    """install.sh の存在をチェックして返す。プラグインルート外は拒否する。

    Args:
        plugin_root: 検証済みプラグインルート。

    Returns:
        install.sh の Path。チェック失敗時は None。
    """
    install_sh = (plugin_root / "install.sh").resolve()
    if not install_sh.is_file() or not install_sh.is_relative_to(plugin_root):
        print("[SessionInstall] install.sh がプラグインルート外です。スキップします。", file=sys.stderr)
        return None
    return install_sh


def _lock_phase_should_skip(plugin_root: Path, current_version: str) -> bool:
    """ロック取得後の再チェックで処理スキップ要否を返す。

    別プロセスが先にインストールを完了している場合に True を返す。

    Args:
        plugin_root: プラグインルートディレクトリのパス。
        current_version: plugin.json から取得した最新バージョン。

    Returns:
        スキップすべき場合は True。
    """
    try:
        installed_version = _get_installed_version()
    except OSError as e:
        print(
            f"[SessionInstall] インストール済みバージョンの読み込みに失敗しました: {_sanitize_exception(e)}",
            file=sys.stderr,
        )
        return True

    if installed_version == current_version:
        print(f"[SessionInstall] 別プロセスがインストール済み: {sanitize_log_value(current_version)}", file=sys.stderr)
        if _should_repair_venv_symlink(plugin_root):
            _repair_venv_symlink(plugin_root)
        return True

    return False


def _run_install(install_sh: Path) -> subprocess.CompletedProcess[str] | None:
    """install.sh を ONNX 非同期モードで実行し結果を返す。失敗時は None を返す。

    Args:
        install_sh: 実行する install.sh の Path。

    Returns:
        subprocess.CompletedProcess。実行失敗時は None。
    """
    try:
        # BLUECORE_INSTALL_ONNX_ASYNC=1 で ONNX ビルドをバックグラウンドに切り出す（タイムアウト回避）
        return run_text(["bash", str(install_sh)], timeout=120, extra_env={"BLUECORE_INSTALL_ONNX_ASYNC": "1"})
    except (subprocess.SubprocessError, OSError) as e:
        print(f"[SessionInstall] install.sh の実行に失敗しました: {_sanitize_exception(e)}", file=sys.stderr)
        return None


def _handle_install_result(result: subprocess.CompletedProcess[str]) -> bool:
    """install 実行結果を stderr に出力し、成功なら True を返す。

    バージョン書き込みは install.sh 側で行うためここでは行わない。

    Args:
        result: subprocess の実行結果。

    Returns:
        install.sh が正常終了した場合 True、非ゼロ終了の場合 False。
    """
    if result.stdout:
        print(sanitize_log_value(result.stdout, max_len=4000), file=sys.stderr)
    if result.stderr:
        print(sanitize_log_value(result.stderr, max_len=4000), file=sys.stderr)

    if result.returncode != 0:
        print(
            f"[SessionInstall] install.sh が失敗しました (exit {result.returncode})。次回再試行します。",
            file=sys.stderr,
        )
        return False

    print("[SessionInstall] インストール完了", file=sys.stderr)
    return True


def _run_install_with_lock(plugin_root: Path, current_version: str | None) -> bool:
    """ロックを取得して install.sh を実行する。成功なら True を返す。

    Args:
        plugin_root: プラグインルートディレクトリ。
        current_version: plugin.json から読んだ最新バージョン。

    Returns:
        install.sh が正常終了した場合 True。スキップ・失敗時は False。
    """
    _BLUECORE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(_BLUECORE_DIR, 0o700)
    lock_path = _BLUECORE_DIR / "install.lock"
    try:
        with install_lock(lock_path):
            if current_version is not None and _lock_phase_should_skip(plugin_root, current_version):
                return False
            install_sh = _precheck_install_target(plugin_root)
            if install_sh is None:
                return False
            result = _run_install(install_sh)
            if result is None:
                return False
            return _handle_install_result(result)
    except OSError as e:
        print(f"[SessionInstall] ロック取得失敗: {_sanitize_exception(e)}", file=sys.stderr)
        return False


def run(_raw_input: str) -> str:
    """install.sh の実行判定と実行を行い hookSpecificOutput の JSON を返す。

    バージョン一致時は .venv symlink 修復のみ行う。
    バージョン不一致・未インストール時は install.sh を同期実行（ONNX のみ非同期）。

    Args:
        _raw_input: フックへの標準入力（未使用）。

    Returns:
        hookSpecificOutput を含む JSON 文字列。

    Raises:
        例外は発生しません。
    """
    plugin_root = _resolve_plugin_root()
    if plugin_root is None:
        return _session_start_output()

    current_version = _get_plugin_version(plugin_root)
    installed_version = _get_installed_version()

    if current_version is not None and installed_version == current_version:
        if _should_repair_venv_symlink(plugin_root):
            _repair_venv_symlink(plugin_root)
        print(f"[SessionInstall] 既にインストール済みです: {sanitize_log_value(str(current_version))}", file=sys.stderr)
        return _session_start_output()

    print(
        "[SessionInstall] バージョン変更を検出しました: "
        f"{sanitize_log_value(repr(installed_version))} → {sanitize_log_value(repr(current_version))}",
        file=sys.stderr,
    )

    success = _run_install_with_lock(plugin_root, current_version)
    if not success:
        return _session_start_output()

    if _should_repair_venv_symlink(plugin_root):
        _repair_venv_symlink(plugin_root)
    if not (Path.home() / ".bluecore" / "models" / "model.onnx").exists():
        print("[SessionInstall] onnx building...", file=sys.stderr)

    return _session_start_output()


def main() -> int:
    """スクリプトとして実行されたときのエントリポイント。

    Args:
        引数はありません。

    Returns:
        終了コード（常に 0 — 失敗してもセッションをブロックしない）。

    Raises:
        例外は発生しません。
    """
    try:
        raw = "" if sys.stdin.isatty() else sys.stdin.read()
        output = run(raw)
        print(output, end="")
        return 0
    except Exception as err:
        print(f"[SessionInstall] エラー: {_sanitize_exception(err)}", file=sys.stderr)
        print(_session_start_output(), end="")
        return 0


if __name__ == "__main__":
    sys.exit(main())
