"""guard.py（未知の引数の拒否・主体 × ツールの呼び出しの制限と、公開する inputSchema・text の整形）のテスト。"""

from __future__ import annotations

import asyncio
import types
from typing import Any

import pytest

from ._support import _TEMPLATE, _Clock, _load


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


class TestCallLimiter:
    """主体 × ツールの呼び出しの回数を、全キー共通の固定窓で数える。"""

    def test_admits_up_to_limit_then_refuses(self, guard: types.ModuleType) -> None:
        """同じ窓の中では calls 回まで通し、それを超えたら断る（断った呼び出しは数えない）。"""
        limiter = guard.CallLimiter(2, 60.0, clock=_Clock())
        assert [limiter.admit("alice", "t") for _ in range(4)] == [True, True, False, False]

    def test_recovers_in_next_window(self, guard: types.ModuleType) -> None:
        """窓が変われば、断られていた主体 × ツールもまた通す。"""
        clock = _Clock(0.0)
        limiter = guard.CallLimiter(1, 60.0, clock=clock)
        admitted = [limiter.admit("alice", "t"), limiter.admit("alice", "t")]
        clock.now = 60.0
        admitted.append(limiter.admit("alice", "t"))
        assert admitted == [True, False, True]

    def test_counts_per_principal_and_tool(self, guard: types.ModuleType) -> None:
        """回数は主体 × ツールごとに数える（ある主体のあるツールが上限でも、他の組み合わせは通す）。"""
        limiter = guard.CallLimiter(1, 60.0, clock=_Clock())
        limiter.admit("alice", "t")
        assert [limiter.admit("alice", "t"), limiter.admit("alice", "u"), limiter.admit("bob", "t")] == [False, True, True]

    def test_forgets_previous_window(self, guard: types.ModuleType) -> None:
        """窓が変わったら前の窓の記録を丸ごと消す（主体 × ツールの数だけ記録が増え続けない）。"""
        clock = _Clock(0.0)
        limiter = guard.CallLimiter(5, 60.0, clock=clock)
        for principal in ("alice", "bob", "carol"):
            limiter.admit(principal, "t")
        clock.now = 120.0
        limiter.admit("dave", "t")
        assert limiter._counts == {("dave", "t"): 1}


class _FakeServer:
    """install_guard が使う最小限のサーバ面。"""

    def __init__(self) -> None:
        """登録済みのツールを 2 つ持つ。"""
        self.middleware: list[Any] = []
        self.name = "fake"
        self.version = "0.0.1"

    async def list_tools(self) -> list[Any]:
        """公開中のツール定義を返す。"""
        return [
            types.SimpleNamespace(name="orders_search", input_schema={"properties": {"status": {}}}),
            types.SimpleNamespace(name="orders_get", input_schema={"properties": {"order_id": {}}}),
        ]


class _PrincipalUnknown(Exception):
    """主体を決められないことを表す、テスト用の宣言済みの失敗。"""


def _installed(guard: types.ModuleType, limiter: Any, principal: Any = lambda: "alice", principal_errors: Any = ()) -> Any:
    """ガードを掛けたサーバの middleware を返す。後続の呼び出しは middleware.calls に記録する。"""
    server = _FakeServer()
    guard.install_guard(server, limiter=limiter, principal=principal, principal_errors=principal_errors)
    calls: list[Any] = []
    middleware = server.middleware[0]

    def send(method: str, params: Any, result: Any = None) -> Any:
        """middleware を 1 回通し、その結果を返す（後続は result の写しを返す）。"""

        async def call_next(ctx: Any) -> Any:
            """後続の処理の代わりに、渡された文脈を記録して結果を返す。"""
            calls.append(ctx)
            return {"content": []} if result is None else result

        return asyncio.run(middleware(types.SimpleNamespace(method=method, params=params), call_next))

    send.calls = calls
    return send


def _tool_call(name: str, arguments: Any) -> dict[str, Any]:
    """tools/call の検証前のパラメータを組み立てる。"""
    return {"name": name, "arguments": arguments}


