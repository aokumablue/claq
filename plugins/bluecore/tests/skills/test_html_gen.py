"""html-gen テンプレートの公式準拠と静的サイト E2E。"""

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


def _copy_site(tmp_path: Path, name: str = "site") -> Path:
    """検査可能なサイト一式を一時ディレクトリへ複製する。"""
    dest = tmp_path / name
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir()
    for filename in ("index.html", "styles.css", "theme.js", "check_site.py"):
        shutil.copy(_TEMPLATE / filename, dest / filename)
    shutil.copy(_TOKENS, dest / "tokens.json")
    return dest


def _edit_html(dest: Path, *replacements: tuple[str, str]) -> str:
    """index.html を置換して書き戻し、違反を 1 本の文字列で返す。"""
    html = (dest / "index.html").read_text(encoding="utf-8")
    for old, new in replacements:
        html = html.replace(old, new)
    (dest / "index.html").write_text(html, encoding="utf-8")
    return "\n".join(check_site.validate_site(dest))


def _roles(palette_id: str = "blue", theme: str = "light") -> dict[str, str]:
    """テンプレートの役割割当を取り出す。"""
    return check_site.theme_roles(check_site.load_tokens(_TOKENS), palette_id, theme)


# --- テンプレート本体 -------------------------------------------------------


def test_template_passes_validator() -> None:
    """同梱テンプレートは公式トークン検査に合格する。"""
    assert check_site.validate_site(_TEMPLATE) == []


def test_cli_pass_on_template() -> None:
    """CLI はテンプレートで exit 0 と PASS を返す。"""
    assert check_site.main([str(_TEMPLATE)]) == 0


def test_loading_check_site_leaves_no_bytecode_in_the_shipped_assets() -> None:
    """検査スクリプトの読み込みが配布ディレクトリに .pyc を落とさない。"""
    shutil.rmtree(_TEMPLATE / "__pycache__", ignore_errors=True)
    _load_check_site()
    assert not (_TEMPLATE / "__pycache__").exists()


def test_write_css_matches_committed_file(tmp_path: Path) -> None:
    """--write-css の出力はコミット済み styles.css と一致する。"""
    dest = _copy_site(tmp_path)
    assert check_site.main(["--write-css", str(dest)]) == 0
    assert (dest / "styles.css").read_text(encoding="utf-8") == (
        _TEMPLATE / "styles.css"
    ).read_text(encoding="utf-8")


# --- 公式カラーコードとの一致 -----------------------------------------------


def test_official_color_code_page_hexes_are_frozen() -> None:
    """カラーコードページ（2026-07-17）の代表値が tokens.json と一致する。"""
    tokens = check_site.load_tokens(_TOKENS)
    assert tokens["shared"]["text_black"] == "#000000"
    assert tokens["shared"]["text_white"] == "#FFFFFF"
    assert tokens["shared"]["label"] == "#626264"
    assert tokens["shared"]["link"] == "#0017C1"
    assert tokens["shared"]["bg_standard"] == "#F8F8FB"
    assert tokens["shared"]["bg_control"] == "#F1F1F4"
    assert tokens["palettes"]["solid-gray"]["highlight"] == "#4D4D4D"
    assert tokens["palettes"]["blue"]["highlight"] == "#0017C1"
    assert tokens["palettes"]["blue"]["chart"]["Blue 1200"] == "#000060"
    assert tokens["palettes"]["blue"]["chart"]["Blue 600"] == "#3460FB"
    assert tokens["palettes"]["light-blue"]["highlight"] == "#0055AD"
    assert tokens["palettes"]["cyan"]["highlight"] == "#006F83"
    assert tokens["palettes"]["green"]["highlight"] == "#115A36"
    assert tokens["palettes"]["orange"]["highlight"] == "#AC3E00"
    assert tokens["palettes"]["orange"]["semantic"]["error"] == "#850000"
    assert tokens["palettes"]["red"]["highlight"] == "#CE0000"
    assert tokens["palettes"]["red"]["semantic"]["error"] == "#850000"
    assert tokens["layout"]["page_width_16x9"] == 1280
    assert tokens["layout"]["page_width_4x3"] == 960
    assert tokens["layout"]["kpi_font_px"] == 36
    assert tokens["layout"]["radius_button_px"] == 8
    assert tokens["layout"]["radius_card_px"] == 12


