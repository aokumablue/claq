"""html-gen のデザイントークンの検査（upstream への忠実性・色空間・スキーム・コントラスト契約・tokens.css の生成）。"""

from __future__ import annotations

import copy
import json
import math
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pytest import approx

from ._support import BASES, CSS, TOKENS, TOKENS_CSS, _append, _copy_site, _edit, check_site

# --- upstream への忠実性 -----------------------------------------------------


def test_bases_are_the_five_shadcn_registry_base_colors() -> None:
    """ベースカラーは shadcn/ui が配る 5 つと同じ顔ぶれで、既定は neutral。"""
    assert BASES == ("neutral", "zinc", "slate", "stone", "gray")
    assert TOKENS["default_base"] == "neutral"


def test_neutral_tokens_are_verbatim_upstream_values() -> None:
    """neutral の主要トークンが shadcn/ui レジストリの値そのままである（upstream との同一性のアンカー。生成器を書き換えてもこの値がずれれば「shadcn/ui の配色」を名乗れない）。"""
    light = TOKENS["bases"]["neutral"]["light"]
    dark = TOKENS["bases"]["neutral"]["dark"]
    assert light["background"] == "oklch(1 0 0)"
    assert light["foreground"] == "oklch(0.145 0 0)"
    assert light["primary"] == "oklch(0.205 0 0)"
    assert light["border"] == "oklch(0.922 0 0)"
    assert dark["background"] == "oklch(0.145 0 0)"
    assert dark["card"] == "oklch(0.205 0 0)"
    assert dark["border"] == "oklch(1 0 0 / 10%)"
    assert TOKENS["radius"]["base_rem"] == 0.625


def test_every_base_carries_the_full_role_set_for_both_themes() -> None:
    """全ベースが role_order の全役割をライト/ダーク両方で持つ。"""
    expected = set(TOKENS["role_order"])
    for base_id in BASES:
        for theme in check_site.THEMES:
            assert set(TOKENS["bases"][base_id][theme]) == expected, f"{base_id}/{theme}"


def test_series_uses_five_colors_like_upstream() -> None:
    """系列色は upstream と同じ 5 本。"""
    assert check_site.SERIES_COUNT == 5
    assert TOKENS["series"]["count"] == 5
    assert len(check_site.CHART_VARS) == 5


