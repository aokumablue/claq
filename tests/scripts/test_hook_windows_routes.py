"""Windows で、ホストと同じ経路から hooks.json のフックを起動する契約テスト。

Windows の CI ジョブでだけ走り、他の OS では skip する。

- Claude Code は ``command`` の文字列を Git Bash で実行する（Windows でも POSIX 版 wrapper が動く）。
  ``CLAUDE_PLUGIN_ROOT`` の書き方（区切りが ``\\`` か ``/`` か）はホストに依るため、両方で起動する。
- ``powershell`` の文字列は、それを読むホストが PowerShell の ``-Command`` で実行するものとして起動する。
  ``-Command`` は native の終了コード 2 を 1 へ丸めうるため、deny は stdout の ``permissionDecision`` で見る。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows のホスト経路を起動する")

_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugins" / "claq"
_HOOKS = json.loads((_PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
_GIT_BASH = Path(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "bin", "bash.exe")

_BASH_ALLOW = {"tool_name": "Bash", "tool_input": {"command": "git status"}}
_BASH_DENY = {"tool_name": "Bash", "tool_input": {"command": "git commit --no-verify -m x"}}
_POWERSHELL_DENY = {"tool_name": "PowerShell", "tool_input": {"command": "git commit --no-verify -m x"}}
_WRITE_ALLOW = {"tool_name": "Write", "tool_input": {"file_path": "app.py", "content": "x"}}
_WRITE_DENY = {"tool_name": "Write", "tool_input": {"file_path": "ruff.toml", "content": "x"}}


def _hook(module: str) -> dict[str, str]:
    """hooks.json で *module* を起動する PreToolUse の command フック定義を返す。"""
    return next(
        hook
        for entry in _HOOKS["PreToolUse"]
        for hook in entry["hooks"]
        if hook["command"].endswith(f" {module}")
    )


def _run(argv: list[str], payload: dict, tmp_path: Path, plugin_root: str) -> subprocess.CompletedProcess[str]:
    """ホストと同じく stdin へツール入力 JSON を渡してフックを起動する（~/.claq は tmp_path に置く）。"""
    env = {**os.environ, "CLAUDE_PLUGIN_ROOT": plugin_root, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    return subprocess.run(
        argv,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=tmp_path,
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize("root_style", ["native", "forward-slash"])
@pytest.mark.parametrize(
    ("module", "payload", "expected"),
    [
        ("claq.hooks.block_no_verify", _BASH_ALLOW, 0),
        ("claq.hooks.block_no_verify", _BASH_DENY, 2),
        ("claq.hooks.block_no_verify", _POWERSHELL_DENY, 2),
        ("claq.hooks.config_protection", _WRITE_ALLOW, 0),
        ("claq.hooks.config_protection", _WRITE_DENY, 2),
    ],
    ids=["bash-allow", "bash-deny", "powershell-tool-deny", "write-allow", "write-deny"],
)
def test_command_under_git_bash(
    tmp_path: Path, root_style: str, module: str, payload: dict, expected: int
) -> None:
    """Claude Code と同じく ``command`` を Git Bash で実行し、allow は 0、deny は 2 で返ること。"""
    plugin_root = str(_PLUGIN_ROOT) if root_style == "native" else _PLUGIN_ROOT.as_posix()

    result = _run([str(_GIT_BASH), "-c", _hook(module)["command"]], payload, tmp_path, plugin_root)

    assert result.returncode == expected, result.stderr


@pytest.mark.parametrize(
    ("payload", "denied"),
    [(_BASH_ALLOW, False), (_BASH_DENY, True)],
    ids=["allow", "deny"],
)
def test_powershell_field_under_powershell(tmp_path: Path, payload: dict, denied: bool) -> None:
    """``powershell`` の文字列を PowerShell で実行し、deny を stdout の permissionDecision で返すこと。"""
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    assert powershell is not None
    script = _hook("claq.hooks.block_no_verify")["powershell"].replace("${CLAUDE_PLUGIN_ROOT}", str(_PLUGIN_ROOT))

    result = _run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", script], payload, tmp_path, str(_PLUGIN_ROOT)
    )

    if denied:
        assert json.loads(result.stdout)["permissionDecision"] == "deny", result.stderr
        assert result.returncode != 0
    else:
        assert (result.returncode, result.stdout) == (0, ""), result.stderr