def test_every_role_hex_is_on_the_official_color_code_page() -> None:
    """役割に割り当てた hex が全てそのパレットの公式集合に含まれる。"""
    tokens = check_site.load_tokens(_TOKENS)
    for palette_id in check_site.PALETTE_IDS:
        allowed = check_site.allowed_hexes(tokens, palette_id)
        for theme in check_site.THEMES:
            for name, value in check_site.theme_roles(tokens, palette_id, theme).items():
                assert check_site._norm_hex(value) in allowed, f"{palette_id}/{theme} {name}={value}"


def test_no_unverifiable_hex_survives_in_the_token_source() -> None:
    """公式ページに無い旧 extras（#949494 / #E6E6E6 / #FFD43D）が残っていない。"""
    raw = _TOKENS.read_text(encoding="utf-8").upper()
    for retired in ("#949494", "#E6E6E6", "#FFD43D"):
        assert retired not in raw


# --- コントラスト契約 -------------------------------------------------------


def test_contrast_contract_holds_for_all_fourteen_blocks() -> None:
    """7 パレット × ライト/ダークの全ペアが DADS の下限を満たす。"""
    tokens = check_site.load_tokens(_TOKENS)
    for palette_id in check_site.PALETTE_IDS:
        for theme in check_site.THEMES:
            roles = check_site.theme_roles(tokens, palette_id, theme)
            assert check_site.contrast_violations(roles, f"{theme}/{palette_id}") == []


def test_contrast_contract_covers_chart_series_and_semantic_text() -> None:
    """契約が系列色・増減・リンク・成功/エラーを含む（抜けると回帰が素通りする）。"""
    pairs = {(fg, bg) for fg, bg, _ in check_site.CONTRAST_CONTRACT}
    for name in check_site.CHART_VARS:
        assert (name, "--color-surface") in pairs
    for name in ("--color-positive", "--color-negative", "--color-link", "--color-success", "--color-error"):
        assert (name, "--color-surface") in pairs


def test_sunken_chart_series_is_reported() -> None:
    """カード背景に沈む系列色を赤くできる（旧テンプレートの実害）。"""
    roles = dict(_roles("blue", "dark"))
    roles["--chart-1"] = roles["--color-surface"]
    violations = "\n".join(check_site.contrast_violations(roles, "dark/blue"))
    assert "--chart-1" in violations
    assert "--color-surface" in violations


def test_low_contrast_delta_text_is_reported() -> None:
    """増減テキストが 4.5:1 を割ると赤くできる。"""
    roles = dict(_roles("blue", "dark"))
    roles["--color-positive"] = "#3460FB"
    violations = "\n".join(check_site.contrast_violations(roles, "dark/blue"))
    assert "--color-positive" in violations


def test_highlight_equal_to_surface_is_reported() -> None:
    """ハイライトがカードと同色なら赤くできる。"""
    roles = dict(_roles("solid-gray", "dark"))
    roles["--color-surface"] = roles["--color-highlight"]
    violations = "\n".join(check_site.contrast_violations(roles, "dark/solid-gray"))
    assert "ハイライト" in violations


def test_duplicate_series_is_reported() -> None:
    """系列色の重複を赤くできる。"""
    roles = dict(_roles("blue", "light"))
    roles["--chart-2"] = roles["--chart-1"]
    violations = "\n".join(check_site.contrast_violations(roles, "light/blue"))
    assert "系列色に重複" in violations


