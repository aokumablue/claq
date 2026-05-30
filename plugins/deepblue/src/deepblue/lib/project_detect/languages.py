"""プロジェクトの使用言語検出とファイル走査。"""

from __future__ import annotations

from pathlib import Path

from deepblue.lib.project_detect.rules import LANGUAGE_RULES


def detect_languages(project_root: str | Path) -> list[str]:
    """プロジェクトで使われているプログラミング言語を検出する。

    Args:
        project_root: project_root の値

    Returns:
        list[str]: str の一覧を返します。

    Raises:
        例外は発生しません。
    """
    root = Path(project_root)
    if not root.exists():
        return []

    detected: set[str] = set()

    # ルートディレクトリのファイルを収集する（速度のため非再帰）
    try:
        root_files = {f.name for f in root.iterdir() if f.is_file()}
    except (PermissionError, OSError):
        root_files = set()

    for rule in LANGUAGE_RULES:
        # マーカーファイルを確認
        for marker_file in rule.files:
            if "*" in marker_file:
                # グロブパターンを処理
                pattern = marker_file
                if any(root.glob(pattern)):
                    detected.add(rule.name)
                    break
            elif marker_file in root_files:
                detected.add(rule.name)
                break

    # 拡張子の高速スキャン（性能のため深さを制限）
    extension_languages: dict[str, str] = {}
    for rule in LANGUAGE_RULES:
        for ext in rule.extensions:
            extension_languages[ext] = rule.name

    for file_path in _limited_file_scan(root, max_depth=3, max_files=1000):
        ext = file_path.suffix
        if ext in extension_languages:
            detected.add(extension_languages[ext])

    return sorted(detected)


def _limited_file_scan(
    root: Path,
    max_depth: int = 3,
    max_files: int = 1000,
) -> list[Path]:
    """深さと件数の上限付きでファイルを走査する。

    Args:
        root: root の値
        max_depth: 探索する最大深さ
        max_files: 返す最大ファイル数

    Returns:
        list[Path]: Path の一覧を返します。

    Raises:
        例外は発生しません。
    """
    files: list[Path] = []

    def scan(directory: Path, depth: int) -> None:
        """ディレクトリを再帰的に探索し、見つかったファイルを外側の files リストに追加する。

        Args:
            directory: 探索対象ディレクトリ。
            depth: 現在の探索深さ。

        Returns:
            None: 結果は外側の files リストに追加されます。

        Raises:
            例外は発生しません。
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

            if entry.name.startswith("."):
                continue
            if entry.name == "node_modules":
                continue
            if entry.name == "__pycache__":
                continue
            if entry.name == "venv" or entry.name == ".venv":
                continue

            if entry.is_file():
                files.append(entry)
            elif entry.is_dir():
                scan(entry, depth + 1)

    scan(root, 0)
    return files
