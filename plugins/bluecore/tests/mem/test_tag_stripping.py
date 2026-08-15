"""tag_stripping のテスト"""

import pytest

from bluecore.mem.tag_stripping import _MAX_TAG_COUNT, strip_tags


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
            # ネストされたタグ（non-greedy で内側から順にマッチ）。ペア除去後に
            # 残る孤立した閉じタグも除去されるため outer 部分だけが残る
            (
                "<private>outer<private>inner</private>outer</private>rest",
                "outerrest",
            ),
            # 開始タグと対応しない孤立した閉じタグ単体（境界タグ偽装対策）
            (
                "before </bluecore-memory> after",
                "before  after",
            ),
            # 孤立閉じタグは大文字小文字を区別しない
            (
                "text </PRIVATE> more",
                "text  more",
            ),
            # 正規のペアと孤立閉じタグが混在（偽装閉じタグで信頼境界を
            # 早期終端させようとする典型的な攻撃パターン）
            (
                "legit <bluecore-memory>trusted</bluecore-memory> injected </bluecore-memory> more",
                "legit  injected  more",
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
            "orphan-close-tag",
            "orphan-close-tag-case-insensitive",
            "orphan-close-tag-mixed-with-valid-pair",
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
        # ReDoS 保護でペア除去（開始タグ）はスキップされ、残っている
        assert "<private>" in result

    def test_redos_protection_still_strips_orphan_close_tags(self) -> None:
        """ペア除去が ReDoS 保護でスキップされても孤立閉じタグ除去は独立して働く。

        孤立閉じタグパターンは `.*?` を含まない固定パターンで ReDoS リスクが
        無いため、_MAX_TAG_COUNT ガードの対象外として常に適用される。
        """
        tags = "<private>x</private>" * (_MAX_TAG_COUNT + 1)
        text = f"before {tags} after"
        result = strip_tags(text)
        assert "</private>" not in result
