"""package_manager モジュールのテスト。"""

import json
import os
from unittest.mock import patch

import pytest

from bluecore.lib.package_manager import (
    DETECTION_PRIORITY,
    PACKAGE_MANAGERS,
    PackageManagerConfig,
    PackageManagerResult,
    detect_from_lock_file,
    detect_from_package_json,
    get_package_manager,
    get_selection_prompt,
    load_config,
    set_preferred_package_manager,
)


class TestPackageManagerConfig:
    """PackageManagerConfig のテスト。"""

    def test_npm_config(self):
        npm = PACKAGE_MANAGERS["npm"]
        assert npm.name == "npm"
        assert npm.lock_file == "package-lock.json"
        assert npm.install_cmd == "npm install"
        assert npm.run_cmd == "npm run"

    def test_pnpm_config(self):
        pnpm = PACKAGE_MANAGERS["pnpm"]
        assert pnpm.name == "pnpm"
        assert pnpm.lock_file == "pnpm-lock.yaml"
        assert pnpm.exec_cmd == "pnpm dlx"

    def test_yarn_config(self):
        yarn = PACKAGE_MANAGERS["yarn"]
        assert yarn.name == "yarn"
        assert yarn.lock_file == "yarn.lock"
        assert yarn.install_cmd == "yarn"

    def test_bun_config(self):
        bun = PACKAGE_MANAGERS["bun"]
        assert bun.name == "bun"
        assert bun.lock_file == "bun.lockb"
        assert bun.test_cmd == "bun test"


class TestDetectionPriority:
    """DETECTION_PRIORITY のテスト。"""

    def test_priority_order(self):
        assert DETECTION_PRIORITY == ["pnpm", "bun", "yarn", "npm"]


class TestDetectFromLockFile:
    """detect_from_lock_file 関数のテスト。"""

    def test_detects_npm(self, tmp_path):
        (tmp_path / "package-lock.json").write_text("{}")
        assert detect_from_lock_file(tmp_path) == "npm"

    def test_detects_pnpm(self, tmp_path):
        (tmp_path / "pnpm-lock.yaml").write_text("")
        assert detect_from_lock_file(tmp_path) == "pnpm"

    def test_detects_yarn(self, tmp_path):
        (tmp_path / "yarn.lock").write_text("")
        assert detect_from_lock_file(tmp_path) == "yarn"

    def test_detects_bun(self, tmp_path):
        (tmp_path / "bun.lockb").write_bytes(b"")
        assert detect_from_lock_file(tmp_path) == "bun"

    def test_returns_none_when_no_lock_file(self, tmp_path):
        assert detect_from_lock_file(tmp_path) is None

    def test_priority_pnpm_over_npm(self, tmp_path):
        (tmp_path / "pnpm-lock.yaml").write_text("")
        (tmp_path / "package-lock.json").write_text("{}")
        assert detect_from_lock_file(tmp_path) == "pnpm"

    def test_uses_cwd_when_no_dir_provided(self):
        # 例外が発生しないこと
        result = detect_from_lock_file()
        assert result is None or result in PACKAGE_MANAGERS


class TestDetectFromPackageJson:
    """detect_from_package_json 関数のテスト。"""

    def test_detects_pnpm(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({"packageManager": "pnpm@8.6.0"}))
        assert detect_from_package_json(tmp_path) == "pnpm"

    def test_detects_yarn(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({"packageManager": "yarn@4.0.0"}))
        assert detect_from_package_json(tmp_path) == "yarn"

    def test_detects_without_version(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({"packageManager": "bun"}))
        assert detect_from_package_json(tmp_path) == "bun"

    def test_returns_none_without_field(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({"name": "test"}))
        assert detect_from_package_json(tmp_path) is None

    def test_returns_none_for_unknown_pm(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({"packageManager": "unknown@0.0.1"}))
        assert detect_from_package_json(tmp_path) is None

    def test_returns_none_for_invalid_json(self, tmp_path):
        (tmp_path / "package.json").write_text("not json")
        assert detect_from_package_json(tmp_path) is None

    def test_returns_none_for_missing_file(self, tmp_path):
        assert detect_from_package_json(tmp_path) is None


