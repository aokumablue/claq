"""_paths.py の二重管理コピーが乖離していないことを検証するパリティテスト。

deepblue.mem._paths と model_build._paths は配布独立のため import 関係を持たず、
同一仕様のコピーを維持する設計（各ファイル冒頭の docstring 参照）。
モジュール docstring 以外の関数・定数本体が両ファイルで一致することを保証し、
片方のみ更新する事故を CI で検出する。
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MEM_PATHS = _ROOT / "src" / "deepblue" / "mem" / "_paths.py"
_MODELBUILD_PATHS = _ROOT / "src" / "model_build" / "_paths.py"


def _body_without_module_docstring(source: str) -> str:
    """ソースを ast.parse し、モジュール docstring を除いた本体を正規化文字列で返す。

    ast.unparse による再生成で空白・コメントの差異を吸収し、関数・定数の
    構造的等価性のみを比較対象とする。先頭がモジュール docstring の場合だけ除外する。
    """
    module = ast.parse(source)
    if ast.get_docstring(module, clean=False) is not None:
        module.body = module.body[1:]
    return ast.unparse(module)


class TestPathsParity:
    """二重管理された _paths.py のパリティ検証。"""

    def test_both_files_exist(self) -> None:
        """比較対象の 2 ファイルが両方存在する。"""
        assert _MEM_PATHS.is_file()
        assert _MODELBUILD_PATHS.is_file()

    def test_module_docstrings_differ(self) -> None:
        """パッケージ名を含むモジュール docstring は両ファイルで異なる（前提の確認）。"""
        mem_doc = ast.get_docstring(ast.parse(_MEM_PATHS.read_text(encoding="utf-8")))
        modelbuild_doc = ast.get_docstring(ast.parse(_MODELBUILD_PATHS.read_text(encoding="utf-8")))
        assert mem_doc is not None
        assert modelbuild_doc is not None
        assert mem_doc != modelbuild_doc

    def test_bodies_match_excluding_module_docstring(self) -> None:
        """モジュール docstring を除いた関数・定数本体が両ファイルで完全一致する。"""
        mem_body = _body_without_module_docstring(_MEM_PATHS.read_text(encoding="utf-8"))
        modelbuild_body = _body_without_module_docstring(_MODELBUILD_PATHS.read_text(encoding="utf-8"))
        assert mem_body == modelbuild_body, (
            "deepblue.mem._paths と model_build._paths の本体が乖離しています。"
            "どちらか一方のみを更新していないか確認してください。"
        )
