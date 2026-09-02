"""Additional coverage for CI validator modules."""

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

import pytest

from ple4.ci import (
    validate_agents,
    validate_commands,
    validate_hooks,
    validate_skills,
)


def _raise_oserror_on_read(monkeypatch: pytest.MonkeyPatch, broken_file: Path) -> None:
    """指定パスの Path.read_text だけ OSError にする。"""
    original_read_text = Path.read_text

    def fake_read_text(self: Path, *args, **kwargs):  # noqa: ANN001
        if self == broken_file:
            raise OSError("boom")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)


def test_validate_skills_handles_missing_dir_and_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "missing-skills"
    # 既定では欠落を失敗として扱う（F-03）。--optional でだけスキップする。
    assert validate_skills.validate_skills(missing) == 1
    assert "見つかりません" in capsys.readouterr().err
    assert validate_skills.validate_skills(missing, optional=True) == 0
    assert "検証をスキップします" in capsys.readouterr().out

    skills_dir = tmp_path / "skills"
    for name in ("alpha", "beta"):
        skill_dir = skills_dir / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: いつ呼ぶかの説明\n---\n\n# Skill\n", encoding="utf-8"
    )

    assert validate_skills.validate_skills(skills_dir) == 0
    assert "2 個のスキルディレクトリを検証しました" in capsys.readouterr().out


def test_validate_skills_reports_missing_skill_md(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "broken"
    skill_dir.mkdir(parents=True)

    assert validate_skills.validate_skills(skills_dir) == 1
    assert "SKILL.md が見つかりません" in capsys.readouterr().err


def test_validate_skills_reports_empty_and_read_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    skills_dir = tmp_path / "skills"
    ok_dir = skills_dir / "ok"
    empty_dir = skills_dir / "empty"
    broken_dir = skills_dir / "broken"
    ok_dir.mkdir(parents=True)
    empty_dir.mkdir()
    broken_dir.mkdir()
    (ok_dir / "SKILL.md").write_text("# OK\n", encoding="utf-8")
    (empty_dir / "SKILL.md").write_text("", encoding="utf-8")
    broken_file = broken_dir / "SKILL.md"
    broken_file.write_text("broken", encoding="utf-8")
    _raise_oserror_on_read(monkeypatch, broken_file)

    assert validate_skills.validate_skills(skills_dir) == 1
    stderr = capsys.readouterr().err
    assert "SKILL.md - ファイルが空です" in stderr
    assert "SKILL.md - ファイルの読み取りに失敗しました" in stderr


def test_validate_agents_handles_valid_bom_crlf_and_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "planner.md").write_text(
        "\ufeff---\r\nmodel: sonnet\r\ntools: Read\r\n---\r\n# Planner\r\n", encoding="utf-8"
    )
    (agents_dir / "missing_frontmatter.md").write_text("plain text", encoding="utf-8")
    broken_file = agents_dir / "broken.md"
    broken_file.write_text("---\nmodel: sonnet\n---\n", encoding="utf-8")
    _raise_oserror_on_read(monkeypatch, broken_file)

    assert validate_agents.validate_agents(agents_dir) == 1
    stderr = capsys.readouterr().err
    assert "フロントマターがありません" in stderr
    assert "ファイルの読み取りに失敗しました" in stderr


def test_validate_agents_missing_dir_fails_unless_optional(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing-agents"
    # 既定では欠落を失敗として扱う（F-03）。--optional でだけスキップする。
    assert validate_agents.validate_agents(missing) == 1
    assert "見つかりません" in capsys.readouterr().err
    assert validate_agents.validate_agents(missing, optional=True) == 0
    assert "検証をスキップします" in capsys.readouterr().out


def test_validate_agents_accepts_valid_bom_crlf_file(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "planner.md").write_text(
        "\ufeff---\r\nmodel: opus\r\ntools: Read\r\n---\r\n# Planner\r\n", encoding="utf-8"
    )

    assert validate_agents.validate_agents(agents_dir) == 0


def test_validate_agents_allows_missing_model_and_inherit(tmp_path: Path) -> None:
    """model は任意: 未指定でも model: inherit でも PASS する。"""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "no_model.md").write_text(
        "---\nname: agent\ntools: Read\n---\n# No model\n", encoding="utf-8"
    )
    (agents_dir / "inherit_model.md").write_text(
        "---\nmodel: inherit\ntools: Read\n---\n# Inherit\n", encoding="utf-8"
    )

    assert validate_agents.validate_agents(agents_dir) == 0


def test_validate_commands_covers_warnings_and_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path
    commands_dir = root / "commands"
    agents_dir = root / "agents"
    skills_dir = root / "skills"
    commands_dir.mkdir()
    agents_dir.mkdir()
    (skills_dir / "clean").mkdir(parents=True)

    (commands_dir / "clean.md").write_text("Clean command.\n", encoding="utf-8")
    (agents_dir / "clean.md").write_text("Agent.\n", encoding="utf-8")
    (agents_dir / "reviewer.md").write_text("Agent.\n", encoding="utf-8")
    (commands_dir / "build.md").write_text(
        "Use `/clean` and agents/clean.md.\n"
        "clean -> reviewer\n"
        "creates: `/missing`\n"
        "skills/clean/docs\n"
        "skills/missing/docs\n"
        "```bash\n"
        "/missing-inside-code\n"
        "agents/missing.md\n"
        "```\n",
        encoding="utf-8",
    )

    assert validate_commands.validate_commands(root, commands_dir, agents_dir, skills_dir) == 0
    stdout = capsys.readouterr().out
    assert "1 件の警告" in stdout


