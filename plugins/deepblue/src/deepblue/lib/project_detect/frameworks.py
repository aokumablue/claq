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
        # フレームワークの言語が未検出ならスキップ
        if rule.language not in detected_languages:
            continue

        # マーカーファイルを確認
        for marker_file in rule.files:
            if "*" in marker_file:
                if any(root.glob(marker_file)):
                    detected.add(rule.name)
                    break
            elif (root / marker_file).exists():
                detected.add(rule.name)
                break

        if rule.name in detected:
            continue

        # 言語に応じた依存ファイルを確認
        if rule.package_json and _check_package_json_deps(root, rule.package_json):
            detected.add(rule.name)
        elif rule.requirements and _check_requirements_deps(root, rule.requirements):
            detected.add(rule.name)
        elif rule.cargo_toml and _check_cargo_toml_deps(root, rule.cargo_toml):
            detected.add(rule.name)
        elif rule.go_mod and _check_go_mod_deps(root, rule.go_mod):
            detected.add(rule.name)
        elif rule.gemfile and _check_gemfile_deps(root, rule.gemfile):
            detected.add(rule.name)
        elif rule.composer_json and _check_composer_json_deps(root, rule.composer_json):
            detected.add(rule.name)
        elif rule.pubspec and _check_pubspec_deps(root, rule.pubspec):
            detected.add(rule.name)
        elif rule.pom_xml and _check_pom_xml_deps(root, rule.pom_xml):
            detected.add(rule.name)
        elif rule.gradle and _check_gradle_deps(root, rule.gradle):
            detected.add(rule.name)
        elif rule.csproj and _check_csproj_deps(root, rule.csproj):
            detected.add(rule.name)

        if rule.name in detected:
            continue

        # ファイル内容を確認
        if rule.file_contents and _check_file_contents(root, rule.file_contents):
            detected.add(rule.name)

    return sorted(detected)
