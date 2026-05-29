"""skill_evolution.versioning のテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from devgear.lib.skill_evolution import versioning as versioning


def test_ensure_skill_versioning_creates_directories(skill_env, make_skill):
    """バージョニング初期化でディレクトリとログが作成されること。"""
    skill_dir = make_skill(skill_env["skills_root"], "alpha")
    result = versioning.ensure_skill_versioning(skill_dir)

    assert Path(result["versions_dir"]).exists()
    assert Path(result["evolution_dir"]).exists()
    assert versioning.list_versions(skill_dir) == []


def test_get_current_version_defaults_to_one(skill_env, make_skill):
    """スナップショットがないスキルはバージョン 1 を返すこと。"""
    skill_dir = make_skill(skill_env["skills_root"], "beta")
    assert versioning.get_current_version(skill_dir) == 1


def test_create_version_and_list_versions(skill_env, make_skill):
    """バージョンスナップショットが作成・列挙されること。"""
    skill_dir = make_skill(skill_env["skills_root"], "gamma", "# Gamma v1\n")
    first = versioning.create_version(
        skill_dir, timestamp="2026-03-15T11:00:00.000Z", reason="bootstrap", author="observer"
    )
    (skill_dir / "SKILL.md").write_text("# Gamma v2\n", encoding="utf-8")
    second = versioning.create_version(
        skill_dir, timestamp="2026-03-16T11:00:00.000Z", reason="accepted", author="observer"
    )

    assert first["version"] == 1
    assert second["version"] == 2
    assert versioning.get_current_version(skill_dir) == 2
    assert [entry["version"] for entry in versioning.list_versions(skill_dir)] == [1, 2]
    assert len(versioning.get_evolution_log(skill_dir, "amendments")) == 2


def test_rollback_to_previous_version(skill_env, make_skill):
    """ロールバックで古いスナップショットを復元し、新しい版が作られること。"""
    skill_dir = make_skill(skill_env["skills_root"], "delta", "# Delta v1\n")
    versioning.create_version(skill_dir, timestamp="2026-03-15T11:00:00.000Z", reason="bootstrap", author="observer")
    (skill_dir / "SKILL.md").write_text("# Delta v2\n", encoding="utf-8")
    versioning.create_version(skill_dir, timestamp="2026-03-16T11:00:00.000Z", reason="accepted", author="observer")

    rollback = versioning.rollback_to(
        skill_dir, 1, timestamp="2026-03-17T11:00:00.000Z", author="maintainer", reason="restore"
    )
    assert rollback["version"] == 3
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == "# Delta v1\n"
    assert [entry["version"] for entry in versioning.list_versions(skill_dir)] == [1, 2, 3]
    amendments = versioning.get_evolution_log(skill_dir, "amendments")
    assert amendments[-1]["event"] == "rollback"
    assert amendments[-1]["target_version"] == 1


def test_read_jsonl_skips_malformed_rows(skill_env, make_skill):
    """壊れた JSONL 行は無視されること。"""
    skill_dir = make_skill(skill_env["skills_root"], "epsilon")
    versioning.ensure_skill_versioning(skill_dir)
    amendments_path = versioning.get_evolution_log_path(skill_dir, "amendments")
    amendments_path.write_text(
        '{"event":"snapshot","version":1,"reason":"ok","author":"a","status":"applied","created_at":"2026-03-15T11:00:00.000Z"}\n'
        "{bad-json}\n",
        encoding="utf-8",
    )

    entries = versioning.get_evolution_log(skill_dir, "amendments")
    assert len(entries) == 1
    assert entries[0]["version"] == 1


def test_read_jsonl_skips_empty_lines(skill_env, make_skill):
    skill_dir = make_skill(skill_env["skills_root"], "epsilon-empty")
    versioning.ensure_skill_versioning(skill_dir)
    amendments_path = versioning.get_evolution_log_path(skill_dir, "amendments")
    amendments_path.write_text(
        "\n"
        '{"event":"snapshot","version":1,"reason":"ok","author":"a","status":"applied","created_at":"2026-03-15T11:00:00.000Z"}\n',
        encoding="utf-8",
    )

    entries = versioning.get_evolution_log(skill_dir, "amendments")
    assert len(entries) == 1


def test_invalid_log_type_raises(skill_env, make_skill):
    """未知のログ種別は拒否されること。"""
    skill_dir = make_skill(skill_env["skills_root"], "zeta")
    with pytest.raises(ValueError, match="Unknown evolution log type"):
        versioning.get_evolution_log_path(skill_dir, "unknown")


def test_ensure_skill_exists_raises_for_missing_skill_file(skill_env, make_skill):
    skill_dir = make_skill(skill_env["skills_root"], "zeta-missing")
    (skill_dir / "SKILL.md").unlink()

    with pytest.raises(FileNotFoundError, match="Skill file not found"):
        versioning.ensure_skill_exists(skill_dir)


def test_parse_version_number_and_current_version_edges(skill_env, make_skill):
    """バージョン番号の解析と初期値を確認する。"""
    skill_dir = make_skill(skill_env["skills_root"], "eta")

    assert versioning.parse_version_number("v1.md") == 1
    assert versioning.parse_version_number("v01.md") == 1
    assert versioning.parse_version_number("vabc.md") is None
    assert versioning.parse_version_number("v1.txt") is None
    assert versioning.parse_version_number("not-a-version.md") is None
    assert versioning.get_current_version(skill_dir) == 1

    skill_dir.joinpath("SKILL.md").unlink()
    assert versioning.get_current_version(skill_dir) == 0


def test_list_versions_sorts_and_ignores_non_snapshot_files(skill_env, make_skill):
    """list_versions が命名規則順に並べ、不要なファイルを除外すること。"""
    skill_dir = make_skill(skill_env["skills_root"], "theta", "# Theta\n")
    versions_dir = versioning.get_versions_dir(skill_dir)
    versions_dir.mkdir(parents=True, exist_ok=True)
    (versions_dir / "v2.md").write_text("two", encoding="utf-8")
    (versions_dir / "v1.md").write_text("one", encoding="utf-8")
    (versions_dir / "notes.txt").write_text("ignore", encoding="utf-8")
    (versions_dir / "nested").mkdir()

    versions = versioning.list_versions(skill_dir)

    assert [item["version"] for item in versions] == [1, 2]
    assert all(item["path"].endswith(".md") for item in versions)


def test_create_version_and_rollback_validation_edges(skill_env, make_skill):
    """create_version/rollback_to の境界値を確認する。"""
    skill_dir = make_skill(skill_env["skills_root"], "iota", "# Iota v1\n")

    created = versioning.create_version(
        skill_dir,
        timestamp="2026-03-15T11:00:00.000Z",
        reason="bootstrap",
        author="observer",
    )
    assert created["version"] == 1
    assert (skill_dir / ".versions" / "v1.md").read_text(encoding="utf-8") == "# Iota v1\n"

    with pytest.raises(ValueError, match="Invalid target version: True"):
        versioning.rollback_to(skill_dir, True)
    with pytest.raises(ValueError, match="Invalid target version: 1.5"):
        versioning.rollback_to(skill_dir, 1.5)
    with pytest.raises(ValueError, match="Invalid target version: abc"):
        versioning.rollback_to(skill_dir, "abc")
    with pytest.raises(FileNotFoundError, match="Version not found: v99"):
        versioning.rollback_to(skill_dir, 99)
