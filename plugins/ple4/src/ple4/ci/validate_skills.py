"""キュレーション済みのスキルディレクトリを検証する。"""

from __future__ import annotations

import argparse
from pathlib import Path

from ple4.ci.ci_common import REPO_ROOT, emit_error, extract_frontmatter

DEFAULT_SKILLS_DIR = REPO_ROOT / "skills"

# SKILL.md の frontmatter に必須のフィールド。`name` が無いとホストが登録
# できず、`description` が無いと「いつ呼ぶか」を判断できない。
_REQUIRED_FRONTMATTER_FIELDS = ("name", "description")


def validate_skills(skills_dir: str | Path = DEFAULT_SKILLS_DIR, *, optional: bool = False) -> int:
    """スキルディレクトリを検証し、JS バリデータと同じメッセージを表示する。

    Args:
        skills_dir: 処理に渡す skills_dir の値です。
        optional: True なら対象パスが存在しない場合に検証をスキップして 0 を返す。
            既定の False では欠落を失敗として扱う（宣言がまるごと失われた破損を
            成功と報告しないため。F-03）。

    Returns:
        処理結果を返します。

    Raises:
        例外は発生しません。
    """
    skills_path = Path(skills_dir)
    if not skills_path.exists():
        if optional:
            print("skills ディレクトリが見つかりません。--optional 指定のため検証をスキップします")
            return 0
        emit_error(f"skills ディレクトリが見つかりません: {skills_path}")
        return 1

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

        # agents と同じく frontmatter を必須にする（F-03）。名前と説明が無い
        # SKILL.md はホストが登録できず、あるいは「いつ呼ぶか」を判断できない。
        # 以前は「存在する・読める・空でない」しか見ておらず、agents 側だけが
        # frontmatter を検査するという非対称が残っていた。
        frontmatter = extract_frontmatter(content)
        if frontmatter is None:
            emit_error(f"{directory.name}/SKILL.md - フロントマターがありません")
            has_errors = True
            continue

        missing = [field for field in _REQUIRED_FRONTMATTER_FIELDS if not frontmatter.get(field)]
        if missing:
            emit_error(f"{directory.name}/SKILL.md - frontmatter に {' '.join(missing)} がありません")
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
    parser.add_argument(
        "--optional",
        action="store_true",
        help="対象パスが存在しない場合に失敗ではなくスキップする",
    )
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
    return validate_skills(args.skills_dir, optional=args.optional)


if __name__ == "__main__":
    raise SystemExit(main())
