"""guard.py（未知の引数の拒否と、公開する inputSchema・text の整形）のテスト。"""

from __future__ import annotations

import asyncio
import types
from typing import Any

import pytest

from ._support import _TEMPLATE, _load


@pytest.fixture(scope="module")
def guard() -> types.ModuleType:
    """共通スケルトンの guard.py を読み込む。"""
    return _load("mcp_gen_guard", _TEMPLATE / "guard.py")


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

    def test_keeps_non_null_default(self, guard: types.ModuleType) -> None:
        """省略可能な引数でも、null 以外の既定値は残す。"""
        schema = {"type": "object", "properties": {"unit": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": "ms"}}}
        assert guard.normalize_input_schema(schema)["properties"]["unit"] == {"type": "string", "default": "ms"}

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

    def test_leaves_too_deeply_nested_text(self, guard: types.ModuleType) -> None:
        """入れ子が深すぎて json.loads が RecursionError を出す text にも、例外を出さずに触れない。"""
        depth = 1_000_000
        text = "[" * depth + "]" * depth
        result = {"structuredContent": {"a": 1}, "content": [{"type": "text", "text": text}]}
        guard.compact_text(result)
        assert result["content"][0]["text"] == text


class TestRejection:
    """未知の引数の判定と拒否の形。"""

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

    def test_rejection_bounds_echoed_names(self, guard: types.ModuleType) -> None:
        """名指しする未知の引数は先頭の数件に絞り、長い名前は切り詰める（要求の文字列をそのまま返さない）。"""
        long_name = "a" * 500
        unknown = [long_name, "b", "c", "d", "e", "omitted_f", "omitted_g"]
        text = guard.rejection(unknown, ["status"], server_name="s", server_version="1")["content"][0]["text"]
        assert "a" * guard.ECHOED_NAME_MAX_CHARS + "…" in text
        assert "a" * (guard.ECHOED_NAME_MAX_CHARS + 1) not in text
        assert all(name in text for name in ["b", "c", "d", "e"])
        assert "omitted" not in text
        assert "ほか 2 個" in text
        assert len(text) < 300


class _FakeServer:
    """install_guard が使う最小限のサーバ面。"""

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
        guard.install_guard(server)
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
        result = {"tools": [{"name": "t", "inputSchema": {"type": "object", "title": "x"}}]}
        outcome, _ = self._run(guard, "tools/list", {}, result)
        assert outcome["tools"][0]["inputSchema"] == {"type": "object", "additionalProperties": False}

    def test_leaves_other_methods(self, guard: types.ModuleType) -> None:
        """対象外のメソッドの結果には触れない。"""
        untouched = {"content": [{"type": "text", "text": '{\n "a": 1\n}'}], "structuredContent": {"a": 1}}
        assert self._run(guard, "resources/read", {}, untouched)[0]["content"][0]["text"] == '{\n "a": 1\n}'
