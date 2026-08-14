"""組込フィルタ TOML のインラインケース検証。

各フィルタ TOML の ``[[cases.<filter>]]`` を読み込み、対応するフィルタ定義を
``apply_spec`` に通して ``expected`` と一致するか検証する。RTK 由来 59 フィルタの
圧縮ロジックを宣言データとして網羅テストする。
"""

from __future__ import annotations

import pytest

from bluecore.redux.engine import apply_spec
from bluecore.redux.loader import FilterCase, builtin_filter_paths, load_builtin_cases
from bluecore.redux.loader import _parse_toml as parse_toml

# 全組込フィルタ定義を name → spec で集約
_SPECS = {spec.name: spec for path in builtin_filter_paths() for spec in parse_toml(path)[0]}

_CASES = load_builtin_cases()


def test_all_builtin_filters_load() -> None:
    """組込フィルタが59個（default 含め60定義）読み込めること。"""
    paths = builtin_filter_paths()
    assert len(paths) == 60  # 59 コマンド別 + default.toml
    assert _SPECS, "フィルタが1つも読めていない"


def test_inline_cases_exist() -> None:
    """インラインケースが収集できていること。"""
    assert len(_CASES) > 0


@pytest.mark.parametrize("case", _CASES, ids=lambda c: f"{c.filter_name}:{c.name}")
def test_builtin_filter_case(case: FilterCase) -> None:
    """各フィルタのインラインケースが期待出力を生むこと。"""
    spec = _SPECS[case.filter_name]
    assert apply_spec(spec, case.input) == case.expected
