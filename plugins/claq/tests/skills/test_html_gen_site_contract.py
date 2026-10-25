"""html-gen テンプレート本体の契約（HTML・CSS・JS・手書きの色・CLI）の検査。

壊し方の表は、テンプレートの複製を 1 か所だけ書き換えてサイト全体を検査し、違反に出る語
（通る書き方なら違反 0 件）を確かめる。1 行が 1 つの書き換えに当たる。
"""

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
    CSS,
    HTML,
    JS,
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


def test_loading_check_site_leaves_no_bytecode_in_the_shipped_assets() -> None:
    """検査スクリプトの読み込みが配布ディレクトリに .pyc を落とさない。"""
    shutil.rmtree(_TEMPLATE / "__pycache__", ignore_errors=True)
    _load_check_site()
    assert not (_TEMPLATE / "__pycache__").exists()


def test_write_css_matches_committed_tokens_css(tmp_path: Path) -> None:
    """--write-css の出力はコミット済み tokens.css と一致する。"""
    dest = _copy_site(tmp_path)
    assert check_site.main(["--write-css", str(dest)]) == 0
    assert (dest / "tokens.css").read_text(encoding="utf-8") == (_TEMPLATE / "tokens.css").read_text(encoding="utf-8")


def test_generated_tokens_css_is_the_only_place_with_raw_color() -> None:
    """手書きの 3 ファイルは生の色を 1 つも持たない（色の正本は tokens.json）。"""
    for filename, text in (("index.html", HTML), ("styles.css", CSS), ("app.js", JS)):
        assert check_site.HEX_RE.findall(text) == [], filename
        assert [m.group(1) for m in check_site.COLOR_FUNCTION_RE.finditer(text)] == [], filename
    assert check_site.COLOR_FUNCTION_RE.search((_TEMPLATE / "tokens.css").read_text(encoding="utf-8"))


# --- 壊すと落ちる契約（置換による書き換え） ------------------------------------

_SMIL = '<animate attributeName="opacity" values="1;0.4;1" dur="2s" repeatCount="indefinite"/>'
_DOT = '<circle class="series-dot" style="--i: 1'
_STYLESHEETS = '<link rel="stylesheet" href="tokens.css">\n  <link rel="stylesheet" href="styles.css">'
_AURORA = '<th scope="row">Aurora 工業</th>'
_CSP = (
    '<meta http-equiv="Content-Security-Policy" content="default-src \'self\'; script-src \'self\'; '
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'none'\">"
)


