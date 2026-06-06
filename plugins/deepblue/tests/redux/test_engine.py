"""redux エンジン（engine.py）のユニットテスト。"""

from __future__ import annotations

import re

from deepblue.redux.config import ReduxConfig
from deepblue.redux.engine import (
    ReduxEngine,
    ReduxFilterSpec,
    ShortCircuitRule,
    SubstituteRule,
    _stage_head_tail,
    apply_spec,
    select_filter,
    strip_ansi,
)


def _spec(**kw: object) -> ReduxFilterSpec:
    """テスト用フィルタ定義を生成（name/command_pattern を補完）。"""
    kw.setdefault("name", "t")
    kw.setdefault("command_pattern", re.compile(".*"))
    return ReduxFilterSpec(**kw)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# strip_ansi
# ---------------------------------------------------------------------------


class TestStripAnsi:
    def test_removes_color_codes(self) -> None:
        assert strip_ansi("\x1b[32mSuccess\x1b[0m") == "Success"

    def test_plain_text_unchanged(self) -> None:
        assert strip_ansi("plain") == "plain"


# ---------------------------------------------------------------------------
# _stage_head_tail（全分岐）
# ---------------------------------------------------------------------------


class TestStageHeadTail:
    def test_head_and_tail_over(self) -> None:
        lines = [str(i) for i in range(10)]
        out = _stage_head_tail(lines, 2, 2)
        assert out[:2] == ["0", "1"]
        assert out[-2:] == ["8", "9"]
        assert "6 行省略" in out[2]

    def test_head_and_tail_under(self) -> None:
        lines = ["a", "b"]
        assert _stage_head_tail(lines, 2, 2) == lines

    def test_head_only_over(self) -> None:
        lines = [str(i) for i in range(5)]
        out = _stage_head_tail(lines, 2, None)
        assert out == ["0", "1", "... (3 行省略)"]

    def test_head_only_under(self) -> None:
        lines = ["a", "b"]
        assert _stage_head_tail(lines, 5, None) == lines

    def test_tail_only_over(self) -> None:
        lines = [str(i) for i in range(5)]
        out = _stage_head_tail(lines, None, 2)
        assert out == ["... (3 行省略)", "3", "4"]

    def test_tail_only_under(self) -> None:
        lines = ["a", "b"]
        assert _stage_head_tail(lines, None, 5) == lines

    def test_neither(self) -> None:
        lines = ["a", "b", "c"]
        assert _stage_head_tail(lines, None, None) == lines


# ---------------------------------------------------------------------------
# apply_spec（各宣言ステージ）
# ---------------------------------------------------------------------------