def test_contrast_ratio_matches_wcag_reference_values() -> None:
    """既知の基準値でコントラスト計算そのものを確かめる。"""
    assert check_site.contrast_ratio("#000000", "#FFFFFF") == pytest.approx(21.0, abs=0.01)
    assert check_site.contrast_ratio("#FFFFFF", "#FFFFFF") == pytest.approx(1.0, abs=0.001)
    assert check_site.contrast_ratio("#767676", "#FFFFFF") == pytest.approx(4.54, abs=0.01)


# --- HTML 契約 --------------------------------------------------------------


def test_missing_theme_toggle_fails(tmp_path: Path) -> None:
    """ホワイト/ダークトグルを外すと不合格になる。"""
    dest = _copy_site(tmp_path)
    violations = _edit_html(dest, ('data-theme-value="dark"', 'data-other="dark"'))
    assert "トグル" in violations


def test_html_skip_link_landmarks_and_noto(tmp_path: Path) -> None:
    """スキップリンク・ランドマーク・書体・ボタン type の欠落を検出する。"""
    dest = _copy_site(tmp_path)
    violations = _edit_html(
        dest,
        ('class="skip-link" href="#main"', 'class="home" href="/"'),
        ("<header", "<div"),
        ("</header>", "</div>"),
        ("<main", "<div"),
        ("</main>", "</div>"),
        ("<footer", "<div"),
        ("</footer>", "</div>"),
        ('type="button"', ""),
        ("Noto+Sans+JP", "Example"),
        ("fonts.googleapis.com", "example.invalid"),
        ('data-theme="light"', ""),
    )
    assert "スキップリンク" in violations
    assert "<header>" in violations
    assert "<main>" in violations
    assert "<footer>" in violations
    assert "type=button" in violations
    assert "Noto Sans JP" in violations
    assert "data-theme" in violations


def test_html_contract_failures(tmp_path: Path) -> None:
    """lang・KPI・SVG・原点の欠落を検出する。"""
    dest = _copy_site(tmp_path)
    violations = _edit_html(
        dest,
        ('lang="ja"', 'lang="en"'),
        ('class="kpi"', 'class="value"'),
        ("data-kpi", "data-x"),
        ('role="img"', 'role="presentation"'),
        ('data-origin="0"', ""),
    )
    assert "lang=ja" in violations
    assert "KPI" in violations
    assert "role=img" in violations
    assert "data-origin" in violations


def test_svg_removal_is_reported(tmp_path: Path) -> None:
    """ダッシュボードから SVG を全部消すと不合格になる。"""
    dest = _copy_site(tmp_path)
    violations = _edit_html(dest, ("<svg", "<div"), ("</svg>", "</div>"))
    assert "チャート SVG が無い" in violations


def test_invalid_page_kind_fails(tmp_path: Path) -> None:
    """data-page-kind の未知値は不合格になる。"""
    dest = _copy_site(tmp_path)
    assert "data-page-kind" in _edit_html(dest, ('data-page-kind="dashboard"', 'data-page-kind="app"'))


def test_invalid_canvas_fails(tmp_path: Path) -> None:
    """data-canvas の未知値は不合格になる。"""
    dest = _copy_site(tmp_path)
    assert "data-canvas" in _edit_html(dest, ('data-canvas="16x9"', 'data-canvas="21x9"'))


def test_four_by_three_canvas_is_accepted(tmp_path: Path) -> None:
    """4:3 キャンバスはそのまま合格する（eval 3 の要求）。"""
    dest = _copy_site(tmp_path)
    assert _edit_html(dest, ('data-canvas="16x9"', 'data-canvas="4x3"')) == ""


def test_palette_id_rejected(tmp_path: Path) -> None:
    """公式外のパレット識別子は不合格になる。"""
    dest = _copy_site(tmp_path)
    assert "data-palette" in _edit_html(dest, ('data-palette="blue"', 'data-palette="tailwind"'))


