"""skill evolution テスト用の共有フィクスチャ。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def skill_env(tmp_path):
    """スキルテスト向けのリポジトリ/ホームディレクトリ構成を作成する。"""
    repo_root = tmp_path / "repo"
    home_dir = tmp_path / "home"
    skills_root = repo_root / "skills"
    learned_root = home_dir / ".claude" / "skills" / "learned"
    imported_root = home_dir / ".claude" / "skills" / "imported"
    runs_file = home_dir / ".devgear" / "state" / "skill-runs.jsonl"

    for directory in [skills_root, learned_root, imported_root, runs_file.parent]:
        directory.mkdir(parents=True, exist_ok=True)

    return {
        "repo_root": repo_root,
        "home_dir": home_dir,
        "skills_root": skills_root,
        "learned_root": learned_root,
        "imported_root": imported_root,
        "runs_file": runs_file,
    }


@pytest.fixture
def now():
    """固定の ISO タイムスタンプを返す。"""
    return "2026-03-15T12:00:00.000Z"


@pytest.fixture
def make_skill():
    """スキルディレクトリを作成するヘルパーを返す。"""

    def _make(root: Path, name: str, content: str = "# Skill\n") -> Path:
        skill_dir = root / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        return skill_dir

    return _make


@pytest.fixture
def append_jsonl():
    """JSONL 行を書き込むヘルパーを返す。"""

    def _append(file_path: Path, rows: list[dict]) -> Path:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n",
            encoding="utf-8",
        )
        return file_path

    return _append
