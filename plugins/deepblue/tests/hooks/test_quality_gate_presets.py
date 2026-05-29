"""quality-gate 言語プリセットのテスト。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from devgear.hooks import quality_gate_presets
from devgear.hooks.quality_gate_presets import QUALITY_GATE_PRESETS, resolve_quality_gate_config
from devgear.lib.project_detect import ProjectInfo


@pytest.mark.parametrize(
    ("language", "expected_extensions"),
    [
        ("python", [".py", ".pyi"]),
        ("javascript", [".js", ".mjs", ".cjs"]),
        ("typescript", [".ts", ".tsx"]),
        ("go", [".go"]),
        ("rust", [".rs"]),
        ("ruby", [".rb", ".rake"]),
    ],
)
def test_resolve_uses_primary_language_preset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    language: str,
    expected_extensions: list[str],
) -> None:
    def fake_detect(_root: Any) -> ProjectInfo:
        return ProjectInfo(root=tmp_path, languages=[language], frameworks=[], primary_language=language)

    monkeypatch.setattr(quality_gate_presets, "detect_project", fake_detect)
    monkeypatch.setattr(quality_gate_presets, "_has_executable", lambda _argv: True)

    config = resolve_quality_gate_config(tmp_path)
    rules = config["actions"]["post-edit"]["rules"]
    assert len(rules) == 1
    assert rules[0]["extensions"] == expected_extensions
    assert rules[0]["steps"], "steps should be non-empty when tools are available"


def test_resolve_returns_empty_rules_when_language_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_detect(_root: Any) -> ProjectInfo:
        return ProjectInfo(root=tmp_path, languages=[], frameworks=[], primary_language=None)

    monkeypatch.setattr(quality_gate_presets, "detect_project", fake_detect)

    config = resolve_quality_gate_config(tmp_path)
    assert config == {"actions": {"post-edit": {"rules": []}}}


def test_resolve_falls_back_to_supported_language(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_detect(_root: Any) -> ProjectInfo:
        return ProjectInfo(
            root=tmp_path,
            languages=["brainfuck", "ruby"],
            frameworks=[],
            primary_language="brainfuck",
        )

    monkeypatch.setattr(quality_gate_presets, "detect_project", fake_detect)
    monkeypatch.setattr(quality_gate_presets, "_has_executable", lambda _argv: True)

    config = resolve_quality_gate_config(tmp_path)
    rules = config["actions"]["post-edit"]["rules"]
    assert len(rules) == 1
    assert rules[0]["extensions"] == [".rb", ".rake"]


def test_resolve_uses_repo_root_when_src_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_detect(_root: Any) -> ProjectInfo:
        return ProjectInfo(root=tmp_path, languages=["python"], frameworks=[], primary_language="python")

    monkeypatch.setattr(quality_gate_presets, "detect_project", fake_detect)
    monkeypatch.setattr(quality_gate_presets, "_has_executable", lambda _argv: True)

    config = resolve_quality_gate_config(tmp_path)
    rules = config["actions"]["post-edit"]["rules"]
    assert rules[0]["steps"][0]["argv"][-1] == "."


def test_resolve_skips_steps_when_tools_not_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_detect(_root: Any) -> ProjectInfo:
        return ProjectInfo(root=tmp_path, languages=["python"], frameworks=[], primary_language="python")

    monkeypatch.setattr(quality_gate_presets, "detect_project", fake_detect)
    monkeypatch.setattr(quality_gate_presets, "_has_executable", lambda _argv: False)

    config = resolve_quality_gate_config(tmp_path)
    assert config == {"actions": {"post-edit": {"rules": []}}}


def test_resolve_returns_empty_on_detect_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def raiser(_root: Any) -> ProjectInfo:
        raise RuntimeError("boom")

    monkeypatch.setattr(quality_gate_presets, "detect_project", raiser)

    config = resolve_quality_gate_config(tmp_path)
    assert config == {"actions": {"post-edit": {"rules": []}}}


def test_resolve_skips_invalid_bash_entries_and_empty_executable_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "src").mkdir()

    def fake_detect(_root: Any) -> ProjectInfo:
        return ProjectInfo(root=tmp_path, languages=["python"], frameworks=[], primary_language="python")

    monkeypatch.setattr(quality_gate_presets, "detect_project", fake_detect)
    assert quality_gate_presets._has_executable([]) is False
    monkeypatch.setattr(quality_gate_presets, "_has_executable", lambda _argv: True)
    monkeypatch.setitem(
        quality_gate_presets.QUALITY_GATE_PRESETS,
        "python",
        {
            "extensions": [".py"],
            "bash": [
                [],
                "not-a-list",
                ["ruff", "check", "."],
            ],
        },
    )

    config = resolve_quality_gate_config(tmp_path)
    rules = config["actions"]["post-edit"]["rules"]
    assert len(rules) == 1
    assert rules[0]["steps"] == [{"argv": ["ruff", "check", "src"]}]


def test_resolve_uses_file_path_for_python(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """file_path を指定すると ruff の対象が単一ファイルになること。"""

    def fake_detect(_root: Any) -> ProjectInfo:
        return ProjectInfo(root=tmp_path, languages=["python"], frameworks=[], primary_language="python")

    monkeypatch.setattr(quality_gate_presets, "detect_project", fake_detect)
    monkeypatch.setattr(quality_gate_presets, "_has_executable", lambda _argv: True)

    fp = "/path/to/module.py"
    config = resolve_quality_gate_config(tmp_path, file_path=fp)
    rules = config["actions"]["post-edit"]["rules"]
    assert len(rules) == 1
    assert rules[0]["steps"][0]["argv"][-1] == fp


def test_resolve_ignores_file_path_for_non_python_extension(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """file_path が .py でない場合は既存の target に戻ること。"""

    def fake_detect(_root: Any) -> ProjectInfo:
        return ProjectInfo(root=tmp_path, languages=["python"], frameworks=[], primary_language="python")

    monkeypatch.setattr(quality_gate_presets, "detect_project", fake_detect)
    monkeypatch.setattr(quality_gate_presets, "_has_executable", lambda _argv: True)

    config = resolve_quality_gate_config(tmp_path, file_path="/path/to/file.txt")
    rules = config["actions"]["post-edit"]["rules"]
    assert len(rules) == 1
    assert rules[0]["steps"][0]["argv"][-1] != "/path/to/file.txt"


def test_resolve_file_path_none_uses_default_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """file_path=None の場合は既存の target を使うこと（後方互換）。"""

    def fake_detect(_root: Any) -> ProjectInfo:
        return ProjectInfo(root=tmp_path, languages=["python"], frameworks=[], primary_language="python")

    monkeypatch.setattr(quality_gate_presets, "detect_project", fake_detect)
    monkeypatch.setattr(quality_gate_presets, "_has_executable", lambda _argv: True)

    config_default = resolve_quality_gate_config(tmp_path)
    config_none = resolve_quality_gate_config(tmp_path, file_path=None)
    assert config_default == config_none


def test_preset_table_uses_list_of_argvs() -> None:
    for language, preset in QUALITY_GATE_PRESETS.items():
        assert isinstance(preset.get("extensions"), list)
        assert isinstance(preset.get("bash"), list)
        for argv in preset["bash"]:
            assert isinstance(argv, list), f"{language}: bash entries must be argv lists"
            assert argv, f"{language}: argv must be non-empty"
