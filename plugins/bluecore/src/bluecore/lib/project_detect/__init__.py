"""
リポジトリ内の言語とフレームワークを検出します。
ファイル名、依存関係、ファイル内容を組み合わせて判定し、テストやビルドの既定コマンドも推定します。
プロジェクト種別に応じた後続処理の分岐に使います。

分割前の単一モジュール ``bluecore.lib.project_detect`` と同一の名前空間を保つため、
公開シンボルだけでなく内部ヘルパ・``Path`` も従来通りこのパッケージ直下から
参照できるよう再エクスポートする。``__all__`` には公開 API のみを列挙する。
"""

from __future__ import annotations

# 旧モジュールでは ``Path`` がモジュール属性として参照可能だった（テストの
# ``monkeypatch.setattr(pd.Path, ...)`` 等）ため、同じ名前で再公開する。
from pathlib import Path as Path

from bluecore.lib.project_detect.commands import (
    detect_project as detect_project,
)
from bluecore.lib.project_detect.commands import (
    get_build_command as get_build_command,
)
from bluecore.lib.project_detect.commands import (
    get_test_command as get_test_command,
)
from bluecore.lib.project_detect.dependency_checks import (
    _check_cargo_toml_deps as _check_cargo_toml_deps,
)
from bluecore.lib.project_detect.dependency_checks import (
    _check_composer_json_deps as _check_composer_json_deps,
)
from bluecore.lib.project_detect.dependency_checks import (
    _check_csproj_deps as _check_csproj_deps,
)
from bluecore.lib.project_detect.dependency_checks import (
    _check_file_contents as _check_file_contents,
)
from bluecore.lib.project_detect.dependency_checks import (
    _check_gemfile_deps as _check_gemfile_deps,
)
from bluecore.lib.project_detect.dependency_checks import (
    _check_go_mod_deps as _check_go_mod_deps,
)
from bluecore.lib.project_detect.dependency_checks import (
    _check_gradle_deps as _check_gradle_deps,
)
from bluecore.lib.project_detect.dependency_checks import (
    _check_package_json_deps as _check_package_json_deps,
)
from bluecore.lib.project_detect.dependency_checks import (
    _check_pom_xml_deps as _check_pom_xml_deps,
)
from bluecore.lib.project_detect.dependency_checks import (
    _check_pubspec_deps as _check_pubspec_deps,
)
from bluecore.lib.project_detect.dependency_checks import (
    _check_requirements_deps as _check_requirements_deps,
)
from bluecore.lib.project_detect.dependency_checks import (
    _is_rails_app as _is_rails_app,
)
from bluecore.lib.project_detect.dependency_checks import (
    _read_json_file as _read_json_file,
)
from bluecore.lib.project_detect.dependency_checks import (
    _read_text_file as _read_text_file,
)
from bluecore.lib.project_detect.frameworks import (
    detect_frameworks as detect_frameworks,
)
from bluecore.lib.project_detect.languages import (
    _limited_file_scan as _limited_file_scan,
)
from bluecore.lib.project_detect.languages import (
    detect_languages as detect_languages,
)
from bluecore.lib.project_detect.models import (
    FrameworkRule as FrameworkRule,
)
from bluecore.lib.project_detect.models import (
    LanguageRule as LanguageRule,
)
from bluecore.lib.project_detect.models import (
    ProjectInfo as ProjectInfo,
)
from bluecore.lib.project_detect.rules import (
    FRAMEWORK_RULES as FRAMEWORK_RULES,
)
from bluecore.lib.project_detect.rules import (
    LANGUAGE_RULES as LANGUAGE_RULES,
)

__all__ = [
    "FRAMEWORK_RULES",
    "LANGUAGE_RULES",
    "FrameworkRule",
    "LanguageRule",
    "ProjectInfo",
    "detect_frameworks",
    "detect_languages",
    "detect_project",
    "get_build_command",
    "get_test_command",
]
