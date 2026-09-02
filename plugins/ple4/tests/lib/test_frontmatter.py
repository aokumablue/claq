"""ple4.lib.frontmatter の単体テスト。"""

from __future__ import annotations

import pytest

from ple4.lib.frontmatter import (
    FrontmatterError,
    MissingFrontmatterError,
    UnterminatedFrontmatterError,
    parse_yaml,
    split_frontmatter,
)


def _nested_mapping(levels: int) -> str:
    """levels 段だけネストしたブロックマッピングのテキストを組み立てる。"""
    return "\n".join(" " * level + "k:" for level in range(levels))


# --------------------------------------------------------------------------
# split_frontmatter
# --------------------------------------------------------------------------


def test_split_frontmatter_returns_block_between_fences() -> None:
    """先頭と終了の --- の間だけを返す。"""
    assert split_frontmatter("---\nname: x\n---\nbody\n") == "name: x"


def test_split_frontmatter_returns_multiline_block() -> None:
    """複数行の frontmatter を改行区切りで返す。"""
    assert split_frontmatter("---\na: 1\nb: 2\n---\n") == "a: 1\nb: 2"


def test_split_frontmatter_returns_empty_string_for_empty_block() -> None:
    """フェンスが連続する空の frontmatter は空文字列を返す。"""
    assert split_frontmatter("---\n---\n") == ""


def test_split_frontmatter_strips_bom_and_normalizes_crlf() -> None:
    """BOM を除去し CRLF を LF へ正規化してから切り出す。"""
    assert split_frontmatter("\ufeff---\r\nname: x\r\n---\r\nbody") == "name: x"


def test_split_frontmatter_normalizes_lone_cr() -> None:
    """CR のみの改行も LF へ正規化する。"""
    assert split_frontmatter("---\ra: 1\r---\rbody") == "a: 1"


def test_split_frontmatter_allows_surrounding_spaces_on_fence() -> None:
    """フェンス行の前後空白は許容する。"""
    assert split_frontmatter("---  \nname: x\n  ---\n") == "name: x"


def test_split_frontmatter_rejects_missing_start_fence() -> None:
    """先頭行が --- でなければ MissingFrontmatterError を送出する。"""
    with pytest.raises(MissingFrontmatterError):
        split_frontmatter("name: x\n---\n")


def test_split_frontmatter_rejects_fence_with_trailing_text() -> None:
    """---extra はフェンスとみなさない。"""
    with pytest.raises(MissingFrontmatterError):
        split_frontmatter("---extra\nname: x\n---\n")


def test_split_frontmatter_rejects_missing_end_fence() -> None:
    """終了の --- が無ければ UnterminatedFrontmatterError を送出する。"""
    with pytest.raises(UnterminatedFrontmatterError):
        split_frontmatter("---\nname: x\nbody\n")


def test_split_frontmatter_errors_derive_from_frontmatter_error() -> None:
    """両エラーとも FrontmatterError（ひいては ValueError）派生である。"""
    assert issubclass(MissingFrontmatterError, FrontmatterError)
    assert issubclass(UnterminatedFrontmatterError, FrontmatterError)
    assert issubclass(FrontmatterError, ValueError)


# --------------------------------------------------------------------------
# スカラー型解決
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a: 123", 123),
        ("a: 0", 0),
        ("a: -7", -7),
        ("a: +7", 7),
        ("a: 1.5", 1.5),
        ("a: 1.", 1.0),
        ("a: .5", 0.5),
        ("a: -0.25", -0.25),
        ("a: 1.5e3", 1500.0),
        ("a: 2e3", 2000.0),
        ("a: true", True),
        ("a: True", True),
        ("a: TRUE", True),
        ("a: false", False),
        ("a: False", False),
        ("a: FALSE", False),
        ("a: ~", None),
        ("a: null", None),
        ("a: Null", None),
        ("a: NULL", None),
        ("a: maybe", "maybe"),
        ("a: -bad", "-bad"),
        ("a: 0x1f", "0x1f"),
        ("a: 1_000", "1_000"),
        ("a: .inf", ".inf"),
        ("a: 12:30", "12:30"),
        ("a: 2026-08-09", "2026-08-09"),
        ("a: 2つの出力を比較する", "2つの出力を比較する"),
        ("a: ２", "２"),
        ("a: https://example.com/x", "https://example.com/x"),
    ],
)
def test_plain_scalar_type_resolution(text: str, expected: object) -> None:
    """プレーンスカラーを想定どおりの Python 型へ解決する。"""
    assert parse_yaml(text) == {"a": expected}


