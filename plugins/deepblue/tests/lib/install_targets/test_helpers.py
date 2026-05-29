"""install_targets のヘルパーに対するテスト。"""

from __future__ import annotations

import os

import pytest

from devgear.lib.install_targets.install_target_helpers import (
    InstallTargetConfig,
    ManagedOperation,
    ValidationIssue,
    build_validation_issue,
    create_install_target_adapter,
    create_managed_operation,
    normalize_relative_path,
    resolve_base_root,
)


class TestNormalizeRelativePath:
    """normalize_relative_path のテスト。"""

    def test_handles_empty_string(self):
        """空文字列を扱えること。"""
        assert normalize_relative_path("") == ""

    def test_handles_none(self):
        """None を扱えること。"""
        assert normalize_relative_path(None) == ""

    def test_replaces_backslashes(self):
        """バックスラッシュをスラッシュに置き換えること。"""
        assert normalize_relative_path("a\\b\\c") == "a/b/c"

    def test_removes_leading_dot_slash(self):
        """先頭の ./ を削除すること。"""
        assert normalize_relative_path("./path/to/file") == "path/to/file"
        assert normalize_relative_path("././path") == "path"

    def test_removes_trailing_slashes(self):
        """末尾のスラッシュを削除すること。"""
        assert normalize_relative_path("path/to/dir/") == "path/to/dir"
        assert normalize_relative_path("path/") == "path"

    def test_preserves_normal_paths(self):
        """既に正規化済みのパスは維持すること。"""
        assert normalize_relative_path("path/to/file") == "path/to/file"


class TestResolveBaseRoot:
    """resolve_base_root のテスト。"""

    def test_home_scope_uses_home_dir(self, tmp_path):
        """home スコープでは指定した home_dir を使うこと。"""
        result = resolve_base_root("home", home_dir=str(tmp_path))
        assert result == str(tmp_path)

    def test_home_scope_falls_back_to_expanduser(self, monkeypatch):
        """home スコープでは os.path.expanduser にフォールバックすること。"""
        expected = os.path.expanduser("~")
        result = resolve_base_root("home")
        assert result == expected

    def test_project_scope_uses_project_root(self, tmp_path):
        """project スコープでは project_root を使うこと。"""
        result = resolve_base_root("project", project_root=str(tmp_path))
        assert result == str(tmp_path)

    def test_project_scope_uses_repo_root(self, tmp_path):
        """project スコープでは repo_root をフォールバックとして使うこと。"""
        result = resolve_base_root("project", repo_root=str(tmp_path))
        assert result == str(tmp_path)

    def test_project_scope_raises_without_root(self):
        """project スコープでルートがない場合はエラーになること。"""
        with pytest.raises(ValueError, match="projectRoot or repoRoot is required"):
            resolve_base_root("project")

    def test_invalid_scope_raises(self):
        """不正なスコープではエラーになること。"""
        with pytest.raises(ValueError, match="Unsupported install target scope"):
            resolve_base_root("invalid")


    def test_home_validation_branch_can_be_forced(self, monkeypatch):
        """home の検証分岐を強制できること。"""
        from devgear.lib.install_targets import install_target_helpers as helpers

        monkeypatch.setattr(helpers.os.path, "expanduser", lambda value: "")
        adapter = create_install_target_adapter(
            InstallTargetConfig(
                id="test-home",
                target="test",
                kind="home",
                root_segments=[".test"],
                install_state_path_segments=["state.json"],
            )
        )
        issues = adapter.validate()
        assert issues[0].code == "missing-home-dir"


class TestBuildValidationIssue:
    """build_validation_issue のテスト。"""

    def test_creates_issue(self):
        """検証 issue を作成できること。"""
        issue = build_validation_issue("error", "test-code", "Test message")
        assert issue.severity == "error"
        assert issue.code == "test-code"
        assert issue.message == "Test message"

    def test_includes_extra_fields(self):
        """追加フィールドを含められること。"""
        issue = build_validation_issue("warning", "code", "msg", path="/test", line=10)
        assert issue.extra["path"] == "/test"
        assert issue.extra["line"] == 10

    def test_to_dict(self):
        """辞書へ変換できること。"""
        issue = build_validation_issue("info", "code", "msg", detail="extra")
        result = issue.to_dict()
        assert result == {
            "severity": "info",
            "code": "code",
            "message": "msg",
            "detail": "extra",
        }


