"""resolve_formatter モジュールのテスト。"""

from __future__ import annotations

import json
import platform

import pytest

from devgear.lib.resolve_formatter import (
    BIOME_CONFIGS,
    FORMATTER_PACKAGES,
    PRETTIER_CONFIGS,
    PROJECT_ROOT_MARKERS,
    FormatterBinInfo,
    RunnerInfo,
    clear_caches,
    detect_formatter,
    find_project_root,
    get_runner_from_package_manager,
    resolve_formatter_bin,
)


@pytest.fixture(autouse=True)
def clear_all_caches():
    """各テストの前後でキャッシュをクリアする。"""
    clear_caches()
    yield
    clear_caches()


class TestFindProjectRoot:
    """find_project_root のテスト。"""

    def test_finds_package_json(self, tmp_path):
        """package.json からプロジェクトルートを見つけること。"""
        project_root = tmp_path / "project"
        project_root.mkdir()
        (project_root / "package.json").write_text("{}")
        subdir = project_root / "src" / "lib"
        subdir.mkdir(parents=True)

        result = find_project_root(subdir)
        assert result == str(project_root)

    def test_finds_biome_config(self, tmp_path):
        """biome.json からプロジェクトルートを見つけること。"""
        project_root = tmp_path / "project"
        project_root.mkdir()
        (project_root / "biome.json").write_text("{}")
        subdir = project_root / "nested"
        subdir.mkdir()

        result = find_project_root(subdir)
        assert result == str(project_root)

    def test_finds_prettier_config(self, tmp_path):
        """.prettierrc からプロジェクトルートを見つけること。"""
        project_root = tmp_path / "project"
        project_root.mkdir()
        (project_root / ".prettierrc").write_text("{}")
        subdir = project_root / "src"
        subdir.mkdir()

        result = find_project_root(subdir)
        assert result == str(project_root)

    def test_returns_start_dir_when_no_markers(self, tmp_path):
        """マーカーがない場合は開始ディレクトリを返すこと。"""
        subdir = tmp_path / "no-project"
        subdir.mkdir()

        result = find_project_root(subdir)
        assert result == str(subdir)

    def test_caches_result(self, tmp_path):
        """繰り返し参照時に結果をキャッシュすること。"""
        project_root = tmp_path / "project"
        project_root.mkdir()
        (project_root / "package.json").write_text("{}")

        # 1 回目の呼び出し
        result1 = find_project_root(project_root)
        # 2 回目の呼び出し（キャッシュヒット）
        result2 = find_project_root(project_root)

        assert result1 == result2 == str(project_root)


class TestDetectFormatter:
    """detect_formatter のテスト。"""

    def test_detects_biome(self, tmp_path):
        """biome.json がある場合は biome を検出すること。"""
        (tmp_path / "biome.json").write_text("{}")

        result = detect_formatter(tmp_path)
        assert result == "biome"

    def test_detects_biome_jsonc(self, tmp_path):
        """biome.jsonc がある場合は biome を検出すること。"""
        (tmp_path / "biome.jsonc").write_text("{}")

        result = detect_formatter(tmp_path)
        assert result == "biome"

    def test_detects_prettier_from_config_file(self, tmp_path):
        """.prettierrc がある場合は prettier を検出すること。"""
        (tmp_path / ".prettierrc").write_text("{}")

        result = detect_formatter(tmp_path)
        assert result == "prettier"

    def test_detects_prettier_from_package_json(self, tmp_path):
        """package.json に 'prettier' キーがある場合は prettier を検出すること。"""
        (tmp_path / "package.json").write_text(json.dumps({"prettier": {}}))

        result = detect_formatter(tmp_path)
        assert result == "prettier"

    def test_biome_takes_priority_over_prettier(self, tmp_path):
        """両方ある場合は prettier より biome を優先すること。"""
        (tmp_path / "biome.json").write_text("{}")
        (tmp_path / ".prettierrc").write_text("{}")

        result = detect_formatter(tmp_path)
        assert result == "biome"

    def test_returns_none_when_no_formatter(self, tmp_path):
        """フォーマッタ未検出時は None を返すこと。"""
        result = detect_formatter(tmp_path)
        assert result is None

    def test_handles_malformed_package_json(self, tmp_path):
        """壊れた package.json でも適切に処理すること。"""
        (tmp_path / "package.json").write_text("not valid json")
        (tmp_path / ".prettierrc").write_text("{}")

        result = detect_formatter(tmp_path)
        assert result == "prettier"

    def test_caches_result(self, tmp_path):
        """繰り返し参照時に結果をキャッシュすること。"""
        (tmp_path / "biome.json").write_text("{}")

        result1 = detect_formatter(tmp_path)
        result2 = detect_formatter(tmp_path)

        assert result1 == result2 == "biome"


class TestGetRunnerFromPackageManager:
    """get_runner_from_package_manager のテスト。"""

    def test_returns_runner_info(self, tmp_path, monkeypatch):
        """bin と prefix を持つ RunnerInfo を返すこと。"""
        from devgear.lib import package_manager as package_manager
        from devgear.lib.package_manager import PACKAGE_MANAGERS, PackageManagerResult

        monkeypatch.setattr(
            package_manager,
            "get_package_manager",
            lambda **kw: PackageManagerResult(name="npm", config=PACKAGE_MANAGERS["npm"], source="lock-file"),
        )

        result = get_runner_from_package_manager(tmp_path)

        assert isinstance(result, RunnerInfo)
        assert "npx" in result.bin

    def test_handles_exec_cmd_with_args(self, tmp_path, monkeypatch):
        """複数引数を含む exec_cmd を扱えること。"""
        from devgear.lib import package_manager as package_manager
        from devgear.lib.package_manager import PACKAGE_MANAGERS, PackageManagerResult

        monkeypatch.setattr(
            package_manager,
            "get_package_manager",
            lambda **kw: PackageManagerResult(name="pnpm", config=PACKAGE_MANAGERS["pnpm"], source="lock-file"),
        )

        result = get_runner_from_package_manager(tmp_path)

        assert "pnpm" in result.bin
        assert "dlx" in result.prefix


