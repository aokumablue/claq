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

from bluecore.hooks import quality_gate as quality_gate


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

    raw_input = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": "src/example.ts"}})
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

    raw_input = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": "src/example.py"}})
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

    raw_input = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": "src/example.py"}})
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

    raw_input = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": "src/example.ts"}})
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

    raw_input = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(py_file)}})
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

    raw_input = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(py_file)}})
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

    raw_input = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": "src/example.py"}})
    quality_gate.run(raw_input, action="post-edit")

    assert len(steps_executed) == 1
    assert steps_executed[0].get("argv") == ["ruff", "check", "src", "tests"]


def test_quality_gate_base_env_without_pythonpath_uses_plugin_src_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PYTHONPATH 未設定時は PLUGIN_ROOT/src のみが設定される（if pythonpath: の False 分岐）。"""
    monkeypatch.delenv("PYTHONPATH", raising=False)

    env = quality_gate._base_env()

    assert env["PYTHONPATH"] == str(quality_gate.PLUGIN_ROOT / "src")


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
    assert captured["timeout"] == quality_gate.DEFAULT_STEP_TIMEOUT
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

    assert quality_gate._run_configured_rules(
        "post-edit", "payload", input_data, config, file_path="src/example.py"
    )
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
    assert quality_gate._extract_target_file_paths({"file_path": "src/app.py"}) == ["src/app.py"]
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
    assert captured["timeout"] == quality_gate.DEFAULT_STEP_TIMEOUT

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
    monkeypatch.setattr(quality_gate, "run", lambda raw, action="post-edit": [])

    assert quality_gate.main(["post-edit"]) == 0
    # lint に失敗がなければ additionalContext を出さない（トークンコスト 0）
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


def test_build_failure_context_reports_only_failing_steps_with_output() -> None:
    """成功 step と出力なし step は additionalContext に載せない。"""
    results = [
        quality_gate._StepResult(name="ok", returncode=0, output="all clear"),
        quality_gate._StepResult(name="silent", returncode=1, output=""),
        quality_gate._StepResult(name="ruff", returncode=1, output="F401 unused import"),
    ]

    context = quality_gate._build_failure_context(results)

    assert context.startswith("<quality-gate>")
    assert context.endswith("</quality-gate>")
    assert "## ruff (exit 1)" in context
    assert "F401 unused import" in context
    assert "all clear" not in context
    assert "silent" not in context


def test_build_failure_context_returns_empty_when_nothing_failed() -> None:
    """報告すべき失敗がなければ空文字列を返す。"""
    assert quality_gate._build_failure_context([]) == ""
    assert (
        quality_gate._build_failure_context(
            [quality_gate._StepResult(name="ruff", returncode=0, output="clean")]
        )
        == ""
    )


def test_build_failure_context_truncates_at_context_limit() -> None:
    """全体長は CONTEXT_LIMIT で打ち切る。"""
    huge = quality_gate._StepResult(name="ruff", returncode=1, output="x" * 5000)

    context = quality_gate._build_failure_context([huge])

    body = context.removeprefix("<quality-gate>\n").removesuffix("\n</quality-gate>")
    assert len(body) == quality_gate.CONTEXT_LIMIT


def test_quality_gate_main_emits_additional_context_on_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """lint が失敗したら additionalContext を stdout に出し、exit は 0 のまま。"""
    monkeypatch.setattr(quality_gate, "read_raw_stdin", lambda: "payload")
    monkeypatch.setattr(
        quality_gate,
        "run",
        lambda raw, action="post-edit": [
            quality_gate._StepResult(name="ruff", returncode=1, output="F401 unused import")
        ],
    )

    assert quality_gate.main(["post-edit"]) == 0

    payload = json.loads(capsys.readouterr().out)
    hook_output = payload["hookSpecificOutput"]
    assert hook_output["hookEventName"] == "PostToolUse"
    assert "F401 unused import" in hook_output["additionalContext"]


def test_run_rule_steps_skips_steps_that_failed_to_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """起動できなかった step（None）は結果に含めず、後続 step の実行を止めない。"""
    monkeypatch.setattr(
        quality_gate,
        "run_step",
        lambda step, raw_input, **_kw: (
            None
            if step["name"] == "broken"
            else quality_gate._StepResult(name=step["name"], returncode=0, output="")
        ),
    )

    results = quality_gate._run_rule_steps(
        {"steps": [{"name": "broken"}, {"name": "works"}]},
        "payload",
        {"BASE": "1"},
        tmp_path,
    )

    assert [r.name for r in results] == ["works"]


def test_quality_gate_entrypoint_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["quality_gate.py"])
    # tool_name を書込み系にしておかないと _is_write_tool ゲートで早期 return し、
    # load_config() 経由の実プリセット解決（quality_gate_presets の実コード
    # パス）が一切実行されなくなる。空 stdin のままだと entrypoint smoke test
    # としては通るが、他モジュールのカバレッジ低下を招くため書込みツールにする。
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool_name": "Edit"})))

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("bluecore.hooks.quality_gate", run_name="__main__")

    assert excinfo.value.code == 0


def test_extract_target_file_paths_non_string_values() -> None:
    """tool_input.file_path も input_data.file_path も文字列でなければ空リスト。"""
    assert quality_gate._extract_target_file_paths({"tool_input": {"file_path": 123}}) == []


def test_extract_tool_name_normalizes_apply_patch() -> None:
    """Codex の apply_patch は Edit に正規化される。"""
    assert quality_gate._extract_tool_name({"tool_name": "apply_patch"}) == "Edit"


def test_extract_target_file_paths_from_apply_patch_input() -> None:
    """apply_patch のパッチテキストから全対象ファイルを取り出す。"""
    patch = "*** Begin Patch\n*** Update File: src/x.py\n@@\n-a\n+b\n*** Add File: src/y.ts\n+a\n*** End Patch"
    data = {"tool_name": "apply_patch", "tool_input": {"input": patch}}
    assert quality_gate._extract_target_file_paths(data) == ["src/x.py", "src/y.ts"]


def test_extract_target_file_paths_apply_patch_unparseable_returns_empty() -> None:
    """パース不能な apply_patch 入力は空リストを返す。"""
    data = {"tool_name": "apply_patch", "tool_input": {"input": "garbage"}}
    assert quality_gate._extract_target_file_paths(data) == []


def test_quality_gate_run_skips_non_write_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """matcher が "*" に広がっても、Read 等の非書込みツールでは quality-gate 自体を実行しない（早期 return）。"""
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

    raw_input = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "src/example.ts"}})
    quality_gate.run(raw_input, action="post-edit")

    assert not marker.exists()


def test_quality_gate_run_skips_copilot_lowercase_non_write_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Copilot CLI の lowercase 非書込みツール名（read）でも早期 return する。"""
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

    raw_input = json.dumps({"tool_name": "read", "tool_input": {"file_path": "src/example.ts"}})
    quality_gate.run(raw_input, action="post-edit")

    assert not marker.exists()


