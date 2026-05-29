"""devgear-helpers.sh のテスト。"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def _run_bash(script: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-lc", script],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def test_devgear_mem_search_builds_repo_scoped_payload() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    helper = repo_root / "plugins" / "devgear" / "runtime" / "devgear-helpers.sh"
    script = f'''
set -euo pipefail
cd "{repo_root}"
source "{helper}"
devgear_mem_json() {{
  printf '%s\n%s\n' "$1" "$2"
}}
devgear_mem_search "hello world" 7
'''

    result = _run_bash(script)
    lines = result.stdout.strip().splitlines()
    assert lines[0] == "search"
    payload = json.loads(lines[1])
    assert payload == {
        "cwd": str(repo_root),
        "query": "hello world",
        "limit": 7,
    }


def test_devgear_run_bg_returns_pid(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[4]
    helper = repo_root / "plugins" / "devgear" / "runtime" / "devgear-helpers.sh"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    python3 = fake_bin / "python3"
    python3.write_text("#!/usr/bin/env bash\nsleep 5\n", encoding="utf-8")
    python3.chmod(0o755)

    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}
    script = f'''
set -euo pipefail
source "{helper}"
pid="$(devgear_run_bg demo.command --flag)"
case "$pid" in
  (*[!0-9]*|"") exit 1 ;;
esac
kill -0 "$pid"
kill "$pid"
wait "$pid" 2>/dev/null || true
'''

    _run_bash(script, env=env)


def test_devgear_plugin_root_prefers_claude_plugin_root_env(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[4]
    helper = repo_root / "plugins" / "devgear" / "runtime" / "devgear-helpers.sh"
    script = f'''
set -euo pipefail
source "{helper}"
printf '%s\n' "$(devgear_plugin_root)"
'''

    env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(tmp_path / "copilot")}
    result = _run_bash(script, env=env)

    assert result.stdout.strip() == str(tmp_path / "copilot")


def test_devgear_plugin_root_uses_file_location_fallback_with_env(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[4]
    helper = repo_root / "plugins" / "devgear" / "runtime" / "devgear-helpers.sh"
    script = f'''
set -euo pipefail
source "{helper}"
printf '%s\n' "$(devgear_plugin_root)"
'''

    env = dict(os.environ)
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    result = _run_bash(script, env=env)

    assert result.stdout.strip() == str(repo_root / "plugins" / "devgear")


def test_devgear_plugin_root_uses_file_location_fallback_without_env(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[4]
    helper = repo_root / "plugins" / "devgear" / "runtime" / "devgear-helpers.sh"
    script = f'''
set -euo pipefail
unset CLAUDE_PLUGIN_ROOT
source "{helper}"
printf '%s\n' "$(devgear_plugin_root)"
'''

    result = _run_bash(script)

    assert result.stdout.strip() == str(repo_root / "plugins" / "devgear")