class TestGetPackageManager:
    """get_package_manager 関数のテスト。"""

    def test_detects_from_environment(self, tmp_path):
        with patch.dict(os.environ, {"CLAUDE_PACKAGE_MANAGER": "pnpm"}):
            result = get_package_manager(project_dir=tmp_path)
            assert result.name == "pnpm"
            assert result.source == "environment"

    def test_detects_from_project_config(self, tmp_path):
        config_dir = tmp_path / ".claude"
        config_dir.mkdir()
        (config_dir / "package-manager.json").write_text(json.dumps({"packageManager": "yarn"}))

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("CLAUDE_PACKAGE_MANAGER", None)
            result = get_package_manager(project_dir=tmp_path)
            assert result.name == "yarn"
            assert result.source == "project-config"

    def test_detects_from_package_json(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({"packageManager": "bun@0.0.1"}))

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("CLAUDE_PACKAGE_MANAGER", None)
            result = get_package_manager(project_dir=tmp_path)
            assert result.name == "bun"
            assert result.source == "package.json"

    def test_detects_from_lock_file(self, tmp_path):
        (tmp_path / "pnpm-lock.yaml").write_text("")

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("CLAUDE_PACKAGE_MANAGER", None)
            result = get_package_manager(project_dir=tmp_path)
            assert result.name == "pnpm"
            assert result.source == "lock-file"

    def test_returns_none_when_no_pm_detected(self, tmp_path):
        """PM が検出できない場合（Go/Python 等のプロジェクト）は name=None を返す。"""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("CLAUDE_PACKAGE_MANAGER", None)
            result = get_package_manager(project_dir=tmp_path)
            assert result.name is None
            assert result.config is None
            assert result.source == "none"

    def test_returns_package_manager_result_with_config_when_detected(self, tmp_path):
        """PM が検出された場合、config は PackageManagerConfig を返す。"""
        (tmp_path / "package-lock.json").write_text("{}")
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("CLAUDE_PACKAGE_MANAGER", None)
            result = get_package_manager(project_dir=tmp_path)
        assert isinstance(result, PackageManagerResult)
        assert isinstance(result.config, PackageManagerConfig)

    def test_returns_none_config_when_not_detected(self, tmp_path):
        """PM が検出されない場合、config は None を返す。"""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("CLAUDE_PACKAGE_MANAGER", None)
            result = get_package_manager(project_dir=tmp_path)
        assert isinstance(result, PackageManagerResult)
        assert result.config is None


class TestSetPreferredPackageManager:
    """set_preferred_package_manager 関数のテスト。"""

    def test_sets_valid_pm(self, tmp_path):
        with patch("bluecore.lib.package_manager.get_config_path", return_value=tmp_path / "config.json"):
            result = set_preferred_package_manager("pnpm")
            assert result["packageManager"] == "pnpm"
            assert "setAt" in result

    def test_rejects_invalid_pm(self):
        with pytest.raises(ValueError) as exc_info:
            set_preferred_package_manager("invalid")
        assert "Unknown package manager" in str(exc_info.value)


class TestGetSelectionPrompt:
    """get_selection_prompt 関数のテスト。"""

    def test_returns_message(self):
        result = get_selection_prompt()
        assert "[PackageManager]" in result
        assert "Node.js" in result
        assert "npm" in result
        assert "pnpm" in result
        assert "yarn" in result
        assert "bun" in result


def test_config_loading_and_detection_edge_paths(tmp_path, monkeypatch):
    config_path = tmp_path / ".claude" / "package-manager.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("not json", encoding="utf-8")

    with patch("bluecore.lib.package_manager.get_config_path", return_value=config_path):
        assert load_config() is None

    monkeypatch.chdir(tmp_path)
    (tmp_path / "package.json").write_text(json.dumps({"packageManager": "pnpm@8.6.0"}), encoding="utf-8")
    assert detect_from_package_json() == "pnpm"

    with patch.dict(os.environ, {}, clear=True):
        result = get_package_manager()
        assert result.name == "pnpm"
        assert result.source == "package.json"

    with patch("bluecore.lib.package_manager.detect_from_package_json", return_value=None), patch(
        "bluecore.lib.package_manager.detect_from_lock_file", return_value=None
    ), patch("bluecore.lib.package_manager.load_config", return_value={"packageManager": "bun"}):
        result = get_package_manager(project_dir=tmp_path)
        assert result.name == "bun"
        assert result.source == "global-config"


def test_detect_from_project_config_no_pm_field(tmp_path) -> None:
    """project 設定に packageManager が無ければ None。"""
    import json

    from bluecore.lib.package_manager import _detect_from_project_config

    cfg = tmp_path / ".claude" / "package-manager.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"other": "x"}), encoding="utf-8")
    assert _detect_from_project_config(tmp_path) is None


def test_detect_from_global_config_no_pm_field(monkeypatch) -> None:
    """global 設定に packageManager が無ければ None。"""
    import bluecore.lib.package_manager as pm

    monkeypatch.setattr(pm, "load_config", lambda: {"other": "x"})
    assert pm._detect_from_global_config() is None
