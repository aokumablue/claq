"""コードベースのコードマップ（構造ドキュメント）を生成するスクリプト。

各領域（frontend/backend/database/integrations/workers）のマークダウンドキュメントと、
全体インデックスを OUTPUT_DIR に書き出す。
"""

from __future__ import annotations

from pathlib import Path

# グローバル設定（テスト時に差し替え可能）
ROOT: Path = Path.cwd()
SRC_DIR: Path = ROOT / "src"
OUTPUT_DIR: Path = ROOT / "docs" / "codemaps"

# node_modules など除外するディレクトリ名
_EXCLUDE_DIRS = frozenset(
    [
        "node_modules",
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        ".next",
        "coverage",
    ]
)

# 領域定義: キー → (表示名, パスパターン)
_AREA_PATTERNS: dict[str, tuple[str, list[str]]] = {
    "frontend": ("フロントエンド", ["components", "pages", "views", "ui", "client"]),
    "backend": ("バックエンド/API", ["api", "routes", "controllers", "server", "handlers"]),
    "database": ("データベース", ["models", "migrations", "schemas", "db", "database"]),
    "integrations": ("インテグレーション", ["integrations", "adapters", "external", "third-party"]),
    "workers": ("ワーカー", ["workers", "jobs", "tasks", "queues", "background"]),
}


class AreaInfo:
    """コードベース領域の情報を保持するクラス。"""

    def __init__(self, display_name: str) -> None:
        """初期化。

        Args:
            display_name: 領域の表示名。
        """
        self.display_name = display_name
        self.files: list[str] = []
        self.entry_points: list[str] = []
        self.directories: list[str] = []


def walk_dir(root: Path) -> list[Path]:
    """ディレクトリを再帰的に走査し、除外対象を除いたファイル一覧を返す。

    Args:
        root: 走査開始ディレクトリ。

    Returns:
        ファイルパスのリスト。node_modules 等は除外される。
    """
    results: list[Path] = []

    def _walk(current: Path) -> None:
        """ディレクトリを再帰的に走査し、ファイルを results に追加する。"""
        try:
            for entry in current.iterdir():
                if entry.is_dir():
                    if entry.name not in _EXCLUDE_DIRS:
                        _walk(entry)
                elif entry.is_file():
                    results.append(entry)
        except PermissionError:
            pass

    _walk(root)
    return results


def classify_files(files: list[Path]) -> dict[str, AreaInfo]:
    """ファイルリストを領域ごとに分類する。

    Args:
        files: 分類対象のファイルパスリスト。

    Returns:
        領域キーから AreaInfo へのマッピング。
    """
    areas: dict[str, AreaInfo] = {key: AreaInfo(display_name) for key, (display_name, _) in _AREA_PATTERNS.items()}

    for file_path in files:
        parts = file_path.parts
        # ファイルのパス構成要素と領域パターンを照合
        matched = False
        for key, (_, patterns) in _AREA_PATTERNS.items():
            for pattern in patterns:
                if any(pattern in part.lower() for part in parts):
                    area = areas[key]
                    rel = str(file_path.relative_to(ROOT)) if file_path.is_absolute() else str(file_path)
                    area.files.append(rel)
                    # ディレクトリを追加（重複排除）
                    parent = (
                        str(file_path.parent.relative_to(ROOT)) if file_path.is_absolute() else str(file_path.parent)
                    )
                    if parent not in area.directories:
                        area.directories.append(parent)
                    matched = True
                    break
            if matched:
                break

    return areas


def line_count(path: Path) -> int:
    """ファイルの行数を返す。ファイルが存在しない場合は 0 を返す。

    Args:
        path: 対象ファイルパス。

    Returns:
        行数。
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        return len(lines)
    except (OSError, UnicodeDecodeError):
        return 0


def build_tree(root: Path, prefix: str = "", _is_last: bool = True) -> str:
    """ディレクトリツリーを文字列として生成する。

    Args:
        root: ツリールートパス。
        prefix: 現在の行のインデントプレフィックス。
        _is_last: 最後の要素かどうか（内部使用）。

    Returns:
        ツリー表現の文字列。
    """
    lines: list[str] = [root.name]

    try:
        entries = sorted(root.iterdir(), key=lambda e: (e.is_file(), e.name))
        entries = [e for e in entries if e.name not in _EXCLUDE_DIRS]
    except (PermissionError, OSError):
        return "\n".join(lines)

    for i, entry in enumerate(entries):
        is_last_entry = i == len(entries) - 1
        connector = "└── " if is_last_entry else "├── "
        child_prefix = prefix + ("    " if is_last_entry else "│   ")

        if entry.is_dir():
            subtree = build_tree(entry, child_prefix, is_last_entry)
            lines.append(prefix + connector + subtree)
        else:
            lines.append(prefix + connector + entry.name)

    return "\n".join(lines)


def generate_area_doc(key: str, area: AreaInfo, _other_areas: list) -> str:
    """領域のマークダウンドキュメントを生成する。

    Args:
        key: 領域キー。
        area: 領域情報。
        _other_areas: 他の領域リスト（将来の相互参照用）。

    Returns:
        マークダウン文字列。
    """
    lines: list[str] = [
        f"# {area.display_name} コードマップ",
        "",
        "## エントリポイント",
        "",
    ]

    if area.entry_points:
        for ep in area.entry_points:
            lines.append(f"- `{ep}`")
    else:
        lines.append("_（自動検出なし）_")

    lines += [
        "",
        "## アーキテクチャ",
        "",
        "## 主要モジュール",
        "",
    ]

    for file_str in area.files:
        file_path = ROOT / file_str
        lc = line_count(file_path)
        lines.append(f"### `{file_str}` ({lc} 行)")
        lines.append("")

    return "\n".join(lines)


def generate_index(areas: dict[str, AreaInfo], _files: list) -> str:
    """コードベース全体のインデックスドキュメントを生成する。

    Args:
        areas: 領域キーから AreaInfo へのマッピング。
        _files: 全ファイルリスト（将来の統計用）。

    Returns:
        マークダウン文字列。
    """
    lines: list[str] = [
        "# コードベース概要 — codemaps インデックス",
        "",
        "## 領域",
        "",
    ]

    for key, area in areas.items():
        file_count = len(area.files)
        lines.append(f"- [{area.display_name}]({key}.md) — {file_count} ファイル")

    lines += [
        "",
        "## リポジトリ構成",
        "",
        "```",
        build_tree(ROOT),
        "```",
        "",
        "## 再生成方法",
        "",
        "```bash",
        "python3 -m bluecore.codemaps.generate_codemaps",
        "```",
        "",
    ]

    return "\n".join(lines)


def main() -> None:
    """コードマップを生成して OUTPUT_DIR に書き出す。"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    all_files = walk_dir(SRC_DIR) if SRC_DIR.exists() else walk_dir(ROOT)
    areas = classify_files(all_files)

    # 各領域のドキュメントを生成
    for key, area in areas.items():
        doc = generate_area_doc(key, area, [])
        (OUTPUT_DIR / f"{key}.md").write_text(doc, encoding="utf-8")

    # インデックスを生成
    index = generate_index(areas, all_files)
    (OUTPUT_DIR / "index.md").write_text(index, encoding="utf-8")


if __name__ == "__main__":  # pragma: no cover
    main()
