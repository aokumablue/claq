"""Tests for skills utility helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from devgear.skills.utils import parse_skill_md


def _write_skill(tmp_path: Path, content: str) -> Path:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir


def test_parse_skill_md_returns_name_description_and_content(tmp_path: Path) -> None:
    skill_dir = _write_skill(
        tmp_path,
        "---\n"
        'name: "sample-skill"\n'
        "description: 'short description'\n"
        "---\n"
        "# Body\n",
    )

    name, description, content = parse_skill_md(skill_dir)

    assert name == "sample-skill"
    assert description == "short description"
    assert content.startswith("---\n")


def test_parse_skill_md_supports_multiline_description(tmp_path: Path) -> None:
    skill_dir = _write_skill(
        tmp_path,
        "---\n"
        "name: sample-skill\n"
        "description: >\n"
        "  first line\n"
        "  second line\n"
        "---\n"
        "# Body\n",
    )

    name, description, _ = parse_skill_md(skill_dir)

    assert name == "sample-skill"
    assert description == "first line second line"


def test_parse_skill_md_rejects_missing_frontmatter_start(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "name: sample-skill\n---\n# Body\n")

    with pytest.raises(ValueError, match="先頭の --- がない"):
        parse_skill_md(skill_dir)


def test_parse_skill_md_rejects_missing_frontmatter_end(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "---\nname: sample-skill\n# Body\n")

    with pytest.raises(ValueError, match="末尾の --- がない"):
        parse_skill_md(skill_dir)