# --- 色空間 -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("convert", "args", "expected"),
    [
        # 3 桁・4 桁・8 桁の hex を 6 桁へ正規化する
        pytest.param(check_site._norm_hex, ("#abc",), "#AABBCC", id="hex-3-digits"),
        pytest.param(check_site._norm_hex, ("#abcd",), "#AABBCC", id="hex-4-digits"),
        pytest.param(check_site._norm_hex, ("#AABBCCDD",), "#AABBCC", id="hex-8-digits"),
        # WCAG のコントラスト比の既知の組み合わせ
        pytest.param(check_site.contrast_ratio, ("#000000", "#FFFFFF"), approx(21.0, abs=1e-6), id="contrast-21"),
        pytest.param(check_site.contrast_ratio, ("#777777", "#FFFFFF"), approx(4.48, abs=0.01), id="contrast-4.48"),
        pytest.param(check_site.contrast_ratio, ("#FFFFFF", "#FFFFFF"), approx(1.0, abs=1e-9), id="contrast-1"),
        # Lab 変換と色差（同じ色はゼロ、白と黒は 100）
        pytest.param(check_site.hex_to_lab, ("#FFFFFF",), approx((100.0, 0.0, 0.0), abs=0.01), id="lab-white"),
        pytest.param(check_site.hex_to_lab, ("#000000",), approx((0.0, 0.0, 0.0), abs=1e-6), id="lab-black"),
        pytest.param(check_site.delta_e, ("#123456", "#123456"), approx(0.0, abs=1e-9), id="delta-e-same"),
        pytest.param(check_site.delta_e, ("#000000", "#FFFFFF"), approx(100.0, abs=0.01), id="delta-e-black-white"),
        # 不透明・半透明どちらの oklch() も読む
        pytest.param(check_site.parse_oklch, ("oklch(0.5 0.1 200)",), (0.5, 0.1, 200.0, 1.0), id="oklch-opaque"),
        pytest.param(check_site.parse_oklch, ("oklch(1 0 0 / 10%)",), (1.0, 0.0, 0.0, 0.1), id="oklch-translucent"),
        # upstream の oklch が Tailwind v4 の既知の hex（白・slate-500・slate-200・orange-600）へ落ちる。
        # OKLab の逆変換とガンマの両方を外部の既知値で確かめる（ずれると全コントラスト測定が静かに嘘になる）
        pytest.param(check_site.oklch_to_hex, ("oklch(1 0 0)",), "#FFFFFF", id="white"),
        pytest.param(check_site.oklch_to_hex, ("oklch(0.554 0.046 257.417)",), "#62748E", id="slate-500"),
        pytest.param(check_site.oklch_to_hex, ("oklch(0.929 0.013 255.508)",), "#E2E8F0", id="slate-200"),
        pytest.param(check_site.oklch_to_hex, ("oklch(0.646 0.222 41.116)",), "#F54900", id="orange-600"),
        # 半透明は下地へ合成した実効色になる（白の上では白、黒の上では暗い灰）
        pytest.param(check_site.oklch_to_hex, ("oklch(1 0 0 / 10%)", "oklch(1 0 0)"), "#FFFFFF", id="alpha-on-white"),
        pytest.param(check_site.oklch_to_hex, ("oklch(1 0 0 / 10%)", "oklch(0 0 0)"), "#595959", id="alpha-on-black"),
        # 既に満たしている色は明度を動かさない（upstream の値をできる限り残す）
        pytest.param(check_site._lower_lightness_until, ("oklch(0.7 0 0)", "#000000", 4.5), "oklch(0.7 0 0)", id="already-compliant"),
    ],
)
def test_color_math_matches_reference_values(convert: Callable[..., Any], args: tuple[Any, ...], expected: Any) -> None:
    """色空間の正規化・変換・比率が既知の値を再現する。"""
    assert convert(*args) == expected


@pytest.mark.parametrize(
    ("convert", "args", "message"),
    [
        pytest.param(check_site._norm_hex, ("blue",), "hex ではない", id="hex-without-hash"),
        pytest.param(check_site._norm_hex, ("#12345",), "不正な hex", id="hex-5-digits"),
        pytest.param(check_site.parse_oklch, ("#ffffff",), r"oklch\(\) ではない", id="not-oklch"),
        pytest.param(check_site.oklch_to_hex, ("oklch(1 0 0 / 10%)",), "合成先が無い", id="translucent-without-backdrop"),
        # どれだけ暗くしても満たせない要求（白背景の上限は 21:1）
        pytest.param(check_site._lower_lightness_until, ("oklch(0.7 0 0)", "#FFFFFF", 25.0), "満たせない", id="unreachable"),
    ],
)
def test_color_math_rejects_invalid_input(convert: Callable[..., Any], args: tuple[Any, ...], message: str) -> None:
    """hex・oklch() として読めない値、合成先の無い半透明、届かない比率は ValueError になる。"""
    with pytest.raises(ValueError, match=message):
        convert(*args)


def test_fit_chroma_clamps_chroma_and_keeps_lightness() -> None:
    """域外の彩度だけを落とし、明度は動かさない。"""
    hue = 264.376
    fitted = check_site.fit_chroma(0.58, 0.4, hue)
    assert fitted < 0.4
    assert check_site._in_gamut(check_site.oklch_to_linear_rgb(0.58, fitted, hue))
    assert check_site.fit_chroma(0.58, 0.02, hue) == 0.02


