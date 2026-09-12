"""claq テスト向けの Pytest 設定と共有フィクスチャ。

テストのインポート用に plugin root 配下の src/ が Python パスへ含まれることを保証する。
"""

from __future__ import annotations

import os
import runpy
import sys
from collections.abc import Iterator
from functools import wraps
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "plugins" / "claq" / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def _fresh_runpy_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """runpy.run_module の実行前に対象モジュールを外して警告を抑える。"""

    original_run_module = runpy.run_module

    @wraps(original_run_module)
    def run_module(module_name: str, *args: object, **kwargs: object) -> object:
        sys.modules.pop(module_name, None)
        return original_run_module(module_name, *args, **kwargs)

    monkeypatch.setattr(runpy, "run_module", run_module)


@pytest.fixture(autouse=True)
def _clear_host_marker_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """各テスト前後で host マーカー環境変数をクリアし、テスト間の漏れ込みを防ぐ。

    実行環境の CLAUDECODE / CODEX_* / COPILOT_* / PLUGIN_DATA が漏れ込むと
    env 依存のテスト（cli_runner の CLI バイナリ判定等）の結果が変わる。
    """
    for key in list(os.environ):
        if key.startswith(("CODEX_", "COPILOT_")) or key in {
            "CLAUDECODE",
            "PLUGIN_DATA",
            "CLAUDE_PLUGIN_ROOT",
            "CLAUDE_SESSION_ID",
            "CLAUDE_PROJECT_DIR",
        }:
            monkeypatch.delenv(key, raising=False)
    yield
