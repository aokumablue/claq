"""tag_stripping の許可リスト処理（keep_allowed_chars / keep_allowed_chars_for_injection / 全角山括弧）のテスト。

制御文字・不可視文字・異体字セレクタ・ZWJ・全角山括弧の扱いを検査する。
``strip_tags`` と足場タグの無害化のテストは ``test_tag_stripping.py`` にある。
"""

import time
import unicodedata

import pytest

from claq.lib.slim_text import remove_filler_phrases
from claq.mem import tag_stripping
from claq.mem.tag_stripping import (
    _AllowlistTable,
    _compat_open_positions,
    keep_allowed_chars,
    keep_allowed_chars_for_injection,
    strip_tags,
)


class TestKeepAllowedChars:
    """``keep_allowed_chars``: 許可した文字だけを残す（L*・M*・N*・P*・S*・Zs とタブ・LF）。"""

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "日本語 English 123",
            "タブ\t付き\n二行目",
            "（全角）ＡＢＣ１２３　全角空白",
            "ｱｲｳ 半角カナ ㍿ ①",
            "か\u3099 ひらがなの結合濁点",
            "हिन्दी देवनागरी",
            "❤\ufe0f 😀 ☺\ufe0f 異体字セレクタ",
            "a\u00a0b NBSP",
            "{[(<>)]} ~`!@#$%^&*-_=+|\\;:'\",./? ASCII 記号",
        ],
    )
    def test_ordinary_text_is_unchanged(self, text: str) -> None:
        """日本語・英語・タブ・LF・全角・結合記号・異体字セレクタは 1 文字も変えない。"""
        assert keep_allowed_chars(text) == text

    @pytest.mark.parametrize(
        "ch",
        [
            "\u200b",  # ZWSP
            "\u200c",  # ZWNJ
            "\u200d",  # ZWJ
            "\u200e",  # LRM
            "\u202e",  # RLO
            "\u2066",  # LRI
            "\u2060",  # WORD JOINER
            "\U000e0041",  # タグ文字（不可視の ASCII 'A'）
            "\U000e0001",  # LANGUAGE TAG
            "\ufeff",  # BOM
            "\u00ad",  # ソフトハイフン
            "\u061c",  # ALM
            "\u0080",  # C1
            "\u009f",  # C1
            "\ue000",  # 私用領域（Co）
            "\u0378",  # 未割り当て（Cn）
            "\ud800",  # 孤立サロゲート（Cs）
            *[chr(c) for c in [*range(0x09), *range(0x0E, 0x20), 0x7F]],  # C0（タブ・LF・VT・FF・CR を除く）と DEL
        ],
    )
    def test_removes_characters_outside_the_allowlist(self, ch: str) -> None:
        """許可リスト外（Cf・Cc・Co・Cn・Cs）は 1 文字ずつ落ちる。"""
        assert keep_allowed_chars(f"a{ch}b") == "ab"

    @pytest.mark.parametrize("separator", ["\r\n", "\r", "\x85", "\u2028", "\u2029", "\x0b", "\x0c", "\n"])
    def test_line_separators_become_a_single_lf(self, separator: str) -> None:
        """CRLF・CR・NEL・LS・PS・VT・FF は LF 1 つに揃う（\\r\\n は 1 つの LF）。"""
        assert keep_allowed_chars(f"a{separator}b") == "a\nb"

    def test_crlf_pairs_are_not_doubled(self) -> None:
        """空行を挟んだ CRLF も、行数を変えずに LF へ揃う。"""
        assert keep_allowed_chars("a\r\n\r\nb\r\n") == "a\n\nb\n"

    def test_zwj_emoji_sequence_loses_only_the_joiners(self) -> None:
        """ZWJ 絵文字は ZWJ だけが落ち、構成要素の絵文字は残る。

        ZWJ を残すと、鍵を ZWJ で分断して ``redact`` をすり抜けられるため落とす。
        """
        assert keep_allowed_chars("👨\u200d👩\u200d👧") == "👨👩👧"

    def test_nfkc_is_not_used_to_rewrite_the_body(self) -> None:
        """互換文字は正規化しない（全角括弧・全角英数字・合字は表示のまま）。"""
        text = "（ＡＢＣ）㍿ﬁ"
        assert keep_allowed_chars(text) == text

    def test_every_codepoint_follows_the_category_rule(self) -> None:
        """全コードポイントについて、残る文字は許可カテゴリ・タブ・LF だけで、許可カテゴリは必ず残る。"""
        allowed = set()
        for cp in range(0x110000):
            ch = chr(cp)
            category = unicodedata.category(ch)
            if category[0] in "LMNPS" or category == "Zs":
                allowed.add(cp)
        line_breaks = {0x0B, 0x0C, 0x0D, 0x85, 0x2028, 0x2029}
        assert allowed.isdisjoint(line_breaks)
        invisible_but_allowed = {0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x3164, 0xFFA0, 0x2800, 0x16FE4}
        selectors = {0x180B, 0x180C, 0x180D, 0x180F, *range(0xFE00, 0xFE10), *range(0xE0100, 0xE01F0)}
        assert invisible_but_allowed <= allowed
        assert selectors <= allowed
        for cp in range(0x110000):
            result = keep_allowed_chars(chr(cp))
            if cp in invisible_but_allowed or cp in selectors:
                # 許可カテゴリだが、ASCII の間へ挟めば分断できる不可視文字。常に落とす 6 文字と、
                # 先頭（ASCII と同じ文脈）に置かれた異体字セレクタ。
                assert result == "", hex(cp)
            elif cp in allowed or cp in (0x09, 0x0A):
                assert result == chr(cp), hex(cp)
            elif cp in line_breaks:
                assert result == "\n", hex(cp)
            else:
                assert result == "", hex(cp)

    @pytest.mark.parametrize("ch", ["\u034f", "\u115f", "\u1160", "\u17b4", "\u17b5", "\u3164", "\uffa0", "\u2800", "\U00016fe4"])
    def test_invisible_fillers_are_always_removed(self, ch: str) -> None:
        """CGJ・ハングル filler・点字空白は、前後が何であっても落ちる（ASCII の間でも日本語の間でも）。"""
        assert keep_allowed_chars(f"a{ch}b") == "ab"
        assert keep_allowed_chars(f"日{ch}本") == "日本"
        assert keep_allowed_chars(f"{ch}x{ch}") == "x"

    @pytest.mark.parametrize("selector", ["\u180b", "\u180d", "\u180f", "\ufe00", "\ufe0e", "\ufe0f", "\U000e0100", "\U000e01ef"])
    def test_variation_selector_after_ascii_is_removed(self, selector: str) -> None:
        """異体字セレクタは直前が ASCII（改行・文頭を含む）なら落ち、連続していても全て落ちる。"""
        assert keep_allowed_chars(f"a{selector}b") == "ab"
        assert keep_allowed_chars(f"a{selector}{selector}b") == "ab"
        assert keep_allowed_chars(f"x\n{selector}## y") == "x\n## y"
        assert keep_allowed_chars(f"{selector}## y") == "## y"

    def test_variation_selector_after_a_removed_character_is_judged_on_the_output(self) -> None:
        """間の不可視文字が落ちた後の並びで判定する（CGJ や ZWSP を挟んでも ASCII の直後と同じ）。"""
        assert keep_allowed_chars("a\u034f\ufe0fb") == "ab"
        assert keep_allowed_chars("a​\ufe0f‍\U000e0100b") == "ab"

    @pytest.mark.parametrize(
        "text",
        [
            "❤\ufe0f",  # 絵文字の表示選択子
            "☺\ufe0e",  # テキスト表示選択子
            "葛\U000e0100",  # 日本語の人名の異体字（IVS）
            "\u1820\u180b",  # モンゴル文字 + 自由異体字セレクタ
            "渡\U000e0101辺",
            "1\ufe0f\u20e3",  # keycap
            "#\ufe0f\u20e3",
            "*\ufe0f\u20e3",
            "0\u20e3",  # FE0F の無い keycap
            "x 9\ufe0f\u20e3 y",
        ],
    )
    def test_variation_selector_after_non_ascii_and_keycaps_are_kept(self, text: str) -> None:
        """ASCII に付かない異体字セレクタ（絵文字・IVS）と keycap の並びは壊さない。"""
        assert keep_allowed_chars(text) == text

    def test_variation_selector_after_ascii_in_a_non_keycap_is_removed(self) -> None:
        """keycap 例外は ``[0-9#*]`` + FE0F? + U+20E3 の並びだけ。FE0F が重なる並びや U+20E3 の無い数字は落とす。"""
        assert keep_allowed_chars("1\ufe0f\ufe0f\u20e3") == "1\u20e3"
        assert keep_allowed_chars("a\ufe0f\u20e3") == "a\u20e3"
        assert keep_allowed_chars("1\ufe0f2") == "12"

    @pytest.mark.parametrize(
        "text",
        [
            "e\u0301",  # NFD のラテン文字（視認できる結合記号）
            "か\u3099",
            "ア\u3099",
            "हिन्दी",
            "a\u0301b",
        ],
    )
    def test_visible_combining_marks_stay_even_after_ascii(self, text: str) -> None:
        """視認できる結合記号（分音記号・濁点・母音記号）は ASCII に付いていても残す。"""
        assert keep_allowed_chars(text) == text

    def test_tag_split_by_a_combining_grapheme_joiner_is_neutralized(self) -> None:
        """CGJ で分断した ``</claq-memory>`` は、除去で 1 本に戻り strip_tags で倒れる。"""
        cleaned = keep_allowed_chars("<\u034f/claq-memory>")

        assert cleaned == "</claq-memory>"
        assert strip_tags(cleaned) == "&lt;/claq-memory>"

    @pytest.mark.parametrize(
        "filler",
        ["\u3164", "\u2800", "\u115f", "\u1160", "\uffa0", "\u034f", "\u17b4", "\u17b5", "\U00016fe4", "\u180b", "\u180f", "\ufe0f", "\U000e0100"],
    )
    def test_line_start_markers_behind_an_invisible_filler_are_exposed(self, filler: str) -> None:
        """行頭の ``## x`` ``- [x]`` を不可視文字で隠せない（除去後に行頭マーカーとして見える）。"""
        assert keep_allowed_chars(f"終\n{filler}## x\n{filler}- [x] y") == "終\n## x\n- [x] y"

    @pytest.mark.parametrize(
        "filler",
        ["\u034f", "\ufe0f", "\U000e0100", "\u3164", "\u2800", "\u115f", "\u1160", "\uffa0", "\u17b4", "\u17b5", "\U00016fe4", "\u180b", "\u180c", "\u180d", "\u180f"],
    )
    def test_secret_split_by_each_invisible_character_rejoins(self, filler: str) -> None:
        """CGJ・異体字セレクタ・ハングル filler・点字空白で分断した鍵も 1 本に戻る。"""
        assert keep_allowed_chars(f"sk-ant-{filler}api03-{filler}" + "A" * 20) == "sk-ant-api03-" + "A" * 20

    def test_table_does_not_cache_beyond_its_cap(self) -> None:
        """判定表の記憶は上限で止まり、上限を超えた文字も同じ規則で判定される。"""
        table = _AllowlistTable(max_entries=2)
        text = "ab\u200bc日本\u202e"

        assert text.translate(table) == "abc日本"
        assert len(table) == 2

    def test_secret_split_by_invisible_chars_rejoins(self) -> None:
        """不可視文字で分断された鍵は 1 本に戻る（後段の redact が見られる形になる）。"""
        assert keep_allowed_chars("sk-ant-\u200bapi03-\u200d" + "A" * 20) == "sk-ant-api03-" + "A" * 20


