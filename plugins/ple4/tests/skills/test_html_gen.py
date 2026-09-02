"""html-gen テンプレートの Material Design 3 準拠と静的サイト E2E。"""

from __future__ import annotations

import importlib.util
import json
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
SEEDS = check_site.seed_ids(TOKENS)


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
    """同梱テンプレートは Material 3 契約検査に合格する。"""
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


def test_generated_tokens_css_is_the_only_place_with_raw_hex() -> None:
    """手書きの 3 ファイルは生の hex を 1 つも持たない（色の正本は tokens.json）。"""
    for filename in ("index.html", "styles.css", "app.js"):
        text = (_TEMPLATE / filename).read_text(encoding="utf-8")
        assert check_site.HEX_RE.findall(text) == [], filename
    assert check_site.HEX_RE.findall((_TEMPLATE / "tokens.css").read_text(encoding="utf-8"))


# --- 色空間 -----------------------------------------------------------------


def test_norm_hex_expands_short_form() -> None:
    """3 桁・4 桁・8 桁 hex を 6 桁へ正規化する。"""
    assert check_site._norm_hex("#abc") == "#AABBCC"
    assert check_site._norm_hex("#abcd") == "#AABBCC"
    assert check_site._norm_hex("#AABBCCDD") == "#AABBCC"
    with pytest.raises(ValueError):
        check_site._norm_hex("blue")
    with pytest.raises(ValueError):
        check_site._norm_hex("#GGHHII")


def test_contrast_ratio_matches_wcag_reference_values() -> None:
    """既知の基準値でコントラスト計算そのものを確かめる。"""
    assert check_site.contrast_ratio("#000000", "#FFFFFF") == pytest.approx(21.0, abs=0.01)
    assert check_site.contrast_ratio("#FFFFFF", "#FFFFFF") == pytest.approx(1.0, abs=0.001)
    assert check_site.contrast_ratio("#767676", "#FFFFFF") == pytest.approx(4.54, abs=0.01)


def test_lab_conversion_matches_reference_values() -> None:
    """白・黒・中間グレーの L* が CIE の既知値と一致する。"""
    assert check_site.hex_to_lab("#FFFFFF")[0] == pytest.approx(100.0, abs=0.01)
    assert check_site.hex_to_lab("#000000")[0] == pytest.approx(0.0, abs=0.01)
    assert check_site.hex_to_lab("#777777")[0] == pytest.approx(50.03, abs=0.1)
    assert check_site.hex_to_lab("#FFFFFF")[1] == pytest.approx(0.0, abs=0.02)


def test_lch_round_trip_preserves_tone() -> None:
    """LCh から作った色を読み戻すと tone（L*）が保存される。"""
    for tone in (10, 30, 40, 60, 80, 90):
        for hue in (0, 90, 180, 270):
            produced = check_site.lch_to_hex(float(tone), 40.0, float(hue))
            assert check_site.hex_to_lch(produced)[0] == pytest.approx(tone, abs=1.0)


def test_lch_clamps_chroma_instead_of_tone_when_out_of_gamut() -> None:
    """域外の彩度は chroma を落として収める。tone は動かさない。

    tone がコントラストを担保しているので、ここで L* を動かすと契約が崩れる。
    """
    produced = check_site.lch_to_hex(50.0, 200.0, 140.0)
    lightness, chroma, _ = check_site.hex_to_lch(produced)
    assert lightness == pytest.approx(50.0, abs=1.0)
    assert chroma < 200.0


def test_extreme_tones_stay_in_range() -> None:
    """tone 0 / 100 は黒と白になる。"""
    assert check_site.lch_to_hex(0.0, 40.0, 30.0) == "#000000"
    assert check_site.lch_to_hex(100.0, 40.0, 30.0) == "#FFFFFF"
    assert check_site.lch_to_hex(-10.0, 40.0, 30.0) == "#000000"


def test_delta_e_is_zero_for_identical_colors() -> None:
    """同じ色の ΔE は 0、離れた色は大きい。"""
    assert check_site.delta_e("#123456", "#123456") == pytest.approx(0.0, abs=1e-9)
    assert check_site.delta_e("#000000", "#FFFFFF") == pytest.approx(100.0, abs=0.1)


