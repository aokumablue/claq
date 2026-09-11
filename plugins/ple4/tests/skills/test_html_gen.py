"""html-gen テンプレートの shadcn/ui トークン契約と静的サイト E2E。"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_SKILL_DIR = _PLUGIN_ROOT / "skills" / "html-gen"
_TEMPLATE = _SKILL_DIR / "assets" / "template"
_TOKENS = _SKILL_DIR / "references" / "tokens.json"
_CHECK_SITE = _TEMPLATE / "check_site.py"
_SITE_FILES = ("index.html", "styles.css", "tokens.css", "app.js", "check_site.py")


def _load_check_site():
    """check_site.py をモジュールとして読み込む。

    `assets/template/` は配布される成果物なので、読み込みの副作用で
    `__pycache__` を書かせない（配布物に .pyc が混ざる）。
    """
    spec = importlib.util.spec_from_file_location("html_gen_check_site", _CHECK_SITE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    original = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = original
    return module


check_site = _load_check_site()
TOKENS = check_site.load_tokens(_TOKENS)
BASES = check_site.base_ids(TOKENS)


def _copy_site(tmp_path: Path, name: str = "site") -> Path:
    """検査可能なサイト一式を一時ディレクトリへ複製する。"""
    dest = tmp_path / name
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir()
    for filename in _SITE_FILES:
        shutil.copy(_TEMPLATE / filename, dest / filename)
    shutil.copy(_TOKENS, dest / "tokens.json")
    return dest


def _edit(dest: Path, filename: str, *replacements: tuple[str, str]) -> str:
    """サイト内のファイルを置換して書き戻し、違反を 1 本の文字列で返す。"""
    path = dest / filename
    text = path.read_text(encoding="utf-8")
    for old, new in replacements:
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
    return "\n".join(check_site.validate_site(dest))


def _append(dest: Path, filename: str, snippet: str) -> str:
    """サイト内のファイルへ追記し、違反を 1 本の文字列で返す。"""
    path = dest / filename
    path.write_text(path.read_text(encoding="utf-8") + snippet, encoding="utf-8")
    return "\n".join(check_site.validate_site(dest))


# --- テンプレート本体 -------------------------------------------------------


def test_template_passes_validator() -> None:
    """同梱テンプレートは shadcn/ui 契約検査に合格する。"""
    assert check_site.validate_site(_TEMPLATE) == []


def test_cli_pass_on_template() -> None:
    """CLI はテンプレートで exit 0 と PASS を返す。"""
    assert check_site.main([str(_TEMPLATE)]) == 0


def test_loading_check_site_leaves_no_bytecode_in_the_shipped_assets() -> None:
    """検査スクリプトの読み込みが配布ディレクトリに .pyc を落とさない。"""
    shutil.rmtree(_TEMPLATE / "__pycache__", ignore_errors=True)
    _load_check_site()
    assert not (_TEMPLATE / "__pycache__").exists()


def test_write_css_matches_committed_tokens_css(tmp_path: Path) -> None:
    """--write-css の出力はコミット済み tokens.css と一致する。"""
    dest = _copy_site(tmp_path)
    assert check_site.main(["--write-css", str(dest)]) == 0
    assert (dest / "tokens.css").read_text(encoding="utf-8") == (
        _TEMPLATE / "tokens.css"
    ).read_text(encoding="utf-8")


def test_generated_tokens_css_is_the_only_place_with_raw_color() -> None:
    """手書きの 3 ファイルは生の色を 1 つも持たない（色の正本は tokens.json）。"""
    for filename in ("index.html", "styles.css", "app.js"):
        text = (_TEMPLATE / filename).read_text(encoding="utf-8")
        assert check_site.HEX_RE.findall(text) == [], filename
        assert [m.group(1) for m in check_site.COLOR_FUNCTION_RE.finditer(text)] == [], filename
    generated = (_TEMPLATE / "tokens.css").read_text(encoding="utf-8")
    assert check_site.COLOR_FUNCTION_RE.search(generated)


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


# --- HTML の契約 -------------------------------------------------------------


def test_missing_theme_toggle_fails(tmp_path: Path) -> None:
    """テーマトグルを消すと落ちる。"""
    dest = _copy_site(tmp_path)
    assert "トグル" in _edit(dest, "index.html", ('data-theme-value="dark"', "data-x"))


def test_theme_toggle_needs_type_and_aria_pressed(tmp_path: Path) -> None:
    """トグルに type=button と aria-pressed が要る。"""
    dest = _copy_site(tmp_path)
    violations = _edit(dest, "index.html", ('type="button" data-theme-value', "data-theme-value"))
    assert "type=button" in violations
    dest = _copy_site(tmp_path, "site2")
    violations = _edit(dest, "index.html", ('aria-pressed="true"', "data-pressed"))
    assert "aria-pressed" in violations


def test_html_landmarks_skip_link_and_viewport(tmp_path: Path) -> None:
    """lang / ランドマーク / スキップリンク / viewport が要る。"""
    dest = _copy_site(tmp_path)
    assert "html[lang]" in _edit(dest, "index.html", ('<html lang="ja"', "<html lang=\"\""))

    dest = _copy_site(tmp_path, "s2")
    assert "viewport" in _edit(dest, "index.html", ('name="viewport"', 'name="vp"'))

    dest = _copy_site(tmp_path, "s3")
    assert "スキップリンク" in _edit(dest, "index.html", ('class="skip-link" href="#main"', 'class="x" href="#main2"'))

    for landmark in ("header", "main", "footer"):
        dest = _copy_site(tmp_path, f"s-{landmark}")
        assert f"<{landmark}> が無い" in _edit(
            dest, "index.html", (f"<{landmark} ", "<div "), (f"</{landmark}>", "</div>")
        )


def test_unknown_base_is_rejected(tmp_path: Path) -> None:
    """tokens.json に無いベースカラーは落ちる。"""
    dest = _copy_site(tmp_path)
    assert "data-base" in _edit(dest, "index.html", ('data-base="neutral"', 'data-base="mauve"'))


def test_missing_theme_attribute_is_rejected(tmp_path: Path) -> None:
    """data-theme が light/dark 以外なら落ちる。"""
    dest = _copy_site(tmp_path)
    assert "data-theme" in _edit(dest, "index.html", ('data-theme="light"', 'data-theme="auto"'))


def test_invalid_page_kind_fails(tmp_path: Path) -> None:
    """data-page-kind は dashboard か content のみ。"""
    dest = _copy_site(tmp_path)
    assert "data-page-kind" in _edit(
        dest, "index.html", ('data-page-kind="dashboard"', 'data-page-kind="blog"')
    )


def test_stylesheet_order_is_enforced(tmp_path: Path) -> None:
    """tokens.css → styles.css の順でなければ落ちる。"""
    dest = _copy_site(tmp_path)
    assert "順で読み込まれていない" in _edit(
        dest,
        "index.html",
        ('<link rel="stylesheet" href="tokens.css">\n  <link rel="stylesheet" href="styles.css">',
         '<link rel="stylesheet" href="styles.css">\n  <link rel="stylesheet" href="tokens.css">'),
    )


def test_dashboard_data_contract_failures(tmp_path: Path) -> None:
    """指標・原点 0・表・desc の欠落がそれぞれ違反になる。"""
    dest = _copy_site(tmp_path)
    assert "主要指標" in _edit(dest, "index.html", ('data-metric="true"', "data-m"), ("card metric reveal", "card reveal"))

    dest = _copy_site(tmp_path, "s2")
    assert "原点 0" in _edit(dest, "index.html", ('data-origin="0"', 'data-origin="1"'))

    dest = _copy_site(tmp_path, "s3")
    assert "表が無い" in _edit(dest, "index.html", ("<table", "<div class=\"t\""), ("</table>", "</div>"))

    dest = _copy_site(tmp_path, "s4")
    assert "<desc>" in _edit(dest, "index.html", ("<desc", "<p data-desc"), ("</desc>", "</p>"))


def test_svg_removal_is_reported(tmp_path: Path) -> None:
    """SVG を消す・role=img を外すとそれぞれ違反になる。"""
    dest = _copy_site(tmp_path)
    assert "チャート SVG" in _edit(dest, "index.html", ("<svg", "<div"), ("</svg>", "</div>"))
    dest = _copy_site(tmp_path, "s2")
    assert "svg[role=img]" in _edit(dest, "index.html", ('role="img"', 'role="presentation"'))


def test_content_page_skips_dashboard_structure(tmp_path: Path) -> None:
    """content ページには指標や図を要求しない。"""
    dest = _copy_site(tmp_path)
    _edit(dest, "index.html", ('data-page-kind="dashboard"', 'data-page-kind="content"'))
    violations = _edit(
        dest,
        "index.html",
        ("<table", '<div class="t"'),
        ("</table>", "</div>"),
        ('data-origin="0"', ""),
    )
    assert violations == ""


def test_script_in_body_is_reported(tmp_path: Path) -> None:
    """app.js を body 末尾へ移すと落ちる（初回描画で白がちらつく）。"""
    dest = _copy_site(tmp_path)
    assert "白のちらつき" in _edit(
        dest,
        "index.html",
        ('<script src="app.js" defer></script>', ""),
        ("</body>", '<script src="app.js"></script></body>'),
    )


# --- CSS の契約 --------------------------------------------------------------


def test_missing_role_variable_use_is_reported(tmp_path: Path) -> None:
    """設計の骨格になる変数を使わなくなると落ちる。"""
    dest = _copy_site(tmp_path)
    assert "--ring" in _edit(dest, "styles.css", ("var(--ring)", "currentColor"))


def test_required_style_vars_are_shadcn_roles() -> None:
    """必須変数の一覧が shadcn/ui の役割名で構成されている。"""
    assert "--background" in check_site.REQUIRED_STYLE_VARS
    assert "--radius" in check_site.REQUIRED_STYLE_VARS
    assert "--font-sans" in check_site.REQUIRED_STYLE_VARS
    assert not any(name.startswith("--md-sys") for name in check_site.REQUIRED_STYLE_VARS)


def test_missing_breakpoints_and_fluid_primitives(tmp_path: Path) -> None:
    """Tailwind のブレークポイント・minmax・clamp・focus-visible が要る。"""
    dest = _copy_site(tmp_path)
    assert "1024px" in _edit(dest, "styles.css", ("min-width: 1024px", "min-width: 1023px"))

    dest = _copy_site(tmp_path, "s2")
    assert "minmax" in _edit(dest, "styles.css", ("minmax(", "calc("))

    dest = _copy_site(tmp_path, "s3")
    assert "clamp" in _edit(dest, "styles.css", ("clamp(", "max("))

    dest = _copy_site(tmp_path, "s4")
    assert "focus-visible" in _edit(dest, "styles.css", (":focus-visible", ":focus-within"))


def test_required_breakpoints_follow_tailwind() -> None:
    """必須ブレークポイントが Tailwind の md / lg / xl である。"""
    assert check_site.REQUIRED_BREAKPOINTS == (768, 1024, 1280)


def test_reduced_motion_block_is_required(tmp_path: Path) -> None:
    """モーション低減ブロックが無いと落ちる。"""
    dest = _copy_site(tmp_path)
    assert "prefers-reduced-motion" in _edit(
        dest, "styles.css", ("@media (prefers-reduced-motion: reduce)", "@media (min-width: 1px)")
    )


def test_reduced_motion_block_must_stop_both_animation_and_transition(tmp_path: Path) -> None:
    """animation か transition のどちらかしか止めないと落ちる。"""
    dest = _copy_site(tmp_path)
    assert "transition を止めていない" in _edit(
        dest, "styles.css", ("    transition: none !important;\n", "")
    )
    dest = _copy_site(tmp_path, "s2")
    assert "animation を止めていない" in _edit(
        dest, "styles.css", ("    animation: none !important;\n", "")
    )


def test_resting_state_must_stay_visible(tmp_path: Path) -> None:
    """静止状態を不可視にする書き方は落ちる。"""
    dest = _copy_site(tmp_path)
    assert "forwards" in _append(dest, "styles.css", "\n.x { animation: rise 1s forwards; }\n")

    dest = _copy_site(tmp_path, "s2")
    assert "forwards" in _append(dest, "styles.css", "\n.x { animation-fill-mode: forwards; }\n")

    dest = _copy_site(tmp_path, "s3")
    assert "opacity: 0" in _append(dest, "styles.css", "\n.reveal { opacity: 0; }\n")


def test_unscoped_fill_mode_both_is_reported(tmp_path: Path) -> None:
    """[data-animate] の外で both / backwards を使うと落ちる。

    遅延中と停止中に `from` の状態が貼り付くため、背面タブでは要素が
    見えないまま固まる。実際にこの形で出荷しかけた。
    """
    dest = _copy_site(tmp_path)
    assert "both/backwards" in _append(
        dest, "styles.css", "\n.x { animation: rise 300ms both; }\n"
    )
    dest = _copy_site(tmp_path, "s2")
    assert "both/backwards" in _append(
        dest, "styles.css", "\n.x { animation-fill-mode: backwards; }\n"
    )


def test_scoped_fill_mode_both_is_allowed(tmp_path: Path) -> None:
    """[data-animate] の下なら both を使ってよい（JS が画面内でだけ起動する）。"""
    dest = _copy_site(tmp_path)
    assert (
        _append(dest, "styles.css", '\n[data-animate="in"] .x { animation: rise 300ms both; }\n')
        == ""
    )


def test_resting_state_allows_opacity_zero_inside_keyframes(tmp_path: Path) -> None:
    """@keyframes の中の opacity: 0 は正しい書き方なので通す。"""
    dest = _copy_site(tmp_path)
    assert _append(
        dest, "styles.css", "\n@keyframes slide {\n  from {\n    opacity: 0;\n  }\n}\n"
    ) == ""


def test_fixed_canvas_width_is_reported(tmp_path: Path) -> None:
    """ブレークポイント以上の固定幅は落ちる。"""
    dest = _copy_site(tmp_path)
    assert "固定幅" in _append(dest, "styles.css", "\n.x { width: 1280px; }\n")


def test_small_fixed_width_is_allowed(tmp_path: Path) -> None:
    """アイコンなどの小さな固定幅は通す。"""
    dest = _copy_site(tmp_path)
    assert _append(dest, "styles.css", "\n.x { width: 320px; }\n") == ""


def test_narrow_max_width_is_reported(tmp_path: Path) -> None:
    """狭い max-width は広い画面を捨てるので落ちる。"""
    dest = _copy_site(tmp_path)
    assert "広い画面を捨てている" in _append(dest, "styles.css", "\n.x { max-width: 960px; }\n")


def test_media_query_max_width_is_not_a_fixed_width(tmp_path: Path) -> None:
    """メディアクエリの max-width は固定幅ではない。"""
    dest = _copy_site(tmp_path)
    assert _append(dest, "styles.css", "\n@media (max-width: 500px) { .x { gap: 4px; } }\n") == ""


def test_tiny_font_is_reported(tmp_path: Path) -> None:
    """12px 未満の font-size は落ちる。"""
    dest = _copy_site(tmp_path)
    assert "12px 未満" in _append(dest, "styles.css", "\n.x { font-size: 10px; }\n")
    dest = _copy_site(tmp_path, "s2")
    assert _append(dest, "styles.css", "\n.x { font-size: 12px; }\n") == ""


# --- 手書きの色 --------------------------------------------------------------


def test_raw_hex_in_authored_css_is_reported(tmp_path: Path) -> None:
    """手書き CSS の hex は落ちる。"""
    dest = _copy_site(tmp_path)
    assert "生の hex" in _append(dest, "styles.css", "\n.x { color: #ff0000; }\n")


def test_raw_color_function_in_authored_css_is_reported(tmp_path: Path) -> None:
    """手書き CSS の色関数（oklch / rgb / hsl）は落ちる。"""
    for snippet, needle in (
        ("\n.x { color: oklch(0.5 0.1 200); }\n", "oklch()"),
        ("\n.x { color: rgb(0 0 0); }\n", "rgb()"),
        ("\n.x { color: hsl(200 50% 50%); }\n", "hsl()"),
    ):
        dest = _copy_site(tmp_path, f"s{needle[:3]}")
        assert needle in _append(dest, "styles.css", snippet)


def test_color_mix_and_var_are_allowed(tmp_path: Path) -> None:
    """color-mix と var は通す（トークン経由の合成は正しい書き方）。

    ただし文字色を透明と混ぜるのは別（可読性契約を割るので拒否する）。
    """
    dest = _copy_site(tmp_path)
    assert (
        _append(
            dest,
            "styles.css",
            "\n.x { background: color-mix(in oklab, var(--primary) 40%, transparent);"
            " color: color-mix(in oklab, var(--primary) 60%, var(--foreground)); }\n",
        )
        == ""
    )


def test_named_color_fails(tmp_path: Path) -> None:
    """名前付き色は落ちる。"""
    dest = _copy_site(tmp_path)
    assert "名前付き色" in _append(dest, "styles.css", "\n.x { background: teal; }\n")


def test_hyphenated_color_property_is_not_a_blind_spot(tmp_path: Path) -> None:
    """border-top-color のようなハイフン付きプロパティも見る。"""
    dest = _copy_site(tmp_path)
    assert "名前付き色" in _append(dest, "styles.css", "\n.x { border-top-color: navy; }\n")


def test_svg_geometry_keywords_are_not_treated_as_colors() -> None:
    """stroke-linecap のような幾何プロパティを色と誤認しない。"""
    text = "path { stroke-linecap: butt; background-blend-mode: multiply; }"
    assert check_site._validate_authored_colors(text) == []


# --- JS の契約 ---------------------------------------------------------------


def test_js_contract_failures(tmp_path: Path) -> None:
    """テーマ・ベース・永続・低減設定の扱いが欠けると落ちる。"""
    for old, new, needle in (
        ("localStorage", "sessionStorage", "localStorage"),
        ("prefers-reduced-motion", "prefers-color", "prefers-reduced-motion"),
        ("aria-pressed", "data-pressed", "aria-pressed"),
    ):
        dest = _copy_site(tmp_path, f"s-{needle[:6]}")
        assert needle in _edit(dest, "app.js", (old, new))


def test_js_must_handle_the_base_attribute(tmp_path: Path) -> None:
    """app.js が data-base を扱わなくなると落ちる。"""
    dest = _copy_site(tmp_path)
    assert "data-base" in _edit(dest, "app.js", ("dataset.base", "dataset.palette"), ("data-base", "data-x"))


def test_empty_and_missing_files_are_reported(tmp_path: Path) -> None:
    """空の app.js と欠けたファイルはそれぞれ違反になる。"""
    dest = _copy_site(tmp_path)
    (dest / "app.js").write_text("", encoding="utf-8")
    assert "app.js が空" in "\n".join(check_site.validate_site(dest))

    dest = _copy_site(tmp_path, "s2")
    (dest / "styles.css").unlink()
    assert check_site.validate_site(dest) == ["styles.css が無い"]


def test_theme_js_guards_localstorage_failures() -> None:
    """localStorage が使えない環境でも落ちないよう try/catch で包む。"""
    script = (_TEMPLATE / "app.js").read_text(encoding="utf-8")
    assert script.count("try {") >= 2
    assert "catch" in script


def test_reveal_animation_waits_for_a_visible_tab() -> None:
    """背面タブでは入場アニメーションを起動しない（from の状態で固まるため）。"""
    script = (_TEMPLATE / "app.js").read_text(encoding="utf-8")
    assert "visibilityState" in script
    assert "visibilitychange" in script


# --- CLI ---------------------------------------------------------------------


def test_cli_fail_on_broken_site(tmp_path: Path) -> None:
    """違反があれば CLI は exit 1 を返す。"""
    dest = _copy_site(tmp_path)
    _edit(dest, "styles.css", ("var(--ring)", "currentColor"))
    assert check_site.main([str(dest)]) == 1


def test_cli_errors_when_tokens_missing(tmp_path: Path) -> None:
    """tokens.json が見つからなければ exit 2 を返す。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert check_site.main([str(empty)]) == 2


