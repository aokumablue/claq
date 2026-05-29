"""quality-gate の言語プリセット駆動実行を検証するテスト。"""

from __future__ import annotations

import io
import json
import runpy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from devgear.hooks import quality_gate as quality_gate


def _write_step_script(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "import sys",
                "marker = Path(sys.argv[1])",
                "marker.write_text('ran', encoding='utf-8')",
            ]
        ),
        encoding="utf-8",
    )


def _patch_preset(monkeypatch: pytest.MonkeyPatch, preset: dict[str, Any]) -> None:
    """`resolve_quality_gate_config` を固定プリセットに差し替える。"""
    monkeypatch.setattr(quality_gate, "resolve_quality_gate_config", lambda **_kw: preset)


def test_quality_gate_runs_configured_step_and_preserves_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "marker.txt"
    step_script = tmp_path / "step.py"
    _write_step_script(step_script)

    preset = {
        "actions": {
            "post-edit": {
                "rules": [
                    {
                        "extensions": [".ts"],
                        "steps": [
                            {
                                "argv": [
                                    sys.executable,
                                    str(step_script),
                                    str(marker),
                                ],
                            }
                        ],
                    }
                ]
            }
        },
    }
    _patch_preset(monkeypatch, preset)

    raw_input = json.dumps({"tool_input": {"file_path": "src/example.ts"}})
    quality_gate.run(raw_input, action="post-edit")

    assert marker.read_text(encoding="utf-8") == "ran"


def test_quality_gate_skips_non_matching_file_extension(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "marker.txt"
    step_script = tmp_path / "step.py"
    _write_step_script(step_script)

    preset = {
        "actions": {
            "post-edit": {
                "rules": [
                    {
                        "extensions": [".ts"],
                        "steps": [
                            {
                                "argv": [
                                    sys.executable,
                                    str(step_script),
                                    str(marker),
                                ],
                            }
                        ],
                    }
                ]
            }
        },
    }
    _patch_preset(monkeypatch, preset)

    raw_input = json.dumps({"tool_input": {"file_path": "src/example.py"}})
    quality_gate.run(raw_input, action="post-edit")

    assert not marker.exists()


def test_quality_gate_skips_when_preset_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """プリセットが空の場合、linter は実行されない。"""
    steps_executed: list[dict[str, object]] = []

    _patch_preset(monkeypatch, {"actions": {}})

    def fake_run_step(
        step: dict[str, object],
        raw_input: str,
        base_env: dict[str, str] | None = None,
        default_cwd: str | Path | None = None,
    ) -> bool:
        steps_executed.append(step)
        return True

    monkeypatch.setattr(quality_gate, "run_step", fake_run_step)

    raw_input = json.dumps({"tool_input": {"file_path": "src/example.py"}})
    quality_gate.run(raw_input, action="post-edit")

    assert steps_executed == []


def test_quality_gate_expands_step_env_before_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "env-marker.txt"
    step_script = tmp_path / "step.py"

    step_script.write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "import sys",
                "marker = Path(sys.argv[1])",
                "marker.write_text(sys.argv[2], encoding='utf-8')",
            ]
        ),
        encoding="utf-8",
    )

    preset = {
        "actions": {
            "post-edit": {
                "rules": [
                    {
                        "extensions": [".ts"],
                        "steps": [
                            {
                                "env": {"GREETING": "hello"},
                                "argv": [
                                    sys.executable,
                                    str(step_script),
                                    str(marker),
                                    "${GREETING}",
                                ],
                            }
                        ],
                    }
                ]
            }
        },
    }
    _patch_preset(monkeypatch, preset)

    raw_input = json.dumps({"tool_input": {"file_path": "src/example.ts"}})
    quality_gate.run(raw_input, action="post-edit")

    assert marker.read_text(encoding="utf-8") == "hello"


