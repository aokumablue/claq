#!/usr/bin/env python3
"""スキルの簡易バリデーションスクリプト。"""

import re
import sys
from pathlib import Path

import yaml


def _parse_frontmatter(content: str) -> tuple[bool, str, dict | None]:
    """SKILL.md テキストから frontmatter を解析し、(ok, error_msg, frontmatter_dict) を返す。"""
    if not content.startswith("---"):
        return False, "YAML frontmatter が見つかりません", None
    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return False, "frontmatter の形式が不正です", None
    try:
        frontmatter = yaml.safe_load(match.group(1))
        if not isinstance(frontmatter, dict):
            return False, "frontmatter は YAML の辞書である必要があります", None
    except yaml.YAMLError as e:
        return False, f"frontmatter 内の YAML が不正です: {e}", None
    return True, "", frontmatter


def _validate_frontmatter_keys(frontmatter: dict) -> tuple[bool, str]:
    """frontmatter のキー・name・description・compatibility を検証する。"""
    ALLOWED_PROPERTIES = {"name", "description", "license", "allowed-tools", "metadata", "compatibility"}
    unexpected_keys = set(frontmatter.keys()) - ALLOWED_PROPERTIES
    if unexpected_keys:
        return False, (
            f"SKILL.md frontmatter に想定外のキーがあります: {', '.join(sorted(unexpected_keys))}. "
            f"許可されるプロパティ: {', '.join(sorted(ALLOWED_PROPERTIES))}"
        )
    if "name" not in frontmatter:
        return False, "frontmatter に 'name' がありません"
    if "description" not in frontmatter:
        return False, "frontmatter に 'description' がありません"

    name = frontmatter.get("name", "")
    if not isinstance(name, str):
        return False, f"name は文字列である必要があります（{type(name).__name__} が渡されました）"
    name = name.strip()
    if name:
        if not re.match(r"^[a-z0-9-]+$", name):
            return False, f"name '{name}' は kebab-case（小文字、数字、ハイフンのみ）である必要があります"
        if name.startswith("-") or name.endswith("-") or "--" in name:
            return False, f"name '{name}' は先頭/末尾にハイフンを置けず、連続ハイフンも使えません"
        if len(name) > 64:
            return False, f"name が長すぎます（{len(name)} 文字）。最大 64 文字です。"

    description = frontmatter.get("description", "")
    if not isinstance(description, str):
        return False, f"description は文字列である必要があります（{type(description).__name__} が渡されました）"
    description = description.strip()
    if description:
        if "<" in description or ">" in description:
            return False, "description に山括弧（< または >）を含めることはできません"
        if len(description) > 1024:
            return False, f"description が長すぎます（{len(description)} 文字）。最大 1024 文字です。"

    compatibility = frontmatter.get("compatibility", "")
    if compatibility:
        if not isinstance(compatibility, str):
            return False, f"compatibility は文字列である必要があります（{type(compatibility).__name__} が渡されました）"
        if len(compatibility) > 500:
            return False, f"compatibility が長すぎます（{len(compatibility)} 文字）。最大 500 文字です。"

    return True, "スキルは有効です"


def validate_skill(skill_path):
    """スキルの基本的な妥当性を検証する。"""
    skill_path = Path(skill_path)
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        return False, "SKILL.md が見つかりません"

    content = skill_md.read_text()
    ok, error_msg, frontmatter = _parse_frontmatter(content)
    if not ok:
        return False, error_msg

    return _validate_frontmatter_keys(frontmatter)  # type: ignore[arg-type]


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("使い方: python quick_validate.py <skill_directory>")
        sys.exit(1)

    valid, message = validate_skill(sys.argv[1])
    print(message)
    sys.exit(0 if valid else 1)
