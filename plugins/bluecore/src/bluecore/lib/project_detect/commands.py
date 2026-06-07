"""統合プロジェクト検出とテスト・ビルドコマンドの推定。"""

from __future__ import annotations

from pathlib import Path

from bluecore.lib.project_detect.dependency_checks import (
    _is_rails_app,
    _read_json_file,
    _read_text_file,
)
from bluecore.lib.project_detect.frameworks import detect_frameworks
from bluecore.lib.project_detect.languages import detect_languages
from bluecore.lib.project_detect.models import ProjectInfo


def detect_project(project_root: str | Path) -> ProjectInfo:
    """プロジェクト情報全体を検出する。

    Args:
        project_root: project_root の値

    Returns:
        ProjectInfo: 処理結果を返します。

    Raises:
        例外は発生しません。
    """
    root = Path(project_root).resolve()
    languages = detect_languages(root)
    frameworks = detect_frameworks(root, languages)

    # 主要言語を決定（最も多い言語、または最初に検出された言語）
    primary = languages[0] if languages else None

    return ProjectInfo(
        root=root,
        languages=languages,
        frameworks=frameworks,
        primary_language=primary,
    )


def _get_js_test_command(root: Path) -> str | None:
    """package.json の scripts からテストコマンドを推定する。"""
    package_json = root / "package.json"
    if not package_json.exists():
        return None
    scripts = _read_json_file(package_json).get("scripts")
    if isinstance(scripts, dict):
        if "test" in scripts:
            return "npm test"
        if "tests" in scripts:
            return "npm run tests"
    return None


def _get_python_test_command(root: Path) -> str | None:
    """Python プロジェクトのテストコマンドを推定する。"""
    if (root / "pytest.ini").exists() or (root / "conftest.py").exists():
        return "pytest"
    if (root / "pyproject.toml").exists() and "pytest" in _read_text_file(root / "pyproject.toml"):
        return "pytest"
    return None


def _get_ruby_test_command(root: Path) -> str | None:
    """Ruby プロジェクトのテストコマンドを推定する。"""
    if (root / ".rspec").exists() or (root / "spec").is_dir():
        return "rspec"
    if (root / "test" / "test_helper.rb").exists():
        return "rails test" if _is_rails_app(root) else "rake test"
    if (root / "Rakefile").exists():
        return "rake test"
    return None


def get_test_command(project_root: str | Path) -> str | None:
    """プロジェクトに適したテストコマンドを取得する。

    Args:
        project_root: project_root の値

    Returns:
        str | None: str を返します。見つからない場合は None です。

    Raises:
        例外は発生しません。
    """
    root = Path(project_root)

    cmd = _get_js_test_command(root)
    if cmd:
        return cmd

    cmd = _get_python_test_command(root)
    if cmd:
        return cmd

    cmd = _get_ruby_test_command(root)
    if cmd:
        return cmd

    if (root / "go.mod").exists():
        return "go test ./..."
    if (root / "Cargo.toml").exists():
        return "cargo test"
    if (root / "pom.xml").exists():
        return "mvn test"
    if (root / "build.gradle").exists() or (root / "build.gradle.kts").exists():
        return "./gradlew test"
    if (root / "mix.exs").exists():
        return "mix test"

    return None


def get_build_command(project_root: str | Path) -> str | None:
    """プロジェクトに適したビルドコマンドを取得する。

    Args:
        project_root: project_root の値

    Returns:
        str | None: str を返します。見つからない場合は None です。

    Raises:
        例外は発生しません。
    """
    root = Path(project_root)

    # package.json の scripts を確認
    package_json = root / "package.json"
    if package_json.exists():
        data = _read_json_file(package_json)
        scripts = data.get("scripts")
        if isinstance(scripts, dict) and "build" in scripts:
            return "npm run build"

    # Go
    if (root / "go.mod").exists():
        return "go build ./..."

    # Rust
    if (root / "Cargo.toml").exists():
        return "cargo build"

    # Java
    if (root / "pom.xml").exists():
        return "mvn compile"
    if (root / "build.gradle").exists() or (root / "build.gradle.kts").exists():
        return "./gradlew build"

    # C/C++（CMake）
    if (root / "CMakeLists.txt").exists():
        return "cmake --build build"

    # C/C++（Make）
    if (root / "Makefile").exists():
        return "make"

    return None
