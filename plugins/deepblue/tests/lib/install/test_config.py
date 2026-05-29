"""install config モジュールのテスト。"""

import json

import pytest
from pydantic import ValidationError

from devgear.lib.install.install_config import (
    DEFAULT_INSTALL_CONFIG,
    InstallConfig,
    InstallConfigSchema,
    find_default_install_config_path,
    load_install_config,
    resolve_install_config_path,
)


def make_valid_config() -> dict:
    """最小限の有効な設定を作成する。"""
    return {
        "version": 1,
        "modules": ["core"],
    }


def test_dedupe_and_validation_edges(tmp_path):
    """重複排除と検証エラーの整形を確認する。"""
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps(
            {
                "version": 1,
                "modules": ["core", " core ", "", "hooks", "core"],
                "include": ["x", "x", " y "],
                "exclude": [],
                "options": {},
            }
        ),
        encoding="utf-8",
    )

    result = load_install_config(config_file)
    assert result.module_ids == ["core", "hooks"]
    assert result.include_component_ids == ["x", "y"]
    assert result.exclude_component_ids == []


def test_load_install_config_rejects_extra_fields_with_path_in_message(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"version": 1, "extra": True}), encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid install config"):
        load_install_config(config_file)


class TestInstallConfigSchema:
    """InstallConfigSchema Pydantic モデルのテスト。"""

    def test_valid_minimal_config(self):
        schema = InstallConfigSchema.model_validate(
            {
                "version": 1,
            }
        )
        assert schema.version == 1
        assert schema.modules == []

    def test_valid_full_config(self):
        schema = InstallConfigSchema.model_validate(
            {
                "version": 2,
                "target": "home",
                "profile": "standard",
                "modules": ["core", "hooks"],
                "include": ["comp1"],
                "exclude": ["comp2"],
                "options": {"debug": True},
            }
        )
        assert schema.version == 2
        assert schema.target == "home"
        assert schema.profile == "standard"
        assert schema.modules == ["core", "hooks"]

    def test_rejects_invalid_version(self):
        with pytest.raises(ValidationError):
            InstallConfigSchema.model_validate({"version": 0})

    def test_rejects_missing_version(self):
        with pytest.raises(ValidationError):
            InstallConfigSchema.model_validate({})

    def test_rejects_extra_fields(self):
        with pytest.raises(ValidationError):
            InstallConfigSchema.model_validate(
                {
                    "version": 1,
                    "unknown": "field",
                }
            )


class TestResolveInstallConfigPath:
    """resolve_install_config_path 関数のテスト。"""

    def test_absolute_path_unchanged(self, tmp_path):
        abs_path = tmp_path / "config.json"
        result = resolve_install_config_path(abs_path)
        assert result == abs_path

    def test_relative_path_resolved(self, tmp_path):
        result = resolve_install_config_path("config.json", cwd=tmp_path)
        assert result == tmp_path / "config.json"

    def test_default_cwd_used(self):
        result = resolve_install_config_path("config.json")
        assert result.name == "config.json"
        assert result.is_absolute()

    def test_raises_for_empty_path(self):
        with pytest.raises(ValueError) as exc_info:
            resolve_install_config_path("")
        assert "required" in str(exc_info.value)

    def test_relative_path_resolved_from_custom_cwd_string(self, tmp_path):
        result = resolve_install_config_path("nested/config.json", cwd=str(tmp_path))
        assert result == tmp_path / "nested" / "config.json"


class TestFindDefaultInstallConfigPath:
    """find_default_install_config_path 関数のテスト。"""

    def test_finds_existing_config(self, tmp_path):
        config_file = tmp_path / DEFAULT_INSTALL_CONFIG
        config_file.write_text("{}")

        result = find_default_install_config_path(cwd=tmp_path)
        assert result == config_file

    def test_returns_none_when_missing(self, tmp_path):
        result = find_default_install_config_path(cwd=tmp_path)
        assert result is None


class TestLoadInstallConfig:
    """load_install_config 関数のテスト。"""

    def test_loads_valid_config(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "target": "project",
                    "profile": "minimal",
                    "modules": ["core"],
                    "include": ["hook1"],
                    "exclude": ["agent1"],
                    "options": {"verbose": True},
                }
            )
        )

        result = load_install_config(config_file)

        assert isinstance(result, InstallConfig)
        assert result.path == config_file
        assert result.version == 1
        assert result.target == "project"
        assert result.profile_id == "minimal"
        assert result.module_ids == ["core"]
        assert result.include_component_ids == ["hook1"]
        assert result.exclude_component_ids == ["agent1"]
        assert result.options == {"verbose": True}

    def test_deduplicates_modules(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "modules": ["core", "hooks", "core", "  hooks  "],
                }
            )
        )

        result = load_install_config(config_file)
        assert result.module_ids == ["core", "hooks"]

    def test_filters_empty_strings(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "modules": ["core", "", "  ", "hooks"],
                }
            )
        )

        result = load_install_config(config_file)
        assert result.module_ids == ["core", "hooks"]

    def test_raises_for_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError) as exc_info:
            load_install_config(tmp_path / "nonexistent.json")
        assert "not found" in str(exc_info.value)

    def test_raises_for_invalid_json(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text("not valid json")

        with pytest.raises(ValueError) as exc_info:
            load_install_config(config_file)
        assert "Invalid JSON" in str(exc_info.value)

    def test_raises_for_invalid_config(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "version": 0,  # 無効: 1 以上である必要がある
                }
            )
        )

        with pytest.raises(ValueError) as exc_info:
            load_install_config(config_file)
        assert "Invalid install config" in str(exc_info.value)

    def test_handles_relative_path_with_cwd(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"version": 1}))

        result = load_install_config("config.json", cwd=tmp_path)
        assert result.path == config_file

    def test_handles_none_options(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "version": 1,
                }
            )
        )

        result = load_install_config(config_file)
        assert result.options == {}

    def test_handles_null_target_and_profile(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "target": None,
                    "profile": None,
                }
            )
        )

        result = load_install_config(config_file)
        assert result.target is None
        assert result.profile_id is None


class TestDefaultInstallConfig:
    """DEFAULT_INSTALL_CONFIG 定数のテスト。"""

    def test_default_filename(self):
        assert DEFAULT_INSTALL_CONFIG == "devgear-install.json"
