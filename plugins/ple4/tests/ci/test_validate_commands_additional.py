"""validate_commands の追加テスト。"""

from __future__ import annotations

from pathlib import Path

from ple4.ci import validate_commands


def test_validate_commands_helper(tmp_path: Path) -> None:
    assert validate_commands._list_markdown_files(tmp_path / "missing") == []

    commands_dir = tmp_path / "commands"
    commands_dir.mkdir()
    (commands_dir / "keep.md").write_text("ok\n", encoding="utf-8")
    (commands_dir / "ignore.txt").write_text("ignore\n", encoding="utf-8")
    assert [path.name for path in validate_commands._list_markdown_files(commands_dir)] == ["keep.md"]
