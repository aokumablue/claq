"""project_detect モジュールの追加テスト。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bluecore.lib import project_detect as pd


def test_limited_file_scan_skips_ignored_entries_and_respects_depth(tmp_path: Path) -> None:
    """_limited_file_scan が無視対象を飛ばし、深さ制限も守ること。"""
    root = tmp_path / "root"
    root.mkdir()
    (root / "keep.txt").write_text("keep", encoding="utf-8")
    nested = root / "nested"
    nested.mkdir()
    (nested / "child.txt").write_text("child", encoding="utf-8")

    for ignored in [".hidden", "node_modules", "__pycache__", "venv", ".venv"]:
        ignored_dir = root / ignored
        ignored_dir.mkdir()
        (ignored_dir / "ignored.txt").write_text("ignored", encoding="utf-8")

    scanned = pd._limited_file_scan(root, max_depth=0, max_files=10)
    assert [path.name for path in scanned] == ["keep.txt"]


def test_limited_file_scan_ignores_permission_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """権限エラーのディレクトリは無視されること。"""
    root = tmp_path / "root"
    root.mkdir()
    (root / "keep.txt").write_text("keep", encoding="utf-8")
    blocked = root / "blocked"
    blocked.mkdir()
    (blocked / "ignored.txt").write_text("ignored", encoding="utf-8")

    original_iterdir = pd.Path.iterdir

    def fake_iterdir(self):  # noqa: ANN001
        if self == blocked:
            raise PermissionError("denied")
        return original_iterdir(self)

    monkeypatch.setattr(pd.Path, "iterdir", fake_iterdir, raising=False)

    scanned = pd._limited_file_scan(root, max_depth=3, max_files=10)
    assert [path.name for path in scanned] == ["keep.txt"]


def test_limited_file_scan_stops_at_max_files(tmp_path: Path) -> None:
    """_limited_file_scan が最大件数で止まること。"""
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    (root / "b.txt").write_text("b", encoding="utf-8")

    scanned = pd._limited_file_scan(root, max_depth=3, max_files=1)
    assert len(scanned) == 1


def test_read_json_and_text_helpers_cover_error_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """JSON/text 読み込みの成功・失敗パスを確認する。"""
    valid_json = tmp_path / "valid.json"
    valid_json.write_text("{\"alpha\": 1}", encoding="utf-8")
    invalid_json = tmp_path / "invalid.json"
    invalid_json.write_text("{", encoding="utf-8")
    valid_text = tmp_path / "valid.txt"
    valid_text.write_text("hello", encoding="utf-8")
    broken_json = tmp_path / "broken.json"
    broken_json.write_text("{}", encoding="utf-8")
    broken_text = tmp_path / "broken.txt"
    broken_text.write_text("hello", encoding="utf-8")

    assert pd._read_json_file(valid_json) == {"alpha": 1}
    assert pd._read_json_file(invalid_json) == {}
    assert pd._read_text_file(valid_text) == "hello"

    original_read_text = pd.Path.read_text

    def fake_read_text(self, *args, **kwargs):  # noqa: ANN001
        if self.name in {"broken.json", "broken.txt"}:
            raise OSError("boom")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(pd.Path, "read_text", fake_read_text, raising=False)

    assert pd._read_json_file(broken_json) == {}
    assert pd._read_text_file(broken_text) == ""


@pytest.mark.parametrize(
    ("helper_name", "helper"),
    [
        ("package_json", pd._check_package_json_deps),
        ("requirements", pd._check_requirements_deps),
        ("cargo_toml", pd._check_cargo_toml_deps),
        ("go_mod", pd._check_go_mod_deps),
        ("gemfile", pd._check_gemfile_deps),
        ("composer_json", pd._check_composer_json_deps),
        ("pubspec", pd._check_pubspec_deps),
        ("pom_xml", pd._check_pom_xml_deps),
        ("gradle", pd._check_gradle_deps),
        ("csproj", pd._check_csproj_deps),
    ],
)
def test_dependency_helpers_return_false_when_files_are_missing(
    tmp_path: Path,
    helper_name: str,
    helper,
) -> None:
    """依存関係ヘルパーの存在しないファイル分岐を通す。"""
    assert helper(tmp_path, ["needle"]) is False, helper_name


def test_detect_languages_covers_glob_marker_files(tmp_path: Path) -> None:
    """glob ベースの言語検出分岐を通す。"""
    (tmp_path / "MyApp.xcodeproj").mkdir()

    assert "swift" in pd.detect_languages(tmp_path)


def test_check_requirements_deps_covers_pipfile_branch(tmp_path: Path) -> None:
    (tmp_path / "Pipfile").write_text("[packages]\nrequests = '*'\n", encoding="utf-8")

    assert pd._check_requirements_deps(tmp_path, ["requests"]) is True


def test_detect_frameworks_defaults_to_detected_languages(tmp_path: Path) -> None:
    """detected_languages 未指定の分岐を通す。"""
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"react": "^18.0.0"}}), encoding="utf-8")

    assert "react" in pd.detect_frameworks(tmp_path)


@pytest.mark.parametrize(
    ("files", "languages", "expected"),
    [
        ({"composer.json": json.dumps({"require": {"laravel/framework": "^10.0"}})}, ["php"], {"laravel"}),
        ({"pubspec.yaml": "dependencies:\n  flutter: any\n"}, ["dart"], {"flutter"}),
        ({"pom.xml": "<project><artifactId>spring-boot</artifactId><artifactId>junit</artifactId></project>"}, ["java"], {"spring", "junit"}),
        ({"build.gradle": "dependencies { implementation 'org.springframework:spring-core' }"}, ["java"], {"spring"}),
        ({"app.csproj": "<Project><PackageReference Include=\"Microsoft.AspNetCore\" /></Project>"}, ["csharp"], {"aspnet"}),
        ({"app.py": "from flask import Flask\nfrom fastapi import FastAPI\n"}, ["python"], {"flask", "fastapi"}),
        ({"mix.exs": "defp deps do\n  [{:phoenix, \"~> 1.7\"}]\nend\n"}, ["elixir"], {"phoenix"}),
        ({"lib/foo_web/router.ex": "defmodule FooWeb.Router do\nend\n"}, ["elixir"], {"phoenix"}),
    ],
)
def test_detect_frameworks_covers_dependency_and_content_branches(
    tmp_path: Path,
    files: dict[str, str],
    languages: list[str],
    expected: set[str],
) -> None:
    """framework ルールの依存関係/ファイル内容/グロブ分岐を通す。"""
    for relative_path, content in files.items():
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    detected = set(pd.detect_frameworks(tmp_path, languages))
    assert expected <= detected


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"package.json": json.dumps({"scripts": {"tests": "pytest -q"}})}, "npm run tests"),
        ({"Rakefile": "task :default do\nend\n"}, "rake test"),
    ],
)
def test_get_test_command_covers_remaining_branches(
    tmp_path: Path,
    files: dict[str, str],
    expected: str,
) -> None:
    """get_test_command の残りの分岐を通す。"""
    for relative_path, content in files.items():
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    assert pd.get_test_command(tmp_path) == expected


class TestMinitestDetection:
    """Minitest フレームワーク検出のテスト。"""

    def test_detects_minitest_by_gemfile(self, tmp_path: Path) -> None:
        """Gemfile に minitest があれば frameworks に含まれること。"""
        (tmp_path / "Gemfile").write_text('gem "minitest"\n', encoding="utf-8")
        (tmp_path / "test").mkdir()
        (tmp_path / "test" / "test_helper.rb").write_text("require 'minitest'\n", encoding="utf-8")

        result = pd.detect_frameworks(tmp_path, ["ruby"])
        assert "minitest" in result

    def test_get_test_command_minitest_prefers_rake_test(self, tmp_path: Path) -> None:
        """Minitest プロジェクト（.rspec なし、test/test_helper.rb あり）は rake test を返すこと。"""
        (tmp_path / "Gemfile").write_text('gem "minitest"\n', encoding="utf-8")
        (tmp_path / "test").mkdir()
        (tmp_path / "test" / "test_helper.rb").write_text("require 'minitest'\n", encoding="utf-8")

        assert pd.get_test_command(tmp_path) == "rake test"

    def test_get_test_command_rails_with_test_helper_returns_rails_test(self, tmp_path: Path) -> None:
        """Rails プロジェクト（test/test_helper.rb + Rails マーカー）は rails test を返すこと。"""
        (tmp_path / "Gemfile").write_text("gem 'rails'\n", encoding="utf-8")
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "routes.rb").write_text("Rails.application.routes.draw do\nend\n", encoding="utf-8")
        (tmp_path / "test").mkdir()
        (tmp_path / "test" / "test_helper.rb").write_text("require 'rails/test_help'\n", encoding="utf-8")

        assert pd.get_test_command(tmp_path) == "rails test"

    def test_rspec_takes_priority_over_minitest(self, tmp_path: Path) -> None:
        """.rspec が存在する場合は rspec が優先されること。"""
        (tmp_path / ".rspec").write_text("--format progress\n", encoding="utf-8")
        (tmp_path / "test").mkdir()
        (tmp_path / "test" / "test_helper.rb").write_text("require 'minitest'\n", encoding="utf-8")

        assert pd.get_test_command(tmp_path) == "rspec"


class TestRubyRailsCompatibilityRegression:
    """Ruby/Rails 系の互換回帰テスト。"""

    def test_detects_rails_like_project_and_prefers_rspec(self, tmp_path: Path) -> None:
        root = tmp_path / "rails-app"
        root.mkdir()
        (root / "Gemfile").write_text(
            "source 'https://rubygems.org'\n"
            "gem 'rails'\n"
            "gem 'rspec-rails'\n",
            encoding="utf-8",
        )
        (root / "config").mkdir()
        (root / "config" / "routes.rb").write_text("Rails.application.routes.draw do\nend\n", encoding="utf-8")
        (root / "spec").mkdir()
        (root / "spec" / "spec_helper.rb").write_text("require 'rspec'\n", encoding="utf-8")

        project = pd.detect_project(root)

        assert project.languages == ["ruby"]
        assert set(project.frameworks) >= {"rails", "rspec"}
        assert pd.get_test_command(root) == "rspec"

    def test_unsupported_language_project_falls_back_cleanly(self, tmp_path: Path) -> None:
        (tmp_path / "script.xyz").write_text("print('hello')", encoding="utf-8")
        (tmp_path / "README.md").write_text("# docs\n", encoding="utf-8")

        project = pd.detect_project(tmp_path)

        assert project.languages == []
        assert project.frameworks == []
        assert project.primary_language is None
        assert pd.get_test_command(tmp_path) is None


def test_check_requirements_pipfile_without_dep(tmp_path) -> None:
    """Pipfile はあるが対象依存が無ければ False。"""
    from bluecore.lib.project_detect.dependency_checks import _check_requirements_deps

    (tmp_path / "Pipfile").write_text("[packages]\nfoo = '*'\n", encoding="utf-8")
    assert _check_requirements_deps(tmp_path, ["django"]) is False


def test_check_file_contents_glob_matches_directory(tmp_path) -> None:
    """glob がディレクトリにマッチしてもファイルでなければ無視する。"""
    from bluecore.lib.project_detect.dependency_checks import _check_file_contents

    (tmp_path / "sub.txt").mkdir()  # *.txt にマッチするが dir
    assert _check_file_contents(tmp_path, [{"file": "*.txt", "pattern": "x"}]) is False


def test_check_file_contents_no_pattern_match(tmp_path) -> None:
    """ファイルは存在するがパターンに一致しなければ False。"""
    from bluecore.lib.project_detect.dependency_checks import _check_file_contents

    (tmp_path / "f.txt").write_text("nomatch here", encoding="utf-8")
    assert _check_file_contents(tmp_path, [{"file": "f.txt", "pattern": "xyz"}]) is False
