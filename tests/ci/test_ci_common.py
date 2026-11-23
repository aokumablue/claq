"""ci_common のテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from claq_dev import ci_common


def test_read_json_parses_and_wraps_decode_errors(tmp_path: Path) -> None:
    """正しい JSON は値を返し、壊れた JSON はファイル名付きの ValueError にする。"""
    path = tmp_path / "data.json"
    path.write_text('{"ok": true}', encoding="utf-8")
    assert ci_common.read_json(path) == {"ok": True}

    path.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="data.json の JSON 形式が不正です"):
        ci_common.read_json(path)


def test_non_empty_predicates() -> None:
    """空白だけの文字列と空配列を「空」と判定する。"""
    assert ci_common.is_non_empty_string("x") is True
    assert ci_common.is_non_empty_string(" ") is False
    assert ci_common.is_non_empty_string_array(["x", "y"]) is True
    assert ci_common.is_non_empty_string_array([]) is False


def test_read_json_wraps_too_deep_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """深すぎる入れ子で JSON 解析が RecursionError を送出しても、ファイル名付きの ValueError にする。"""
    path = tmp_path / "hooks.json"
    path.write_text("[[[]]]", encoding="utf-8")

    def _raise_recursion(_text: str) -> object:
        """深い入れ子の JSON 解析と同じく RecursionError を送出する。"""
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(ci_common.json, "loads", _raise_recursion)
    with pytest.raises(ValueError, match="hooks.json の JSON 形式が不正です"):
        ci_common.read_json(path)
