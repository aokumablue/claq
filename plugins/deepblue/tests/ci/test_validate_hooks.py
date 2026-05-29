"""devgear.ci.validate_hooks のテスト。"""

from __future__ import annotations

import json
from pathlib import Path

from devgear.ci.validate_hooks import validate_hooks


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def test_validate_hooks_skips_when_missing(tmp_path, capsys):
    """hooks.json がない場合はスキップされること。"""
    result = validate_hooks(tmp_path / "hooks.json")
    captured = capsys.readouterr()

    assert result == 0
    assert "検証をスキップします" in captured.out


def test_validate_hooks_rejects_invalid_json(tmp_path, capsys):
    """不正な JSON は即座に失敗すること。"""
    hooks_file = tmp_path / "hooks.json"
    hooks_file.write_text("{ invalid json", encoding="utf-8")

    result = validate_hooks(hooks_file)
    captured = capsys.readouterr()

    assert result == 1
    assert "JSON 形式が不正です" in captured.err


def test_validate_hooks_valid_command_hook(tmp_path, capsys):
    """有効なコマンドフックはオブジェクト形式で通ること。"""
    hooks_file = tmp_path / "hooks.json"
    write_json(
        hooks_file,
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "test",
                        "hooks": [
                            {
                                "type": "command",
                                "command": 'node -e "console.log(1+2)"',
                            }
                        ],
                    }
                ]
            }
        },
    )

    assert validate_hooks(hooks_file) == 0
    captured = capsys.readouterr()
    assert "1 個のフックマッチャーを検証しました" in captured.out


def test_validate_hooks_accepts_inline_js_strings(tmp_path, capsys):
    """旧式のインライン JS コマンドは不透明な文字列として受け入れられること。"""
    hooks_file = tmp_path / "hooks.json"
    write_json(
        hooks_file,
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "test",
                        "hooks": [
                            {
                                "type": "command",
                                "command": 'node -e "function {"',
                            }
                        ],
                    }
                ]
            }
        },
    )

    result = validate_hooks(hooks_file)
    captured = capsys.readouterr()

    assert result == 0
    assert "1 個のフックマッチャーを検証しました" in captured.out