def test_format_oklch_is_reparsable() -> None:
    """書き出した表記をそのまま読み戻せる。"""
    assert check_site.parse_oklch(check_site.format_oklch(0.58, 0.1234567, 264.376))[0] == 0.58


# --- スキームの解決 ---------------------------------------------------------


def test_scheme_applies_contrast_fixes_only_where_needed() -> None:
    """契約を満たさない役割だけ明度が下がり、満たすものは upstream のまま。"""
    light = check_site.scheme(TOKENS, "neutral", "light")
    dark = check_site.scheme(TOKENS, "neutral", "dark")
    upstream_light = TOKENS["bases"]["neutral"]["light"]
    upstream_dark = TOKENS["bases"]["neutral"]["dark"]

    assert light["--ring"] != upstream_light["ring"]
    assert check_site.parse_oklch(light["--ring"])[0] < check_site.parse_oklch(upstream_light["ring"])[0]
    assert light["--muted-foreground"] != upstream_light["muted-foreground"]
    # ダークは upstream のままで契約を満たす。
    assert dark["--ring"] == upstream_dark["ring"]
    assert dark["--muted-foreground"] == upstream_dark["muted-foreground"]
    # 触っていない役割は逐語のまま。
    assert light["--background"] == upstream_light["background"]
    assert light["--primary"] == upstream_light["primary"]
    assert dark["--border"] == upstream_dark["border"]


def test_derived_roles_are_present_in_every_scheme() -> None:
    """レジストリに無い destructive-foreground を全ブロックが持つ。"""
    expected = TOKENS["derived_roles"]["destructive-foreground"]
    for base_id in BASES:
        for theme in check_site.THEMES:
            assert check_site.scheme(TOKENS, base_id, theme)["--destructive-foreground"] == expected[theme]


def test_series_colors_share_hues_across_themes() -> None:
    """系列の色相はテーマで変わらない（切り替えても同じ系列が同じ色）。"""
    light = check_site.series_colors(TOKENS, "light")
    dark = check_site.series_colors(TOKENS, "dark")
    for name in check_site.CHART_VARS:
        assert check_site.parse_oklch(light[name])[2] == check_site.parse_oklch(dark[name])[2]
    assert check_site.parse_oklch(light["--chart-1"])[0] == TOKENS["series"]["light_lightness"]
    assert check_site.parse_oklch(dark["--chart-1"])[0] == TOKENS["series"]["dark_lightness"]


def test_resolved_hex_flattens_translucent_borders() -> None:
    """半透明の border は下地に合成された hex で返る。"""
    colors = check_site.resolved_hex(TOKENS, "neutral", "dark")
    assert colors["--border"].startswith("#")
    assert colors["--border"] != colors["--background"]


def test_color_var_names_cover_roles_derived_and_charts() -> None:
    """CSS へ書き出す変数名が役割・派生・系列を漏れなく並べる。"""
    names = check_site.color_var_names(TOKENS)
    assert names[0] == "--background"
    assert "--destructive-foreground" in names
    assert set(check_site.CHART_VARS) <= set(names)
    assert len(names) == len(set(names))


# --- コントラスト契約 -------------------------------------------------------


def test_contrast_contract_holds_for_every_base_and_theme() -> None:
    """10 ブロック（5 ベース × 2 テーマ）すべてが可読性契約（系列色どうしの色差の下限を含む）を満たす。"""
    for base_id in BASES:
        for theme in check_site.THEMES:
            assert check_site.contrast_violations(TOKENS, base_id, theme) == []


def test_contrast_contract_covers_charts_status_focus_and_sidebar_text() -> None:
    """契約表が系列色・破壊的操作・フォーカスリング・サイドバー面の補助テキスト（面ごとに変数が分かれ、背景とカードだけでは誰にも見張られない。文字の下限 4.5:1 で入れ、全ブロックでの充足は契約の検査が確かめる）を取りこぼしていない。"""
    pairs = {(foreground, background) for foreground, background, _ in check_site.CONTRAST_CONTRACT}
    for pair in (
        *((name, surface) for name in check_site.CHART_VARS for surface in ("--card", "--background")),
        ("--muted-foreground", "--card"),
        ("--destructive-foreground", "--destructive"),
        ("--ring", "--background"),
        ("--sidebar-ring", "--sidebar"),
        ("--sidebar-foreground", "--sidebar"),
    ):
        assert pair in pairs, pair
    assert ("--muted-foreground", "--sidebar", 4.5) in check_site.CONTRAST_CONTRACT


