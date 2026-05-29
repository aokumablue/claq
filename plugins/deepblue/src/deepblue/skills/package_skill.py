#!/usr/bin/env python3
"""
スキルパッケージャー - スキルフォルダから配布用の .skill ファイルを作成する。

使い方:
    python utils/package_skill.py <path/to/skill-folder> [output-directory]

例:
    python utils/package_skill.py skills/public/my-skill
    python utils/package_skill.py skills/public/my-skill ./dist
"""

import fnmatch
import sys
import zipfile
from pathlib import Path

from .quick_validate import validate_skill

# スキルをパッケージ化する際に除外するパターン
EXCLUDE_DIRS = {"__pycache__", "node_modules"}
EXCLUDE_GLOBS = {"*.pyc"}
EXCLUDE_FILES = {".DS_Store"}
# Directories excluded only at the skill root (not when nested deeper).
ROOT_EXCLUDE_DIRS = {"evals"}


def should_exclude(rel_path: Path) -> bool:
    """パッケージ化から除外すべきパスかどうかを判定する。"""
    parts = rel_path.parts
    if any(part in EXCLUDE_DIRS for part in parts):
        return True
    # rel_path は skill_path.parent からの相対パスなので、
    # parts[0] がスキルフォルダ名、parts[1]（あれば）が最初のサブディレクトリ。
    if len(parts) > 1 and parts[1] in ROOT_EXCLUDE_DIRS:
        return True
    name = rel_path.name
    if name in EXCLUDE_FILES:
        return True
    return any(fnmatch.fnmatch(name, pat) for pat in EXCLUDE_GLOBS)


def package_skill(skill_path, output_dir=None):
    """
    スキルフォルダを .skill ファイルとしてまとめる。

    Args:
        skill_path: スキルフォルダのパス
        output_dir: .skill ファイルの出力先（未指定ならカレントディレクトリ）

    Returns:
        作成した .skill ファイルのパス。エラー時は None。
    """
    skill_path = Path(skill_path).resolve()

    # スキルフォルダの存在を確認する
    if not skill_path.exists():
        print(f"❌ エラー: スキルフォルダが見つかりません: {skill_path}")
        return None

    if not skill_path.is_dir():
        print(f"❌ エラー: パスがディレクトリではありません: {skill_path}")
        return None

    # SKILL.md の存在を確認する
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        print(f"❌ エラー: {skill_path} に SKILL.md が見つかりません")
        return None

    # パッケージ化前に検証する
    print("🔍 スキルを検証しています...")
    valid, message = validate_skill(skill_path)
    if not valid:
        print(f"❌ 検証失敗: {message}")
        print("   パッケージ化する前に検証エラーを修正してください。")
        return None
    print(f"✅ {message}\n")

    # 出力先を決める
    skill_name = skill_path.name
    if output_dir:
        output_path = Path(output_dir).resolve()
        output_path.mkdir(parents=True, exist_ok=True)
    else:
        output_path = Path.cwd()

    skill_filename = output_path / f"{skill_name}.skill"

    # .skill ファイル（zip 形式）を作成する
    try:
        with zipfile.ZipFile(skill_filename, "w", zipfile.ZIP_DEFLATED) as zipf:
            # ビルド成果物を除外しながらスキルディレクトリを走査する
            for file_path in skill_path.rglob("*"):
                if not file_path.is_file():
                    continue
                arcname = file_path.relative_to(skill_path.parent)
                if should_exclude(arcname):
                    print(f"  スキップ: {arcname}")
                    continue
                zipf.write(file_path, arcname)
                print(f"  追加: {arcname}")

        print(f"\n✅ スキルをパッケージ化しました: {skill_filename}")
        return skill_filename

    except Exception as e:
        print(f"❌ .skill ファイルの作成エラー: {e}")
        return None


def main():
    if len(sys.argv) < 2:
        print("使い方: python utils/package_skill.py <path/to/skill-folder> [output-directory]")
        print("\n例:")
        print("  python utils/package_skill.py skills/public/my-skill")
        print("  python utils/package_skill.py skills/public/my-skill ./dist")
        sys.exit(1)

    skill_path = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else None

    print(f"📦 スキルをパッケージ化しています: {skill_path}")
    if output_dir:
        print(f"   出力先ディレクトリ: {output_dir}")
    print()

    result = package_skill(skill_path, output_dir)

    if result:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
