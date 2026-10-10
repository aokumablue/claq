"""キュレーション済みのスキルディレクトリを検証する。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from claq.ci.ci_common import (
    REPO_ROOT,
    emit_error,
    extract_frontmatter,
    parse_validator_args,
    read_or_report,
    report_errors,
    report_missing,
)

DEFAULT_SKILLS_DIR = REPO_ROOT / "skills"

# SKILL.md の frontmatter に必須のフィールド。`name` が無いとホストが登録
# できず、`description` が無いと「いつ呼ぶか」を判断できない。
_REQUIRED_FRONTMATTER_FIELDS = ("name", "description")

# 真偽値でなければ意味を成さない frontmatter フィールド。ホストは真の bool しか
# 見ないので、`"true"`（引用符付き）/ `yes` / `1` は**宣言したつもりで効かない**。
# 自前パーサはそれらを str / int として読むため、型を見ない限り素通りする。
# 実害: `disable-model-invocation` が効かなければ、スラッシュ起動専用にしたはずの
# skill がモデルの推測で自動発火する。`user-invocable` も同じ形で裏返る。
_BOOLEAN_FRONTMATTER_FIELDS = ("user-invocable", "disable-model-invocation")

# 綴りを間違えても自前パーサは「別のキー」として黙って受理する。ハイフンと
# アンダースコアの取り違えが唯一現実的な誤りなので、その形だけを名指しで拒否する。
_KNOWN_FRONTMATTER_FIELDS = frozenset(
    {"name", "description", "context", *_BOOLEAN_FRONTMATTER_FIELDS}
)


def _frontmatter_field_errors(label: str, frontmatter: dict[str, object]) -> Iterator[str]:
    """ハイフン／アンダースコアの綴り違いと、真偽値フィールドの型の誤りを返す。"""
    for key in frontmatter:
        swapped = key.replace("_", "-")
        if swapped != key and swapped in _KNOWN_FRONTMATTER_FIELDS:
            yield f"{label} - frontmatter の {key} は {swapped} の綴り違い（黙って無視される）"
    for field in _BOOLEAN_FRONTMATTER_FIELDS:
        if field in frontmatter and not isinstance(frontmatter[field], bool):
            yield (
                f"{label} - frontmatter の {field} が真偽値でない（{frontmatter[field]!r}）。"
                "引用符なしの true / false で書く（文字列や数値はホストに効かない）"
            )


def _validate_skill_dir(directory: Path) -> bool:
    """1 つのスキルディレクトリの SKILL.md を検証し、誤りがあれば True を返す。"""
    label = f"{directory.name}/SKILL.md"
    skill_md = directory / "SKILL.md"
    if not skill_md.exists():
        emit_error(f"{directory.name}/ - SKILL.md が見つかりません")
        return True
    content = read_or_report(skill_md, label)
    if content is None:
        return True
    if not content.strip():
        emit_error(f"{label} - ファイルが空です")
        return True
    # agents と同じく frontmatter を必須にする。名前と説明が無い
    # SKILL.md はホストが登録できず、あるいは「いつ呼ぶか」を判断できない。
    frontmatter = extract_frontmatter(content)
    if frontmatter is None:
        emit_error(f"{label} - フロントマターがありません")
        return True
    missing = [field for field in _REQUIRED_FRONTMATTER_FIELDS if not frontmatter.get(field)]
    if missing:
        emit_error(f"{label} - frontmatter に {' '.join(missing)} がありません")
        return True
    return report_errors(_frontmatter_field_errors(label, frontmatter))


def validate_skills(skills_dir: str | Path = DEFAULT_SKILLS_DIR, *, optional: bool = False) -> int:
    """スキルディレクトリを検証し、JS バリデータと同じメッセージを表示する。

    Args:
        skills_dir: 検証するスキルディレクトリ。
        optional: True なら対象パスが存在しない場合に検証をスキップして 0 を返す。

    Returns:
        終了コード（0 = 合格、1 = 不合格）。
    """
    skills_path = Path(skills_dir)
    if not skills_path.exists():
        return report_missing("skills ディレクトリ", skills_path, optional)
    dirs = [entry for entry in skills_path.iterdir() if entry.is_dir()]
    # 全ディレクトリの誤りを出し切るため、短絡させずに検証する。
    if [directory for directory in dirs if _validate_skill_dir(directory)]:
        return 1
    print(f"{len(dirs)} 個のスキルディレクトリを検証しました")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI のエントリポイント。"""
    args = parse_validator_args("Validate curated skills", argv, skills_dir=DEFAULT_SKILLS_DIR)
    return validate_skills(args.skills_dir, optional=args.optional)


if __name__ == "__main__":
    raise SystemExit(main())