@pytest.mark.parametrize(
    ("filename", "replacements", "needle"),
    [
        # HTML: テーマトグル・ランドマーク・属性の値域・読み込み順
        pytest.param("index.html", [('data-theme-value="dark"', "data-x")], "トグル", id="theme-toggle"),
        pytest.param("index.html", [('type="button" data-theme-value', "data-theme-value")], "type=button", id="toggle-type"),
        pytest.param("index.html", [('aria-pressed="true"', "data-pressed")], "aria-pressed", id="toggle-aria-pressed"),
        pytest.param("index.html", [('<html lang="ja"', '<html lang=""')], "html[lang]", id="html-lang"),
        pytest.param("index.html", [('name="viewport"', 'name="vp"')], "viewport", id="viewport"),
        pytest.param(
            "index.html", [('class="skip-link" href="#main"', 'class="x" href="#main2"')], "スキップリンク", id="skip-link"
        ),
        *[
            pytest.param("index.html", [(f"<{tag} ", "<div "), (f"</{tag}>", "</div>")], f"<{tag}> が無い", id=tag)
            for tag in ("header", "main", "footer")
        ],
        pytest.param("index.html", [('data-base="neutral"', 'data-base="mauve"')], "data-base", id="unknown-base"),
        pytest.param("index.html", [('data-theme="light"', 'data-theme="auto"')], "data-theme", id="theme-value"),
        pytest.param(
            "index.html", [('data-page-kind="dashboard"', 'data-page-kind="blog"')], "data-page-kind", id="page-kind"
        ),
        pytest.param(
            "index.html",
            [(_STYLESHEETS, '<link rel="stylesheet" href="styles.css">\n  <link rel="stylesheet" href="tokens.css">')],
            "順で読み込まれていない",
            id="stylesheet-order",
        ),
        # HTML: ダッシュボードのデータ（指標・原点 0・表・desc・SVG・role=img）
        pytest.param(
            "index.html", [('data-metric="true"', "data-m"), ("card metric reveal", "card reveal")], "主要指標", id="metric"
        ),
        pytest.param("index.html", [('data-origin="0"', 'data-origin="1"')], "原点 0", id="origin-zero"),
        pytest.param("index.html", [("<table", '<div class="t"'), ("</table>", "</div>")], "表が無い", id="table"),
        pytest.param("index.html", [("<desc", "<p data-desc"), ("</desc>", "</p>")], "<desc>", id="desc"),
        pytest.param("index.html", [("<svg", "<div"), ("</svg>", "</div>")], "チャート SVG", id="svg"),
        pytest.param("index.html", [('role="img"', 'role="presentation"')], "svg[role=img]", id="svg-role-img"),
        # app.js を body 末尾へ移すと、初回描画で白がちらつく
        pytest.param(
            "index.html",
            [('<script src="app.js" defer></script>', ""), ("</body>", '<script src="app.js"></script></body>')],
            "白のちらつき",
            id="script-in-body",
        ),
        # HTML に直接書いた止まらない動き（SVG の <animate>、style 属性）
        pytest.param(
            "index.html", [('<desc id="mrr-d">', f'{_SMIL}<desc id="mrr-d">')], "止まらない動き", id="html-smil-loop"
        ),
        pytest.param(
            "index.html", [(_DOT, f"{_DOT}; animation: pulse 2s infinite")], "止まらない動き", id="html-style-loop"
        ),
        # HTML: データ由来の文字列に紛れたスクリプト（表のセルへ書いた未エスケープの値）
        *[
            pytest.param("index.html", [(_AURORA, f'<th scope="row">Aurora{payload}</th>')], needle, id=case)
            for case, payload, needle in (
                ("xss-th-repro", "<img src=x onerror=alert(document.domain)>", "イベント属性 onerror"),
                ("xss-svg-slash", "<svg/onload=alert(1)>", "イベント属性 onload"),
                ("xss-uppercase", "<IMG SRC=x ONERROR=alert(1)>", "イベント属性 onerror"),
                ("xss-quoted-gt", "<img alt='>' src=x onerror=alert(1)>", "イベント属性 onerror"),
                ("xss-unquoted-quote", '<img title=a"b onerror=alert(1)>', "イベント属性 onerror"),
                ("xss-no-separator", '<img src="x"onerror=alert(1)>', "イベント属性 onerror"),
                ("xss-comment-quote", '<!--<a title="--><img src=x onerror=alert(1)>"', "イベント属性 onerror"),
                ("xss-inline-script", "<script>alert(document.domain)</script>", "app.js 以外の <script>"),
                (
                    "xss-external-app-js",
                    '<script src="https://evil.example/app.js"></script>',
                    "app.js 以外の <script>",
                ),
                ("xss-duplicate-src", '<script src="evil.js" src="app.js"></script>', "app.js 以外の <script>"),
                ("xss-javascript-url", '<a href="&#106;ava&#x09;script:alert(1)">x</a>', "javascript: の URL"),
                ("xss-meta-refresh", '<meta http-equiv="Refresh" content="0;url=https://evil.example">', "meta refresh"),
            )
        ],
        # SVG の <title>/<desc>/<style> の中（HTMLParser が中身のタグを報告しない場所）
        pytest.param(
            "index.html",
            [("<title>1月&#10;", "<title><img src=x onerror=alert(1)>1月&#10;")],
            "イベント属性 onerror",
            id="xss-hover-title",
        ),
        pytest.param(
            "index.html",
            [('<desc id="mrr-d">', '<desc id="mrr-d"><img src=x onerror=alert(1)>')],
            "イベント属性 onerror",
            id="xss-desc",
        ),
        pytest.param(
            "index.html",
            [('<desc id="mrr-d">', '<style><img src=x onerror=alert(1)></style><desc id="mrr-d">')],
            "イベント属性 onerror",
            id="xss-svg-style",
        ),
        # CSP の meta は <head> に置く（<body> 内ではブラウザが無視する）
        pytest.param("index.html", [(_CSP, "")], "Content-Security-Policy", id="csp-missing"),
        pytest.param(
            "index.html",
            [(_CSP, ""), ("<body>\n", f"<body>\n  {_CSP}\n")],
            "Content-Security-Policy",
            id="csp-in-body",
        ),
        # CSP の中身を弱めない（スクリプトは 'self' だけ、object と base は 'none'、外部オリジンを許さない）
        pytest.param("index.html", [("script-src 'self';", "script-src 'self' 'unsafe-inline';")], "script-src", id="csp-inline-script"),
        pytest.param("index.html", [("script-src 'self';", "script-src *;")], "script-src", id="csp-wildcard-script"),
        pytest.param("index.html", [(" object-src 'none';", "")], "object-src", id="csp-no-object-src"),
        pytest.param("index.html", [(" base-uri 'none';", "")], "base-uri", id="csp-no-base-uri"),
        pytest.param(
            "index.html",
            [("style-src 'self' 'unsafe-inline';", "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com;")],
            "外部のオリジン",
            id="csp-external-origin",
        ),
        # CSS: 骨格の変数・Tailwind のブレークポイント・流体の部品・フォーカス・モーション低減
        pytest.param("styles.css", [("var(--ring)", "currentColor")], "--ring", id="role-variable"),
        pytest.param("styles.css", [("min-width: 1024px", "min-width: 1023px")], "1024px", id="breakpoint"),
        pytest.param("styles.css", [("minmax(", "calc(")], "minmax", id="minmax"),
        pytest.param("styles.css", [("clamp(", "max(")], "clamp", id="clamp"),
        pytest.param("styles.css", [(":focus-visible", ":focus-within")], "focus-visible", id="focus-visible"),
        pytest.param(
            "styles.css",
            [("@media (prefers-reduced-motion: reduce)", "@media (min-width: 1px)")],
            "prefers-reduced-motion",
            id="reduced-motion-block",
        ),
        # モーション低減ブロックは animation と transition の両方を止める
        pytest.param(
            "styles.css",
            [("    transition: none !important;\n", "")],
            "transition を止めていない",
            id="reduced-motion-transition",
        ),
        pytest.param(
            "styles.css",
            [("    animation: none !important;\n", "")],
            "animation を止めていない",
            id="reduced-motion-animation",
        ),
        # JS: テーマ・ベース・永続・低減設定の扱い
        pytest.param("app.js", [("localStorage", "sessionStorage")], "localStorage", id="js-localstorage"),
        pytest.param("app.js", [("prefers-reduced-motion", "prefers-color")], "prefers-reduced-motion", id="js-reduced"),
        pytest.param("app.js", [("aria-pressed", "data-pressed")], "aria-pressed", id="js-aria-pressed"),
        pytest.param(
            "app.js", [("dataset.base", "dataset.palette"), ("data-base", "data-x")], "data-base", id="js-data-base"
        ),
    ],
)
def test_broken_contract_is_reported(
    tmp_path: Path, filename: str, replacements: list[tuple[str, str]], needle: str
) -> None:
    """テンプレートの契約を 1 つ壊すと、その違反が報告される。"""
    assert needle in _edit(_copy_site(tmp_path), filename, *replacements)


