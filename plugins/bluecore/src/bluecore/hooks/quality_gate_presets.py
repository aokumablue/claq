"""言語別の quality-gate プリセット。

`detect_project()` の検出結果に応じて post-edit で走らせる lint
コマンドを決定する。primary_language が未対応でも対応言語があれば採用する。
ツールが PATH に無い場合は該当ステップをスキップする。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from bluecore.lib.project_detect import detect_project

QUALITY_GATE_PRESETS: dict[str, dict[str, Any]] = {
    "python": {
        "extensions": [".py", ".pyi"],
        "bash": [["ruff", "check", "."]],
    },
    "javascript": {
        "extensions": [".js", ".mjs", ".cjs"],
        "bash": [["npx", "--no-install", "eslint", "."]],
    },
    "typescript": {
        "extensions": [".ts", ".tsx"],
        "bash": [["npx", "--no-install", "eslint", "."]],
    },
    "go": {
        "extensions": [".go"],
        "bash": [["go", "vet", "./..."]],
    },
    "rust": {
        "extensions": [".rs"],
        "bash": [["cargo", "clippy", "--all"]],
    },
    "ruby": {
        "extensions": [".rb", ".rake"],
        "bash": [["rubocop"]],
    },
}


def _has_executable(argv: list[str]) -> bool:
    """コマンド argv の先頭実行ファイルが PATH にあるか判定する。

    Args:
        argv: 実行予定のコマンド列。

    Returns:
        先頭コマンドが PATH 上で見つかれば True。

    Raises:
        例外は発生しません。
    """
    if not argv:
        return False
    return shutil.which(argv[0]) is not None


def _select_language(info: Any) -> str | None:
    """検出結果から採用する言語を決める。

    primary_language が未対応でも、languages に対応言語があればそれを採用する。
    """
    candidates: list[str] = []
    primary = getattr(info, "primary_language", None)
    if isinstance(primary, str) and primary:
        candidates.append(primary)

    languages = getattr(info, "languages", [])
    if isinstance(languages, list):
        candidates.extend(language for language in languages if isinstance(language, str))

    seen: set[str] = set()
    for language in candidates:
        normalized = language.strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        if normalized in QUALITY_GATE_PRESETS:
            return normalized
    return None


def _select_target_path(root: Path, preferred: str = "src") -> str:
    """存在する場合は preferred、無ければカレントを対象にする。"""
    return preferred if (root / preferred).exists() else "."


_EMPTY_CONFIG: dict[str, Any] = {"actions": {"post-edit": {"rules": []}}}


def _resolve_argv(argv: list[str], language: str, target: str, file_path: str | None) -> list[str]:
    """argv の最終引数（対象パス）を言語・file_path に応じて解決して返す。

    Args:
        argv: 元のコマンド引数リスト（文字列化済み）。
        language: 検出した言語名。
        target: プロジェクト内の対象ディレクトリ（"src" or "."）。
        file_path: 変更されたファイルパス。指定時は単一ファイルを対象にする。

    Returns:
        最終引数を置換済みのコマンドリスト。
    """
    resolved = list(argv)
    if language == "python" and resolved[0] == "ruff" and len(resolved) >= 3 and resolved[1] == "check":
        if file_path and str(file_path).endswith((".py", ".pyi")):
            resolved[-1] = str(file_path)
        else:
            resolved[-1] = target
    elif language in {"javascript", "typescript"} and resolved[:3] == ["npx", "--no-install", "eslint"]:
        resolved[-1] = target
    return resolved


def _build_steps(preset: dict[str, Any], language: str, target: str, file_path: str | None) -> list[dict[str, Any]]:
    """プリセットの bash コマンド群から実行可能な steps リストを構築する。

    Args:
        preset: QUALITY_GATE_PRESETS のエントリ。
        language: 採用した言語名。
        target: プロジェクト内の対象ディレクトリ。
        file_path: 変更されたファイルパス（省略可）。

    Returns:
        {"argv": [...]} 形式の step 辞書リスト。
    """
    steps: list[dict[str, Any]] = []
    for argv in preset.get("bash", []):
        if not isinstance(argv, list) or not argv or not _has_executable(argv):
            continue
        resolved_argv = _resolve_argv([str(x) for x in argv], language, target, file_path)
        steps.append({"argv": resolved_argv})
    return steps


def resolve_quality_gate_config(
    cwd: str | Path | None = None,
    file_path: str | None = None,
) -> dict[str, Any]:
    """現在のプロジェクトに対応する quality-gate 設定を生成する。

    `detect_project()` の主要言語からプリセットを選び、rule 形式の辞書で返す。
    ツールが PATH に無ければ steps を絞り込む。該当プリセットが無い／steps が無い
    場合は空の rules を返す。

    Args:
        cwd: プロジェクトルート。省略時はカレントディレクトリ。
        file_path: 変更されたファイルパス。指定時は Python の ruff を単一ファイル対象で実行する。

    Returns:
        `{"actions": {"post-edit": {"rules": [...]}}}` 形式の辞書。

    Raises:
        例外は発生しません。
    """
    root = Path(cwd) if cwd is not None else Path.cwd()
    try:
        info = detect_project(root)
    except Exception:  # noqa: BLE001 - 判定失敗時は空設定
        return _EMPTY_CONFIG

    language = _select_language(info)
    preset = QUALITY_GATE_PRESETS.get(language) if language else None
    if not preset:
        return _EMPTY_CONFIG

    target = _select_target_path(root)
    steps = _build_steps(preset, language, target, file_path)
    if not steps:
        return _EMPTY_CONFIG

    rule: dict[str, Any] = {"extensions": list(preset.get("extensions", [])), "steps": steps}
    return {"actions": {"post-edit": {"rules": [rule]}}}


__all__ = [
    "QUALITY_GATE_PRESETS",
    "resolve_quality_gate_config",
]
