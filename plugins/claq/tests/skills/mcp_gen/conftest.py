"""mcp-gen のテストの複数のファイルが使うフィクスチャ（smoke_check・smoke_static・facade）。"""

from __future__ import annotations

import importlib.util
import sys
import types
from collections.abc import Iterator

import pytest

from ._support import _FACADE_MODULES, _OVERLAY, _PROTECTION, _TEMPLATE, _config, _load, _load_smoke_check


@pytest.fixture
def smoke_check(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """smoke_check.py を、共通スケルトンの設定で読み込む。"""
    return _load_smoke_check(monkeypatch, _config(_TEMPLATE / "smoke_config.py"))


@pytest.fixture
def smoke_static() -> types.ModuleType:
    """smoke_static.py（サーバを起動しない検査の部品）を読み込む。"""
    return _load("mcp_gen_smoke_static", _TEMPLATE / "smoke_static.py")


@pytest.fixture(scope="module")
def facade() -> Iterator[types.SimpleNamespace]:
    """facade パッケージを読み込む（相対 import を解決するためパッケージとして）。

    既定の重ね合わせと upstream-protection-overlay の facade/ を同じパッケージの探索先にする
    （利用者が重ね合わせのファイルを facade/ へ複製した形と同じ）。
    """
    package_dir = _OVERLAY / "facade"
    spec = importlib.util.spec_from_file_location(
        "mcp_gen_facade",
        package_dir / "__init__.py",
        submodule_search_locations=[str(package_dir), str(_PROTECTION / "facade")],
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    original = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.modules["mcp_gen_facade"] = package
    try:
        spec.loader.exec_module(package)
        modules = {
            name: importlib.import_module(f"mcp_gen_facade.{name}")
            for name in _FACADE_MODULES
        }
    finally:
        sys.dont_write_bytecode = original
    yield types.SimpleNamespace(**modules)
    for name in [key for key in sys.modules if key == "mcp_gen_facade" or key.startswith("mcp_gen_facade.")]:
        del sys.modules[name]
