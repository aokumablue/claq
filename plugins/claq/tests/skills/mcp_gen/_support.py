"""mcp-gen のテストの複数のファイルが使う補助（パス・読み込み・設定・不可視文字・時計・監査ログ）。

スケルトンは配布物として複製されるため、`__pycache__` を書かせない読み込み方をする。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import types
import unicodedata
from pathlib import Path
from typing import Any

import pytest

_PLUGIN_ROOT = Path(__file__).resolve().parents[3]
_SKILL_DIR = _PLUGIN_ROOT / "skills" / "mcp-gen"
_TEMPLATE = _SKILL_DIR / "assets" / "template"
_OVERLAY = _SKILL_DIR / "assets" / "facade-overlay"
# ソースに直接書かない文字の区分（\n と \t を除く）。書くときは "\u202e" のようなエスケープ表記にする
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cn"})
_HANGUL_FILLERS = frozenset("\u115f\u1160\u3164\uffa0")
# 重ね合わせの facade パッケージのモジュール（facade fixture が読み込み、adapters fixture が facade.* の名前でも登録する）
_FACADE_MODULES = ("errors", "cursor", "shaping", "catalog", "upstream", "audit")
_MINIMAL_SETTINGS: dict[str, Any] = {"HAPPY_TOOL": "t", "HAPPY_ARGS": {}, "HAPPY_EXPECTED_STRUCTURED": {}}
_AUDIT_SECRET = b"k" * 32


def _load(name: str, path: Path) -> types.ModuleType:
    """ファイル path をモジュール name として読み込む（`.pyc` を書かない）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    original = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = original
    return module


def _config(path: Path) -> dict[str, Any]:
    """smoke_config.py を実行し、大文字の名前だけを集めた設定値を返す。"""
    namespace: dict[str, Any] = {}
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)
    return {key: value for key, value in namespace.items() if key.isupper()}


def _load_smoke_check(
    monkeypatch: pytest.MonkeyPatch, settings: dict[str, Any], source: Path = _TEMPLATE / "smoke_check.py"
) -> types.ModuleType:
    """smoke_check.py（既定はスケルトンのもの）を、smoke_config を settings のスタブに差し替えて読み込む。

    import する smoke_static も `.pyc` を書かずに先に登録する（`モジュール.smoke_static` で触れる）。
    """
    stub = types.ModuleType("smoke_config")
    stub.__dict__.update(settings)
    monkeypatch.setitem(sys.modules, "smoke_config", stub)
    monkeypatch.setitem(sys.modules, "smoke_static", _load("mcp_gen_smoke_static", _TEMPLATE / "smoke_static.py"))
    return _load("mcp_gen_smoke_check", source)


def _is_invisible(char: str) -> bool:
    """\\n と \\t 以外の制御文字・書式文字・私用文字・未割り当て・異体字セレクタ・ハングルの埋め字か（見た目と中身が食い違い、レビューで見落とすのでソースに直接書かない）。"""
    if char in "\n\t":
        return False
    code = ord(char)
    return (
        unicodedata.category(char) in _INVISIBLE_CATEGORIES
        or 0xFE00 <= code <= 0xFE0F
        or 0xE0100 <= code <= 0xE01EF
        or char in _HANGUL_FILLERS
    )


class _Clock:
    """進め方を決められる時計。"""

    def __init__(self, now: float = 1000.0) -> None:
        """開始時刻を設定する。"""
        self.now = now

    def __call__(self) -> float:
        """現在時刻を返す。"""
        return self.now


def _audit_log(facade: types.SimpleNamespace, caplog: pytest.LogCaptureFixture, name: str) -> Any:
    """記録を caplog で捕まえる AuditLog を、テストごとに分けた名前 name の logger へ出すように作る。"""
    caplog.set_level(logging.INFO, logger=name)
    return facade.audit.AuditLog(_AUDIT_SECRET, logger=logging.getLogger(name))


def _logged(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    """捕まえた記録を（水準、文面）の一覧にする。"""
    return [(record.levelno, record.getMessage()) for record in caplog.records]
