"""html-gen の Web フォント（テンプレートに同梱した `fonts/` と、`styles.css` / `check_site.py` との整合）の検査。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ._support import _TEMPLATE, CSS, TOKENS, _copy_site, check_site

_FONTS = _TEMPLATE / "fonts"
_BODIES = {
    "OFL.txt": b"Copyright 2024 The Geist Project Authors\nSIL Open Font License, Version 1.1\n",
    "Geist-Variable.woff2": b"wOF2" + b"sans-body" * 5,
    "GeistMono-Variable.woff2": b"wOF2" + b"mono-body" * 5,
}


# --- ライセンスの同梱（check_site.py） -------------------------------------------


def test_template_ships_fonts_with_license() -> None:
    """テンプレートは Geist / Geist Mono の woff2 と、その再配布条件の OFL.txt を同梱する。"""
    assert sorted(path.name for path in _FONTS.iterdir()) == ["Geist-Variable.woff2", "GeistMono-Variable.woff2", "OFL.txt"]


def test_shipped_woff2_are_woff2() -> None:
    """同梱の woff2 は woff2 の先頭（`wOF2`）を持つ。"""
    assert [path.read_bytes()[:4] for path in sorted(_FONTS.glob("*.woff2"))] == [b"wOF2", b"wOF2"]


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        pytest.param(None, [], id="no-fonts-dir"),
        pytest.param((), [], id="empty-fonts-dir"),
        pytest.param(("OFL.txt",), [], id="ofl-only"),
        pytest.param(("OFL.txt", "Geist-Variable.woff2"), [], id="woff2-and-ofl"),
        pytest.param(("Geist-Variable.woff2",), [True], id="woff2-without-ofl"),
    ],
)
def test_ofl_required_when_woff2_present(tmp_path: Path, files: tuple[str, ...] | None, expected: list[bool]) -> None:
    """fonts/ に woff2 を置くなら OFL.txt も要る（SIL OFL 1.1 の再配布条件）。無ければサイト検査が落とす。"""
    dest = _copy_site(tmp_path)
    if files is not None:
        (dest / "fonts").mkdir()
        for name in files:
            (dest / "fonts" / name).write_bytes(_BODIES[name])
    assert ["fonts/OFL.txt" in item for item in check_site.validate_site(dest)] == expected


# --- styles.css の @font-face ---------------------------------------------------

_FONT_FACES = re.findall(r"@font-face\s*\{([^}]*)\}", CSS)


def _faces() -> dict[str, str]:
    """styles.css の @font-face を font-family → fonts/ 内のファイル名の辞書にする。"""
    return {
        re.search(r'font-family:\s*"([^"]+)"', block).group(1): re.search(r'url\("fonts/([^"]+)"\)', block).group(1)
        for block in _FONT_FACES
    }


def _first_family(stack: str) -> str:
    """CSS の font-family 列の先頭候補を引用符を外して返す。"""
    return stack.split(",")[0].strip().strip('"')


def test_font_face_sources_match_shipped_fonts() -> None:
    """@font-face が読むファイルは同梱した woff2 とちょうど一致する。"""
    assert set(_faces().values()) == {path.name for path in _FONTS.glob("*.woff2")}


def test_font_face_families_are_token_first_candidates() -> None:
    """@font-face の family は tokens.json の sans / mono の先頭候補（その次が OS の書体）。"""
    typography = TOKENS["typography"]
    assert set(_faces()) == {_first_family(typography["sans_family"]), _first_family(typography["mono_family"])}


def test_font_face_pairs_family_with_its_file() -> None:
    """各 family は自分の可変フォントを読む（Geist Mono が Geist の字形を読むような取り違えが無い）。"""
    typography = TOKENS["typography"]
    families = (_first_family(typography["sans_family"]), _first_family(typography["mono_family"]))
    assert _faces() == {family: family.replace(" ", "") + "-Variable.woff2" for family in families}


def test_font_face_blocks_declare_variable_woff2_swap() -> None:
    """各 @font-face は woff2 形式・可変の太さの幅・swap（読み込み前は次の候補で描く）を宣言する。"""
    required = ('format("woff2")', "font-weight: 100 900", "font-display: swap")
    assert [all(item in block for item in required) for block in _FONT_FACES] == [True, True]
