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
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_SKILL_DIR = _PLUGIN_ROOT / "skills" / "mcp-gen"
_TEMPLATE = _SKILL_DIR / "assets" / "template"
_OVERLAY = _SKILL_DIR / "assets" / "facade-overlay"
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


@pytest.mark.parametrize("skeleton", [_TEMPLATE, _OVERLAY])
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


def test_overlay_budget_fits_smoke_limit() -> None:
    """重ね合わせの server.py の予算が、smoke_config.py の上限を超えない。"""
    tree = ast.parse((_OVERLAY / "server.py").read_text(encoding="utf-8"))
    constants = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id.isupper()
        and isinstance(node.value, ast.Constant)
    }
    assert constants["MAX_RESULT_CHARS"] <= _config(_OVERLAY / "smoke_config.py")["MAX_RESULT_CHARS"]


@pytest.fixture(scope="module")
def facade() -> Iterator[types.SimpleNamespace]:
    """重ね合わせの facade パッケージを読み込む（相対 import を解決するためパッケージとして）。"""
    package_dir = _OVERLAY / "facade"
    spec = importlib.util.spec_from_file_location(
        "mcp_gen_facade", package_dir / "__init__.py", submodule_search_locations=[str(package_dir)]
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
            for name in ("errors", "cursor", "shaping", "catalog", "upstream")
        }
    finally:
        sys.dont_write_bytecode = original
    yield types.SimpleNamespace(**modules)
    for name in [key for key in sys.modules if key == "mcp_gen_facade" or key.startswith("mcp_gen_facade.")]:
        del sys.modules[name]


class _Clock:
    """進め方を決められる時計。"""

    def __init__(self, now: float = 1000.0) -> None:
        """開始時刻を設定する。"""
        self.now = now

    def __call__(self) -> float:
        """現在時刻を返す。"""
        return self.now


_SECRET = b"s" * 32


class TestCursor:
    """署名付きの自己完結カーソル。"""

    def test_round_trip(self, facade: types.SimpleNamespace) -> None:
        """発行したカーソルから、封じた位置を取り出せる。"""
        codec = facade.cursor.CursorCodec(_SECRET)
        token = codec.issue(principal="alice", fingerprint="q1", position={"page": "o:3", "skip": 1})
        assert codec.open(token, principal="alice", fingerprint="q1") == {"page": "o:3", "skip": 1}

    def test_works_across_codecs_with_same_secret(self, facade: types.SimpleNamespace) -> None:
        """鍵が同じなら、別のインスタンスでも検証できる（状態を持たない）。"""
        token = facade.cursor.CursorCodec(_SECRET).issue(principal="a", fingerprint="q", position={"p": 1})
        assert facade.cursor.CursorCodec(_SECRET).open(token, principal="a", fingerprint="q") == {"p": 1}

    @pytest.mark.parametrize(
        ("principal", "fingerprint"),
        [("mallory", "q1"), ("alice", "q2")],
    )
    def test_rejects_other_principal_or_query(self, facade: types.SimpleNamespace, principal: str, fingerprint: str) -> None:
        """別の主体・別の検索条件のカーソルは、同じ文面で拒否する。"""
        codec = facade.cursor.CursorCodec(_SECRET)
        token = codec.issue(principal="alice", fingerprint="q1", position={})
        with pytest.raises(facade.errors.FacadeError, match="今回の検索条件には使えません"):
            codec.open(token, principal=principal, fingerprint=fingerprint)

    def test_rejects_tampering_and_malformed(self, facade: types.SimpleNamespace) -> None:
        """署名の不一致と形式の不正を拒否する。"""
        codec = facade.cursor.CursorCodec(_SECRET)
        token = codec.issue(principal="a", fingerprint="q", position={})
        other = facade.cursor.CursorCodec(b"t" * 32).issue(principal="a", fingerprint="q", position={})
        with pytest.raises(facade.errors.FacadeError, match="検証できません"):
            codec.open(other, principal="a", fingerprint="q")
        for bad in ("no-dot", ".sig", "body."):
            with pytest.raises(facade.errors.FacadeError, match="形式が不正"):
                codec.open(bad, principal="a", fingerprint="q")
        assert token.count(".") == 1

    def test_rejects_unreadable_or_old_payload(self, facade: types.SimpleNamespace) -> None:
        """署名は合うが中身を読めない・版が違うカーソルを拒否する。"""
        codec = facade.cursor.CursorCodec(_SECRET)
        garbage = "!!!"
        with pytest.raises(facade.errors.FacadeError, match="読み取れません"):
            codec.open(f"{garbage}.{codec._sign(garbage)}", principal="a", fingerprint="q")
        old = facade.cursor._encode(b'{"v":0,"pos":{}}')
        with pytest.raises(facade.errors.FacadeError, match="版が古い"):
            codec.open(f"{old}.{codec._sign(old)}", principal="a", fingerprint="q")

    def test_expires(self, facade: types.SimpleNamespace) -> None:
        """有効期間を過ぎたカーソルを拒否する。"""
        clock = _Clock()
        codec = facade.cursor.CursorCodec(_SECRET, ttl_seconds=10, clock=clock)
        token = codec.issue(principal="a", fingerprint="q", position={})
        clock.now += 11
        with pytest.raises(facade.errors.FacadeError, match="有効期限"):
            codec.open(token, principal="a", fingerprint="q")

    def test_requires_long_secret(self, facade: types.SimpleNamespace) -> None:
        """32 バイト未満の鍵は受け付けない。"""
        with pytest.raises(ValueError, match="32"):
            facade.cursor.CursorCodec(b"short")

    def test_fingerprint(self, facade: types.SimpleNamespace) -> None:
        """指紋は cursor とキーの順序に依存せず、値が変われば変わる。"""
        fingerprint = facade.cursor.query_fingerprint
        assert fingerprint({"a": 1, "b": [1, 2], "cursor": "x"}) == fingerprint({"b": [1, 2], "a": 1})
        assert fingerprint({"a": 1}) != fingerprint({"a": 2})
        assert len(fingerprint({})) == 32


