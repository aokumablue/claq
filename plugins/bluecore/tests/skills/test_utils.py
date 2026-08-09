"""Tests for skills utility helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from bluecore.skills.utils import parse_skill_md


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



def test_parse_skill_md_ignores_other_frontmatter_lines(tmp_path) -> None:
    """name/description 以外の frontmatter 行はスキップする。"""
    from bluecore.skills.utils import parse_skill_md

    (tmp_path / "SKILL.md").write_text(
        "---\nname: myskill\nother: value\ndescription: desc\n---\nbody text\n", encoding="utf-8"
    )
    name, description, _ = parse_skill_md(tmp_path)
    assert name == "myskill"
    assert description == "desc"


def test_parse_skill_md_returns_empty_fields_for_empty_frontmatter(tmp_path: Path) -> None:
    """frontmatter が空なら name と description は空文字列になる。"""
    content = "---\n---\nBody\n"
    skill_dir = _write_skill(tmp_path, content)

    assert parse_skill_md(skill_dir) == ("", "", content)


def test_parse_skill_md_returns_empty_fields_for_missing_keys(tmp_path: Path) -> None:
    """name と description が無い frontmatter では空文字列を返す。"""
    skill_dir = _write_skill(tmp_path, "---\nmodel: opus\n---\n# Body\n")

    name, description, _ = parse_skill_md(skill_dir)

    assert name == ""
    assert description == ""


def test_parse_skill_md_renders_booleans_as_yaml_literals(tmp_path: Path) -> None:
    """真偽値は Python の True/False ではなく YAML 表記の文字列で返す。"""
    skill_dir = _write_skill(tmp_path, "---\nname: true\ndescription: false\n---\n# Body\n")

    name, description, _ = parse_skill_md(skill_dir)

    assert name == "true"
    assert description == "false"
