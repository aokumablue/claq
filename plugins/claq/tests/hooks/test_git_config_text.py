"""`git_config_text`（git config 本文の最小パーサと、危険 key の変更判定）のテスト。"""

from __future__ import annotations

import pytest

from claq.hooks.git_config_text import (
    ConfigEntry,
    GitConfigParseError,
    adds_sensitive_entries,
    parse_git_config,
)


def _sensitive(key: str) -> bool:
    """テスト用の危険 key 判定（alias と core.hookspath）。"""
    lowered = key.lower()
    return lowered.startswith(("alias.", "core.hookspath"))


def _sensitive_include(key: str) -> bool:
    """テスト用の危険 key 判定（include.path）。"""
    return key.lower().startswith("include.path")


def _entries(text: str) -> list[tuple[str, str | None]]:
    """解析結果を (key, value) の組で返す。"""
    return [(entry.key, entry.value) for entry in parse_git_config(text)]


class TestSections:
    """セクション見出しの解釈。"""

    def test_plain_section(self) -> None:
        """`[sec]` の key は `sec.key`（小文字）になる。"""
        assert _entries("[Core]\n\tHooksPath = x\n") == [("core.hookspath", "x")]

    def test_subsection_keeps_case(self) -> None:
        """`[sec "Sub"]` の subsection は大小を保つ。"""
        assert _entries('[remote "Origin"]\n\tURL = u\n') == [("remote.Origin.url", "u")]

    def test_subsection_escapes(self) -> None:
        """subsection 中の `\\"` と `\\\\` は次の 1 文字をそのまま取る。"""
        assert _entries('[s "a\\"b\\\\c"]\n\tk = v\n') == [('s.a"b\\c.k', "v")]

    def test_legacy_dotted_section_is_lowercased(self) -> None:
        """`[sec.sub]` の旧記法は全体を小文字にする。"""
        assert _entries("[Alias.X]\n\tk = v\n") == [("alias.x.k", "v")]

    def test_header_with_space_before_quote(self) -> None:
        """名前と引用符の間の空白は何個でもよい。"""
        assert _entries('[includeIf   "gitdir:~/x/"]\n\tpath = y\n') == [("includeif.gitdir:~/x/.path", "y")]

    def test_key_on_header_line(self) -> None:
        """見出しと同じ行の key を拾う。"""
        assert _entries("[core] hooksPath = x\n") == [("core.hookspath", "x")]

    def test_comment_after_header(self) -> None:
        """見出しの後ろのコメントは読み飛ばす。"""
        assert _entries("[core] # c\n\tk = v\n") == [("core.k", "v")]

    def test_later_section_replaces_earlier(self) -> None:
        """key は直近の見出しに属する。"""
        assert _entries("[a]\nx = 1\n[b]\ny = 2\n") == [("a.x", "1"), ("b.y", "2")]

    @pytest.mark.parametrize(
        "text",
        [
            "[]\n",
            "[core\n",
            "[core",
            "[co!re]\n",
            "[core \n",
            '[core "x\n"]\n',
            '[core "x"',
            '[core "x" ]\n',
            '[core "x\\\n"]\n',
            "[core] ]\n",
            "[core x]\n",
            '[ "x"]\n',
        ],
        ids=[
            "empty",
            "unterminated-newline",
            "unterminated-eof",
            "bad-char",
            "space-then-newline",
            "newline-in-subsection",
            "missing-close",
            "space-before-close",
            "escaped-newline-in-subsection",
            "stray-bracket",
            "unquoted-subsection",
            "empty-name-with-subsection",
        ],
    )
    def test_malformed_header_is_rejected(self, text: str) -> None:
        """壊れた見出しは解析エラーにする。"""
        with pytest.raises(GitConfigParseError):
            parse_git_config(text)


