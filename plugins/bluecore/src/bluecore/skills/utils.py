"""skill-master スクリプト共通のユーティリティ。"""

from pathlib import Path

from bluecore.lib.frontmatter import (
    MissingFrontmatterError,
    UnterminatedFrontmatterError,
    parse_yaml,
    split_frontmatter,
)


def _as_text(value: object) -> str:
    """frontmatter のスカラー値を SKILL.md 表示用の文字列へ落とす。

    ブロックスカラーの解析結果には末尾改行が残るため、必ず strip して正規化する。
    bool は Python の ``True`` / ``False`` ではなく YAML 表記へ戻す。

    Args:
        value: frontmatter から取り出した値。

    Returns:
        表示用に正規化した文字列。値が無ければ空文字列。
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def parse_skill_md(skill_path: Path) -> tuple[str, str, str]:
    """SKILL.md を解析し、(name, description, full_content) を返す。

    Args:
        skill_path: SKILL.md を含むスキルディレクトリのパス。

    Returns:
        name、description、SKILL.md の全文のタプル。frontmatter が辞書でない場合は
        name と description が空文字列になる。

    Raises:
        ValueError: frontmatter の開始または終了の ``---`` が無い場合。
    """
    content = (skill_path / "SKILL.md").read_text(encoding="utf-8")
    try:
        block = split_frontmatter(content)
    except MissingFrontmatterError as err:
        raise ValueError("SKILL.md の frontmatter がありません（先頭の --- がない）") from err
    except UnterminatedFrontmatterError as err:
        raise ValueError("SKILL.md の frontmatter がありません（末尾の --- がない）") from err
    data = parse_yaml(block, lenient=True)
    if not isinstance(data, dict):
        return "", "", content
    return _as_text(data.get("name")), _as_text(data.get("description")), content