def test_theme_script_in_body_is_reported(tmp_path: Path) -> None:
    """theme.js を body 末尾へ移すと（白ちらつき）不合格になる。"""
    dest = _copy_site(tmp_path)
    violations = _edit_html(
        dest,
        ('  <script src="theme.js"></script>\n</head>', "</head>"),
        ("</body>", '  <script src="theme.js"></script>\n</body>'),
    )
    assert "<head> で読み込まれていない" in violations


def test_content_page_skips_dashboard_structure(tmp_path: Path) -> None:
    """data-page-kind=content なら偽 KPI 無しでも色とトグルだけで合格する。"""
    dest = _copy_site(tmp_path)
    violations = _edit_html(
        dest,
        ('data-page-kind="dashboard"', 'data-page-kind="content"'),
        ('class="kpi"', 'class="value"'),
        ("data-kpi", "data-x"),
        ('role="img"', 'role="presentation"'),
        ('data-origin="0"', ""),
    )
    assert violations == ""


# --- Dont's -----------------------------------------------------------------


def test_three_d_transform_is_reported(tmp_path: Path) -> None:
    """rotate3d だけでも 3D 表現として不合格になる。"""
    dest = _copy_site(tmp_path)
    assert "3D" in _edit_html(dest, ("<svg", '<svg style="transform: rotate3d(1,1,0,20deg)"'))


def test_preserve_3d_is_reported(tmp_path: Path) -> None:
    """transform-style: preserve-3d も検出する。"""
    dest = _copy_site(tmp_path)
    css = (dest / "styles.css").read_text(encoding="utf-8")
    (dest / "styles.css").write_text(css + "\n.deep { transform-style: preserve-3d; }\n", encoding="utf-8")
    assert any("3D" in item for item in check_site.validate_site(dest))


def test_decorative_shadow_is_reported(tmp_path: Path) -> None:
    """装飾のドロップシャドウは Dont's として不合格になる。"""
    dest = _copy_site(tmp_path)
    css = (dest / "styles.css").read_text(encoding="utf-8")
    (dest / "styles.css").write_text(
        css + "\n.card { box-shadow: 0 2px 6px #000000; }\n", encoding="utf-8"
    )
    assert any("ドロップシャドウ" in item for item in check_site.validate_site(dest))


def test_drop_shadow_filter_is_reported(tmp_path: Path) -> None:
    """filter: drop-shadow も検出する。"""
    dest = _copy_site(tmp_path)
    css = (dest / "styles.css").read_text(encoding="utf-8")
    (dest / "styles.css").write_text(
        css + "\n.chart { filter: drop-shadow(0 2px 2px #000000); }\n", encoding="utf-8"
    )
    assert any("ドロップシャドウ" in item for item in check_site.validate_site(dest))


def test_focus_ring_shadow_is_not_a_dont() -> None:
    """フォーカスリングの box-shadow は Dont's にしない。"""
    assert check_site._validate_donts(":focus-visible { box-shadow: 0 0 0 3px var(--color-focus); }") == []


# --- 色の宇宙 ---------------------------------------------------------------


def test_unofficial_hex_fails(tmp_path: Path) -> None:
    """公式に無い hex を足すと不合格になる。"""
    dest = _copy_site(tmp_path)
    css = (dest / "styles.css").read_text(encoding="utf-8")
    (dest / "styles.css").write_text(css + "\n.rogue { color: #0D1117; }\n", encoding="utf-8")
    assert any("#0D1117" in item for item in check_site.validate_site(dest))


def test_named_color_fails(tmp_path: Path) -> None:
    """名前付き色は公式 hex ではないので不合格になる。"""
    dest = _copy_site(tmp_path)
    assert "navy" in _edit_html(dest, ("<body>", '<body style="color: navy">'))


def test_uncommon_named_color_fails(tmp_path: Path) -> None:
    """許可キーワード以外は全て名前付き色として拒否する（旧実装は白リスト漏れした）。"""
    dest = _copy_site(tmp_path)
    assert "crimson" in _edit_html(dest, ("<body>", '<body style="background: crimson">'))


