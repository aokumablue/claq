"""tag_stripping のテスト"""

import pytest

from devgear.mem.tag_stripping import _MAX_TAG_COUNT, strip_tags


class TestStripTags:
    """タグストリップのテストケース"""

    @pytest.mark.parametrize(
        "input_text, expected",
        [
            # private タグ
            ("before <private>secret</private> after", "before  after"),
            # mem-context タグ
            (
                "hello <mem-context>ctx</mem-context> world",
                "hello  world",
            ),
            # system_instruction タグ
            (
                "<system_instruction>inst</system_instruction>content",
                "content",
            ),
            # system-instruction タグ（ハイフン区切り）
            (
                "<system-instruction>inst</system-instruction>content",
                "content",
            ),
            # 大文字小文字区別なし
            ("<PRIVATE>secret</PRIVATE>ok", "ok"),
            # 複数行コンテンツ
            (
                "a<private>\nline1\nline2\n</private>b",
                "ab",
            ),
            # 空文字列
            ("", ""),
            # タグなし
            ("no tags here", "no tags here"),
            # ネストされたタグ（non-greedy で内側から順にマッチ）
            (
                "<private>outer<private>inner</private>outer</private>rest",
                "outer</private>rest",
            ),
        ],
        ids=[
            "private",
            "mem-context",
            "system_instruction",
            "system-instruction",
            "case-insensitive",
            "multiline",
            "empty",
            "no-tags",
            "nested",
        ],
    )
    def test_strip(self, input_text: str, expected: str) -> None:
        result = strip_tags(input_text)
        assert result == expected

    def test_redos_protection(self) -> None:
        """タグが _MAX_TAG_COUNT を超える場合、そのパターンはスキップされる"""
        # _MAX_TAG_COUNT + 1 個の private タグを作成
        tags = "<private>x</private>" * (_MAX_TAG_COUNT + 1)
        text = f"before {tags} after"
        result = strip_tags(text)
        # ReDoS 保護で private タグはスキップされ、残っている
        assert "<private>" in result
