"""subprocess_utils のテスト。"""

from __future__ import annotations

import subprocess

from devgear.lib.subprocess_utils import check_output_text, run_text


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


def test_check_output_text_enforces_text_encoding(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_check_output(cmd, **kwargs):  # noqa: ANN001
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return "output"

    monkeypatch.setattr(subprocess, "check_output", fake_check_output)

    result = check_output_text(["git", "status"], timeout=3.0)

    assert result == "output"
    kwargs = captured["kwargs"]
    assert kwargs["text"] is True
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["timeout"] == 3.0
