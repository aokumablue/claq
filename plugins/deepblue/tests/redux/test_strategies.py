"""redux 戦略（strategies.py）のユニットテスト。"""

from __future__ import annotations

import pytest

from deepblue.redux.config import ReduxConfig
from deepblue.redux.strategies import (
    STRATEGY_DISPATCH,
    _LintGroup,
    _render_lint_groups,
    dedup_lines,
    group_lint_errors,
    smart_filter,
    smart_truncate,
)

# ---------------------------------------------------------------------------
# 戦略1: スマートフィルタリング
# ---------------------------------------------------------------------------


class TestSmartFilter:
    """smart_filter のテスト"""

    @pytest.mark.parametrize(
        ("line", "desc"),
        [
            ("npm warn deprecated foo", "npm warn"),
            ("[notice] a new release", "pip notice"),
            ("hint: use --force to overwrite", "git hint"),
            ("Requirement already satisfied: requests", "pip noop"),
            ("-----", "区切り線（ハイフン）"),
            ("=====", "区切り線（イコール）"),
            ("  20 passing", "mocha passing"),
            ("  5 pending", "mocha pending"),
            ("# shell comment", "シェルコメント"),
            ("  .  ", "pytest ドット行"),
            ("remote: Counting objects: 100", "git push verbosity"),
        ],
    )
    def test_removes_boilerplate(self, line: str, desc: str) -> None:
        result = smart_filter(line)
        assert result.strip() == "", f"{desc} は除去されるべき"

    @pytest.mark.parametrize(
        ("line", "desc"),
        [
            ("ERROR: connection refused", "エラー行"),
            ("FAILED tests/foo.py::test_bar", "FAILED 行"),
            ("warning: unused variable", "warning 行"),
            ("fatal: not a git repository", "fatal 行"),
            ("Traceback (most recent call last):", "Python Traceback"),
            ('  File "foo.py", line 10', "Python スタックトレース"),
            ("  at fn (foo.js:1)", "JS スタックトレース"),
            ("AssertionError: expected 1, got 2", "AssertionError"),
            ("  3 errors found", "エラー件数行"),
        ],
    )
    def test_preserves_important_lines(self, line: str, desc: str) -> None:
        result = smart_filter(line)
        assert line in result, f"{desc} は保持されるべき"

    def test_compresses_consecutive_blank_lines(self) -> None:
        text = "line1\n\n\n\nline2"
        result = smart_filter(text)
        assert "\n\n\n" not in result, "連続空行は1行に圧縮されるべき"
        assert "line1" in result
        assert "line2" in result

    def test_empty_input(self) -> None:
        assert smart_filter("") == ""

    def test_preserves_normal_lines(self) -> None:
        text = "Running tests...\nAll tests passed."
        assert "Running tests..." in smart_filter(text)


# ---------------------------------------------------------------------------
# 戦略2: 重複排除
# ---------------------------------------------------------------------------


class TestDedupLines:
    """dedup_lines のテスト"""

    def test_folds_repeated_lines(self) -> None:
        line = "[ERROR] Connection refused"
        text = "\n".join([line] * 10)
        result = dedup_lines(text, threshold=3)
        lines = result.splitlines()
        assert lines.count(line) == 1
        assert any("×10" in ln for ln in lines), "折りたたみ通知が挿入されるべき"

    def test_below_threshold_is_passthrough(self) -> None:
        line = "[ERROR] Connection refused"
        text = "\n".join([line] * 2)
        result = dedup_lines(text, threshold=3)
        assert result.count(line) == 2
        assert "折りたたみ" not in result

    def test_normalizes_timestamps_addr_digits(self) -> None:
        lines = [
            "2026-01-01T12:00:00Z ERROR at 0xdeadbeef code 12345",
            "2026-01-01T12:00:01Z ERROR at 0xcafef00d code 67890",
            "2026-01-01T12:00:02Z ERROR at 0xfeedface code 11111",
            "2026-01-01T12:00:03Z ERROR at 0xabad1dea code 22222",
        ]
        text = "\n".join(lines)
        result = dedup_lines(text, threshold=3)
        non_notice_lines = [ln for ln in result.splitlines() if "折りたたみ" not in ln]
        assert len(non_notice_lines) == 1

    def test_different_lines_not_folded(self) -> None:
        text = "foo\nbar\nbaz"
        result = dedup_lines(text, threshold=3)
        assert result == text

    def test_empty_input(self) -> None:
        assert dedup_lines("") == ""


# ---------------------------------------------------------------------------
# 戦略3: グループ化
# ---------------------------------------------------------------------------