class TestInstalledGuard:
    """登録した middleware の振る舞い。"""

    @staticmethod
    def _run(guard: types.ModuleType, method: str, params: Any, result: Any) -> tuple[Any, list[Any]]:
        """どの上限にも当たらない制限で middleware を 1 回通し、結果と後続の呼び出し記録を返す。"""
        send = _installed(guard, guard.CallLimiter(100, 60.0, clock=_Clock()))
        outcome = send(method, params, result)
        return outcome, send.calls

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

    def test_limits_calls_per_principal_and_tool(self, guard: types.ModuleType) -> None:
        """主体 × ツールごとに上限まで通し、超えた呼び出しは後続へ渡さずに isError で返す（他の組み合わせは通す）。"""
        caller = ["alice"]
        send = _installed(guard, guard.CallLimiter(1, 60.0, clock=_Clock()), principal=lambda: caller[0])
        refused = [
            send("tools/call", _tool_call("orders_search", {})).get("isError") is True,
            send("tools/call", _tool_call("orders_search", {"status": "open"})).get("isError") is True,
            send("tools/call", _tool_call("orders_get", {"order_id": "ord_000001"})).get("isError") is True,
        ]
        caller[0] = "bob"
        refused.append(send("tools/call", _tool_call("orders_search", {})).get("isError") is True)
        assert (refused, len(send.calls)) == ([False, True, False, False], 3)

    def test_limit_recovers_after_window(self, guard: types.ModuleType) -> None:
        """窓が変われば、上限で断られていた呼び出しもまた後続へ渡す。"""
        clock = _Clock(0.0)
        send = _installed(guard, guard.CallLimiter(1, 60.0, clock=clock))
        send("tools/call", _tool_call("orders_search", {}))
        send("tools/call", _tool_call("orders_search", {}))
        clock.now = 60.0
        send("tools/call", _tool_call("orders_search", {}))
        assert len(send.calls) == 2

    def test_unknown_tool_and_unknown_argument_are_not_counted(self, guard: types.ModuleType) -> None:
        """未知のツール・object でない引数（SDK に任せる）と、未知の引数で拒否した呼び出しは回数に数えない。"""
        send = _installed(guard, guard.CallLimiter(1, 60.0, clock=_Clock()))
        send("tools/call", _tool_call("unknown_tool", {"x": 1}))
        send("tools/call", _tool_call("orders_search", ["not", "a", "mapping"]))
        send("tools/call", _tool_call("orders_search", {"stauts": "open"}))
        assert send("tools/call", _tool_call("orders_search", {})).get("isError") is not True

    def test_declared_principal_error_returns_tool_error(self, guard: types.ModuleType) -> None:
        """主体を決められない失敗が宣言した型なら、その文面の isError を返し、後続へ渡さない。"""

        def principal() -> str:
            """主体を決められない。"""
            raise _PrincipalUnknown("主体を決められません。運用者に連絡してください。")

        send = _installed(guard, guard.CallLimiter(1, 60.0, clock=_Clock()), principal, (_PrincipalUnknown,))
        outcome = send("tools/call", _tool_call("orders_search", {}))
        assert (outcome["isError"], outcome["content"][0]["text"], send.calls) == (
            True,
            "主体を決められません。運用者に連絡してください。",
            [],
        )

    def test_undeclared_principal_error_propagates(self, guard: types.ModuleType) -> None:
        """宣言していない型の失敗は、そのまま投げる（プログラムの誤りを結果に変えない）。"""

        def principal() -> str:
            """宣言外の失敗を投げる。"""
            raise RuntimeError("bug")

        send = _installed(guard, guard.CallLimiter(1, 60.0, clock=_Clock()), principal, (_PrincipalUnknown,))
        with pytest.raises(RuntimeError):
            send("tools/call", _tool_call("orders_search", {}))


def test_limited_is_tool_execution_error(guard: types.ModuleType) -> None:
    """上限を超えた呼び出しは、ツール名と上限を示し、間隔を空けるか条件を絞るよう促す isError の結果にする。"""
    limited = guard.limited("orders_search", 30, 60.0, server_name="s", server_version="1")
    assert limited == {
        "resultType": "complete",
        "isError": True,
        "content": [
            {
                "type": "text",
                "text": "orders_search の呼び出しが多すぎます（60 秒に 30 回まで）。"
                "間隔を空けて呼び直すか、条件を絞って呼び出しの回数を減らしてください。",
            }
        ],
        "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "s", "version": "1"}},
    }
