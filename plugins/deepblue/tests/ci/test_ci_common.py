"""ci_common のテスト。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from devgear.ci import ci_common


def test_read_json_and_basic_predicates(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"ok": True}), encoding="utf-8")

    assert ci_common.read_json(path, "data") == {"ok": True}
    assert ci_common.is_non_empty_string("x") is True
    assert ci_common.is_non_empty_string(" ") is False
    assert ci_common.is_non_empty_string_array(["x", "y"]) is True
    assert ci_common.is_non_empty_string_array([]) is False
    assert ci_common.normalize_relative_path(r"a\b/") == "a/b"
    assert ci_common.resolve_repo_path(tmp_path, r"./docs\guide.md") == tmp_path / "docs" / "guide.md"


def test_read_json_wraps_decode_errors(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{", encoding="utf-8")

    with pytest.raises(ValueError, match="data の JSON 形式が不正です"):
        ci_common.read_json(path, "data")
