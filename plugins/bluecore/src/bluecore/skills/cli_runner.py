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


def detect_cli_binary() -> str:
    """実行環境に応じて使用する LLM CLI バイナリ名を返す。"""
    if detect_harness() == "claude":
        return "claude"
    if shutil.which("copilot"):
        return "copilot"
    return "claude"


def build_tools_args(binary: str, tools: list[str]) -> list[str]:
    """バイナリ別のツール許可フラグを組み立てる。

    claude: --allowedTools Read,Write,...
    copilot: --allow-tool Read --allow-tool Write ...
    """
    if binary == "claude":
        return ["--allowedTools", ",".join(tools)]
    return [arg for tool in tools for arg in ("--allow-tool", tool)]


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
