"""tests/lib/test_env_pointer*.py が共有する補助（テンプレートの場所・最小 plugin root・HOME 隔離）。"""

from __future__ import annotations

from pathlib import Path

import pytest

_ENV_TEMPLATE = Path(__file__).resolve().parents[2] / "runtime" / "env-template.sh"


def _make_plugin_root(root: Path) -> Path:
    """``runtime/claq-helpers.sh`` と ``runtime/env-template.sh`` を持つ最小 plugin root を作る。"""
    runtime_dir = root / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "claq-helpers.sh").write_text(
        'claq_run() { printf "ran:%s\\n" "$*"; }\n', encoding="utf-8"
    )
    (runtime_dir / "env-template.sh").write_text(_ENV_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``HOME`` をテスト用ディレクトリへ差し替える（state dir は ``$HOME`` 固定）。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAQ_HOME", raising=False)
    return home