# --- M3 との一致 ------------------------------------------------------------


def test_indigo_seed_reproduces_the_m3_baseline_scheme() -> None:
    """既定 seed のライト配色が M3 baseline の公表値とほぼ一致する。

    HCT ではなく LCh(ab) で近似しているので完全一致はしない。ΔE*ab 6 以内
    （並べて比べないと分からない差）に収まっていることを確認する。
    """
    light = check_site.scheme(TOKENS, "indigo", "light")
    baseline = {
        "--md-sys-color-primary": "#6750A4",
        "--md-sys-color-on-primary": "#FFFFFF",
        "--md-sys-color-primary-container": "#EADDFF",
        "--md-sys-color-surface": "#FEF7FF",
        "--md-sys-color-on-surface": "#1D1B20",
    }
    for name, expected in baseline.items():
        distance = check_site.delta_e(light[name], expected)
        assert distance <= 6.0, f"{name}: {light[name]} vs {expected} (ΔE {distance:.1f})"


def test_dark_scheme_flips_surface_and_text_for_every_seed() -> None:
    """全 seed でライト/ダークが実際に反転している。"""
    for seed_id in SEEDS:
        light = check_site.scheme(TOKENS, seed_id, "light")
        dark = check_site.scheme(TOKENS, seed_id, "dark")
        assert light["--md-sys-color-surface"] != dark["--md-sys-color-surface"]
        assert light["--md-sys-color-on-surface"] != dark["--md-sys-color-on-surface"]
        assert check_site.relative_luminance(light["--md-sys-color-surface"]) > (
            check_site.relative_luminance(dark["--md-sys-color-surface"])
        )


def test_primary_chroma_floor_lifts_a_desaturated_seed() -> None:
    """彩度の低い seed でも主色が灰色に潰れない（chroma floor）。"""
    tokens = json.loads(_TOKENS.read_text(encoding="utf-8"))
    tokens["seeds"]["indigo"]["hex"] = "#6E6A73"
    palettes = check_site.tonal_palettes(tokens, "indigo")
    assert check_site.hex_to_lch(palettes["primary"][40])[1] > 20.0


def test_every_seed_has_its_own_hue() -> None:
    """seed ごとに主色の色相が異なる（選択の意味がある）。"""
    hues = {
        round(check_site.hex_to_lch(check_site.scheme(TOKENS, seed_id, "light")["--md-sys-color-primary"])[2])
        for seed_id in SEEDS
    }
    assert len(hues) == len(SEEDS)


# --- コントラスト契約 -------------------------------------------------------


def test_contrast_contract_holds_for_every_seed_and_theme() -> None:
    """全 seed × ライト/ダークが WCAG の下限を満たす。"""
    for seed_id in SEEDS:
        for theme in check_site.THEMES:
            resolved = check_site.scheme(TOKENS, seed_id, theme)
            assert check_site.contrast_violations(resolved, f"{theme}/{seed_id}") == []


def test_contrast_contract_covers_chart_series_and_status_colors() -> None:
    """契約が系列色・増減・主色を含む（抜けると回帰が素通りする）。"""
    pairs = {(fg, bg) for fg, bg, _ in check_site.CONTRAST_CONTRACT}
    for name in check_site.CHART_VARS:
        assert (name, "--md-sys-color-surface-container") in pairs
    for name in (
        "--md-sys-color-positive",
        "--md-sys-color-negative",
        "--md-sys-color-primary",
        "--md-sys-color-on-surface-variant",
    ):
        assert (name, "--md-sys-color-surface-container") in pairs


def test_sunken_chart_series_is_reported() -> None:
    """カード背景に沈む系列色を赤くできる。"""
    resolved = dict(check_site.scheme(TOKENS, "indigo", "dark"))
    resolved["--chart-1"] = resolved["--md-sys-color-surface-container"]
    violations = "\n".join(check_site.contrast_violations(resolved, "dark/indigo"))
    assert "--chart-1" in violations