class TestGroupLintErrors:
    """group_lint_errors のテスト"""

    def test_groups_ruff_errors_by_code(self) -> None:
        text = (
            "src/foo.py:1:5: E501 line too long\n"
            "src/bar.py:2:1: E501 line too long\n"
            "src/baz.py:3:1: E501 line too long\n"
            "src/foo.py:4:1: F401 unused import\n"
        )
        result = group_lint_errors(text)
        assert "[E501]: 3件" in result
        assert "[F401]: 1件" in result
        assert "src/foo.py:1:5:" not in result

    def test_groups_eslint_errors_by_rule(self) -> None:
        text = (
            "  src/a.ts:10:5  error  'x' is never reassigned  prefer-const\n"
            "  src/b.ts:20:3  error  'y' is never reassigned  prefer-const\n"
            "  src/a.ts:30:1  warning  Missing semicolon  semi\n"
        )
        result = group_lint_errors(text)
        assert "[prefer-const]" in result
        assert "2件" in result
        assert "[semi]" in result
        assert "(warning)" in result

    def test_groups_pytest_failures(self) -> None:
        text = (
            "FAILED tests/foo.py::test_a - AssertionError: expected 1\n"
            "FAILED tests/bar.py::test_b - AssertionError: expected 1\n"
            "FAILED tests/baz.py::test_c - AssertionError: expected 2\n"
        )
        result = group_lint_errors(text)
        assert "2件" in result
        assert "pytest FAILED" in result

    def test_passthrough_if_no_lint_errors(self) -> None:
        text = "Build succeeded!\n3 warnings generated."
        result = group_lint_errors(text)
        assert "Build succeeded!" in result

    def test_empty_input(self) -> None:
        assert group_lint_errors("") == ""

    def test_fmt_files_shows_overflow_count(self) -> None:
        text = "\n".join(f"src/file{i}.py:1:1: E501 line too long" for i in range(4))
        result = group_lint_errors(text)
        assert "+1ファイル" in result

    def test_eslint_dedup_files(self) -> None:
        """同一ファイルが複数回出ても files には1回だけ追加される。"""
        text = (
            "  src/a.ts:10:5  error  msg one  prefer-const\n"
            "  src/a.ts:20:3  error  msg two  prefer-const\n"
        )
        result = group_lint_errors(text)
        # files は src/a.ts のみ（重複なし）
        assert result.count("src/a.ts") == 1
        assert "2件" in result

    def test_ruff_dedup_files(self) -> None:
        """ruff でも同一ファイルは files に1回だけ。"""
        text = "src/a.py:1:1: E501 long\nsrc/a.py:2:1: E501 long\n"
        result = group_lint_errors(text)
        assert "[E501]: 2件" in result

    def test_render_eslint_without_first_msg(self) -> None:
        """first_msg が空の eslint グループでは『例:』行を出力しない。"""
        parts: list[str] = []
        group = _LintGroup(rule="r", severity="error", count=1, files=["f.ts"], first_msg="")
        _render_lint_groups(parts, {"r": group}, {}, {})
        assert not any("例:" in p for p in parts)
        assert any("[r]" in p for p in parts)


# ---------------------------------------------------------------------------
# 戦略4: スマートトランケーション
# ---------------------------------------------------------------------------


class TestSmartTruncate:
    """smart_truncate のテスト"""

    def test_passthrough_when_short(self) -> None:
        text = "short text"
        assert smart_truncate(text, max_len=1000) == text

    def test_truncates_long_text(self) -> None:
        lines = [f"line {i}" for i in range(200)]
        text = "\n".join(lines)
        result = smart_truncate(text, max_len=100, head_lines=5, tail_lines=5)
        assert "省略" in result
        assert "line 0" in result
        assert "line 199" in result
        assert "line 100" not in result

    def test_truncation_message_shows_counts(self) -> None:
        lines = [f"line {i}" for i in range(100)]
        text = "\n".join(lines)
        result = smart_truncate(text, max_len=10, head_lines=5, tail_lines=5)
        assert "90 行省略" in result
        assert "計 100 行" in result

    def test_character_based_truncation_for_few_long_lines(self) -> None:
        text = "a" * 500
        result = smart_truncate(text, max_len=100, head_lines=30, tail_lines=30)
        assert "文字省略" in result
        assert result.startswith("a" * 50)
        assert result.endswith("a" * 50)


# ---------------------------------------------------------------------------
# 戦略ディスパッチ
# ---------------------------------------------------------------------------


class TestStrategyDispatch:
    """STRATEGY_DISPATCH の各戦略と enabled フラグ。"""

    def test_smart_filter_enabled(self) -> None:
        cfg = ReduxConfig()
        out = STRATEGY_DISPATCH["smart_filter"]("npm warn x\nkeep me", cfg)
        assert "npm warn" not in out
        assert "keep me" in out

    def test_smart_filter_disabled(self) -> None:
        cfg = ReduxConfig(smart_filter_enabled=False)
        text = "npm warn x"
        assert STRATEGY_DISPATCH["smart_filter"](text, cfg) == text

    def test_dedup_enabled(self) -> None:
        cfg = ReduxConfig(dedup_threshold=3)
        text = "\n".join(["same line"] * 5)
        out = STRATEGY_DISPATCH["dedup"](text, cfg)
        assert "折りたたみ" in out

    def test_dedup_disabled(self) -> None:
        cfg = ReduxConfig(dedup_enabled=False)
        text = "\n".join(["same line"] * 5)
        assert STRATEGY_DISPATCH["dedup"](text, cfg) == text

    def test_group_lint_enabled(self) -> None:
        cfg = ReduxConfig()
        text = "src/a.py:1:1: E501 long\nsrc/b.py:2:1: E501 long\n"
        out = STRATEGY_DISPATCH["group_lint"](text, cfg)
        assert "[E501]" in out

    def test_group_lint_disabled(self) -> None:
        cfg = ReduxConfig(group_lint_enabled=False)
        text = "src/a.py:1:1: E501 long"
        assert STRATEGY_DISPATCH["group_lint"](text, cfg) == text

    def test_smart_truncate_enabled(self) -> None:
        cfg = ReduxConfig(smart_truncate_enabled=True, max_output_len=10, head_lines=2, tail_lines=2)
        text = "\n".join(f"line {i}" for i in range(50))
        out = STRATEGY_DISPATCH["smart_truncate"](text, cfg)
        assert "省略" in out

    def test_smart_truncate_disabled(self) -> None:
        cfg = ReduxConfig(smart_truncate_enabled=False, max_output_len=10)
        text = "\n".join(f"line {i}" for i in range(50))
        assert STRATEGY_DISPATCH["smart_truncate"](text, cfg) == text

    def test_smart_truncate_under_limit_passthrough(self) -> None:
        cfg = ReduxConfig(smart_truncate_enabled=True, max_output_len=100000)
        text = "short"
        assert STRATEGY_DISPATCH["smart_truncate"](text, cfg) == text