@pytest.mark.parametrize("literal", ["yes", "no", "on", "off", "Yes", "No", "On", "Off", "y", "n"])
def test_yaml11_booleans_stay_strings(literal: str) -> None:
    """yes/no/on/off は bool にせず str のまま扱う（pyyaml との意図的な差分）。"""
    assert parse_yaml(f"a: {literal}") == {"a": literal}


def test_int_one_is_not_bool() -> None:
    """1 は int として解決され bool にはならない。"""
    result = parse_yaml("a: 1")
    assert result == {"a": 1}
    assert isinstance(result["a"], int)
    assert not isinstance(result["a"], bool)


def test_int_zero_is_not_bool() -> None:
    """0 は int として解決され bool にはならない。"""
    assert not isinstance(parse_yaml("a: 0")["a"], bool)


def test_bool_true_is_bool() -> None:
    """true リテラルは bool として解決される。"""
    assert isinstance(parse_yaml("a: true")["a"], bool)


def test_numeric_key_becomes_string() -> None:
    """数値に見えるキーも常に str として保持する。"""
    result = parse_yaml("1: x\nname: a")
    assert result == {"1": "x", "name": "a"}
    assert all(isinstance(key, str) for key in result)


def test_bool_like_key_becomes_string() -> None:
    """true に見えるキーも str として保持する。"""
    assert list(parse_yaml("true: x")) == ["true"]


def test_empty_document_returns_none() -> None:
    """空文書は None を返す。"""
    assert parse_yaml("") is None


def test_blank_and_comment_only_document_returns_none() -> None:
    """空行と行全体コメントだけの文書は None を返す。"""
    assert parse_yaml("\n\n# only a comment\n\n") is None


def test_toplevel_scalar_is_returned_as_is() -> None:
    """最上位がスカラーならその値をそのまま返す。"""
    assert parse_yaml("plain text") == "plain text"


# --------------------------------------------------------------------------
# コメント除去
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a: v # note", "v"),
        ("a: v\t# note", "v"),
        ("a: v   # note # more", "v"),
        ("a: issue#123", "issue#123"),
        ("a: 1 # note", 1),
        ('a: "v # x"', "v # x"),
        ("a: 'v # x'", "v # x"),
    ],
)
def test_comment_stripping(text: str, expected: object) -> None:
    """空白直前の # 以降だけをコメントとして落とす。"""
    assert parse_yaml(text) == {"a": expected}


def test_full_line_comment_is_skipped() -> None:
    """行全体コメントは読み飛ばす。"""
    assert parse_yaml("# lead\na: 1\n  # inner\nb: 2") == {"a": 1, "b": 2}


def test_blank_lines_between_entries_are_skipped() -> None:
    """エントリ間の空行は読み飛ばす。"""
    assert parse_yaml("a: 1\n\n\nb: 2") == {"a": 1, "b": 2}


def test_comment_only_value_becomes_none() -> None:
    """値がコメントだけならブロック値として扱い None になる。"""
    assert parse_yaml("a: # note\nb: 1") == {"a": None, "b": 1}


def test_comment_after_flow_collection_is_ignored() -> None:
    """フローコレクションの後ろのコメントは無視する。"""
    assert parse_yaml("a: [1, 2] # note") == {"a": [1, 2]}


def test_comment_after_quoted_value_is_ignored() -> None:
    """引用符付き値の後ろのコメントは無視する。"""
    assert parse_yaml('a: "x" # note') == {"a": "x"}