def test_duplicate_series_is_reported() -> None:
    """系列色の重複を赤くできる。"""
    resolved = dict(check_site.scheme(TOKENS, "indigo", "light"))
    resolved["--chart-2"] = resolved["--chart-1"]
    assert "系列色に重複" in "\n".join(check_site.contrast_violations(resolved, "light/indigo"))


def test_indistinguishable_series_is_reported() -> None:
    """重複していなくても知覚差が小さい系列色は赤くできる。"""
    resolved = dict(check_site.scheme(TOKENS, "indigo", "light"))
    lightness, chroma, hue = check_site.hex_to_lch(resolved["--chart-1"])
    resolved["--chart-2"] = check_site.lch_to_hex(lightness, chroma, hue + 3)
    violations = "\n".join(check_site.contrast_violations(resolved, "light/indigo"))
    assert "色差" in violations


def test_series_stay_apart_in_every_generated_scheme() -> None:
    """生成される全スキームで系列色が互いに ΔE 下限以上離れている。"""
    for seed_id in SEEDS:
        for theme in check_site.THEMES:
            resolved = check_site.scheme(TOKENS, seed_id, theme)
            series = [resolved[name] for name in check_site.CHART_VARS]
            for left in range(len(series)):
                for right in range(left + 1, len(series)):
                    assert check_site.delta_e(series[left], series[right]) >= (
                        check_site.SERIES_MIN_DELTA_E
                    )


def test_broken_tokens_surface_the_contrast_failure(tmp_path: Path) -> None:
    """tokens.json 側で役割トーンを壊すと、生成 CSS 経由でも赤くなる。"""
    dest = _copy_site(tmp_path)
    tokens = json.loads((dest / "tokens.json").read_text(encoding="utf-8"))
    tokens["roles"]["--md-sys-color-on-surface"]["light"] = 95
    (dest / "tokens.json").write_text(json.dumps(tokens, ensure_ascii=False), encoding="utf-8")
    check_site.main(["--write-css", str(dest)])
    violations = "\n".join(check_site.validate_site(dest))
    assert "--md-sys-color-on-surface" in violations


# --- 生成 CSS ---------------------------------------------------------------


def test_tokens_css_has_a_block_for_every_seed_and_theme() -> None:
    """seed × テーマの全ブロックが配信 CSS に揃っている。"""
    css = (_TEMPLATE / "tokens.css").read_text(encoding="utf-8")
    for seed_id in SEEDS:
        for theme in check_site.THEMES:
            assert f'[data-theme="{theme}"][data-seed="{seed_id}"]' in css


def test_tokens_css_carries_the_full_m3_token_set() -> None:
    """配色以外のシステムトークン（タイポ / シェイプ / 影 / モーション）も出る。"""
    css = (_TEMPLATE / "tokens.css").read_text(encoding="utf-8")
    for needle in (
        "--md-sys-typescale-display-large-font",
        "--md-sys-shape-corner-extra-large",
        "--md-sys-elevation-level3",
        "--md-sys-motion-easing-emphasized-decelerate",
        "--md-sys-motion-duration-long2",
        "--md-sys-state-hover-opacity",
        "--md-ref-layout-breakpoint-expanded-px",
        "color-scheme: dark",
        "color-scheme: light",
    ):
        assert needle in css, needle


def test_seed_swatch_variables_are_theme_independent() -> None:
    """seed 選択 UI 用の色は :root にあり、テーマで動かない。"""
    css = (_TEMPLATE / "tokens.css").read_text(encoding="utf-8")
    for seed_id in SEEDS:
        assert f"--seed-{seed_id}:" in css
    assert css.index("--seed-indigo:") < css.index('[data-theme="light"][data-seed=')


def test_edited_tokens_css_is_reported(tmp_path: Path) -> None:
    """生成物を手で書き換えると不合格になる。"""
    dest = _copy_site(tmp_path)
    assert "tokens.css が tokens.json からの生成結果と一致しない" in _append(
        dest, "tokens.css", "\n:root { --rogue: 1; }\n"
    )


# --- HTML 契約 --------------------------------------------------------------


