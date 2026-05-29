#!/usr/bin/env python3
"""スキルの簡易バリデーションスクリプト。"""

import re
import sys
from pathlib import Path

import yaml


def validate_skill(skill_path):
    """スキルの基本的な妥当性を検証する。"""
    skill_path = Path(skill_path)

    # SKILL.md の存在を確認する
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        return False, "SKILL.md が見つかりません"

    # frontmatter を読み込んで検証する
    content = skill_md.read_text()
    if not content.startswith("---"):
        return False, "YAML frontmatter が見つかりません"

    # frontmatter を抽出する
    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return False, "frontmatter の形式が不正です"

    frontmatter_text = match.group(1)

    # YAML frontmatter を解析する
    try:
        frontmatter = yaml.safe_load(frontmatter_text)
        if not isinstance(frontmatter, dict):
            return False, "frontmatter は YAML の辞書である必要があります"
    except yaml.YAMLError as e:
        return False, f"frontmatter 内の YAML が不正です: {e}"

    # 許可するプロパティを定義する
    ALLOWED_PROPERTIES = {"name", "description", "license", "allowed-tools", "metadata", "compatibility"}

    # 想定外のプロパティを確認する（metadata 配下の入れ子キーは除外）
    unexpected_keys = set(frontmatter.keys()) - ALLOWED_PROPERTIES
    if unexpected_keys:
        return False, (
            f"SKILL.md frontmatter に想定外のキーがあります: {', '.join(sorted(unexpected_keys))}. "
            f"許可されるプロパティ: {', '.join(sorted(ALLOWED_PROPERTIES))}"
        )

    # Check required fields
    if "name" not in frontmatter:
        return False, "frontmatter に 'name' がありません"
    if "description" not in frontmatter:
        return False, "frontmatter に 'description' がありません"

    # 検証用に name を取り出す
    name = frontmatter.get("name", "")
    if not isinstance(name, str):
        return False, f"name は文字列である必要があります（{type(name).__name__} が渡されました）"
    name = name.strip()
    if name:
        # 命名規則（kebab-case: 小文字とハイフンのみ）を確認する
        if not re.match(r"^[a-z0-9-]+$", name):
            return False, f"name '{name}' は kebab-case（小文字、数字、ハイフンのみ）である必要があります"
        if name.startswith("-") or name.endswith("-") or "--" in name:
            return False, f"name '{name}' は先頭/末尾にハイフンを置けず、連続ハイフンも使えません"
        # name の長さを確認する（仕様上は最大 64 文字）
        if len(name) > 64:
            return False, f"name が長すぎます（{len(name)} 文字）。最大 64 文字です。"

    # description を取り出して検証する
    description = frontmatter.get("description", "")
    if not isinstance(description, str):
        return False, f"description は文字列である必要があります（{type(description).__name__} が渡されました）"
    description = description.strip()
    if description:
        # 山括弧を含まないことを確認する
        if "<" in description or ">" in description:
            return False, "description に山括弧（< または >）を含めることはできません"
        # description の長さを確認する（仕様上は最大 1024 文字）
        if len(description) > 1024:
            return False, f"description が長すぎます（{len(description)} 文字）。最大 1024 文字です。"

    # compatibility があれば検証する（任意）
    compatibility = frontmatter.get("compatibility", "")
    if compatibility:
        if not isinstance(compatibility, str):
            return False, f"compatibility は文字列である必要があります（{type(compatibility).__name__} が渡されました）"
        if len(compatibility) > 500:
            return False, f"compatibility が長すぎます（{len(compatibility)} 文字）。最大 500 文字です。"

    return True, "スキルは有効です"


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("使い方: python quick_validate.py <skill_directory>")
        sys.exit(1)

    valid, message = validate_skill(sys.argv[1])
    print(message)
    sys.exit(0 if valid else 1)
