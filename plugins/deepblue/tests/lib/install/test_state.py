"""install state モジュールのテスト。"""

import json

import pytest
from pydantic import ValidationError

from devgear.lib.install.install_state import (
    InstallOperation,
    InstallRequest,
    InstallSource,
    InstallState,
    InstallTarget,
    ValidationResult,
    _clone_json_value,
    assert_valid_install_state,
    create_install_state,
    format_validation_errors,
    read_install_state,
    validate_install_state,
    write_install_state,
)


def make_valid_state() -> dict:
    """最小限の有効な install state を作成する。"""
    return {
        "schemaVersion": "devgear.install.v1",
        "installedAt": "2024-01-01T00:00:00Z",
        "target": {
            "id": "test-id",
            "root": "/path/to/root",
            "installStatePath": "/path/to/state.json",
        },
        "request": {
            "profile": "standard",
            "modules": ["core"],
            "includeComponents": [],
            "excludeComponents": [],
            "legacyLanguages": [],
            "legacyMode": False,
        },
        "resolution": {
            "selectedModules": ["core"],
            "skippedModules": [],
        },
        "source": {
            "repoVersion": "0.0.1",
            "repoCommit": "abc123",
            "manifestVersion": 1,
        },
        "operations": [],
    }


def test_create_read_write_roundtrip_with_optional_fields(tmp_path):
    """install-state の roundtrip と任意フィールドを確認する。"""
    state_file = tmp_path / "nested" / "state.json"
    state = create_install_state(
        adapter={"id": "adapter", "target": "custom", "kind": "home"},
        target_root="/root",
        install_state_path=str(state_file),
        request={
            "profile": "standard",
            "modules": ["core"],
            "includeComponents": ["a"],
            "excludeComponents": ["b"],
            "legacyLanguages": ["python"],
            "legacyMode": True,
        },
        resolution={"selectedModules": ["core"], "skippedModules": ["skip"]},
        source={"repoVersion": "1.2.3", "repoCommit": "deadbeef", "manifestVersion": 2},
        operations=[{"kind": "copy", "moduleId": "core", "sourceRelativePath": "src", "destinationPath": "dst", "strategy": "preserve-relative-path", "ownership": "managed", "scaffoldOnly": True}],
        installed_at="2024-01-01T00:00:00Z",
        last_validated_at="2024-01-02T00:00:00Z",
    )

    assert state["target"]["target"] == "custom"
    assert state["target"]["kind"] == "home"
    assert state["lastValidatedAt"] == "2024-01-02T00:00:00Z"
    assert write_install_state(state_file, state) == state
    assert read_install_state(state_file) == state


def test_validate_install_state_catches_extra_and_wrong_nested_types():
    """無効な state の分岐を確認する。"""
    state = make_valid_state()
    state["operations"] = [{"kind": "copy", "moduleId": "", "sourceRelativePath": "src", "destinationPath": "dst", "strategy": "s", "ownership": "o", "scaffoldOnly": True}]
    result = validate_install_state(state)
    assert result.valid is False
    assert any("moduleId" in error["instancePath"] for error in result.errors)

    state = make_valid_state()
    state["unexpected"] = True
    result = validate_install_state(state)
    assert result.valid is False


class TestInstallTargetModel:
    """InstallTarget Pydantic モデルのテスト。"""

    def test_valid_target(self):
        target = InstallTarget(
            id="test",
            root="/root",
            install_state_path="/state.json",
        )
        assert target.id == "test"
        assert target.root == "/root"
        assert target.installStatePath == "/state.json"

    def test_optional_fields(self):
        target = InstallTarget(
            id="test",
            target="custom",
            kind="home",
            root="/root",
            install_state_path="/state.json",
        )
        assert target.target == "custom"
        assert target.kind == "home"

    def test_rejects_empty_id(self):
        with pytest.raises(ValidationError):
            InstallTarget(
                id="",
                root="/root",
                install_state_path="/state.json",
            )


class TestInstallRequestModel:
    """InstallRequest Pydantic モデルのテスト。"""

    def test_valid_request(self):
        request = InstallRequest(
            profile="standard",
            modules=["core", "hooks"],
            include_components=["comp1"],
            exclude_components=[],
            legacy_languages=["python"],
            legacy_mode=False,
        )
        assert request.profile == "standard"
        assert request.modules == ["core", "hooks"]
        assert request.legacyMode is False

    def test_defaults(self):
        request = InstallRequest()
        assert request.profile is None
        assert request.modules == []
        assert request.legacyMode is False

    def test_rejects_empty_string_in_modules(self):
        with pytest.raises(ValidationError):
            InstallRequest(modules=["valid", ""])