@pytest.mark.parametrize(
    "replacements",
    [
        pytest.param(
            [(_AURORA, '<th scope="row">Aurora&lt;img src=x onerror=alert(document.domain)&gt;</th>')],
            id="escaped-th",
        ),
        pytest.param(
            [("<title>1月&#10;", "<title>&lt;img src=x onerror=alert(1)&gt;1月&#10;")], id="escaped-hover-title"
        ),
        # 属性値の中の語はイベント属性でも URL でもない
        pytest.param([('title="Neutral"', 'title="onerror=alert(1) &quot;javascript:x"')], id="data-in-attribute-value"),
        # javascript: を見るのは URL を取る属性だけ
        pytest.param([('title="Neutral"', 'title="JavaScript: 入門"')], id="javascript-word-in-title"),
    ],
)
def test_escaped_data_passes(tmp_path: Path, replacements: list[tuple[str, str]]) -> None:
    """HTML エスケープしたデータ由来の文字列は、スクリプト混入として報告されない。"""
    assert _edit(_copy_site(tmp_path), "index.html", *replacements) == ""


# --- 壊すと落ちる契約（styles.css への追記） -----------------------------------

_STRING_OR_URL_PREFIXES = (
    '.a::before { content: "/*"; }',
    ".a { background: url(/*x.png); }",
    ".a { background: url(img/*.png); }",
    '.a { background: url("data:image/svg+xml;utf8,<svg>/*</svg>"); }',
    'a[href*="/*"] { color: red; }',
    ".a::before { content: '/*'; }",
    ".a { background: URL(/*x.png); }",
)


