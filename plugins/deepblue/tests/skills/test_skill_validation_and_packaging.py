"""Tests for skill validation and packaging helpers."""

from __future__ import annotations

import runpy
import sys
import zipfile
from pathlib import Path

import pytest

from devgear.skills import package_skill as pkg
from devgear.skills import quick_validate as qv


def _write_skill_md(skill_dir: Path, content: str) -> None:
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content)


def test_validate_skill_accepts_valid_skill(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    _write_skill_md(
        skill_dir,
        "---\n"
        "name: sample-skill\n"
        "description: sample description\n"
        "license: MIT\n"
        "allowed-tools: [Read, Write]\n"
        "metadata:\n"
        "  owner: team-a\n"
        "compatibility: claude-code\n"
        "---\n"
        "Body\n",
    )

    valid, message = qv.validate_skill(skill_dir)

    assert valid is True
    assert message == "スキルは有効です"


@pytest.mark.parametrize(
    ("content", "expected_message"),
    [
        (None, "SKILL.md が見つかりません"),
        ("name: skill\ndescription: desc", "YAML frontmatter が見つかりません"),
        ("---\nname: skill\ndescription: desc\nbody", "frontmatter の形式が不正です"),
        ("---\nname: [\ndescription: desc\n---\nBody\n", "frontmatter 内の YAML が不正です"),
        ("---\n- item\n---\nBody\n", "frontmatter は YAML の辞書である必要があります"),
        ("---\nname: skill\ndescription: desc\nfoo: bar\n---\nBody\n", "想定外のキーがあります"),
        ("---\ndescription: desc\n---\nBody\n", "frontmatter に 'name' がありません"),
        ("---\nname: skill\n---\nBody\n", "frontmatter に 'description' がありません"),
        ("---\nname: 123\ndescription: desc\n---\nBody\n", "name は文字列である必要があります"),
        ("---\nname: BadName\ndescription: desc\n---\nBody\n", "kebab-case"),
        ("---\nname: -bad\ndescription: desc\n---\nBody\n", "先頭/末尾にハイフン"),
        ("---\nname: " + "a" * 65 + "\ndescription: desc\n---\nBody\n", "name が長すぎます"),
        ("---\nname: skill\ndescription: 123\n---\nBody\n", "description は文字列である必要があります"),
        ("---\nname: skill\ndescription: bad <desc>\n---\nBody\n", "山括弧"),
        ("---\nname: skill\ndescription: " + "x" * 1025 + "\n---\nBody\n", "description が長すぎます"),
        ("---\nname: skill\ndescription: desc\ncompatibility: [a, b]\n---\nBody\n", "compatibility は文字列である必要があります"),
        ("---\nname: skill\ndescription: desc\ncompatibility: " + "x" * 501 + "\n---\nBody\n", "compatibility が長すぎます"),
    ],
)
def test_validate_skill_rejects_invalid_inputs(
    tmp_path: Path, content: str | None, expected_message: str
) -> None:
    skill_dir = tmp_path / "skill"
    if content is not None:
        _write_skill_md(skill_dir, content)

    valid, message = qv.validate_skill(skill_dir)

    assert valid is False
    assert expected_message in message


def test_quick_validate_cli_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    skill_dir = tmp_path / "skill"
    _write_skill_md(skill_dir, "---\nname: skill\ndescription: desc\n---\nBody\n")
    monkeypatch.setattr(sys, "argv", ["quick_validate.py", str(skill_dir)])

    with pytest.raises(SystemExit) as exc_info:
        runpy.run_path(str(Path(qv.__file__).resolve()), run_name="__main__")

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == "スキルは有効です"


def test_quick_validate_cli_usage_error(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["quick_validate.py"])

    with pytest.raises(SystemExit) as exc_info:
        runpy.run_path(str(Path(qv.__file__).resolve()), run_name="__main__")

    assert exc_info.value.code == 1
    assert "使い方: python quick_validate.py <skill_directory>" in capsys.readouterr().out


def test_should_exclude_matches_expected_paths() -> None:
    cases = [
        (Path("skill/__pycache__/mod.py"), True),
        (Path("skill/node_modules/pkg/index.js"), True),
        (Path("skill/evals/root.txt"), True),
        (Path("skill/sub/evals/nested.txt"), False),
        (Path("skill/.DS_Store"), True),
        (Path("skill/build/file.pyc"), True),
        (Path("skill/src/app.py"), False),
    ]

    for rel_path, expected in cases:
        assert pkg.should_exclude(rel_path) is expected


def test_package_skill_rejects_invalid_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "missing"
    assert pkg.package_skill(missing) is None

    file_path = tmp_path / "file.txt"
    file_path.write_text("content")
    assert pkg.package_skill(file_path) is None

    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    assert pkg.package_skill(skill_dir) is None

    _write_skill_md(skill_dir, "---\nname: skill\ndescription: desc\n---\nBody\n")
    monkeypatch.setattr(pkg, "validate_skill", lambda path: (False, "bad skill"))

    assert pkg.package_skill(skill_dir) is None


def test_package_skill_creates_archive(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    _write_skill_md(skill_dir, "---\nname: skill\ndescription: desc\n---\nBody\n")
    (skill_dir / "src").mkdir()
    (skill_dir / "src" / "app.py").write_text("print('ok')\n")
    (skill_dir / "src" / "__pycache__").mkdir()
    (skill_dir / "src" / "__pycache__" / "skip.pyc").write_bytes(b"")
    (skill_dir / "evals").mkdir()
    (skill_dir / "evals" / "root.txt").write_text("skip")
    nested = skill_dir / "nested"
    nested.mkdir()
    (nested / "evals").mkdir()
    (nested / "evals" / "keep.txt").write_text("keep")
    (skill_dir / ".DS_Store").write_text("skip")

    output_dir = tmp_path / "dist"
    archive = pkg.package_skill(skill_dir, output_dir)

    assert archive == output_dir / "skill.skill"
    assert archive.exists()

    with zipfile.ZipFile(archive) as zipf:
        names = set(zipf.namelist())

    assert "skill/SKILL.md" in names
    assert "skill/src/app.py" in names
    assert "skill/nested/evals/keep.txt" in names
    assert "skill/src/__pycache__/skip.pyc" not in names
    assert "skill/evals/root.txt" not in names
    assert "skill/.DS_Store" not in names


def test_package_skill_uses_cwd_when_output_dir_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    skill_dir = tmp_path / "skill"
    _write_skill_md(skill_dir, "---\nname: skill\ndescription: desc\n---\nBody\n")
    monkeypatch.chdir(tmp_path)

    archive = pkg.package_skill(skill_dir)

    assert archive == tmp_path / "skill.skill"
    assert archive.exists()


def test_package_skill_returns_none_when_zip_creation_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    skill_dir = tmp_path / "skill"
    _write_skill_md(skill_dir, "---\nname: skill\ndescription: desc\n---\nBody\n")

    class BrokenZipFile:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            pass

        def __enter__(self):
            raise RuntimeError("zip failure")

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(pkg.zipfile, "ZipFile", BrokenZipFile)

    assert pkg.package_skill(skill_dir, tmp_path / "dist") is None


def test_package_skill_main_usage_error(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["package_skill.py"])

    with pytest.raises(SystemExit) as exc_info:
        pkg.main()

    assert exc_info.value.code == 1
    assert "使い方: python utils/package_skill.py <path/to/skill-folder> [output-directory]" in capsys.readouterr().out


def test_package_skill_main_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    skill_dir = tmp_path / "skill"
    _write_skill_md(skill_dir, "---\nname: skill\ndescription: desc\n---\nBody\n")
    (skill_dir / "src").mkdir()
    (skill_dir / "src" / "app.py").write_text("print('ok')\n")
    output_dir = tmp_path / "dist"
    monkeypatch.setattr(sys, "argv", ["package_skill.py", str(skill_dir), str(output_dir)])

    with pytest.raises(SystemExit) as exc_info:
        pkg.main()

    assert exc_info.value.code == 0
    assert "スキルをパッケージ化しています" in capsys.readouterr().out
    assert (output_dir / "skill.skill").exists()


def test_package_skill_main_returns_error_when_packager_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    skill_dir = tmp_path / "skill"
    _write_skill_md(skill_dir, "---\nname: skill\ndescription: desc\n---\nBody\n")
    monkeypatch.setattr(pkg, "package_skill", lambda skill_path, output_dir=None: None)
    monkeypatch.setattr(sys, "argv", ["package_skill.py", str(skill_dir)])

    with pytest.raises(SystemExit) as exc_info:
        pkg.main()

    assert exc_info.value.code == 1


def test_package_skill_module_entrypoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    skill_dir = tmp_path / "skill"
    _write_skill_md(skill_dir, "---\nname: skill\ndescription: desc\n---\nBody\n")
    (skill_dir / "src").mkdir()
    (skill_dir / "src" / "app.py").write_text("print('ok')\n")
    monkeypatch.setattr(sys, "argv", ["package_skill.py", str(skill_dir)])

    with pytest.raises(SystemExit) as exc_info:
        runpy.run_module("devgear.skills.package_skill", run_name="__main__")

    assert exc_info.value.code in (0, None)
