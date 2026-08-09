"""bluecore.skills.learn.storage のテスト。

観測ログの保存先が ``repos.id``（人間可読スラッグ）配下に落ちること、
``repos`` 台帳への登録が観測開始時点で済むことを確認する。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import bluecore.mem.settings as settings_mod
from bluecore.mem.database import Database
from bluecore.skills.learn import storage


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """データディレクトリを tmp 配下に差し替えて返す。"""
    root = tmp_path / "bluecore"
    monkeypatch.setattr(settings_mod, "_DEFAULT_DATA_DIR", root)
    return root


def test_repos_root_follows_settings(data_dir: Path) -> None:
    """観測ログのルートは <data_path>/repos。"""
    assert storage.repos_root() == data_dir / "repos"


def test_observation_target_paths(data_dir: Path, tmp_path: Path) -> None:
    """repo_id をディレクトリ名に使い、ファイル生成は行わない。"""
    target = storage.observation_target("bluecore-dev", tmp_path / "src")
    assert target.storage_dir == data_dir / "repos" / "bluecore-dev"
    assert target.observations_file == target.storage_dir / "observations.jsonl"
    assert target.archive_dir == target.storage_dir / "observations.archive"
    assert not target.storage_dir.exists()


def test_ensure_storage_dirs_creates_archive(data_dir: Path, tmp_path: Path) -> None:
    """アーカイブディレクトリまで作る（観測ログの親も同時に作られる）。"""
    target = storage.observation_target("bluecore-dev", tmp_path / "src")
    storage.ensure_storage_dirs(target)
    assert target.archive_dir.is_dir()
    assert target.observations_file.parent.is_dir()


def test_resolve_registers_repo_and_uses_its_slug(data_dir: Path, tmp_path: Path) -> None:
    """解決時に repos 行を作り、その id を保存先ディレクトリ名にする。"""
    repo_root = tmp_path / "My Project"
    repo_root.mkdir()

    target = storage.resolve_observation_target(str(repo_root))
    assert target.repo_id == "my-project"
    assert target.storage_dir == data_dir / "repos" / "my-project"
    assert target.repo_root == Path(os.path.realpath(repo_root))

    with Database(data_dir / "mem.db") as db:
        assert [repo.id for repo in db.list_repos()] == ["my-project"]


def test_resolve_reuses_same_slug_for_same_repo(data_dir: Path, tmp_path: Path) -> None:
    """同じリポジトリを 2 回解決しても保存先は 1 つに収束する。"""
    repo_root = tmp_path / "stable"
    repo_root.mkdir()

    first = storage.resolve_observation_target(str(repo_root))
    second = storage.resolve_observation_target(str(repo_root))
    assert first == second
    with Database(data_dir / "mem.db") as db:
        assert len(db.list_repos()) == 1