@pytest.mark.parametrize(
    ("snippet", "needle"),
    [
        # 静止状態を不可視にする書き方
        pytest.param("\n.x { animation: rise 1s forwards; }\n", "forwards", id="forwards"),
        pytest.param("\n.x { animation-fill-mode: forwards; }\n", "forwards", id="fill-mode-forwards"),
        pytest.param("\n.reveal { opacity: 0; }\n", "opacity: 0", id="opacity-zero"),
        # [data-animate] の外の both / backwards は、遅延中と停止中に from の状態が貼り付き、背面タブで見えないまま固まる
        pytest.param("\n.x { animation: rise 300ms both; }\n", "both/backwards", id="unscoped-both"),
        pytest.param("\n.x { animation-fill-mode: backwards; }\n", "both/backwards", id="unscoped-backwards"),
        # 止まらない動き（WCAG 2.2 SC 2.2.2。静的なページでは「リアルタイム更新中」の誤った合図にもなる）
        pytest.param("\n.live { animation: pulse 2s ease-out infinite; }\n", "infinite", id="infinite"),
        pytest.param("\n.live { animation-iteration-count: infinite; }\n", "infinite", id="iteration-count"),
        # プロパティ名とキーワードは大小文字を区別せず、コロンの前に空白も置ける
        pytest.param("\n.live { ANIMATION: pulse 2S INFINITE; }\n", "infinite", id="uppercase"),
        pytest.param("\n.live { animation : pulse 2s infinite; }\n", "infinite", id="space-before-colon"),
        # Tailwind v4 の既定テーマ（--animate-ping）は値に infinite を含み、animation の宣言には現れない
        pytest.param(
            "\n:root { --animate-ping: ping 1s cubic-bezier(0, 0, 0.2, 1) infinite; }\n"
            ".live { animation: var(--animate-ping); }\n",
            "infinite",
            id="custom-property",
        ),
        # 回帰: 語としての照合に変えても典型形は落ちる
        pytest.param("\n.live { animation: spin 1s linear infinite; }\n", "infinite", id="spin"),
        pytest.param("\n:root { --animate-spin: spin 1s linear infinite; }\n", "infinite", id="animate-spin"),
        pytest.param("\n.live { animation-iteration-count: INFINITE; }\n", "infinite", id="iteration-uppercase"),
        # 偽陰性: 文字列リテラルや url() の中の /* はコメントではない（以降を消すと後ろの動きを見逃す）
        *[
            pytest.param(f"\n{prefix} .b {{ animation: x 1s infinite; }}\n", "infinite", id=f"after-literal-{index}")
            for index, prefix in enumerate(_STRING_OR_URL_PREFIXES, 1)
        ],
        # 幅・文字の大きさ
        pytest.param("\n.x { width: 1280px; }\n", "固定幅", id="fixed-width"),
        pytest.param("\n.x { max-width: 960px; }\n", "広い画面を捨てている", id="narrow-max-width"),
        pytest.param("\n.x { font-size: 10px; }\n", "12px 未満", id="tiny-font"),
        # 手書きの色（hex・色関数・名前付き色。ハイフン付きの色プロパティも見る）
        pytest.param("\n.x { color: #ff0000; }\n", "生の hex", id="hex"),
        pytest.param("\n.x { color: oklch(0.5 0.1 200); }\n", "oklch()", id="oklch"),
        pytest.param("\n.x { color: rgb(0 0 0); }\n", "rgb()", id="rgb"),
        pytest.param("\n.x { color: hsl(200 50% 50%); }\n", "hsl()", id="hsl"),
        pytest.param("\n.x { background: teal; }\n", "名前付き色", id="named-color"),
        pytest.param("\n.x { border-top-color: navy; }\n", "名前付き色", id="hyphenated-color-property"),
    ],
)
def test_appended_css_violation_is_reported(tmp_path: Path, snippet: str, needle: str) -> None:
    """styles.css へ契約に反する書き方を足すと、その違反が報告される。"""
    assert needle in _append(_copy_site(tmp_path), "styles.css", snippet)


