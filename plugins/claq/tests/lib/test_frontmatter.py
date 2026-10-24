"""claq.lib.frontmatter の単体テスト（厳格モードと寛容モード）。"""

from __future__ import annotations

import pytest

from claq.lib.frontmatter import (
    FrontmatterError,
    MissingFrontmatterError,
    UnterminatedFrontmatterError,
    parse_yaml,
    split_frontmatter,
)


def _nested_mapping(levels: int) -> str:
    """levels 段だけネストしたブロックマッピングのテキストを組み立てる。"""
    return "\n".join(" " * level + "k:" for level in range(levels))


def _nested_dict(levels: int) -> dict[str, object]:
    """levels 段だけネストした ``{"k": {...}}`` を組み立てる（深さ超過時の寛容モードの部分結果）。"""
    result: dict[str, object] = {}
    for _ in range(levels):
        result = {"k": result}
    return result


# --------------------------------------------------------------------------
# split_frontmatter
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        pytest.param("---\nname: x\n---\nbody\n", "name: x", id="block-between-fences"),
        pytest.param("---\na: 1\nb: 2\n---\n", "a: 1\nb: 2", id="multiline"),
        pytest.param("---\n---\n", "", id="empty-block"),
        pytest.param("\ufeff---\r\nname: x\r\n---\r\nbody", "name: x", id="bom-and-crlf"),
        pytest.param("---\ra: 1\r---\rbody", "a: 1", id="lone-cr"),
        # 行末空白は許容する（列 0 から始まっていれば良い）。
        pytest.param("---  \nname: x\n---  \n", "name: x", id="trailing-spaces-on-fence"),
    ],
)
def test_split_frontmatter_returns_block(content: str, expected: str) -> None:
    """BOM 除去と改行正規化のあと、列 0 のフェンスに挟まれた部分だけを返す。"""
    assert split_frontmatter(content) == expected


@pytest.mark.parametrize(
    ("content", "error"),
    [
        pytest.param("name: x\n---\n", MissingFrontmatterError, id="missing-start-fence"),
        pytest.param("---extra\nname: x\n---\n", MissingFrontmatterError, id="fence-with-trailing-text"),
        pytest.param("---\nname: x\nbody\n", UnterminatedFrontmatterError, id="missing-end-fence"),
        # strip() 一致で探すと列 0 を要求せず、内側のインデント付き --- で切ってしまう。
        pytest.param("---\nname: x\n  ---\n", UnterminatedFrontmatterError, id="indented-dashes-are-not-fence"),
    ],
)
def test_split_frontmatter_rejects(content: str, error: type[FrontmatterError]) -> None:
    """フェンスが欠ける・列 0 の ``---`` だけの行でない入力は種類別の例外にする。"""
    with pytest.raises(error):
        split_frontmatter(content)


def test_split_frontmatter_keeps_indented_dashes_inside_block_scalar() -> None:
    """ブロックスカラー内の ``---`` で frontmatter が途中終了しないこと。

    列 0 を要求しない照合では ``  ---`` で切られ、``name`` と ``tools`` が本文へ落ちて
    frontmatter を読む側（validate_agents 等）から必須キーが黙って消える。
    """
    content = "---\ndescription: |\n  ---\nname: x\ntools: Read\n---\nbody\n"

    assert split_frontmatter(content) == "description: |\n  ---\nname: x\ntools: Read"
    assert parse_yaml(split_frontmatter(content)) == {"description": "---\n", "name": "x", "tools": "Read"}


def test_split_frontmatter_errors_derive_from_frontmatter_error() -> None:
    """両エラーとも FrontmatterError（ひいては ValueError）派生である。"""
    assert issubclass(MissingFrontmatterError, FrontmatterError)
    assert issubclass(UnterminatedFrontmatterError, FrontmatterError)
    assert issubclass(FrontmatterError, ValueError)


# --------------------------------------------------------------------------
# 値 1 つ（キー a の値）の解釈
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a: 1", 1),
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
    """プレーンスカラーを想定どおりの値と型へ解決する（1 / 0 は bool にならない）。"""
    result = parse_yaml(text)

    assert result == {"a": expected}
    assert type(result["a"]) is type(expected)


