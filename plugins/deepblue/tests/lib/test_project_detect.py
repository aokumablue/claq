"""project_detect モジュールのテスト。"""

import json
from pathlib import Path

from devgear.lib.project_detect import (
    FRAMEWORK_RULES,
    LANGUAGE_RULES,
    ProjectInfo,
    detect_frameworks,
    detect_languages,
    detect_project,
    get_build_command,
    get_test_command,
)


class TestLanguageRules:
    """LANGUAGE_RULES 設定のテスト。"""

    def test_has_common_languages(self):
        names = {r.name for r in LANGUAGE_RULES}
        assert "python" in names
        assert "javascript" in names
        assert "typescript" in names
        assert "go" in names
        assert "rust" in names

    def test_rules_have_extensions_or_files(self):
        for rule in LANGUAGE_RULES:
            assert rule.extensions or rule.files, f"{rule.name} has no detection method"


class TestFrameworkRules:
    """FRAMEWORK_RULES 設定のテスト。"""

    def test_has_common_frameworks(self):
        names = {r.name for r in FRAMEWORK_RULES}
        assert "react" in names
        assert "django" in names
        assert "flask" in names
        assert "express" in names

    def test_frameworks_have_language(self):
        for rule in FRAMEWORK_RULES:
            assert rule.language, f"{rule.name} has no language"


