"""subprocess 呼び出しを UTF-8 text モードで統一するヘルパー。"""

from __future__ import annotations

import os
import subprocess

# 外部コマンドを起動できない（git が無い・実行権限が無い = ``OSError``）か、時間内に終わらない
# （``TimeoutExpired``）ことを示す例外。保護フックの git 呼び出しは全てこの 1 タプルで受け、
# 判定不能として扱う（``FileNotFoundError`` だけを受けると ``PermissionError`` が貫通して hook が
# exit 1 で落ち、PreToolUse では許可へ倒れる）。
SUBPROCESS_FAILURES = (OSError, subprocess.TimeoutExpired)


def run_text(
    cmd: list[str],
    *,
    timeout: float,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """UTF-8 text モードで subprocess.run を実行する。

    Args:
        cmd: 実行コマンド。
        timeout: タイムアウト秒数。
        extra_env: 追加で設定する環境変数。

    Returns:
        実行結果。
    """
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        cmd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=timeout,
        env=env,
    )