class TestResolveFormatterBin:
    """resolve_formatter_bin のテスト。"""

    def test_returns_none_for_unknown_formatter(self, tmp_path):
        """未知のフォーマッタでは None を返すこと。"""
        result = resolve_formatter_bin(tmp_path, "unknown")
        assert result is None

    def test_uses_local_bin_when_available(self, tmp_path, monkeypatch):
        """利用可能ならローカルの node_modules/.bin を使うこと。"""
        # ローカルバイナリを作成
        bin_dir = tmp_path / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True)
        is_win = platform.system() == "Windows"
        bin_name = "prettier.cmd" if is_win else "prettier"
        (bin_dir / bin_name).write_text("#!/bin/sh")

        result = resolve_formatter_bin(tmp_path, "prettier")

        assert result is not None
        assert str(bin_dir / bin_name) in result.bin
        assert result.prefix == []

    def test_falls_back_to_package_manager(self, tmp_path, monkeypatch):
        """ローカルバイナリがない場合はパッケージマネージャーへフォールバックすること。"""
        from devgear.lib import package_manager as package_manager
        from devgear.lib.package_manager import PACKAGE_MANAGERS, PackageManagerResult

        monkeypatch.setattr(
            package_manager,
            "get_package_manager",
            lambda **kw: PackageManagerResult(name="npm", config=PACKAGE_MANAGERS["npm"], source="lock-file"),
        )

        result = resolve_formatter_bin(tmp_path, "biome")

        assert result is not None
        assert "npx" in result.bin
        assert "@biomejs/biome" in result.prefix

    def test_caches_result(self, tmp_path, monkeypatch):
        """繰り返し参照時に結果をキャッシュすること。"""
        from devgear.lib import package_manager as package_manager
        from devgear.lib.package_manager import PACKAGE_MANAGERS, PackageManagerResult

        monkeypatch.setattr(
            package_manager,
            "get_package_manager",
            lambda **kw: PackageManagerResult(name="npm", config=PACKAGE_MANAGERS["npm"], source="lock-file"),
        )

        result1 = resolve_formatter_bin(tmp_path, "prettier")
        result2 = resolve_formatter_bin(tmp_path, "prettier")

        assert result1 == result2


class TestClearCaches:
    """clear_caches のテスト。"""

    def test_clears_all_caches(self, tmp_path):
        """すべてのキャッシュをクリアすること。"""
        project = tmp_path / "project"
        project.mkdir()
        (project / "biome.json").write_text("{}")

        # キャッシュを埋める
        find_project_root(project)
        detect_formatter(project)

        # クリア
        clear_caches()

        # キャッシュが空になり、関数が引き続き動作することを確認
        # マーカーのない完全に別のパスを作る
        empty_path = tmp_path / "empty"
        empty_path.mkdir()
        result = find_project_root(empty_path)
        assert result == str(empty_path)


class TestConstants:
    """モジュール定数のテスト。"""

    def test_biome_configs_contains_expected(self):
        """BIOME_CONFIGS に期待ファイルが含まれること。"""
        assert "biome.json" in BIOME_CONFIGS
        assert "biome.jsonc" in BIOME_CONFIGS

    def test_prettier_configs_contains_expected(self):
        """PRETTIER_CONFIGS に期待ファイルが含まれること。"""
        assert ".prettierrc" in PRETTIER_CONFIGS
        assert ".prettierrc.json" in PRETTIER_CONFIGS
        assert "prettier.config.js" in PRETTIER_CONFIGS

    def test_project_root_markers_includes_all(self):
        """PROJECT_ROOT_MARKERS に全設定ファイルが含まれること。"""
        assert "package.json" in PROJECT_ROOT_MARKERS
        for cfg in BIOME_CONFIGS:
            assert cfg in PROJECT_ROOT_MARKERS
        for cfg in PRETTIER_CONFIGS:
            assert cfg in PROJECT_ROOT_MARKERS

    def test_formatter_packages_structure(self):
        """FORMATTER_PACKAGES が正しい構造を持つこと。"""
        assert "biome" in FORMATTER_PACKAGES
        assert "prettier" in FORMATTER_PACKAGES
        assert FORMATTER_PACKAGES["biome"]["bin_name"] == "biome"
        assert FORMATTER_PACKAGES["prettier"]["pkg_name"] == "prettier"


class TestNamedTuples:
    """NamedTuple 型のテスト。"""

    def test_runner_info(self):
        """RunnerInfo が bin と prefix を持つこと。"""
        info = RunnerInfo(bin="npx", prefix=["dlx"])
        assert info.bin == "npx"
        assert info.prefix == ["dlx"]

    def test_formatter_bin_info(self):
        """FormatterBinInfo が bin と prefix を持つこと。"""
        info = FormatterBinInfo(bin="/path/to/bin", prefix=["--flag"])
        assert info.bin == "/path/to/bin"
        assert info.prefix == ["--flag"]