def test_quality_gate_does_not_run_linter_when_rule_has_no_steps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rules に steps がない場合、linter は実行されない。"""
    py_file = tmp_path / "example.py"
    py_file.write_text("print('hi')\n", encoding="utf-8")
    calls: list[tuple[str, list[str], str | Path | None]] = []

    _patch_preset(
        monkeypatch,
        {"actions": {"post-edit": {"rules": [{"extensions": [".py"]}]}}},
    )

    def fake_exec_command(command: str, args: list[str], cwd: str | Path | None = None) -> dict[str, object]:
        calls.append((command, args, cwd))
        return {"status": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(quality_gate, "exec_command", fake_exec_command)

    raw_input = json.dumps({"tool_input": {"file_path": str(py_file)}})
    quality_gate.run(raw_input, action="post-edit")

    assert calls == []


def test_quality_gate_no_fallback_when_rules_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rules が空の場合、linter は実行されない。"""
    py_file = tmp_path / "example.py"
    py_file.write_text("print('hi')\n", encoding="utf-8")
    calls: list[tuple[str, list[str], str | Path | None]] = []

    _patch_preset(monkeypatch, {"actions": {"post-edit": {"rules": []}}})

    def fake_exec_command(command: str, args: list[str], cwd: str | Path | None = None) -> dict[str, object]:
        calls.append((command, args, cwd))
        return {"status": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(quality_gate, "exec_command", fake_exec_command)

    raw_input = json.dumps({"tool_input": {"file_path": str(py_file)}})
    quality_gate.run(raw_input, action="post-edit")

    assert calls == []


def test_quality_gate_uses_language_preset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """言語プリセットから設定が解決される。"""
    preset_config = {
        "actions": {
            "post-edit": {
                "rules": [
                    {
                        "extensions": [".py"],
                        "steps": [{"argv": ["ruff", "check", "src", "tests"]}],
                    }
                ]
            }
        }
    }
    _patch_preset(monkeypatch, preset_config)

    steps_executed: list[dict[str, object]] = []

    def fake_run_step(
        step: dict[str, object],
        raw_input: str,
        base_env: dict[str, str] | None = None,
        default_cwd: str | Path | None = None,
    ) -> bool:
        steps_executed.append(step)
        return True

    monkeypatch.setattr(quality_gate, "run_step", fake_run_step)

    raw_input = json.dumps({"tool_input": {"file_path": "src/example.py"}})
    quality_gate.run(raw_input, action="post-edit")

    assert len(steps_executed) == 1
    assert steps_executed[0].get("argv") == ["ruff", "check", "src", "tests"]


def test_quality_gate_load_config_delegates_to_preset_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preset = {"actions": {"post-edit": {"rules": []}}}
    _patch_preset(monkeypatch, preset)

    assert quality_gate.load_config() == preset


def test_quality_gate_run_step_builds_command_env_and_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", "/home/tester")
    monkeypatch.setenv("PYTHONPATH", "base-path")
    (tmp_path / "nested").mkdir()

    captured: dict[str, object] = {}

    def fake_run(
        command: list[str],
        *,
        input: str,
        text: bool,
        capture_output: bool,
        cwd: str,
        env: dict[str, str],
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["input"] = input
        captured["cwd"] = cwd
        captured["env"] = env
        captured["timeout"] = timeout
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="child stderr")

    monkeypatch.setattr(quality_gate.subprocess, "run", fake_run)

    step = {
        "module": "pkg.tool",
        "args": ["${HOME}", "${NOT_ALLOWED}", 123],
        "env": {"EXTRA": "${HOME}", "DISABLED": None},
        "cwd": "nested",
        "timeout_seconds": "bad",
        "name": "demo-step",
    }

    assert quality_gate.run_step(
        step,
        "payload",
        base_env=quality_gate._base_env(),
        default_cwd=tmp_path,
    )
    assert captured["command"] == [sys.executable, "-m", "pkg.tool", "/home/tester", "${NOT_ALLOWED}"]
    assert captured["input"] == "payload"
    assert captured["cwd"] == str(tmp_path / "nested")
    assert captured["timeout"] == 30.0
    captured_env: dict[str, str] = captured["env"]  # type: ignore[assignment]
    assert captured_env["EXTRA"] == "/home/tester"
    assert captured_env["CLAUDE_PLUGIN_ROOT"] == str(quality_gate.PLUGIN_ROOT)
    assert captured_env["PYTHONPATH"].startswith(str(quality_gate.PLUGIN_ROOT / "src"))


def test_quality_gate_run_step_rejects_cwd_outside_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def fake_run(*args, **kwargs):  # noqa: ANN001
        nonlocal called
        called = True
        raise AssertionError("subprocess should not be called")

    monkeypatch.setattr(quality_gate.subprocess, "run", fake_run)

    assert not quality_gate.run_step({"argv": ["echo"], "cwd": ".."}, "payload", default_cwd=tmp_path)
    assert not called


def test_quality_gate_run_configured_rules_handles_mismatches_and_invalid_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executed: list[dict[str, object]] = []

    def fake_run_step(
        step: dict[str, object],
        raw_input: str,
        base_env: dict[str, str] | None = None,
        default_cwd: str | Path | None = None,
    ) -> bool:
        executed.append(step)
        return True

    monkeypatch.setattr(quality_gate, "run_step", fake_run_step)
    monkeypatch.setattr(quality_gate, "_base_env", lambda: {"BASE": "1"})
    monkeypatch.setattr(quality_gate, "_project_root", lambda: tmp_path)

    config = {
        "actions": {
            "post-edit": {
                "rules": [
                    None,
                    {"extensions": [".ts"], "steps": [{"argv": ["skip"]}]},
                    {"extensions": [".py"], "tool_names": ["Edit"], "steps": [{"argv": ["run"]}, "bad"]},
                ]
            }
        }
    }
    input_data = {"tool_name": "Edit", "tool_input": {"file_path": "src/example.py"}}

    assert quality_gate._run_configured_rules("post-edit", "payload", input_data, config)
    assert executed == [{"argv": ["run"]}]


def test_quality_gate_main_returns_zero_when_reader_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(quality_gate, "read_raw_stdin", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    assert quality_gate.main(["post-edit"]) == 0


def test_quality_gate_configuration_helpers_cover_edge_cases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", "/home/tester")

    assert quality_gate._expand_text("", {}) == ""
    assert quality_gate._expand_text("${HOME}", {}, allowed_names=None) == "/home/tester"

    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project_root))
    assert quality_gate._project_root() == project_root.resolve()

    preset = {"actions": {"post-edit": {"rules": []}}}
    _patch_preset(monkeypatch, preset)
    assert quality_gate.load_config() == preset


