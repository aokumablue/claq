"""LLM CLI（claude / copilot）の実行環境を抽象化するヘルパー。

環境判定:
  ハーネスが claude（lib.harness.detect_harness）→ "claude"
  それ以外で copilot が PATH に存在 → "copilot"
  フォールバック → "claude"
"""

from __future__ import annotations

import os
import shutil
import subprocess

from bluecore.lib.harness import detect_harness

_COPILOT_TOOL_NAMES = {
    "Read": "view",
    "Write": "edit",
    "Edit": "edit",
    "Bash": "bash",
    "Glob": "glob",
    "Grep": "grep",
    "Agent": "task",
}
_COPILOT_PERMISSION_NAMES = {
    "Write": "write",
    "Edit": "write",
    "Bash": "shell",
}


def detect_cli_binary() -> str:
    """実行環境に応じて使用する LLM CLI バイナリ名を返す。"""
    if detect_harness() == "claude":
        return "claude"
    if shutil.which("copilot"):
        return "copilot"
    return "claude"


def build_tools_args(binary: str, tools: list[str]) -> list[str]:
    """バイナリ別にモデルが利用できるツールの制限フラグを組み立てる。

    claude: --allowedTools Read,Write,...
    copilot: --available-tools view edit ...
    """
    if binary == "claude":
        return ["--allowedTools", ",".join(tools)]
    copilot_tools = _map_tools(tools, _COPILOT_TOOL_NAMES)
    return ["--available-tools", *copilot_tools]


def build_permission_args(binary: str, tools: list[str]) -> list[str]:
    """バイナリ別に承認プロンプトを省略する権限フラグを組み立てる。"""
    if binary == "claude":
        return []
    permissions = _map_tools(tools, _COPILOT_PERMISSION_NAMES, ignore_unmapped=True)
    return ["--allow-tool", *permissions] if permissions else []


def _map_tools(tools: list[str], mapping: dict[str, str], *, ignore_unmapped: bool = False) -> list[str]:
    mapped: list[str] = []
    for tool in tools:
        if tool not in mapping:
            if ignore_unmapped:
                continue
            raise ValueError(f"Unsupported tool for Copilot: {tool!r}")
        value = mapping[tool]
        if value not in mapped:
            mapped.append(value)
    return mapped


def build_output_format_args(binary: str, fmt: str) -> list[str]:
    """バイナリ別の出力フォーマットフラグを組み立てる。

    copilot は stream-json 非対応のため json に読み替える。
    """
    if binary == "copilot" and fmt == "stream-json":
        return ["--output-format", "json"]
    return ["--output-format", fmt]


def run_cli(
    args: list[str],
    *,
    stdin_input: str | None = None,
    timeout: int = 120,
    strip_claudecode_env: bool = False,
    cwd: str | None = None,
) -> subprocess.CompletedProcess:
    """バイナリを自動選択してサブプロセスを実行し CompletedProcess を返す。

    strip_claudecode_env=True の場合、CLAUDECODE を環境から除去する。
    これは claude -p のネスト実行時に対話端末との衝突を防ぐための措置。
    """
    binary = detect_cli_binary()
    cmd = [binary, *args]
    env = {k: v for k, v in os.environ.items() if not (strip_claudecode_env and k == "CLAUDECODE")}
    return subprocess.run(
        cmd,
        input=stdin_input,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        cwd=cwd,
    )
