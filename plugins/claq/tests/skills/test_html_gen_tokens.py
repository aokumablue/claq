"""html-gen のデザイントークンの検査（upstream への忠実性・色空間・スキーム・コントラスト契約・tokens.css の生成）。"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

import pytest

from ._support import (
    _TEMPLATE,
    _TOKENS,
    BASES,
    TOKENS,
    _append,
    _copy_site,
    _edit,
    check_site,
)

# --- upstream への忠実性 -----------------------------------------------------


def test_bases_are_the_five_shadcn_registry_base_colors() -> None:
    """ベースカラーは shadcn/ui が配る 5 つと同じ顔ぶれで、既定は neutral。"""
    assert BASES == ("neutral", "zinc", "slate", "stone", "gray")
    assert TOKENS["default_base"] == "neutral"


def test_neutral_tokens_are_verbatim_upstream_values() -> None:
    """neutral の主要トークンが shadcn/ui レジストリの値そのままである。

    ここが upstream との同一性のアンカー。生成器を書き換えても、この値が
    ずれれば「shadcn/ui の配色」を名乗れなくなる。
    """
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


def test_norm_hex_expands_short_form() -> None:
    """3 桁・4 桁・8 桁 hex を 6 桁へ正規化する。"""
    assert check_site._norm_hex("#abc") == "#AABBCC"
    assert check_site._norm_hex("#abcd") == "#AABBCC"
    assert check_site._norm_hex("#AABBCCDD") == "#AABBCC"
    with pytest.raises(ValueError):
        check_site._norm_hex("blue")
    with pytest.raises(ValueError):
        check_site._norm_hex("#12345")


def test_contrast_ratio_matches_wcag_reference_values() -> None:
    """既知の組み合わせで WCAG の比率を再現する。"""
    assert check_site.contrast_ratio("#000000", "#FFFFFF") == pytest.approx(21.0, abs=1e-6)
    assert check_site.contrast_ratio("#777777", "#FFFFFF") == pytest.approx(4.48, abs=0.01)
    assert check_site.contrast_ratio("#FFFFFF", "#FFFFFF") == pytest.approx(1.0, abs=1e-9)


def test_lab_conversion_matches_reference_values() -> None:
    """Lab 変換が既知の値と一致する。"""
    lightness, a_axis, b_axis = check_site.hex_to_lab("#FFFFFF")
    assert lightness == pytest.approx(100.0, abs=0.01)
    assert (a_axis, b_axis) == pytest.approx((0.0, 0.0), abs=0.01)
    assert check_site.hex_to_lab("#000000")[0] == pytest.approx(0.0, abs=1e-6)


def test_delta_e_is_zero_for_identical_colors() -> None:
    """同じ色の色差はゼロ、白と黒は 100 を超える。"""
    assert check_site.delta_e("#123456", "#123456") == pytest.approx(0.0, abs=1e-9)
    assert check_site.delta_e("#000000", "#FFFFFF") > 99.0


def test_parse_oklch_reads_both_forms() -> None:
    """不透明・半透明どちらの oklch() も読み、それ以外は拒否する。"""
    assert check_site.parse_oklch("oklch(0.5 0.1 200)") == (0.5, 0.1, 200.0, 1.0)
    assert check_site.parse_oklch("oklch(1 0 0 / 10%)") == (1.0, 0.0, 0.0, 0.1)
    with pytest.raises(ValueError):
        check_site.parse_oklch("#ffffff")


def test_oklch_matches_known_tailwind_colors() -> None:
    """upstream の oklch が Tailwind の既知 hex へ落ちる。

    OKLab の逆変換とガンマの両方が正しいことを、外部の既知値で確かめる。
    ここがずれると全コントラスト測定が静かに嘘になる。
    """
    assert check_site.oklch_to_hex("oklch(1 0 0)") == "#FFFFFF"
    # slate-500 / slate-200 / orange-600（Tailwind v4）
    assert check_site.oklch_to_hex("oklch(0.554 0.046 257.417)") == "#62748E"
    assert check_site.oklch_to_hex("oklch(0.929 0.013 255.508)") == "#E2E8F0"
    assert check_site.oklch_to_hex("oklch(0.646 0.222 41.116)") == "#F54900"


def test_translucent_color_is_composited_over_its_backdrop() -> None:
    """半透明は下地へ合成した実効色になる（合成先が無ければエラー）。"""
    over_black = check_site.oklch_to_hex("oklch(1 0 0 / 10%)", "oklch(0 0 0)")
    over_white = check_site.oklch_to_hex("oklch(1 0 0 / 10%)", "oklch(1 0 0)")
    assert over_white == "#FFFFFF"
    assert over_black != over_white
    assert check_site.relative_luminance(over_black) < 0.1
    with pytest.raises(ValueError):
        check_site.oklch_to_hex("oklch(1 0 0 / 10%)")


def test_fit_chroma_clamps_chroma_and_keeps_lightness() -> None:
    """域外の彩度だけを落とし、明度は動かさない。"""
    hue = 264.376
    fitted = check_site.fit_chroma(0.58, 0.4, hue)
    assert fitted < 0.4
    assert check_site._in_gamut(check_site.oklch_to_linear_rgb(0.58, fitted, hue))
    assert check_site.fit_chroma(0.58, 0.02, hue) == 0.02


def test_format_oklch_is_reparsable() -> None:
    """書き出した表記をそのまま読み戻せる。"""
    text = check_site.format_oklch(0.58, 0.1234567, 264.376)
    assert check_site.parse_oklch(text)[0] == 0.58


def test_lower_lightness_gives_up_when_the_target_is_unreachable() -> None:
    """どれだけ暗くしても満たせない要求は例外にする（白背景の上限は 21:1）。"""
    with pytest.raises(ValueError):
        check_site._lower_lightness_until("oklch(0.7 0 0)", "#FFFFFF", 25.0)


def test_lower_lightness_returns_immediately_when_already_compliant() -> None:
    """既に満たしている色は明度を動かさない（upstream の値をできる限り残す）。"""
    assert check_site._lower_lightness_until("oklch(0.7 0 0)", "#000000", 4.5) == check_site.format_oklch(
        0.7, 0.0, 0.0
    )


# --- スキームの解決 ---------------------------------------------------------


def test_scheme_applies_contrast_fixes_only_where_needed() -> None:
    """契約を満たさない役割だけ明度が下がり、満たすものは upstream のまま。"""
    light = check_site.scheme(TOKENS, "neutral", "light")
    dark = check_site.scheme(TOKENS, "neutral", "dark")
    upstream_light = TOKENS["bases"]["neutral"]["light"]
    upstream_dark = TOKENS["bases"]["neutral"]["dark"]

    assert light["--ring"] != upstream_light["ring"]
    assert check_site.parse_oklch(light["--ring"])[0] < check_site.parse_oklch(
        upstream_light["ring"]
    )[0]
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
    for base_id in BASES:
        for theme in check_site.THEMES:
            resolved = check_site.scheme(TOKENS, base_id, theme)
            assert resolved["--destructive-foreground"] == TOKENS["derived_roles"][
                "destructive-foreground"
            ][theme]


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
    """10 ブロック（5 ベース × 2 テーマ）すべてが可読性契約を満たす。"""
    for base_id in BASES:
        for theme in check_site.THEMES:
            assert check_site.contrast_violations(TOKENS, base_id, theme) == []


def test_contrast_contract_covers_charts_status_and_focus() -> None:
    """契約表が系列色・破壊的操作・フォーカスリングを取りこぼしていない。"""
    pairs = {(foreground, background) for foreground, background, _ in check_site.CONTRAST_CONTRACT}
    for name in check_site.CHART_VARS:
        assert (name, "--card") in pairs
        assert (name, "--background") in pairs
    for pair in (
        ("--muted-foreground", "--card"),
        ("--destructive-foreground", "--destructive"),
        ("--ring", "--background"),
        ("--sidebar-ring", "--sidebar"),
        ("--sidebar-foreground", "--sidebar"),
    ):
        assert pair in pairs


def test_muted_foreground_is_checked_on_the_sidebar_surface() -> None:
    """サイドバー面の補助テキストも契約に入っている。

    面ごとに変数が分かれているので、背景・カードだけ見ていると
    サイドバーの文字が誰にも見張られないまま出荷される。
    """
    pairs = {(foreground, background) for foreground, background, _ in check_site.CONTRAST_CONTRACT}
    assert ("--muted-foreground", "--sidebar") in pairs
    for base_id in BASES:
        for theme in check_site.THEMES:
            colors = check_site.resolved_hex(TOKENS, base_id, theme)
            assert check_site.contrast_ratio(colors["--muted-foreground"], colors["--sidebar"]) >= 4.5


def test_text_color_from_a_transparent_mix_is_reported(tmp_path: Path) -> None:
    """文字色を透明との混合で作ると落ちる。

    ライトでは前景を薄めるほど背景（白）へ寄るので、可読性契約を割る。
    面や枠の混合は問題ないので通す。
    """
    dest = _copy_site(tmp_path)
    assert "透明との color-mix" in _append(
        dest, "styles.css", "\n.x { color: color-mix(in oklab, var(--foreground) 50%, transparent); }\n"
    )
    dest = _copy_site(tmp_path, "s2")
    assert (
        _append(
            dest,
            "styles.css",
            "\n.x { background: color-mix(in oklab, var(--foreground) 8%, transparent);"
            " border-color: color-mix(in oklab, var(--ring) 40%, transparent); }\n",
        )
        == ""
    )


def test_heatmap_cap_keeps_cell_text_readable_in_every_block() -> None:
    """ヒートマップの濃さの上限（`.heat td` の 55%）で、セルの文字が 4.5:1 を保つこと。

    上限を上げると、ダークの濃いセルで --card-foreground が下地の系列色に近づき
    可読性契約を割る。CSS の `color-mix(in oklab, …)` と同じ OKLab 上の線形混合で、
    全ベースカラー × 両テーマの最も濃いセル（`--v: 1`）を確かめる。
    """
    css = (_TEMPLATE / "styles.css").read_text(encoding="utf-8")
    cap = re.search(r"\.heat td \{[^}]*?calc\(var\(--v, 0\) \* ([\d.]+)%\)", css)
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
    css = (_TEMPLATE / "styles.css").read_text(encoding="utf-8")
    assert not re.search(r"(?<![-\w])color:\s*color-mix\([^;}]*\btransparent\b", css)


def test_decorative_borders_are_excluded_from_the_3_to_1_rule() -> None:
    """ヘアラインは 3:1 契約の外。ただし背景と同化していないことは見る。"""
    decorative = set(TOKENS["decorative_roles"]["vars"])
    assert decorative == {"border", "input", "sidebar-border"}
    for foreground, _, _ in check_site.CONTRAST_CONTRACT:
        assert foreground.removeprefix("--") not in decorative
    colors = check_site.resolved_hex(TOKENS, "neutral", "light")
    ratio = check_site.contrast_ratio(colors["--border"], colors["--background"])
    assert TOKENS["decorative_roles"]["min_ratio"] <= ratio < 3.0


def test_border_that_matches_the_background_is_reported() -> None:
    """ヘアラインが背景と同値になれば違反として出る。"""
    broken = json.loads(_TOKENS.read_text(encoding="utf-8"))
    broken["bases"]["neutral"]["light"]["border"] = "oklch(1 0 0)"
    violations = check_site.contrast_violations(broken, "neutral", "light")
    assert any("--border" in item and "同化" in item for item in violations)


def test_sunken_chart_series_is_reported() -> None:
    """カード面に沈んだ系列色は違反として出る。"""
    broken = json.loads(_TOKENS.read_text(encoding="utf-8"))
    broken["series"]["light_lightness"] = 0.98
    violations = check_site.contrast_violations(broken, "neutral", "light")
    assert any("--chart-" in item and "コントラスト" in item for item in violations)


def test_duplicate_series_is_reported() -> None:
    """系列色が重複したら違反として出る。"""
    broken = json.loads(_TOKENS.read_text(encoding="utf-8"))
    broken["series"]["hues"] = [264.376] * 5
    broken["series"]["chromas"] = [0.243] * 5
    violations = check_site.contrast_violations(broken, "neutral", "light")
    assert any("重複" in item for item in violations)


def test_indistinguishable_series_is_reported() -> None:
    """色差が下限を割った系列は違反として出る。"""
    broken = json.loads(_TOKENS.read_text(encoding="utf-8"))
    broken["series"]["hues"] = [264.376, 266.0, 268.0, 270.0, 272.0]
    violations = check_site.contrast_violations(broken, "neutral", "light")
    assert any("色差" in item for item in violations)


def test_series_stay_apart_in_every_block() -> None:
    """全ブロックで系列色の相互距離が下限を上回る。"""
    minimum = TOKENS["series"]["min_delta_e"]
    for base_id in BASES:
        for theme in check_site.THEMES:
            colors = check_site.resolved_hex(TOKENS, base_id, theme)
            series = [colors[name] for name in check_site.CHART_VARS]
            for left in range(len(series)):
                for right in range(left + 1, len(series)):
                    assert check_site.delta_e(series[left], series[right]) >= minimum


def test_broken_tokens_surface_the_contrast_failure(tmp_path: Path) -> None:
    """tokens.json を壊すと検査が実測で落ちる（検査が赤くなることの実証）。"""
    dest = _copy_site(tmp_path)
    broken = json.loads((dest / "tokens.json").read_text(encoding="utf-8"))
    broken["bases"]["neutral"]["light"]["foreground"] = "oklch(0.97 0 0)"
    (dest / "tokens.json").write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
    violations = "\n".join(check_site.validate_site(dest))
    assert "--foreground" in violations and "コントラスト" in violations


# --- tokens.css の生成 -------------------------------------------------------


def test_tokens_css_has_a_block_for_every_base_and_theme() -> None:
    """生成 CSS が 5 ベース × 2 テーマの全ブロックを持つ。"""
    css = (_TEMPLATE / "tokens.css").read_text(encoding="utf-8")
    for base_id in BASES:
        for theme in check_site.THEMES:
            assert f':root[data-theme="{theme}"][data-base="{base_id}"] {{' in css


def test_tokens_css_carries_the_shadcn_token_set() -> None:
    """配色以外のトークン（タイポ・角丸・影・モーション・レイアウト）も出る。"""
    css = (_TEMPLATE / "tokens.css").read_text(encoding="utf-8")
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
        assert f"  {name}:" in css, name
    assert "--radius-lg: var(--radius);" in css
    assert "--radius-sm: calc(var(--radius) - 4px);" in css


def test_base_swatch_variables_are_theme_independent_and_distinct() -> None:
    """ベース選択 UI 用の色は :root にあり、5 つが別の値になる。"""
    css = (_TEMPLATE / "tokens.css").read_text(encoding="utf-8")
    head = css.split(':root[data-theme="light"]', 1)[0]
    values = []
    for base_id in BASES:
        marker = f"  --base-{base_id}: "
        assert marker in head, base_id
        values.append(head.split(marker, 1)[1].split(";", 1)[0])
    assert len(set(values)) == len(values)


def test_edited_tokens_css_is_reported(tmp_path: Path) -> None:
    """tokens.css を手で直すと生成結果との不一致で落ちる。"""
    dest = _copy_site(tmp_path)
    assert "生成結果と一致しない" in _edit(
        dest, "tokens.css", ("--radius: 0.625rem;", "--radius: 1rem;")
    )
