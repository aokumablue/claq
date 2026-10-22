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
    """テスト用の危険 key 判定（alias・core.hookspath・include.path）。"""
    return key.lower().startswith(("alias.", "core.hookspath", "include.path"))


# 解析結果の表: 本文 → (key, value) の列。key は section 名・key 名を小文字にし、subsection は大小を保つ。
_PARSED = [
    # --- セクション見出し ---
    pytest.param("[Core]\n\tHooksPath = x\n", [("core.hookspath", "x")], id="plain-section"),
    pytest.param('[remote "Origin"]\n\tURL = u\n', [("remote.Origin.url", "u")], id="subsection-keeps-case"),
    # subsection 中の `\"` と `\\` は次の 1 文字をそのまま取る。
    pytest.param('[s "a\\"b\\\\c"]\n\tk = v\n', [('s.a"b\\c.k', "v")], id="subsection-escapes"),
    pytest.param("[Alias.X]\n\tk = v\n", [("alias.x.k", "v")], id="legacy-dotted-section-lowercased"),
    pytest.param(
        '[includeIf   "gitdir:~/x/"]\n\tpath = y\n', [("includeif.gitdir:~/x/.path", "y")], id="spaces-before-quote"
    ),
    pytest.param("[core] hooksPath = x\n", [("core.hookspath", "x")], id="key-on-header-line"),
    pytest.param("[core] # c\n\tk = v\n", [("core.k", "v")], id="comment-after-header"),
    pytest.param("[a]\nx = 1\n[b]\ny = 2\n", [("a.x", "1"), ("b.y", "2")], id="key-belongs-to-latest-header"),
    # --- key 行 ---
    pytest.param(
        "[core]\n\tbare\n\tother\t\n[x]\n\tlast",
        [("core.bare", None), ("core.other", None), ("x.last", None)],
        id="value-less-key-is-none",
    ),
    pytest.param("[a]\nk =\n", [("a.k", "")], id="empty-value"),
    pytest.param("[a]\nk-1 = v\n", [("a.k-1", "v")], id="key-charset-letter-digit-dash"),
    pytest.param("[a]\nk = v\n", [("a.k", "v")], id="entries-are-config-entry"),
    # --- 値 ---
    pytest.param("[a]\nk = v ; c\nj = w # c\n", [("a.k", "v"), ("a.j", "w")], id="inline-comments"),
    pytest.param('[a]\nk = " v # ; "\n', [("a.k", " v # ; ")], id="quoted-value-keeps-comment-chars-and-spaces"),
    pytest.param("[a]\nk = a  b \t \n", [("a.k", "a  b")], id="internal-whitespace-kept-trailing-trimmed"),
    pytest.param('[a]\nk = "a\\n\\t\\b\\\\\\""\n', [("a.k", 'a\n\t\b\\"')], id="escapes"),
    # 継続行の行頭の空白も値の区切りとして数える。
    pytest.param(
        "[alias]\n\tci = commit \\\n\t--no-verify\n", [("alias.ci", "commit  --no-verify")], id="continuation-line"
    ),
    pytest.param('[a]\nk = "x\\\ny"\n', [("a.k", "xy")], id="continuation-inside-quotes"),
    pytest.param(
        "[a]\nk = v # c \\\nj = w\n", [("a.k", "v"), ("a.j", "w")], id="comment-backslash-is-not-continuation"
    ),
    pytest.param("\ufeff[a]\r\nk = v\r\n", [("a.k", "v")], id="crlf-and-bom"),
    pytest.param("[a]\nk = v", [("a.k", "v")], id="value-at-eof-without-newline"),
    pytest.param("# a\n; b\n\n[a] \n  # hooksPath = x\n", [], id="comment-and-blank-lines-ignored"),
    # `""` だけの値の後ろの空白は、値が空のままなので区切りに数えない。
    pytest.param('[a]\nk = "" a\n', [("a.k", "a")], id="empty-quotes-then-space-is-not-separator"),
]

# 解析エラーの表: 見出し・key 行・値として読めない本文。
_MALFORMED = [
    # --- 見出し ---
    pytest.param("[]\n", id="header-empty"),
    pytest.param("[core\n", id="header-unterminated-newline"),
    pytest.param("[core", id="header-unterminated-eof"),
    pytest.param("[co!re]\n", id="header-bad-char"),
    pytest.param("[core \n", id="header-space-then-newline"),
    pytest.param('[core "x\n"]\n', id="header-newline-in-subsection"),
    pytest.param('[core "x"', id="header-missing-close"),
    pytest.param('[core "x" ]\n', id="header-space-before-close"),
    pytest.param('[core "x\\\n"]\n', id="header-escaped-newline-in-subsection"),
    pytest.param("[core] ]\n", id="header-stray-bracket"),
    pytest.param("[core x]\n", id="header-unquoted-subsection"),
    pytest.param('[ "x"]\n', id="header-empty-name-with-subsection"),
    # --- key 行 ---
    pytest.param("hooksPath = x\n", id="key-before-any-section"),
    pytest.param("[a]\n1k = v\n", id="key-digit-start"),
    # key でも見出しでもコメントでもない行。
    pytest.param("[a]\n= v\n", id="key-no-name"),
    pytest.param("[a]\nk ; c\n", id="key-comment-after-bare"),
    pytest.param("[a]\nk v\n", id="key-junk-after-name"),
    pytest.param("[a]\nk!x = v\n", id="key-bad-char"),
    # --- 値 ---
    pytest.param('[a]\nk = "v\n', id="value-open-quote-newline"),
    pytest.param('[a]\nk = "v', id="value-open-quote-eof"),
    pytest.param("[a]\nk = v\\", id="value-trailing-backslash-eof"),
    pytest.param("[a]\nk = \\q\n", id="value-unknown-escape"),
]

