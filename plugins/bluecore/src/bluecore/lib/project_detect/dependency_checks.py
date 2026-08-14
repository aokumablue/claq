"""ファイル読込ヘルパと依存関係マニフェストのチェック関数群。"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _read_json_file(path: Path) -> dict[str, Any]:
    """JSONファイルを読み込んで解析し、エラー時は空辞書を返す。

    Args:
        path: path の値

    Returns:
        dict[str, Any]: 処理結果を返します。

    Raises:
        例外は発生しません。
    """
    import json

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _read_text_file(path: Path) -> str:
    """テキストファイルを読み込み、エラー時は空文字列を返す。

    Args:
        path: path の値

    Returns:
        str: 処理結果を返します。

    Raises:
        例外は発生しません。
    """
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _file_contains_any(path: Path, deps: list[str], *, ignore_case: bool = False) -> bool:
    """ファイルが存在し、deps のいずれかを含むか確認する。"""
    if not path.exists():
        return False
    content = _read_text_file(path)
    if ignore_case:
        content = content.lower()
        return any(dep.lower() in content for dep in deps)
    return any(dep in content for dep in deps)


def _json_section_keys_contain_any(path: Path, section_keys: list[str], deps: list[str]) -> bool:
    """JSON の指定セクションキー集合に deps のいずれかが含まれるか確認する。"""
    if not path.exists():
        return False
    data = _read_json_file(path)
    all_deps: set[str] = set()
    for key in section_keys:
        if key in data and isinstance(data[key], dict):
            all_deps.update(data[key].keys())
    return any(dep in all_deps for dep in deps)


def _check_package_json_deps(root: Path, deps: list[str]) -> bool:
    """package.json に指定依存関係のいずれかが含まれるか確認する。

    Args:
        root: root の値
        deps: deps の値

    Returns:
        bool: 条件を満たす場合は True、そうでない場合は False。

    Raises:
        例外は発生しません。
    """
    return _json_section_keys_contain_any(
        root / "package.json",
        ["dependencies", "devDependencies", "peerDependencies"],
        deps,
    )


def _check_requirements_deps(root: Path, deps: list[str]) -> bool:
    """requirements.txt または pyproject.toml に依存関係が含まれるか確認する。

    Args:
        root: root の値
        deps: deps の値

    Returns:
        bool: 条件を満たす場合は True、そうでない場合は False。

    Raises:
        例外は発生しません。
    """
    return any(
        _file_contains_any(root / name, deps, ignore_case=True)
        for name in ("requirements.txt", "pyproject.toml", "Pipfile")
    )


def _check_cargo_toml_deps(root: Path, deps: list[str]) -> bool:
    """Cargo.toml に依存関係が含まれるか確認する。

    Args:
        root: root の値
        deps: deps の値

    Returns:
        bool: 条件を満たす場合は True、そうでない場合は False。

    Raises:
        例外は発生しません。
    """
    return _file_contains_any(root / "Cargo.toml", deps)


def _check_go_mod_deps(root: Path, deps: list[str]) -> bool:
    """go.mod に依存関係が含まれるか確認する。

    Args:
        root: root の値
        deps: deps の値

    Returns:
        bool: 条件を満たす場合は True、そうでない場合は False。

    Raises:
        例外は発生しません。
    """
    return _file_contains_any(root / "go.mod", deps)


def _check_gemfile_deps(root: Path, deps: list[str]) -> bool:
    """Gemfile に依存関係が含まれるか確認する。

    Args:
        root: root の値
        deps: deps の値

    Returns:
        bool: 条件を満たす場合は True、そうでない場合は False。

    Raises:
        例外は発生しません。
    """
    return _file_contains_any(root / "Gemfile", deps)


def _is_rails_app(root: Path) -> bool:
    """Rails アプリかどうかを判定する。

    Rails も Minitest を ``test/test_helper.rb`` で使うため、素の Minitest
    プロジェクトと区別する目的で Rails 固有マーカーの有無を確認する。
    detect_frameworks の rails ルール（Gemfile の rails / config/routes.rb /
    app/controllers/application_controller.rb）と同じシグナルを用いる。

    Args:
        root: プロジェクトルートのパス。

    Returns:
        bool: Rails マーカーが見つかれば True、そうでなければ False。

    Raises:
        例外は発生しません。
    """
    has_rails_gem = _check_gemfile_deps(root, ["rails"])
    has_routes = (root / "config" / "routes.rb").exists()
    has_app_controller = (root / "app" / "controllers" / "application_controller.rb").exists()
    return has_rails_gem or has_routes or has_app_controller


def _check_composer_json_deps(root: Path, deps: list[str]) -> bool:
    """composer.json に依存関係が含まれるか確認する。

    Args:
        root: root の値
        deps: deps の値

    Returns:
        bool: 条件を満たす場合は True、そうでない場合は False。

    Raises:
        例外は発生しません。
    """
    return _json_section_keys_contain_any(root / "composer.json", ["require", "require-dev"], deps)


def _check_pubspec_deps(root: Path, deps: list[str]) -> bool:
    """pubspec.yaml に依存関係が含まれるか確認する。

    Args:
        root: root の値
        deps: deps の値

    Returns:
        bool: 条件を満たす場合は True、そうでない場合は False。

    Raises:
        例外は発生しません。
    """
    return _file_contains_any(root / "pubspec.yaml", deps)


def _check_pom_xml_deps(root: Path, deps: list[str]) -> bool:
    """pom.xml に依存関係が含まれるか確認する。

    Args:
        root: root の値
        deps: deps の値

    Returns:
        bool: 条件を満たす場合は True、そうでない場合は False。

    Raises:
        例外は発生しません。
    """
    return _file_contains_any(root / "pom.xml", deps)


def _check_gradle_deps(root: Path, deps: list[str]) -> bool:
    """build.gradle または build.gradle.kts に依存関係が含まれるか確認する。

    Args:
        root: root の値
        deps: deps の値

    Returns:
        bool: 条件を満たす場合は True、そうでない場合は False。

    Raises:
        例外は発生しません。
    """
    return any(_file_contains_any(root / name, deps) for name in ("build.gradle", "build.gradle.kts"))


def _check_csproj_deps(root: Path, deps: list[str]) -> bool:
    """いずれかの .csproj ファイルに依存関係が含まれるか確認する。

    Args:
        root: root の値
        deps: deps の値

    Returns:
        bool: 条件を満たす場合は True、そうでない場合は False。

    Raises:
        例外は発生しません。
    """
    for csproj in root.glob("*.csproj"):
        if any(dep in _read_text_file(csproj) for dep in deps):
            return True
    return False


def _check_file_contents(root: Path, patterns: list[dict[str, str]]) -> bool:
    """ファイルに指定パターンが含まれるか確認する。

    Args:
        root: root の値
        patterns: 検索パターンの一覧

    Returns:
        bool: 条件を満たす場合は True、そうでない場合は False。

    Raises:
        例外は発生しません。
    """
    for pattern_spec in patterns:
        file_pattern = pattern_spec.get("file", "")
        search_pattern = pattern_spec.get("pattern", "")
        if "*" in file_pattern:
            if _any_glob_file_contains(root, file_pattern, search_pattern):
                return True
            continue
        file_path = root / file_pattern
        if file_path.exists() and search_pattern in _read_text_file(file_path):
            return True
    return False


def _any_glob_file_contains(root: Path, file_pattern: str, search_pattern: str) -> bool:
    """グロブにマッチする通常ファイルのいずれかが search_pattern を含むか。"""
    for file_path in root.glob(file_pattern):
        if file_path.is_file() and search_pattern in _read_text_file(file_path):
            return True
    return False
