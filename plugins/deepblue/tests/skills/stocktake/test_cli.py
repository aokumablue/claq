"""devgear.skills.stocktake.cli のテスト。"""

import json
import runpy
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from devgear.skills.stocktake import cli as _mod

# ─────────────────────────────────────────────
# ヘルパー
# ─────────────────────────────────────────────


def _make_skill_dir(root: Path, name: str) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Test.\n---\n")
    return d


# ─────────────────────────────────────────────
# main: サブコマンドなし
# ─────────────────────────────────────────────


def test_main_no_subcommand_returns_1(capsys: pytest.CaptureFixture) -> None:
    rc = _mod.main([])
    assert rc == 1
    out = capsys.readouterr().out
    assert "usage" in out.lower() or "{scan,diff,save}" in out


# ─────────────────────────────────────────────
# scan
# ─────────────────────────────────────────────


def test_cmd_scan_outputs_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    global_dir = tmp_path / "global"
    _make_skill_dir(global_dir, "alpha")
    _make_skill_dir(global_dir, "beta")

    monkeypatch.setenv("SKILL_STOCKTAKE_GLOBAL_DIR", str(global_dir))
    monkeypatch.setenv("SKILL_STOCKTAKE_PROJECT_DIR", str(tmp_path / "no-project"))
    monkeypatch.setenv("SKILL_STOCKTAKE_OBSERVATIONS", str(tmp_path / "obs.jsonl"))

    rc = _mod.main(["scan"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["scan_summary"]["global"]["count"] == 2
    assert len(data["skills"]) == 2


def test_cmd_scan_project_dir_arg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    project_dir = tmp_path / "proj"
    _make_skill_dir(project_dir, "proj-skill")

    monkeypatch.setenv("SKILL_STOCKTAKE_GLOBAL_DIR", str(global_dir))
    monkeypatch.setenv("SKILL_STOCKTAKE_OBSERVATIONS", str(tmp_path / "obs.jsonl"))
    monkeypatch.delenv("SKILL_STOCKTAKE_PROJECT_DIR", raising=False)

    rc = _mod.main(["scan", "--project-dir", str(project_dir)])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["scan_summary"]["project"]["count"] == 1


# ─────────────────────────────────────────────
# diff
# ─────────────────────────────────────────────


def test_cmd_diff_missing_results_returns_1(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    rc = _mod.main(["diff", str(tmp_path / "missing.json")])
    assert rc == 1
    assert "Error" in capsys.readouterr().err


def test_cmd_diff_invalid_evaluated_at_returns_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    results = tmp_path / "results.json"
    results.write_text(json.dumps({"evaluated_at": "not-a-date", "skills": {}}))
    monkeypatch.setenv("SKILL_STOCKTAKE_GLOBAL_DIR", str(tmp_path / "no-global"))
    monkeypatch.setenv("SKILL_STOCKTAKE_PROJECT_DIR", str(tmp_path / "no-project"))

    rc = _mod.main(["diff", str(results)])
    assert rc == 1
    assert "Error" in capsys.readouterr().err


def test_cmd_diff_no_changes_outputs_empty_array(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    global_dir = tmp_path / "global"
    _make_skill_dir(global_dir, "stable")
    monkeypatch.setenv("SKILL_STOCKTAKE_GLOBAL_DIR", str(global_dir))
    monkeypatch.setenv("SKILL_STOCKTAKE_PROJECT_DIR", str(tmp_path / "no-project"))

    # classify_changed の home を tmp_path / "global" にするために monkeypatch
    import devgear.skills.stocktake.core as core_mod
    original_classify = core_mod.classify_changed

    def _patched_classify(known_paths, evaluated_at, skill_files, home=None):
        return original_classify(known_paths, evaluated_at, skill_files, home=global_dir)

    monkeypatch.setattr("devgear.skills.stocktake.cli.core.classify_changed", _patched_classify)

    # stable/SKILL.md の known_path は global_dir を home とした ~/... 表現
    stable_path = "~/stable/SKILL.md"
    results = tmp_path / "results.json"
    results.write_text(
        json.dumps({
            "evaluated_at": "2099-01-01T00:00:00Z",
            "skills": {stable_path: {"verdict": "Keep"}},
        })
    )
    rc = _mod.main(["diff", str(results)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out == []


def test_cmd_diff_detects_new_skill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    global_dir = tmp_path / "global"
    _make_skill_dir(global_dir, "new-skill")
    monkeypatch.setenv("SKILL_STOCKTAKE_GLOBAL_DIR", str(global_dir))
    monkeypatch.setenv("SKILL_STOCKTAKE_PROJECT_DIR", str(tmp_path / "no-project"))

    results = tmp_path / "results.json"
    results.write_text(
        json.dumps({"evaluated_at": "2099-01-01T00:00:00Z", "skills": {}})
    )
    rc = _mod.main(["diff", str(results)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out) == 1
    assert out[0]["is_new"] is True


# ─────────────────────────────────────────────
# save
# ─────────────────────────────────────────────


def test_cmd_save_invalid_json_returns_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    results = tmp_path / "results.json"
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("NOT JSON"))
    rc = _mod.main(["save", str(results)])
    assert rc == 1
    assert "Error" in capsys.readouterr().err


def test_cmd_save_non_object_json_returns_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    results = tmp_path / "results.json"
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("[1, 2, 3]"))
    rc = _mod.main(["save", str(results)])
    assert rc == 1


def test_cmd_save_creates_results_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    results = tmp_path / "results.json"
    payload = {"skills": {"foo": {"verdict": "Keep"}}, "mode": "full"}
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(payload)))

    fixed_now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    with patch("devgear.skills.stocktake.cli.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.strptime.side_effect = datetime.strptime
        mock_dt.fromtimestamp.side_effect = datetime.fromtimestamp
        rc = _mod.main(["save", str(results)])

    assert rc == 0
    assert results.is_file()
    data = json.loads(results.read_text())
    assert data["evaluated_at"] == "2026-04-26T12:00:00Z"
    assert data["skills"]["foo"]["verdict"] == "Keep"


def test_cmd_save_merges_into_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    results = tmp_path / "results.json"
    results.write_text(
        json.dumps({
            "evaluated_at": "2026-01-01T00:00:00Z",
            "skills": {"bar": {"verdict": "Retire"}},
        })
    )
    payload = {"skills": {"foo": {"verdict": "Keep"}}}
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(payload)))

    rc = _mod.main(["save", str(results)])
    assert rc == 0
    data = json.loads(results.read_text())
    assert data["skills"]["foo"]["verdict"] == "Keep"
    assert data["skills"]["bar"]["verdict"] == "Retire"


# ─────────────────────────────────────────────
# __main__ エントリポイント
# ─────────────────────────────────────────────


def test_entrypoint_exits_nonzero_on_no_args(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["cli.py"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("devgear.skills.stocktake.cli", run_name="__main__")
    assert exc.value.code != 0