def test_quality_gate_run_runs_for_copilot_lowercase_write_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Copilot CLI の lowercase 書込みツール名（write）では quality-gate を実行する。"""
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

    raw_input = json.dumps({"tool_name": "write", "tool_input": {"file_path": "src/example.ts"}})
    quality_gate.run(raw_input, action="post-edit")

    assert marker.read_text(encoding="utf-8") == "ran"


def test_is_write_tool_normalizes_apply_patch_and_copilot_lowercase() -> None:
    """_is_write_tool は apply_patch と Copilot CLI の lowercase tool_name を正しく判定する。"""
    assert quality_gate._is_write_tool({"tool_name": "write"})
    assert quality_gate._is_write_tool({"tool_name": "edit"})
    assert quality_gate._is_write_tool({"tool_name": "multiedit"})
    assert quality_gate._is_write_tool({"tool_name": "apply_patch"})
    assert quality_gate._is_write_tool({"tool_name": "Edit"})
    assert not quality_gate._is_write_tool({"tool_name": "bash"})
    assert not quality_gate._is_write_tool({"tool_name": "Read"})
    assert not quality_gate._is_write_tool({})


def test_quality_gate_run_lints_every_file_in_multi_file_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """複数ファイルパッチでは全ファイルが拡張子ルールの対象になる。"""
    linted: list[str | None] = []

    def fake_resolve(**kwargs: Any) -> dict[str, Any]:
        linted.append(kwargs.get("file_path"))
        return {
            "actions": {
                "post-edit": {
                    "rules": [
                        {"extensions": [".py", ".ts"], "steps": [{"argv": ["lint"]}]},
                        {"steps": [{"argv": ["project-wide"]}]},
                    ]
                }
            }
        }

    executed: list[list[str]] = []
    monkeypatch.setattr(quality_gate, "resolve_quality_gate_config", fake_resolve)
    monkeypatch.setattr(
        quality_gate,
        "run_step",
        lambda step, raw_input, base_env=None, default_cwd=None: executed.append(step["argv"]) or True,
    )

    patch = "*** Begin Patch\n*** Update File: src/x.py\n@@\n*** Add File: src/y.ts\n+a\n*** End Patch"
    raw_input = json.dumps({"tool_name": "apply_patch", "tool_input": {"input": patch}})
    quality_gate.run(raw_input, action="post-edit")

    assert linted == ["src/x.py", "src/y.ts"]
    # 拡張子ルールはファイルごと、extensions なしルールは初回のみ実行される
    assert executed == [["lint"], ["project-wide"], ["lint"]]


def test_quality_gate_extracts_file_path_from_native_camel_case_payload() -> None:
    """R-04: native toolName/toolArgs.file_path から対象パスを取り出す。"""
    data = {"toolName": "edit", "toolArgs": {"file_path": "sample.py"}}
    assert quality_gate._extract_target_file_paths(data) == ["sample.py"]


def test_quality_gate_does_not_treat_legacy_file_as_target_path() -> None:
    """quality_gate は旧 file キーを対象パスにしない。"""
    data = {"tool_name": "Edit", "tool_input": {"file": "sample.py"}}
    assert quality_gate._extract_target_file_paths(data) == []


def test_quality_gate_extracts_all_patch_paths_from_native_tool_args() -> None:
    """native toolArgs の構造化パッチから Add/Update/Delete を marker 順に返す。"""
    patch = (
        "*** Begin Patch\n"
        "*** Add File: new.py\n"
        "+x = 1\n"
        "*** Update File: mod.py\n"
        "@@\n"
        "*** Delete File: old.py\n"
        "*** End Patch"
    )
    data = {"toolName": "apply_patch", "toolArgs": {"input": patch}}
    assert quality_gate._extract_target_file_paths(data) == ["new.py", "mod.py", "old.py"]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"tool_name": "Edit", "tool_input": {"file_path": "sample.py"}},
            ["sample.py"],
        ),
        (
            {"tool_name": "Edit", "tool_args": {"file_path": "sample.py"}},
            ["sample.py"],
        ),
        (
            {"toolName": "edit", "toolArgs": json.dumps({"file_path": "sample.py"})},
            ["sample.py"],
        ),
        (
            {"toolName": "apply_patch", "toolArgs": {"input": "*** Update File: a.py\n@@\n-a\n+b\n"}},
            ["a.py"],
        ),
        (
            {"tool_name": "Edit", "file_path": "sample.py"},
            ["sample.py"],
        ),
        (
            {"toolName": "edit", "toolArgs": {}},
            [],
        ),
        (
            {"toolName": "bash", "toolArgs": {"file_path": "sample.py"}},
            ["sample.py"],
        ),
    ],
    ids=[
        "dt04-1-snake",
        "dt04-3-tool_args",
        "dt04-4-json-string",
        "dt04-5-single-patch",
        "dt04-7-top-level-file_path",
        "dt04-9-no-path",
        "dt04-10-bash-still-extracts",
    ],
)
def test_quality_gate_extracts_remaining_dt04_rows(payload: dict, expected: list[str]) -> None:
    """DT-04 残行: tool_args・JSON 文字列・トップレベル fallback・bash。"""
    assert quality_gate._extract_target_file_paths(payload) == expected


def _spy_quality_gate_run(monkeypatch: pytest.MonkeyPatch) -> tuple[list[str | None], list[tuple[str, bool]]]:
    """load_config と _run_configured_rules を spy して呼び出し履歴を返す。

    Args:
        monkeypatch: pytest の monkeypatch フィクスチャ。

    Returns:
        (load_config に渡った file_path, (file_path, run_pathless) のリスト)。
    """
    loaded: list[str | None] = []
    rules: list[tuple[str, bool]] = []

    def fake_load(*, file_path: str | None = None) -> dict[str, Any]:
        """load_config の呼び出しを記録する。"""
        loaded.append(file_path)
        return {"actions": {}}

    def fake_rules(
        action: str,
        raw_input: str,
        input_data: dict[str, Any],
        config: dict[str, Any],
        file_path: str = "",
        run_pathless: bool = True,
    ) -> list[Any]:
        """_run_configured_rules の呼び出しを記録する。"""
        rules.append((file_path, run_pathless))
        return []

    monkeypatch.setattr(quality_gate, "load_config", fake_load)
    monkeypatch.setattr(quality_gate, "_run_configured_rules", fake_rules)
    return loaded, rules


def test_quality_gate_run_applies_rules_to_native_camel_case_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-05: native camelCase の sample.py 編集で rule が起動する。"""
    loaded, rules = _spy_quality_gate_run(monkeypatch)
    raw = json.dumps({"toolName": "edit", "toolArgs": {"file_path": "sample.py"}})
    assert quality_gate.run(raw) == []
    assert loaded == ["sample.py"]
    assert rules == [("sample.py", True)]