class TestApplySpec:
    def test_strip_ansi_stage(self) -> None:
        spec = _spec(strip_ansi=True)
        assert apply_spec(spec, "\x1b[31merr\x1b[0m") == "err"

    def test_substitute_chained(self) -> None:
        spec = _spec(
            substitute=[
                SubstituteRule(re.compile(r"\d+"), "N"),
                SubstituteRule(re.compile(r"N N"), "NN"),
            ]
        )
        assert apply_spec(spec, "1 2 foo") == "NN foo"

    def test_short_circuit_match(self) -> None:
        spec = _spec(short_circuit=[ShortCircuitRule(re.compile("OK"), "全部OK")])
        assert apply_spec(spec, "OK\ndone") == "全部OK"

    def test_short_circuit_unless_skips(self) -> None:
        spec = _spec(
            short_circuit=[ShortCircuitRule(re.compile("OK"), "全部OK", re.compile("ERROR"))]
        )
        # ERROR があるので短絡せず通常処理 → 元のまま
        assert apply_spec(spec, "OK\nERROR happened") == "OK\nERROR happened"

    def test_short_circuit_no_match(self) -> None:
        spec = _spec(short_circuit=[ShortCircuitRule(re.compile("XYZ"), "msg")])
        assert apply_spec(spec, "abc\ndef") == "abc\ndef"

    def test_drop_lines(self) -> None:
        spec = _spec(drop_lines=[re.compile(r"^DEBUG")])
        assert apply_spec(spec, "DEBUG x\nINFO y\nDEBUG z") == "INFO y"

    def test_keep_lines(self) -> None:
        spec = _spec(keep_lines=[re.compile(r"ERROR")])
        assert apply_spec(spec, "ok\nERROR boom\nok2") == "ERROR boom"

    def test_clip_width(self) -> None:
        spec = _spec(clip_width=3)
        assert apply_spec(spec, "abcdef\nxy") == "abc\nxy"

    def test_limit_lines_over(self) -> None:
        spec = _spec(limit_lines=2)
        out = apply_spec(spec, "a\nb\nc\nd")
        assert out == "a\nb\n... (2 行切り捨て)"

    def test_limit_lines_under(self) -> None:
        spec = _spec(limit_lines=10)
        assert apply_spec(spec, "a\nb") == "a\nb"

    def test_strategies_stage(self) -> None:
        spec = _spec(strategies=["smart_filter"])
        out = apply_spec(spec, "npm warn x\nkeep me")
        assert "npm warn" not in out
        assert "keep me" in out

    def test_empty_message_fires(self) -> None:
        spec = _spec(drop_lines=[re.compile(".")], empty_message="（出力なし）")
        assert apply_spec(spec, "abc\ndef") == "（出力なし）"

    def test_empty_message_none_returns_empty(self) -> None:
        spec = _spec(drop_lines=[re.compile(".")])
        assert apply_spec(spec, "abc") == ""

    def test_uses_provided_config(self) -> None:
        spec = _spec(strategies=["dedup"])
        cfg = ReduxConfig(dedup_threshold=2)
        out = apply_spec(spec, "x\nx\ny", cfg)
        assert "折りたたみ" in out


# ---------------------------------------------------------------------------
# select_filter
# ---------------------------------------------------------------------------


class TestSelectFilter:
    def test_first_match_wins(self) -> None:
        a = _spec(name="a", command_pattern=re.compile("^git"))
        b = _spec(name="b", command_pattern=re.compile("^git status"))
        assert select_filter("git status", [a, b]).name == "a"

    def test_no_match(self) -> None:
        a = _spec(name="a", command_pattern=re.compile("^ps"))
        assert select_filter("git status", [a]) is None


# ---------------------------------------------------------------------------
# ReduxEngine.reduce
# ---------------------------------------------------------------------------


class TestReduxEngine:
    def test_reduces_matching_command(self) -> None:
        eng = ReduxEngine([_spec(command_pattern=re.compile("^ps"), limit_lines=1)])
        out = eng.reduce("ps aux", "a\nb\nc")
        assert out == "a\n... (2 行切り捨て)"

    def test_disabled_passthrough(self) -> None:
        eng = ReduxEngine([_spec(command_pattern=re.compile("^ps"), limit_lines=1)])
        assert eng.reduce("ps aux", "a\nb\nc", ReduxConfig(enabled=False)) == "a\nb\nc"

    def test_empty_output_passthrough(self) -> None:
        eng = ReduxEngine([_spec(command_pattern=re.compile(".*"), limit_lines=1)])
        assert eng.reduce("ps", "") == ""

    def test_blank_output_passthrough(self) -> None:
        eng = ReduxEngine([_spec(command_pattern=re.compile(".*"), limit_lines=1)])
        assert eng.reduce("ps", "   ") == "   "

    def test_no_matching_filter_passthrough(self) -> None:
        eng = ReduxEngine([_spec(command_pattern=re.compile("^ps"))])
        assert eng.reduce("git status", "a\nb") == "a\nb"

    def test_specs_property(self) -> None:
        specs = [_spec(name="x")]
        eng = ReduxEngine(specs)
        assert eng.specs[0].name == "x"
