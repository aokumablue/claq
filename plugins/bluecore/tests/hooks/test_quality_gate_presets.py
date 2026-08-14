"""quality-gate 言語プリセットのテスト。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bluecore.hooks import quality_gate_presets
from bluecore.hooks.quality_gate_presets import QUALITY_GATE_PRESETS, _select_language, resolve_quality_gate_config
from bluecore.lib.project_detect import ProjectInfo


def _project_info(
    root: Path,
    *,
    primary: str | None,
    languages: list[str] | None = None,
) -> ProjectInfo:
    """テスト用の ProjectInfo を組み立てる。"""
    resolved_languages = languages if languages is not None else ([primary] if primary else [])
    return ProjectInfo(root=root, languages=resolved_languages, frameworks=[], primary_language=primary)


def _stub_detect(monkeypatch: pytest.MonkeyPatch, info: ProjectInfo) -> None:
    """detect_project を固定の ProjectInfo を返すように差し替える。"""
    monkeypatch.setattr(quality_gate_presets, "detect_project", lambda _root: info)


def _allow_all_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """PATH 上のツール有無判定を常に True にする。"""
    monkeypatch.setattr(quality_gate_presets, "_has_executable", lambda _argv: True)


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
    _stub_detect(monkeypatch, _project_info(tmp_path, primary=language))
    _allow_all_tools(monkeypatch)

    config = resolve_quality_gate_config(tmp_path)
    rules = config["actions"]["post-edit"]["rules"]
    assert len(rules) == 1
    assert rules[0]["extensions"] == expected_extensions
    assert rules[0]["steps"], "steps should be non-empty when tools are available"


def test_resolve_returns_empty_rules_when_language_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_detect(monkeypatch, _project_info(tmp_path, primary=None))

    config = resolve_quality_gate_config(tmp_path)
    assert config == {"actions": {"post-edit": {"rules": []}}}


def test_resolve_falls_back_to_supported_language(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_detect(
        monkeypatch,
        _project_info(tmp_path, primary="brainfuck", languages=["brainfuck", "ruby"]),
    )
    _allow_all_tools(monkeypatch)

    config = resolve_quality_gate_config(tmp_path)
    rules = config["actions"]["post-edit"]["rules"]
    assert len(rules) == 1
    assert rules[0]["extensions"] == [".rb", ".rake"]


def test_resolve_uses_repo_root_when_src_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_detect(monkeypatch, _project_info(tmp_path, primary="python"))
    _allow_all_tools(monkeypatch)

    config = resolve_quality_gate_config(tmp_path)
    rules = config["actions"]["post-edit"]["rules"]
    assert rules[0]["steps"][0]["argv"][-1] == "."


def test_resolve_skips_steps_when_tools_not_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_detect(monkeypatch, _project_info(tmp_path, primary="python"))
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

    _stub_detect(monkeypatch, _project_info(tmp_path, primary="python"))
    assert quality_gate_presets._has_executable([]) is False
    # shutil.which による実判定（見つかる/見つからない双方の分岐）を直接確認する。
    assert quality_gate_presets._has_executable(["python3"]) is True
    assert quality_gate_presets._has_executable(["definitely-not-a-real-command-xyz"]) is False
    _allow_all_tools(monkeypatch)
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
    _stub_detect(monkeypatch, _project_info(tmp_path, primary="python"))
    _allow_all_tools(monkeypatch)

    fp = "/path/to/module.py"
    config = resolve_quality_gate_config(tmp_path, file_path=fp)
    rules = config["actions"]["post-edit"]["rules"]
    assert len(rules) == 1
    assert rules[0]["steps"][0]["argv"][-1] == fp


def test_resolve_ignores_file_path_for_non_python_extension(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """file_path が .py でない場合は既存の target に戻ること。"""
    _stub_detect(monkeypatch, _project_info(tmp_path, primary="python"))
    _allow_all_tools(monkeypatch)

    config = resolve_quality_gate_config(tmp_path, file_path="/path/to/file.txt")
    rules = config["actions"]["post-edit"]["rules"]
    assert len(rules) == 1
    assert rules[0]["steps"][0]["argv"][-1] != "/path/to/file.txt"


def test_resolve_file_path_none_uses_default_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """file_path=None の場合は既存の target を使うこと（後方互換）。"""
    _stub_detect(monkeypatch, _project_info(tmp_path, primary="python"))
    _allow_all_tools(monkeypatch)

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


def test_select_language_non_list_languages() -> None:
    """languages が list でなければ primary_language のみ採用する。"""
    from types import SimpleNamespace

    info = SimpleNamespace(primary_language="python", languages="notalist")
    assert _select_language(info) == "python"