def test_text_color_from_a_transparent_mix_is_reported(tmp_path: Path) -> None:
    """文字色を透明との混合で作ると落ち（ライトでは前景を薄めるほど白い背景へ寄って可読性契約を割る）、面や枠の混合は通る。"""
    text = "\n.x { color: color-mix(in oklab, var(--foreground) 50%, transparent); }\n"
    assert "透明との color-mix" in _append(_copy_site(tmp_path), "styles.css", text)
    surfaces = (
        "\n.x { background: color-mix(in oklab, var(--foreground) 8%, transparent);"
        " border-color: color-mix(in oklab, var(--ring) 40%, transparent); }\n"
    )
    assert _append(_copy_site(tmp_path), "styles.css", surfaces) == ""


def test_heatmap_cap_keeps_cell_text_readable_in_every_block() -> None:
    """ヒートマップの濃さの上限（`.heat td` の 55%）で、全ブロックの最も濃いセル（`--v: 1`）の文字が 4.5:1 を保つ（上限を上げるとダークで --card-foreground が下地の系列色に近づく。CSS の color-mix(in oklab, …) と同じ OKLab 上の線形混合で測る）。"""
    cap = re.search(r"\.heat td \{[^}]*?calc\(var\(--v, 0\) \* ([\d.]+)%\)", CSS)
    assert cap is not None
    share = float(cap.group(1)) / 100

    def oklab(value: str) -> tuple[float, float, float]:
        """不透明な oklch() を OKLab の (L, a, b) にする。"""
        lightness, chroma, hue, alpha = check_site.parse_oklch(value)
        assert alpha == 1.0, value
        return lightness, chroma * math.cos(math.radians(hue)), chroma * math.sin(math.radians(hue))

    for base_id in BASES:
        for theme in check_site.THEMES:
            resolved = check_site.scheme(TOKENS, base_id, theme)
            lightness, a_axis, b_axis = (
                share * top + (1 - share) * under
                for top, under in zip(oklab(resolved["--chart-1"]), oklab(resolved["--card"]), strict=True)
            )
            hue = math.degrees(math.atan2(b_axis, a_axis)) % 360
            cell = check_site.oklch_to_hex(f"oklch({lightness:.6f} {math.hypot(a_axis, b_axis):.6f} {hue:.6f})")
            text = check_site.resolved_hex(TOKENS, base_id, theme)["--card-foreground"]
            assert check_site.contrast_ratio(text, cell) >= 4.5, f"{theme}/{base_id}"


def test_authored_text_colors_go_through_role_variables() -> None:
    """テンプレート自身が文字色を透明混合で作っていない。"""
    assert not re.search(r"(?<![-\w])color:\s*color-mix\([^;}]*\btransparent\b", CSS)


def test_decorative_borders_are_excluded_from_the_3_to_1_rule() -> None:
    """ヘアラインは 3:1 契約の外。ただし背景と同化していないことは見る。"""
    decorative = set(TOKENS["decorative_roles"]["vars"])
    assert decorative == {"border", "input", "sidebar-border"}
    for foreground, _, _ in check_site.CONTRAST_CONTRACT:
        assert foreground.removeprefix("--") not in decorative
    colors = check_site.resolved_hex(TOKENS, "neutral", "light")
    ratio = check_site.contrast_ratio(colors["--border"], colors["--background"])
    assert TOKENS["decorative_roles"]["min_ratio"] <= ratio < 3.0