def test_default_tokens_path_resolution(tmp_path: Path) -> None:
    """サイト内の tokens.json を優先し、無ければ skill 側を辿る。"""
    dest = _copy_site(tmp_path)
    assert check_site.default_tokens_path(dest) == dest / "tokens.json"
    (dest / "tokens.json").unlink()
    with pytest.raises(FileNotFoundError):
        check_site.default_tokens_path(dest)
    assert check_site.default_tokens_path(_TEMPLATE) == _TOKENS


def test_cli_runs_as_a_subprocess() -> None:
    """スキルの手順どおり python3 で直接叩いても PASS する。"""
    result = subprocess.run(
        [sys.executable, str(_CHECK_SITE), str(_TEMPLATE)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "PASS" in result.stdout


# --- E2E ---------------------------------------------------------------------


def test_e2e_user_opens_static_site_and_can_choose_modes() -> None:
    """静的サイトを開いた利用者がテーマとベースカラーを選べて、選択が残る。"""
    html = (_TEMPLATE / "index.html").read_text(encoding="utf-8")
    script = (_TEMPLATE / "app.js").read_text(encoding="utf-8")
    for theme in check_site.THEMES:
        assert f'data-theme-value="{theme}"' in html
    for base_id in BASES:
        assert f'data-base-value="{base_id}"' in html
    assert "localStorage" in script
    assert "prefers-color-scheme: dark" in script
    assert '<script src="app.js" defer></script>' in html


def test_e2e_browser_script_shares_the_python_thresholds() -> None:
    """ブラウザ実測スクリプトが Python 側と同じ閾値・同じペアを見る。

    ペア集合は両実装から機械的に導出して双方向で突き合わせる。片側にだけ
    役割が増えても、包含チェックでは気づけない。
    """
    script = (_TEMPLATE / "e2e_contrast.js").read_text(encoding="utf-8")
    assert "const TEXT_MIN = 4.5;" in script
    assert "const MARK_MIN = 3.0;" in script
    for base_id in BASES:
        assert f'"{base_id}"' in script

    minimums = {"TEXT_MIN": 4.5, "MARK_MIN": 3.0}
    literal = {
        (foreground, background, minimums[name])
        for foreground, background, name in re.findall(
            r'\["(--[\w-]+)", "(--[\w-]+)", (TEXT_MIN|MARK_MIN)\]', script
        )
    }
    mapped = {
        (chart, background, minimums[name])
        for background, name in re.findall(
            r'CHARTS\.map\(\(name\) => \[name, "(--[\w-]+)", (TEXT_MIN|MARK_MIN)\]\)', script
        )
        for chart in check_site.CHART_VARS
    }
    assert literal | mapped == set(check_site.CONTRAST_CONTRACT)


def test_e2e_browser_script_reads_real_pixels() -> None:
    """実測は canvas のピクセルで行う。

    `getComputedStyle` は `oklch()` を `oklch()` のまま返すので、文字列から
    数値を拾うと L C H を R G B と取り違えて全ペアが「ほぼ黒」になり、
    検査が丸ごと嘘をつく。
    """
    script = (_TEMPLATE / "e2e_contrast.js").read_text(encoding="utf-8")
    assert "getImageData" in script
    assert "createElement(\"canvas\")" in script


def test_e2e_script_measures_rendered_text_not_just_variables() -> None:
    """実測は変数ペアだけでなく、実際に描かれた文字も見る。

    `color-mix` や不透明度で作った文字色はどちらの変数にも現れないので、
    ペア表だけでは見張れない。サイドバーのラベルが 2.2:1 のまま
    全チェック緑になった実例がある。
    """
    script = (_TEMPLATE / "e2e_contrast.js").read_text(encoding="utf-8")
    assert "measureRenderedText" in script
    assert "backdropOf" in script
    assert "textFailures" in script
    # 判定は両方のパスを含む。
    assert "failures.length === 0 && textFailures.length === 0" in script


def test_e2e_script_freezes_transitions_while_measuring() -> None:
    """測定中は色遷移を止める。

    テーマ切替の色は 150ms かけて遷移するので、止めずに読むと
    「切り替え前の色」で比を測り、無関係な要素が大量に赤くなる。
    """
    script = (_TEMPLATE / "e2e_contrast.js").read_text(encoding="utf-8")
    assert "transition: none !important" in script
    assert "freeze.remove()" in script


def test_e2e_rendered_chart_marks_stay_visible_in_both_themes() -> None:
    """テンプレートが実際に使う系列色が、置かれる面の上で見える。"""
    html = (_TEMPLATE / "index.html").read_text(encoding="utf-8")
    used = {name for name in check_site.CHART_VARS if f"var({name})" in html}
    assert used, "テンプレートが系列色を使っていない"
    for base_id in BASES:
        for theme in check_site.THEMES:
            colors = check_site.resolved_hex(TOKENS, base_id, theme)
            for name in used:
                for surface in ("--card", "--background"):
                    ratio = check_site.contrast_ratio(colors[name], colors[surface])
                    assert ratio >= 3.0, f"{theme}/{base_id} {name} on {surface}: {ratio:.2f}"


def test_e2e_animation_never_hides_content_permanently() -> None:
    """アニメーションが走らなくても中身が読める書き方になっている。

    入場は JS が付ける data-animate の下だけで起き、静止状態は完成形。
    """
    css = (_TEMPLATE / "styles.css").read_text(encoding="utf-8")
    assert check_site._validate_resting_state(css) == []
    assert '[data-animate="in"] .series-line {' in css
    assert '[data-animate="in"] .bar {' in css
    assert '[data-animate="in"] .arc {' in css
    assert ".series-line {\n  fill: none;" in css


def test_e2e_arc_offset_is_left_to_the_svg_attribute() -> None:
    """円弧の開始位置を CSS で宣言しない。

    CSS は presentation attribute より優先されるため、ここで
    `stroke-dashoffset` を宣言すると全セグメントが 12 時から重なって描かれる。
    """
    css = (_TEMPLATE / "styles.css").read_text(encoding="utf-8")
    arc_block = css.split(".arc {", 1)[1].split("}", 1)[0]
    assert "stroke-dashoffset" not in arc_block
    assert "@keyframes arc-draw {" in css


def test_e2e_squashed_sparkline_does_not_use_dash_drawing() -> None:
    """潰した SVG のドローは dasharray ではなく clip-path で行う。

    `preserveAspectRatio="none"` はパスの実長を変えるので、dasharray の
    ドローだと線が途中で切れて見える。
    """
    html = (_TEMPLATE / "index.html").read_text(encoding="utf-8")
    css = (_TEMPLATE / "styles.css").read_text(encoding="utf-8")
    assert 'class="metric__spark" role="img" viewBox="0 0 200 48" preserveAspectRatio="none"' in html
    assert "stroke-dasharray" not in html.split('class="metric__spark"', 1)[1].split("</svg>", 1)[0]
    assert '[data-animate="in"] .metric__spark {' in css
    assert "@keyframes wipe {" in css


def test_e2e_charts_stay_legible_on_small_screens() -> None:
    """狭い画面では図を等倍のまま横スクロールさせる。"""
    html = (_TEMPLATE / "index.html").read_text(encoding="utf-8")
    css = (_TEMPLATE / "styles.css").read_text(encoding="utf-8")
    assert html.count('class="chart-scroll"') >= 2
    assert ".chart-scroll {\n  overflow-x: auto;" in css
    # 具体的な px は固定しない。値の妥当性は `_validate_axis_text_scale` が
    # viewBox 幅との比で判定するので、ここは「下限が宣言されている」ことだけ見る
    # （ADR-0023: 検知器のある固定値は二重に書かない）。
    assert re.search(r"\.chart \{[^}]*min-inline-size:\s*\d+px", css, re.DOTALL)


def test_e2e_metric_direction_and_sentiment_are_separate() -> None:
    """バッジの色は良し悪し、アイコンは増減の向きで、両者を混ぜない。"""
    html = (_TEMPLATE / "index.html").read_text(encoding="utf-8")
    css = (_TEMPLATE / "styles.css").read_text(encoding="utf-8")
    assert ".badge--good {" in css and ".badge--bad {" in css
    assert "badge--up" not in css and "badge--down" not in css
    # 下向き矢印に good が付く指標（解約率）が実在する＝反転していない証拠。
    good_badges = html.split('class="badge badge--good"')
    assert any('d="M12 5v13"' in chunk.split("</span>", 1)[0] for chunk in good_badges[1:])


def test_e2e_sidebar_collapses_on_narrow_screens() -> None:
    """狭い画面ではサイドバーをオフキャンバスにして開閉できる。"""
    html = (_TEMPLATE / "index.html").read_text(encoding="utf-8")
    css = (_TEMPLATE / "styles.css").read_text(encoding="utf-8")
    script = (_TEMPLATE / "app.js").read_text(encoding="utf-8")
    assert 'data-sidebar="open"' in html and 'data-sidebar="close"' in html
    assert 'aria-controls="sidebar"' in html
    assert '.sidebar[data-open="true"] {' in css
    assert "@media (min-width: 768px)" in css
    assert 'event.key === "Escape"' in script


# --- ドキュメント -------------------------------------------------------------


def test_skill_and_design_docs_describe_shadcn() -> None:
    """SKILL.md と design.md が shadcn/ui の語彙で書かれている。"""
    skill = (_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    design = (_SKILL_DIR / "references" / "design.md").read_text(encoding="utf-8")
    assert "shadcn/ui" in skill and "Material Design" not in skill
    assert "shadcn/ui" in design and "Material Design" not in design
    assert "data-base" in skill
    for base_id in BASES:
        assert base_id in skill
    # React を足す改修を将来の担当者が始めない理由が書いてある。
    assert "React" in design


def test_evals_reference_the_shadcn_contract() -> None:
    """eval 群がベースカラーと静止状態の契約を検査対象にしている。"""
    evals = json.loads((_SKILL_DIR / "evals" / "evals.json").read_text(encoding="utf-8"))
    assert evals["skill_name"] == "html-gen"
    text = json.dumps(evals, ensure_ascii=False)
    assert "shadcn" in text
    assert "data-base" in text
    assert "Material Design" not in text
    named = {base_id for base_id in BASES if f"data-base is {base_id}" in text}
    assert len(named) >= 3


def test_chart_axis_removal_is_reported(tmp_path: Path) -> None:
    """目盛りラベル・目盛り線を消すとそれぞれ違反になる。

    テンプレート同梱のチャートは正しい軸を持つが、モデルが新しく書いた図から
    軸が落ちても検知器が無い間は全項目が合格していた。両方向を固定する。
    """
    dest = _copy_site(tmp_path)
    assert "目盛りラベル" in _edit(dest, "index.html", ('class="axis-text"', 'class="ax"'))

    dest = _copy_site(tmp_path, "s2")
    assert "目盛り線" in _edit(dest, "index.html", ('class="grid-line"', 'class="gl"'))


def test_cartesian_chart_detection_excludes_radial_and_stretched() -> None:
    """直交軸の判定がマークの種類で行われること。

    ドーナツは角度で、スパークラインは潰した面で符号化するので直交軸を持たない。
    class 名ではなくマークで判定するため、命名を変えても判定は動かない。
    """
    assert check_site._is_cartesian_chart('<svg><rect class="bar" /></svg>') is True
    assert check_site._is_cartesian_chart('<svg><path class="series-line" /></svg>') is True
    assert check_site._is_cartesian_chart('<svg><circle class="arc" /></svg>') is False
    assert (
        check_site._is_cartesian_chart(
            '<svg preserveAspectRatio="none"><path class="series-area" /></svg>'
        )
        is False
    )


def test_stretched_svg_with_text_is_reported() -> None:
    """潰し描画の SVG が文字を持つと違反になる（縦横独立に伸びて歪む）。"""
    stretched = '<svg preserveAspectRatio="none"><text>1月</text></svg>'
    assert check_site._validate_stretched_svg_text(stretched) != []
    assert check_site._validate_stretched_svg_text('<svg preserveAspectRatio="none"></svg>') == []


def test_chart_form_rules_are_enforced() -> None:
    """数えれば決まる種別規則 3 つがそれぞれ違反になる。"""
    donut = "<svg>" + '<circle class="arc" />' * 6 + "</svg>"
    assert "ドーナツ" in "\n".join(check_site._validate_chart_form(donut))

    single = '<svg><rect class="bar" width="10" height="50" /></svg>'
    assert "棒 1 本" in "\n".join(check_site._validate_chart_form(single))

    many = "<svg>" + '<rect class="bar" width="10" height="50" />' * 8 + "</svg>"
    assert "縦棒" in "\n".join(check_site._validate_chart_form(many))

    wide = "<svg>" + '<rect class="bar" width="50" height="10" />' * 8 + "</svg>"
    assert check_site._validate_chart_form(wide) == []


def test_bar_orientation_is_read_from_dimensions() -> None:
    """棒の向きは class 名ではなく寸法で判定される。"""
    assert check_site._is_vertical_bar('<rect class="bar" width="10" height="50" />') is True
    assert check_site._is_vertical_bar('<rect class="bar" width="50" height="10" />') is False
    assert check_site._is_vertical_bar('<rect class="bar" />') is False


def test_fixed_chart_height_is_reported(tmp_path: Path) -> None:
    """チャート高さの固定 px 上限が違反になる。

    `_validate_fluid_width` が横方向で禁じているのと同じ誤りを縦方向でも落とす。
    ビューポートに追従する式（clamp / vh）は通る。
    """
    dest = _copy_site(tmp_path)
    assert "max-block-size" in _edit(
        dest, "styles.css", ("max-block-size: clamp(320px, 46vh, 620px)", "max-block-size: 320px")
    )
    assert check_site._validate_fluid_height("a{max-block-size: clamp(320px, 46vh, 620px);}") == []
    assert check_site._validate_fluid_height("a{max-block-size: 60vh;}") == []


def test_dashed_gridline_is_reported(tmp_path: Path) -> None:
    """破線の目盛り線が違反になる（破線は予測・しきい値の記法）。"""
    dest = _copy_site(tmp_path)
    assert "破線" in _edit(
        dest, "styles.css", ("  stroke-width: 1;\n}\n\n/* viewBox", "  stroke-width: 1;\n  stroke-dasharray: 3 4;\n}\n\n/* viewBox")
    )


def test_axis_text_rendered_size_is_enforced(tmp_path: Path) -> None:
    """目盛り文字が描画時に 12px を割る組み合わせが違反になる。

    SVG の文字は viewBox の倍率で縮むため、`font-size: 12px` の宣言でも実際には
    12px を割る。ブラウザ実測 7.7px を静的計算が再現することを固定する。
    """
    dest = _copy_site(tmp_path)
    report = _edit(dest, "styles.css", ("font-size: 15px", "font-size: 12px"))
    assert "目盛り文字" in report
    assert "10.0px" in report


def test_axis_text_scale_needs_a_declared_font_size() -> None:
    """`.axis-text` の font-size が読めなければ違反として報告されること。"""
    assert check_site._validate_axis_text_scale("<svg>axis-text</svg>", "") != []


def test_css_length_returns_none_for_missing_declarations() -> None:
    """セレクタもプロパティも無ければ None を返すこと。"""
    assert check_site._css_length("", ".chart", "min-inline-size") is None
    assert check_site._css_length(".chart{color:red}", ".chart", "min-inline-size") is None


def test_axis_text_scale_skips_charts_without_measurable_geometry() -> None:
    """viewBox・class・最小幅のどれかが無い SVG は計算対象にならないこと。"""
    css = ".axis-text{font-size:15px}.chart{min-inline-size:600px}"
    assert check_site._validate_axis_text_scale('<svg class="chart">axis-text</svg>', css) == []
    assert check_site._validate_axis_text_scale('<svg viewBox="0 0 720 250">axis-text</svg>', css) == []
    assert check_site._validate_axis_text_scale("<svg>no ticks</svg>", css) == []
    assert (
        check_site._validate_axis_text_scale(
            '<svg class="chart" viewBox="0 0 720 250">axis-text</svg>', ".axis-text{font-size:15px}"
        )
        == []
    )


def test_chart_without_scroll_wrapper_is_reported() -> None:
    """最小幅を持つチャートが .chart-scroll の外にあると違反になる。

    器が無いと SVG がページごと横へはみ出す（実測: 幅 1269px の画面で文書幅
    1422px）。ドーナツは最小幅を持たないので対象外。
    """
    assert check_site._validate_chart_scroll_wrapper('<div><svg class="chart">') != []
    assert check_site._validate_chart_scroll_wrapper('<div class="chart-scroll"><svg class="chart">') == []
    assert check_site._validate_chart_scroll_wrapper('<div><svg class="chart chart--donut">') == []


def test_scatter_and_stack_marks_are_checked_for_axes() -> None:
    """散布点・積み上げ区画も直交軸として軸検査の対象になること。

    `chart-forms.md` へ散布図と 100% 積み上げ横棒を足したとき、マーク class の
    一覧へ `point` / `seg` を足し忘れ、その 2 形だけが軸ゼロでも素通りしていた。
    形の語彙を広げて検知器を広げないと、新しい形だけが無検査になる。
    """
    scatter = '<svg class="chart chart--scatter" viewBox="0 0 720 380"><circle class="point" r="6"/></svg>'
    stack = '<svg class="chart chart--stack" viewBox="0 0 720 128"><rect class="seg" width="100"/></svg>'
    hbar = '<svg class="chart chart--hbars" viewBox="0 0 720 422"><rect class="bar--h" width="500"/></svg>'
    for name, svg in (("散布図", scatter), ("積み上げ", stack), ("横棒", hbar)):
        assert check_site._is_cartesian_chart(svg) is True, name
        assert len(check_site._validate_chart_axes(svg)) == 2, name


def test_tick_steps_must_be_nice_numbers() -> None:
    """目盛りの刻みが 1・2・5 の倍数でなければ違反になること。

    テンプレート自身が刻み 45 と 150（最大値を本数で割った端数）で出荷されていた。
    散文の規則だけでは守られない。
    """
    def svg(*labels: str) -> str:
        cells = "".join(f'<text class="axis-text" x="10" y="10">{label}</text>' for label in labels)
        return f"<svg>{cells}</svg>"

    assert check_site._validate_tick_steps(svg("0", "150", "300", "450")) != []
    assert check_site._validate_tick_steps(svg("60", "105", "150")) != []
    assert check_site._validate_tick_steps(svg("0", "100", "200", "300")) == []
    assert check_site._validate_tick_steps(svg("0", "2,000", "4,000")) == []
    assert check_site._validate_tick_steps(svg("0", "5", "10")) == []
    # 不等間隔
    assert "等間隔" in "\n".join(check_site._validate_tick_steps(svg("0", "100", "400")))
    # 分類ラベルは数値として読めないので対象外。目盛り 1 本以下も対象外
    assert check_site._validate_tick_steps(svg("1月", "2月", "3月")) == []
    assert check_site._validate_tick_steps(svg("100")) == []


def test_nice_step_rejects_non_decade_mantissas() -> None:
    """刻みの仮数判定が 1・2・5（と 10）だけを通すこと。"""
    assert check_site._is_nice_step(0.5) is True
    assert check_site._is_nice_step(20) is True
    assert check_site._is_nice_step(45) is False
    assert check_site._is_nice_step(25) is False
    assert check_site._is_nice_step(0) is False


def test_template_charts_use_nice_tick_steps() -> None:
    """テンプレート同梱の図が刻み規則を満たすこと（模写元が規則を破らない）。"""
    html = (_TEMPLATE / "index.html").read_text(encoding="utf-8")
    assert check_site._validate_tick_steps(html) == []
