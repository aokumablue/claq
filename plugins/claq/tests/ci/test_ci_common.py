"""ci_common のテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from claq.ci import ci_common


def test_read_json_parses_and_wraps_decode_errors(tmp_path: Path) -> None:
    """正しい JSON は値を返し、壊れた JSON はラベル付きの ValueError にする。"""
    path = tmp_path / "data.json"
    path.write_text('{"ok": true}', encoding="utf-8")
    assert ci_common.read_json(path, "data") == {"ok": True}

    path.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="data の JSON 形式が不正です"):
        ci_common.read_json(path, "data")


def test_non_empty_predicates() -> None:
    """空白だけの文字列と空配列を「空」と判定する。"""
    assert ci_common.is_non_empty_string("x") is True
    assert ci_common.is_non_empty_string(" ") is False
    assert ci_common.is_non_empty_string_array(["x", "y"]) is True
    assert ci_common.is_non_empty_string_array([]) is False
