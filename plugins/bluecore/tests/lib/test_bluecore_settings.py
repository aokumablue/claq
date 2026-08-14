"""CLAUDE.md からの coverage ヒント抽出のテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from bluecore.lib.settings import (
    extract_coverage_hint_lines,
)


@pytest.mark.parametrize(
    ("claude_md", "expected_contains"),
    [
        # カバレッジ XX% 表記
        ("## テスト\n\nカバレッジ90%以上を維持", "カバレッジ90%以上を維持"),
        # coverage: N 表記
        ("coverage: 75", "coverage: 75"),
        # Coverage（大文字混じり）
        ("Coverage target: 70%", "Coverage target: 70%"),
        # 全角数字
        ("カバレッジ８０%以上", "カバレッジ８０%以上"),
        # 日本語文章表記
        ("カバレッジは85パーセント以上を目標とする", "カバレッジは85パーセント以上を目標とする"),
        # 複数行のうち coverage 行だけを対象にする
        ("version: 2\ncoverage: 65\nauthor: foo", "coverage: 65"),
    ],
)
def test_extract_coverage_hint_lines_returns_matching_lines(
    tmp_path: Path, claude_md: str, expected_contains: str
) -> None:
    (tmp_path / "CLAUDE.md").write_text(claude_md, encoding="utf-8")
    result = extract_coverage_hint_lines(tmp_path)
    assert expected_contains in result


def test_extract_coverage_hint_lines_returns_empty_when_no_match(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("no relevant mention here", encoding="utf-8")
    assert extract_coverage_hint_lines(tmp_path) == ""


def test_extract_coverage_hint_lines_returns_empty_when_file_missing(tmp_path: Path) -> None:
    assert extract_coverage_hint_lines(tmp_path) == ""


def test_extract_coverage_hint_lines_reads_dot_claude_claude_md(tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "CLAUDE.md").write_text("カバレッジ60%", encoding="utf-8")
    assert "カバレッジ60%" in extract_coverage_hint_lines(tmp_path)


def test_extract_coverage_hint_lines_prefers_top_level_claude_md(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("カバレッジ95%", encoding="utf-8")
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "CLAUDE.md").write_text("カバレッジ60%", encoding="utf-8")
    result = extract_coverage_hint_lines(tmp_path)
    assert "95" in result
    assert "60" not in result


def test_extract_coverage_hint_lines_returns_multiple_matching_lines(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text(
        "カバレッジ80%以上を維持\nその他の設定\nCoverage check: enabled",
        encoding="utf-8",
    )
    result = extract_coverage_hint_lines(tmp_path)
    assert "カバレッジ80%以上を維持" in result
    assert "Coverage check: enabled" in result
    assert "その他の設定" not in result