@pytest.mark.parametrize(
    "snippet",
    [
        # [data-animate] の下の both は、JS が画面内でだけ起動するので通す
        pytest.param('\n[data-animate="in"] .x { animation: rise 300ms both; }\n', id="scoped-both"),
        pytest.param("\n@keyframes slide {\n  from {\n    opacity: 0;\n  }\n}\n", id="opacity-zero-in-keyframes"),
        pytest.param("\n.live { animation-iteration-count: 2; }\n", id="finite-iteration"),
        pytest.param("\n:root { --pulse-count: 1; }\n", id="finite-custom-property"),
        # 偽陽性: キーフレーム名が infinite を含むだけの有限の動き。コメント内は動きにならない（閉じていなければ末尾まで）
        pytest.param("\n.live { animation: infinite-scroll 1s 2; }\n", id="name-starts-with-infinite"),
        pytest.param("\n/* animation: x infinite */ .a { color: inherit; }\n", id="in-comment"),
        pytest.param(
            "\n.a { color: inherit; } /* --animate-spin: spin 1s infinite;\n   animation: x infinite */\n",
            id="in-multiline-comment",
        ),
        pytest.param("\n.live { animation: pulse 1s 2; } /* animation: x infinite;\n", id="in-unclosed-comment"),
        pytest.param("\n.live { animation: scroll-infinite 1s 2; }\n", id="name-ends-with-infinite"),
        pytest.param("\n.x { width: 320px; }\n", id="small-fixed-width"),
        pytest.param("\n@media (max-width: 500px) { .x { gap: 4px; } }\n", id="media-query-max-width"),
        pytest.param("\n.x { font-size: 12px; }\n", id="font-12px"),
        # トークン経由の合成は正しい書き方（文字色を透明と混ぜるのは別の契約で拒否する）
        pytest.param(
            "\n.x { background: color-mix(in oklab, var(--primary) 40%, transparent);"
            " color: color-mix(in oklab, var(--primary) 60%, var(--foreground)); }\n",
            id="color-mix-and-var",
        ),
    ],
)
def test_appended_css_that_follows_the_contract_passes(tmp_path: Path, snippet: str) -> None:
    """styles.css へ契約に沿った書き方を足しても、サイト全体が合格のままである。"""
    assert _append(_copy_site(tmp_path), "styles.css", snippet) == ""


def test_content_page_skips_dashboard_structure(tmp_path: Path) -> None:
    """content ページには指標や図を要求しない。"""
    dest = _copy_site(tmp_path)
    _edit(dest, "index.html", ('data-page-kind="dashboard"', 'data-page-kind="content"'))
    violations = _edit(
        dest, "index.html", ("<table", '<div class="t"'), ("</table>", "</div>"), ('data-origin="0"', "")
    )
    assert violations == ""


def test_looping_motion_written_in_the_html_is_reported() -> None:
    """HTML に直接書いた止まらない動きも落ちる。

    styles.css だけを見ていると、SVG の <animate repeatCount="indefinite"> や
    style 属性・<style> 要素に書いた infinite で脈動が素通りする。回数が有限なら通す。
    """
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


# --- CSS・色・JS の定数と静的な形 -------------------------------------------------


def test_required_style_vars_are_shadcn_roles() -> None:
    """必須変数の一覧が shadcn/ui の役割名で構成されている。"""
    assert {"--background", "--radius", "--font-sans"} <= set(check_site.REQUIRED_STYLE_VARS)
    assert not any(name.startswith("--md-sys") for name in check_site.REQUIRED_STYLE_VARS)


def test_required_breakpoints_follow_tailwind() -> None:
    """必須ブレークポイントが Tailwind の md / lg / xl である。"""
    assert check_site.REQUIRED_BREAKPOINTS == (768, 1024, 1280)


def test_svg_geometry_keywords_are_not_treated_as_colors() -> None:
    """stroke-linecap のような幾何プロパティを色と誤認しない。"""
    text = "path { stroke-linecap: butt; background-blend-mode: multiply; }"
    assert check_site._validate_authored_colors(text) == []


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
    assert JS.count("try {") >= 2
    assert "catch" in JS


def test_reveal_animation_waits_for_a_visible_tab() -> None:
    """背面タブでは入場アニメーションを起動しない（from の状態で固まるため）。"""
    assert "visibilityState" in JS
    assert "visibilitychange" in JS


# --- CLI ---------------------------------------------------------------------


def test_cli_exit_codes(tmp_path: Path) -> None:
    """CLI はテンプレートで 0、違反があれば 1、tokens.json が見つからなければ 2 を返す。"""
    broken = _copy_site(tmp_path)
    _edit(broken, "styles.css", ("var(--ring)", "currentColor"))
    empty = tmp_path / "empty"
    empty.mkdir()
    assert [check_site.main([str(site)]) for site in (_TEMPLATE, broken, empty)] == [0, 1, 2]


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
        [sys.executable, str(_CHECK_SITE), str(_TEMPLATE)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    assert "PASS" in result.stdout
