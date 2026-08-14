"""bluecore-helpers.sh のテスト。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_HELPER = _REPO_ROOT / "plugins" / "bluecore" / "runtime" / "bluecore-helpers.sh"
_PLUGIN_ROOT = _REPO_ROOT / "plugins" / "bluecore"


def _run_bash(script: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """bash -lc でスクリプトを実行する。"""
    return subprocess.run(
        ["bash", "-lc", script],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def _print_plugin_root_script(*, unset_env: bool = False) -> str:
    """helper を source して bluecore_plugin_root を 1 行出す。"""
    unset = "unset CLAUDE_PLUGIN_ROOT\n" if unset_env else ""
    return f'''
set -euo pipefail
{unset}source "{_HELPER}"
printf '%s\\n' "$(bluecore_plugin_root)"
'''


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
