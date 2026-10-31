"""スケルトン一式の整合（配布物に入れてはいけないものと、ファイルの間でそろえる値）のテスト。"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ._support import _OVERLAY, _PLUGIN_ROOT, _SKILL_DIR, _TEMPLATE, _config, _is_invisible

_OVERLAY_SERVER = ast.parse((_OVERLAY / "server.py").read_text(encoding="utf-8"))


def _parse(path: Path) -> ast.Module:
    """ファイル path の構文木を返す。"""
    return ast.parse(path.read_text(encoding="utf-8"))


def _constants(tree: ast.Module) -> dict[str, object]:
    """モジュールの最上位で、大文字の名前にリテラルを代入した定数を集める。"""
    return {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id.isupper()
        and isinstance(node.value, ast.Constant)
    }


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


def test_overlay_budget_fits_smoke_limit() -> None:
    """重ね合わせの server.py の予算が、smoke_config.py の上限を超えない。"""
    assert _constants(_OVERLAY_SERVER)["MAX_RESULT_CHARS"] <= _config(_OVERLAY / "smoke_config.py")["MAX_RESULT_CHARS"]


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


def _definition(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef:
    """tree の中から、名前の一致する関数かクラスの定義を取り出す。"""
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and node.name == name
    )


def _method(tree: ast.AST, class_name: str, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """tree の中のクラス class_name から、メソッド name の定義を取り出す。"""
    return next(
        node
        for node in _definition(tree, class_name).body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
    )


def _keyword_only(function: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[set[str], set[str]]:
    """関数のキーワード専用引数の（全ての名前、既定値の無い名前）を返す。"""
    names = [argument.arg for argument in function.args.kwonlyargs]
    return set(names), {name for name, default in zip(names, function.args.kw_defaults, strict=True) if default is None}


def _calls(tree: ast.AST, function: str) -> list[ast.Call]:
    """tree の中の、呼び出す式のソースが function の呼び出しを出現順に集める。"""
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call) and ast.unparse(node.func) == function]


def _smoke_calls_per_exchange() -> int:
    """smoke_check.py が 1 つの `_exchange([...])`（1 回の起動）で送るツール呼び出しの数の最大を返す。"""
    return max(
        len(_calls(exchange.args[0], "_call")) for exchange in _calls(_parse(_TEMPLATE / "smoke_check.py"), "_exchange")
    )


@pytest.mark.parametrize("skeleton", [_TEMPLATE, _OVERLAY])
def test_rate_limits_fit_smoke(skeleton: Path) -> None:
    """server.py の受信側の上限は、smoke が 1 回の起動で送る呼び出しを絞らない（smoke が上限で落ちない）。"""
    assert _constants(_parse(skeleton / "server.py"))["TOOL_CALLS_PER_WINDOW"] >= _smoke_calls_per_exchange()


@pytest.mark.parametrize("skeleton", [_TEMPLATE, _OVERLAY])
def test_guard_wiring_matches_signature(skeleton: Path) -> None:
    """server.py の install_guard と CallLimiter の呼び出しが guard.py の定義と合い、上限に定数・主体に _principal を渡す。

    server.py は mcp が無いと import できないので、呼び出しの食い違いを構文木で先に見つける。
    """
    guard = _parse(_TEMPLATE / "guard.py")
    server = _parse(skeleton / "server.py")
    accepted, required = _keyword_only(_definition(guard, "install_guard"))
    limiter_init = _method(guard, "CallLimiter", "__init__")
    (installed,) = _calls(server, "install_guard")
    keywords = {item.arg: ast.unparse(item.value) for item in installed.keywords}
    (limiter,) = _calls(installed, "CallLimiter")
    expected = {"limiter": "CallLimiter(TOOL_CALLS_PER_WINDOW, TOOL_CALL_WINDOW_SECONDS)", "principal": "_principal"}
    if skeleton == _OVERLAY:
        expected["principal_errors"] = "(FacadeError,)"
    assert (
        [ast.unparse(argument) for argument in installed.args],
        required <= set(keywords) <= accepted,
        len(limiter.args) == len(limiter_init.args.args) - 1,
        isinstance(_definition(server, "_principal"), ast.FunctionDef),
        keywords,
    ) == (["mcp"], True, True, True, expected)


def test_overlay_upstream_calls_go_through_deadline() -> None:
    """重ね合わせの上流の呼び出しは、全て _upstream から call_with_deadline を通る（締切と再試行を迂回しない）。

    ツール関数 → 本体・配送の補助 → _upstream → call_with_deadline、の呼び出しの鎖を固定し、上流の
    メソッドを直接呼ぶ箇所が残っていないことを見る。
    """
    chain = [
        ("orders_search", "_search_orders"),
        ("_search_orders", "_upstream"),
        ("orders_get", "_upstream"),
        ("orders_get", "_shipments"),
        ("_shipments", "_upstream"),
        ("_upstream", "call_with_deadline"),
    ]
    missing = [(caller, callee) for caller, callee in chain if not _calls(_overlay_function(caller), callee)]
    direct = [
        ast.unparse(node.func)
        for node in ast.walk(_OVERLAY_SERVER)
        if isinstance(node, ast.Call) and ast.unparse(node.func).startswith(("ORDERS.", "SHIPMENTS."))
    ]
    assert (missing, direct) == ([], [])
