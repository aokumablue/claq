"""claq.hooks.commit_message のコミットメッセージ検証のテスト。"""

from __future__ import annotations


def test_validate_commit_message_lowercase_no_period() -> None:
    """conventional commit で小文字始まり・末尾ピリオド無しなら指摘なし。"""
    from claq.hooks.commit_message import validate_commit_message

    result = validate_commit_message('git commit -m "feat: add new feature"')
    assert result is not None
    types = {i["type"] for i in result["issues"]}
    assert "capitalization" not in types
    assert "punctuation" not in types
