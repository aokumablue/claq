"""devgear テスト向けの Pytest 設定と共有フィクスチャ。

テストのインポート用に plugin root 配下の src/ が Python パスへ含まれることを保証する。
"""

from __future__ import annotations

import runpy
import sys
from functools import wraps
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "plugins" / "devgear" / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def _fresh_runpy_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """runpy.run_module の実行前に対象モジュールを外して警告を抑える。"""

    original_run_module = runpy.run_module

    @wraps(original_run_module)
    def run_module(module_name: str, *args, **kwargs):
        sys.modules.pop(module_name, None)
        return original_run_module(module_name, *args, **kwargs)

    monkeypatch.setattr(runpy, "run_module", run_module)