def test_hyphenated_color_property_is_not_a_blind_spot(tmp_path: Path) -> None:
    """background-color など連結プロパティ経由の名前付き色も拒否する。"""
    dest = _copy_site(tmp_path)
    assert "crimson" in _edit_html(dest, ("<body>", '<body style="background-color: crimson">'))


def test_color_scheme_keywords_are_not_treated_as_colors() -> None:
    """color-scheme: light|dark は名前付き色ではない（自前 CSS を誤検出しない）。"""
    for value in ("light", "dark"):
        assert check_site._validate_hex_universe(
            f":root {{ color-scheme: {value}; }}", check_site.load_tokens(_TOKENS)
        ) == []


def test_structural_keyword_is_not_a_color(tmp_path: Path) -> None:
    """border の solid など構造キーワードは違反にしない。"""
    dest = _copy_site(tmp_path)
    css = (dest / "styles.css").read_text(encoding="utf-8")
    (dest / "styles.css").write_text(
        css + "\n.edge { border: solid 1px #767676; background: none; }\n", encoding="utf-8"
    )
    assert check_site.validate_site(dest) == []


def test_invalid_hex_in_source_is_reported(tmp_path: Path) -> None:
    """正規化できない hex 断片も公式外として報告する。"""
    dest = _copy_site(tmp_path)
    css = (dest / "styles.css").read_text(encoding="utf-8")
    (dest / "styles.css").write_text(css + "\n/* #ABCDE */\n", encoding="utf-8")
    assert any("#ABCDE" in item for item in check_site.validate_site(dest))


def test_appended_css_is_allowed_when_hex_is_official(tmp_path: Path) -> None:
    """生成 CSS の末尾に公式色だけの追加ルールを足せる。"""
    dest = _copy_site(tmp_path)
    css = (dest / "styles.css").read_text(encoding="utf-8")
    (dest / "styles.css").write_text(css + "\n.article { margin: 0; color: #000000; }\n", encoding="utf-8")
    assert check_site.validate_site(dest) == []


# --- CSS 契約 ---------------------------------------------------------------


def test_css_missing_blocks_and_small_font(tmp_path: Path) -> None:
    """14px 未満と必須ブロック欠落を検出する。"""
    dest = _copy_site(tmp_path)
    (dest / "styles.css").write_text("body { font-size: 12px; color: #000000; }\n", encoding="utf-8")
    violations = "\n".join(check_site.validate_site(dest))
    assert "14px 未満" in violations
    assert "Noto Sans JP" in violations
    assert "1280px" in violations
    assert "960px" in violations
    assert "KPI 36px" in violations
    assert "ボタン半径" in violations
    assert "カード半径" in violations
    assert "color-scheme" in violations
    assert "ブロックが無い" in violations


def test_css_role_mismatch(tmp_path: Path) -> None:
    """公式ロール値をずらすと不合格になる。"""
    dest = _copy_site(tmp_path)
    css = (dest / "styles.css").read_text(encoding="utf-8")
    css = css.replace(
        ':root[data-theme="light"][data-palette="blue"] {\n  --color-text: #000000;',
        ':root[data-theme="light"][data-palette="blue"] {\n  --color-text: #333333;',
        1,
    )
    (dest / "styles.css").write_text(css, encoding="utf-8")
    assert "light/blue の --color-text" in "\n".join(check_site.validate_site(dest))


def test_focus_edge_variable_is_required(tmp_path: Path) -> None:
    """フォーカスリング外帯の変数を消すと不合格になる。"""
    assert "--color-focus-edge" in check_site.REQUIRED_VARS
    dest = _copy_site(tmp_path)
    css = (dest / "styles.css").read_text(encoding="utf-8")
    (dest / "styles.css").write_text(
        css.replace("  --color-focus-edge: #000000;\n", "", 1), encoding="utf-8"
    )
    assert "--color-focus-edge" in "\n".join(check_site.validate_site(dest))