def test_comment_before_any_key_separator_makes_line_a_scalar() -> None:
    """キー区切りより先にコメントが現れる行はマッピングではなくスカラーとして扱う。"""
    assert parse_yaml("plain # note") == "plain"


def test_comment_before_key_separator_in_mapping_raises() -> None:
    """マッピング内でコロンがコメントより後ろにある行はエラーにする。"""
    with pytest.raises(FrontmatterError):
        parse_yaml("a: 1\nb # note: c")


def test_comment_after_block_scalar_header_is_ignored() -> None:
    """ブロックスカラーヘッダの後ろのコメントは無視する。"""
    assert parse_yaml("a: | # note\n  x") == {"a": "x\n"}


# --------------------------------------------------------------------------
# クォート
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a: ''", ""),
        ('a: ""', ""),
        ("a: 'plain'", "plain"),
        ('a: "plain"', "plain"),
        ("a: 'it''s'", "it's"),
        (r"a: 'back\slash'", r"back\slash"),
        ("a: '123'", "123"),
        ('a: "123"', "123"),
        ('a: "true"', "true"),
        ("a: '  padded  '", "  padded  "),
        (r'a: "tab\there"', "tab\there"),
        (r'a: "nl\nhere"', "nl\nhere"),
        (r'a: "cr\rhere"', "cr\rhere"),
        (r'a: "bs\bhere"', "bs\bhere"),
        (r'a: "ff\fhere"', "ff\fhere"),
        (r'a: "nul\0here"', "nul\0here"),
        (r'a: "quote\"here"', 'quote"here'),
        (r'a: "slash\/here"', "slash/here"),
        (r'a: "esc\\here"', "esc\\here"),
        (r'a: "hex\x41here"', "hexAhere"),
        (r'a: "uni\u00e9here"', "unié" + "here"),
    ],
)
def test_quoted_scalars(text: str, expected: str) -> None:
    """引用符付きスカラーは型解決せず文字列として解釈する。"""
    assert parse_yaml(text) == {"a": expected}


def test_quoted_key_is_unquoted() -> None:
    """引用符付きキーは引用符を外して解釈する。"""
    assert parse_yaml("'a b': 1") == {"a b": 1}


def test_double_quoted_key_is_unquoted() -> None:
    """二重引用符付きキーも引用符を外して解釈する。"""
    assert parse_yaml('"a:b": 1') == {"a:b": 1}


@pytest.mark.parametrize(
    "text",
    [
        "a: 'unterminated",
        'a: "unterminated',
        "'unterminated: 1",
        r'a: "bad\qescape"',
        r'a: "bad\xZZhex"',
        r'a: "short\x4"',
        r'a: "short\u12"',
        'a: "trailing\\',
    ],
)
def test_quoted_scalar_errors(text: str) -> None:
    """未終端の引用符と不正なエスケープはエラーにする。"""
    with pytest.raises(FrontmatterError):
        parse_yaml(text)


# --------------------------------------------------------------------------
# フローコレクション
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a: [Read, Write]", ["Read", "Write"]),
        ("a: [a, b]", ["a", "b"]),
        ("a: []", []),
        ("a: [ ]", []),
        ("a: [1, 2,]", [1, 2]),
        ("a: [1,2]", [1, 2]),
        ("a: ['x', \"y\"]", ["x", "y"]),
        ("a: [true, null, 1.5]", [True, None, 1.5]),
        ("a: {}", {}),
        ("a: {k: v}", {"k": "v"}),
        ("a: {k: 1,}", {"k": 1}),
        ("a: {'k k': 1}", {"k k": 1}),
        ("a: {k: v, j: [1, 2]}", {"k": "v", "j": [1, 2]}),
        ("a: [[1], {k: 2}]", [[1], {"k": 2}]),
        ("a: [[[1]]]", [[[1]]]),
    ],
)
def test_flow_collections(text: str, expected: object) -> None:
    """フローシーケンスとフローマッピングを相互ネスト込みで解析する。"""
    assert parse_yaml(text) == {"a": expected}


