"""CI 検証モジュールのテスト。

エージェント、コマンド、フック、ルール、スキル、
インストールマニフェスト、Unicode 安全性、個人パス検出の検証を対象とする。
"""

from __future__ import annotations

from pathlib import Path

import devgear.ci.check_unicode_safety as check_unicode_safety
import devgear.ci.validate_agents as validate_agents
import devgear.ci.validate_commands as validate_commands
import devgear.ci.validate_no_personal_paths as validate_no_personal_paths
import devgear.ci.validate_rules as validate_rules
import devgear.ci.validate_skills as validate_skills


def test_validate_agents_accepts_valid_agent(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "planner.md").write_text("---\nmodel: sonnet\ntools: bash\n---\n# Planner\n", encoding="utf-8")

    assert validate_agents.validate_agents(agents_dir) == 0


def test_validate_commands_flags_invalid_references(tmp_path: Path) -> None:
    root = tmp_path
    commands_dir = root / "commands"
    agents_dir = root / "agents"
    skills_dir = root / "skills"
    commands_dir.mkdir()
    agents_dir.mkdir()
    skills_dir.mkdir()
    (commands_dir / "build.md").write_text("Use `/c-missing-command` and agents/a-missing.md\n", encoding="utf-8")

    assert validate_commands.validate_commands(root, commands_dir, agents_dir, skills_dir) == 1


def test_validate_rules_rejects_empty_files(tmp_path: Path) -> None:
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "security").mkdir()
    (rules_dir / "security" / "policy.md").write_text("", encoding="utf-8")

    assert validate_rules.validate_rules(rules_dir) == 1


def test_validate_skills_accepts_skill_directory(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill_dir = skills_dir / "planner"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Planner\n", encoding="utf-8")

    assert validate_skills.validate_skills(skills_dir) == 0


def test_validate_no_personal_paths_flags_hardcoded_path(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("Use /Users/affoon/project for setup.\n", encoding="utf-8")

    assert validate_no_personal_paths.validate_no_personal_paths(tmp_path) == 1


def test_check_unicode_safety_sanitizes_and_flags(tmp_path: Path) -> None:
    doc = tmp_path / "README.md"
    doc.write_text("⚠️  Zero​Width and ✅ emoji\n", encoding="utf-8")

    assert check_unicode_safety.validate_unicode_safety(tmp_path, write_mode=True) == 0
    assert "WARNING:" in doc.read_text(encoding="utf-8")


def test_check_unicode_safety_helpers_detect_invisible_and_emoji() -> None:
    assert check_unicode_safety.collect_dangerous_invisible_matches("x\u200by")
    assert check_unicode_safety.collect_emoji_matches("🙂")