@pytest.mark.parametrize("literal", ["yes", "no", "on", "off", "Yes", "No", "On", "Off", "y", "n"])
def test_yaml11_booleans_stay_strings(literal: str) -> None:
    """yes/no/on/off は bool にせず str のまま扱う（pyyaml との意図的な差分）。"""
    assert parse_yaml(f"a: {literal}") == {"a": literal}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 空白直前の # 以降だけをコメントとして落とす。
        ("a: v # note", "v"),
        ("a: v\t# note", "v"),
        ("a: v   # note # more", "v"),
        ("a: issue#123", "issue#123"),
        ("a: 1 # note", 1),
        ('a: "v # x"', "v # x"),
        ("a: 'v # x'", "v # x"),
        ("a: [1, 2] # note", [1, 2]),
        ('a: "x" # note', "x"),
        ("a: | # note\n  x", "x\n"),
    ],
)
def test_comment_stripping(text: str, expected: object) -> None:
    """値の後ろのコメントを落とし、引用符内や空白の無い # は値として残す。"""
    assert parse_yaml(text) == {"a": expected}


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


def test_flow_depth_within_limit_is_accepted() -> None:
    """上限内のネストしたフローは解析できる。"""
    value = parse_yaml("a: " + "[" * 20 + "]" * 20)["a"]
    for _ in range(19):
        assert isinstance(value, list)
        value = value[0]
    assert value == []


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


# --------------------------------------------------------------------------
# 文書全体の構造
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 最上位の形
        pytest.param("", None, id="empty-document"),
        pytest.param("\n\n# only a comment\n\n", None, id="blank-and-comment-only"),
        pytest.param("plain text", "plain text", id="toplevel-scalar"),
        pytest.param("plain # note", "plain", id="comment-before-key-separator-is-scalar"),
        pytest.param("{k: v}", {"k": "v"}, id="toplevel-flow-mapping"),
        pytest.param(">\n  a\n  b", "a b\n", id="toplevel-folded-block-scalar"),
        pytest.param("|\n  a\n  b", "a\nb\n", id="toplevel-literal-block-scalar"),
        # キーは常に str（数値・bool に見えても型解決しない）
        pytest.param("1: x\nname: a", {"1": "x", "name": "a"}, id="numeric-key"),
        pytest.param("true: x", {"true": "x"}, id="bool-like-key"),
        pytest.param("'a b': 1", {"a b": 1}, id="single-quoted-key"),
        pytest.param('"a:b": 1', {"a:b": 1}, id="double-quoted-key"),
        pytest.param(": v", {"": "v"}, id="empty-key"),
        # E14: 空行と行全体コメントは読み飛ばす
        pytest.param("# lead\na: 1\n  # inner\nb: 2", {"a": 1, "b": 2}, id="E14-full-line-comments"),
        pytest.param("# lead\na: 1", {"a": 1}, id="E14-leading-comment"),
        pytest.param("a: 1\n   # inner comment\nb: 2", {"a": 1, "b": 2}, id="E14-indented-inner-comment"),
        pytest.param("\na: 1\n\n", {"a": 1}, id="E14-surrounding-blank-lines"),
        pytest.param("a: 1\n\n\nb: 2", {"a": 1, "b": 2}, id="E14-blank-lines-between"),
        pytest.param("a: # note\nb: 1", {"a": None, "b": 1}, id="comment-only-value-is-none"),
        # ブロックマッピング
        pytest.param("metadata:\n  owner: team-a", {"metadata": {"owner": "team-a"}}, id="nested-does-not-leak"),
        pytest.param("a:\n  b: 1\nc: 2", {"a": {"b": 1}, "c": 2}, id="resumes-after-nested"),
        pytest.param("a:\n  b:\n    c: 1", {"a": {"b": {"c": 1}}}, id="three-levels"),
        pytest.param("a:\n    b: 1\n    c: 2", {"a": {"b": 1, "c": 2}}, id="indent-width-from-first-child"),
        pytest.param("a:", {"a": None}, id="empty-value-at-end"),
        pytest.param("a:\nb: 1", {"a": None, "b": 1}, id="empty-value-before-sibling"),
        # ブロックシーケンス
        pytest.param("- item", ["item"], id="toplevel-sequence"),
        pytest.param("- a\n- b\n- 3", ["a", "b", 3], id="sequence-items"),
        pytest.param("a:\n  - x\n  - y\nb: 1", {"a": ["x", "y"], "b": 1}, id="sequence-under-key"),
        pytest.param("- k: 1\n  j: 2\n- k: 3", [{"k": 1, "j": 2}, {"k": 3}], id="compact-mapping"),
        pytest.param("-\n  - x", [["x"]], id="nested-sequence"),
        pytest.param("-", [None], id="bare-dash"),
        pytest.param("-   item", ["item"], id="wide-dash-spacing"),
        # 複数行プレーンスカラーは空白 1 個で連結し、空行・コメント行で終わる
        pytest.param("a: foo\n  bar\n  baz", {"a": "foo bar baz"}, id="multiline-plain-joined"),
        pytest.param("a: foo\n\nb: 1", {"a": "foo", "b": 1}, id="multiline-plain-stops-at-blank"),
        pytest.param("a: foo\n  # note\nb: 1", {"a": "foo", "b": 1}, id="multiline-plain-stops-at-comment"),
        pytest.param("- a\n  b", ["a b"], id="multiline-plain-in-sequence"),
        # ブロックスカラーの終わり
        pytest.param("a: |\n  x\n\n\nb: 1", {"a": "x\n", "b": 1}, id="block-scalar-clips-trailing-blanks"),
        pytest.param("a: |\n  x\nb: 1", {"a": "x\n", "b": 1}, id="block-scalar-stops-at-shallower"),
    ],
)
def test_document_structure(text: str, expected: object) -> None:
    """文書全体を想定どおりの最上位値へ解析する。"""
    assert parse_yaml(text) == expected