def test_quality_gate_rule_and_step_helpers_cover_missing_branches(
    tmp_path: Path,
) -> None:
    env = {"HOME": "/home/tester"}

    assert quality_gate._normalize_extension(None) == ""
    assert quality_gate._extract_file_path({"file_path": "src/app.py"}) == "src/app.py"
    assert quality_gate._extract_tool_name({"tool_name": 123}) == ""
    assert not quality_gate._rule_matches({"extensions": [".py"]}, {"tool_name": "Edit"})
    assert not quality_gate._rule_matches({"tool_names": ["edit"]}, {"tool_name": "Write"})
    assert quality_gate._build_step_command({}, env, set()) is None

    cwd = quality_gate._build_step_cwd({"cwd": str(tmp_path / "nested")}, tmp_path, env, set())
    assert cwd == (tmp_path / "nested").resolve()


def test_quality_gate_run_step_exec_command_and_configured_rules_error_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logs: list[str] = []
    monkeypatch.setattr(quality_gate, "log", logs.append)

    assert not quality_gate.run_step({"name": "invalid"}, "payload", base_env={"BASE": "1"}, default_cwd=tmp_path)

    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        captured["timeout"] = kwargs["timeout"]
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(quality_gate.subprocess, "run", fake_run)
    assert quality_gate.run_step(
        {"argv": ["echo"], "timeout_seconds": 0},
        "payload",
        base_env={"BASE": "1"},
        default_cwd=tmp_path,
    )
    assert captured["timeout"] == 30.0

    monkeypatch.setattr(
        quality_gate.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("boom")),
    )
    assert not quality_gate.run_step({"argv": ["echo"]}, "payload", base_env={"BASE": "1"}, default_cwd=tmp_path)

    monkeypatch.setattr(
        quality_gate.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd=args[0], timeout=5)),
    )
    assert not quality_gate.run_step({"argv": ["echo"]}, "payload", base_env={"BASE": "1"}, default_cwd=tmp_path)

    monkeypatch.setattr(
        quality_gate.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="ok", stderr="warn"),
    )
    assert quality_gate.exec_command("ruff", ["check"], tmp_path) == {
        "status": 0,
        "stdout": "ok",
        "stderr": "warn",
    }

    monkeypatch.setattr(
        quality_gate.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("boom")),
    )
    error_result = quality_gate.exec_command("ruff", ["check"], tmp_path)
    assert error_result["status"] == -1
    assert "boom" in error_result["stderr"]

    assert not quality_gate._run_configured_rules("post-edit", "payload", {}, {"actions": "bad"})
    assert not quality_gate._run_configured_rules("post-edit", "payload", {}, {"actions": {"post-edit": "bad"}})
    assert not quality_gate._run_configured_rules(
        "post-edit", "payload", {}, {"actions": {"post-edit": {"rules": "bad"}}}
    )


def test_quality_gate_main_success_and_exception_paths(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(quality_gate, "read_raw_stdin", lambda: "payload")
    monkeypatch.setattr(quality_gate, "run", lambda raw, action="post-edit": None)

    assert quality_gate.main(["post-edit"]) == 0
    # quality_gate は PostToolUse フックのため stdout は出さない
    assert capsys.readouterr().out == ""

    logs: list[str] = []
    monkeypatch.setattr(quality_gate, "log", logs.append)
    monkeypatch.setattr(
        quality_gate,
        "run",
        lambda raw, action="post-edit": (_ for _ in ()).throw(RuntimeError("boom")),
    )

    assert quality_gate.main(["post-edit"]) == 0
    assert any("unexpected error: boom" in message for message in logs)


def test_quality_gate_entrypoint_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["quality_gate.py"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("devgear.hooks.quality_gate", run_name="__main__")

    assert excinfo.value.code == 0