def test_missing_theme_toggle_fails(tmp_path: Path) -> None:
    """ライト/ダークトグルを外すと不合格になる。"""
    dest = _copy_site(tmp_path)
    assert "トグル" in _edit(dest, "index.html", ('data-theme-value="dark"', 'data-other="dark"'))


def test_theme_toggle_needs_type_and_aria_pressed(tmp_path: Path) -> None:
    """トグルの type と aria-pressed の欠落を検出する。"""
    dest = _copy_site(tmp_path)
    violations = _edit(dest, "index.html", ('type="button" data-theme-value', "data-theme-value"))
    assert "type=button" in violations
    dest = _copy_site(tmp_path, "no-aria")
    violations = _edit(dest, "index.html", ('aria-pressed="true"', 'data-x="true"'))
    assert "aria-pressed" in violations


def test_html_landmarks_skip_link_and_viewport(tmp_path: Path) -> None:
    """スキップリンク・ランドマーク・viewport の欠落を検出する。"""
    dest = _copy_site(tmp_path)
    violations = _edit(
        dest,
        "index.html",
        ('class="skip-link" href="#main"', 'class="home" href="/"'),
        ("<header", "<div"),
        ("</header>", "</div>"),
        ("<main", "<div"),
        ("</main>", "</div>"),
        ("<footer", "<div"),
        ("</footer>", "</div>"),
        ("width=device-width, initial-scale=1, viewport-fit=cover", "initial-scale=1"),
        ('<html lang="ja" data-theme="light"', "<html"),
    )
    assert "スキップリンク" in violations
    assert "<header>" in violations
    assert "<main>" in violations
    assert "<footer>" in violations
    assert "viewport meta" in violations
    assert "html[lang]" in violations
    assert "data-theme" in violations


def test_unknown_seed_is_rejected(tmp_path: Path) -> None:
    """tokens.json に無い seed 識別子は不合格になる。"""
    dest = _copy_site(tmp_path)
    assert "data-seed" in _edit(dest, "index.html", ('data-seed="indigo"', 'data-seed="tailwind"'))


def test_invalid_page_kind_fails(tmp_path: Path) -> None:
    """data-page-kind の未知値は不合格になる。"""
    dest = _copy_site(tmp_path)
    assert "data-page-kind" in _edit(
        dest, "index.html", ('data-page-kind="dashboard"', 'data-page-kind="app"')
    )


def test_stylesheet_order_is_enforced(tmp_path: Path) -> None:
    """tokens.css より先に styles.css を読むと不合格になる。"""
    dest = _copy_site(tmp_path)
    violations = _edit(
        dest,
        "index.html",
        ('<link rel="stylesheet" href="tokens.css">\n  <link rel="stylesheet" href="styles.css">',
         '<link rel="stylesheet" href="styles.css">\n  <link rel="stylesheet" href="tokens.css">'),
    )
    assert "tokens.css → styles.css の順" in violations


def test_dashboard_data_contract_failures(tmp_path: Path) -> None:
    """指標・SVG・代替テキスト・原点・表の欠落を検出する。"""
    dest = _copy_site(tmp_path)
    violations = _edit(
        dest,
        "index.html",
        ('data-metric="true"', 'data-x="true"'),
        ('class="card metric reveal"', 'class="card mtrc reveal"'),
        ('role="img"', 'role="presentation"'),
        ("<desc", "<span"),
        ('data-origin="0"', ""),
        ("<table", "<div"),
    )
    assert "主要指標" in violations
    assert "svg[role=img]" in violations
    assert "<desc>" in violations
    assert "data-origin" in violations
    assert "表が無い" in violations


def test_svg_removal_is_reported(tmp_path: Path) -> None:
    """ダッシュボードから SVG を全部消すと不合格になる。"""
    dest = _copy_site(tmp_path)
    assert "チャート SVG が無い" in _edit(dest, "index.html", ("<svg", "<div"), ("</svg>", "</div>"))


def test_content_page_skips_dashboard_structure(tmp_path: Path) -> None:
    """data-page-kind=content なら偽の指標や図が無くても合格する。"""
    dest = _copy_site(tmp_path)
    violations = _edit(
        dest,
        "index.html",
        ('data-page-kind="dashboard"', 'data-page-kind="content"'),
        ('data-metric="true"', 'data-x="true"'),
        ('data-origin="0"', ""),
    )
    assert violations == ""


