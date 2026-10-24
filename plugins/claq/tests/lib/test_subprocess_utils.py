"""subprocess_utils のテスト。"""

from __future__ import annotations

import os
import subprocess

import pytest

from claq.lib.subprocess_utils import run_text


@pytest.mark.parametrize("extra_env", [{"EXTRA_ENV": "2"}, None], ids=["extra-env", "without-extra-env"])
def test_run_text_enforces_text_encoding_and_env(
    monkeypatch: pytest.MonkeyPatch, extra_env: dict[str, str] | None
) -> None:
    """UTF-8 text モード・check なし・出力捕捉で呼び、環境は現在の環境に extra_env を重ねたものにする。"""
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """呼び出しを記録して成功を返す。"""
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setenv("BASE_ENV", "1")
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_text(["echo", "hi"], timeout=1.5, input_text="payload", extra_env=extra_env)

    assert result.stdout == "ok"
    expected_kwargs = {"input": "payload", "text": True, "encoding": "utf-8", "errors": "replace", "capture_output": True}
    expected_kwargs |= {"check": False, "timeout": 1.5, "env": {**os.environ, **(extra_env or {})}}
    assert calls == [(["echo", "hi"], expected_kwargs)]


def test_run_text_runs_real_command() -> None:
    """実際の subprocess.run へ渡す引数の組み合わせが有効で、コマンドを実行できる。"""
    assert run_text(["true"], timeout=10).returncode == 0