class TestShaping:
    """応答の整形。"""

    _ORDER = {
        "order_id": "o1",
        "shipping": {"address": {"city": "大阪市", "postal_code": "5300001"}},
        "items": [{"sku": f"S{i}", "quantity": i, "price": {"amount": i}} for i in range(12)],
        "status": "open",
    }

    def test_compact_json_keeps_japanese(self, facade: types.SimpleNamespace) -> None:
        """日本語をエスケープせず、空白を入れない。"""
        assert facade.shaping.compact_json({"a": "田中", "b": [1, 2]}) == '{"a":"田中","b":[1,2]}'

    def test_project_keeps_structure(self, facade: types.SimpleNamespace) -> None:
        """指定したパスだけを入れ子のまま返し、配列は上限と総数を添える。"""
        projected = facade.shaping.project(self._ORDER, ["shipping.address.city", "items.sku"], array_limit=2)
        assert projected == {
            "shipping": {"address": {"city": "大阪市"}},
            "items": [{"sku": "S0"}, {"sku": "S1"}],
            "items_more": {"total": 12, "returned": 2},
        }

    def test_project_whole_subtree_and_absorbed_paths(self, facade: types.SimpleNamespace) -> None:
        """祖先のパスは子孫のパスを吸収し、葉は値全体（配列は上限付き）を返す。"""
        for paths in (["items", "items.sku"], ["items.sku", "items"]):
            projected = facade.shaping.project(self._ORDER, paths, array_limit=1)
            assert projected == {
                "items": [{"sku": "S0", "quantity": 0, "price": {"amount": 0}}],
                "items_more": {"total": 12, "returned": 1},
            }

    def test_project_ignores_missing_and_scalar_paths(self, facade: types.SimpleNamespace) -> None:
        """存在しないパスは無視し、スカラーより深いパスはスカラーを返す。"""
        projected = facade.shaping.project(self._ORDER, ["nope.x", "status.deeper", "order_id"], array_limit=5)
        assert projected == {"status": "open", "order_id": "o1"}

    def test_project_nested_lists(self, facade: types.SimpleNamespace) -> None:
        """配列の中の配列も上限を守る。"""
        document = {"grid": [[{"v": 1}, {"v": 2}, {"v": 3}], [{"v": 4}]]}
        assert facade.shaping.project(document, ["grid.v"], array_limit=2) == {
            "grid": [[{"v": 1}, {"v": 2}], [{"v": 4}]]
        }

    def test_cap_arrays(self, facade: types.SimpleNamespace) -> None:
        """全ての配列を上限で切り、元の値は変えない。"""
        value = {"a": [1, 2, 3], "b": {"c": [4, 5]}, "d": 1}
        assert facade.shaping.cap_arrays(value, array_limit=2) == {
            "a": [1, 2],
            "a_more": {"total": 3, "returned": 2},
            "b": {"c": [4, 5]},
            "d": 1,
        }
        assert facade.shaping.cap_arrays([[1, 2, 3]], array_limit=2) == [[1, 2]]
        assert value["a"] == [1, 2, 3]

    def test_fit_items(self, facade: types.SimpleNamespace) -> None:
        """予算に収まる最大の件数を返す。"""
        items = ["x" * 10] * 10

        def build(chosen: list[str]) -> dict[str, Any]:
            """一覧を包んだ結果を作る。"""
            return {"items": chosen}

        size = len(facade.shaping.compact_json(build(items[:4])))
        assert facade.shaping.fit_items(items, build, max_chars=size) == 4
        assert facade.shaping.fit_items(items, build, max_chars=10_000) == 10
        assert facade.shaping.fit_items(items, build, max_chars=5) == 0
        assert facade.shaping.fit_items([], build, max_chars=100) == 0

    def test_clean_text(self, facade: types.SimpleNamespace) -> None:
        """制御列・不可視文字を除き、改行とタブは残し、長すぎれば切り詰める。"""
        raw = "至急\x1b[31m 対応​‮お願い\x07\n\tします\x1bM"
        assert facade.shaping.clean_text(raw, max_chars=100) == "至急 対応お願い\n\tします"
        assert facade.shaping.clean_text("あいうえお", max_chars=3) == "あいう…"