def test_validate_commands_skips_missing_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path
    assert validate_commands.validate_commands(root, root / "commands", root / "agents", root / "skills") == 0
    assert "検証をスキップします" in capsys.readouterr().out


def test_validate_commands_reports_errors_and_io_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path
    commands_dir = root / "commands"
    agents_dir = root / "agents"
    skills_dir = root / "skills"
    commands_dir.mkdir()
    agents_dir.mkdir()
    skills_dir.mkdir()
    (commands_dir / "existing.md").write_text("Existing command.\n", encoding="utf-8")
    empty_file = commands_dir / "empty.md"
    empty_file.write_text("", encoding="utf-8")
    broken_file = commands_dir / "broken.md"
    broken_file.write_text("Broken command.\n", encoding="utf-8")
    (agents_dir / "existing.md").write_text("Agent.\n", encoding="utf-8")
    _raise_oserror_on_read(monkeypatch, broken_file)
    (commands_dir / "bad.md").write_text(
        "Use `/missing` and agents/missing.md.\n"
        "existing -> missing\n",
        encoding="utf-8",
    )

    assert validate_commands.validate_commands(root, commands_dir, agents_dir, skills_dir) == 1
    stderr = capsys.readouterr().err
    assert "コマンドファイルが空です" in stderr
    assert "ファイルの読み取りに失敗しました" in stderr
    assert "存在しないコマンド /missing" in stderr
    assert "存在しないエージェント agents/missing.md" in stderr
    assert "存在しないエージェント \"missing\"" in stderr


def test_validator_main_entrypoints(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "alpha"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: いつ呼ぶかの説明\n---\n\n# Skill\n", encoding="utf-8"
    )

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "alpha.md").write_text("---\nmodel: sonnet\ntools: Read\n---\n", encoding="utf-8")

    commands_dir = tmp_path / "commands"
    commands_dir.mkdir()
    (commands_dir / "alpha.md").write_text("Use `/alpha`.\n", encoding="utf-8")

    assert validate_skills.main(["--skills-dir", str(skills_dir)]) == 0
    assert validate_agents.main(["--agents-dir", str(agents_dir)]) == 0
    assert validate_commands.main(
        [
            "--root-dir",
            str(tmp_path),
            "--commands-dir",
            str(commands_dir),
            "--agents-dir",
            str(agents_dir),
            "--skills-dir",
            str(skills_dir),
        ]
    ) == 0

    stdout = capsys.readouterr().out
    assert "1 個のスキルディレクトリを検証しました" in stdout
    assert "1 個のエージェントファイルを検証しました" in stdout
    assert "1 個のコマンドファイルを検証しました" in stdout

    entrypoints = [
        ("ple4.ci.validate_skills", ["--skills-dir", str(skills_dir)]),
        ("ple4.ci.validate_agents", ["--agents-dir", str(agents_dir)]),
        (
            "ple4.ci.validate_commands",
            [
                "--root-dir",
                str(tmp_path),
                "--commands-dir",
                str(commands_dir),
                "--agents-dir",
                str(agents_dir),
                "--skills-dir",
                str(skills_dir),
            ],
        ),
    ]

    for module_name, argv in entrypoints:
        monkeypatch.setattr(sys, "argv", [module_name.rsplit(".", 1)[-1], *argv])
        with pytest.raises(SystemExit) as excinfo:
            runpy.run_module(module_name, run_name="__main__")
        assert excinfo.value.code == 0


def test_validate_agents_accepts_quoted_model_values(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "double.md").write_text('---\nmodel: "sonnet"\ntools: Read\n---\n', encoding="utf-8")
    (agents_dir / "single.md").write_text("---\nmodel: 'opus'\ntools: Read\n---\n", encoding="utf-8")

    assert validate_agents.validate_agents(agents_dir) == 0


def test_validate_hooks_main_without_schema_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    hooks_file = tmp_path / "hooks.json"
    matcher = {"matcher": ".", "hooks": [{"type": "command", "command": "echo hi"}]}
    hooks_file.write_text(
        json.dumps(dict.fromkeys(validate_hooks.REQUIRED_EVENTS, [matcher])),
        encoding="utf-8",
    )

    assert validate_hooks.main(["--hooks-file", str(hooks_file)]) == 0
    assert "個のフックマッチャーを検証しました" in capsys.readouterr().out


def test_validate_agents_accepts_quoted_model_with_spaces(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "leading.md").write_text('---\nmodel: " sonnet"\ntools: Read\n---\n', encoding="utf-8")
    (agents_dir / "trailing.md").write_text('---\nmodel: "opus "\ntools: Read\n---\n', encoding="utf-8")
    (agents_dir / "both.md").write_text("---\nmodel: ' haiku '\ntools: Read\n---\n", encoding="utf-8")

    assert validate_agents.validate_agents(agents_dir) == 0


def test_validate_commands_resolves_relative_dirs_via_root_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    commands_dir = tmp_path / "commands"
    agents_dir = tmp_path / "agents"
    skills_dir = tmp_path / "skills"
    commands_dir.mkdir()
    agents_dir.mkdir()
    skills_dir.mkdir()
    (commands_dir / "test.md").write_text("Test command.\n", encoding="utf-8")

    assert validate_commands.validate_commands(tmp_path, "commands", "agents", "skills") == 0
    assert "1 個のコマンドファイルを検証しました" in capsys.readouterr().out
