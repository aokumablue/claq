"""html-gen テンプレート本体の契約（HTML・CSS・JS・手書きの色・CLI）の検査。"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ._support import (
    _CHECK_SITE,
    _TEMPLATE,
    _TOKENS,
    _append,
    _copy_site,
    _edit,
    _load_check_site,
    check_site,
)

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
    見えないまま固まる。
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


def test_looping_animation_is_reported(tmp_path: Path) -> None:
    """止まらない動き（infinite）は落ちる。

    自動で始まり 5 秒を超えて続く動きは、止める手段が無いと WCAG 2.2 SC 2.2.2 に反する。
    脈動する点は静的なページで「リアルタイム更新中」の誤った合図にもなる。
    """
    dest = _copy_site(tmp_path)
    assert "infinite" in _append(dest, "styles.css", "\n.live { animation: pulse 2s ease-out infinite; }\n")

    dest = _copy_site(tmp_path, "s2")
    assert "infinite" in _append(dest, "styles.css", "\n.live { animation-iteration-count: infinite; }\n")

    dest = _copy_site(tmp_path, "s3")
    assert _append(dest, "styles.css", "\n.live { animation-iteration-count: 2; }\n") == ""

    # CSS のプロパティ名とキーワードは大小文字を区別せず、コロンの前に空白も置ける。
    # どちらもブラウザでは止まらない動きになる。
    for name, rule in (("s4", "ANIMATION: pulse 2S INFINITE"), ("s5", "animation : pulse 2s infinite")):
        dest = _copy_site(tmp_path, name)
        assert "infinite" in _append(dest, "styles.css", f"\n.live {{ {rule}; }}\n"), rule

    # Tailwind v4 の既定テーマ（--animate-ping など）は値に infinite を含む。写して
    # animation: var(--animate-ping) で使うと、animation の宣言には infinite が現れない。
    dest = _copy_site(tmp_path, "s6")
    ping = ":root { --animate-ping: ping 1s cubic-bezier(0, 0, 0.2, 1) infinite; }"
    assert "infinite" in _append(dest, "styles.css", f"\n{ping}\n.live {{ animation: var(--animate-ping); }}\n")

    dest = _copy_site(tmp_path, "s7")
    assert _append(dest, "styles.css", "\n:root { --pulse-count: 1; }\n") == ""

    # 回帰: 止まらない動きの典型形は、語としての照合に変えても落ち続ける。
    for name, rule in (
        ("s8", ".live { animation: spin 1s linear infinite; }"),
        ("s9", ":root { --animate-spin: spin 1s linear infinite; }"),
        ("s10", ".live { animation-iteration-count: INFINITE; }"),
    ):
        dest = _copy_site(tmp_path, name)
        assert "infinite" in _append(dest, "styles.css", f"\n{rule}\n"), rule

    # 偽陰性: 文字列リテラルや url() の中の ``/*`` はコメントではない。コメントと誤認して以降を
    # 消すと、後ろに書いた止まらない動きを見逃す。
    for name, prefix in (
        ("e1", '.a::before { content: "/*"; }'),
        ("e2", ".a { background: url(/*x.png); }"),
        ("e3", ".a { background: url(img/*.png); }"),
        ("e4", '.a { background: url("data:image/svg+xml;utf8,<svg>/*</svg>"); }'),
        ("e5", 'a[href*="/*"] { color: red; }'),
        ("e6", ".a::before { content: '/*'; }"),
        ("e7", ".a { background: URL(/*x.png); }"),
    ):
        dest = _copy_site(tmp_path, name)
        assert "infinite" in _append(dest, "styles.css", f"\n{prefix} .b {{ animation: x 1s infinite; }}\n"), prefix

    # 偽陽性: キーフレーム名が infinite- で始まるだけで反復は 2 回。
    # コメントに書いた記述はブラウザが読まないので動きにならない（閉じていないコメントは末尾まで）。
    for name, rule in (
        ("s11", ".live { animation: infinite-scroll 1s 2; }"),
        ("s12", "/* animation: x infinite */ .a { color: inherit; }"),
        ("s13", ".a { color: inherit; } /* --animate-spin: spin 1s infinite;\n   animation: x infinite */"),
        ("s14", ".live { animation: pulse 1s 2; } /* animation: x infinite;"),
        ("s15", ".live { animation: scroll-infinite 1s 2; }"),
    ):
        dest = _copy_site(tmp_path, name)
        assert _append(dest, "styles.css", f"\n{rule}\n") == "", rule


def test_looping_motion_written_in_the_html_is_reported(tmp_path: Path) -> None:
    """HTML に直接書いた止まらない動きも落ちる。

    styles.css だけを見ていると、SVG の <animate repeatCount="indefinite"> や
    style 属性・<style> 要素に書いた infinite で脈動が素通りする。回数が有限なら通す。
    """
    dest = _copy_site(tmp_path)
    smil = '<animate attributeName="opacity" values="1;0.4;1" dur="2s" repeatCount="indefinite"/>'
    assert "止まらない動き" in _edit(dest, "index.html", ('<desc id="mrr-d">', f'{smil}<desc id="mrr-d">'))

    dest = _copy_site(tmp_path, "s2")
    dot = '<circle class="series-dot" style="--i: 1'
    assert "止まらない動き" in _edit(dest, "index.html", (dot, f"{dot}; animation: pulse 2s infinite"))

    for markup in (
        "<style>.live { animation-iteration-count: infinite; }</style>",
        "<animate repeatDur='indefinite'/>",
        '<i style="ANIMATION: pulse 2S INFINITE"></i>',
        '<i style="--animate-ping: ping 1s infinite"></i>',
        "<style>:root { --animate-pulse: pulse 2s infinite; }</style>",
        # HTML パーサは属性名を大小文字を問わず repeatCount / repeatDur へ直し、
        # 引用符の無い値も読むので、どちらも止まらない動きになる
        "<animate repeatcount=indefinite />",
        '<animate REPEATDUR="indefinite"/>',
        # 値の前後の空白を除いて読むブラウザでは止まらない（Chrome は repeatDur、Firefox は repeatCount）
        '<animate repeatDur=" indefinite "/>',
        "<animate repeatCount=' indefinite'/>",
        # 回帰: 典型形と、カスタムプロパティ経由の止まらない動き
        '<i style="animation: spin 1s linear infinite"></i>',
        '<i style="--animate-spin: spin 1s linear infinite"></i>',
        "<style>:root { --animate-spin: spin 1s linear INFINITE; }</style>",
        # コメントの外にある宣言は、同じ要素のコメントに紛れても落とす
        "<style>/* note */ .live { animation: pulse 2s infinite; }</style>",
        # 文字列リテラルや url()（大文字の URL() を含む）の中の /* はコメントの開始ではない
        '<style>.a::before { content: "/*"; } .b { animation: x 1s infinite; }</style>',
        "<style>.a { background: url(img/*.png); } .b { animation: x 1s infinite; }</style>",
        "<style>.a { background: URL(/*x.png); } .b { animation: x 1s infinite; }</style>",
        '<i style="content: \'/*\'; animation: x 1s infinite"></i>',
    ):
        assert check_site._validate_inline_motion(markup) != [], markup
    for markup in (
        '<animate repeatCount="3"/>',
        '<i style="animation-iteration-count: 2"></i>',
        # 属性の境界をまたいで別の属性の語を拾わない
        '<i style="animation: rise 1s" data-note="infinite"></i>',
        '<i style="--gap: 4px; --n: 2"></i>',
        # 値は大小文字を区別する。INDEFINITE はブラウザが無効な値として捨て、1 回で止まる
        '<animate REPEATDUR="INDEFINITE"/>',
        # キーフレーム名が infinite- で始まるだけの有限の動き
        '<i style="animation: infinite-scroll 1s 2"></i>',
        "<style>.live { animation: infinite-scroll 1s 2; }</style>",
        # コメント内の記述は動きにならない。閉じていないコメントは末尾まで
        "<style>/* animation: x infinite */ .a { color: inherit; }</style>",
        '<i style="/* animation: x infinite */ color: inherit"></i>',
        "<style>.a { color: inherit; } /* --animate-spin: spin 1s infinite;</style>",
    ):
        assert check_site._validate_inline_motion(markup) == [], markup


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
