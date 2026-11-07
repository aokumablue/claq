"""claq テスト向けの Pytest 設定と共有フィクスチャ。

テストのインポート用に plugin root 配下の src/ が Python パスへ含まれることを保証し、
利用者の git 設定と host マーカー環境変数を全テストから切り離す。
"""

from __future__ import annotations

import os
import runpy
import sys
from collections.abc import Collection
from functools import wraps
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "plugins" / "claq" / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

_GIT_CONFIG_ENV_NAMES = frozenset({"GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"})
_GIT_CONFIG_ENV_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
_HOST_MARKER_ENV_NAMES = frozenset(
    {"CLAUDECODE", "PLUGIN_DATA", "CLAUDE_PLUGIN_ROOT", "CLAUDE_SESSION_ID", "CLAUDE_PROJECT_DIR"}
)
_HOST_MARKER_ENV_PREFIXES = ("CODEX_", "COPILOT_")


def _delete_env(monkeypatch: pytest.MonkeyPatch, names: Collection[str], prefixes: tuple[str, ...]) -> None:
    """names に一致する、または prefixes で始まる環境変数をテストの間だけ消す。"""
    for key in list(os.environ):
        if key in names or key.startswith(prefixes):
            monkeypatch.delenv(key)


@pytest.fixture(autouse=True)
def _fresh_runpy_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """runpy.run_module の実行前に対象モジュールを外して警告を抑える。"""
    original_run_module = runpy.run_module

    @wraps(original_run_module)
    def run_module(module_name: str, *args: object, **kwargs: object) -> object:
        """対象モジュールを sys.modules から外してから元の run_module を呼ぶ。"""
        sys.modules.pop(module_name, None)
        return original_run_module(module_name, *args, **kwargs)

    monkeypatch.setattr(runpy, "run_module", run_module)


@pytest.fixture(autouse=True)
def _isolate_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """利用者の git 設定（ファイルと、GIT_CONFIG_COUNT 等の環境変数で渡す設定）をテストから切り離す。

    `url.<base>.insteadOf` が環境変数で渡されると `git remote get-url` の結果が書き換わり、
    remote を読むテストが利用者の環境で変わる。
    """
    _delete_env(monkeypatch, _GIT_CONFIG_ENV_NAMES, _GIT_CONFIG_ENV_PREFIXES)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture(autouse=True)
def _clear_host_marker_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """各テスト前後で host マーカー環境変数をクリアし、テスト間の漏れ込みを防ぐ。

    実行環境の CLAUDECODE / CODEX_* / COPILOT_* / PLUGIN_DATA が漏れ込むと
    env 依存のテスト（cli_runner の CLI バイナリ判定等）の結果が変わる。
    """
    _delete_env(monkeypatch, _HOST_MARKER_ENV_NAMES, _HOST_MARKER_ENV_PREFIXES)