class TestCreateManagedOperation:
    """create_managed_operation のテスト。"""

    def test_creates_with_defaults(self):
        """デフォルト値で operation を作成できること。"""
        op = create_managed_operation()
        assert op.kind == "copy-path"
        assert op.strategy == "preserve-relative-path"
        assert op.ownership == "managed"
        assert op.scaffold_only is True

    def test_creates_with_custom_values(self):
        """カスタム値で operation を作成できること。"""
        op = create_managed_operation(
            kind="sync",
            module_id="test-module",
            source_relative_path="./path/to/file",
            destination_path="/dest/file",
            strategy="sync-root-children",
        )
        assert op.kind == "sync"
        assert op.module_id == "test-module"
        assert op.source_relative_path == "path/to/file"  # 正規化済み
        assert op.destination_path == "/dest/file"
        assert op.strategy == "sync-root-children"

    def test_normalizes_source_path(self):
        """source relative path を正規化すること。"""
        op = create_managed_operation(source_relative_path="./a\\b/c/")
        assert op.source_relative_path == "a/b/c"

    def test_to_dict(self):
        """辞書へ変換できること。"""
        op = create_managed_operation(
            module_id="mod",
            source_relative_path="src",
            source_path="/full/path",
            destination_path="/dest",
        )
        result = op.to_dict()
        assert result["moduleId"] == "mod"
        assert result["sourceRelativePath"] == "src"
        assert result["sourcePath"] == "/full/path"
        assert result["destinationPath"] == "/dest"