def _broken_tokens(section: tuple[str, ...], values: dict[str, Any]) -> dict[str, Any]:
    """TOKENS の写しの、キーの経路 section が指す辞書を values で上書きして返す。"""
    broken = copy.deepcopy(TOKENS)
    target = broken
    for key in section:
        target = target[key]
    target.update(values)
    return broken


@pytest.mark.parametrize(
    ("section", "values", "needles"),
    [
        pytest.param(("bases", "neutral", "light"), {"border": "oklch(1 0 0)"}, ("--border", "同化"), id="border-matches-background"),
        pytest.param(("series",), {"light_lightness": 0.98}, ("--chart-", "コントラスト"), id="series-sunk-into-card"),
        pytest.param(("series",), {"hues": [264.376] * 5, "chromas": [0.243] * 5}, ("重複",), id="duplicate-series"),
        pytest.param(("series",), {"hues": [264.376, 266.0, 268.0, 270.0, 272.0]}, ("色差",), id="indistinguishable-series"),
    ],
)
def test_broken_tokens_are_reported(section: tuple[str, ...], values: dict[str, Any], needles: tuple[str, ...]) -> None:
    """tokens.json の配色を壊すと、そのブロックの検査に、壊した役割の違反が出る。"""
    violations = check_site.contrast_violations(_broken_tokens(section, values), "neutral", "light")
    assert any(all(needle in item for needle in needles) for item in violations), violations


def test_broken_tokens_surface_the_contrast_failure(tmp_path: Path) -> None:
    """tokens.json を壊すと検査が実測で落ちる（検査が赤くなることの実証）。"""
    dest = _copy_site(tmp_path)
    broken = _broken_tokens(("bases", "neutral", "light"), {"foreground": "oklch(0.97 0 0)"})
    (dest / "tokens.json").write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
    assert "--foreground と --background のコントラスト" in "\n".join(check_site.validate_site(dest))


# --- tokens.css の生成 -------------------------------------------------------


def test_tokens_css_has_a_block_for_every_base_and_theme() -> None:
    """生成 CSS が 5 ベース × 2 テーマの全ブロックを持つ。"""
    for base_id in BASES:
        for theme in check_site.THEMES:
            assert f':root[data-theme="{theme}"][data-base="{base_id}"] {{' in TOKENS_CSS


def test_tokens_css_carries_the_shadcn_token_set() -> None:
    """配色以外のトークン（タイポ・角丸・影・モーション・レイアウト）も出る。"""
    for name in (
        "--font-sans",
        "--text-sm",
        "--text-sm-line",
        "--tracking-tight",
        "--font-weight-medium",
        "--radius",
        "--radius-xl",
        "--shadow-xs",
        "--shadow-sm",
        "--ease-out",
        "--duration-base",
        "--state-ring-width",
        "--layout-breakpoint-md",
        "--layout-pane-max",
        "--layout-touch-target",
    ):
        assert f"  {name}:" in TOKENS_CSS, name
    assert "--radius-lg: var(--radius);" in TOKENS_CSS
    assert "--radius-sm: calc(var(--radius) - 4px);" in TOKENS_CSS


def test_base_swatch_variables_are_theme_independent_and_distinct() -> None:
    """ベース選択 UI 用の色は :root にあり、5 つが別の値になる。"""
    head = TOKENS_CSS.split(':root[data-theme="light"]', 1)[0]
    values = []
    for base_id in BASES:
        marker = f"  --base-{base_id}: "
        assert marker in head, base_id
        values.append(head.split(marker, 1)[1].split(";", 1)[0])
    assert len(set(values)) == len(values)


def test_edited_tokens_css_is_reported(tmp_path: Path) -> None:
    """tokens.css を手で直すと生成結果との不一致で落ちる。"""
    assert "生成結果と一致しない" in _edit(_copy_site(tmp_path), "tokens.css", ("--radius: 0.625rem;", "--radius: 1rem;"))
