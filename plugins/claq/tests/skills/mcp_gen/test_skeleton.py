"""スケルトン一式の整合（配布物に入れてはいけないものと、ファイルの間でそろえる値）のテスト。"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ._support import _OVERLAY, _PLUGIN_ROOT, _SKILL_DIR, _TEMPLATE, _config, _is_invisible

_OVERLAY_SERVER = ast.parse((_OVERLAY / "server.py").read_text(encoding="utf-8"))


def _tool_functions(server: Path) -> set[str]:
    """server.py で `@mcp.tool(...)` を付けた（ツールとして公開される）関数名を集める。"""
    tree = ast.parse(server.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if isinstance(target, ast.Attribute) and target.attr == "tool":
                names.add(node.name)
    return names


@pytest.mark.parametrize("skeleton", [_TEMPLATE, _OVERLAY])
def test_smoke_config_names_exist_in_server(skeleton: Path) -> None:
    """smoke_config.py が指すツールが、対になる server.py に実在する。"""
    config = _config(skeleton / "smoke_config.py")
    tools = _tool_functions(skeleton / "server.py")
    for key in ("HAPPY_TOOL", "INVALID_TOOL", "PAGED_TOOL", "FIELDS_TOOL"):
        if config.get(key):
            assert config[key] in tools, f"{skeleton.name}/smoke_config.py の {key}={config[key]} が server.py に無い"


def test_assets_ship_no_bytecode() -> None:
    """配布物のスケルトンに `.pyc` / `__pycache__` を含めない。"""
    leftovers = [p for p in (_SKILL_DIR / "assets").rglob("*") if p.suffix == ".pyc" or p.name == "__pycache__"]
    assert leftovers == []


def test_skill_files_have_no_invisible_characters() -> None:
    """スキルの配布物とこのテスト（このパッケージの全てのファイル）に、不可視文字を直接書かない（エスケープ表記にする）。"""
    targets = [path for path in (_SKILL_DIR / "assets").rglob("*") if path.is_file()]
    targets += [*(_SKILL_DIR / "references").rglob("*.md"), _SKILL_DIR / "SKILL.md", *Path(__file__).parent.glob("*.py")]
    found = [
        f"{path.relative_to(_PLUGIN_ROOT)}:{number}: U+{ord(char):04X}"
        for path in sorted(targets)
        for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1)
        for char in line
        if _is_invisible(char)
    ]
    assert found == []


def test_overlay_paged_secret_is_fresh_each_run() -> None:
    """重ね合わせの PAGED_ENV の鍵は、公開された固定値を置かず、実行ごとに 32 バイト以上で作る。"""
    first, second = (_config(_OVERLAY / "smoke_config.py")["PAGED_ENV"]["FACADE_CURSOR_SECRET"] for _ in range(2))
    assert first != second
    assert len(first.encode("utf-8")) >= 32


def test_overlay_budget_fits_smoke_limit() -> None:
    """重ね合わせの server.py の予算が、smoke_config.py の上限を超えない。"""
    constants = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in _OVERLAY_SERVER.body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id.isupper()
        and isinstance(node.value, ast.Constant)
    }
    assert constants["MAX_RESULT_CHARS"] <= _config(_OVERLAY / "smoke_config.py")["MAX_RESULT_CHARS"]


def _overlay_function(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """重ね合わせの server.py から、名前の一致する関数（ツール関数と、その本体を持つ補助関数）の構文木を取り出す。"""
    return next(
        node for node in ast.walk(_OVERLAY_SERVER) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
    )


def _keyword_sources(node: ast.AST, keyword: str) -> list[str]:
    """node の中の呼び出しで、名前付き引数 keyword に渡した式のソースを出現順に集める。"""
    return [ast.unparse(item.value) for call in ast.walk(node) if isinstance(call, ast.Call) for item in call.keywords if item.arg == keyword]


def test_overlay_cursor_limits_share_constants() -> None:
    """重ね合わせの server.py は、cursor 引数の max_length と PageCursor の上限に、同じ定数を渡す（開けない cursor を発行しない）。"""
    cursor_argument = next(argument for argument in _overlay_function("orders_search").args.args if argument.arg == "cursor")
    page_cursors = [node for node in ast.walk(_OVERLAY_SERVER) if isinstance(node, ast.Call) and ast.unparse(node.func) == "PageCursor"]
    assert (
        _keyword_sources(cursor_argument.annotation, "max_length"),
        [_keyword_sources(call, "max_cursor_chars") for call in page_cursors],
        [_keyword_sources(call, "max_page_token_chars") for call in page_cursors],
    ) == (["MAX_CURSOR_CHARS"], [["MAX_CURSOR_CHARS"]], [["MAX_PAGE_TOKEN_CHARS"]])


@pytest.mark.parametrize("tool", ["orders_search", "orders_get"])
def test_overlay_fields_items_share_catalog_limits(tool: str) -> None:
    """fields 引数の要素（FieldPath）には、カタログが自分のパスを検査するのと同じ定数で、長さと書式の上限を付ける。"""
    fields = next(argument for argument in _overlay_function(tool).args.args if argument.arg == "fields")
    assert "list[FieldPath]" in ast.unparse(fields.annotation)
    field_path = next(
        node.value
        for node in _OVERLAY_SERVER.body
        if isinstance(node, ast.Assign) and [ast.unparse(target) for target in node.targets] == ["FieldPath"]
    )
    assert _keyword_sources(field_path, "max_length") == ["PATH_MAX_CHARS"]
    assert _keyword_sources(field_path, "pattern") == ["PATH_PATTERN"]


def test_overlay_search_leaves_paging_to_facade() -> None:
    """一覧の予算と続きの位置の決定は facade の fit_page に任せ、server.py に予算の分岐を置かない（CI で検査できる側に置く）。

    ツール関数 → 本体 → 一覧の整形 → fit_page、の呼び出しの鎖を固定する（途中が切れると fit_page が呼ばれなくなる）。
    """
    chain = ("orders_search", "_search_orders", "_page_result", "fit_page")
    for caller, callee in zip(chain, chain[1:], strict=False):
        calls = {ast.unparse(node.func) for node in ast.walk(_overlay_function(caller)) if isinstance(node, ast.Call)}
        assert callee in calls, f"{caller} が {callee} を呼んでいない"
    assert "fit_items" not in (_OVERLAY / "server.py").read_text(encoding="utf-8")