def test_script_in_body_is_reported(tmp_path: Path) -> None:
    """app.js を body 末尾へ移すと（初回の白ちらつき）不合格になる。"""
    dest = _copy_site(tmp_path)
    violations = _edit(
        dest,
        "index.html",
        ('  <script src="app.js" defer></script>\n</head>', "</head>"),
        ("</body>", '  <script src="app.js" defer></script>\n</body>'),
    )
    assert "<head> で読み込まれていない" in violations


# --- 手書き CSS 契約 --------------------------------------------------------


def test_missing_role_variable_use_is_reported(tmp_path: Path) -> None:
    """役割変数を使わない CSS は不合格になる。"""
    dest = _copy_site(tmp_path)
    (dest / "styles.css").write_text("body { margin: 0; }\n", encoding="utf-8")
    violations = "\n".join(check_site.validate_site(dest))
    for name in check_site.REQUIRED_STYLE_VARS:
        assert name in violations


def test_missing_breakpoints_and_fluid_primitives(tmp_path: Path) -> None:
    """ブレークポイント・伸縮グリッド・流体タイポの欠落を検出する。"""
    dest = _copy_site(tmp_path)
    (dest / "styles.css").write_text("body { margin: 0; }\n", encoding="utf-8")
    violations = "\n".join(check_site.validate_site(dest))
    for width in check_site.REQUIRED_BREAKPOINTS:
        assert f"{width}px のブレークポイント" in violations
    assert "minmax" in violations
    assert "clamp" in violations


def test_reduced_motion_block_is_required(tmp_path: Path) -> None:
    """モーション低減ブロックが無いと不合格になる。"""
    dest = _copy_site(tmp_path)
    css = (dest / "styles.css").read_text(encoding="utf-8")
    (dest / "styles.css").write_text(
        css.replace("@media (prefers-reduced-motion: reduce) {", "@media screen {", 1), encoding="utf-8"
    )
    assert "prefers-reduced-motion" in "\n".join(check_site.validate_site(dest))


def test_reduced_motion_block_must_stop_both_animation_and_transition(tmp_path: Path) -> None:
    """低減ブロックが片方しか止めないと不合格になる。"""
    dest = _copy_site(tmp_path)
    css = (dest / "styles.css").read_text(encoding="utf-8")
    stripped = css[: css.index("@media (prefers-reduced-motion: reduce) {")]
    (dest / "styles.css").write_text(
        stripped + "@media (prefers-reduced-motion: reduce) {\n  .x { opacity: 1; }\n}\n",
        encoding="utf-8",
    )
    violations = "\n".join(check_site.validate_site(dest))
    assert "animation を止めていない" in violations
    assert "transition を止めていない" in violations


def test_fixed_canvas_width_is_reported(tmp_path: Path) -> None:
    """固定幅キャンバスは不合格になる（表示領域を使い切れない）。"""
    dest = _copy_site(tmp_path)
    assert "固定幅 1280px" in _append(dest, "styles.css", "\n.canvas { width: 1280px; }\n")


def test_small_fixed_width_is_allowed(tmp_path: Path) -> None:
    """アイコンやレールの実寸（ブレークポイント未満）は違反にしない。"""
    dest = _copy_site(tmp_path)
    assert _append(dest, "styles.css", "\n.rail { width: 320px; }\n") == ""


def test_narrow_max_width_is_reported(tmp_path: Path) -> None:
    """広い画面を捨てる max-width は不合格になる。"""
    dest = _copy_site(tmp_path)
    assert "広い画面を捨てている" in _append(dest, "styles.css", "\n.pane { max-width: 1100px; }\n")


def test_media_query_max_width_is_not_a_fixed_width(tmp_path: Path) -> None:
    """メディアクエリの max-width は固定幅として誤検出しない。"""
    dest = _copy_site(tmp_path)
    assert _append(dest, "styles.css", "\n@media (max-width: 839px) { .x { color: inherit; } }\n") == ""


