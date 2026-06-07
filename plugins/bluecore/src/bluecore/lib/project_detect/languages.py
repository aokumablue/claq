"""プロジェクトの使用言語検出とファイル走査。"""

from __future__ import annotations

from pathlib import Path

from bluecore.lib.project_detect.rules import LANGUAGE_RULES


def _collect_root_files(root: Path) -> set[str]:
    """ルートディレクトリの直下ファイル名一覧を返す（速度のため非再帰）。

    Args:
        root: 対象ルートディレクトリ。

    Returns:
        ファイル名の集合。権限エラー時は空集合。
    """
    try:
        return {f.name for f in root.iterdir() if f.is_file()}
    except (PermissionError, OSError):
        return set()


def _detect_by_marker_files(root: Path, root_files: set[str], detected: set[str]) -> None:
    """マーカーファイル（またはグロブパターン）で言語を検出し detected へ追加する。

    Args:
        root: プロジェクトルートディレクトリ。
        root_files: ルート直下のファイル名集合。
        detected: 検出済み言語名を蓄積するセット（in-place 更新）。
    """
    for rule in LANGUAGE_RULES:
        for marker_file in rule.files:
            if "*" in marker_file:
                if any(root.glob(marker_file)):
                    detected.add(rule.name)
                    break
            elif marker_file in root_files:
                detected.add(rule.name)
                break


def _build_extension_map() -> dict[str, str]:
    """拡張子→言語名のマッピングを構築して返す。

    Returns:
        拡張子をキー、言語名を値とする辞書。
    """
    return {ext: rule.name for rule in LANGUAGE_RULES for ext in rule.extensions}


def _detect_by_extensions(root: Path, extension_map: dict[str, str], detected: set[str]) -> None:
    """ファイル拡張子スキャンで言語を検出し detected へ追加する。

    Args:
        root: プロジェクトルートディレクトリ。
        extension_map: 拡張子→言語名マッピング。
        detected: 検出済み言語名を蓄積するセット（in-place 更新）。
    """
    for file_path in _limited_file_scan(root, max_depth=3, max_files=1000):
        lang = extension_map.get(file_path.suffix)
        if lang is not None:
            detected.add(lang)


def detect_languages(project_root: str | Path) -> list[str]:
    """プロジェクトで使われているプログラミング言語を検出する。

    Args:
        project_root: プロジェクトルートのパス。

    Returns:
        検出された言語名のソート済みリスト。存在しないパスの場合は空リスト。

    Raises:
        例外は発生しません。
    """
    root = Path(project_root)
    if not root.exists():
        return []

    detected: set[str] = set()
    root_files = _collect_root_files(root)
    _detect_by_marker_files(root, root_files, detected)
    _detect_by_extensions(root, _build_extension_map(), detected)
    return sorted(detected)


_SKIP_DIRS = frozenset(["node_modules", "__pycache__", "venv", ".venv", ".git"])


def _scan_dir(directory: Path, depth: int, files: list[Path], max_depth: int, max_files: int) -> None:
    """ディレクトリを再帰的に探索し、見つかったファイルを files リストに追加する。

    Args:
        directory: 探索対象ディレクトリ。
        depth: 現在の探索深さ。
        files: 結果を蓄積するリスト。
        max_depth: 探索する最大深さ。
        max_files: 蓄積する最大ファイル数。
    """
    if depth > max_depth or len(files) >= max_files:
        return
    try:
        entries = list(directory.iterdir())
    except (PermissionError, OSError):
        return
    for entry in entries:
        if len(files) >= max_files:
            return
        if entry.name.startswith(".") or entry.name in _SKIP_DIRS:
            continue
        if entry.is_file():
            files.append(entry)
        elif entry.is_dir():
            _scan_dir(entry, depth + 1, files, max_depth, max_files)


def _limited_file_scan(
    root: Path,
    max_depth: int = 3,
    max_files: int = 1000,
) -> list[Path]:
    """深さと件数の上限付きでファイルを走査する。

    Args:
        root: 走査を開始するルートディレクトリ。
        max_depth: 探索する最大深さ。
        max_files: 返す最大ファイル数。

    Returns:
        収集したファイルパスのリスト。
    """
    files: list[Path] = []
    _scan_dir(root, 0, files, max_depth, max_files)
    return files
