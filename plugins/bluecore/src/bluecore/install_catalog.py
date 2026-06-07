"""インストール用コンポーネントとプロファイルを見つけるための CLI。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SUPPORTED_INSTALL_TARGETS = ["claude"]
COMPONENT_FAMILY_PREFIXES = {
    "baseline": "baseline:",
    "language": "lang:",
    "framework": "framework:",
    "capability": "capability:",
    "agent": "agent:",
    "skill": "skill:",
}
FAMILY_ALIASES = {
    "baseline": "baseline",
    "baselines": "baseline",
    "language": "language",
    "languages": "language",
    "lang": "language",
    "framework": "framework",
    "frameworks": "framework",
    "capability": "capability",
    "capabilities": "capability",
    "agent": "agent",
    "agents": "agent",
    "skill": "skill",
    "skills": "skill",
}

HELP_TEXT = """
Discover bluecore install components and profiles

Usage:
  python -m bluecore.install_catalog profiles [--json]
  python -m bluecore.install_catalog components [--family <family>] [--target <target>] [--json]
  python -m bluecore.install_catalog show <component-id> [--json]

Examples:
  python -m bluecore.install_catalog profiles
  python -m bluecore.install_catalog components --family language
  python -m bluecore.install_catalog show framework:nextjs
"""


def normalize_family(value: Any) -> str | None:
    """ファミリー名を正規化し、エイリアスを解決します。

    Args:
        value: 正規化対象の値です。

    Returns:
        正規化されたファミリー名、または値が空の場合は None を返します。

    Raises:
        このヘルパー関数は例外を発生させません。
    """
    if not value:
        return None
    normalized = str(value).strip().lower()
    return FAMILY_ALIASES.get(normalized, normalized)


def dedupe_strings(values: Any) -> list[str]:
    """文字列配列から重複を取り除き、空文字列を除外します。

    Args:
        values: 処理対象のリストです。

    Returns:
        重複を除いた文字列のリストを返します。

    Raises:
        例外は発生しません。
    """
    if not isinstance(values, list):
        return []

    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            deduped.append(text)
    return deduped


def read_json(file_path: str | Path, label: str) -> Any:
    """JSON ファイルを読み取り、パースします。

    Args:
        file_path: 読み取り対象のファイルパスです。
        label: エラーメッセージに使用するラベルです。

    Returns:
        パースされた JSON データを返します。

    Raises:
        RuntimeError: JSON のパースに失敗した場合に発生します。
    """
    try:
        return json.loads(Path(file_path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Failed to read {label}: {error}") from error


def get_manifest_paths(repo_root: str | Path = REPO_ROOT) -> dict[str, Path]:
    """インストールマニフェストファイルのパスを取得します。

    Args:
        repo_root: リポジトリのルートディレクトリです。

    Returns:
        マニフェストファイルパスの辞書を返します。

    Raises:
        例外は発生しません。
    """
    root = Path(repo_root)
    return {
        "modulesPath": root / "manifests" / "install-modules.json",
        "profilesPath": root / "manifests" / "install-profiles.json",
        "componentsPath": root / "manifests" / "install-components.json",
    }


def _read_manifest_data(paths: dict[str, Path]) -> tuple[Any, Any, Any]:
    """3 つのマニフェスト JSON を読み込み、(modules_data, profiles_data, components_data) を返す。"""
    modules_data = read_json(paths["modulesPath"], "install-modules.json")
    profiles_data = read_json(paths["profilesPath"], "install-profiles.json")
    components_data = (
        read_json(paths["componentsPath"], "install-components.json")
        if paths["componentsPath"].exists()
        else {"version": None, "components": []}
    )
    return modules_data, profiles_data, components_data


def _extract_manifest_lists(
    modules_data: Any, profiles_data: Any, components_data: Any
) -> tuple[list[dict], dict, list[dict]]:
    """各マニフェストデータから modules/profiles/components を抽出して返す。"""
    modules = (
        modules_data["modules"]
        if isinstance(modules_data, dict) and isinstance(modules_data.get("modules"), list)
        else []
    )
    profiles = (
        profiles_data["profiles"]
        if isinstance(profiles_data, dict) and isinstance(profiles_data.get("profiles"), dict)
        else {}
    )
    components = (
        components_data["components"]
        if isinstance(components_data, dict) and isinstance(components_data.get("components"), list)
        else []
    )
    return modules, profiles, components


def load_install_manifests(options: dict[str, Any] | None = None) -> dict[str, Any]:
    """インストールマニフェストを読み込み、ID でインデックス化します。

    Args:
        options: リポジトリルートを指定するオプションです（repoRoot キー）。

    Returns:
        次のキーを持つ辞書: repoRoot / modulesPath / profilesPath /
        componentsPath / modules / profiles / components / modulesById /
        componentsById / modulesVersion / profilesVersion / componentsVersion。

    Raises:
        RuntimeError: マニフェストファイルが見つからない場合に発生します。
    """
    opts = options or {}
    repo_root = Path(opts.get("repoRoot") or REPO_ROOT)
    paths = get_manifest_paths(repo_root)
    if not paths["modulesPath"].exists() or not paths["profilesPath"].exists():
        raise RuntimeError(f"Install manifests not found under {repo_root}")
    modules_data, profiles_data, components_data = _read_manifest_data(paths)
    modules, profiles, components = _extract_manifest_lists(modules_data, profiles_data, components_data)
    modules_by_id = {m["id"]: m for m in modules if isinstance(m, dict) and "id" in m}
    components_by_id = {c["id"]: c for c in components if isinstance(c, dict) and "id" in c}
    return {
        "repoRoot": repo_root,
        "modulesPath": paths["modulesPath"],
        "profilesPath": paths["profilesPath"],
        "componentsPath": paths["componentsPath"],
        "modules": modules, "profiles": profiles, "components": components,
        "modulesById": modules_by_id, "componentsById": components_by_id,
        "modulesVersion": modules_data.get("version") if isinstance(modules_data, dict) else None,
        "profilesVersion": profiles_data.get("version") if isinstance(profiles_data, dict) else None,
        "componentsVersion": components_data.get("version") if isinstance(components_data, dict) else None,
    }


def _intersect_targets(modules: list[dict[str, Any]]) -> list[str]:
    """全モジュールで共通するターゲットを抽出します。

    Args:
        modules: モジュールの辞書のリストです。

    Returns:
        共通ターゲットのリストを返します。

    Raises:
        例外は発生しません。
    """
    if not modules:
        return []
    if all(isinstance(module.get("targets"), list) and "claude" in module["targets"] for module in modules):
        return ["claude"]
    return []


def list_install_profiles(options: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """インストールプロファイルを一覧表示します。

    Args:
        options: マニフェスト読み込み用のオプションです。

    Returns:
        プロファイル情報の辞書のリストを返します。

    Raises:
        RuntimeError: マニフェストの読み込みに失敗した場合に発生します。
    """
    manifests = load_install_manifests(options)
    return [
        {
            "id": profile_id,
            "description": profile.get("description"),
            "moduleCount": len(profile.get("modules", [])) if isinstance(profile, dict) else 0,
        }
        for profile_id, profile in manifests["profiles"].items()
    ]


def list_install_components(options: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """インストールコンポーネントを一覧表示します。

    Args:
        options: family や target などのフィルタオプションです。

    Returns:
        コンポーネント情報の辞書のリストを返します。

    Raises:
        ValueError: 不明なファミリーまたはターゲットが指定された場合に発生します。
        RuntimeError: マニフェストの読み込みに失敗した場合に発生します。
    """
    manifests = load_install_manifests(options)
    family = (options or {}).get("family") or None
    target = (options or {}).get("target") or "claude"

    if family and family not in COMPONENT_FAMILY_PREFIXES:
        raise ValueError(f"Unknown component family: {family}. Expected one of {', '.join(COMPONENT_FAMILY_PREFIXES)}")

    if target and target not in SUPPORTED_INSTALL_TARGETS:
        raise ValueError(f"Unknown install target: {target}. Expected one of {', '.join(SUPPORTED_INSTALL_TARGETS)}")

    components: list[dict[str, Any]] = []
    for component in manifests["components"]:
        if not isinstance(component, dict):
            continue
        if family and component.get("family") != family:
            continue

        module_ids = dedupe_strings(component.get("modules"))
        modules = [manifests["modulesById"].get(module_id) for module_id in module_ids]
        modules = [module for module in modules if module]
        targets = _intersect_targets(modules)
        result = {
            "id": component.get("id"),
            "family": component.get("family"),
            "description": component.get("description"),
            "moduleIds": module_ids,
            "moduleCount": len(module_ids),
            "targets": targets,
        }
        if not target or target in targets:
            components.append(result)

    return components


def _resolve_component_modules(
    manifests: dict[str, Any],
    module_ids: list[str],
) -> list[dict[str, Any]]:
    """モジュール ID リストをマニフェストから解決し、モジュール詳細辞書のリストを返す。"""
    modules: list[dict[str, Any]] = []
    for module_id in module_ids:
        module = manifests["modulesById"].get(module_id)
        if not module:
            continue
        modules.append(
            {
                "id": module.get("id"),
                "kind": module.get("kind"),
                "description": module.get("description"),
                "targets": module.get("targets"),
                "defaultInstall": module.get("defaultInstall"),
                "cost": module.get("cost"),
                "stability": module.get("stability"),
                "dependencies": dedupe_strings(module.get("dependencies")),
            }
        )
    return modules


def get_install_component(component_id: Any, options: dict[str, Any] | None = None) -> dict[str, Any]:
    """指定された ID のインストールコンポーネントを取得します。

    Args:
        component_id: 取得するコンポーネントの ID です。
        options: マニフェスト読み込み用のオプションです。

    Returns:
        コンポーネントと解決されたモジュールの詳細情報を返します。

    Raises:
        ValueError: コンポーネント ID が空、または存在しない場合に発生します。
        RuntimeError: マニフェストの読み込みに失敗した場合に発生します。
    """
    manifests = load_install_manifests(options)
    normalized_component_id = str(component_id or "").strip()
    if not normalized_component_id:
        raise ValueError("An install component ID is required")

    component = manifests["componentsById"].get(normalized_component_id)
    if not component:
        raise ValueError(f"Unknown install component: {normalized_component_id}")

    module_ids = dedupe_strings(component.get("modules"))
    modules = _resolve_component_modules(manifests, module_ids)

    return {
        "id": component.get("id"),
        "family": component.get("family"),
        "description": component.get("description"),
        "moduleIds": module_ids,
        "moduleCount": len(module_ids),
        "targets": _intersect_targets(modules),
        "modules": modules,
    }


def _show_help() -> None:
    """ヘルプテキストを標準出力に表示します。

    Args:
        なし

    Returns:
        なし

    Raises:
        例外は発生しません。
    """
    sys.stdout.write(HELP_TEXT)


def _normalize_options(argv: list[str]) -> dict[str, Any]:
    """コマンドライン引数を解析し、正規化します。

    Args:
        argv: コマンドライン引数のリストです。

    Returns:
        パースされたオプションの辞書を返します。

    Raises:
        ValueError: 不明な引数や、必須の値が不足している場合に発生します。
    """
    parsed = {
        "command": None,
        "componentId": None,
        "family": None,
        "target": None,
        "json": False,
        "help": False,
    }

    if not argv or argv[0] in {"--help", "-h"}:
        parsed["help"] = True
        return parsed

    parsed["command"] = argv[0]
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg in {"--help", "-h"}:
            parsed["help"] = True
        elif arg == "--json":
            parsed["json"] = True
        elif arg == "--family":
            if index + 1 >= len(argv) or not argv[index + 1]:
                raise ValueError("Missing value for --family")
            parsed["family"] = normalize_family(argv[index + 1])
            index += 1
        elif arg == "--target":
            if index + 1 >= len(argv) or not argv[index + 1]:
                raise ValueError("Missing value for --target")
            parsed["target"] = argv[index + 1]
            index += 1
        elif parsed["command"] == "show" and not parsed["componentId"]:
            parsed["componentId"] = arg
        else:
            raise ValueError(f"Unknown argument: {arg}")
        index += 1

    return parsed


def _print_profiles(profiles: list[dict[str, Any]]) -> None:
    """プロファイル一覧を人間が読める形式で出力します。

    Args:
        profiles: プロファイル情報の辞書のリストです。

    Returns:
        なし

    Raises:
        例外は発生しません。
    """
    print("Install profiles:\n")
    for profile in profiles:
        print(f"- {profile['id']} ({profile['moduleCount']} modules)")
        print(f"  {profile.get('description')}")


def _print_components(components: list[dict[str, Any]]) -> None:
    """コンポーネント一覧を人間が読める形式で出力します。

    Args:
        components: コンポーネント情報の辞書のリストです。

    Returns:
        なし

    Raises:
        例外は発生しません。
    """
    print("Install components:\n")
    for component in components:
        print(f"- {component['id']} [{component['family']}]")
        print(f"  targets={', '.join(component['targets'])} modules={', '.join(component['moduleIds'])}")
        print(f"  {component.get('description')}")


def _print_component(component: dict[str, Any]) -> None:
    """単一のコンポーネントの詳細を人間が読める形式で出力します。

    Args:
        component: コンポーネント情報の辞書です。

    Returns:
        なし

    Raises:
        例外は発生しません。
    """
    print(f"Install component: {component['id']}\n")
    print(f"Family: {component['family']}")
    print(f"Targets: {', '.join(component['targets'])}")
    print(f"Modules: {', '.join(component['moduleIds'])}")
    print(f"Description: {component.get('description')}")

    if component["modules"]:
        print("\nResolved modules:")
        for module in component["modules"]:
            print(f"- {module['id']} [{module['kind']}]")
            print(
                f"  targets={', '.join(module['targets'])} default={module['defaultInstall']} "
                f"cost={module['cost']} stability={module['stability']}"
            )
            print(f"  {module['description']}")


def main(argv: list[str] | None = None) -> int:
    """CLI のエントリポイントです。

    Args:
        argv: コマンドライン引数のリストです。

    Returns:
        成功時は 0、エラー時は 1 を返します。

    Raises:
        例外はキャッチされ、エラーメッセージとして出力されます。
    """
    try:
        options = _normalize_options(list(sys.argv[1:] if argv is None else argv))

        if options["help"]:
            _show_help()
            return 0

        if options["command"] == "profiles":
            profiles = list_install_profiles()
            if options["json"]:
                sys.stdout.write(json.dumps({"profiles": profiles}, indent=2, ensure_ascii=False) + "\n")
            else:
                _print_profiles(profiles)
            return 0

        if options["command"] == "components":
            components = list_install_components({"family": options["family"], "target": options["target"]})
            if options["json"]:
                sys.stdout.write(json.dumps({"components": components}, indent=2, ensure_ascii=False) + "\n")
            else:
                _print_components(components)
            return 0

        if options["command"] == "show":
            if not options["componentId"]:
                raise ValueError("Catalog show requires an install component ID")
            component = get_install_component(options["componentId"])
            if options["json"]:
                sys.stdout.write(json.dumps(component, indent=2, ensure_ascii=False) + "\n")
            else:
                _print_component(component)
            return 0

        raise ValueError(f"Unknown catalog command: {options['command']}")
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