def test_tiny_font_is_reported(tmp_path: Path) -> None:
    """12px 未満の font-size は不合格になる。"""
    dest = _copy_site(tmp_path)
    assert "12px 未満" in _append(dest, "styles.css", "\n.fine { font-size: 10px; }\n")


# --- 色の宇宙 ---------------------------------------------------------------


def test_raw_hex_in_authored_css_is_reported(tmp_path: Path) -> None:
    """手書き CSS に生の hex を足すと不合格になる。"""
    dest = _copy_site(tmp_path)
    assert "#0D1117" in _append(dest, "styles.css", "\n.rogue { color: #0D1117; }\n")


def test_named_color_fails(tmp_path: Path) -> None:
    """名前付き色は不合格になる。"""
    dest = _copy_site(tmp_path)
    assert "navy" in _edit(dest, "index.html", ("<body>", '<body style="color: navy">'))


def test_hyphenated_color_property_is_not_a_blind_spot(tmp_path: Path) -> None:
    """background-color など連結プロパティ経由の名前付き色も拒否する。"""
    dest = _copy_site(tmp_path)
    assert "crimson" in _edit(dest, "index.html", ("<body>", '<body style="background-color: crimson">'))


def test_svg_geometry_keywords_are_not_treated_as_colors() -> None:
    """stroke-linecap: butt のような非色プロパティを色と誤認しない。"""
    text = (
        ".a { stroke-linecap: butt; stroke-linejoin: miter; }"
        ".b { background-blend-mode: multiply; background-clip: padding-box; }"
        ":root { color-scheme: light dark; }"
    )
    assert check_site._validate_authored_colors(text) == []


def test_color_functions_are_allowed(tmp_path: Path) -> None:
    """color-mix / グラデーションは変数経由なので通す。"""
    dest = _copy_site(tmp_path)
    snippet = (
        "\n.mix { background: color-mix(in oklab, var(--md-sys-color-primary) 40%, transparent); }\n"
        ".grad { background: linear-gradient(90deg, var(--chart-1), var(--chart-2)); }\n"
    )
    assert _append(dest, "styles.css", snippet) == ""


# --- JS 契約 ----------------------------------------------------------------


def test_js_contract_failures(tmp_path: Path) -> None:
    """app.js から必須動作を削ると不合格になる。"""
    dest = _copy_site(tmp_path)
    (dest / "app.js").write_text("console.log('x');\n", encoding="utf-8")
    violations = "\n".join(check_site.validate_site(dest))
    for label in (
        "data-theme",
        "localStorage",
        "aria-pressed",
        "data-seed",
        "prefers-reduced-motion",
        "DOM 構築前の実行順",
        "light/dark",
    ):
        assert label in violations, label


def test_empty_and_missing_files_are_reported(tmp_path: Path) -> None:
    """必須ファイルの欠落と空の app.js を検出する。"""
    dest = _copy_site(tmp_path, "empty-js")
    (dest / "app.js").write_text("   \n", encoding="utf-8")
    assert any("app.js が空" in item for item in check_site.validate_site(dest))
    for filename in ("index.html", "styles.css", "tokens.css", "app.js"):
        dest = _copy_site(tmp_path, f"no-{filename}")
        (dest / filename).unlink()
        assert check_site.validate_site(dest) == [f"{filename} が無い"]


def test_theme_js_guards_localstorage_failures() -> None:
    """localStorage が使えない環境でも例外で止まらないよう try/catch がある。"""
    script = (_TEMPLATE / "app.js").read_text(encoding="utf-8")
    assert script.count("try {") >= 2
    assert "catch" in script


# --- CLI / ユーティリティ ---------------------------------------------------


def test_cli_fail_on_broken_site(tmp_path: Path) -> None:
    """違反があると CLI が 1 を返す。"""
    dest = _copy_site(tmp_path)
    (dest / "index.html").unlink()
    assert check_site.main([str(dest)]) == 1


