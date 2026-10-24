"""コードを書く定義の簡潔さ基準が一字一句そろっていることを固定する構造テスト。

docs/adr/code-simplicity-and-defense.md は、サブエージェントに参照先が届かないため、
簡潔さ基準をコードを書く各定義へ同じ文言で置くと決めている。写しは片方だけ直されると
ずれ、定義ごとにフォールバックや防御の判断が食い違う。正本を tdd-writer に置き、
他の定義が同じ箇条書きを連続した行として含むことを確かめる。
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TARGET_DIRS = ("agents", "skills", "commands")
_CANONICAL = Path("agents") / "tdd-writer.md"
_HEADING = "## 簡潔さ基準"
_COPIES = (
    Path("agents") / "code-refiner.md",
    Path("skills") / "quick-code" / "SKILL.md",
    Path("skills") / "quick-debug" / "SKILL.md",
    Path("skills") / "quick-refactor" / "SKILL.md",
    Path("skills") / "quick-review" / "SKILL.md",
    Path("skills") / "mcp-gen" / "SKILL.md",
    Path("skills") / "html-gen" / "SKILL.md",
    Path("skills") / "release-verify" / "SKILL.md",
)


def _lines(relative_path: Path) -> list[str]:
    """定義ファイルを行のリストで返す。"""
    return (_ROOT / relative_path).read_text(encoding="utf-8").splitlines()


def _canonical_block() -> list[str]:
    """正本の見出しの直後から空行までの箇条書きを返す。"""
    lines = _lines(_CANONICAL)
    start = lines.index(_HEADING) + 2
    return lines[start : lines.index("", start)]


def _contains(lines: list[str], block: list[str]) -> bool:
    """block が lines の中に連続した行として現れるかを返す。"""
    return any(lines[i : i + len(block)] == block for i in range(len(lines) - len(block) + 1))


@pytest.mark.parametrize("relative_path", _COPIES, ids=str)
def test_copy_contains_canonical_block(relative_path: Path) -> None:
    """各定義が正本と同じ簡潔さ基準を、一字一句同じ連続した行として含むこと。"""
    assert _contains(_lines(relative_path), _canonical_block()), (
        f"{relative_path} の簡潔さ基準が {_CANONICAL} と一致しない"
    )


def test_every_partial_copy_is_registered() -> None:
    """基準の先頭行を持つ定義が、正本と登録済みの写しに限られること。

    写しを新しい定義へ足したのに登録を忘れると、その写しだけが照合から漏れてずれる。
    """
    first_line = _canonical_block()[0]
    definitions = {path.relative_to(_ROOT) for dirname in _TARGET_DIRS for path in (_ROOT / dirname).rglob("*.md")}
    holders = {path for path in definitions if "assets" not in path.parts and first_line in _lines(path)}
    assert holders == {_CANONICAL, *_COPIES}
