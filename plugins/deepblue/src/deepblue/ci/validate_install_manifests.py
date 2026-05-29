"""インストールマニフェストとその相互参照を検証する。"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from devgear.ci.ci_common import REPO_ROOT, emit_error, is_non_empty_string, read_json, resolve_repo_path

COMPONENT_FAMILY_PREFIXES = {
    "baseline": "baseline:",
    "language": "lang:",
    "framework": "framework:",
    "capability": "capability:",
}

DEFAULT_MODULES_MANIFEST_PATH = REPO_ROOT / "manifests" / "install-modules.json"
DEFAULT_PROFILES_MANIFEST_PATH = REPO_ROOT / "manifests" / "install-profiles.json"
DEFAULT_COMPONENTS_MANIFEST_PATH = REPO_ROOT / "manifests" / "install-components.json"
DEFAULT_MODULES_SCHEMA_PATH = REPO_ROOT / "schemas" / "install-modules.schema.json"
DEFAULT_PROFILES_SCHEMA_PATH = REPO_ROOT / "schemas" / "install-profiles.schema.json"
DEFAULT_COMPONENTS_SCHEMA_PATH = REPO_ROOT / "schemas" / "install-components.schema.json"


def _require_list(value: Any, label: str, has_errors: list[bool]) -> list[Any]:
    """値がリストであることを確認し、違反時はエラーを記録する。

    Args:
        value: チェックする値
        label: エラーメッセージ用のラベル
        has_errors: エラー状態を記録するリスト

    Returns:
        value がリストであればそのまま、そうでなければ空リスト

    Raises:
        例外は発生しません（エラーは has_errors に記録）。
    """
    if not isinstance(value, list):
        emit_error(f"{label} は配列である必要があります")
        has_errors[0] = True
        return []
    return value


def validate_install_manifests(
    repo_root: str | Path = REPO_ROOT,
    modules_manifest_path: str | Path = DEFAULT_MODULES_MANIFEST_PATH,
    profiles_manifest_path: str | Path = DEFAULT_PROFILES_MANIFEST_PATH,
    components_manifest_path: str | Path = DEFAULT_COMPONENTS_MANIFEST_PATH,
    modules_schema_path: str | Path | None = DEFAULT_MODULES_SCHEMA_PATH,
    profiles_schema_path: str | Path | None = DEFAULT_PROFILES_SCHEMA_PATH,
    components_schema_path: str | Path | None = DEFAULT_COMPONENTS_SCHEMA_PATH,
) -> int:
    """インストールマニフェストを検証し、JS バリデータと同じメッセージを表示する。

    Args:
        repo_root: 処理に渡す repo_root の値です。
        modules_manifest_path: 処理に渡す modules_manifest_path の値です。
        profiles_manifest_path: 処理に渡す profiles_manifest_path の値です。
        components_manifest_path: 処理に渡す components_manifest_path の値です。
        modules_schema_path: 処理に渡す modules_schema_path の値です。
        profiles_schema_path: 処理に渡す profiles_schema_path の値です。
        components_schema_path: 処理に渡す components_schema_path の値です。

    Returns:
        処理結果を返します。

    Raises:
        例外は発生しません。
    """
    modules_path = Path(modules_manifest_path)
    profiles_path = Path(profiles_manifest_path)
    components_path = Path(components_manifest_path)

    if not modules_path.exists() or not profiles_path.exists():
        print("install マニフェストが見つかりません。検証をスキップします")
        return 0

    try:
        modules_data = read_json(modules_path, "install-modules.json")
        profiles_data = read_json(profiles_path, "install-profiles.json")
        components_data = (
            read_json(components_path, "install-components.json")
            if components_path.exists()
            else {"version": None, "components": []}
        )
    except ValueError as error:
        emit_error(str(error))
        return 1

    if not isinstance(modules_data, dict):
        emit_error("install-modules.json はオブジェクトである必要があります")
        return 1
    if not isinstance(profiles_data, dict):
        emit_error("install-profiles.json はオブジェクトである必要があります")
        return 1
    if not isinstance(components_data, dict):
        emit_error("install-components.json はオブジェクトである必要があります")
        return 1

    modules = modules_data.get("modules")
    profiles = profiles_data.get("profiles")
    components = components_data.get("components")

    errors = [False]
    modules = _require_list(modules, "install-modules.json modules", errors)
    if not isinstance(profiles, dict):
        emit_error("install-profiles.json の profiles はオブジェクトである必要があります")
        return 1
    if components_path.exists():
        components = _require_list(components, "install-components.json components", errors)
    else:
        components = []

    has_errors = errors[0]
    parsed_modules: list[dict[str, Any]] = []
    module_ids: set[str] = set()
    claimed_paths: dict[str, str] = {}

    for module in modules:
        if not isinstance(module, dict):
            emit_error("モジュールエントリはオブジェクトではありません")
            has_errors = True
            continue

        module_id = module.get("id")
        if not is_non_empty_string(module_id):
            emit_error("モジュールエントリの id が不足しているか無効です")
            has_errors = True
            continue

        if module_id in module_ids:
            emit_error(f"重複したインストールモジュール ID: {module_id}")
            has_errors = True
        module_ids.add(module_id)

        dependencies = module.get("dependencies")
        if dependencies is None:
            dependencies = []
        elif not isinstance(dependencies, list):
            emit_error(f"モジュール {module_id} の dependencies 配列が無効です")
            has_errors = True
            dependencies = []

        paths = module.get("paths")
        if paths is None:
            paths = []
        elif not isinstance(paths, list):
            emit_error(f"モジュール {module_id} の paths 配列が無効です")
            has_errors = True
            paths = []

        parsed_modules.append(
            {
                "id": module_id,
                "dependencies": dependencies,
                "paths": paths,
            }
        )

    expected_profile_ids = ["core", "developer", "security", "research", "full"]
    parsed_profiles: dict[str, list[str]] = {}

    for profile_id in expected_profile_ids:
        if profile_id not in profiles:
            emit_error(f"必須のインストールプロファイルがありません: {profile_id}")
            has_errors = True

    for profile_id, profile in profiles.items():
        if not isinstance(profile, dict):
            emit_error(f"プロファイル {profile_id} はオブジェクトである必要があります")
            has_errors = True
            parsed_profiles[profile_id] = []
            continue

        modules_list = profile.get("modules")
        if not isinstance(modules_list, list):
            emit_error(f"プロファイル {profile_id} の modules は配列である必要があります")
            has_errors = True
            parsed_profiles[profile_id] = []
            continue

        parsed_profiles[profile_id] = modules_list

    parsed_components: list[dict[str, Any]] = []
    component_ids: set[str] = set()

    for component in components:
        if not isinstance(component, dict):
            emit_error("コンポーネントエントリはオブジェクトではありません")
            has_errors = True
            continue

        component_id = component.get("id")
        if not is_non_empty_string(component_id):
            emit_error("コンポーネントエントリの id が不足しているか無効です")
            has_errors = True
            continue

        if component_id in component_ids:
            emit_error(f"重複したインストールコンポーネント ID: {component_id}")
            has_errors = True
        component_ids.add(component_id)

        family = component.get("family")
        if not is_non_empty_string(family):
            emit_error(f"コンポーネント {component_id} の family が不足しているか無効です")
            has_errors = True
            family = ""

        modules_list = component.get("modules")
        if not isinstance(modules_list, list):
            emit_error(f"コンポーネント {component_id} の modules は配列である必要があります")
            has_errors = True
            modules_list = []

        parsed_components.append({"id": component_id, "family": family, "modules": modules_list})

    for module in parsed_modules:
        module_id = module["id"]
        for dependency in module["dependencies"]:
            if not is_non_empty_string(dependency):
                emit_error(f"モジュール {module_id} の依存関係 {dependency} が無効です")
                has_errors = True
                continue
            if dependency == module_id:
                emit_error(f"モジュール {module_id} は自分自身に依存できません")
                has_errors = True
            elif dependency not in module_ids:
                emit_error(f"モジュール {module_id} は不明なモジュール {dependency} に依存しています")
                has_errors = True

        for relative_path in module["paths"]:
            if not is_non_empty_string(relative_path):
                emit_error(f"モジュール {module_id} は存在しないパスを参照しています: {relative_path}")
                has_errors = True
                continue
            normalized_path = str(relative_path).replace("\\", "/").rstrip("/")
            absolute_path = resolve_repo_path(repo_root, normalized_path)
            if not absolute_path.exists():
                emit_error(f"モジュール {module_id} は存在しないパスを参照しています: {normalized_path}")
                has_errors = True

            if normalized_path in claimed_paths:
                emit_error(
                    f"インストールパス {normalized_path} は {claimed_paths[normalized_path]} と {module_id} の両方で宣言されています"
                )
                has_errors = True
            else:
                claimed_paths[normalized_path] = module_id

    for profile_id, module_list in parsed_profiles.items():
        seen_modules: set[str] = set()
        for module_id in module_list:
            if not is_non_empty_string(module_id):
                emit_error(f"プロファイル {profile_id} は不明なモジュール {module_id} を参照しています")
                has_errors = True
                continue
            if module_id not in module_ids:
                emit_error(f"プロファイル {profile_id} は不明なモジュール {module_id} を参照しています")
                has_errors = True

            if module_id in seen_modules:
                emit_error(f"プロファイル {profile_id} に重複したモジュール {module_id} が含まれています")
                has_errors = True
            seen_modules.add(module_id)

    if "full" in parsed_profiles:
        full_modules = set(parsed_profiles["full"])
        for module_id in module_ids:
            if module_id not in full_modules:
                emit_error(f"full プロファイルにモジュール {module_id} がありません")
                has_errors = True

    for component in parsed_components:
        component_id = component["id"]
        family = component["family"]
        expected_prefix = COMPONENT_FAMILY_PREFIXES.get(family)
        if expected_prefix and not component_id.startswith(expected_prefix):
            emit_error(
                f"コンポーネント {component_id} は想定される {family} のプレフィックス {expected_prefix} と一致しません"
            )
            has_errors = True

        seen_modules: set[str] = set()
        for module_id in component["modules"]:
            if not is_non_empty_string(module_id):
                emit_error(f"コンポーネント {component_id} は不明なモジュール {module_id} を参照しています")
                has_errors = True
                continue
            if module_id not in module_ids:
                emit_error(f"コンポーネント {component_id} は不明なモジュール {module_id} を参照しています")
                has_errors = True

            if module_id in seen_modules:
                emit_error(f"コンポーネント {component_id} に重複したモジュール {module_id} が含まれています")
                has_errors = True
            seen_modules.add(module_id)

    if has_errors:
        return 1

    print(
        f"{len(parsed_modules)} 個のインストールモジュール、{len(parsed_components)} 個のインストールコンポーネント、{len(profiles)} 個のプロファイルを検証しました"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    """CLI パーサーを構築する。

    Args:
        引数はありません。

    Returns:
        処理結果を返します。

    Raises:
        例外は発生しません。
    """
    parser = argparse.ArgumentParser(description="Validate install manifests")
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument("--modules-manifest-path", default=str(DEFAULT_MODULES_MANIFEST_PATH))
    parser.add_argument("--profiles-manifest-path", default=str(DEFAULT_PROFILES_MANIFEST_PATH))
    parser.add_argument("--components-manifest-path", default=str(DEFAULT_COMPONENTS_MANIFEST_PATH))
    parser.add_argument("--modules-schema-path", default=str(DEFAULT_MODULES_SCHEMA_PATH))
    parser.add_argument("--profiles-schema-path", default=str(DEFAULT_PROFILES_SCHEMA_PATH))
    parser.add_argument("--components-schema-path", default=str(DEFAULT_COMPONENTS_SCHEMA_PATH))
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI のエントリポイント。

    Args:
        argv: 処理に渡す argv の値です。

    Returns:
        処理結果を返します。

    Raises:
        例外は発生しません。
    """
    args = build_parser().parse_args(argv)
    return validate_install_manifests(
        args.repo_root,
        args.modules_manifest_path,
        args.profiles_manifest_path,
        args.components_manifest_path,
        args.modules_schema_path,
        args.profiles_schema_path,
        args.components_schema_path,
    )


if __name__ == "__main__":
    raise SystemExit(main())