class TestInstallSourceModel:
    """InstallSource Pydantic モデルのテスト。"""

    def test_valid_source(self):
        source = InstallSource(
            repo_version="0.0.1",
            repo_commit="abc123",
            manifest_version=2,
        )
        assert source.repoVersion == "0.0.1"
        assert source.manifestVersion == 2

    def test_rejects_zero_manifest_version(self):
        with pytest.raises(ValidationError):
            InstallSource(manifest_version=0)

    def test_rejects_negative_manifest_version(self):
        with pytest.raises(ValidationError):
            InstallSource(manifest_version=-1)


class TestInstallResolutionModel:
    """InstallResolution Pydantic モデルのテスト。"""

    def test_rejects_empty_string_in_selected_modules(self):
        with pytest.raises(ValidationError):
            from devgear.lib.install.install_state import InstallResolution

            InstallResolution(selected_modules=["valid", ""])


class TestInstallOperationModel:
    """InstallOperation Pydantic モデルのテスト。"""

    def test_valid_operation(self):
        op = InstallOperation(
            kind="copy",
            module_id="core",
            source_relative_path="src/file.txt",
            destination_path="/dest/file.txt",
            strategy="overwrite",
            ownership="user",
            scaffold_only=False,
        )
        assert op.kind == "copy"
        assert op.moduleId == "core"
        assert op.scaffoldOnly is False

    def test_rejects_empty_kind(self):
        with pytest.raises(ValidationError):
            InstallOperation(
                kind="",
                module_id="core",
                source_relative_path="src/file.txt",
                destination_path="/dest/file.txt",
                strategy="overwrite",
                ownership="user",
                scaffold_only=False,
            )


class TestInstallStateModel:
    """InstallState Pydantic モデルのテスト。"""

    def test_valid_state(self):
        state_dict = make_valid_state()
        state = InstallState.model_validate(state_dict)
        assert state.schemaVersion == "devgear.install.v1"
        assert state.target.id == "test-id"

    def test_rejects_wrong_schema_version(self):
        state_dict = make_valid_state()
        state_dict["schemaVersion"] = "wrong.version"
        with pytest.raises(ValidationError):
            InstallState.model_validate(state_dict)

    def test_optional_last_validated_at(self):
        state_dict = make_valid_state()
        state_dict["lastValidatedAt"] = "2024-01-02T00:00:00Z"
        state = InstallState.model_validate(state_dict)
        assert state.lastValidatedAt == "2024-01-02T00:00:00Z"


class TestValidateInstallState:
    """validate_install_state 関数のテスト。"""

    def test_valid_state_returns_true(self):
        result = validate_install_state(make_valid_state())
        assert result.valid is True
        assert result.errors == []

    def test_invalid_state_returns_errors(self):
        state = make_valid_state()
        del state["target"]
        result = validate_install_state(state)
        assert result.valid is False
        assert len(result.errors) > 0

    def test_missing_required_field(self):
        state = make_valid_state()
        del state["schemaVersion"]
        result = validate_install_state(state)
        assert result.valid is False

    def test_wrong_type(self):
        state = make_valid_state()
        state["installedAt"] = 12345  # 文字列であるべき
        result = validate_install_state(state)
        assert result.valid is False


class TestAssertValidInstallState:
    """assert_valid_install_state 関数のテスト。"""

    def test_does_not_raise_for_valid(self):
        assert_valid_install_state(make_valid_state())  # 例外が発生しないこと

    def test_raises_for_invalid(self):
        state = make_valid_state()
        del state["target"]
        with pytest.raises(ValueError) as exc_info:
            assert_valid_install_state(state)
        assert "Invalid install-state" in str(exc_info.value)

    def test_includes_label_in_error(self):
        state = make_valid_state()
        del state["target"]
        with pytest.raises(ValueError) as exc_info:
            assert_valid_install_state(state, "test-file.json")
        assert "test-file.json" in str(exc_info.value)


class TestFormatValidationErrors:
    """format_validation_errors 関数のテスト。"""

    def test_formats_errors(self):
        errors = [
            {"instancePath": "/target", "message": "required"},
            {"instancePath": "/source/manifestVersion", "message": "must be >= 1"},
        ]
        result = format_validation_errors(errors)
        assert "/target required" in result
        assert "/source/manifestVersion must be >= 1" in result

    def test_handles_empty_errors(self):
        result = format_validation_errors([])
        assert result == ""