def test_toplevel_flow_mapping() -> None:
    """最上位のフローマッピングをそのまま解析する。"""
    assert parse_yaml("{k: v}") == {"k": "v"}


def test_flow_sequence_unterminated_raises() -> None:
    """未終端のフローシーケンスはエラーにする（既存テストの必須条件）。"""
    with pytest.raises(FrontmatterError):
        parse_yaml("name: [")


@pytest.mark.parametrize(
    "text",
    [
        "a: [",
        "a: [1",
        "a: [1,",
        "a: [1}",
        "a: [,]",
        "a: [a,,b]",
        "a: [a: 1]",
        "a: [a:]",
        'a: ["x" "y"]',
        "a: {",
        "a: {k",
        "a: {k}",
        "a: {k:",
        "a: {: 1}",
        "a: {&x: 1}",
        "a: {k: 1, k: 2}",
    ],
)
def test_flow_collection_errors(text: str) -> None:
    """フローの未終端・空要素・暗黙マップ・重複キーはエラーにする。"""
    with pytest.raises(FrontmatterError):
        parse_yaml(text)


def test_flow_depth_limit_exceeded_raises() -> None:
    """33 段ネストのフローは深さ上限を超えてエラーになる。"""
    with pytest.raises(FrontmatterError):
        parse_yaml("a: " + "[" * 33 + "]" * 33)


def test_flow_depth_within_limit_is_accepted() -> None:
    """上限内のネストしたフローは解析できる。"""
    value = parse_yaml("a: " + "[" * 20 + "]" * 20)["a"]
    for _ in range(19):
        assert isinstance(value, list)
        value = value[0]
    assert value == []


# --------------------------------------------------------------------------
# ブロック構造
# --------------------------------------------------------------------------


def test_nested_block_mapping_does_not_leak_to_toplevel() -> None:
    """入れ子のキーが最上位へ漏れない（既存テストの必須条件）。"""
    assert parse_yaml("metadata:\n  owner: team-a") == {"metadata": {"owner": "team-a"}}


def test_block_mapping_resumes_after_nested_block() -> None:
    """入れ子ブロックの後に同階層のエントリへ戻れる。"""
    assert parse_yaml("a:\n  b: 1\nc: 2") == {"a": {"b": 1}, "c": 2}


def test_deeply_nested_block_mapping() -> None:
    """3 段以上のネストも解析できる。"""
    assert parse_yaml("a:\n  b:\n    c: 1") == {"a": {"b": {"c": 1}}}


def test_block_mapping_with_varied_indent_width() -> None:
    """インデント幅は最初の子行に合わせて決まる。"""
    assert parse_yaml("a:\n    b: 1\n    c: 2") == {"a": {"b": 1, "c": 2}}


def test_empty_value_at_end_of_document_becomes_none() -> None:
    """値が無いまま文書が終わるキーは None になる。"""
    assert parse_yaml("a:") == {"a": None}


def test_empty_value_followed_by_sibling_becomes_none() -> None:
    """深い行が続かないキーは None になる。"""
    assert parse_yaml("a:\nb: 1") == {"a": None, "b": 1}


def test_empty_key_is_allowed() -> None:
    """コロンで始まる行は空文字列キーとして解釈する。"""
    assert parse_yaml(": v") == {"": "v"}


def test_toplevel_block_sequence() -> None:
    """最上位のブロックシーケンスは list になる（既存テストの必須条件）。"""
    assert parse_yaml("- item") == ["item"]


def test_block_sequence_with_multiple_items() -> None:
    """複数のシーケンス要素を順に解析する。"""
    assert parse_yaml("- a\n- b\n- 3") == ["a", "b", 3]


def test_block_sequence_under_key() -> None:
    """キー配下のより深いブロックシーケンスを解析する。"""
    assert parse_yaml("a:\n  - x\n  - y\nb: 1") == {"a": ["x", "y"], "b": 1}


def test_block_sequence_compact_mapping() -> None:
    """`- k: v` のコンパクトマップを解析する。"""
    assert parse_yaml("- k: 1\n  j: 2\n- k: 3") == [{"k": 1, "j": 2}, {"k": 3}]


