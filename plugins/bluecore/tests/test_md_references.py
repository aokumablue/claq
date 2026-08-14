"""agents/skills/commands md 内の相対 .md 参照が実在することを検証する構造テスト。

バッククォートで囲まれた `../` または `references/` 始まりの .md 参照を、
記載ファイル自身の場所を基準に解決して存在を確認する。
スキル本文の参照切れは実行時の Read 失敗（プロンプト破損）に直結するため CI で検出する。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TARGET_DIRS = ("agents", "skills", "commands")
_REF_PATTERN = re.compile(r"`((?:\.\./|references/)[^`\n]+?\.md)`")


def _iter_md_files() -> Iterator[Path]:
    """検証対象の md ファイルを列挙する。"""
    for dirname in _TARGET_DIRS:
        yield from sorted((_ROOT / dirname).rglob("*.md"))


def test_relative_md_references_resolve() -> None:
    """バッククォート内の相対 .md 参照がすべて実在ファイルに解決されること。"""
    broken: list[str] = []
    for md_file in _iter_md_files():
        text = md_file.read_text(encoding="utf-8")
        for match in _REF_PATTERN.finditer(text):
            target = (md_file.parent / match.group(1)).resolve()
            if not target.is_file():
                broken.append(f"{md_file.relative_to(_ROOT)}: `{match.group(1)}`")
    assert broken == [], "解決できない md 参照:\n" + "\n".join(broken)


def test_dead_code_cleaner_and_harness_tuner_have_missing_input_fail_contract() -> None:
    """入力不足時に即 FAIL する契約が 2 エージェント定義に明示されていること。"""
    cleaner = (_ROOT / "agents" / "dead-code-cleaner.md").read_text(encoding="utf-8")
    tuner = (_ROOT / "agents" / "harness-tuner.md").read_text(encoding="utf-8")

    assert "FAIL" in cleaner
    assert "対象パスまたは diff" in cleaner
    assert "リポジトリ全体の探索は行わない" in cleaner

    assert "FAIL" in tuner
    assert "baseline JSON が無い場合は直ちに **FAIL**" in tuner
    assert "自分で1回だけ採取" not in tuner
    assert "自己収集" not in tuner