def test_cli_errors_when_tokens_missing(tmp_path: Path) -> None:
    """tokens.json が解決できないと exit 2。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert check_site.main([str(empty)]) == 2


def test_default_tokens_path_resolution(tmp_path: Path) -> None:
    """テンプレートからは references/、サイト直下からは自身の tokens.json を選ぶ。"""
    assert check_site.default_tokens_path(_TEMPLATE) == _TOKENS
    dest = _copy_site(tmp_path)
    assert check_site.default_tokens_path(dest) == dest / "tokens.json"


# --- E2E --------------------------------------------------------------------


def test_e2e_user_opens_static_site_and_can_choose_modes() -> None:
    """静的ホストが返す 4 ファイルをユーザー視点で辿り、両モードと配色を確認する。"""
    html = (_TEMPLATE / "index.html").read_text(encoding="utf-8")
    script = (_TEMPLATE / "app.js").read_text(encoding="utf-8")
    assert 'data-theme-value="light"' in html
    assert 'data-theme-value="dark"' in html
    assert html.count('href="tokens.css"') == 1
    assert html.count('href="styles.css"') == 1
    assert html.count('src="app.js"') == 1
    assert "localStorage" in script
    completed = subprocess.run(
        [sys.executable, str(_CHECK_SITE), str(_TEMPLATE)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0
    assert "PASS" in completed.stdout


def test_e2e_browser_script_shares_the_python_thresholds() -> None:
    """ブラウザ側の受け入れ閾値が CONTRAST_CONTRACT と食い違わない。"""
    script = (_TEMPLATE / "e2e_contrast.js").read_text(encoding="utf-8")
    assert "const TEXT_MIN = 4.5;" in script
    assert "const MARK_MIN = 3.0;" in script
    assert {minimum for _, _, minimum in check_site.CONTRAST_CONTRACT} == {4.5, 3.0}
    for seed_id in SEEDS:
        assert f'"{seed_id}"' in script


def test_e2e_rendered_chart_marks_stay_visible_in_both_themes() -> None:
    """index.html が実際に参照する系列変数を解決し、両モードで沈まないことを見る。

    文字列一致では `--chart-6` がカード背景と同値でも通ってしまう。ここでは
    「その SVG が使っている変数」を列挙し、テーマごとに実際の色へ解決して比を測る。
    """
    html = (_TEMPLATE / "index.html").read_text(encoding="utf-8")
    used = {name for name in check_site.CHART_VARS if f"var({name})" in html}
    assert used, "テンプレートが系列色を 1 つも使っていない"
    for seed_id in SEEDS:
        for theme in check_site.THEMES:
            resolved = check_site.scheme(TOKENS, seed_id, theme)
            for name in used:
                ratio = check_site.contrast_ratio(
                    resolved[name], resolved["--md-sys-color-surface-container-low"]
                    if "--md-sys-color-surface-container-low" in resolved
                    else resolved["--md-sys-color-surface-container"]
                )
                assert ratio >= 3.0, f"{theme}/{seed_id} {name} が {ratio:.2f}"


def test_e2e_animation_never_hides_content_permanently() -> None:
    """入場アニメーションは from 側で隠す。静止状態は必ず読める。

    `.reveal { opacity: 0 }` のように「アニメーションが完走しないと見えない」
    書き方は、背面タブ・JS 無効・印刷で真っ白なページになる。
    """
    css = (_TEMPLATE / "styles.css").read_text(encoding="utf-8")
    assert "forwards" not in css, "fill-mode は both（from 側で隠す）にする"
    assert "@keyframes rise {\n  from {" in css
    assert ".reveal {\n  opacity: 0;\n}" not in css


def test_e2e_charts_stay_legible_on_small_screens() -> None:
    """狭い幅ではチャートを等倍で横スクロールさせ、軸文字を潰さない。"""
    css = (_TEMPLATE / "styles.css").read_text(encoding="utf-8")
    html = (_TEMPLATE / "index.html").read_text(encoding="utf-8")
    assert ".chart-scroll" in css
    assert "overflow-x: auto" in css
    assert "@container" in css
    assert html.count('class="chart-scroll"') >= 3
    assert "max-height" in css, "図が幅いっぱいの正方形へ膨らむのを止める"