class TestDetectLanguages:
    """detect_languages 関数のテスト。"""

    def test_detects_javascript_by_package_json(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")

        result = detect_languages(tmp_path)
        assert "javascript" in result

    def test_detects_python_by_pyproject(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]")

        result = detect_languages(tmp_path)
        assert "python" in result

    def test_detects_python_by_requirements(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("flask")

        result = detect_languages(tmp_path)
        assert "python" in result

    def test_detects_typescript_by_tsconfig(self, tmp_path):
        (tmp_path / "tsconfig.json").write_text("{}")

        result = detect_languages(tmp_path)
        assert "typescript" in result

    def test_detects_go_by_go_mod(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/test")

        result = detect_languages(tmp_path)
        assert "go" in result

    def test_detects_rust_by_cargo_toml(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]")

        result = detect_languages(tmp_path)
        assert "rust" in result

    def test_detects_ruby_by_gemfile(self, tmp_path):
        (tmp_path / "Gemfile").write_text("source 'https://rubygems.org'")

        result = detect_languages(tmp_path)
        assert "ruby" in result

    def test_detects_by_file_extension(self, tmp_path):
        (tmp_path / "main.py").write_text("print('hello')")

        result = detect_languages(tmp_path)
        assert "python" in result

    def test_detects_multiple_languages(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "pyproject.toml").write_text("[project]")

        result = detect_languages(tmp_path)
        assert "javascript" in result
        assert "python" in result

    def test_handles_nonexistent_directory(self):
        result = detect_languages("/nonexistent/path")
        assert result == []

    def test_handles_permission_error_when_reading_root(self, tmp_path, monkeypatch):
        original_iterdir = Path.iterdir

        def fake_iterdir(self):
            if self == tmp_path:
                raise PermissionError("denied")
            return original_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", fake_iterdir, raising=False)

        result = detect_languages(tmp_path)
        assert result == []

    def test_returns_sorted_list(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("")
        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "go.mod").write_text("")

        result = detect_languages(tmp_path)
        assert result == sorted(result)


class TestDetectFrameworks:
    """detect_frameworks 関数のテスト。"""

    def test_detects_react(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"react": "^18.0.0"}}))

        result = detect_frameworks(tmp_path, ["javascript"])
        assert "react" in result

    def test_detects_nextjs(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"next": "^14.0.0"}}))

        result = detect_frameworks(tmp_path, ["javascript"])
        assert "next.js" in result

    def test_detects_django(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("Django==4.2")
        (tmp_path / "manage.py").write_text("import django")

        result = detect_frameworks(tmp_path, ["python"])
        assert "django" in result

    def test_detects_flask(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("flask")

        result = detect_frameworks(tmp_path, ["python"])
        assert "flask" in result

    def test_detects_fastapi(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("fastapi")

        result = detect_frameworks(tmp_path, ["python"])
        assert "fastapi" in result

    def test_detects_pytest(self, tmp_path):
        (tmp_path / "conftest.py").write_text("")
        (tmp_path / "pyproject.toml").write_text("[tool.pytest]")

        result = detect_frameworks(tmp_path, ["python"])
        assert "pytest" in result

    def test_detects_jest(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({"devDependencies": {"jest": "^29.0.0"}}))

        result = detect_frameworks(tmp_path, ["javascript"])
        assert "jest" in result

    def test_detects_rails(self, tmp_path):
        (tmp_path / "Gemfile").write_text("gem 'rails'")

        result = detect_frameworks(tmp_path, ["ruby"])
        assert "rails" in result

    def test_detects_gin(self, tmp_path):
        (tmp_path / "go.mod").write_text("require github.com/gin-gonic/gin")

        result = detect_frameworks(tmp_path, ["go"])
        assert "gin" in result

    def test_detects_actix(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text('[dependencies]\nactix-web = "4"')

        result = detect_frameworks(tmp_path, ["rust"])
        assert "actix" in result

    def test_skips_frameworks_without_matching_language(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"react": "^18.0.0"}}))

        # javascript ではなく python のみを検出対象にする
        result = detect_frameworks(tmp_path, ["python"])
        assert "react" not in result

    def test_handles_nonexistent_directory(self):
        result = detect_frameworks("/nonexistent/path")
        assert result == []


class TestDetectProject:
    """detect_project 関数のテスト。"""

    def test_returns_project_info(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"react": "^18.0.0"}}))

        result = detect_project(tmp_path)

        assert isinstance(result, ProjectInfo)
        assert result.root == tmp_path.resolve()
        assert "javascript" in result.languages
        assert "react" in result.frameworks

    def test_sets_primary_language(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("")
        (tmp_path / "main.py").write_text("")

        result = detect_project(tmp_path)

        assert result.primary_language == "python"

    def test_handles_empty_project(self, tmp_path):
        result = detect_project(tmp_path)

        assert result.languages == []
        assert result.frameworks == []
        assert result.primary_language is None


class TestGetTestCommand:
    """get_test_command 関数のテスト。"""

    def test_npm_test(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))

        result = get_test_command(tmp_path)
        assert result == "npm test"

    def test_pytest(self, tmp_path):
        (tmp_path / "conftest.py").write_text("")

        result = get_test_command(tmp_path)
        assert result == "pytest"

    def test_pytest_from_pyproject(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[tool.pytest]")

        result = get_test_command(tmp_path)
        assert result == "pytest"

    def test_rspec(self, tmp_path):
        (tmp_path / ".rspec").write_text("")

        result = get_test_command(tmp_path)
        assert result == "rspec"

    def test_go_test(self, tmp_path):
        (tmp_path / "go.mod").write_text("module test")

        result = get_test_command(tmp_path)
        assert result == "go test ./..."

    def test_cargo_test(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("")

        result = get_test_command(tmp_path)
        assert result == "cargo test"

    def test_mvn_test(self, tmp_path):
        (tmp_path / "pom.xml").write_text("<project></project>")

        result = get_test_command(tmp_path)
        assert result == "mvn test"

    def test_gradle_test(self, tmp_path):
        (tmp_path / "build.gradle").write_text("")

        result = get_test_command(tmp_path)
        assert result == "./gradlew test"

    def test_mix_test(self, tmp_path):
        (tmp_path / "mix.exs").write_text("")

        result = get_test_command(tmp_path)
        assert result == "mix test"

    def test_returns_none_for_unknown(self, tmp_path):
        result = get_test_command(tmp_path)
        assert result is None


class TestGetBuildCommand:
    """get_build_command 関数のテスト。"""

    def test_npm_build(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({"scripts": {"build": "webpack"}}))

        result = get_build_command(tmp_path)
        assert result == "npm run build"

    def test_go_build(self, tmp_path):
        (tmp_path / "go.mod").write_text("module test")

        result = get_build_command(tmp_path)
        assert result == "go build ./..."

    def test_cargo_build(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("")

        result = get_build_command(tmp_path)
        assert result == "cargo build"

    def test_mvn_compile(self, tmp_path):
        (tmp_path / "pom.xml").write_text("<project></project>")

        result = get_build_command(tmp_path)
        assert result == "mvn compile"

    def test_gradle_build(self, tmp_path):
        (tmp_path / "build.gradle.kts").write_text("")

        result = get_build_command(tmp_path)
        assert result == "./gradlew build"

    def test_cmake_build(self, tmp_path):
        (tmp_path / "CMakeLists.txt").write_text("")

        result = get_build_command(tmp_path)
        assert result == "cmake --build build"

    def test_make_build(self, tmp_path):
        (tmp_path / "Makefile").write_text("")

        result = get_build_command(tmp_path)
        assert result == "make"

    def test_returns_none_for_unknown(self, tmp_path):
        result = get_build_command(tmp_path)
        assert result is None


class TestIntegration:
    """プロジェクト検出の統合テスト。"""

    def test_full_javascript_project(self, tmp_path):
        # 現実的な JS プロジェクト構成を用意
        (tmp_path / "package.json").write_text(
            json.dumps(
                {
                    "name": "test-app",
                    "dependencies": {"react": "^18.0.0", "next": "^14.0.0"},
                    "devDependencies": {"jest": "^29.0.0"},
                    "scripts": {"build": "next build", "test": "jest"},
                }
            )
        )
        (tmp_path / "tsconfig.json").write_text("{}")

        src = tmp_path / "src"
        src.mkdir()
        (src / "App.tsx").write_text("export default function App() {}")

        result = detect_project(tmp_path)

        assert "javascript" in result.languages
        assert "typescript" in result.languages
        assert "react" in result.frameworks
        assert "next.js" in result.frameworks
        assert "jest" in result.frameworks
        assert get_test_command(tmp_path) == "npm test"
        assert get_build_command(tmp_path) == "npm run build"

    def test_full_python_project(self, tmp_path):
        # 現実的な Python プロジェクト構成を用意
        (tmp_path / "pyproject.toml").write_text("""
[project]
name = "test-app"
dependencies = ["fastapi", "pydantic"]

[tool.pytest.ini_options]
testpaths = ["tests"]
""")
        (tmp_path / "conftest.py").write_text("")
        (tmp_path / "main.py").write_text("from fastapi import FastAPI")

        result = detect_project(tmp_path)

        assert "python" in result.languages
        assert "fastapi" in result.frameworks
        assert "pytest" in result.frameworks
        assert "pydantic" in result.frameworks
        assert get_test_command(tmp_path) == "pytest"

    def test_monorepo_detection(self, tmp_path):
        # バックエンド（Python）
        backend = tmp_path / "backend"
        backend.mkdir()
        (backend / "requirements.txt").write_text("django")

        # フロントエンド（JS）
        frontend = tmp_path / "frontend"
        frontend.mkdir()
        (frontend / "package.json").write_text(json.dumps({"dependencies": {"react": "^18.0.0"}}))

        # ルートには両方のマーカーを配置
        (tmp_path / "pyproject.toml").write_text("")
        (tmp_path / "package.json").write_text("{}")

        result = detect_project(tmp_path)

        assert "python" in result.languages
        assert "javascript" in result.languages