def test_block_depth_within_limit_is_accepted() -> None:
    """32 段ネストのブロックマッピングは受け入れる。"""
    result = parse_yaml(_nested_mapping(32))
    for _ in range(32):
        assert isinstance(result, dict)
        result = result["k"]
    assert result is None


# --------------------------------------------------------------------------
# エラー条件
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # 引用符の未終端と不正なエスケープ
        "a: 'unterminated",
        'a: "unterminated',
        "'unterminated: 1",
        r'a: "bad\qescape"',
        r'a: "bad\xZZhex"',
        r'a: "short\x4"',
        r'a: "short\u12"',
        'a: "trailing\\',
        # フローの未終端・空要素・暗黙マップ
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
        # ブロックスカラーの明示インデント指示子・中途半端なデデント・タブインデント
        "a: >2\n  x",
        "a: |2-\n  x",
        "a: |\n    x\n  y",
        "a: |\n\tx",
        # マッピング内でコロンがコメントより後ろにある行
        "a: 1\nb # note: c",
    ],
)
def test_strict_mode_rejects_malformed_syntax(text: str) -> None:
    """解釈できない構文は FrontmatterError にする。"""
    with pytest.raises(FrontmatterError):
        parse_yaml(text)


# 解釈できない構文（厳格モードは FrontmatterError、寛容モードは部分結果）と、
# 寛容モードが返すべき値の対。値まで固定するのは「どこまで捨てたか」の変化を検出するため。
#
#   * 重複キーは寛容モードでも不合格にする。lenient は解釈できないエントリを読み飛ばす
#     緩和であって矛盾した宣言の黙認ではない。先勝ちで黙認すると検証器とホストで
#     見える値が分かれるパーサ差分になる。最上位では None、フロー側はエントリごと消える。
#   * multi-line-block-sequence: キーと同インデントのブロックシーケンスは YAML 1.2 で
#     妥当だが非対応で、寛容モードでは値ごと落ちて {'tools': None} になる（無警告の損失）。
#   * multi-document: 厳格モードは弾くが、寛容モードでは `---` がコロン無しの行として
#     読み飛ばされ、2 つのドキュメントが 1 つの dict へ合流する。
_ERROR_INPUTS = [
    pytest.param('name: [', {}, id='E1-unterminated-flow'),
    pytest.param("a: 'x", {}, id='E2-unterminated-quote'),
    pytest.param(r'a: "x\q"', {}, id='E3-unknown-escape'),
    pytest.param('\ta: 1', None, id='E4-tab-indent-root'),
    pytest.param('a: 1\n\tb: 2', {}, id='E4-tab-indent-entry'),
    pytest.param('a: b\n\tc', {}, id='E4-tab-indent-continuation'),
    pytest.param('a:\n\tb: 1', {}, id='E4-tab-indent-block-value'),
    pytest.param('a: [1]\n  b: 2', {'a': [1]}, id='E5-unexpected-indent'),
    pytest.param('a:\n  b: 1\n c: 2', {'a': {'b': 1}}, id='E5-bad-dedent'),
    pytest.param('  a: 1\nb: 2', {'a': 1}, id='E5-shallower-than-root'),
    pytest.param('a: 1\nnocolon', {'a': 1}, id='E6-missing-colon'),
    pytest.param('name: x\nnocolon', {'name': 'x'}, id='E6-missing-colon-name'),
    pytest.param('name: x\nbad\n  deeper\n\n  more\nkind: y', {'name': 'x', 'kind': 'y'}, id='E6-drops-deeper-lines'),
    pytest.param('a: foo: bar', {}, id='E7-colon-in-plain'),
    pytest.param('a: foo:', {}, id='E7-trailing-colon-in-plain'),
    pytest.param('a: 1\na: 2', None, id='E8-duplicate-key'),
    pytest.param('a: {k: 1, k: 2}', {}, id='E8-duplicate-key-in-flow'),
    pytest.param(_nested_mapping(33), _nested_dict(31), id='E9-depth-block'),
    pytest.param("a: " + "[" * 33 + "]" * 33, {}, id='E9-depth-flow'),
    pytest.param('a: &anchor', {}, id='E10-anchor'),
    pytest.param('a: *alias', {}, id='E10-alias'),
    pytest.param('a: !!str x', {}, id='E10-tag'),
    pytest.param('&anchor: 1', {}, id='E10-anchor-key'),
    pytest.param('? a\n: b', {'': 'b'}, id='E11-complex-key'),
    pytest.param('a: 1\n? b\n: c', {'a': 1, '': 'c'}, id='E11-complex-key-entry'),
    pytest.param('a: |2\n  x', {}, id='E12-explicit-indent-indicator'),
    pytest.param('a: [1] junk', {}, id='E13-trailing-token-flow'),
    pytest.param('a: "x" junk', {}, id='E13-trailing-token-quote'),
    pytest.param("'a' b: 1", {}, id='E13-trailing-token-key'),
    pytest.param('name: x\nbroken: [\nkind: y', {'name': 'x', 'kind': 'y'}, id='broken-flow-value-entry'),
    pytest.param('- a\nb: 1', ['a'], id='sequence-entry-without-dash'),
    pytest.param('- [1]\n   - b', [[1]], id='sequence-entry-bad-indent'),
    pytest.param('"abc', None, id='unterminated-quote-in-key-scan'),
    pytest.param('tools:\n- Read\n- Write', {'tools': None}, id='multi-line-block-sequence'),
    pytest.param('a: 1\n---\nb: 2', {'a': 1, 'b': 2}, id='multi-document'),
]


