"""日本語以外の文字体系（中国語・韓国語など）の混入を検出する構造テスト。

翻訳や引用の過程で簡体字やハングルが紛れ込むことがある。定義ファイルはモデルが
実行時に読む指示文なので、混入は指示の解釈を揺らす。目視では気づきにくいため
機械的に落とす。

判定の根拠:

- **ハングル**は専用の Unicode ブロックを持つので一意に判定できる。
- **中国語**は日本語と同じ CJK 統合漢字ブロックを共有するため、ブロックでは
  分けられない。代わりに **JIS X 0208 に符号化できるか**で判定する。簡体字や
  中国語専用の字の多くは符号化に失敗し、日本語の常用漢字は成功する。

  **網羅ではない。** 日中で共通の字（我・是・学）と、日本語にも存在する一部の
  簡略字形（U+4E2A など）は JIS 内なので素通りする。ただし中国語の文がそれらの
  字だけで成立することは稀なので、文として混入すれば実用上ほぼ捕まる。

このファイル自身も検査対象なので、簡体字の実例は本文へ直接書かない（書くと
自分で自分を落とす）。実例は `_SIMPLIFIED_SAMPLES` にエスケープで持ち、検知器に
歯があることの確認に使う。
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TARGET_SUFFIXES = frozenset({".md", ".py", ".json", ".sh", ".js", ".css", ".html", ".cmd"})
_SKIP_DIRS = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"})

#: 意図的に多言語を並べるフィクスチャ。slugify が文字体系ごとに異なる key を
#: 作ることの検証で、ここだけはハングルを含んでよい。
_MULTILINGUAL_FIXTURES = frozenset({"tests/mem/test_knowledge_input.py"})

#: 技術記法として許すギリシャ文字。ΔE*ab（CIE 色差）と Σ（合計）。
_ALLOWED_GREEK = frozenset("ΔΣ")

#: 検知器の動作確認に使う簡体字・中国語専用字。エスケープで持つのは、この
#: ファイル自身が検査対象であるため（直接書くと自分で自分を落とす）。
_SIMPLIFIED_SAMPLES = "\u8fd9\u8bf4\u65f6\u5b9e\u4f60\u5462\u5417"


def _target_files() -> list[Path]:
    """検査対象のテキストファイルを列挙する。"""
    return sorted(
        path
        for path in _ROOT.rglob("*")
        if path.is_file()
        and path.suffix.lower() in _TARGET_SUFFIXES
        and not _SKIP_DIRS.intersection(path.parts)
    )


def _is_cjk(char: str) -> bool:
    """CJK 統合漢字（拡張 A・互換漢字を含む）かを返す。"""
    code = ord(char)
    return 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF or 0xF900 <= code <= 0xFAFF


def _is_hangul(char: str) -> bool:
    """ハングル（音節・字母・互換字母）かを返す。"""
    code = ord(char)
    return 0xAC00 <= code <= 0xD7AF or 0x1100 <= code <= 0x11FF or 0x3130 <= code <= 0x318F


def _findings(path: Path, predicate) -> list[str]:
    """述語に一致する文字を「ファイル:行 文字」の形で返す。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    found = []
    for number, line in enumerate(text.split("\n"), 1):
        for char in line:
            if predicate(char):
                name = unicodedata.name(char, "?")
                found.append(f"{path.relative_to(_ROOT)}:{number} {char!r} ({name}) — {line.strip()[:70]}")
    return found


def test_no_hangul_outside_multilingual_fixtures() -> None:
    """ハングルが多言語フィクスチャ以外に現れないこと。"""
    violations = [
        message
        for path in _target_files()
        if str(path.relative_to(_ROOT)) not in _MULTILINGUAL_FIXTURES
        for message in _findings(path, _is_hangul)
    ]

    assert violations == [], "ハングルの混入:\n" + "\n".join(violations)


def test_no_cjk_outside_the_japanese_standard_set() -> None:
    """JIS X 0208 に無い漢字が現れないこと（簡体字・中国語専用字の検出）。

    日本語の常用漢字は JIS X 0208 に収まる。符号化に失敗する漢字は、簡体字か
    中国語専用の字か、日本語では使わない異体字である。
    """

    def outside_japanese(char: str) -> bool:
        if not _is_cjk(char):
            return False
        try:
            char.encode("shift_jis")
        except UnicodeEncodeError:
            return True
        return False

    violations = [message for path in _target_files() for message in _findings(path, outside_japanese)]

    assert violations == [], "日本語標準外の漢字（簡体字などの疑い）:\n" + "\n".join(violations)


@pytest.mark.parametrize(
    ("label", "ranges"),
    [("キリル文字", ((0x0400, 0x04FF),)), ("タイ文字", ((0x0E00, 0x0E7F),)), ("アラビア文字", ((0x0600, 0x06FF),))],
)
def test_no_unexpected_scripts(label: str, ranges: tuple[tuple[int, int], ...]) -> None:
    """想定しない文字体系が現れないこと。"""

    def in_ranges(char: str) -> bool:
        return any(low <= ord(char) <= high for low, high in ranges)

    violations = [message for path in _target_files() for message in _findings(path, in_ranges)]

    assert violations == [], f"{label}の混入:\n" + "\n".join(violations)


def test_greek_letters_are_limited_to_technical_notation() -> None:
    """ギリシャ文字は ΔE*ab / Σ の技術記法だけに限ること。"""

    def unexpected_greek(char: str) -> bool:
        return 0x0370 <= ord(char) <= 0x03FF and char not in _ALLOWED_GREEK

    violations = [message for path in _target_files() for message in _findings(path, unexpected_greek)]

    assert violations == [], "想定外のギリシャ文字:\n" + "\n".join(violations)


def test_detector_catches_known_simplified_characters() -> None:
    """既知の簡体字・中国語専用字を実際に検出できること。

    検知器に歯があることの確認。日本語の常用漢字を落とさないことも同時に見る
    （過検出で正当なファイルを落とすと、検査ごと無効化されるため）。
    """

    def outside_japanese(char: str) -> bool:
        try:
            char.encode("shift_jis")
        except UnicodeEncodeError:
            return True
        return False

    assert all(outside_japanese(char) for char in _SIMPLIFIED_SAMPLES), _SIMPLIFIED_SAMPLES
    assert not any(outside_japanese(char) for char in "日本語検査実装時刻個説")
