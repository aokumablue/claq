"""smoke_check.py（実起動の適合検査）のうち、サーバを起動せずに確かめられる部分のテスト。"""

from __future__ import annotations

import ast
import sys
import types
import unittest
from pathlib import Path
from typing import Any

import pytest

from ._support import _MINIMAL_SETTINGS, _OVERLAY, _TEMPLATE, _config, _load_smoke_check


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
        """smoke_check.py は標準ライブラリと設定モジュール・smoke_static だけを、smoke_static.py は標準ライブラリだけを import する。"""
        for name, allowed in (("smoke_check.py", {"smoke_config", "smoke_static"}), ("smoke_static.py", set())):
            tree = ast.parse((_TEMPLATE / name).read_text(encoding="utf-8"))
            imported = {
                alias.name.split(".")[0]
                for node in ast.walk(tree)
                if isinstance(node, ast.Import | ast.ImportFrom)
                for alias in (node.names if isinstance(node, ast.Import) else [ast.alias(node.module or "")])
            }
            assert imported - allowed <= set(sys.stdlib_module_names) | {"__future__"}, name


class TestSmokeCheckSettings:
    """smoke_config.py の名前の検査。"""

    @pytest.mark.parametrize("skeleton", [_TEMPLATE, _OVERLAY])
    def test_known_settings_pass(self, monkeypatch: pytest.MonkeyPatch, skeleton: Path) -> None:
        """両スケルトンの設定は、知らない名前を持たず、有効にした検査の期待値を揃えている。"""
        _load_smoke_check(monkeypatch, _config(skeleton / "smoke_config.py")).test_settings_are_known()

    @pytest.mark.parametrize(
        ("settings", "named"),
        [
            ({"INVALID_TOOL": "t"}, "INVALID_EXPECTED_MESSAGE"),
            ({"INVALID_TOOL": "t", "INVALID_EXPECTED_MESSAGE": ""}, "INVALID_EXPECTED_MESSAGE"),
            ({"RESOURCE_URI": "config://x"}, "RESOURCE_EXPECTED_SUBSTRING"),
            ({"PROMPT_NAME": "p"}, "PROMPT_EXPECTED_SUBSTRING"),
            ({"FIELDS_TOOL": "t", "FIELDS_EXPECTED_SUGGESTION": "a.b"}, "FIELDS_BAD_ARGS"),
            ({"FIELDS_TOOL": "t", "FIELDS_BAD_ARGS": {"fields": ["x"]}}, "FIELDS_EXPECTED_SUGGESTION"),
            ({"PAGED_TOOL": "t", "PAGED_CHANGED_ARGS": {"a": 2}}, "PAGED_ARGS"),
            ({"PAGED_TOOL": "t", "PAGED_ARGS": {"a": 1}}, "PAGED_CHANGED_ARGS"),
            ({"PAGED_TOOL": "t", "PAGED_ARGS": {"a": 1}, "PAGED_CHANGED_ARGS": {"a": 1}}, "PAGED_CHANGED_ARGS"),
            ({"PAGED_TOOL": "t", "PAGED_ARGS": {"a": 1}, "PAGED_CHANGED_ARGS": {"a": 2}, "CURSOR_ERROR_SUBSTRING": ""}, "CURSOR_ERROR_SUBSTRING"),
            ({"MRTR_CASES": [{"tool": "t", "arguments": {}, "action": "decline", "expected_is_error": False}]}, r"\['expected_text'\]"),
            ({"MRTR_CASES": [{"tool": "t", "arguments": {}, "action": "decline", "expected_is_error": False, "expected_text": ""}]}, "expected_text が空"),
            ({"MRTR_CASES": [{"arguments": {}, "action": "decline", "expected_is_error": False, "expected_text": "x"}]}, r"\['tool'\]"),
            ({"MRTR_CASES": ["not a case"]}, r"MRTR_CASES\[0\]"),
        ],
    )
    def test_enabled_check_needs_expectation(self, monkeypatch: pytest.MonkeyPatch, settings: dict[str, Any], named: str) -> None:
        """有効にした検査に対になる期待値が無い・空なら、名指しで落とす（空文字はどの文面にも含まれ、検査が必ず通る）。"""
        module = _load_smoke_check(monkeypatch, {"HAPPY_TOOL": "t", "HAPPY_ARGS": {}, "HAPPY_EXPECTED_STRUCTURED": {}, **settings})
        with pytest.raises(AssertionError, match=named):
            module.test_settings_are_known()

    def test_complete_mrtr_case_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """必須のキーが揃い、expected_text が空でない MRTR のケースは通す。"""
        case = {"tool": "items_delete", "arguments": {"item_id": "item-1"}, "action": "decline", "expected_is_error": False, "expected_text": "中止しました"}
        settings = {"HAPPY_TOOL": "t", "HAPPY_ARGS": {}, "HAPPY_EXPECTED_STRUCTURED": {}, "MRTR_CASES": [case]}
        _load_smoke_check(monkeypatch, settings).test_settings_are_known()

    def test_fields_check_requires_published_tool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FIELDS_TOOL が未公開なら、未知ツールの isError で空振り合格させず、tools/list で名指しして落とす。"""
        settings = {
            "HAPPY_TOOL": "t",
            "HAPPY_ARGS": {},
            "HAPPY_EXPECTED_STRUCTURED": {},
            "FIELDS_TOOL": "orders_get",
            "FIELDS_BAD_ARGS": {"fields": ["shiping"]},
            "FIELDS_EXPECTED_SUGGESTION": "shipping",
        }
        module = _load_smoke_check(monkeypatch, settings)
        unknown_tool = {"isError": True, "content": [{"type": "text", "text": "Unknown tool: orders_get (shipping)"}]}

        def exchange(tools: list[str]) -> Any:
            """tools/list と tools/call に固定の応答を返す _exchange の代わりを作る。"""
            return lambda requests, env=None: {1: {"result": {"tools": [{"name": name} for name in tools]}}, 2: {"result": unknown_tool}}

        monkeypatch.setattr(module, "_exchange", exchange(["other"]))
        with pytest.raises(AssertionError, match="orders_get が未公開"):
            module.test_unknown_field_gets_suggestion()
        monkeypatch.setattr(module, "_exchange", exchange(["orders_get"]))
        module.test_unknown_field_gets_suggestion()

    def test_typo_and_missing_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """綴りを誤った名前と、必須の名前の欠落を落とす。"""
        settings = {**_config(_TEMPLATE / "smoke_config.py"), "PAGE_TOOL": "orders_search"}
        with pytest.raises(AssertionError, match="PAGE_TOOL"):
            _load_smoke_check(monkeypatch, settings).test_settings_are_known()
        settings = {key: value for key, value in _config(_TEMPLATE / "smoke_config.py").items() if key != "HAPPY_ARGS"}
        with pytest.raises(AssertionError, match="HAPPY_ARGS"):
            _load_smoke_check(monkeypatch, settings).test_settings_are_known()

    def test_omitted_settings_use_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """書かなかった任意の名前は既定値になる（検査は SKIP）。"""
        settings = {"HAPPY_TOOL": "t", "HAPPY_ARGS": {}, "HAPPY_EXPECTED_STRUCTURED": {}}
        module = _load_smoke_check(monkeypatch, settings)
        assert module.cfg.INVALID_TOOL == ""
        assert module.cfg.PAGED_TOOL == ""
        assert module.cfg.MRTR_CASES == []
        assert module.REQUEST_META["io.modelcontextprotocol/clientCapabilities"] == {}


def _no_spawn(*args: Any, **kwargs: Any) -> Any:
    """サーバの起動の代わりに置き、呼ばれたら落とす（SKIP する検査が起動しないことを確かめる）。"""
    raise AssertionError("SKIP するはずの検査がサーバを起動しました")


class TestSmokeCheckMain:
    """smoke_check.py の任意の検査の SKIP と、main の表示。"""

    @pytest.mark.parametrize(
        ("check", "setting"),
        [
            ("test_tool_error_reaches_the_model", "INVALID_TOOL"),
            ("test_resource_is_served", "RESOURCE_URI"),
            ("test_prompt_is_served", "PROMPT_NAME"),
            ("test_paging_round_trip", "PAGED_TOOL"),
            ("test_unknown_field_gets_suggestion", "FIELDS_TOOL"),
            ("test_mrtr_round_trips", "MRTR_CASES"),
        ],
    )
    def test_unset_optional_check_skips(self, monkeypatch: pytest.MonkeyPatch, check: str, setting: str) -> None:
        """設定の無い任意の検査は、サーバを起動せずに unittest.SkipTest を投げる（pytest でも SKIP になる）。"""
        module = _load_smoke_check(monkeypatch, _MINIMAL_SETTINGS)
        monkeypatch.setattr(module, "_spawn", _no_spawn)
        with pytest.raises(unittest.SkipTest, match=setting):
            getattr(module, check)()

    def test_main_reports_pass_fail_and_skip(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """main は検査ごとに PASS・FAIL・SKIP（理由付き）を表示し、FAIL があれば 1 を返す。"""
        module = _load_smoke_check(monkeypatch, _MINIMAL_SETTINGS)

        def passing() -> None:
            """通る。"""

        def failing() -> None:
            """落ちる。"""
            raise AssertionError("壊れている")

        def skipped() -> None:
            """設定が無いので SKIP する。"""
            raise unittest.SkipTest("X 未設定")

        monkeypatch.setattr(module, "CHECKS", (passing, failing, skipped))
        monkeypatch.setattr(module, "sdk_protocol_warning", lambda: None)
        assert module.main() == 1
        assert capsys.readouterr().err.splitlines() == [
            "PASS test_settings_are_known",
            "PASS passing",
            "FAIL failing: 壊れている",
            "SKIP skipped (X 未設定)",
        ]
        monkeypatch.setattr(module, "CHECKS", (passing, skipped))
        assert module.main() == 0

    def test_main_stops_on_bad_settings(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """設定が足りなければ、サーバを起動する検査を回さずに 1 を返す。"""
        module = _load_smoke_check(monkeypatch, {"HAPPY_TOOL": "t", "HAPPY_EXPECTED_STRUCTURED": {}})
        ran: list[str] = []

        def recorded() -> None:
            """回されたことを記録する。"""
            ran.append("recorded")

        monkeypatch.setattr(module, "CHECKS", (recorded,))
        assert module.main() == 1
        assert ran == []
        assert capsys.readouterr().err.startswith("FAIL test_settings_are_known:")

    def test_main_prints_sdk_warning(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """SDK のプロトコル版の知らせは最後に WARN として出し、終了コードは変えない。"""
        module = _load_smoke_check(monkeypatch, _MINIMAL_SETTINGS)
        monkeypatch.setattr(module, "CHECKS", ())
        monkeypatch.setattr(module, "sdk_protocol_warning", lambda: "SDK が進んでいます")
        assert module.main() == 0
        assert capsys.readouterr().err.splitlines() == ["PASS test_settings_are_known", "WARN SDK が進んでいます"]

    def test_main_lists_every_check(self, smoke_check: types.ModuleType) -> None:
        """main が回す検査の一覧に、モジュールの全ての検査（設定の検査を除く）が入っている。"""
        defined = {name for name in vars(smoke_check) if name.startswith("test_")} - {"test_settings_are_known"}
        assert {check.__name__ for check in smoke_check.CHECKS} == defined


class TestSdkProtocolWarning:
    """SDK のプロトコル版の警告。"""

    @staticmethod
    def _fake_run(returncode: int, stdout: str) -> Any:
        """subprocess.run の代わりに固定の結果を返す関数を作る。"""

        def run(*args: Any, **kwargs: Any) -> Any:
            """固定の完了結果を返す。"""
            return types.SimpleNamespace(returncode=returncode, stdout=stdout)

        return run

    def test_warns_only_when_sdk_moved(self, smoke_check: types.ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        """版が違えば警告し、同じとき・SDK を読めないときは黙る。"""
        monkeypatch.setattr(smoke_check.subprocess, "run", self._fake_run(0, "2099-01-01\n"))
        assert "2099-01-01" in smoke_check.sdk_protocol_warning()
        monkeypatch.setattr(smoke_check.subprocess, "run", self._fake_run(0, smoke_check.PROTOCOL_VERSION))
        assert smoke_check.sdk_protocol_warning() is None
        monkeypatch.setattr(smoke_check.subprocess, "run", self._fake_run(1, ""))
        assert smoke_check.sdk_protocol_warning() is None

    def test_warns_when_probe_times_out(self, smoke_check: types.ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        """SDK の版を確かめる子プロセスが時間切れなら、例外を出さずに確かめられなかったことを警告する。"""

        def run(*args: Any, **kwargs: Any) -> Any:
            """時間切れを起こす。"""
            raise smoke_check.subprocess.TimeoutExpired(args[0], kwargs["timeout"])

        monkeypatch.setattr(smoke_check.subprocess, "run", run)
        warning = smoke_check.sdk_protocol_warning()
        assert "確かめられません" in warning
        assert smoke_check.PROTOCOL_VERSION in warning
