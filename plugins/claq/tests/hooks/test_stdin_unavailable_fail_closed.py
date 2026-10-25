"""stdin を読めなかったとき、4 つの保護 hook が揃って deny することのテスト。

Windows の通常パイプでは ``select.select`` が ``OSError`` になる。それを
「入力なし」へ正規化すると ``git status`` と ``git commit --no-verify`` が
揃って exit 0 になり、保護 hook が 1 バイトも検査しないまま素通りする
（`docs/adr/hook-failure-direction.md`）。

「読む対象が無い」（tty 起動・stdin 未接続・即 EOF）と「読めなかった」を
区別するのが修正の核心なので、両方をこの 1 ファイルで並べて固定する。
4 hook を parametrize でまとめて見るのは、片方だけ強化されて
「Write なら止まるが Bash なら通る」型の非対称が生まれるのを防ぐため。

デシジョンテーブル:
  - StdinUnavailableError → exit 2 + deny JSON + hook 名と理由入りの stderr
  - 空文字列（payload なし） → exit 0・出力なし（素通り。空入力は非ブロッキングの契約を維持）
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from claq.hooks.stdin_io import StdinUnavailableError, stdin_unreadable_message

from ._support import LAUNCHER, _launcher_env

# (hook モジュール名, deny 理由に載る hook 名, read_raw_stdin_with_truncation を差し替えるモジュール名)。
# pre_bash_commit_quality は main() 内で stdin_io から遅延 import するため、差し替え先は共通側。
# モジュールは実行時に import_module で引き直す。conftest の `_fresh_runpy_module` が runpy 実行のたびに
# sys.modules から対象を落とすため、収集時に掴んだモジュールへの差し替えは効かないことがある。
_PROTECTION_HOOKS = [
    ("claq.hooks.block_no_verify", "pre:block-no-verify", "claq.hooks.block_no_verify"),
    ("claq.hooks.config_protection", "pre:config-protection", "claq.hooks.config_protection"),
    ("claq.hooks.bash_config_protection", "pre:bash-config-protection", "claq.hooks.bash_config_protection"),
    ("claq.hooks.pre_bash_commit_quality", "pre:bash-commit-quality", "claq.hooks.stdin_io"),
]
_HOOK_IDS = [hook.rsplit(".", 1)[1] for hook, _, _ in _PROTECTION_HOOKS]


@pytest.mark.parametrize(("hook", "hook_name", "reader_module"), _PROTECTION_HOOKS, ids=_HOOK_IDS)
def test_unreadable_stdin_is_denied(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    hook: str,
    hook_name: str,
    reader_module: str,
) -> None:
    """読み取り不能な stdin は全保護 hook が exit 2 で拒否し、hook 名と理由を名指す deny JSON と stderr を出すこと。"""
    reason = stdin_unreadable_message(hook_name, "pipe read failed")
    assert hook_name in reason and "pipe read failed" in reason and "BLOCKED" in reason
    monkeypatch.setattr(
        importlib.import_module(reader_module),
        "read_raw_stdin_with_truncation",
        mock.Mock(side_effect=StdinUnavailableError("pipe read failed")),
    )

    rc = importlib.import_module(hook).main()
    captured = capsys.readouterr()

    assert (rc, json.loads(captured.out), captured.err) == (
        2,
        {"permissionDecision": "deny", "permissionDecisionReason": reason},
        reason + "\n",
    )


@pytest.mark.parametrize(("hook", "hook_name", "reader_module"), _PROTECTION_HOOKS, ids=_HOOK_IDS)
def test_absent_stdin_still_passes_through(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    hook: str,
    hook_name: str,
    reader_module: str,
) -> None:
    """payload が無い場合（tty 起動等）は全保護 hook が何も出さず exit 0 で素通りすること。

    ここまで deny にすると、payload を渡さない host で全ツール呼び出しが拒否され、セッションが復旧不能になる。
    """
    monkeypatch.setattr(importlib.import_module(reader_module), "read_raw_stdin_with_truncation", lambda: ("", False))

    rc = importlib.import_module(hook).main()

    assert (rc, *capsys.readouterr()) == (0, "", "")


def test_stalled_stdin_exits_cleanly_in_a_real_process(tmp_path: Path) -> None:
    """開いたまま何も書かれない stdin でも、abort せず exit 2 と deny JSON で終わること。

    ワーカーが `BufferedReader.read1()` の中でロックを握ったままブロックすると、deny を出して戻った後の
    インタプリタ終了時に stdin 後始末がそのロックを取れず `Fatal Python error: _enter_buffered_busy` で
    abort し、終了コードが 2 でなくなる。プロセス終了時にしか現れないため実プロセスで確かめる。
    """
    process = subprocess.Popen(
        [sys.executable, str(LAUNCHER), "claq.hooks.block_no_verify"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_launcher_env(tmp_path),
    )
    # stdin は開いたまま何も書かない（`communicate()` は即座に閉じてしまい、子は EOF＝payload なしとして素通りする）。
    try:
        process.wait(timeout=60)
        stdout, stderr = process.stdout.read(), process.stderr.read()
    finally:
        process.kill()  # 終了済みなら何もしない（タイムアウト時の後始末）
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()

    assert "Fatal Python error" not in stderr, stderr
    assert process.returncode == 2, (process.returncode, stderr)
    assert json.loads(stdout)["permissionDecision"] == "deny"