_NON_GIT_BLANKS = {"nbsp": "\xa0", "vt": "\v", "ff": "\f", "ideographic-space": "\u3000"}

# 危険 key の前後比較の表: (置換前, 置換後) → 危険 key が増えた・変わったか。
_SENSITIVE_CHANGES = [
    pytest.param("[user]\nname = x\n", "[user]\nname = x\n[core]\nhooksPath = y\n", True, id="new-sensitive-entry"),
    pytest.param(
        "[alias]\nci = commit\n[remote]\nurl = a\n",
        "[alias]\nci = commit\n[remote]\nurl = b\n",
        False,
        id="sensitive-unchanged-other-key-changed",
    ),
    pytest.param("[alias]\nci = commit\n", "[alias]\nci = commit --no-verify\n", True, id="changed-value"),
    pytest.param(
        "[alias]\nst = status\n",
        "[alias]\nst = status\nci = commit --no-verify\n",
        True,
        id="added-alias-in-existing-section",
    ),
    # 後ろの上書きだけを消すと実効値が危険側へ戻る。
    pytest.param(
        "[alias]\nci = commit --no-verify\nci = commit\n",
        "[alias]\nci = commit --no-verify\n",
        True,
        id="removing-shadowing-entry",
    ),
    pytest.param(
        "[alias]\na = 1\n[core]\nhooksPath = x\n",
        "[core]\nhooksPath = x\n[alias]\na = 1\n",
        True,
        id="reordering",
    ),
    pytest.param("[alias]\nci = commit\nst = status\n", "[alias]\nst = status\n", False, id="pure-removal"),
    pytest.param(
        "[core]\nhooksPath = a\nhooksPath = b\n",
        "[core]\nhooksPath = a\n",
        True,
        id="removing-trailing-entry-changes-effective-value",
    ),
    # 置換前が解析できなければ、置換後の危険 key は全て新規扱い。
    pytest.param("garbage\n", "[alias]\nci = x\n", True, id="unparsable-before-sensitive-after"),
    pytest.param("garbage\n", "[user]\nname = x\n", False, id="unparsable-before-ordinary-after"),
    # 複数値の key（include.path の列）。無関係な編集は通し、途中の値の書き換えは部分列でなくなるため検出する。
    pytest.param(
        "[include]\n path=a\n path=b\n",
        '[include]\n path=a\n path=b\n[remote "o"]\n url=x\n',
        False,
        id="multi-valued-key-unrelated-edit",
    ),
    pytest.param(
        "[include]\n path=a\n path=b\n",
        "[include]\n path=evil\n path=b\n",
        True,
        id="multi-valued-key-non-final-value-changed",
    ),
    # git が空白と見ない文字（NBSP・VT・FF・全角空白）は値の一部で、値の変更として検出する。
    *(
        pytest.param(
            f"[alias]\n ci = commit{blank}--no-verify\n",
            "[alias]\n ci = commit --no-verify\n",
            True,
            id=f"non-git-blank-{name}-inside-value",
        )
        for name, blank in _NON_GIT_BLANKS.items()
    ),
    *(
        pytest.param(
            "[core]\n hooksPath=/a\n", f"[core]\n hooksPath=/a{blank}\n", True, id=f"non-git-blank-{name}-trailing"
        )
        for name, blank in _NON_GIT_BLANKS.items()
    ),
    pytest.param("", "# hooksPath = x\n[user]\n# [core]\n", False, id="sensitive-key-in-comment-ignored"),
]


@pytest.mark.parametrize(("text", "expected"), _PARSED)
def test_parse_git_config(text: str, expected: list[tuple[str, str | None]]) -> None:
    """本文を ConfigEntry の列へ解析する。"""
    entries = parse_git_config(text)

    assert entries == [ConfigEntry(key, value) for key, value in expected]
    assert [(entry.key, entry.value) for entry in entries] == expected


@pytest.mark.parametrize("text", _MALFORMED)
def test_malformed_text_is_rejected(text: str) -> None:
    """読めない見出し・key 行・値は解析エラーにする。"""
    with pytest.raises(GitConfigParseError):
        parse_git_config(text)


@pytest.mark.parametrize(("before", "after", "expected"), _SENSITIVE_CHANGES)
def test_adds_sensitive_entries(before: str, after: str, expected: bool) -> None:
    """危険 key が置換前に無かった・値や順序が変わったときだけ True。"""
    assert adds_sensitive_entries(before, after, _sensitive) is expected


def test_unparsable_after_raises() -> None:
    """置換後が解析できなければ例外を伝える。"""
    with pytest.raises(GitConfigParseError):
        adds_sensitive_entries("", "garbage\n", _sensitive)