def test_block_sequence_nested_sequence() -> None:
    """ダッシュだけの行に続く深いシーケンスを入れ子として解析する。"""
    assert parse_yaml("-\n  - x") == [["x"]]


def test_bare_dash_without_value_becomes_none() -> None:
    """値の無いダッシュだけの要素は None になる。"""
    assert parse_yaml("-") == [None]


def test_block_sequence_with_wide_dash_spacing() -> None:
    """ダッシュ後の空白が複数でも要素内容を正しく取る。"""
    assert parse_yaml("-   item") == ["item"]


def test_multiline_plain_scalar_is_joined_with_space() -> None:
    """深いインデントの継続行は空白 1 個で連結する。"""
    assert parse_yaml("a: foo\n  bar\n  baz") == {"a": "foo bar baz"}


def test_multiline_plain_scalar_stops_at_blank_line() -> None:
    """空行でプレーンスカラーの継続は終わる。"""
    assert parse_yaml("a: foo\n\nb: 1") == {"a": "foo", "b": 1}


def test_multiline_plain_scalar_stops_at_comment_line() -> None:
    """行全体コメントでプレーンスカラーの継続は終わる。"""
    assert parse_yaml("a: foo\n  # note\nb: 1") == {"a": "foo", "b": 1}


def test_multiline_plain_scalar_in_sequence_item() -> None:
    """シーケンス要素のプレーンスカラーも継続行を連結する。"""
    assert parse_yaml("- a\n  b") == ["a b"]


def test_block_depth_limit_exceeded_raises() -> None:
    """33 段ネストのブロックマッピングは深さ上限を超えてエラーになる。"""
    with pytest.raises(FrontmatterError):
        parse_yaml(_nested_mapping(33))


def test_block_depth_within_limit_is_accepted() -> None:
    """32 段ネストのブロックマッピングは受け入れる。"""
    result = parse_yaml(_nested_mapping(32))
    for _ in range(32):
        assert isinstance(result, dict)
        result = result["k"]
    assert result is None


# --------------------------------------------------------------------------
# ブロックスカラー
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a: |\n  x\n  y", "x\ny\n"),
        ("a: |-\n  x\n  y", "x\ny"),
        ("a: |+\n  x\n\nb: 1", "x\n\n"),
        ("a: |\n  x\n\n  y", "x\n\ny\n"),
        ("a: >\n  x\n  y", "x y\n"),
        ("a: >-\n  x\n  y", "x y"),
        ("a: >+\n  x\n\nb: 1", "x\n\n"),
        ("a: >\n  x\n\n  y", "x\ny\n"),
        ("a: >\n  x\n\n\n  y", "x\n\ny\n"),
        ("a: >\n  x\n   y\n  z", "x\n y\nz\n"),
        ("a: >\n  x\n\n   y", "x\n\n y\n"),
        ("a: >\n  x\n   y\n\n  z", "x\n y\n\nz\n"),
        ("a: |\n    deep", "deep\n"),
        ("a: >\nb: 1", ""),
        ("a: |\nb: 1", ""),
    ],
)
def test_block_scalars(text: str, expected: str) -> None:
    """ブロックスカラーの折り畳みとチョンピングが pyyaml 準拠になる。"""
    assert parse_yaml(text)["a"] == expected


def test_folded_block_scalar_at_toplevel() -> None:
    """最上位の折り畳みブロックスカラーは末尾改行付きの文字列になる。"""
    assert parse_yaml(">\n  a\n  b") == "a b\n"


def test_literal_block_scalar_at_toplevel() -> None:
    """最上位のリテラルブロックスカラーは行ごとに改行を保つ。"""
    assert parse_yaml("|\n  a\n  b") == "a\nb\n"


def test_block_scalar_trailing_blank_lines_are_clipped() -> None:
    """clip では末尾の空行を 1 個の改行へ畳む。"""
    assert parse_yaml("a: |\n  x\n\n\nb: 1") == {"a": "x\n", "b": 1}


