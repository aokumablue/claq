"""shell_split モジュールのユニットテスト。

デシジョンテーブル:
  - 単一コマンド → セグメント1件
  - && 演算子 → 分割
  - || 演算子 → 分割
  - ; 演算子 → 分割
  - 単独 & (バックグラウンド) → 分割
  - &> リダイレクト → 分割しない
  - >& リダイレクト → 分割しない
  - digit>& リダイレクト → 分割しない
  - 引用符内の演算子 → 分割しない
  - バックスラッシュエスケープ → 分割しない
  - 引用符内のバックスラッシュ → 保持
  - 空文字/空白のみ → 空リスト
  - 演算子の前後が空白 → トリム済みで返す
  - ネスト演算子 → 正しい分割
"""

from __future__ import annotations

import pytest

from devgear.lib.shell_split import split_shell_segments


class TestSingleCommand:
    """単一コマンド（演算子なし）"""

    def test_plain_command_returns_single_segment(self) -> None:
        assert split_shell_segments("ls -la") == ["ls -la"]

    def test_empty_string_returns_empty_list(self) -> None:
        assert split_shell_segments("") == []

    def test_whitespace_only_returns_empty_list(self) -> None:
        assert split_shell_segments("   ") == []

    def test_command_with_args_preserved(self) -> None:
        result = split_shell_segments("git commit -m 'fix bug'")
        assert result == ["git commit -m 'fix bug'"]


class TestAndAndOperator:
    """&& 演算子による分割"""

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("echo a && echo b", ["echo a", "echo b"]),
            ("a && b && c", ["a", "b", "c"]),
            ("cmd1 && cmd2", ["cmd1", "cmd2"]),
        ],
    )
    def test_splits_on_double_ampersand(self, command: str, expected: list[str]) -> None:
        assert split_shell_segments(command) == expected

    def test_empty_segment_before_and_is_skipped(self) -> None:
        # "&& cmd" — 先頭が空
        result = split_shell_segments("&& echo b")
        assert result == ["echo b"]

    def test_empty_segment_after_and_is_skipped(self) -> None:
        result = split_shell_segments("echo a &&")
        assert result == ["echo a"]


class TestOrOrOperator:
    """|| 演算子による分割"""

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("echo a || echo b", ["echo a", "echo b"]),
            ("false || true || echo done", ["false", "true", "echo done"]),
        ],
    )
    def test_splits_on_double_pipe(self, command: str, expected: list[str]) -> None:
        assert split_shell_segments(command) == expected


class TestSemicolonOperator:
    """; 演算子による分割"""

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("echo a; echo b", ["echo a", "echo b"]),
            ("a; b; c", ["a", "b", "c"]),
            ("cmd1;cmd2", ["cmd1", "cmd2"]),
        ],
    )
    def test_splits_on_semicolon(self, command: str, expected: list[str]) -> None:
        assert split_shell_segments(command) == expected


class TestBackgroundAmpersand:
    """単独 & (バックグラウンド実行) による分割"""

    def test_single_ampersand_splits(self) -> None:
        result = split_shell_segments("sleep 10 & echo done")
        assert result == ["sleep 10", "echo done"]

    def test_trailing_ampersand_for_background(self) -> None:
        result = split_shell_segments("sleep 10 &")
        assert result == ["sleep 10"]


class TestRedirectNotSplit:
    """リダイレクト演算子は分割対象外"""

    def test_and_redirect_not_split(self) -> None:
        # &> はリダイレクト — 分割しない
        result = split_shell_segments("cmd &> /dev/null")
        assert len(result) == 1
        assert "&>" in result[0]

    def test_gt_ampersand_not_split(self) -> None:
        # >& はリダイレクト — 分割しない
        result = split_shell_segments("cmd >& /dev/null")
        # >& は prev_ch == ">" かつ ch == "&" の場合に処理される
        assert len(result) == 1

    def test_stderr_redirect_not_split(self) -> None:
        # 2>&1 — prev_ch がdigitで "&" が後続する場合
        result = split_shell_segments("cmd 2>&1")
        assert len(result) == 1
        assert "2>" in result[0] or "2>&1" in result[0]


class TestQuotedStrings:
    """引用符内の演算子は分割しない"""

    @pytest.mark.parametrize(
        ("command", "desc"),
        [
            ('echo "a && b"', "ダブルクォート内の&&"),
            ("echo 'a || b'", "シングルクォート内の||"),
            ('echo "a; b"', "ダブルクォート内の;"),
            ("echo 'a & b'", "シングルクォート内の&"),
        ],
    )
    def test_operator_inside_quotes_not_split(self, command: str, desc: str) -> None:
        result = split_shell_segments(command)
        assert len(result) == 1, f"{desc}: 引用符内の演算子で分割されてはいけない"

    def test_quoted_string_preserved_exactly(self) -> None:
        cmd = 'git commit -m "fix: handle && operator"'
        result = split_shell_segments(cmd)
        assert result == [cmd]

    def test_double_quote_closed_correctly(self) -> None:
        result = split_shell_segments('"hello" && echo world')
        assert result == ['"hello"', "echo world"]

    def test_single_quote_closed_correctly(self) -> None:
        result = split_shell_segments("'hello' && echo world")
        assert result == ["'hello'", "echo world"]


class TestBackslashEscape:
    """バックスラッシュエスケープ処理"""

    def test_escaped_semicolon_not_split(self) -> None:
        result = split_shell_segments(r"echo a\; echo b")
        # \; はエスケープされているので分割されない
        assert len(result) == 1

    def test_escaped_ampersand_not_split(self) -> None:
        result = split_shell_segments(r"echo a\&\& echo b")
        assert len(result) == 1

    def test_backslash_in_double_quote_preserved(self) -> None:
        # 引用符内のバックスラッシュは次の文字と一緒に保持
        result = split_shell_segments(r'"a\"b" && echo c')
        assert len(result) == 2


class TestMixedOperators:
    """複数演算子の組み合わせ"""

    def test_mixed_operators(self) -> None:
        result = split_shell_segments("a && b; c || d")
        assert result == ["a", "b", "c", "d"]

    def test_realistic_shell_command(self) -> None:
        result = split_shell_segments("cd /tmp && mkdir -p build && make all || echo 'build failed'")
        assert result == ["cd /tmp", "mkdir -p build", "make all", "echo 'build failed'"]

    def test_whitespace_trimmed_in_segments(self) -> None:
        result = split_shell_segments("  echo a  &&  echo b  ")
        assert result == ["echo a", "echo b"]