class TestKeys:
    """key 行の解釈。"""

    def test_key_before_any_section_is_rejected(self) -> None:
        """見出しより前の key は解析エラーにする。"""
        with pytest.raises(GitConfigParseError):
            parse_git_config("hooksPath = x\n")

    def test_value_less_key(self) -> None:
        """`=` の無い key は値なし（None）になる。"""
        assert _entries("[core]\n\tbare\n\tother\t\n[x]\n\tlast") == [
            ("core.bare", None),
            ("core.other", None),
            ("x.last", None),
        ]

    def test_empty_value(self) -> None:
        """`key =` の値は空文字列。"""
        assert _entries("[a]\nk =\n") == [("a.k", "")]

    def test_key_charset(self) -> None:
        """key は英字で始まり、英数字と `-` を続けられる。"""
        assert _entries("[a]\nk-1 = v\n") == [("a.k-1", "v")]

    @pytest.mark.parametrize(
        "text",
        ["[a]\n1k = v\n", "[a]\n= v\n", "[a]\nk ; c\n", "[a]\nk v\n", "[a]\nk!x = v\n"],
        ids=["digit-start", "no-name", "comment-after-bare", "junk-after-name", "bad-char"],
    )
    def test_malformed_key_line_is_rejected(self, text: str) -> None:
        """key 行として読めない行は解析エラーにする。"""
        with pytest.raises(GitConfigParseError):
            parse_git_config(text)


class TestValues:
    """値の解釈。"""

    def test_inline_comments(self) -> None:
        """引用符の外の `#` と `;` から行末まではコメント。"""
        assert _entries("[a]\nk = v ; c\nj = w # c\n") == [("a.k", "v"), ("a.j", "w")]

    def test_quoted_value_keeps_comment_chars_and_spaces(self) -> None:
        """引用符の中の `#` と `;` と前後の空白は値に残る。"""
        assert _entries('[a]\nk = " v # ; "\n') == [("a.k", " v # ; ")]

    def test_internal_whitespace_is_kept_and_trailing_trimmed(self) -> None:
        """値の内側の空白は残り、末尾の空白は落ちる。"""
        assert _entries("[a]\nk = a  b \t \n") == [("a.k", "a  b")]

    def test_escapes(self) -> None:
        """`\\n` `\\t` `\\b` `\\\\` `\\\"` を解釈する。"""
        assert _entries('[a]\nk = "a\\n\\t\\b\\\\\\""\n') == [("a.k", 'a\n\t\b\\"')]

    def test_continuation_line(self) -> None:
        """行末の `\\` は次の行へ値を続ける（継続行の行頭の空白も値の区切りとして数える）。"""
        assert _entries("[alias]\n\tci = commit \\\n\t--no-verify\n") == [("alias.ci", "commit  --no-verify")]

    def test_continuation_inside_quotes(self) -> None:
        """引用符の中でも継続行は使える。"""
        assert _entries('[a]\nk = "x\\\ny"\n') == [("a.k", "xy")]

    def test_comment_ends_at_newline_even_with_backslash(self) -> None:
        """コメントの末尾の `\\` は継続行にならない。"""
        assert _entries("[a]\nk = v # c \\\nj = w\n") == [("a.k", "v"), ("a.j", "w")]

    def test_crlf_and_bom(self) -> None:
        """CRLF と先頭の BOM を受け付ける。"""
        assert _entries("\ufeff[a]\r\nk = v\r\n") == [("a.k", "v")]

    def test_value_at_eof_without_newline(self) -> None:
        """末尾の改行が無くても値を読み切る。"""
        assert _entries("[a]\nk = v") == [("a.k", "v")]

    def test_comment_lines_and_blank_lines(self) -> None:
        """行頭のコメントと空行は無視する。"""
        assert _entries("# a\n; b\n\n[a] \n  # hooksPath = x\n") == []

    @pytest.mark.parametrize(
        "text",
        ['[a]\nk = "v\n', '[a]\nk = "v', "[a]\nk = v\\", "[a]\nk = \\q\n"],
        ids=["open-quote-newline", "open-quote-eof", "trailing-backslash-eof", "unknown-escape"],
    )
    def test_malformed_value_is_rejected(self, text: str) -> None:
        """閉じない引用符・未知のエスケープは解析エラーにする。"""
        with pytest.raises(GitConfigParseError):
            parse_git_config(text)

    def test_unrecognized_line_is_rejected(self) -> None:
        """key でも見出しでもコメントでもない行は解析エラーにする。"""
        with pytest.raises(GitConfigParseError):
            parse_git_config("[a]\n= v\n")


