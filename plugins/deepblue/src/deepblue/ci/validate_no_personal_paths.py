"""公開ドキュメントにユーザー固有の絶対パスが含まれるのを防ぐ。"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from devgear.ci.ci_common import REPO_ROOT

TARGETS = [
    "README.md",
    "skills",
    "commands",
    "agents",
    "docs",
    ".opencode/commands",
]

BLOCK_PATTERNS = [
    re.compile(r"/Users/affoon\b"),
    re.compile(r"C:\\Users\\affoon\b", re.I),
]
FILE_EXTENSIONS = re.compile(r"\.(md|json|js|ts|sh|toml|yml|yaml)$", re.I)


def _collect_files(target_path: Path, out: list[Path]) -> None:
    """指定されたパス配下のファイルを再帰的に収集する。

    Args:
        target_path: スキャンするパス
        out: ファイルパスを追加するリスト

    Returns:
        戻り値はありません（out を直接変更）。

    Raises:
        例外は発生しません。
    """
    if not target_path.exists():
        return
    if target_path.is_file():
        out.append(target_path)
        return

    for entry in target_path.iterdir():
        if entry.name in {"node_modules", ".git"}:
            continue
        _collect_files(entry, out)


def validate_no_personal_paths(root: str | Path = REPO_ROOT) -> int:
    """公開済みドキュメントに個人用のハードコードされたパスが含まれていないことを検証する。

    Args:
        root: 処理に渡す root の値です。

    Returns:
        処理結果を返します。

    Raises:
        例外は発生しません。
    """
    root_path = Path(root)
    files: list[Path] = []
    for target in TARGETS:
        _collect_files(root_path / target, files)

    failures = 0
    for file_path in files:
        if not FILE_EXTENSIONS.search(str(file_path)):
            continue
        try:
            content = file_path.read_text(encoding="utf-8")
        except OSError:
            continue

        for pattern in BLOCK_PATTERNS:
            match = pattern.search(content)
            if match:
                print(f"エラー: {file_path.relative_to(root_path)} に個人用パスが検出されました")
                failures += len(pattern.findall(content))
                break

    if failures > 0:
        return 1

    print("検証済み: 配布対象の docs/skills/commands に個人用の絶対パスはありません")
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
    parser = argparse.ArgumentParser(description="Validate docs for personal paths")
    parser.add_argument("--root", default=str(REPO_ROOT))
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
    return validate_no_personal_paths(args.root)


if __name__ == "__main__":
    raise SystemExit(main())
