"""bridge.py の追加テスト。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from devgear.mem import bridge
from devgear.mem.database import Database, MemoryChunk


def _make_chunk(session_id: str = "s1", project: str = "proj") -> MemoryChunk:
    """テスト用の MemoryChunk を作成する。"""
    return MemoryChunk(
        session_id=session_id,
        project=project,
        chunk_index=0,
        content="content",
        tool_names=["Bash"],
        files_read=[],
        files_modified=[],
        user_prompt="prompt",
        created_at_epoch=1700000000,
    )


def test_get_project_id_covers_hash_and_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """_get_project_id の成功系とフォールバックを確認する。"""
    url = "https://example.com/repo.git"
    expected = hashlib.sha256(url.encode()).hexdigest()[:12]

    monkeypatch.setattr(
        bridge.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=f"{url}\n"),
    )
    assert bridge._get_project_id("proj", cwd="/tmp") == expected

    monkeypatch.setattr(bridge.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=""))
    assert bridge._get_project_id("proj", cwd="/tmp") == "proj"

    monkeypatch.setattr(bridge.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("boom")))
    assert bridge._get_project_id("proj", cwd="/tmp") == "proj"


def test_get_project_observations_path_creates_project_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_get_project_observations_path が project.json を作成すること。"""
    monkeypatch.setattr(bridge, "_DEVGEAR_DIR", tmp_path / ".devgear")

    obs_path = bridge._get_project_observations_path("proj", "project-name")
    assert obs_path.name == "observations.jsonl"
    assert obs_path.parent == tmp_path / ".devgear" / "projects" / "proj"
    project_json = obs_path.parent / "project.json"
    assert project_json.exists()
    assert '"project_id": "proj"' in project_json.read_text(encoding="utf-8")


def test_get_project_observations_path_uses_devgear_projects_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """project.json と observations.jsonl は ~/.devgear/projects 配下に作成されること。"""
    monkeypatch.setattr(bridge, "_DEVGEAR_DIR", tmp_path / ".devgear")

    legacy_dir = tmp_path / ".claude" / "c-projects" / "proj"
    legacy_dir.mkdir(parents=True)

    obs_path = bridge._get_project_observations_path("proj", "project-name")
    assert obs_path == tmp_path / ".devgear" / "projects" / "proj" / "observations.jsonl"
    assert obs_path.parent == tmp_path / ".devgear" / "projects" / "proj"
    assert (obs_path.parent / "project.json").exists()
    assert not (legacy_dir / "observations.jsonl").exists()


def test_sync_session_to_observations_skips_large_existing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """10MB 超の observations.jsonl は同期対象から外れること。"""
    db = Database(tmp_path / "test.db")
    db.store_chunk(_make_chunk())

    obs_path = tmp_path / "observations.jsonl"
    obs_path.write_bytes(b"x" * (10 * 1024 * 1024 + 1))

    monkeypatch.setattr(bridge, "_get_project_id", lambda project_name, cwd=None: "proj")
    monkeypatch.setattr(bridge, "_get_project_observations_path", lambda project_id, project_name: obs_path)

    warnings: list[str] = []
    monkeypatch.setattr(bridge.log, "warning", lambda msg, *args: warnings.append(msg % args if args else msg))

    count = bridge.sync_session_to_observations(db, "s1")
    assert count == 0
    assert any("大きすぎる" in warning for warning in warnings)
    db.close()


def test_sync_session_to_observations_handles_write_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """書き込み失敗時も例外を飲み込んで 0 を返すこと。"""
    db = Database(tmp_path / "test.db")
    db.store_chunk(_make_chunk())

    obs_path = tmp_path / "observations.jsonl"

    monkeypatch.setattr(bridge, "_get_project_id", lambda project_name, cwd=None: "proj")
    monkeypatch.setattr(bridge, "_get_project_observations_path", lambda project_id, project_name: obs_path)

    original_open = bridge.Path.open

    def fake_open(self, *args, **kwargs):  # noqa: ANN001
        if self == obs_path:
            raise OSError("boom")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(bridge.Path, "open", fake_open, raising=False)

    warnings: list[str] = []
    monkeypatch.setattr(bridge.log, "warning", lambda msg, *args: warnings.append(msg % args if args else msg))

    count = bridge.sync_session_to_observations(db, "s1")
    assert count == 0
    assert any("書き出し失敗" in warning for warning in warnings)
    db.close()
