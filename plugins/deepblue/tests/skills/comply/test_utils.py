"""utils モジュールのテスト — LLM出力からのYAML抽出。

デシジョンテーブル:
  | # | 入力パターン                        | 先頭フェンス | 末尾フェンス | 期待結果                        |
  |---|-------------------------------------|------------|------------|-------------------------------|
  | 1 | フェンスなし平文YAML                  | なし       | なし       | 入力そのまま返却                |
  | 2 | ```yaml ... ``` で囲まれたYAML        | あり       | あり       | フェンスを除いた内側だけ返却      |
  | 3 | ``` のみ（言語指定なし）で囲まれた    | あり       | あり       | フェンスを除いた内側だけ返却      |
  | 4 | 先頭フェンスのみ（末尾なし）           | あり       | なし       | 先頭フェンスのみ除去して返却      |
  | 5 | 末尾フェンスのみ（先頭なし）           | なし       | あり       | 末尾フェンスのみ除去して返却      |
  | 6 | 空文字列                             | -          | -          | 空文字列返却                    |
  | 7 | フェンスの間に複数行YAML              | あり       | あり       | 中身の複数行を改行つきで返却      |
  | 8 | フェンス行に追加テキスト（```yaml）   | あり       | あり       | フェンスを除いた内側だけ返却      |
"""

from __future__ import annotations

from devgear.skills.comply.utils import extract_yaml


class TestExtractYaml:
    """extract_yaml 関数のデシジョンテーブルテスト。"""

    # ケース1: フェンスなし
    def test_no_fence_returns_as_is(self) -> None:
        """フェンスが付いていない場合は入力をそのまま返す。"""
        text = "id: test\nname: Test"
        assert extract_yaml(text) == text

    # ケース2: ```yaml ... ``` フェンスあり
    def test_yaml_fence_removed(self) -> None:
        """```yaml ``` で囲まれた場合フェンスを除去して中身だけ返す。"""
        text = "```yaml\nid: test\nname: Test\n```"
        result = extract_yaml(text)
        assert result == "id: test\nname: Test"

    # ケース3: ``` のみ（言語指定なし）
    def test_backtick_fence_no_lang_removed(self) -> None:
        """言語指定なしのバッククォートフェンスも除去される。"""
        text = "```\nid: test\n```"
        result = extract_yaml(text)
        assert result == "id: test"

    # ケース4: 先頭フェンスのみ
    def test_only_leading_fence_removed(self) -> None:
        """先頭フェンスのみがある場合は先頭フェンスだけ除去する。"""
        text = "```yaml\nid: test\nname: Test"
        result = extract_yaml(text)
        assert result == "id: test\nname: Test"

    # ケース5: 末尾フェンスのみ
    def test_only_trailing_fence_removed(self) -> None:
        """末尾フェンスのみがある場合は末尾フェンスだけ除去する。"""
        text = "id: test\nname: Test\n```"
        result = extract_yaml(text)
        assert result == "id: test\nname: Test"

    # ケース6: 空文字列
    def test_empty_string_returns_empty(self) -> None:
        """空文字列を渡した場合は空文字列を返す。"""
        assert extract_yaml("") == ""

    # ケース7: 複数行YAML
    def test_multiline_yaml_preserved(self) -> None:
        """フェンス内の複数行は改行を維持して返す。"""
        text = "```yaml\nid: test\nsteps:\n  - id: s1\n```"
        result = extract_yaml(text)
        assert result == "id: test\nsteps:\n  - id: s1"

    # ケース8: フェンス行に追加テキスト（```yaml）
    def test_fence_with_language_identifier(self) -> None:
        """```yaml のように言語識別子がついたフェンスも除去される。"""
        text = "```yaml\nkey: value\n```"
        result = extract_yaml(text)
        assert result == "key: value"

    def test_spaces_stripped_from_outer(self) -> None:
        """前後の空白・改行はストリップされてから処理される。"""
        text = "\n```yaml\nid: test\n```\n"
        result = extract_yaml(text)
        assert result == "id: test"

    def test_single_line_no_fence(self) -> None:
        """1行のみ・フェンスなしはそのまま返る。"""
        assert extract_yaml("id: only") == "id: only"
