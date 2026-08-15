"""bluecore-helpers.sh のテスト。"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_HELPER = _REPO_ROOT / "plugins" / "bluecore" / "runtime" / "bluecore-helpers.sh"
_PLUGIN_ROOT = _REPO_ROOT / "plugins" / "bluecore"

_ZSH_AVAILABLE = shutil.which("zsh") is not None
_skip_without_zsh = pytest.mark.skipif(
    not _ZSH_AVAILABLE, reason="zsh が PATH 上に見つからないため zsh 依存テストをスキップする。"
)


def _run_bash(script: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """bash -lc でスクリプトを実行する。"""
    return subprocess.run(
        ["bash", "-lc", script],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def _run_zsh(script: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """zsh -c でスクリプトを実行する（~/.zshrc を読み込まずマシン非依存にする）。"""
    return subprocess.run(
        ["zsh", "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def _print_plugin_root_script(*, unset_env: bool = False, cd_to: Path | None = None) -> str:
    """helper を source して bluecore_plugin_root を 1 行出す。

    cd_to を渡すと、source する前にそのディレクトリへ cd してから実行する。
    リポジトリ外の cwd でも結果が変わらないことを検証するために使う。
    """
    unset = "unset CLAUDE_PLUGIN_ROOT\n" if unset_env else ""
    cd = f'cd "{cd_to}"\n' if cd_to is not None else ""
    return f'''
set -euo pipefail
{cd}{unset}source "{_HELPER}"
printf '%s\\n' "$(bluecore_plugin_root)"
'''


def _print_plugin_root_script_relative_source_then_cd(cd_to: Path) -> str:
    """相対パスで helper を source した後に別ディレクトリへ cd してから呼び出す。

    _REPO_ROOT で相対パス "plugins/bluecore/runtime/bluecore-helpers.sh" を
    source し、CLAUDE_PLUGIN_ROOT を unset した状態で cd_to へ cd してから
    bluecore_plugin_root を呼ぶ。fix 前のコードは BASH_SOURCE[0] を
    bluecore_plugin_root 呼び出し時に相対パスのまま dirname 解決していたため、
    source 後に cd すると cwd 起点で解決が壊れる（bash でも zsh でも）。
    fix 後のコードは source 時点でファイル先頭スコープの
    `_BLUECORE_HELPERS_DIR` を絶対パスへ解決済みのため、この cd の影響を受けない。
    """
    return f'''
set -euo pipefail
cd "{_REPO_ROOT}"
unset CLAUDE_PLUGIN_ROOT
source "plugins/bluecore/runtime/bluecore-helpers.sh"
cd "{cd_to}"
printf '%s\\n' "$(bluecore_plugin_root)"
'''


def test_bluecore_plugin_root_relative_source_then_cd_survives_under_bash(tmp_path: Path) -> None:
    """bash: 相対パスで source した後に別ディレクトリへ cd しても正しい plugin root を返す。"""
    result = _run_bash(_print_plugin_root_script_relative_source_then_cd(tmp_path))

    assert result.stdout.strip() == str(_PLUGIN_ROOT)


@_skip_without_zsh
def test_bluecore_plugin_root_relative_source_then_cd_survives_under_zsh(tmp_path: Path) -> None:
    """zsh: 相対パスで source した後に別ディレクトリへ cd しても正しい plugin root を返す。"""
    result = _run_zsh(_print_plugin_root_script_relative_source_then_cd(tmp_path))

    assert result.stdout.strip() == str(_PLUGIN_ROOT)


def test_bluecore_run_bg_returns_pid(tmp_path: Path) -> None:
    """bluecore_run_bg が数値 PID を返し、そのプロセスが実在すること。"""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    python3 = fake_bin / "python3"
    python3.write_text("#!/usr/bin/env bash\nsleep 5\n", encoding="utf-8")
    python3.chmod(0o755)

    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}
    script = f'''
set -euo pipefail
source "{_HELPER}"
pid="$(bluecore_run_bg demo.command --flag)"
case "$pid" in
  (*[!0-9]*|"") exit 1 ;;
esac
kill -0 "$pid"
kill "$pid"
wait "$pid" 2>/dev/null || true
'''

    _run_bash(script, env=env)


def test_bluecore_plugin_root_prefers_claude_plugin_root_env(tmp_path: Path) -> None:
    """CLAUDE_PLUGIN_ROOT があればその値を返す。"""
    env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(tmp_path / "copilot")}
    result = _run_bash(_print_plugin_root_script(), env=env)

    assert result.stdout.strip() == str(tmp_path / "copilot")


def test_bluecore_plugin_root_uses_file_location_fallback_with_env() -> None:
    """親環境から CLAUDE_PLUGIN_ROOT を除いた場合はファイル位置にフォールバックする。"""
    env = dict(os.environ)
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    result = _run_bash(_print_plugin_root_script(), env=env)

    assert result.stdout.strip() == str(_PLUGIN_ROOT)


def test_bluecore_plugin_root_uses_file_location_fallback_without_env() -> None:
    """シェル内で CLAUDE_PLUGIN_ROOT を unset した場合もファイル位置にフォールバックする。"""
    result = _run_bash(_print_plugin_root_script(unset_env=True))

    assert result.stdout.strip() == str(_PLUGIN_ROOT)


@_skip_without_zsh
def test_bluecore_plugin_root_uses_file_location_fallback_with_env_under_zsh(tmp_path: Path) -> None:
    """zsh: 親環境から CLAUDE_PLUGIN_ROOT を除いても、リポジトリ外の cwd でファイル位置に解決する。"""
    env = dict(os.environ)
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    result = _run_zsh(_print_plugin_root_script(cd_to=tmp_path), env=env)

    assert result.stdout.strip() == str(_PLUGIN_ROOT)


@_skip_without_zsh
def test_bluecore_plugin_root_uses_file_location_fallback_without_env_under_zsh(tmp_path: Path) -> None:
    """zsh: シェル内で CLAUDE_PLUGIN_ROOT を unset しても、リポジトリ外の cwd でファイル位置に解決する。"""
    result = _run_zsh(_print_plugin_root_script(unset_env=True, cd_to=tmp_path))

    assert result.stdout.strip() == str(_PLUGIN_ROOT)
