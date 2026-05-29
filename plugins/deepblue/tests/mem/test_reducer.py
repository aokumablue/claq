"""reducer.py のユニットテスト — RTK スタイルのトークン削減ロジック。"""

from __future__ import annotations

import pytest

from devgear.mem.reducer import (
    ReduceConfig,
    dedup_lines,
    group_lint_errors,
    reduce_bash_output,
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
            ("AssertionError: expected 1, got 2", "AssertionError"),
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
        # 最初の1行のみ保持 + 折りたたみ通知
        assert lines.count(line) == 1
        assert any("×10" in ln for ln in lines), "折りたたみ通知が挿入されるべき"

    def test_below_threshold_is_passthrough(self) -> None:
        line = "[ERROR] Connection refused"
        text = "\n".join([line] * 2)
        result = dedup_lines(text, threshold=3)
        # threshold 未満はそのまま
        assert result.count(line) == 2
        assert "折りたたみ" not in result

    def test_normalizes_timestamps(self) -> None:
        lines = [
            "2026-01-01T12:00:00Z ERROR: disk full",
            "2026-01-01T12:00:01Z ERROR: disk full",
            "2026-01-01T12:00:02Z ERROR: disk full",
            "2026-01-01T12:00:03Z ERROR: disk full",
        ]
        text = "\n".join(lines)
        result = dedup_lines(text, threshold=3)
        # タイムスタンプ違いも同一視される
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
        # 元の行は除去される
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

    def test_groups_pytest_failures(self) -> None:
        text = (
            "FAILED tests/foo.py::test_a - AssertionError: expected 1\n"
            "FAILED tests/bar.py::test_b - AssertionError: expected 1\n"
            "FAILED tests/baz.py::test_c - AssertionError: expected 2\n"
        )
        result = group_lint_errors(text)
        # AssertionError: expected 1 が2件グループ化される
        assert "2件" in result
        assert "pytest FAILED" in result

    def test_passthrough_if_no_lint_errors(self) -> None:
        text = "Build succeeded!\n3 warnings generated."
        result = group_lint_errors(text)
        assert "Build succeeded!" in result

    def test_empty_input(self) -> None:
        assert group_lint_errors("") == ""

    def test_fmt_files_shows_overflow_count(self) -> None:
        """4件以上のファイルがある場合、+Nファイル が表示される。"""
        # ruff エラーを4ファイルで発生させる
        text = "\n".join(f"src/file{i}.py:1:1: E501 line too long" for i in range(4))
        result = group_lint_errors(text)
        # max_show=3 なので +1ファイル が表示される
        assert "+1ファイル" in result


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
        assert "line 0" in result  # 先頭は保持
        assert "line 199" in result  # 末尾は保持
        assert "line 100" not in result  # 中間は省略

    def test_truncation_message_shows_counts(self) -> None:
        lines = [f"line {i}" for i in range(100)]
        text = "\n".join(lines)
        result = smart_truncate(text, max_len=10, head_lines=5, tail_lines=5)
        assert "90 行省略" in result
        assert "計 100 行" in result

    def test_character_based_truncation_for_few_long_lines(self) -> None:
        """行数は少ないが文字数が多い場合、文字数ベースでトランケートされる（lines 240-241）。"""
        text = "a" * 500  # 1行で 500 文字
        result = smart_truncate(text, max_len=100, head_lines=30, tail_lines=30)
        assert "文字省略" in result
        # 先頭/末尾の文字が含まれる
        assert result.startswith("a" * 50)
        assert result.endswith("a" * 50)


# ---------------------------------------------------------------------------
# パイプライン統合
# ---------------------------------------------------------------------------


class TestReduceBashOutput:
    """reduce_bash_output のテスト"""

    def test_disabled_returns_original(self) -> None:
        text = "npm warn deprecated foo\n" * 100
        config = ReduceConfig(enabled=False)
        assert reduce_bash_output(text, config) == text

    def test_empty_input_returns_original(self) -> None:
        assert reduce_bash_output("") == ""
        assert reduce_bash_output("   ") == "   "

    def test_pipeline_applies_all_strategies(self) -> None:
        # ruff エラー + 重複ログ + ボイラープレート + 大量行
        lines = (
            ["npm warn deprecated foo"]
            + ["src/foo.py:1:1: E501 line too long"] * 5
            + ["src/bar.py:2:1: E501 line too long"] * 5
            + ["2026-01-01 12:00:00 INFO: processing"] * 10
            + [f"data line {i}" for i in range(100)]
        )
        text = "\n".join(lines)
        config = ReduceConfig(max_output_len=500, head_lines=10, tail_lines=10)
        result = reduce_bash_output(text, config)

        assert len(result) < len(text), "削減されるべき"
        # ruff エラーはグループ化される
        assert "[E501]" in result or "E501" in result

    def test_does_not_expand_short_output(self) -> None:
        text = "All tests passed!"
        result = reduce_bash_output(text)
        # 短い出力は変わらない（グループ化ヘッダー等が付かない）
        assert len(result) <= len(text) + 10  # ほぼ同じ長さ