def test_quality_gate_runs_pathless_rules_once_for_multi_file_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """複数ファイルパッチでは run_pathless が True のあと False。"""
    loaded, rules = _spy_quality_gate_run(monkeypatch)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: a.py\n"
        "@@\n-a\n+b\n"
        "*** Add File: b.py\n"
        "+x\n"
        "*** End Patch"
    )
    raw = json.dumps({"toolName": "apply_patch", "toolArgs": {"input": patch}})
    quality_gate.run(raw)
    assert loaded == ["a.py", "b.py"]
    assert rules == [("a.py", True), ("b.py", False)]


def test_quality_gate_rule_matches_skips_blank_extensions() -> None:
    """空の拡張子は無視し、残りの拡張子で照合する。"""
    rule = {"extensions": ["", ".py"]}
    assert quality_gate._rule_matches(rule, {"tool_name": "Edit", "file_path": "a.py"}, "a.py")
    assert not quality_gate._rule_matches(rule, {"tool_name": "Edit", "file_path": "a.txt"}, "a.txt")


def test_quality_gate_build_step_command_module_with_non_list_args() -> None:
    """module 指定時に args が list でなくても -m コマンドを返す。"""
    command = quality_gate._build_step_command(
        {"module": "pkg.tool", "args": "not-a-list"},
        {"HOME": "/home/tester"},
        set(),
    )
    assert command == [sys.executable, "-m", "pkg.tool"]


