"""subprocess 呼び出しを UTF-8 text モードで統一するヘルパー。"""

from __future__ import annotations

import os
import subprocess
from typing import Any


def run_text(
    cmd: list[str],
    *,
    timeout: float | None,
    input_text: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """UTF-8 text モードで subprocess.run を実行する。

    Args:
        cmd: 実行コマンド。
        timeout: タイムアウト秒数。
        input_text: 標準入力に渡す文字列。
        extra_env: 追加で設定する環境変数。

    Returns:
        実行結果。
    """
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    run_kwargs: dict[str, Any] = {
        "input": input_text,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "capture_output": True,
        "check": False,
        "timeout": timeout,
        "env": env,
    }
    return subprocess.run(cmd, **run_kwargs)


def check_output_text(cmd: list[str], *, timeout: float = 5.0) -> str:
    """UTF-8 text モードで subprocess.check_output を実行する。

    Args:
        cmd: 実行コマンド。
        timeout: タイムアウト秒数。

    Returns:
        標準出力文字列。
    """
    return subprocess.check_output(
        cmd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stderr=subprocess.DEVNULL,
        timeout=timeout,
    )