class TestCreateInstallState:
    """create_install_state 関数のテスト。"""

    def test_creates_valid_state(self):
        state = create_install_state(
            adapter={"id": "test-adapter"},
            target_root="/root",
            install_state_path="/state.json",
            request={
                "profile": "standard",
                "modules": ["core"],
            },
            resolution={
                "selectedModules": ["core"],
            },
            source={
                "manifestVersion": 1,
            },
        )
        assert state["schemaVersion"] == "devgear.install.v1"
        assert state["target"]["id"] == "test-adapter"
        assert state["request"]["profile"] == "standard"

    def test_uses_provided_installed_at(self):
        state = create_install_state(
            adapter={"id": "test"},
            target_root="/root",
            install_state_path="/state.json",
            request={"modules": []},
            resolution={"selectedModules": []},
            source={"manifestVersion": 1},
            installed_at="2024-06-15T12:00:00Z",
        )
        assert state["installedAt"] == "2024-06-15T12:00:00Z"

    def test_generates_installed_at_if_not_provided(self):
        state = create_install_state(
            adapter={"id": "test"},
            target_root="/root",
            install_state_path="/state.json",
            request={"modules": []},
            resolution={"selectedModules": []},
            source={"manifestVersion": 1},
        )
        assert "installedAt" in state
        assert len(state["installedAt"]) > 0

    def test_includes_operations(self):
        ops = [
            {
                "kind": "copy",
                "moduleId": "core",
                "sourceRelativePath": "src/file.txt",
                "destinationPath": "/dest/file.txt",
                "strategy": "overwrite",
                "ownership": "user",
                "scaffoldOnly": False,
            }
        ]
        state = create_install_state(
            adapter={"id": "test"},
            target_root="/root",
            install_state_path="/state.json",
            request={"modules": ["core"]},
            resolution={"selectedModules": ["core"]},
            source={"manifestVersion": 1},
            operations=ops,
        )
        assert len(state["operations"]) == 1
        assert state["operations"][0]["kind"] == "copy"

    def test_includes_optional_target_fields(self):
        state = create_install_state(
            adapter={"id": "test", "target": "custom", "kind": "project"},
            target_root="/root",
            install_state_path="/state.json",
            request={"modules": []},
            resolution={"selectedModules": []},
            source={"manifestVersion": 1},
        )
        assert state["target"]["target"] == "custom"
        assert state["target"]["kind"] == "project"

    def test_clone_json_value_handles_none_and_deep_copy(self):
        value = {"nested": [1, 2, 3]}
        cloned = _clone_json_value(value)
        assert _clone_json_value(None) is None
        assert cloned == value
        assert cloned is not value


class TestReadInstallState:
    """read_install_state 関数のテスト。"""

    def test_reads_valid_file(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps(make_valid_state()))

        result = read_install_state(state_file)
        assert result["schemaVersion"] == "devgear.install.v1"

    def test_raises_for_invalid_json(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text("not json")

        with pytest.raises(ValueError) as exc_info:
            read_install_state(state_file)
        assert "Failed to read" in str(exc_info.value)

    def test_raises_for_invalid_state(self, tmp_path):
        state_file = tmp_path / "state.json"
        invalid_state = {"invalid": "state"}
        state_file.write_text(json.dumps(invalid_state))

        with pytest.raises(ValueError) as exc_info:
            read_install_state(state_file)
        assert "Invalid install-state" in str(exc_info.value)

    def test_raises_for_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            read_install_state(tmp_path / "nonexistent.json")

    def test_raises_for_invalid_labelled_state(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"schemaVersion": "devgear.install.v1"}), encoding="utf-8")

        with pytest.raises(ValueError) as exc_info:
            read_install_state(state_file)
        assert str(state_file) in str(exc_info.value)


class TestWriteInstallState:
    """write_install_state 関数のテスト。"""

    def test_writes_valid_state(self, tmp_path):
        state_file = tmp_path / "state.json"
        state = make_valid_state()

        result = write_install_state(state_file, state)

        assert state_file.exists()
        assert result == state

        content = state_file.read_text()
        assert json.loads(content) == state

    def test_creates_parent_directories(self, tmp_path):
        state_file = tmp_path / "nested" / "dir" / "state.json"
        state = make_valid_state()

        write_install_state(state_file, state)

        assert state_file.exists()

    def test_raises_for_invalid_state(self, tmp_path):
        state_file = tmp_path / "state.json"
        invalid_state = {"invalid": "state"}

        with pytest.raises(ValueError):
            write_install_state(state_file, invalid_state)

        # ファイルは作成されないこと
        assert not state_file.exists()

    def test_file_ends_with_newline(self, tmp_path):
        state_file = tmp_path / "state.json"
        state = make_valid_state()

        write_install_state(state_file, state)

        content = state_file.read_text()
        assert content.endswith("\n")


class TestValidationResult:
    """ValidationResult クラスのテスト。"""

    def test_valid_result(self):
        result = ValidationResult(valid=True)
        assert result.valid is True
        assert result.errors == []

    def test_invalid_result(self):
        errors = [{"instancePath": "/test", "message": "error"}]
        result = ValidationResult(valid=False, errors=errors)
        assert result.valid is False
        assert result.errors == errors
