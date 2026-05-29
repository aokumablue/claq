"""validate_commands と validate_rules の追加テスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from devgear.ci import validate_commands, validate_rules


def test_validate_commands_helper_and_rules_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert validate_commands._list_markdown_files(tmp_path / "missing") == []

    commands_dir = tmp_path / "commands"
    commands_dir.mkdir()
    (commands_dir / "keep.md").write_text("ok\n", encoding="utf-8")
    (commands_dir / "ignore.txt").write_text("ignore\n", encoding="utf-8")
    assert [path.name for path in validate_commands._list_markdown_files(commands_dir)] == ["keep.md"]

    rules_dir = tmp_path / "rules"
    nested = rules_dir / "nested"
    nested.mkdir(parents=True)
    file_path = nested / "rule.md"
    file_path.write_text("rule\n", encoding="utf-8")
    non_regular = nested / "link.md"
    non_regular.write_text("link\n", encoding="utf-8")

    original_stat = Path.stat

    def fake_stat(self: Path, *args, **kwargs):  # noqa: ANN001
        if self == non_regular:
            return type("Stat", (), {"st_mode": 0})()
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fake_stat)
    files = validate_rules._collect_rule_files(rules_dir)
    assert file_path in files
    assert non_regular in files
    assert validate_rules._collect_rule_files(tmp_path / "missing-rules") == []
    assert validate_rules.validate_rules(rules_dir) == 0