class TestKeepAllowedCharsForInjection:
    """``keep_allowed_chars_for_injection``: 注入の読み出し側。除去で再結合した鍵だけを伏せる。"""

    def test_text_without_removed_characters_is_returned_as_is(self) -> None:
        """除く文字が無ければ redact を走らせず、そのまま返す（base64 風の長い文字列も伏せない）。"""
        text = "x" * 60 + " 本文"
        assert keep_allowed_chars_for_injection(text) == text

    def test_removed_characters_without_a_secret_leave_the_body_alone(self) -> None:
        """除く文字があっても鍵が無ければ、除去しただけの本文を返す。"""
        assert keep_allowed_chars_for_injection("a\u200bb 👨\u200d👩") == "ab 👨👩"

    def test_secret_rejoined_by_removal_is_masked(self) -> None:
        """ZWSP で分断した鍵は、除去で再結合した形で伏せる（旧データが注入時に鍵へ戻らない）。"""
        key = "sk-ant-api03-" + "A" * 30
        result = keep_allowed_chars_for_injection(f"鍵 {key[:8]}\u200b{key[8:]} 以上")

        assert "A" * 30 not in result
        assert result == "鍵 [REDACTED] 以上"

    def test_secret_split_by_every_invisible_filler_is_masked(self) -> None:
        """CGJ・異体字セレクタ・IVS・ハングル filler・点字空白を混ぜて分断した鍵は、除去で再結合した形で伏せる。"""
        key = "sk-ant-api03-" + "A" * 30
        split = f"{key[:8]}\u034f{key[8:10]}\ufe0f{key[10:12]}\U000e0100{key[12:14]}\u3164{key[14:16]}\u2800{key[16:]}"

        assert keep_allowed_chars_for_injection(f"鍵 {split} 以上") == "鍵 [REDACTED] 以上"

    def test_secret_already_whole_is_left_to_the_write_side_redaction(self) -> None:
        """除去と無関係に本文へ入っている鍵は、除去と redact が可換なので本関数は触らない（伏せるのは書き込み側の redact）。"""
        key = "sk-ant-api03-" + "A" * 30
        assert keep_allowed_chars_for_injection(f"{key}\u200b") == key