def test_quality_gate_rule_matches_native_tool_name_filter() -> None:
    """_rule_matches は native toolName を tool_names=['edit'] と照合する。"""
    rule = {"tool_names": ["edit"]}
    assert quality_gate._rule_matches(
        rule, {"toolName": "edit", "toolArgs": {"file_path": "sample.py"}}, "sample.py"
    )
    assert not quality_gate._rule_matches(
        rule, {"toolName": "bash", "toolArgs": {"file_path": "sample.py"}}, "sample.py"
    )


def test_quality_gate_run_remaining_dt05_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """DT-05 残行: snake_case、JSON 文字列、bash 対象外、パスなし、toolName 不正。"""
    loaded, rules = _spy_quality_gate_run(monkeypatch)

    quality_gate.run(json.dumps({"tool_name": "Edit", "tool_input": {"file_path": "sample.py"}}))
    assert loaded == ["sample.py"]
    assert rules == [("sample.py", True)]

    loaded.clear()
    rules.clear()
    quality_gate.run(
        json.dumps({"toolName": "edit", "toolArgs": json.dumps({"file_path": "sample.py"})})
    )
    assert loaded == ["sample.py"]
    assert rules == [("sample.py", True)]

    loaded.clear()
    rules.clear()
    assert quality_gate.run(
        json.dumps({"toolName": "bash", "toolArgs": {"file_path": "sample.py"}})
    ) == []
    assert loaded == []
    assert rules == []

    loaded.clear()
    rules.clear()
    quality_gate.run(json.dumps({"toolName": "edit", "toolArgs": {}}))
    assert loaded == [None]
    assert rules == [("", True)]

    loaded.clear()
    rules.clear()
    assert quality_gate.run(
        json.dumps({"toolName": 123, "toolArgs": {"file_path": "sample.py"}})
    ) == []
    assert quality_gate.run(
        json.dumps({"toolArgs": {"file_path": "sample.py"}})
    ) == []
    assert loaded == []
    assert rules == []
