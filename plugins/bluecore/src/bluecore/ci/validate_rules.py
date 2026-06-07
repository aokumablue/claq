"""ルール Markdown ファイルを検証する。"""

from __future__ import annotations

import argparse
import stat
from pathlib import Path

from bluecore.ci.ci_common import REPO_ROOT, emit_error

DEFAULT_RULES_DIR = REPO_ROOT / "rules"


def _collect_rule_files(directory: Path) -> list[Path]:
    """ルールディレクトリから Markdown ファイルを再帰的に収集する。

    Args:
        directory: スキャンするディレクトリパス

    Returns:
        Markdown ファイルのパスリスト

    Raises:
        例外は発生しません。
    """
    files: list[Path] = []
    if not directory.exists():
        return files

    for entry in directory.iterdir():
        if entry.is_dir():
            files.extend(_collect_rule_files(entry))
        elif entry.name.endswith(".md"):
            files.append(entry)
    return files


def validate_rules(rules_dir: str | Path = DEFAULT_RULES_DIR) -> int:
    """ルール Markdown ファイルを検証し、JS バリデータと同じメッセージを表示する。

    Args:
        rules_dir: 処理に渡す rules_dir の値です。

    Returns:
        処理結果を返します。

    Raises:
        例外は発生しません。
    """
    directory = Path(rules_dir)
    rules_path = directory
    if not directory.exists():
        print("rules ディレクトリが見つかりません。検証をスキップします")
        return 0

    files = _collect_rule_files(rules_path)
    has_errors = False
    validated_count = 0

    for file_path in files:
        try:
            file_stat = file_path.stat()
            if not stat.S_ISREG(file_stat.st_mode):
                continue
            content = file_path.read_text(encoding="utf-8")
            if content.strip() == "":
                emit_error(f"{file_path.relative_to(rules_path)} - ルールファイルが空です")
                has_errors = True
                continue
            validated_count += 1
        except OSError as err:
            emit_error(f"{file_path.relative_to(rules_path)} - ファイルの読み取りに失敗しました: {err}")
            has_errors = True

    if has_errors:
        return 1

    print(f"{validated_count} 個のルールファイルを検証しました")
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
    parser = argparse.ArgumentParser(description="Validate rule markdown files")
    parser.add_argument("--rules-dir", default=str(DEFAULT_RULES_DIR))
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
    return validate_rules(args.rules_dir)


if __name__ == "__main__":
    raise SystemExit(main())