def test_broken_tokens_surface_the_contrast_failure(tmp_path: Path) -> None:
    """tokens.json 側で系列色を沈めると、生成 CSS 経由でも赤くなる。"""
    dest = _copy_site(tmp_path)
    tokens = json.loads((dest / "tokens.json").read_text(encoding="utf-8"))
    tokens["palettes"]["blue"]["roles"]["dark"]["series"][0] = "#333333"
    (dest / "tokens.json").write_text(json.dumps(tokens, ensure_ascii=False), encoding="utf-8")
    check_site.main(["--write-css", str(dest)])
    violations = "\n".join(check_site.validate_site(dest))
    assert "dark/blue --chart-1" in violations


# --- JS 契約 ----------------------------------------------------------------


def test_js_contract_failures(tmp_path: Path) -> None:
    """theme.js から必須動作を削ると不合格になる。"""
    dest = _copy_site(tmp_path)
    (dest / "theme.js").write_text("console.log('x');\n", encoding="utf-8")
    violations = "\n".join(check_site.validate_site(dest))
    assert "data-theme" in violations
    assert "localStorage" in violations
    assert "light/dark" in violations
    assert "aria-pressed" in violations
    assert "DOM 構築前" in violations


def test_missing_styles_and_empty_js(tmp_path: Path) -> None:
    """styles.css 欠落と空の theme.js を検出する。"""
    dest = _copy_site(tmp_path, "no-css")
    (dest / "styles.css").unlink()
    assert check_site.validate_site(dest) == ["styles.css が無い"]
    dest = _copy_site(tmp_path, "empty-js")
    (dest / "theme.js").write_text("   \n", encoding="utf-8")
    assert any("theme.js が空" in item for item in check_site.validate_site(dest))
    dest = _copy_site(tmp_path, "no-js")
    (dest / "theme.js").unlink()
    assert any("theme.js が無い" in item for item in check_site.validate_site(dest))


def test_theme_js_guards_localstorage_failures() -> None:
    """localStorage が使えない環境でも例外で止まらないよう try/catch がある。"""
    js = (_TEMPLATE / "theme.js").read_text(encoding="utf-8")
    assert js.count("try {") >= 2
    assert "catch" in js


# --- CLI / ユーティリティ ---------------------------------------------------


def test_cli_fail_on_missing_html(tmp_path: Path) -> None:
    """index.html が無いサイトは CLI が 1 を返す。"""
    dest = _copy_site(tmp_path)
    (dest / "index.html").unlink()
    assert check_site.main([str(dest)]) == 1


