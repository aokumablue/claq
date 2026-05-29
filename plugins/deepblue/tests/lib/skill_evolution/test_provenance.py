"""skill_evolution.provenance のテスト。"""

from __future__ import annotations

import pytest

from devgear.lib.skill_evolution import provenance as provenance


def test_package_exports_modules():
    """パッケージが provenance モジュールを公開していること。"""
    from devgear.lib import skill_evolution

    assert skill_evolution.provenance is provenance
    assert callable(skill_evolution.collect_skill_health)
    assert callable(skill_evolution.render_dashboard)


def test_classify_skill_paths(skill_env, make_skill):
    """スキルがルートごとに分類されること。"""
    curated = make_skill(skill_env["skills_root"], "curated-alpha")
    learned = make_skill(skill_env["learned_root"], "learned-beta")
    imported = make_skill(skill_env["imported_root"], "imported-gamma")

    roots = provenance.get_skill_roots(skill_env)
    assert roots["curated"] == str(skill_env["skills_root"])
    assert roots["learned"] == str(skill_env["learned_root"])
    assert roots["imported"] == str(skill_env["imported_root"])

    assert provenance.classify_skill_path(curated, skill_env) == provenance.SKILL_TYPES["CURATED"]
    assert provenance.classify_skill_path(learned, skill_env) == provenance.SKILL_TYPES["LEARNED"]
    assert provenance.classify_skill_path(imported, skill_env) == provenance.SKILL_TYPES["IMPORTED"]
    assert provenance.requires_provenance(curated, skill_env) is False
    assert provenance.requires_provenance(learned, skill_env) is True


def test_write_and_read_provenance(skill_env, make_skill):
    """来歴メタデータが書き戻し・読み出しで一致すること。"""
    skill_dir = make_skill(skill_env["imported_root"], "imported-delta")
    record = {
        "source": "https://example.com/skills/imported-delta",
        "created_at": "2026-03-15T10:00:00.000Z",
        "confidence": 0.86,
        "author": "external-importer",
    }

    result = provenance.write_provenance(skill_dir, record, skill_env)
    assert result["path"] == str(skill_dir / ".provenance.json")
    assert provenance.read_provenance(skill_dir, skill_env) == record


@pytest.mark.parametrize(
    ("record", "message"),
    [
        ({}, "source is required"),
        ({"source": "x", "created_at": "bad", "confidence": 0.5, "author": "a"}, "created_at must be an ISO timestamp"),
        (
            {"source": "x", "created_at": "2026-03-15T10:00:00.000Z", "confidence": 2, "author": "a"},
            "confidence must be between 0 and 1",
        ),
        (
            {"source": "x", "created_at": "2026-03-15T10:00:00.000Z", "confidence": 0.5, "author": ""},
            "author is required",
        ),
    ],
)
def test_validate_provenance_rejects_invalid_record(record, message):
    """不正な来歴レコードが拒否されること。"""
    result = provenance.validate_provenance(record)
    assert result["valid"] is False
    assert message in result["errors"]


def test_read_provenance_missing_required_raises(skill_env, make_skill):
    """必須の来歴が欠けている場合は例外になること。"""
    skill_dir = make_skill(skill_env["learned_root"], "missing")
    with pytest.raises(ValueError, match="Missing provenance metadata"):
        provenance.read_provenance(skill_dir, {**skill_env, "required": True})


def test_write_provenance_rejects_curated_skills(skill_env, make_skill):
    """キュレーション済みスキルでは来歴書き込みを要求しないこと。"""
    skill_dir = make_skill(skill_env["skills_root"], "curated-epsilon")
    record = {
        "source": "https://example.com",
        "created_at": "2026-03-15T10:00:00.000Z",
        "confidence": 0.5,
        "author": "author",
    }

    with pytest.raises(ValueError, match="learned or imported skills"):
        provenance.write_provenance(skill_dir, record, skill_env)


def test_normalize_skill_dir_accepts_skill_md(skill_env, make_skill):
    """SKILL.md のパスがそのディレクトリへ正規化されること。"""
    skill_dir = make_skill(skill_env["learned_root"], "learned-zeta")
    skill_file = skill_dir / "SKILL.md"
    assert provenance.normalize_skill_dir(skill_file) == skill_dir


def test_provenance_helpers_cover_defaults_and_invalid_inputs(skill_env, make_skill):
    """来歴ヘルパーの境界条件を確認する。"""
    curated = make_skill(skill_env["skills_root"], "curated-one")
    learned = make_skill(skill_env["learned_root"], "learned-one")
    imported = make_skill(skill_env["imported_root"], "imported-one")

    assert provenance.normalize_skill_dir(curated / "SKILL.md") == curated
    with pytest.raises(ValueError, match="skillPath is required"):
        provenance.normalize_skill_dir(None)
    with pytest.raises(ValueError, match="skillPath is required"):
        provenance.normalize_skill_dir(" ")

    assert provenance.classify_skill_path(curated, skill_env) == provenance.SKILL_TYPES["CURATED"]
    assert provenance.classify_skill_path(learned, skill_env) == provenance.SKILL_TYPES["LEARNED"]
    assert provenance.classify_skill_path(imported, skill_env) == provenance.SKILL_TYPES["IMPORTED"]
    assert provenance.classify_skill_path(skill_env["repo_root"] / "outside", skill_env) == provenance.SKILL_TYPES["UNKNOWN"]

    assert provenance.requires_provenance(curated, skill_env) is False
    assert provenance.requires_provenance(learned, skill_env) is True
    assert provenance.requires_provenance(imported, skill_env) is True
    assert provenance.get_provenance_path(learned / "SKILL.md") == learned / ".provenance.json"


def test_validate_provenance_rejects_boolean_confidence():
    """confidence に bool が入った場合は拒否されること。"""
    result = provenance.validate_provenance(
        {
            "source": "https://example.com",
            "created_at": "2026-03-15T10:00:00.000Z",
            "confidence": True,
            "author": "author",
        }
    )

    assert result["valid"] is False
    assert "confidence must be a number" in result["errors"]


def test_validate_and_assert_provenance_cover_non_object_and_raise():
    """非オブジェクトと不正レコードの分岐を確認する。"""
    result = provenance.validate_provenance("not-an-object")
    assert result == {"valid": False, "errors": ["provenance record must be an object"]}

    with pytest.raises(ValueError, match="Invalid provenance metadata"):
        provenance.assert_valid_provenance({"source": "", "created_at": "bad", "confidence": 2, "author": ""})


def test_read_and_write_provenance_roundtrip_and_missing_optional(skill_env, make_skill):
    """provenance の roundtrip と任意対象の未設定を確認する。"""
    curated_dir = make_skill(skill_env["skills_root"], "curated-roundtrip")
    skill_dir = make_skill(skill_env["learned_root"], "learned-roundtrip")
    record = {
        "source": "https://example.com/roundtrip",
        "created_at": "2026-03-15T10:00:00.000Z",
        "confidence": 0.75,
        "author": "importer",
    }

    assert provenance.read_provenance(curated_dir, skill_env) is None
    result = provenance.write_provenance(skill_dir, record, skill_env)
    assert result["record"] == record
    assert (skill_dir / ".provenance.json").read_text(encoding="utf-8").endswith("\n")
    assert provenance.read_provenance(skill_dir, skill_env) == record