@pytest.mark.parametrize(("text", "lenient_expected"), _ERROR_INPUTS)
def test_strict_and_lenient_modes_on_malformed_syntax(text: str, lenient_expected: object) -> None:
    """厳格モードは FrontmatterError を送出し、寛容モードは宣言どおりの部分結果を返す。"""
    with pytest.raises(FrontmatterError):
        parse_yaml(text)
    assert parse_yaml(text, lenient=True) == lenient_expected


# --------------------------------------------------------------------------
# 実データ相当の統合ケース
# --------------------------------------------------------------------------


def test_realistic_skill_frontmatter() -> None:
    """実データ相当の SKILL.md frontmatter を厳格・寛容の両モードで同じ値へ解析する。"""
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
    expected = {
        "name": "sample-skill",
        "description": "sample description",
        "license": "MIT",
        "allowed-tools": ["Read", "Write"],
        "metadata": {"owner": "team-a"},
        "compatibility": "claude-code",
        "user-invocable": True,
    }

    assert parse_yaml(split_frontmatter(content)) == expected
    assert parse_yaml(split_frontmatter(content), lenient=True) == expected


def test_realistic_folded_description_needs_caller_strip() -> None:
    """折り畳み description は YAML 準拠で末尾改行を残す（呼び出し元で strip する前提）。"""
    content = "---\nname: sample-skill\ndescription: >\n  first line\n  second line\n---\n# Body\n"
    assert parse_yaml(split_frontmatter(content))["description"] == "first line second line\n"