def test_cli_errors_when_tokens_missing(tmp_path: Path) -> None:
    """tokens.json が解決できないと exit 2。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert check_site.main([str(empty)]) == 2


def test_norm_hex_expands_short_form() -> None:
    """3 桁・4 桁 hex を 6 桁へ正規化する。"""
    assert check_site._norm_hex("#abc") == "#AABBCC"
    assert check_site._norm_hex("#abcd") == "#AABBCC"
    assert check_site._norm_hex("#AABBCCDD") == "#AABBCC"
    with pytest.raises(ValueError):
        check_site._norm_hex("blue")
    with pytest.raises(ValueError):
        check_site._norm_hex("#GGHHII")


def test_default_tokens_path_from_template_dir() -> None:
    """テンプレートディレクトリからは references/tokens.json を解決する。"""
    assert check_site.default_tokens_path(_TEMPLATE) == _TOKENS


def test_default_tokens_path_prefers_site_copy(tmp_path: Path) -> None:
    """サイト直下の tokens.json を優先する。"""
    dest = _copy_site(tmp_path)
    assert check_site.default_tokens_path(dest) == dest / "tokens.json"


# --- E2E --------------------------------------------------------------------


def test_e2e_user_opens_static_site_and_can_choose_modes() -> None:
    """静的ホストが返す3ファイルをユーザー視点で辿り、両モードと公式色を確認する。"""
    html = (_TEMPLATE / "index.html").read_text(encoding="utf-8")
    css = (_TEMPLATE / "styles.css").read_text(encoding="utf-8")
    js = (_TEMPLATE / "theme.js").read_text(encoding="utf-8")
    assert "ホワイトモード" in html
    assert "ダークモード" in html
    assert 'data-theme-value="light"' in html
    assert 'data-theme-value="dark"' in html
    assert html.count('href="styles.css"') == 1
    assert html.count('src="theme.js"') == 1
    assert ':root[data-theme="dark"][data-palette="blue"]' in css
    assert "localStorage" in js
    completed = subprocess.run(
        [sys.executable, str(_CHECK_SITE), str(_TEMPLATE)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0
    assert "PASS" in completed.stdout


def test_e2e_browser_script_shares_the_python_thresholds() -> None:
    """ブラウザ側の受け入れ閾値が CONTRAST_CONTRACT と食い違わない。"""
    script = (_TEMPLATE / "e2e_contrast.js").read_text(encoding="utf-8")
    assert "const TEXT_MIN = 4.5;" in script
    assert "const MARK_MIN = 3.0;" in script
    minimums = {minimum for _, _, minimum in check_site.CONTRAST_CONTRACT}
    assert minimums == {4.5, 3.0}
    for palette_id in check_site.PALETTE_IDS:
        assert f'"{palette_id}"' in script


def test_e2e_all_seven_palettes_are_in_css() -> None:
    """7 パレットのライト/ダークブロックが配信 CSS に揃っている。"""
    css = (_TEMPLATE / "styles.css").read_text(encoding="utf-8")
    for palette_id in check_site.PALETTE_IDS:
        for theme in check_site.THEMES:
            assert f'[data-theme="{theme}"][data-palette="{palette_id}"]' in css


def test_e2e_rendered_chart_marks_stay_visible_after_switching_to_dark() -> None:
    """index.html が実際に参照する変数を解決し、両モードで図形が背景に沈まないことを見る。

    文字列一致では `--chart-5` がカード背景と同値でも通ってしまう。ここでは
    「その SVG が使っている変数」を列挙し、テーマごとに実際の色へ解決して比を測る。
    """
    html = (_TEMPLATE / "index.html").read_text(encoding="utf-8")
    tokens = check_site.load_tokens(_TOKENS)
    used = {
        name
        for name in check_site.CHART_VARS
        if f"var({name})" in html
    }
    assert used, "テンプレートが系列色を 1 つも使っていない"
    for palette_id in check_site.PALETTE_IDS:
        for theme in check_site.THEMES:
            roles = check_site.theme_roles(tokens, palette_id, theme)
            for name in used:
                ratio = check_site.contrast_ratio(roles[name], roles["--color-surface"])
                assert ratio >= 3.0, f"{theme}/{palette_id} {name} が {ratio:.2f}"
            gridline = check_site.contrast_ratio(roles["--color-gridline"], roles["--color-surface"])
            assert gridline > 1.0, f"{theme}/{palette_id} 目盛線がカードと同色"


def test_e2e_theme_toggle_flips_page_and_text_together() -> None:
    """トグルの両端が実際に反転した配色ブロックへ結び付いている。"""
    tokens = check_site.load_tokens(_TOKENS)
    for palette_id in check_site.PALETTE_IDS:
        light = check_site.theme_roles(tokens, palette_id, "light")
        dark = check_site.theme_roles(tokens, palette_id, "dark")
        assert light["--color-page"] != dark["--color-page"]
        assert light["--color-text"] != dark["--color-text"]
        assert light["--color-surface"] != dark["--color-surface"]
        assert check_site.relative_luminance(light["--color-page"]) > check_site.relative_luminance(
            dark["--color-page"]
        )