def _catalog(facade: types.SimpleNamespace) -> Any:
    """見本の項目カタログを作る。"""
    spec = facade.catalog.FieldSpec
    return facade.catalog.Catalog(
        "order",
        [
            spec("order_id", "string", "受注 ID", views=("summary", "detail")),
            spec("shipping.address.city", "string", "配送先の市区町村", views=("detail",), in_search=True),
            spec("shipping.address.postal_code", "string", "配送先の郵便番号", sensitivity="personal"),
        ],
    )


class TestCatalog:
    """項目カタログ。"""

    def test_rejects_bad_definitions(self, facade: types.SimpleNamespace) -> None:
        """パスの重複と未知の機微区分は定義の誤りとして弾く。"""
        spec = facade.catalog.FieldSpec
        with pytest.raises(ValueError, match="重複"):
            facade.catalog.Catalog("o", [spec("a", "string", "x"), spec("a", "string", "y")])
        with pytest.raises(ValueError, match="機微区分"):
            facade.catalog.Catalog("o", [spec("a", "string", "x", sensitivity="secret")])

    def test_view(self, facade: types.SimpleNamespace) -> None:
        """ビューに含まれるパスを定義順に返す。"""
        assert _catalog(facade).view("detail") == ["order_id", "shipping.address.city"]

    def test_describe(self, facade: types.SimpleNamespace) -> None:
        """パスか説明の部分一致で返し、上限を超えたら has_more を立てる。"""
        catalog = _catalog(facade)
        described = catalog.describe("配送", limit=1)
        assert described["total_matches"] == 2
        assert described["has_more"] is True
        assert described["fields"] == [
            {
                "path": "shipping.address.city",
                "type": "string",
                "description": "配送先の市区町村",
                "views": ["detail"],
                "sensitivity": "internal",
                "in_search": True,
            }
        ]
        assert catalog.describe("ORDER_ID", limit=10)["total_matches"] == 1
        assert catalog.describe(None, limit=10)["total_matches"] == 3

    def test_resolve(self, facade: types.SimpleNamespace) -> None:
        """未知・検索で使えない・権限の無いパスを、次の行動を添えて拒否する。"""
        catalog = _catalog(facade)
        allowed = {"public", "internal"}
        assert catalog.resolve(["shipping.address.city"], for_search=True, allowed_sensitivity=allowed) == [
            "shipping.address.city"
        ]
        with pytest.raises(facade.errors.FacadeError, match="近いパス: shipping.address.city"):
            catalog.resolve(["shiping.address.city"], for_search=False, allowed_sensitivity=allowed)
        with pytest.raises(facade.errors.FacadeError, match="fields_describe") as unknown:
            catalog.resolve(["zzz"], for_search=False, allowed_sensitivity=allowed)
        assert "近いパス" not in str(unknown.value)
        with pytest.raises(facade.errors.FacadeError, match="検索結果には付けられません"):
            catalog.resolve(["order_id"], for_search=True, allowed_sensitivity=allowed)
        with pytest.raises(facade.errors.FacadeError, match="権限"):
            catalog.resolve(["shipping.address.postal_code"], for_search=False, allowed_sensitivity=allowed)


class _Sleeper:
    """待ち時間を記録するだけの sleep。"""

    def __init__(self) -> None:
        """記録を空にする。"""
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        """待たずに記録する。"""
        self.delays.append(delay)


