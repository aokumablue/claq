"""bridge.py のテスト — mem チャンク → s-learn observations.jsonl 変換"""

import json
from pathlib import Path

import pytest

from devgear.mem.bridge import _epoch_to_iso, chunk_to_observation, sync_session_to_observations
from devgear.mem.database import Database, MemoryChunk


def _make_chunk(
    session_id: str = "s1",
    project: str = "proj",
    chunk_index: int = 0,
    content: str = "test content",
    tool_names: list | None = None,
    files_read: list | None = None,
    files_modified: list | None = None,
    user_prompt: str = "do something",
    created_at_epoch: int = 1700000000,
) -> MemoryChunk:
    return MemoryChunk(
        session_id=session_id,
        project=project,
        chunk_index=chunk_index,
        content=content,
        tool_names=tool_names or ["Bash"],
        files_read=files_read or [],
        files_modified=files_modified or [],
        user_prompt=user_prompt,
        created_at_epoch=created_at_epoch,
    )


class TestChunkToObservation:
    """chunk_to_observation のテスト"""

    def test_basic_structure(self) -> None:
        chunk = _make_chunk()
        obs = chunk_to_observation(chunk)
        assert obs["event"] == "tool_complete"
        assert obs["session"] == "s1"
        assert obs["project_name"] == "proj"
        assert obs["source"] == "mem"
        assert "timestamp" in obs

    def test_tool_name_from_tool_names(self) -> None:
        chunk = _make_chunk(tool_names=["Edit", "Write"])
        obs = chunk_to_observation(chunk)
        assert obs["tool"] == "Edit"  # 最初のツール名

    def test_empty_tool_names(self) -> None:
        # tool_names=[] の場合はデフォルトリストを使わず直接 MemoryChunk を作成
        from devgear.mem.database import MemoryChunk

        chunk = MemoryChunk(
            session_id="s1",
            project="proj",
            chunk_index=0,
            content="test",
            tool_names=[],
            files_read=[],
            files_modified=[],
            user_prompt="",
            created_at_epoch=1700000000,
        )
        obs = chunk_to_observation(chunk)
        assert obs["tool"] == "unknown"

    def test_files_modified_in_output(self) -> None:
        chunk = _make_chunk(files_modified=["/src/a.py", "/src/b.py"])
        obs = chunk_to_observation(chunk)
        assert obs["output"] is not None
        assert "files_modified" in obs["output"]
        assert "/src/a.py" in obs["output"]

    def test_files_read_in_output(self) -> None:
        chunk = _make_chunk(files_read=["/src/c.py"])
        obs = chunk_to_observation(chunk)
        assert obs["output"] is not None
        assert "files_read" in obs["output"]

    def test_user_prompt_in_input(self) -> None:
        chunk = _make_chunk(user_prompt="refactor the auth module")
        obs = chunk_to_observation(chunk)
        assert obs["input"] is not None
        assert "refactor the auth module" in obs["input"]

    def test_no_user_prompt(self) -> None:
        chunk = _make_chunk(user_prompt="")
        obs = chunk_to_observation(chunk)
        assert obs["input"] is None

    def test_structured_metadata_fields(self) -> None:
        chunk = _make_chunk(
            tool_names=["Read", "Edit"],
            files_read=["/a.py"],
            files_modified=["/b.py"],
        )
        obs = chunk_to_observation(chunk)
        assert obs["tool_names"] == ["Read", "Edit"]
        assert obs["files_read"] == ["/a.py"]
        assert obs["files_modified"] == ["/b.py"]


class TestSyncSessionToObservations:
    """sync_session_to_observations のテスト"""

    def test_empty_session(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "test.db")
        count = sync_session_to_observations(db, "nonexistent-session")
        assert count == 0
        db.close()

    def test_writes_observations(self, tmp_path: Path, monkeypatch) -> None:
        db = Database(tmp_path / "test.db")

        # 既知のパスに書き出すよう monkeypatch
        obs_dir = tmp_path / ".devgear" / "projects" / "proj"
        obs_dir.mkdir(parents=True)
        obs_file = obs_dir / "observations.jsonl"

        def fake_get_project_id(name, cwd=None):
            return "proj"

        def fake_get_path(project_id, project_name):
            return obs_file

        monkeypatch.setattr("devgear.mem.bridge._get_project_id", fake_get_project_id)
        monkeypatch.setattr("devgear.mem.bridge._get_project_observations_path", fake_get_path)

        # チャンクを保存
        db.store_chunk(_make_chunk(session_id="s1", project="proj", chunk_index=0))
        db.store_chunk(_make_chunk(session_id="s1", project="proj", chunk_index=1))

        count = sync_session_to_observations(db, "s1")
        assert count == 2

        # ファイルが書かれていること
        lines = obs_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        for line in lines:
            obs = json.loads(line)
            assert obs["event"] == "tool_complete"
            assert obs["source"] == "mem"

        db.close()


class TestEpochToIso:
    """_epoch_to_iso のテスト"""

    @pytest.mark.parametrize(
        "epoch, expected_prefix",
        [
            (0, "1970-01-01T00:00:00Z"),
            (1700000000, "2023-11-"),
        ],
        ids=["epoch-zero", "epoch-recent"],
    )
    def test_iso_format(self, epoch: int, expected_prefix: str) -> None:
        result = _epoch_to_iso(epoch)
        assert result.startswith(expected_prefix) or expected_prefix in result
        assert result.endswith("Z")
