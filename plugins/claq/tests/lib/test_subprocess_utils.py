"""subprocess_utils のテスト。"""

from __future__ import annotations

import subprocess

from claq.lib.subprocess_utils import run_text


def test_run_text_enforces_text_encoding_and_env(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):  # noqa: ANN001
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setenv("BASE_ENV", "1")
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_text(["echo", "hi"], timeout=1.5, input_text="payload", extra_env={"EXTRA_ENV": "2"})

    assert result.stdout == "ok"
    kwargs = captured["kwargs"]
    assert kwargs["text"] is True
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"
    assert kwargs["check"] is False
    assert kwargs["capture_output"] is True
    assert kwargs["input"] == "payload"
    assert kwargs["timeout"] == 1.5
    assert kwargs["env"]["BASE_ENV"] == "1"
    assert kwargs["env"]["EXTRA_ENV"] == "2"


def test_run_text_without_extra_env() -> None:
    """extra_env 未指定でもコマンドを実行できる。"""
    from claq.lib.subprocess_utils import run_text

    result = run_text(["true"], timeout=10)
    assert result.returncode == 0