class TestUpstream:
    """上流の呼び出しを包む部品。"""

    def test_retryable_and_message(self, facade: types.SimpleNamespace) -> None:
        """一時的な失敗の判定と、モデル向けの文面（内部の詳細を含めない）。"""
        error = facade.upstream.UpstreamError
        assert error(None).retryable and error(429).retryable and error(503).retryable
        assert not error(404).retryable
        assert "status=500" in str(error(500, "boom"))
        expected = {
            400: "引数",
            401: "管理者",
            403: "管理者",
            404: "見つからないか",
            409: "取得し直して",
            412: "取得し直して",
            429: "混み合って",
            500: "一時的",
            None: "一時的",
        }
        for status, phrase in expected.items():
            message = facade.upstream.model_message(error(status, "secret internal detail"))
            assert phrase in message
            assert "secret" not in message

    def test_retry_until_success(self, facade: types.SimpleNamespace) -> None:
        """一時的な失敗は指数バックオフ（ジッタ込み）で再試行する。"""
        outcomes = [facade.upstream.UpstreamError(503), facade.upstream.UpstreamError(None), "ok"]
        sleeper = _Sleeper()

        async def operation() -> str:
            """用意した順に失敗と成功を返す。"""
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        result = asyncio.run(
            facade.upstream.call_with_retry(operation, base_delay=0.2, sleep=sleeper, jitter=lambda: 1.0)
        )
        assert result == "ok"
        assert sleeper.delays == [0.2, 0.4]

    def test_retry_gives_up(self, facade: types.SimpleNamespace) -> None:
        """再試行しない失敗・回数切れ・締切越えでは諦める。上流の retry_after に従う。"""
        error = facade.upstream.UpstreamError

        def failing(exc: Exception) -> Any:
            """常に同じ失敗を投げる関数を作る。"""

            async def operation() -> None:
                """失敗を投げる。"""
                raise exc

            return operation

        sleeper = _Sleeper()
        with pytest.raises(error):
            asyncio.run(facade.upstream.call_with_retry(failing(error(404)), sleep=sleeper))
        assert sleeper.delays == []
        with pytest.raises(error):
            asyncio.run(facade.upstream.call_with_retry(failing(error(429, retry_after=1.5)), attempts=2, sleep=sleeper))
        assert sleeper.delays == [1.5]
        clock = _Clock(100.0)
        with pytest.raises(error):
            asyncio.run(
                facade.upstream.call_with_retry(failing(error(503)), deadline=100.1, clock=clock, sleep=sleeper)
            )
        assert sleeper.delays == [1.5]

    def test_circuit_breaker(self, facade: types.SimpleNamespace) -> None:
        """連続した一時的な失敗で遮断し、時間が経てば試しに通し、成功で戻る。"""
        error = facade.upstream.UpstreamError
        clock = _Clock()
        breaker = facade.upstream.CircuitBreaker(failure_threshold=2, reset_seconds=30, clock=clock)

        async def fail(status: int) -> None:
            """指定のステータスで失敗する。"""
            raise error(status)

        async def succeed() -> str:
            """成功する。"""
            return "ok"

        for status in (404, 404, 503, 503):
            with pytest.raises(error):
                asyncio.run(breaker.call(lambda status=status: fail(status)))
        assert breaker.allow() is False
        with pytest.raises(error, match="circuit open"):
            asyncio.run(breaker.call(succeed))
        clock.now += 30
        assert asyncio.run(breaker.call(succeed)) == "ok"
        assert breaker.allow() is True

    def test_fan_out(self, facade: types.SimpleNamespace) -> None:
        """成功・失敗・締切超えを分けて返し、全体は失敗にしない。"""
        error = facade.upstream.UpstreamError

        async def ok() -> int:
            """成功する。"""
            return 1

        async def broken() -> None:
            """上流の失敗を投げる。"""
            raise error(503)

        async def slow() -> None:
            """締切に間に合わない。"""
            await asyncio.sleep(10)

        results, failures = asyncio.run(
            facade.upstream.fan_out({"a": ok, "b": broken, "c": slow}, timeout=0.05)
        )
        assert results == {"a": 1}
        assert failures["b"].status == 503
        assert failures["c"].status is None
        assert asyncio.run(facade.upstream.fan_out({}, timeout=1)) == ({}, {})

    def test_fan_out_reraises_bugs(self, facade: types.SimpleNamespace) -> None:
        """上流の失敗以外の例外（プログラムの誤り）は握りつぶさない。"""

        async def bug() -> None:
            """誤りを投げる。"""
            raise KeyError("bug")

        with pytest.raises(KeyError):
            asyncio.run(facade.upstream.fan_out({"x": bug}, timeout=1))
