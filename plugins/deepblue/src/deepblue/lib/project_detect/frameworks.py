"""プロジェクトの使用フレームワーク検出。"""

from __future__ import annotations

from pathlib import Path

from deepblue.lib.project_detect.dependency_checks import (
    _check_cargo_toml_deps,
    _check_composer_json_deps,
    _check_csproj_deps,
    _check_file_contents,
    _check_gemfile_deps,
    _check_go_mod_deps,
    _check_gradle_deps,
    _check_package_json_deps,
    _check_pom_xml_deps,
    _check_pubspec_deps,
    _check_requirements_deps,
)
from deepblue.lib.project_detect.languages import detect_languages
from deepblue.lib.project_detect.rules import FRAMEWORK_RULES


def _check_marker_files(root: Path, rule: object, detected: set[str]) -> None:
    """ルールのマーカーファイルをチェックし、検出されたらフレームワーク名を追加する。"""
    for marker_file in rule.files:  # type: ignore[attr-defined]
        if "*" in marker_file:
            if any(root.glob(marker_file)):
                detected.add(rule.name)  # type: ignore[attr-defined]
                break
        elif (root / marker_file).exists():
            detected.add(rule.name)  # type: ignore[attr-defined]
            break


def _check_dependency_files(root: Path, rule: object, detected: set[str]) -> None:
    """各言語の依存ファイルをチェックし、フレームワーク名を detected に追加する。"""
    checks = [
        (rule.package_json, _check_package_json_deps),  # type: ignore[attr-defined]
        (rule.requirements, _check_requirements_deps),  # type: ignore[attr-defined]
        (rule.cargo_toml, _check_cargo_toml_deps),  # type: ignore[attr-defined]
        (rule.go_mod, _check_go_mod_deps),  # type: ignore[attr-defined]
        (rule.gemfile, _check_gemfile_deps),  # type: ignore[attr-defined]
        (rule.composer_json, _check_composer_json_deps),  # type: ignore[attr-defined]
        (rule.pubspec, _check_pubspec_deps),  # type: ignore[attr-defined]
        (rule.pom_xml, _check_pom_xml_deps),  # type: ignore[attr-defined]
        (rule.gradle, _check_gradle_deps),  # type: ignore[attr-defined]
        (rule.csproj, _check_csproj_deps),  # type: ignore[attr-defined]
    ]
    for dep_spec, check_fn in checks:
        if dep_spec and check_fn(root, dep_spec):
            detected.add(rule.name)  # type: ignore[attr-defined]
            return


def detect_frameworks(
    project_root: str | Path,
    detected_languages: list[str] | None = None,
) -> list[str]:
    """プロジェクトで使われているフレームワークを検出する。

    Args:
        project_root: project_root の値
        detected_languages: detected_languages の値

    Returns:
        list[str]: str の一覧を返します。

    Raises:
        例外は発生しません。
    """
    root = Path(project_root)
    if not root.exists():
        return []

    if detected_languages is None:
        detected_languages = detect_languages(project_root)

    detected: set[str] = set()

    for rule in FRAMEWORK_RULES:
        if rule.language not in detected_languages:
            continue

        _check_marker_files(root, rule, detected)
        if rule.name in detected:
            continue

        _check_dependency_files(root, rule, detected)
        if rule.name in detected:
            continue

        if rule.file_contents and _check_file_contents(root, rule.file_contents):
            detected.add(rule.name)

    return sorted(detected)
