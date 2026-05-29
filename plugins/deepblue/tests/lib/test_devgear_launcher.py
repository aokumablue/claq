"""launcher モジュールのテスト。"""

from __future__ import annotations

import io
import os
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import devgear.launcher as launcher


class FakeStdin:
    """stdin の代替オブジェクト。"""

    def __init__(self, tty: bool, data: str) -> None:
        self._tty = tty
        self._data = data
        self.read_called = False

    def isatty(self) -> bool:
        return self._tty

    def read(self, n: int = -1) -> str:
        self.read_called = True
        return self._data[:n] if n >= 0 else self._data


def _create_repo_venv(tmp_path: Path) -> Path:
    venv_python = tmp_path / ".venv" / "bin" / "python3"
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    venv_python.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    venv_python.chmod(0o755)
    return venv_python


@pytest.mark.parametrize(
    ("tty", "expected_input", "should_read"),
    [
        (True, "", False),
        (False, "payload", True),
    ],
)
def test_main_reads_stdin_only_when_piped(
    monkeypatch, capsys, tty: bool, expected_input: str, should_read: bool
) -> None:
    fake_stdin = FakeStdin(tty, "payload")
    captured = {}

    monkeypatch.setattr(launcher.sys, "stdin", fake_stdin)
    monkeypatch.setattr(launcher, "build_env", lambda: {})

    def fake_run(*args, **kwargs):
        captured["input"] = kwargs["input"]
        return SimpleNamespace(stdout="ok", stderr="", returncode=0)

    monkeypatch.setattr(launcher.subprocess, "run", fake_run)

    result = launcher.main(["dummy-target"])

    assert result == 0
    assert captured.get("input", "") == expected_input
    assert capsys.readouterr().out == "ok"


def test_main_does_not_echo_piped_input_when_subprocess_is_silent(monkeypatch, capsys) -> None:
    fake_stdin = FakeStdin(False, "payload")
    captured = {}

    monkeypatch.setattr(launcher.sys, "stdin", fake_stdin)
    monkeypatch.setattr(launcher, "build_env", lambda: {})

    def fake_run(*args, **kwargs):
        captured["input"] = kwargs["input"]
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(launcher.subprocess, "run", fake_run)

    result = launcher.main(["dummy-target"])

    assert result == 0
    assert fake_stdin.read_called is True
    assert captured["input"] == "payload"
    assert capsys.readouterr().out == ""


def test_resolve_command_prefers_repo_venv_python(monkeypatch, tmp_path: Path) -> None:
    venv_python = _create_repo_venv(tmp_path)
    monkeypatch.setattr(launcher, "REPO_ROOT", tmp_path)

    cmd = launcher.resolve_command("devgear.hooks.doc_file_warning", ["arg1"])

    assert cmd == [str(venv_python), "-m", "devgear.hooks.doc_file_warning", "arg1"]


def test_resolve_command_falls_back_to_system_python_without_repo_venv(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(launcher, "REPO_ROOT", tmp_path)

    cmd = launcher.resolve_command("devgear.hooks.doc_file_warning", [])

    assert cmd == [sys.executable, "-m", "devgear.hooks.doc_file_warning"]


def test_resolve_command_runs_python_script_with_repo_venv(monkeypatch, tmp_path: Path) -> None:
    venv_python = _create_repo_venv(tmp_path)
    script = tmp_path / "tool.py"
    script.write_text("print('ok')", encoding="utf-8")
    monkeypatch.setattr(launcher, "REPO_ROOT", tmp_path)

    assert launcher.resolve_command(str(script), ["arg"]) == [str(venv_python), str(script), "arg"]


def test_build_env_prepends_repo_venv_to_path(monkeypatch, tmp_path: Path) -> None:
    _create_repo_venv(tmp_path)
    monkeypatch.setattr(launcher, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("PATH", "/usr/local/bin")

    env = launcher.build_env()

    assert env["CLAUDE_PLUGIN_ROOT"] == str(tmp_path)
    assert env["VIRTUAL_ENV"] == str(tmp_path / ".venv")
    assert env["PATH"].split(os.pathsep)[0] == str(tmp_path / ".venv" / "bin")


def test_build_env_appends_existing_pythonpath(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(launcher, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(launcher, "_runtime_python", lambda: (sys.executable, None))
    monkeypatch.setenv("PYTHONPATH", "base-path")

    env = launcher.build_env()

    assert env["PYTHONPATH"] == os.pathsep.join([str(tmp_path / "src"), "base-path"])


def test_resolve_command_covers_shell_batch_and_executable_targets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    shell_script = tmp_path / "tool.sh"
    shell_script.write_text("#!/bin/sh\necho ok", encoding="utf-8")
    assert launcher.resolve_command(str(shell_script), []) == ["bash", str(shell_script)]

    executable = tmp_path / "tool"
    executable.write_text("#!/bin/sh\necho ok", encoding="utf-8")
    executable.chmod(0o755)
    assert launcher.resolve_command(str(executable), []) == [str(executable)]

    batch = tmp_path / "tool.cmd"
    batch.write_text("@echo off\necho ok", encoding="utf-8")
    monkeypatch.setattr(launcher.os, "name", "nt", raising=False)
    assert launcher.resolve_command(str(batch), []) == ["cmd", "/c", str(batch)]


def test_resolve_command_falls_back_when_candidate_resolution_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

    original_resolve = launcher.Path.resolve

    def fake_resolve(self, *args, **kwargs):  # noqa: ANN001
        if self.name == "bad-target":
            raise OSError("boom")
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(launcher.Path, "resolve", fake_resolve)

    expected_python, _ = launcher._runtime_python()
    assert launcher.resolve_command("bad-target", ["arg"]) == [expected_python, "-m", "bad-target", "arg"]


def test_main_covers_usage_stderr_and_entrypoint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert launcher.main([]) == 1
    assert "Usage: python3" in capsys.readouterr().err

    captured = {}

    def fake_run(*args, **kwargs):
        captured["stderr"] = "child stderr"
        return SimpleNamespace(stdout="child stdout", stderr="child stderr", returncode=7)

    monkeypatch.setattr(launcher.sys, "stdin", SimpleNamespace(isatty=lambda: False, read=lambda n=-1: "payload"))
    monkeypatch.setattr(launcher, "build_env", lambda: {})
    monkeypatch.setattr(launcher.subprocess, "run", fake_run)

    assert launcher.main(["dummy-target"]) == 7
    output = capsys.readouterr()
    assert output.out == "child stdout"
    assert output.err == "child stderr"
    assert captured["stderr"] == "child stderr"


def test_main_handles_oserror_and_entrypoint(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(launcher.sys, "stdin", SimpleNamespace(isatty=lambda: False, read=lambda n=-1: "payload"))
    monkeypatch.setattr(launcher, "build_env", lambda: {})
    monkeypatch.setattr(launcher.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("boom")))

    assert launcher.main(["dummy-target"]) == 1
    assert "ERROR: boom" in capsys.readouterr().err

    monkeypatch.setattr(sys, "argv", ["launcher.py"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("devgear.launcher", run_name="__main__")

    assert excinfo.value.code == 1
