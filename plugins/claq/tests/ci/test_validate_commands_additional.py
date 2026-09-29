"""validate_commands の追加テスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from claq.ci import validate_commands


def test_validate_commands_helper(tmp_path: Path) -> None:
    assert validate_commands._list_markdown_files(tmp_path / "missing") == []

    commands_dir = tmp_path / "commands"
    commands_dir.mkdir()
    (commands_dir / "keep.md").write_text("ok\n", encoding="utf-8")
    (commands_dir / "ignore.txt").write_text("ignore\n", encoding="utf-8")
    assert [path.name for path in validate_commands._list_markdown_files(commands_dir)] == ["keep.md"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("`/plain`", ["plain"]),
        ("`/with-hyphen`", ["with-hyphen"]),
        ("`/instinct promote`", ["instinct"]),
        ("`/instinct promote <key>`", ["instinct"]),
        ("`/first arg` と `/second`", ["first", "second"]),
        # 引数部は空白始まりに限るので、パス表記は従来どおり一致しない。
        ("`/usr/bin/env python`", []),
        ("`/tmp/x`", []),
    ],
)
def test_command_reference_pattern_catches_arguments(text: str, expected: list[str]) -> None:
    """引数付きのコマンド参照も名前を取り出せること。

    閉じバッククォートが名前の直後に来る形だけを見ていた頃は、実測で
    ``/does-not-exist arg`` が素通りし、同じ行の ``/also-missing`` だけが
    エラーになっていた。
    """
    assert [m.group(1) for m in validate_commands._COMMAND_REFERENCE_PATTERN.finditer(text)] == expected


def test_validate_commands_flags_missing_command_with_arguments(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """引数付きで書かれた存在しないコマンド参照が検出されること。"""
    root = tmp_path
    commands_dir = root / "commands"
    commands_dir.mkdir()
    (root / "agents").mkdir()
    (root / "skills").mkdir()
    (commands_dir / "guide.md").write_text("Run `/does-not-exist arg` first.\n", encoding="utf-8")

    assert validate_commands.validate_commands(root, commands_dir, root / "agents", root / "skills") == 1
    assert "存在しないコマンド /does-not-exist" in capsys.readouterr().err
