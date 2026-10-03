"""mcp-gen スケルトンの契約テスト（SDK の `mcp` を使わずに検査できる部分）。

スケルトンは配布物として複製されるため、`__pycache__` を書かせない読み込み方をする。
`mcp` を要する実起動の検査は、生成先で `smoke_check.py` が担う。
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import re
import sys
import types
from pathlib import Path
from typing import Any

import pytest

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_SKILL_DIR = _PLUGIN_ROOT / "skills" / "mcp-gen"
_TEMPLATE = _SKILL_DIR / "assets" / "template"
_SPEC_REFERENCE = _SKILL_DIR / "references" / "spec-2026-07-28.md"


def _load(name: str, path: Path) -> types.ModuleType:
    """ファイルをモジュールとして読み込む（`.pyc` を書かない）。

    Args:
        name: 読み込み後のモジュール名。
        path: 読み込むファイル。

    Returns:
        読み込んだモジュール。
    """
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
    """smoke_config.py を実行して設定値を辞書で返す。

    Args:
        path: smoke_config.py のパス。

    Returns:
        大文字の名前だけを集めた設定値。
    """
    namespace: dict[str, Any] = {}
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)
    return {key: value for key, value in namespace.items() if key.isupper()}


def _tool_functions(server: Path) -> set[str]:
    """server.py で `@mcp.tool(...)` を付けた関数名を集める。

    Args:
        server: server.py のパス。

    Returns:
        ツールとして公開される関数名。
    """
    tree = ast.parse(server.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for decorator in node.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                if isinstance(target, ast.Attribute) and target.attr == "tool":
                    names.add(node.name)
    return names


@pytest.fixture(scope="module")
def guard() -> types.ModuleType:
    """共通スケルトンの guard.py を読み込む。"""
    return _load("mcp_gen_guard", _TEMPLATE / "guard.py")


@pytest.fixture
def smoke_check(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """smoke_check.py を、設定モジュールを差し替えて読み込む。"""
    stub = types.ModuleType("smoke_config")
    stub.__dict__.update(_config(_TEMPLATE / "smoke_config.py"))
    monkeypatch.setitem(sys.modules, "smoke_config", stub)
    return _load("mcp_gen_smoke_check", _TEMPLATE / "smoke_check.py")


class TestNormalizeInputSchema:
    """公開する inputSchema の整形。"""

    def test_closes_root_and_collapses_optional(self, guard: types.ModuleType) -> None:
        """root を閉じ、`anyOf` と `null` の組を外し、`default: null` と `title` を除く。"""
        schema = {
            "type": "object",
            "title": "toolArguments",
            "properties": {
                "query": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Query"},
                "limit": {"type": "integer", "default": 10, "title": "Limit"},
            },
        }
        assert guard.normalize_input_schema(schema) == {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 10}},
            "additionalProperties": False,
        }

    def test_keeps_argument_named_title(self, guard: types.ModuleType) -> None:
        """`title` という名前の引数は消さない（消すのは注釈としての `title` だけ）。"""
        schema = {"type": "object", "properties": {"title": {"type": "string", "title": "Title"}}}
        assert guard.normalize_input_schema(schema)["properties"] == {"title": {"type": "string"}}

    def test_recurses_into_items_and_defs(self, guard: types.ModuleType) -> None:
        """配列の要素と `$defs` の中も整える。"""
        schema = {
            "type": "object",
            "properties": {
                "status": {
                    "anyOf": [{"type": "array", "items": {"type": "string", "title": "S"}}, {"type": "null"}],
                    "default": None,
                },
            },
            "$defs": {"Range": {"title": "Range", "type": "object", "properties": {"start": {"title": "Start"}}}},
        }
        normalized = guard.normalize_input_schema(schema)
        assert normalized["properties"]["status"] == {"type": "array", "items": {"type": "string"}}
        assert normalized["$defs"] == {"Range": {"type": "object", "properties": {"start": {}}}}

    def test_keeps_explicit_closure_and_other_anyof(self, guard: types.ModuleType) -> None:
        """既に指定された `additionalProperties` と、null を含まない `anyOf` には触れない。"""
        union = {"anyOf": [{"type": "string"}, {"type": "integer"}]}
        schema = {"type": "object", "additionalProperties": True, "properties": {"value": dict(union)}}
        normalized = guard.normalize_input_schema(schema)
        assert normalized["additionalProperties"] is True
        assert normalized["properties"]["value"] == union

    def test_does_not_mutate_input(self, guard: types.ModuleType) -> None:
        """元のスキーマは書き換えない。"""
        schema = {"type": "object", "title": "x", "properties": {"a": {"title": "A", "type": "string"}}}
        guard.normalize_input_schema(schema)
        assert schema == {"type": "object", "title": "x", "properties": {"a": {"title": "A", "type": "string"}}}


class TestCompactText:
    """structuredContent の写しの text を詰める。"""

    def test_compacts_mirrored_json(self, guard: types.ModuleType) -> None:
        """字下げ付きの写しを、詰めた JSON（日本語はそのまま）に置き換える。"""
        result = {
            "structuredContent": {"label": "田中商事", "n": [1, 2]},
            "content": [{"type": "text", "text": '{\n  "label": "田中商事",\n  "n": [\n    1,\n    2\n  ]\n}'}],
        }
        guard.compact_text(result)
        assert result["content"][0]["text"] == '{"label":"田中商事","n":[1,2]}'

    @pytest.mark.parametrize(
        "result",
        [
            {"structuredContent": {"a": 1}, "content": [{"type": "text", "text": "要約: a は 1"}]},
            {"structuredContent": {"a": 1}, "content": [{"type": "text", "text": '{"a": 2}'}]},
            {"structuredContent": {"a": 1}, "content": [{"type": "text", "text": "x"}, {"type": "text", "text": "y"}]},
            {"structuredContent": {"a": 1}, "content": [{"type": "image", "data": "", "mimeType": "image/png"}]},
            {"content": [{"type": "text", "text": "{}"}]},
            {"structuredContent": {"a": 1}, "content": ["not a block"]},
        ],
    )
    def test_leaves_other_results(self, guard: types.ModuleType, result: dict[str, Any]) -> None:
        """写しではない text・複数ブロック・text 以外・構造化結果なしには触れない。"""
        before = repr(result)
        guard.compact_text(result)
        assert repr(result) == before


class TestRejection:
    """未知の引数の判定と拒否の形。"""

    def test_unknown_arguments_sorted(self, guard: types.ModuleType) -> None:
        """許されていない引数名を名前順で返す。"""
        assert guard.unknown_arguments({"b": 1, "stauts": 2, "a": 3}, ["a", "b"]) == ["stauts"]

    def test_rejection_is_tool_execution_error(self, guard: types.ModuleType) -> None:
        """プロトコルエラーではなく、名指しした `isError` の結果になる。"""
        rejected = guard.rejection(["stauts"], ["status"], server_name="s", server_version="1")
        assert rejected["isError"] is True
        assert rejected["resultType"] == "complete"
        assert "stauts" in rejected["content"][0]["text"]
        assert "status" in rejected["content"][0]["text"]
        assert rejected["_meta"]["io.modelcontextprotocol/serverInfo"] == {"name": "s", "version": "1"}

    def test_rejection_without_arguments(self, guard: types.ModuleType) -> None:
        """引数を取らないツールでは「使える引数: なし」と示す。"""
        assert "なし" in guard.rejection(["x"], [], server_name="s", server_version="1")["content"][0]["text"]


class _FakeServer:
    """guard.install が使う最小限のサーバ面。"""

    def __init__(self) -> None:
        """登録済みのツールを 1 つ持つ。"""
        self.middleware: list[Any] = []
        self.name = "fake"
        self.version = "0.0.1"

    async def list_tools(self) -> list[Any]:
        """公開中のツール定義を返す。"""
        return [types.SimpleNamespace(name="orders_search", input_schema={"properties": {"status": {}}})]


class TestInstalledGuard:
    """登録した middleware の振る舞い。"""

    @staticmethod
    def _run(guard: types.ModuleType, method: str, params: Any, result: Any) -> tuple[Any, list[Any]]:
        """middleware を 1 回通し、結果と後続の呼び出し記録を返す。"""
        server = _FakeServer()
        guard.install(server)
        calls: list[Any] = []

        async def call_next(ctx: Any) -> Any:
            """後続の処理の代わりに、渡された文脈を記録して固定の結果を返す。"""
            calls.append(ctx)
            return result

        middleware = server.middleware[0]
        outcome = asyncio.run(middleware(types.SimpleNamespace(method=method, params=params), call_next))
        return outcome, calls

    def test_rejects_unknown_argument_before_handler(self, guard: types.ModuleType) -> None:
        """未知の引数は後続へ渡さずに拒否する。"""
        params = {"name": "orders_search", "arguments": {"stauts": "open"}}
        outcome, calls = self._run(guard, "tools/call", params, {"content": []})
        assert calls == []
        assert outcome["isError"] is True

    @pytest.mark.parametrize(
        "params",
        [
            {"name": "orders_search", "arguments": {"status": "open"}},
            {"name": "unknown_tool", "arguments": {"x": 1}},
            {"name": "orders_search", "arguments": ["not", "a", "mapping"]},
            None,
        ],
    )
    def test_passes_through_otherwise(self, guard: types.ModuleType, params: Any) -> None:
        """既知の引数・未知のツール・object でない引数・params なしは後続へ渡す。"""
        result = {"structuredContent": {"a": 1}, "content": [{"type": "text", "text": '{\n  "a": 1\n}'}]}
        outcome, calls = self._run(guard, "tools/call", params, result)
        assert len(calls) == 1
        assert outcome["content"][0]["text"] == '{"a":1}'

    def test_normalizes_tools_list(self, guard: types.ModuleType) -> None:
        """tools/list の各ツールの inputSchema を整える。"""
        result = {"tools": [{"name": "t", "inputSchema": {"type": "object", "title": "x"}}, {"name": "u"}]}
        outcome, _ = self._run(guard, "tools/list", {}, result)
        assert outcome["tools"][0]["inputSchema"] == {"type": "object", "additionalProperties": False}
        assert outcome["tools"][1]["inputSchema"] == {"additionalProperties": False}

    def test_leaves_non_dict_results(self, guard: types.ModuleType) -> None:
        """dict でない結果と、対象外のメソッドには触れない。"""
        sentinel = object()
        assert self._run(guard, "tools/list", {}, sentinel)[0] is sentinel
        untouched = {"content": [{"type": "text", "text": '{\n "a": 1\n}'}], "structuredContent": {"a": 1}}
        assert self._run(guard, "resources/read", {}, untouched)[0]["content"][0]["text"] == '{\n "a": 1\n}'


class TestSmokeCheckHelpers:
    """smoke_check.py の判定に使う純粋な部分。"""

    def test_walk_keys_skips_argument_names(self, smoke_check: types.ModuleType) -> None:
        """引数名としての `title` は、禁止キーとして数えない。"""
        schema = {"type": "object", "properties": {"title": {"type": "string"}}}
        assert "title" not in set(smoke_check._walk_keys(schema))
        assert "anyOf" in set(smoke_check._walk_keys({"properties": {"x": {"anyOf": [{"$ref": "#/a"}]}}}))

    def test_docstring_sections_are_detected(self, smoke_check: types.ModuleType) -> None:
        """docstring の節見出しが説明へ漏れたことを検出する。"""
        assert smoke_check.DOCSTRING_SECTION_PATTERN.search("説明。\n\nArgs:\n    x: 値\n")
        assert not smoke_check.DOCSTRING_SECTION_PATTERN.search("使う場面: Args の値を確かめるとき")

    def test_tool_name_pattern(self, smoke_check: types.ModuleType) -> None:
        """ツール名は英小文字・数字・`_` だけを許す。"""
        assert smoke_check.TOOL_NAME_PATTERN.match("orders_search")
        for bad in ("orders.search", "OrdersSearch", "orders-search", "1orders"):
            assert not smoke_check.TOOL_NAME_PATTERN.match(bad)

    def test_smoke_check_imports_only_stdlib(self) -> None:
        """smoke_check.py は標準ライブラリと設定モジュールだけを import する。"""
        tree = ast.parse((_TEMPLATE / "smoke_check.py").read_text(encoding="utf-8"))
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import | ast.ImportFrom)
            for alias in (node.names if isinstance(node, ast.Import) else [ast.alias(node.module or "")])
        }
        assert imported - {"smoke_config"} <= set(sys.stdlib_module_names) | {"__future__"}


@pytest.mark.parametrize("skeleton", [_TEMPLATE])
def test_smoke_config_names_exist_in_server(skeleton: Path) -> None:
    """smoke_config.py が指すツールが、対になる server.py に実在する。"""
    config = _config(skeleton / "smoke_config.py")
    tools = _tool_functions(skeleton / "server.py")
    for key in ("HAPPY_TOOL", "INVALID_TOOL", "PAGED_TOOL", "FIELDS_TOOL"):
        if config.get(key):
            assert config[key] in tools, f"{skeleton.name}/smoke_config.py の {key}={config[key]} が server.py に無い"


def test_assets_pass_forbidden_pattern_grep() -> None:
    """スケルトン一式が、正本の禁止パターンに 1 件も当たらない。"""
    match = re.search(r"'([^']+)' <生成先>", _SPEC_REFERENCE.read_text(encoding="utf-8"))
    assert match, "spec-2026-07-28.md から禁止パターンを取り出せません"
    pattern = re.compile(match.group(1))
    hits = []
    for path in sorted((_SKILL_DIR / "assets").rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    hits.append(f"{path.relative_to(_SKILL_DIR)}:{number}: {line.strip()}")
    assert hits == []


def test_assets_ship_no_bytecode() -> None:
    """配布物のスケルトンに `.pyc` / `__pycache__` を含めない。"""
    leftovers = [p for p in (_SKILL_DIR / "assets").rglob("*") if p.suffix == ".pyc" or p.name == "__pycache__"]
    assert leftovers == []
