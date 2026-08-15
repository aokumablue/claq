"""LLM CLI（claude / copilot）の実行環境を抽象化するヘルパー。

環境判定:
  CLAUDECODE 環境変数あり → "claude"
  それ以外で copilot が PATH に存在 → "copilot"
  フォールバック → "claude"
"""

from __future__ import annotations

import os
import shutil
import subprocess

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
    if os.environ.get("CLAUDECODE"):
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
    """Claude ツール名を copilot 名へ順に写し、重複を除いて返す。

    Args:
        tools: Claude 側のツール名リスト。
        mapping: Claude 名 → copilot 名。
        ignore_unmapped: True なら未登録ツールを黙って飛ばす。False なら ValueError。

    Returns:
        出現順を保った copilot ツール名。
    """
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
    env = dict(os.environ)
    if strip_claudecode_env:
        env.pop("CLAUDECODE", None)
    return subprocess.run(
        cmd,
        input=stdin_input,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        cwd=cwd,
    )
