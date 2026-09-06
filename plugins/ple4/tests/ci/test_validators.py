"""CI 検証モジュールのテスト。

エージェント、コマンド、フック、スキルの検証を対象とする。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import ple4.ci.validate_agents as validate_agents
import ple4.ci.validate_commands as validate_commands
import ple4.ci.validate_skills as validate_skills


def test_validate_agents_accepts_valid_agent(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "planner.md").write_text(
        "---\nname: planner\ndescription: d\nmodel: sonnet\ntools: Read\n---\n# Planner\n",
        encoding="utf-8",
    )

    assert validate_agents.validate_agents(agents_dir) == 0


def test_validate_agents_rejects_agent_without_tools_declaration(tmp_path: Path) -> None:
    """tools frontmatter が無いエージェントは FAIL する（暗黙の全権継承を防ぐため必須）。"""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "agent.md").write_text(
        "---\nname: agent\ndescription: A portable custom agent.\n---\n",
        encoding="utf-8",
    )

    assert validate_agents.validate_agents(agents_dir) == 1


def test_validate_agents_allows_any_custom_tool_names(tmp_path: Path) -> None:
    """tools の値自体は allowlist で縛らない。宣言の有無だけを見る。"""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "agent.md").write_text(
        "---\nname: agent\ndescription: A portable custom agent.\ntools: SomeCustomTool\n---\n",
        encoding="utf-8",
    )

    assert validate_agents.validate_agents(agents_dir) == 0


def test_validate_commands_flags_invalid_references(tmp_path: Path) -> None:
    root = tmp_path
    commands_dir = root / "commands"
    agents_dir = root / "agents"
    skills_dir = root / "skills"
    commands_dir.mkdir()
    agents_dir.mkdir()
    skills_dir.mkdir()
    (commands_dir / "build.md").write_text("Use `/missing-command` and agents/missing.md\n", encoding="utf-8")

    assert validate_commands.validate_commands(root, commands_dir, agents_dir, skills_dir) == 1


def test_validate_skills_accepts_skill_directory(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill_dir = skills_dir / "planner"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: planner\ndescription: いつ呼ぶかの説明\n---\n\n# Planner\n", encoding="utf-8"
    )

    assert validate_skills.validate_skills(skills_dir) == 0


def test_extract_frontmatter_skips_lines_without_colon() -> None:
    """コロンが無い frontmatter 行はスキップする。"""
    from ple4.ci.validate_agents import extract_frontmatter

    fm = extract_frontmatter("---\nname: x\nnocolon\n---\nbody")
    assert fm == {"name": "x"}


def test_extract_frontmatter_returns_none_for_non_mapping_frontmatter() -> None:
    """トップレベルが辞書でない frontmatter は None を返す。"""
    from ple4.ci.validate_agents import extract_frontmatter

    assert extract_frontmatter("---\n- item\n---\nbody") is None


def test_validate_skills_requires_frontmatter_fields(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """SKILL.md の frontmatter に name / description が無ければ失敗すること（F-03）。

    agents は各ファイルの frontmatter を検査するのに skills は「存在する・
    読める・空でない」しか見ておらず、非対称が残っていた。
    """
    skills_dir = tmp_path / "skills"
    (skills_dir / "no-frontmatter").mkdir(parents=True)
    (skills_dir / "no-frontmatter" / "SKILL.md").write_text("# heading only\n", encoding="utf-8")

    assert validate_skills.validate_skills(skills_dir) == 1
    assert "フロントマターがありません" in capsys.readouterr().err

    partial = tmp_path / "partial"
    (partial / "half").mkdir(parents=True)
    (partial / "half" / "SKILL.md").write_text("---\nname: half\n---\n\n# Half\n", encoding="utf-8")

    assert validate_skills.validate_skills(partial) == 1
    assert "description がありません" in capsys.readouterr().err
