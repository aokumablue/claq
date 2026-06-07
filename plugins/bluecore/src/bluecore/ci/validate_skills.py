"""キュレーション済みのスキルディレクトリを検証する。"""

from __future__ import annotations

import argparse
from pathlib import Path

from bluecore.ci.ci_common import REPO_ROOT, emit_error

DEFAULT_SKILLS_DIR = REPO_ROOT / "skills"


def validate_skills(skills_dir: str | Path = DEFAULT_SKILLS_DIR) -> int:
    """スキルディレクトリを検証し、JS バリデータと同じメッセージを表示する。

    Args:
        skills_dir: 処理に渡す skills_dir の値です。

    Returns:
        処理結果を返します。

    Raises:
        例外は発生しません。
    """
    skills_path = Path(skills_dir)
    if not skills_path.exists():
        print("キュレーション済みの skills ディレクトリ (skills/) が見つかりません。検証をスキップします")
        return 0

    entries = list(skills_path.iterdir())
    dirs = [entry for entry in entries if entry.is_dir()]
    has_errors = False
    valid_count = 0

    for directory in dirs:
        skill_md = directory / "SKILL.md"
        if not skill_md.exists():
            emit_error(f"{directory.name}/ - SKILL.md が見つかりません")
            has_errors = True
            continue

        try:
            content = skill_md.read_text(encoding="utf-8")
        except OSError as err:
            emit_error(f"{directory.name}/SKILL.md - ファイルの読み取りに失敗しました: {err}")
            has_errors = True
            continue

        if content.strip() == "":
            emit_error(f"{directory.name}/SKILL.md - ファイルが空です")
            has_errors = True
            continue

        valid_count += 1

    if has_errors:
        return 1

    print(f"{valid_count} 個のスキルディレクトリを検証しました")
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
    parser = argparse.ArgumentParser(description="Validate curated skills")
    parser.add_argument("--skills-dir", default=str(DEFAULT_SKILLS_DIR))
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
    return validate_skills(args.skills_dir)


if __name__ == "__main__":
    raise SystemExit(main())