class TestInstallTargetAdapter:
    """InstallTargetAdapter のテスト。"""

    @pytest.fixture
    def home_config(self):
        """home スコープの設定を作成する。"""
        return InstallTargetConfig(
            id="test-home",
            target="test",
            kind="home",
            root_segments=[".test"],
            install_state_path_segments=["state.json"],
            native_root_relative_path=".test-plugin",
        )

    @pytest.fixture
    def project_config(self):
        """project スコープの設定を作成する。"""
        return InstallTargetConfig(
            id="test-project",
            target="project",
            kind="project",
            root_segments=[".test-project"],
            install_state_path_segments=["state.json"],
        )

    def test_supports_target(self, home_config):
        """設定済みターゲットをサポートすること。"""
        adapter = create_install_target_adapter(home_config)
        assert adapter.supports("test") is True
        assert adapter.supports("test-home") is True
        assert adapter.supports("other") is False

    def test_resolve_root_home(self, home_config, tmp_path):
        """home ターゲットの root を解決できること。"""
        adapter = create_install_target_adapter(home_config)
        result = adapter.resolve_root(home_dir=str(tmp_path))
        expected = str(tmp_path / ".test")
        assert result == expected

    def test_resolve_root_project(self, project_config, tmp_path):
        """project ターゲットの root を解決できること。"""
        adapter = create_install_target_adapter(project_config)
        result = adapter.resolve_root(project_root=str(tmp_path))
        expected = str(tmp_path / ".test-project")
        assert result == expected

    def test_get_install_state_path(self, home_config, tmp_path):
        """install state パスを取得できること。"""
        adapter = create_install_target_adapter(home_config)
        result = adapter.get_install_state_path(home_dir=str(tmp_path))
        expected = str(tmp_path / ".test" / "state.json")
        assert result == expected

    def test_resolve_destination_path(self, home_config, tmp_path):
        """宛先パスを解決できること。"""
        adapter = create_install_target_adapter(home_config)
        result = adapter.resolve_destination_path(
            "path/to/file.md",
            home_dir=str(tmp_path),
        )
        expected = str(tmp_path / ".test" / "path/to/file.md")
        assert result == expected

    def test_resolve_destination_path_native_root(self, home_config, tmp_path):
        """native root パスではターゲット root を返すこと。"""
        adapter = create_install_target_adapter(home_config)
        result = adapter.resolve_destination_path(
            ".test-plugin",
            home_dir=str(tmp_path),
        )
        expected = str(tmp_path / ".test")
        assert result == expected

    def test_determine_strategy_normal(self, home_config):
        """通常パスでは preserve-relative-path を返すこと。"""
        adapter = create_install_target_adapter(home_config)
        assert adapter.determine_strategy("path/to/file") == "preserve-relative-path"

    def test_determine_strategy_native_root(self, home_config):
        """native root パスでは sync-root-children を返すこと。"""
        adapter = create_install_target_adapter(home_config)
        assert adapter.determine_strategy(".test-plugin") == "sync-root-children"

    def test_create_scaffold_operation(self, home_config, tmp_path):
        """scaffold operation を作成できること。"""
        adapter = create_install_target_adapter(home_config)
        op = adapter.create_scaffold_operation(
            "mod-1",
            "agents/test.md",
            repo_root=str(tmp_path / "source"),
            home_dir=str(tmp_path / "home"),
        )
        assert op.module_id == "mod-1"
        assert op.source_relative_path == "agents/test.md"
        assert op.source_path == str(tmp_path / "source" / "agents/test.md")
        assert op.destination_path == str(tmp_path / "home" / ".test" / "agents/test.md")

    def test_plan_operations_with_modules(self, home_config, tmp_path):
        """複数モジュール向け operation を計画できること。"""
        adapter = create_install_target_adapter(home_config)
        modules = [
            {"id": "mod-1", "paths": ["a.md", "b.md"]},
            {"id": "mod-2", "paths": ["c.md"]},
        ]
        ops = adapter.plan_operations(
            modules=modules,
            home_dir=str(tmp_path),
        )
        assert len(ops) == 3
        assert ops[0].module_id == "mod-1"
        assert ops[2].module_id == "mod-2"

    def test_plan_operations_with_single_module(self, home_config, tmp_path):
        """単一モジュール向け operation を計画できること。"""
        adapter = create_install_target_adapter(home_config)
        module = {"id": "test", "paths": ["x.md", "y.md"]}
        ops = adapter.plan_operations(
            module=module,
            home_dir=str(tmp_path),
        )
        assert len(ops) == 2

    def test_validate_home_success(self, home_config, tmp_path):
        """home ターゲットで正常に検証できること。"""
        adapter = create_install_target_adapter(home_config)
        issues = adapter.validate(home_dir=str(tmp_path))
        assert issues == []

    def test_validate_project_without_root(self, project_config):
        """project で root がない場合はエラーを返すこと。"""
        adapter = create_install_target_adapter(project_config)
        issues = adapter.validate()
        assert len(issues) == 1
        assert issues[0].code == "missing-project-root"

    def test_custom_plan_operations_callback_is_used(self, tmp_path):
        """カスタム plan callback が優先されること。"""
        called = {}

        def plan_operations(**kwargs):
            called.update(kwargs)
            return [create_managed_operation(module_id="x", source_relative_path="a", destination_path="b")]

        config = InstallTargetConfig(
            id="custom",
            target="custom",
            kind="home",
            root_segments=[".custom"],
            install_state_path_segments=["state.json"],
            plan_operations=plan_operations,
        )
        adapter = create_install_target_adapter(config)
        ops = adapter.plan_operations(home_dir=str(tmp_path))
        assert ops[0].module_id == "x"
        assert called["adapter"] is adapter

    def test_custom_validate_callback_is_used(self, tmp_path):
        """カスタム validate callback が優先されること。"""
        called = {}

        def validate(**kwargs):
            called.update(kwargs)
            return [build_validation_issue("error", "custom", "custom validation")]

        config = InstallTargetConfig(
            id="custom-validate",
            target="custom-validate",
            kind="home",
            root_segments=[".custom"],
            install_state_path_segments=["state.json"],
            validate=validate,
        )
        adapter = create_install_target_adapter(config)
        issues = adapter.validate(home_dir=str(tmp_path))
        assert issues[0].code == "custom"
        assert called["adapter"] is adapter


class TestManagedOperationDataclass:
    """ManagedOperation データクラスのテスト。"""

    def test_default_values(self):
        """妥当なデフォルト値を持つこと。"""
        op = ManagedOperation()
        assert op.kind == "copy-path"
        assert op.module_id is None
        assert op.strategy == "preserve-relative-path"
        assert op.ownership == "managed"
        assert op.scaffold_only is True


class TestValidationIssueDataclass:
    """ValidationIssue データクラスのテスト。"""

    def test_basic_creation(self):
        """基本フィールドで作成できること。"""
        issue = ValidationIssue("error", "code", "message")
        assert issue.severity == "error"
        assert issue.code == "code"
        assert issue.message == "message"
        assert issue.extra == {}