class TestAddsSensitiveEntries:
    """危険 key の前後比較。"""

    def test_returns_entries_as_named_tuples(self) -> None:
        """解析結果は ConfigEntry。"""
        assert parse_git_config("[a]\nk = v\n") == [ConfigEntry("a.k", "v")]

    def test_new_sensitive_entry(self) -> None:
        """前に無い危険 key が現れたら True。"""
        assert adds_sensitive_entries("[user]\nname = x\n", "[user]\nname = x\n[core]\nhooksPath = y\n", _sensitive)

    def test_unchanged_sensitive_entries(self) -> None:
        """危険 key が前後で同一なら、他の key が変わっても False。"""
        before = "[alias]\nci = commit\n[remote]\nurl = a\n"
        after = "[alias]\nci = commit\n[remote]\nurl = b\n"
        assert not adds_sensitive_entries(before, after, _sensitive)

    def test_changed_value(self) -> None:
        """危険 key の値が変わったら True。"""
        assert adds_sensitive_entries("[alias]\nci = commit\n", "[alias]\nci = commit --no-verify\n", _sensitive)

    def test_added_alias_in_existing_section(self) -> None:
        """既存セクションの内側へ足した alias を検出する。"""
        assert adds_sensitive_entries(
            "[alias]\nst = status\n", "[alias]\nst = status\nci = commit --no-verify\n", _sensitive
        )

    def test_removing_shadowing_entry_is_detected(self) -> None:
        """後ろの上書きだけを消すと実効値が危険側へ戻るため True。"""
        before = "[alias]\nci = commit --no-verify\nci = commit\n"
        after = "[alias]\nci = commit --no-verify\n"
        assert adds_sensitive_entries(before, after, _sensitive)

    def test_reordering_is_detected(self) -> None:
        """危険 key の順序入れ替えは True。"""
        before = "[alias]\na = 1\n[core]\nhooksPath = x\n"
        after = "[core]\nhooksPath = x\n[alias]\na = 1\n"
        assert adds_sensitive_entries(before, after, _sensitive)

    def test_pure_removal_is_allowed(self) -> None:
        """危険 key を丸ごと消すだけなら False。"""
        assert not adds_sensitive_entries("[alias]\nci = commit\nst = status\n", "[alias]\nst = status\n", _sensitive)

    def test_removing_trailing_entry_that_changes_effective_value_is_detected(self) -> None:
        """最後の値を消して手前の値が実効になる場合は True。"""
        before = "[core]\nhooksPath = a\nhooksPath = b\n"
        assert adds_sensitive_entries(before, "[core]\nhooksPath = a\n", _sensitive)

    def test_unparsable_before_counts_as_empty(self) -> None:
        """置換前が解析できなければ、置換後の危険 key は全て新規扱い。"""
        assert adds_sensitive_entries("garbage\n", "[alias]\nci = x\n", _sensitive)
        assert not adds_sensitive_entries("garbage\n", "[user]\nname = x\n", _sensitive)

    def test_unparsable_after_raises(self) -> None:
        """置換後が解析できなければ例外を伝える。"""
        with pytest.raises(GitConfigParseError):
            adds_sensitive_entries("", "garbage\n", _sensitive)

    def test_unrelated_edit_with_multi_valued_key_is_allowed(self) -> None:
        """複数値の key（include.path の列）を持つファイルへの無関係な編集は通す。"""
        before = "[include]\n path=a\n path=b\n"
        assert not adds_sensitive_entries(before, before + '[remote "o"]\n url=x\n', _sensitive_include)

    def test_changing_a_non_final_value_of_multi_valued_key_is_detected(self) -> None:
        """複数値の key の途中の値を書き換える編集は、列の部分列でなくなるため True。"""
        before = "[include]\n path=a\n path=b\n"
        assert adds_sensitive_entries(before, "[include]\n path=evil\n path=b\n", _sensitive_include)

    @pytest.mark.parametrize("blank", ["\xa0", "\v", "\f", "\u3000"])
    def test_non_git_whitespace_is_part_of_the_value(self, blank: str) -> None:
        """git が空白と見ない文字（NBSP・VT・FF・全角空白）は値の一部で、値の変更として検出する。"""
        before = f"[alias]\n ci = commit{blank}--no-verify\n"
        assert adds_sensitive_entries(before, "[alias]\n ci = commit --no-verify\n", _sensitive)
        assert adds_sensitive_entries("[core]\n hooksPath=/a\n", f"[core]\n hooksPath=/a{blank}\n", _sensitive)

    def test_empty_quotes_then_space_is_not_a_separator(self) -> None:
        """``""`` だけの値の後ろの空白は、値が空のままなので区切りに数えない。"""
        assert _entries('[a]\nk = "" a\n') == [("a.k", "a")]

    def test_sensitive_key_in_comment_is_ignored(self) -> None:
        """コメント内の hooksPath は危険 key ではない。"""
        assert not adds_sensitive_entries("", "# hooksPath = x\n[user]\n# [core]\n", _sensitive)