def test_block_scalar_stops_at_shallower_indent() -> None:
    """コンテンツより浅い行でブロックスカラーは終わる。"""
    assert parse_yaml("a: |\n  x\nb: 1") == {"a": "x\n", "b": 1}


@pytest.mark.parametrize("header", ["a: |2\n  x", "a: >2\n  x", "a: |2-\n  x"])
def test_block_scalar_explicit_indent_indicator_raises(header: str) -> None:
    """明示インデント指示子はサポートせずエラーにする。"""
    with pytest.raises(FrontmatterError):
        parse_yaml(header)


def test_block_scalar_dedent_below_content_indent_raises() -> None:
    """コンテンツインデントより浅い中途半端な行はエラーになる。"""
    with pytest.raises(FrontmatterError):
        parse_yaml("a: |\n    x\n  y")


def test_block_scalar_tab_indent_raises() -> None:
    """ブロックスカラー本文のタブインデントはエラーにする。"""
    with pytest.raises(FrontmatterError):
        parse_yaml("a: |\n\tx")


# --------------------------------------------------------------------------
# エラー条件 E1-E14
# --------------------------------------------------------------------------


_ERROR_INPUTS = [
    pytest.param("name: [", id="E1-unterminated-flow"),
    pytest.param("a: 'x", id="E2-unterminated-quote"),
    pytest.param(r'a: "x\q"', id="E3-unknown-escape"),
    pytest.param("\ta: 1", id="E4-tab-indent-root"),
    pytest.param("a: 1\n\tb: 2", id="E4-tab-indent-entry"),
    pytest.param("a: b\n\tc", id="E4-tab-indent-continuation"),
    pytest.param("a:\n\tb: 1", id="E4-tab-indent-block-value"),
    pytest.param("a: [1]\n  b: 2", id="E5-unexpected-indent"),
    pytest.param("a:\n  b: 1\n c: 2", id="E5-bad-dedent"),
    pytest.param("  a: 1\nb: 2", id="E5-shallower-than-root"),
    pytest.param("a: 1\nnocolon", id="E6-missing-colon"),
    pytest.param("a: foo: bar", id="E7-colon-in-plain"),
    pytest.param("a: foo:", id="E7-trailing-colon-in-plain"),
    pytest.param("a: 1\na: 2", id="E8-duplicate-key"),
    pytest.param(_nested_mapping(33), id="E9-depth-block"),
    pytest.param("a: " + "[" * 33 + "]" * 33, id="E9-depth-flow"),
    pytest.param("a: &anchor", id="E10-anchor"),
    pytest.param("a: *alias", id="E10-alias"),
    pytest.param("a: !!str x", id="E10-tag"),
    pytest.param("&anchor: 1", id="E10-anchor-key"),
    pytest.param("? a\n: b", id="E11-complex-key"),
    pytest.param("a: 1\n? b\n: c", id="E11-complex-key-entry"),
    pytest.param("a: |2\n  x", id="E12-explicit-indent-indicator"),
    pytest.param("a: [1] junk", id="E13-trailing-token-flow"),
    pytest.param('a: "x" junk', id="E13-trailing-token-quote"),
    pytest.param("'a' b: 1", id="E13-trailing-token-key"),
    pytest.param("- a\nb: 1", id="sequence-entry-without-dash"),
    pytest.param("- [1]\n   - b", id="sequence-entry-bad-indent"),
    pytest.param('"abc', id="unterminated-quote-in-key-scan"),
]


@pytest.mark.parametrize("text", _ERROR_INPUTS)
def test_strict_mode_raises_frontmatter_error(text: str) -> None:
    """厳格モードでは解釈できない構文をすべて FrontmatterError にする。"""
    with pytest.raises(FrontmatterError):
        parse_yaml(text)


