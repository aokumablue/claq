"""bluecore 本体がランタイムでサードパーティ依存を持たないことを機械的に保証するテスト。

`src/bluecore/**/*.py` を `ast` で走査し、全 import 文のトップレベルモジュール名から
標準ライブラリ（`sys.stdlib_module_names`）と自己参照（`bluecore`・相対 import）を
差し引いた集合が空であることを表明する。

デシジョンテーブル:
  - `import a.b.c` → トップレベル名 `a` を収集
  - `from a.b import c` → トップレベル名 `a` を収集
  - `from . import x` / `from ..y import z`（`node.level > 0`） → 自己参照なので除外
  - 関数内 import・条件付き import → `ast.walk` で収集対象に含める
  - 標準ライブラリ / `bluecore` → 許可
  - それ以外 → 違反（ファイル名・行番号・モジュール名付きで失敗させる）
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "bluecore"

ALLOWED_TOP_LEVEL_MODULES = frozenset(sys.stdlib_module_names) | {"bluecore"}


def _iter_source_files() -> list[Path]:
    """走査対象となる bluecore パッケージ配下の Python ソースを列挙する。"""
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def _iter_imports(source_file: Path) -> list[tuple[str, int]]:
    """1 ファイルから (トップレベルモジュール名, 行番号) の一覧を抽出する。

    相対 import は自己参照のため除外する。関数内や条件分岐内の import も
    `ast.walk` で漏れなく拾う。
    """
    tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
    imports: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((alias.name.split(".")[0], node.lineno))
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imports.append((node.module.split(".")[0], node.lineno))
    return imports


class TestRuntimeDependencies:
    """ランタイム依存ゼロ（標準ライブラリのみ）の静的ガード"""

    def test_source_files_are_discovered(self) -> None:
        """走査対象が 0 件だと表明が空振りするため、ソースの存在自体を確認する。"""
        assert _iter_source_files(), f"bluecore のソースが見つかりません: {PACKAGE_ROOT}"

    def test_imports_are_collected(self) -> None:
        """import 収集が機能していること（収集器の不具合による偽陰性を防ぐ）。"""
        collected = {module for path in _iter_source_files() for module, _ in _iter_imports(path)}
        assert "bluecore" in collected
        assert "pathlib" in collected

    def test_no_third_party_runtime_imports(self) -> None:
        """bluecore パッケージが標準ライブラリと自身以外を import しないこと。"""
        violations: list[str] = []
        for source_file in _iter_source_files():
            for module, lineno in _iter_imports(source_file):
                if module not in ALLOWED_TOP_LEVEL_MODULES:
                    relative = source_file.relative_to(PACKAGE_ROOT.parents[1])
                    violations.append(f"{relative}:{lineno}: {module}")

        assert not violations, (
            "bluecore はランタイム依存ゼロ（標準ライブラリのみ）である必要があります。"
            "サードパーティ import を検出しました:\n  " + "\n  ".join(sorted(violations))
        )