class TestCompatBracketNeutralization:
    """NFKC で ``<`` になる文字で書かれた ``claq-memory`` タグの開き山括弧を ``&lt;`` にする。"""

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("＜/claq-memory＞", "&lt;/claq-memory＞"),
            ("＜／claq-memory＞", "&lt;／claq-memory＞"),
            ("＜claq-memory＞", "&lt;claq-memory＞"),
            ("﹤/claq-memory﹥", "&lt;/claq-memory﹥"),
            ("＜／ｃｌａｑ－ｍｅｍｏｒｙ＞", "&lt;／ｃｌａｑ－ｍｅｍｏｒｙ＞"),
            ("＜/CLAQ-MEMORY＞", "&lt;/CLAQ-MEMORY＞"),
            ("<／claq-memory>", "&lt;／claq-memory>"),
            ("<claq－memory>", "&lt;claq－memory>"),
            ("＜/claq-memory\u0301＞", "&lt;/claq-memory\u0301＞"),
            ("前＜/claq-memory＞後 ＜/claq-memory＞", "前&lt;/claq-memory＞後 &lt;/claq-memory＞"),
        ],
    )
    def test_opening_bracket_becomes_an_entity(self, text: str, expected: str) -> None:
        """全角・小書き・混在の表記でも、開きの山括弧だけが ``&lt;`` になる。"""
        assert strip_tags(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "＜重要＞",
            "＜ｃｌａｑ＞ ＜memory＞",
            "（ＡＢＣ）　全角記号と全角英数字",
            "＜system-reminder＞",
            "日本語のみの本文",
        ],
    )
    def test_other_fullwidth_text_is_unchanged(self, text: str) -> None:
        """``claq-memory`` を書いていない全角記号は、一切変えない。"""
        assert strip_tags(text) == text

    def test_every_codepoint_normalizing_to_a_bracket_is_covered(self) -> None:
        """NFKC で ``<`` になる文字の集合が、実装の集合と一致する（Unicode の更新で漏れを出さない）。"""
        derived = {chr(cp) for cp in range(0x110000) if unicodedata.normalize("NFKC", chr(cp)) == "<"}
        assert derived == set(tag_stripping._LT_LIKE)

    def test_filler_deletion_cannot_forge_a_fullwidth_tag(self) -> None:
        """埋め草削除で組み上がる全角タグも封じる（``＜まあ/claq-memory＞`` は削除後に ``＜/claq-memory＞``）。"""
        result = strip_tags("＜まあ/claq-memory＞ ＜重要＞")

        assert "＜" not in result
        assert not _compat_open_positions(remove_filler_phrases(result))

    def test_filler_deletion_cannot_forge_a_tag_with_ascii_bracket(self) -> None:
        """ASCII の ``<`` と全角の ``／`` の組も、削除後に組み上がる場合は倒す。"""
        result = strip_tags("<まあ／claq-memory>")

        assert not _compat_open_positions(remove_filler_phrases(result))
        assert "<" not in result

    def test_ascii_text_has_no_compat_positions(self) -> None:
        """ASCII のみの本文は走査せず、位置を返さない。"""
        assert _compat_open_positions("</claq-memory>") == []

    def test_long_non_ascii_text_without_the_tag_name_is_cheap(self) -> None:
        """タグ名を含まない 1MB の非 ASCII 本文は、位置の走査へ進まず短時間で終わる。"""
        text = "日本語の本文。" * 150_000
        start = time.perf_counter()
        assert _compat_open_positions(text) == []
        assert time.perf_counter() - start < 1.0

    def test_compat_open_positions_skips_a_source_that_expands_to_more_than_a_bracket(self) -> None:
        """正規化で複数文字に展開される文字（``㍿``）が直前にあっても、位置は元の文字に対応する。"""
        text = "㍿＜/claq-memory＞"
        assert _compat_open_positions(text) == [1]


