"""Additional coverage for CI validator modules."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest

from devgear.ci import (
    validate_agents,
    validate_commands,
    validate_hooks,
    validate_no_personal_paths,
    validate_rules,
    validate_skills,
)


def test_validate_skills_handles_missing_dir_and_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "missing-skills"
    assert validate_skills.validate_skills(missing) == 0
    assert "検証をスキップします" in capsys.readouterr().out

    skills_dir = tmp_path / "skills"
    for name in ("alpha", "beta"):
        skill_dir = skills_dir / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Skill\n", encoding="utf-8")

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

    original_read_text = Path.read_text

    def fake_read_text(self: Path, *args, **kwargs):  # noqa: ANN001
        if self == broken_file:
            raise OSError("boom")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    assert validate_skills.validate_skills(skills_dir) == 1
    stderr = capsys.readouterr().err
    assert "SKILL.md - ファイルが空です" in stderr
    assert "SKILL.md - ファイルの読み取りに失敗しました" in stderr


def test_validate_agents_handles_valid_bom_crlf_and_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "planner.md").write_text("\ufeff---\r\nmodel: sonnet\r\ntools: bash\r\n---\r\n# Planner\r\n", encoding="utf-8")
    (agents_dir / "missing_frontmatter.md").write_text("plain text", encoding="utf-8")
    (agents_dir / "missing_fields.md").write_text("---\nmodel: sonnet\n---\n", encoding="utf-8")
    (agents_dir / "invalid_model.md").write_text("---\nmodel: gemini\ntools: bash\n---\n", encoding="utf-8")
    broken_file = agents_dir / "broken.md"
    broken_file.write_text("---\nmodel: sonnet\ntools: bash\n---\n", encoding="utf-8")

    original_read_text = Path.read_text

    def fake_read_text(self: Path, *args, **kwargs):  # noqa: ANN001
        if self == broken_file:
            raise OSError("boom")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    assert validate_agents.validate_agents(agents_dir) == 1
    stderr = capsys.readouterr().err
    assert "フロントマターがありません" in stderr
    assert "必須フィールドが不足しています: tools" in stderr
    assert "モデル 'gemini' は無効です" in stderr
    assert "ファイルの読み取りに失敗しました" in stderr


def test_validate_agents_skips_missing_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert validate_agents.validate_agents(tmp_path / "missing-agents") == 0
    assert "検証をスキップします" in capsys.readouterr().out


def test_validate_agents_accepts_valid_bom_crlf_file(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "planner.md").write_text("\ufeff---\r\nmodel: opus\r\ntools: bash\r\n---\r\n# Planner\r\n", encoding="utf-8")

    assert validate_agents.validate_agents(agents_dir) == 0


def test_validate_commands_covers_warnings_and_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path
    commands_dir = root / "commands"
    agents_dir = root / "agents"
    skills_dir = root / "skills"
    commands_dir.mkdir()
    agents_dir.mkdir()
    (skills_dir / "s-clean").mkdir(parents=True)

    (commands_dir / "c-clean.md").write_text("Clean command.\n", encoding="utf-8")
    (agents_dir / "a-clean.md").write_text("Agent.\n", encoding="utf-8")
    (agents_dir / "a-review.md").write_text("Agent.\n", encoding="utf-8")
    (commands_dir / "build.md").write_text(
        "Use `/c-clean` and agents/a-clean.md.\n"
        "a-clean -> a-review\n"
        "creates: `/c-missing`\n"
        "skills/s-clean/docs\n"
        "skills/s-missing/docs\n"
        "```bash\n"
        "/c-missing-inside-code\n"
        "agents/a-missing.md\n"
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
    (agents_dir / "a-existing.md").write_text("Agent.\n", encoding="utf-8")

    original_read_text = Path.read_text

    def fake_read_text(self: Path, *args, **kwargs):  # noqa: ANN001
        if self == broken_file:
            raise OSError("boom")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    (commands_dir / "bad.md").write_text(
        "Use `/c-missing` and agents/a-missing.md.\n"
        "c-existing -> a-missing\n",
        encoding="utf-8",
    )

    assert validate_commands.validate_commands(root, commands_dir, agents_dir, skills_dir) == 1
    stderr = capsys.readouterr().err
    assert "コマンドファイルが空です" in stderr
    assert "ファイルの読み取りに失敗しました" in stderr
    assert "存在しないコマンド /c-missing" in stderr
    assert "存在しないエージェント agents/a-missing.md" in stderr
    assert "存在しないエージェント \"a-missing\"" in stderr


def test_validate_rules_reports_empty_and_read_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rules_dir = tmp_path / "rules"
    nested = rules_dir / "security" / "sub"
    nested.mkdir(parents=True)
    (rules_dir / "security" / "policy.md").write_text("Policy\n", encoding="utf-8")
    (nested / "notes.txt").write_text("ignore\n", encoding="utf-8")
    empty_file = nested / "empty.md"
    empty_file.write_text("", encoding="utf-8")
    broken_file = rules_dir / "broken.md"
    broken_file.write_text("Broken\n", encoding="utf-8")

    original_read_text = Path.read_text

    def fake_read_text(self: Path, *args, **kwargs):  # noqa: ANN001
        if self == broken_file:
            raise OSError("boom")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    assert validate_rules.validate_rules(rules_dir) == 1
    stderr = capsys.readouterr().err
    assert "ルールファイルが空です" in stderr
    assert "ファイルの読み取りに失敗しました" in stderr


def test_validate_rules_counts_recursive_markdown(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rules_dir = tmp_path / "rules"
    nested = rules_dir / "security" / "sub"
    nested.mkdir(parents=True)
    (rules_dir / "security" / "policy.md").write_text("Policy\n", encoding="utf-8")
    (nested / "notes.txt").write_text("ignore\n", encoding="utf-8")
    (nested / "guide.md").write_text("Guide\n", encoding="utf-8")

    assert validate_rules.validate_rules(rules_dir) == 0
    assert "2 個のルールファイルを検証しました" in capsys.readouterr().out


def test_validate_rules_skips_missing_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert validate_rules.validate_rules(tmp_path / "missing") == 0
    assert "検証をスキップします" in capsys.readouterr().out


def test_validate_no_personal_paths_covers_clean_skip_and_hits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path
    (root / "README.md").write_text("Clean docs.\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "guide.md").write_text("C:\\Users\\affoon\\secret\n", encoding="utf-8")
    (root / "docs" / "manual.txt").write_text("/Users/affoon/ignored in txt\n", encoding="utf-8")
    (root / "skills").mkdir()
    (root / "skills" / "alpha").mkdir()
    (root / "skills" / "alpha" / "SKILL.md").write_text("Clean skill.\n", encoding="utf-8")
    (root / "docs" / "node_modules").mkdir()
    (root / "docs" / "node_modules" / "ignored.md").write_text("/Users/affoon/should-skip\n", encoding="utf-8")
    (root / "commands").mkdir()
    (root / "commands" / ".git").mkdir()
    (root / "commands" / ".git" / "ignored.md").write_text("/Users/affoon/should-skip\n", encoding="utf-8")

    assert validate_no_personal_paths.validate_no_personal_paths(root) == 1
    stderr = capsys.readouterr().out
    assert "個人用パスが検出されました" in stderr


def test_validate_no_personal_paths_handles_unreadable_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path
    readme = root / "README.md"
    readme.write_text("Clean docs.\n", encoding="utf-8")

    original_read_text = Path.read_text

    def fake_read_text(self: Path, *args, **kwargs):  # noqa: ANN001
        if self == readme:
            raise OSError("boom")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    assert validate_no_personal_paths.validate_no_personal_paths(root) == 0
    assert "検証済み: 配布対象" in capsys.readouterr().out


def test_validator_main_entrypoints(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "alpha"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Skill\n", encoding="utf-8")

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "a-alpha.md").write_text("---\nmodel: sonnet\ntools: bash\n---\n", encoding="utf-8")

    commands_dir = tmp_path / "commands"
    commands_dir.mkdir()
    (commands_dir / "c-alpha.md").write_text("Use `/c-alpha`.\n", encoding="utf-8")

    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "policy.md").write_text("Policy\n", encoding="utf-8")

    docs_root = tmp_path / "docs-root"
    docs_root.mkdir()
    (docs_root / "README.md").write_text("Clean docs.\n", encoding="utf-8")

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
    assert validate_rules.main(["--rules-dir", str(rules_dir)]) == 0
    assert validate_no_personal_paths.main(["--root", str(docs_root)]) == 0

    stdout = capsys.readouterr().out
    assert "1 個のスキルディレクトリを検証しました" in stdout
    assert "1 個のエージェントファイルを検証しました" in stdout
    assert "1 個のコマンドファイルを検証しました" in stdout
    assert "1 個のルールファイルを検証しました" in stdout
    assert "検証済み: 配布対象" in stdout

    entrypoints = [
        ("devgear.ci.validate_skills", ["--skills-dir", str(skills_dir)]),
        ("devgear.ci.validate_agents", ["--agents-dir", str(agents_dir)]),
        (
            "devgear.ci.validate_commands",
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
        ("devgear.ci.validate_rules", ["--rules-dir", str(rules_dir)]),
        ("devgear.ci.validate_no_personal_paths", ["--root", str(docs_root)]),
    ]

    for module_name, argv in entrypoints:
        monkeypatch.setattr(sys, "argv", [module_name.rsplit(".", 1)[-1], *argv])
        with pytest.raises(SystemExit) as excinfo:
            runpy.run_module(module_name, run_name="__main__")
        assert excinfo.value.code == 0


def test_validate_agents_accepts_quoted_model_values(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "double.md").write_text('---\nmodel: "sonnet"\ntools: bash\n---\n', encoding="utf-8")
    (agents_dir / "single.md").write_text("---\nmodel: 'opus'\ntools: bash\n---\n", encoding="utf-8")

    assert validate_agents.validate_agents(agents_dir) == 0


def test_validate_hooks_main_without_schema_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    hooks_file = tmp_path / "hooks.json"
    hooks_file.write_text(
        '{"SessionStart": [{"matcher": ".", "hooks": [{"type": "command", "command": "echo hi"}]}]}',
        encoding="utf-8",
    )

    assert validate_hooks.main(["--hooks-file", str(hooks_file)]) == 0
    assert "1 個のフックマッチャーを検証しました" in capsys.readouterr().out


def test_validate_agents_accepts_quoted_model_with_spaces(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "leading.md").write_text('---\nmodel: " sonnet"\ntools: bash\n---\n', encoding="utf-8")
    (agents_dir / "trailing.md").write_text('---\nmodel: "opus "\ntools: bash\n---\n', encoding="utf-8")
    (agents_dir / "both.md").write_text("---\nmodel: ' haiku '\ntools: bash\n---\n", encoding="utf-8")

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
    (commands_dir / "c-test.md").write_text("Test command.\n", encoding="utf-8")

    assert validate_commands.validate_commands(tmp_path, "commands", "agents", "skills") == 0
    assert "1 個のコマンドファイルを検証しました" in capsys.readouterr().out
