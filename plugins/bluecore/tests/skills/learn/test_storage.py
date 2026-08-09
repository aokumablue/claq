"""bluecore.skills.learn.storage のテスト。

生ログの保存先が ``repos.id``（人間可読スラッグ）配下に落ちること、
``repos`` 台帳への登録が観測開始時点で済むこと、および観測ログと
loop-dev テレメトリが共有する ``JsonlLog`` の追記・ローテーション・
アーカイブ purge を確認する。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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


def test_loop_telemetry_log_sits_next_to_observations(data_dir: Path, tmp_path: Path) -> None:
    """テレメトリ JSONL は観測ログと同じ repo ディレクトリに置く。"""
    target = storage.observation_target("bluecore-dev", tmp_path / "src")
    log = target.loop_telemetry_log
    assert log.path == data_dir / "repos" / "bluecore-dev" / "loop-dev.jsonl"
    assert log.archive_dir == data_dir / "repos" / "bluecore-dev" / "loop-dev.archive"
    assert target.observations_log.path == target.observations_file


# --- JsonlLog ----------------------------------------------------------------


@pytest.fixture
def log(tmp_path: Path) -> storage.JsonlLog:
    """tmp 配下に置いた JsonlLog を返す（ディレクトリは未作成）。"""
    storage_dir = tmp_path / "repo"
    return storage.JsonlLog(storage_dir / "loop-dev.jsonl", storage_dir / "loop-dev.archive")


def test_name_and_purge_marker_derive_from_file_name(log: storage.JsonlLog) -> None:
    """識別名はファイル名の stem、purge マーカーはログごとに別ファイル。"""
    assert log.name == "loop-dev"
    assert log.purge_marker == log.path.parent / ".last-purge-loop-dev"


def test_ensure_dirs_creates_both(log: storage.JsonlLog) -> None:
    """本体の親とアーカイブの両方を作る。"""
    log.ensure_dirs()
    assert log.archive_dir.is_dir()
    assert log.path.parent.is_dir()


def test_append_creates_parent_and_writes_one_line(log: storage.JsonlLog) -> None:
    """親ディレクトリを作りつつ JSONL 1 行を追記する。"""
    log.append({"a": 1})
    log.append({"b": "日本語"})
    lines = log.path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [{"a": 1}, {"b": "日本語"}]
    assert "日本語" in lines[1]  # ensure_ascii=False で人間可読のまま


def test_rotate_noop_when_missing(log: storage.JsonlLog) -> None:
    """ログが無ければアーカイブディレクトリも作らない。"""
    log.rotate_if_too_large()
    assert not log.archive_dir.exists()


def test_rotate_noop_when_small(log: storage.JsonlLog) -> None:
    """閾値未満なら退避しない。"""
    log.append({"a": 1})
    log.rotate_if_too_large()
    assert log.path.exists()


def test_rotate_stat_oserror(log: storage.JsonlLog) -> None:
    """stat が OSError なら何もしない。"""
    log.append({"a": 1})
    with mock.patch.object(Path, "stat", side_effect=OSError):
        log.rotate_if_too_large()
    assert log.path.exists()


def test_rotate_moves_to_archive(log: storage.JsonlLog) -> None:
    """閾値超えでアーカイブへ退避し、名前は <name>-<stamp>-<pid>.jsonl。"""
    log.append({"a": 1})
    with mock.patch.object(Path, "stat", return_value=SimpleNamespace(st_size=storage.MAX_LOG_BYTES + 1)):
        log.rotate_if_too_large()
    assert not log.path.exists()
    assert [path.name.startswith("loop-dev-") for path in log.archive_dir.glob("loop-dev-*.jsonl")] == [True]


def test_rotate_replace_oserror(log: storage.JsonlLog) -> None:
    """replace が OSError でも握りつぶす。"""
    log.append({"a": 1})
    with (
        mock.patch.object(Path, "stat", return_value=SimpleNamespace(st_size=storage.MAX_LOG_BYTES + 1)),
        mock.patch.object(Path, "replace", side_effect=OSError),
    ):
        log.rotate_if_too_large()
    assert log.path.exists()


def test_purge_removes_old_archives_and_stamps_marker(log: storage.JsonlLog) -> None:
    """マーカー無し＝stale。cutoff より古いアーカイブだけ消してマーカーを打つ。"""
    log.ensure_dirs()
    old = log.archive_dir / "loop-dev-old.jsonl"
    old.touch()
    os.utime(old, (0, 0))  # 1970 年 → cutoff より古い
    fresh = log.archive_dir / "loop-dev-new.jsonl"
    fresh.touch()

    log.purge_old_archives()
    assert not old.exists()
    assert fresh.exists()
    assert log.purge_marker.exists()


def test_purge_skips_when_marker_is_fresh(log: storage.JsonlLog) -> None:
    """1 日以内に purge 済みなら何もしない。"""
    log.ensure_dirs()
    log.purge_marker.touch()
    old = log.archive_dir / "loop-dev-old.jsonl"
    old.touch()
    os.utime(old, (0, 0))

    log.purge_old_archives()
    assert old.exists()


def test_purge_marker_stat_oserror_treated_as_stale(log: storage.JsonlLog) -> None:
    """マーカーの stat が OSError なら stale 扱いで続行する。"""
    log.ensure_dirs()
    log.purge_marker.touch()
    with mock.patch.object(Path, "stat", side_effect=OSError):
        log.purge_old_archives()


def test_purge_glob_oserror_continues(log: storage.JsonlLog) -> None:
    """glob が OSError でも空扱いで続行し、マーカーは打つ。"""
    log.ensure_dirs()
    with mock.patch.object(Path, "glob", side_effect=OSError):
        log.purge_old_archives()
    assert log.purge_marker.exists()


def test_purge_unlink_oserror_continues(log: storage.JsonlLog) -> None:
    """古いファイルの unlink が OSError でも次へ進む。"""
    log.ensure_dirs()
    old = log.archive_dir / "loop-dev-old.jsonl"
    old.touch()
    os.utime(old, (0, 0))
    with mock.patch.object(Path, "unlink", side_effect=OSError):
        log.purge_old_archives()
    assert old.exists()


def test_purge_marker_touch_oserror(log: storage.JsonlLog) -> None:
    """マーカーの touch が OSError でも握りつぶす。"""
    log.ensure_dirs()
    with mock.patch.object(Path, "touch", side_effect=OSError):
        log.purge_old_archives()
    assert not log.purge_marker.exists()


def test_maintain_purges_then_rotates(log: storage.JsonlLog) -> None:
    """maintain は purge → rotate の順に呼ぶ。"""
    calls: list[str] = []
    with (
        mock.patch.object(storage.JsonlLog, "purge_old_archives", lambda _self: calls.append("purge")),
        mock.patch.object(storage.JsonlLog, "rotate_if_too_large", lambda _self: calls.append("rotate")),
    ):
        log.maintain()
    assert calls == ["purge", "rotate"]


def test_read_records_merges_archives_then_current(log: storage.JsonlLog) -> None:
    """アーカイブ→本体の順に、古い順で全件返す。"""
    log.ensure_dirs()
    (log.archive_dir / "loop-dev-20260101-000000-1.jsonl").write_text('{"n": 1}\n', encoding="utf-8")
    (log.archive_dir / "loop-dev-20260201-000000-1.jsonl").write_text('{"n": 2}\n', encoding="utf-8")
    log.append({"n": 3})
    assert [record["n"] for record in log.read_records()] == [1, 2, 3]


def test_read_records_skips_blank_broken_and_non_object_lines(log: storage.JsonlLog) -> None:
    """空行・壊れた JSON・非オブジェクトは捨てる。"""
    log.ensure_dirs()
    log.path.write_text('\n  \nnot json\n[1, 2]\n{"n": 1}\n', encoding="utf-8")
    assert log.read_records() == [{"n": 1}]


def test_read_records_empty_when_nothing_written(log: storage.JsonlLog) -> None:
    """ファイルが無ければ空リストを返す（読み取り時に作らない）。"""
    assert log.read_records() == []
    assert not log.path.exists()


def test_read_records_skips_unreadable_file(log: storage.JsonlLog) -> None:
    """read_text が OSError のファイルは飛ばす。"""
    log.append({"n": 1})
    with mock.patch.object(Path, "read_text", side_effect=OSError):
        assert log.read_records() == []
