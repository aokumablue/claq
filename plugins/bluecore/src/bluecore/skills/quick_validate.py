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


def _validate_name(name_raw: object) -> tuple[bool, str]:
    """frontmatter の name フィールドを検証する。空文字列は OK、非文字列・形式違反は NG。"""
    if not isinstance(name_raw, str):
        return False, f"name は文字列である必要があります（{type(name_raw).__name__} が渡されました）"
    name = name_raw.strip()
    if not name:
        return True, ""
    if not re.match(r"^[a-z0-9-]+$", name):
        return False, f"name '{name}' は kebab-case（小文字、数字、ハイフンのみ）である必要があります"
    if name.startswith("-") or name.endswith("-") or "--" in name:
        return False, f"name '{name}' は先頭/末尾にハイフンを置けず、連続ハイフンも使えません"
    if len(name) > 64:
        return False, f"name が長すぎます（{len(name)} 文字）。最大 64 文字です。"
    return True, ""


def _validate_description(desc_raw: object) -> tuple[bool, str]:
    """frontmatter の description フィールドを検証する。空文字列は OK、非文字列・形式違反は NG。"""
    if not isinstance(desc_raw, str):
        return False, f"description は文字列である必要があります（{type(desc_raw).__name__} が渡されました）"
    desc = desc_raw.strip()
    if not desc:
        return True, ""
    if "<" in desc or ">" in desc:
        return False, "description に山括弧（< または >）を含めることはできません"
    if len(desc) > 1024:
        return False, f"description が長すぎます（{len(desc)} 文字）。最大 1024 文字です。"
    return True, ""


def _validate_compatibility(compat_raw: object) -> tuple[bool, str]:
    """frontmatter の compatibility フィールドを検証する。空値は OK、非文字列・長すぎは NG。"""
    if not compat_raw:
        return True, ""
    if not isinstance(compat_raw, str):
        return False, f"compatibility は文字列である必要があります（{type(compat_raw).__name__} が渡されました）"
    if len(compat_raw) > 500:
        return False, f"compatibility が長すぎます（{len(compat_raw)} 文字）。最大 500 文字です。"
    return True, ""


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

    ok, msg = _validate_name(frontmatter.get("name", ""))
    if not ok:
        return False, msg

    ok, msg = _validate_description(frontmatter.get("description", ""))
    if not ok:
        return False, msg

    ok, msg = _validate_compatibility(frontmatter.get("compatibility", ""))
    if not ok:
        return False, msg

    return True, "スキルは有効です"


def validate_skill(skill_path):
    """スキルの基本的な妥当性を検証する。"""
    skill_path = Path(skill_path)
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        return False, "SKILL.md が見つかりません"

    content = skill_md.read_text(encoding="utf-8")
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