@pytest.mark.parametrize("text", _ERROR_INPUTS)
def test_lenient_mode_never_raises(text: str) -> None:
    """寛容モードでは E 群のどの入力でも例外を送出しない。"""
    parse_yaml(text, lenient=True)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("# lead\na: 1", {"a": 1}),
        ("\na: 1\n\n", {"a": 1}),
        ("a: 1\n   # inner comment\nb: 2", {"a": 1, "b": 2}),
    ],
)
def test_blank_and_comment_lines_are_tolerated(text: str, expected: dict[str, object]) -> None:
    """E14: 空行と行全体コメントはエラーにせず読み飛ばす。"""
    assert parse_yaml(text) == expected


# --------------------------------------------------------------------------
# 寛容モード
# --------------------------------------------------------------------------


def test_lenient_skips_line_without_colon() -> None:
    """コロンの無い行を読み飛ばして部分結果を返す（validate_agents の必須条件）。"""
    assert parse_yaml("name: x\nnocolon", lenient=True) == {"name": "x"}


def test_lenient_skips_failed_entry_and_its_deeper_lines() -> None:
    """失敗したエントリに続く深いインデント行もまとめて捨てる。"""
    text = "name: x\nbad\n  deeper\n\n  more\nkind: y"
    assert parse_yaml(text, lenient=True) == {"name": "x", "kind": "y"}


def test_lenient_keeps_first_duplicate_key() -> None:
    """重複キーは先勝ちで黙認する。"""
    assert parse_yaml("a: 1\na: 2", lenient=True) == {"a": 1}


def test_lenient_keeps_first_duplicate_key_in_flow_mapping() -> None:
    """フローマッピングの重複キーも先勝ちで黙認する。"""
    assert parse_yaml("a: {k: 1, k: 2}", lenient=True) == {"a": {"k": 1}}


def test_lenient_returns_none_when_root_fails() -> None:
    """最上位そのものが解析できなければ None を返す。"""
    assert parse_yaml("\ta: 1", lenient=True) is None


def test_lenient_ignores_trailing_shallower_block() -> None:
    """最上位より浅い後続行は無視して部分結果を返す。"""
    assert parse_yaml("  a: 1\nb: 2", lenient=True) == {"a": 1}


def test_lenient_drops_broken_sequence_entry() -> None:
    """壊れたシーケンス要素だけを捨てて残りを返す。"""
    assert parse_yaml("- a\nb: 1", lenient=True) == ["a"]


def test_lenient_drops_entry_with_broken_flow_value() -> None:
    """値が壊れているエントリだけを捨てる。"""
    assert parse_yaml("name: x\nbroken: [\nkind: y", lenient=True) == {"name": "x", "kind": "y"}


def test_lenient_matches_strict_for_valid_input() -> None:
    """正しい入力では厳格モードと同じ結果になる。"""
    text = "name: sample\nallowed-tools: [Read, Write]\nmetadata:\n  owner: team-a"
    assert parse_yaml(text, lenient=True) == parse_yaml(text)


# --------------------------------------------------------------------------
# 実データ相当の統合ケース
# --------------------------------------------------------------------------


def test_realistic_skill_frontmatter() -> None:
    """実データ相当の SKILL.md frontmatter をまとめて解析する。"""
    content = (
        "---\n"
        "name: sample-skill\n"
        "description: sample description\n"
        "license: MIT\n"
        "allowed-tools: [Read, Write]\n"
        "metadata:\n"
        "  owner: team-a\n"
        "compatibility: claude-code\n"
        "user-invocable: true\n"
        "---\n"
        "# Body\n"
    )
    assert parse_yaml(split_frontmatter(content)) == {
        "name": "sample-skill",
        "description": "sample description",
        "license": "MIT",
        "allowed-tools": ["Read", "Write"],
        "metadata": {"owner": "team-a"},
        "compatibility": "claude-code",
        "user-invocable": True,
    }


def test_realistic_folded_description_needs_caller_strip() -> None:
    """折り畳み description は YAML 準拠で末尾改行を残す（呼び出し元で strip する前提）。"""
    content = "---\nname: sample-skill\ndescription: >\n  first line\n  second line\n---\n# Body\n"
    parsed = parse_yaml(split_frontmatter(content))
    assert parsed["description"] == "first line second line\n"
    assert parsed["description"].strip() == "first line second line"
