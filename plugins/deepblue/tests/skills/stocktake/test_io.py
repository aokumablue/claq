"""devgear.skills.stocktake.io のテスト。"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from devgear.skills.stocktake import io as sio

# ─────────────────────────────────────────────
# walk_skills
# ─────────────────────────────────────────────


def test_walk_skills_returns_sorted_md_files(tmp_path: Path) -> None:
    (tmp_path / "b-skill").mkdir()
    (tmp_path / "b-skill" / "SKILL.md").write_text("---\nname: b\n---\n")
    (tmp_path / "a-skill").mkdir()
    (tmp_path / "a-skill" / "SKILL.md").write_text("---\nname: a\n---\n")

    result = sio.walk_skills(tmp_path)
    names = [p.name for p in result]
    assert names == sorted(names)
    assert all(p.suffix == ".md" for p in result)


def test_walk_skills_nonexistent_returns_empty(tmp_path: Path) -> None:
    assert sio.walk_skills(tmp_path / "no-such-dir") == []


def test_walk_skills_skips_symlinks(tmp_path: Path) -> None:
    real = tmp_path / "real.md"
    real.write_text("---\nname: real\n---\n")
    link = tmp_path / "link.md"
    link.symlink_to(real)

    result = sio.walk_skills(tmp_path)
    assert real in result
    assert link not in result


# ─────────────────────────────────────────────
# read_results
# ─────────────────────────────────────────────


def test_read_results_returns_none_when_missing(tmp_path: Path) -> None:
    assert sio.read_results(tmp_path / "results.json") is None


def test_read_results_returns_dict(tmp_path: Path) -> None:
    data = {"evaluated_at": "2026-04-26T12:00:00Z", "skills": {}}
    f = tmp_path / "results.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    assert sio.read_results(f) == data


# ─────────────────────────────────────────────
# atomic_write
# ─────────────────────────────────────────────


def test_atomic_write_creates_file(tmp_path: Path) -> None:
    results = tmp_path / "out" / "results.json"
    data = {"evaluated_at": "2026-04-26T12:00:00Z", "skills": {}}
    sio.atomic_write(results, data)
    assert results.is_file()
    assert json.loads(results.read_text()) == data


def test_atomic_write_leaves_no_temp_file(tmp_path: Path) -> None:
    results = tmp_path / "results.json"
    sio.atomic_write(results, {"key": "value"})
    tmp_files = [f for f in tmp_path.iterdir() if f.suffix == ".tmp"]
    assert tmp_files == []


def test_atomic_write_overwrites_existing(tmp_path: Path) -> None:
    results = tmp_path / "results.json"
    results.write_text(json.dumps({"old": True}))
    sio.atomic_write(results, {"new": True})
    assert json.loads(results.read_text()) == {"new": True}


def test_atomic_write_cleans_up_temp_on_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    results = tmp_path / "results.json"

    # os.replace が失敗するように monkeypatch
    def _bad_replace(_src: str, _dst: str) -> None:
        raise OSError("simulated failure")

    monkeypatch.setattr("devgear.skills.stocktake.io.os.replace", _bad_replace)

    with pytest.raises(OSError, match="simulated"):
        sio.atomic_write(results, {"key": "value"})

    # 一時ファイルが残っていないこと
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == []


def test_atomic_write_unlink_error_is_suppressed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    results = tmp_path / "results.json"
    call_log: list[str] = []

    def _bad_replace(_src: str, _dst: str) -> None:
        raise OSError("replace failed")

    def _bad_unlink(_path: str) -> None:
        call_log.append("unlink_called")
        raise OSError("already gone")

    monkeypatch.setattr("devgear.skills.stocktake.io.os.replace", _bad_replace)
    monkeypatch.setattr("devgear.skills.stocktake.io.os.unlink", _bad_unlink)

    # OSError("replace failed") が再送出され、unlink の OSError は無視される
    with pytest.raises(OSError, match="replace failed"):
        sio.atomic_write(results, {"key": "value"})

    assert call_log == ["unlink_called"]


# ─────────────────────────────────────────────
# merge_results
# ─────────────────────────────────────────────

_NOW = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)


def test_merge_results_initial_create() -> None:
    new_data = {"skills": {"foo": {"verdict": "Keep"}}, "mode": "full"}
    merged = sio.merge_results(None, new_data, _NOW)
    assert merged["evaluated_at"] == "2026-04-26T12:00:00Z"
    assert merged["skills"]["foo"]["verdict"] == "Keep"
    assert merged["mode"] == "full"


def test_merge_results_new_skill_overwrites_existing() -> None:
    existing = {
        "evaluated_at": "2026-01-01T00:00:00Z",
        "skills": {"foo": {"verdict": "Keep"}, "bar": {"verdict": "Retire"}},
    }
    new_data = {"skills": {"foo": {"verdict": "Improve"}}}
    merged = sio.merge_results(existing, new_data, _NOW)
    assert merged["skills"]["foo"]["verdict"] == "Improve"
    # 既存のスキルは保持
    assert merged["skills"]["bar"]["verdict"] == "Retire"


def test_merge_results_mode_updated_only_when_present() -> None:
    existing = {"evaluated_at": "2026-01-01T00:00:00Z", "skills": {}, "mode": "full"}
    # mode を持たない new_data
    merged = sio.merge_results(existing, {"skills": {}}, _NOW)
    assert merged["mode"] == "full"

    # mode を持つ new_data
    merged2 = sio.merge_results(existing, {"skills": {}, "mode": "quick"}, _NOW)
    assert merged2["mode"] == "quick"


def test_merge_results_batch_progress_updated_only_when_present() -> None:
    bp = {"total": 10, "evaluated": 10, "status": "completed"}
    existing = {"evaluated_at": "2026-01-01T00:00:00Z", "skills": {}, "batch_progress": bp}
    merged = sio.merge_results(existing, {"skills": {}}, _NOW)
    assert merged["batch_progress"] == bp

    new_bp = {"total": 20, "evaluated": 20, "status": "completed"}
    merged2 = sio.merge_results(existing, {"skills": {}, "batch_progress": new_bp}, _NOW)
    assert merged2["batch_progress"] == new_bp


def test_merge_results_evaluated_at_always_updated() -> None:
    existing = {"evaluated_at": "2026-01-01T00:00:00Z", "skills": {}}
    merged = sio.merge_results(existing, {"skills": {}}, _NOW)
    assert merged["evaluated_at"] == "2026-04-26T12:00:00Z"
