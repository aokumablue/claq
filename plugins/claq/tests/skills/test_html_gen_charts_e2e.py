"""html-gen の静的サイトの E2E 検査と、チャート・ドキュメントの契約の検査。"""

from __future__ import annotations

import itertools
import json
import re
from html import unescape
from pathlib import Path

import pytest

from ._support import _SKILL_DIR, BASES, CSS, E2E_JS, HTML, JS, TOKENS, _copy_site, _edit, check_site

# --- E2E ---------------------------------------------------------------------


def test_e2e_user_opens_static_site_and_can_choose_modes() -> None:
    """静的サイトを開いた利用者がテーマとベースカラーを選べて、選択が残る。"""
    for theme in check_site.THEMES:
        assert f'data-theme-value="{theme}"' in HTML
    for base_id in BASES:
        assert f'data-base-value="{base_id}"' in HTML
    assert "localStorage" in JS
    assert "prefers-color-scheme: dark" in JS
    assert '<script src="app.js" defer></script>' in HTML


def test_e2e_browser_script_shares_the_python_thresholds() -> None:
    """ブラウザ実測スクリプトが Python 側と同じ閾値・同じペアを見る（片側にだけ役割が増えても包含の検査では気づけないので、ペア集合は両実装から導出して双方向で突き合わせる）。"""
    assert "const TEXT_MIN = 4.5;" in E2E_JS
    assert "const MARK_MIN = 3.0;" in E2E_JS
    for base_id in BASES:
        assert f'"{base_id}"' in E2E_JS
    minimums = {"TEXT_MIN": 4.5, "MARK_MIN": 3.0}
    literal = {
        (foreground, background, minimums[name])
        for foreground, background, name in re.findall(r'\["(--[\w-]+)", "(--[\w-]+)", (TEXT_MIN|MARK_MIN)\]', E2E_JS)
    }
    mapped = {
        (chart, background, minimums[name])
        for background, name in re.findall(r'CHARTS\.map\(\(name\) => \[name, "(--[\w-]+)", (TEXT_MIN|MARK_MIN)\]\)', E2E_JS)
        for chart in check_site.CHART_VARS
    }
    assert literal | mapped == set(check_site.CONTRAST_CONTRACT)


@pytest.mark.parametrize(
    "needles",
    [
        # getComputedStyle は oklch() を oklch() のまま返すので、文字列から数値を拾うと L C H を R G B と取り違えて全ペアが「ほぼ黒」になる
        pytest.param(("getImageData", 'createElement("canvas")'), id="real-pixels"),
        # color-mix や不透明度で作った文字色はどちらの変数にも現れない（サイドバーのラベルが 2.2:1 のまま全チェック緑になった実例がある）
        pytest.param(("measureRenderedText", "backdropOf", "textFailures"), id="rendered-text"),
        pytest.param(("failures.length === 0 && textFailures.length === 0 && behaviorFailures.length === 0",), id="verdict"),
        # テーマ切替の色は 150ms かけて遷移するので、止めずに読むと切り替え前の色で比を測り、無関係な要素が大量に赤くなる
        pytest.param(("transition: none !important", "freeze.remove()"), id="frozen-transitions"),
    ],
)
def test_e2e_browser_script_measures_what_is_rendered(needles: tuple[str, ...]) -> None:
    """ブラウザ実測は canvas のピクセルで、変数ペアだけでなく実際に描かれた文字も、色遷移を止めて測り、両方の結果で判定する。"""
    for needle in needles:
        assert needle in E2E_JS, needle


def test_e2e_rendered_chart_marks_stay_visible_in_both_themes() -> None:
    """テンプレートが実際に使う系列色が、置かれる面の上で見える。"""
    used = {name for name in check_site.CHART_VARS if f"var({name})" in HTML}
    assert used, "テンプレートが系列色を使っていない"
    for base_id in BASES:
        for theme in check_site.THEMES:
            colors = check_site.resolved_hex(TOKENS, base_id, theme)
            for name in used:
                for surface in ("--card", "--background"):
                    ratio = check_site.contrast_ratio(colors[name], colors[surface])
                    assert ratio >= 3.0, f"{theme}/{base_id} {name} on {surface}: {ratio:.2f}"


def test_e2e_animation_never_hides_content_permanently() -> None:
    """アニメーションが走らなくても中身が読める（入場は JS が付ける data-animate の下だけで起き、静止状態は完成形）。"""
    assert check_site._validate_resting_state(CSS) == []
    assert '[data-animate="in"] .series-line {' in CSS
    # 棒とビンが data-animate の下で grow-y の動きを持つことを、セレクタの並びや改行に依らず見る。
    rules = re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", CSS, flags=re.DOTALL))
    for mark in (".bar", ".bin"):
        assert any(
            f'[data-animate="in"] {mark}' in {selector.strip() for selector in selectors.split(",")}
            and re.search(r"\banimation:\s*grow-y(?![\w-])", body)
            for selectors, body in rules
        ), mark
    assert '[data-animate="in"] .arc {' in CSS
    assert ".series-line {\n  fill: none;" in CSS


def test_e2e_arcs_keep_their_start_and_true_length() -> None:
    """円弧の開始位置を CSS で宣言せず（presentation attribute より優先され、全セグメントが 12 時から重なる）、端を丸めない（弧が線幅の半分ずつ伸び、11% の分類が約 25% 増しに見えていた）。"""
    arc_block = CSS.split(".arc {", 1)[1].split("}", 1)[0]
    assert "stroke-dashoffset" not in arc_block
    assert "stroke-linecap" not in arc_block
    # SVG の属性で丸めても同じなので、CSS だけでなくドーナツの SVG も見る。
    donuts = [svg for svg in check_site._iter_svg_blocks(HTML) if "chart--donut" in svg]
    assert len(donuts) == 1
    assert "stroke-linecap" not in donuts[0]
    assert "@keyframes arc-draw {" in CSS


def test_e2e_chart_values_are_also_in_a_table_below_the_chart() -> None:
    """値が <title> にしか無い図の直下に、同じ値を同じ順に並べた数値表がある（<title> はホバー・タップでしか出ず、role="img" の子は支援技術に出ない）。"""
    number = r"[+-]?\d[\d.,]*%?"
    for chart, mark in (('aria-labelledby="mrr-t', "hover-hit"), ('aria-labelledby="ch-t', "bar")):
        svg = next(block for block in check_site._iter_svg_blocks(HTML) if chart in block)
        card = HTML.split(svg, 1)[1].split("</section>", 1)[0]
        table = re.search(r'<details class="chart-data">.*?</details>', card, re.DOTALL)
        assert table, chart
        titles = re.findall(rf'<rect class="{mark}"[^>]*><title>([^<]*)</title>', svg)
        rows = re.findall(r"<tr><th scope=\"row\">(.*?)</tr>", table.group(0))
        assert titles and len(rows) == len(titles), chart
        for title, row in zip(titles, rows, strict=True):
            assert re.findall(number, unescape(title)) == re.findall(number, re.sub(r"<[^>]+>", " ", row)), title


def test_e2e_every_time_point_can_be_read_on_hover_or_tap() -> None:
    """時系列の当たり判定が月ラベルと同じ順に 12 個並び、左端 52 から右端 700 までを隙間なく覆う（点を偶数月だけに置いて奇数月に届かなかった。浮き札の挙動はブラウザで確かめ、ここは JS の結線だけを見る）。"""
    mrr = next(svg for svg in check_site._iter_svg_blocks(HTML) if 'aria-labelledby="mrr-t' in svg)
    months = re.findall(r'text-anchor="middle">(\d+月)</text>', mrr)
    hits = re.findall(r'<rect class="hover-hit" x="([\d.]+)" y="[\d.]+" width="([\d.]+)"[^>]*><title>(\d+月)&#10;', mrr)
    assert len(months) == 12
    assert [month for _, _, month in hits] == months
    spans = [(float(x), float(x) + float(width)) for x, width, _ in hits]
    assert spans[0][0] == pytest.approx(52.0)
    assert spans[-1][1] == pytest.approx(700.0)
    assert all(right == pytest.approx(left) for (_, right), (left, _) in itertools.pairwise(spans))
    assert "bindChartTips();" in JS


#: 時系列の当たり判定 1 個（数えるだけなので位置と大きさは固定）。
_HIT = '<rect class="hover-hit" x="0" y="0" width="1" height="1"/>'
_MONTHS = tuple(f"{month}月" for month in range(1, 13))
_POINT = '<circle class="point" r="6"/>'


def _path(points: int) -> str:
    """時点を points 個置く、M と L だけの折れ線の d を返す。"""
    return "M0 100" + "".join(f"L{point * 10} {100 - point}" for point in range(1, points))


def _line(points: int, hits: int, *labels: str, root: str = "<svg>", tail: str = "") -> str:
    """時点数・当たり判定の数・横軸のラベル・ルートの開始タグ・閉じる前に足す要素を指定した折れ線の SVG を返す。"""
    ticks = "".join(f'<text class="axis-text" x="0" y="236" text-anchor="middle">{label}</text>' for label in labels)
    return f'{root}<path class="series-line" d="{_path(points)}"/>{ticks}{_HIT * hits}{tail}</svg>'


#: 最も長い線（区間コマンド 16 種を 1 つずつ使う 17 時点。1 種類でも数え漏らすと 17 にならない）を比較の線にし、8 時点の本系列と 8 時点の比較の線の間に置く。
_OVERLAID = (
    f'<path class="series-line" d="{_path(8)}"/>'
    '<path class="series-line series-line--compare" d="M0 100L10 99l10 -1H30h10V97v-1C43 96 47 95 50 95c3 0 7 -1 10 -1'
    'S67 93 70 93s7 -1 10 -1Q85 92 90 91q5 0 10 -1T110 89t10 -1A5 5 0 0 1 130 87a5 5 0 0 1 10 -1"/>'
    f'<path class="series-line series-line--compare" d="{_path(8)}"/>'
)


@pytest.mark.parametrize(
    ("svg", "needle"),
    [
        # 月の行の下に年の行を置いてラベルが時点より多くても、ラベルを 1 つおきに間引いても、当たり判定は全時点に要る
        pytest.param(_line(12, 12, *_MONTHS, "2024", "2025"), None, id="year-row"),
        pytest.param(_line(12, 6, *_MONTHS[::2]), "6 個で、線の時点 12 個", id="thinned-labels"),
        # 先頭・末尾・最短の系列や本系列だけで数えると時点が 8 になり、当たり判定 8 個の間引きを見逃す
        pytest.param(f"<svg>{_OVERLAID}{_HIT * 17}</svg>", None, id="longest-series"),
        pytest.param(f"<svg>{_OVERLAID}{_HIT * 8}</svg>", "8 個で、線の時点 17 個", id="shorter-series"),
        # 散布図（ルートの svg に chart--scatter）は回帰線を series-line で引いても対象外、折れ線は強調の点（.point）を置いても対象
        pytest.param(_line(2, 0, "100", "200", "300", root='<svg class="chart chart--scatter">', tail=_POINT), None, id="scatter"),
        pytest.param(_line(12, 11, tail=_POINT), "11 個で、線の時点 12 個", id="line-with-point"),
        # 潰し描画・線の無い図・d の無い線（何も描かない）は数えない
        pytest.param(_line(12, 0, root='<svg preserveAspectRatio="none">'), None, id="stretched"),
        pytest.param('<svg><rect class="bar" width="10" height="50"/><text class="axis-text" text-anchor="middle">Q1</text></svg>', None, id="bars"),
        pytest.param(f'<svg><path class="series-line"/>{_HIT}</svg>', None, id="path-without-d"),
    ],
)
def test_time_series_hit_areas_must_reach_every_point_on_the_line(svg: str, needle: str | None) -> None:
    """線の時点より当たり判定が少ない時系列は違反になる（横軸のラベルで数えていたため、年の行を置いた正しい図が落ち、ラベルを間引いた図の間引きを見逃していた）。"""
    violations = check_site._validate_time_point_hits(svg)
    assert (needle in "\n".join(violations)) if needle else violations == [], violations


@pytest.mark.parametrize(
    ("line", "points"),
    [
        # ブラウザは単引用符の d も、改行をはさんだ d / points も、polyline も同じ線として描く
        pytest.param("<path class=\"series-line\" d='M0 100L10 99L20 98L30 97'/>", 4, id="single-quoted"),
        pytest.param('<path class="series-line" d="M0 100\n  L10 99\n  L20 98"/>', 3, id="newlines"),
        pytest.param('<polyline class="series-line" points="52.0,113.6 110.9,108.9\n  169.8,103.5 228.7,98.8"/>', 4, id="polyline"),
        # 欠測の時点（3 時点目の x=20）で線を途切れさせると M が 2 回現れ、区間の数 + 1 では 1 つ数え漏らす
        pytest.param('<path class="series-line" d="M0 100L10 99M30 97L40 96L50 95"/>', 5, id="gap"),
        # コマンドの後に座標を続けると同じコマンドの繰り返し（M の後は L）になり、コマンドの文字の数では数え漏らす
        pytest.param('<path class="series-line" d="M0,100 10,99 20,98 30,97"/>', 4, id="repeated-m"),
        pytest.param('<path class="series-line" d="M0 100L10 99 20 98 30 97"/>', 4, id="repeated-l"),
        pytest.param('<path class="series-line" d="M0 100C3 100 7 99 10 99 13 99 17 98 20 98 23 98 27 97 30 97"/>', 4, id="repeated-c"),
        # どのコマンドも引数を 2 組続けて 2 時点を置く
        pytest.param(
            '<path class="series-line" d="M0 100 5 100L10 99 15 99H20 25V98 97'
            "C27 97 28 96 30 96 32 96 33 95 35 95S38 94 40 94 43 93 45 93Q48 92 50 92 53 91 55 91"
            'T60 90 65 89A5 5 0 0 1 70 88 5 5 0 0 1 75 87"/>',
            18,
            id="every-command-repeated",
        ),
        # 円弧のフラグは区切らずに書ける（0110 は 0・1・10）。数値が引数の数に満たなくても 1 時点を置く
        pytest.param('<path class="series-line" d="M0 100A5 5 0 0110 99"/>', 2, id="packed-arc-flags"),
    ],
)
def test_time_series_hit_check_counts_points_however_the_line_is_written(line: str, points: int) -> None:
    """線の書き方を変えても、当たり判定が全時点にあれば通り、1 個足りなければ違反になる。"""
    assert check_site._validate_time_point_hits(f"<svg>{line}{_HIT * points}</svg>") == []
    thinned = check_site._validate_time_point_hits(f"<svg>{line}{_HIT * (points - 1)}</svg>")
    assert f"{points - 1} 個で、線の時点 {points} 個" in "\n".join(thinned)


def test_e2e_squashed_sparkline_does_not_use_dash_drawing() -> None:
    """潰した SVG のドローは dasharray ではなく clip-path で行う（preserveAspectRatio="none" はパスの実長を変えるので、dasharray のドローだと線が途中で切れて見える）。"""
    assert 'class="metric__spark" role="img" viewBox="0 0 200 48" preserveAspectRatio="none"' in HTML
    assert "stroke-dasharray" not in HTML.split('class="metric__spark"', 1)[1].split("</svg>", 1)[0]
    assert '[data-animate="in"] .metric__spark {' in CSS
    assert "@keyframes wipe {" in CSS


def test_e2e_charts_stay_legible_on_small_screens() -> None:
    """狭い画面では図を等倍のまま横スクロールさせる。"""
    assert HTML.count('class="chart-scroll"') >= 2
    assert ".chart-scroll {\n  overflow-x: auto;" in CSS
    # 具体的な px は固定しない。値の妥当性は `_validate_axis_text_scale` が viewBox 幅との比で判定するので、
    # ここは下限が宣言されていることだけ見る（docs/adr/staleness-detection.md: 検知器のある固定値は二重に書かない）。
    assert re.search(r"\.chart \{[^}]*min-inline-size:\s*\d+px", CSS, re.DOTALL)


def test_e2e_axis_text_is_capped_on_wide_screens() -> None:
    """広い画面で目盛り文字が、下限（_MIN_RENDERED_AXIS_PX）と宣言の間の上限で頭打ちになる（幅 1920px で宣言 15px が約 20px に伸びていた。app.js が --chart-scale を渡し、CSS が割って上限を掛ける）。"""
    cap = re.search(
        r'@media screen \{\s*\.chart\[style\*="--chart-scale"\] text \{\s*'
        r"font-size: min\((\d+)px, calc\((\d+)px / var\(--chart-scale\)\)\);",
        CSS,
    )
    assert cap is not None
    declared = check_site._css_length(CSS, ".axis-text", "font-size")
    assert float(cap.group(1)) == declared
    assert check_site._MIN_RENDERED_AXIS_PX <= float(cap.group(2)) < declared
    assert 'style.setProperty("--chart-scale"' in JS
    assert "bindChartScale();" in JS


def test_e2e_side_aligned_axis_text_is_centered_on_its_line() -> None:
    """横に寄せる軸文字は、印刷と JS 無効でも効く @media の外の dominant-baseline: central で中心を y に合わせ、目盛りの y は目盛り線と同じ値にする（ベースラインを +5 で置き、頭打ちで 2.4〜3.3px ずれていた）。"""
    assert re.search(r'^\.axis-text:not\(\[text-anchor="middle"\]\) \{\s*dominant-baseline: central;\s*\}', CSS, re.MULTILINE)
    for chart in ("mrr-t", "ch-t"):
        svg = next(svg for svg in check_site._iter_svg_blocks(HTML) if f'aria-labelledby="{chart}' in svg)
        grids = {float(y) for y in re.findall(r'<line class="grid-line" x1="[\d.]+" y1="([\d.]+)"', svg)}
        ticks = [float(y) for y in re.findall(r'<text class="axis-text" x="[\d.]+" y="([\d.]+)" text-anchor="end"', svg)]
        assert len(ticks) >= check_site._MIN_AXIS_TICKS, chart
        assert [tick for tick in ticks if tick not in grids] == [], chart


def test_e2e_metric_direction_and_sentiment_are_separate() -> None:
    """バッジの色は良し悪し、アイコンは増減の向きで、両者を混ぜない。"""
    assert ".badge--good {" in CSS and ".badge--bad {" in CSS
    assert "badge--up" not in CSS and "badge--down" not in CSS
    # 下向き矢印に good が付く指標（解約率）が実在する＝反転していない証拠。
    assert any('d="M12 5v13"' in chunk.split("</span>", 1)[0] for chunk in HTML.split('class="badge badge--good"')[1:])


def test_e2e_sidebar_collapses_on_narrow_screens() -> None:
    """狭い画面ではサイドバーをオフキャンバスにして開閉できる。"""
    assert 'data-sidebar="open"' in HTML and 'data-sidebar="close"' in HTML
    assert 'aria-controls="sidebar"' in HTML
    assert '.sidebar[data-open="true"] {' in CSS
    assert "@media (min-width: 768px)" in CSS
    assert 'event.key === "Escape"' in JS


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


# --- 図の検査 -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "replacement", "needle"),
    [
        pytest.param("index.html", ('<rect class="hover-hit" x="52.0"', '<rect class="x" x="52.0"'), "当たり判定", id="hover-hit"),
        # 同梱の図は正しい軸を持つが、モデルが新しく書いた図から軸が落ちても、検知器が無ければ全項目が合格する
        pytest.param("index.html", ('class="axis-text"', 'class="ax"'), "目盛りラベル", id="axis-text"),
        pytest.param("index.html", ('class="grid-line"', 'class="gl"'), "目盛り線", id="grid-line"),
        # `_validate_fluid_width` が横方向で禁じているのと同じ誤りを縦方向でも落とす
        pytest.param("styles.css", ("max-block-size: clamp(320px, 46vh, 620px)", "max-block-size: 320px"), "max-block-size", id="fixed-height"),
        # 破線は予測・しきい値の記法
        pytest.param("styles.css", ("  stroke-width: 1;\n}\n\n/* viewBox", "  stroke-width: 1;\n  stroke-dasharray: 3 4;\n}\n\n/* viewBox"), "破線", id="dashed-grid-line"),
        # SVG の文字は viewBox の倍率で縮むため、`font-size: 12px` の宣言でも実際には 12px を割る（ブラウザ実測 7.7px を静的計算が再現する）
        pytest.param("styles.css", ("font-size: 15px", "font-size: 12px"), "目盛り文字が最小幅で 10.0px に縮む", id="axis-text-12px"),
    ],
)
def test_broken_chart_is_reported(tmp_path: Path, filename: str, replacement: tuple[str, str], needle: str) -> None:
    """テンプレートの図の契約を 1 か所壊すと、その違反が報告される。"""
    assert needle in _edit(_copy_site(tmp_path), filename, replacement)


@pytest.mark.parametrize(
    ("svg", "cartesian"),
    [
        pytest.param('<svg><rect class="bar" /></svg>', True, id="bar"),
        pytest.param('<svg><path class="series-line" /></svg>', True, id="line"),
        # `chart-forms.md` へ図の形を足したときにマーク class の一覧（point / seg など）へ足し忘れると、その形だけが軸ゼロでも素通りする
        pytest.param('<svg class="chart chart--scatter" viewBox="0 0 720 380"><circle class="point" r="6"/></svg>', True, id="scatter"),
        pytest.param('<svg class="chart chart--stack" viewBox="0 0 720 128"><rect class="seg" width="100"/></svg>', True, id="stack"),
        pytest.param('<svg class="chart chart--hbars" viewBox="0 0 720 422"><rect class="bar--h" width="500"/></svg>', True, id="hbar"),
        pytest.param('<svg class="chart" viewBox="0 0 720 280"><rect class="bin" width="52" height="120"/></svg>', True, id="histogram"),
        # ドーナツは角度で、スパークラインは潰した面で符号化するので直交軸を持たない
        pytest.param('<svg><circle class="arc" /></svg>', False, id="donut"),
        pytest.param('<svg preserveAspectRatio="none"><path class="series-area" /></svg>', False, id="sparkline"),
    ],
)
def test_cartesian_charts_are_detected_by_mark_and_checked_for_axes(svg: str, cartesian: bool) -> None:
    """直交軸の図は class 名ではなくマークの種類で見分け（命名を変えても判定は動かない）、軸の無い直交軸の図は目盛りラベルと目盛り線の 2 件の違反になる。"""
    assert check_site._is_cartesian_chart(svg) is cartesian
    assert len(check_site._validate_chart_axes(svg)) == (2 if cartesian else 0)


def test_stretched_svg_with_text_is_reported() -> None:
    """潰し描画の SVG が文字を持つと違反になる（縦横独立に伸びて歪む）。"""
    assert check_site._validate_stretched_svg_text('<svg preserveAspectRatio="none"><text>1月</text></svg>') != []
    assert check_site._validate_stretched_svg_text('<svg preserveAspectRatio="none"></svg>') == []


@pytest.mark.parametrize(
    ("svg", "needle"),
    [
        pytest.param("<svg>" + '<circle class="arc" />' * 6 + "</svg>", "ドーナツ", id="donut-6"),
        pytest.param('<svg><rect class="bar" width="10" height="50" /></svg>', "棒 1 本", id="single-bar"),
        pytest.param("<svg>" + '<rect class="bar" width="10" height="50" />' * 8 + "</svg>", "縦棒", id="vertical-8"),
        pytest.param("<svg>" + '<rect class="bar" width="50" height="10" />' * 8 + "</svg>", None, id="horizontal-8"),
        # ヒストグラムのビンは分類ではないので、縦棒の分類上限に数えない
        pytest.param("<svg>" + '<rect class="bin" width="10" height="50" />' * 12 + "</svg>", None, id="bins-12"),
    ],
)
def test_chart_form_rules_are_enforced(svg: str, needle: str | None) -> None:
    """数えれば決まる種別規則 3 つ（ドーナツの分類数・棒 1 本・縦棒の分類数）がそれぞれ違反になる。"""
    violations = check_site._validate_chart_form(svg)
    assert (needle in "\n".join(violations)) if needle else violations == [], violations


def test_bar_orientation_is_read_from_dimensions() -> None:
    """棒の向きは class 名ではなく寸法で判定される。"""
    assert check_site._is_vertical_bar('<rect class="bar" width="10" height="50" />') is True
    assert check_site._is_vertical_bar('<rect class="bar" width="50" height="10" />') is False
    assert check_site._is_vertical_bar('<rect class="bar" />') is False


def test_chart_height_that_follows_the_viewport_passes() -> None:
    """チャート高さの上限は、ビューポートに追従する式（clamp / vh）なら通る。"""
    assert check_site._validate_fluid_height("a{max-block-size: clamp(320px, 46vh, 620px);}") == []
    assert check_site._validate_fluid_height("a{max-block-size: 60vh;}") == []


_AXIS_CSS = ".axis-text{font-size:15px}.chart{min-inline-size:600px}"


@pytest.mark.parametrize(
    ("svg", "css", "needle"),
    [
        pytest.param("<svg>axis-text</svg>", "", "font-size が読み取れない", id="no-font-size"),
        # viewBox・class・最小幅のどれかが無い SVG と、目盛りの無い SVG は計算対象にならない
        pytest.param('<svg class="chart">axis-text</svg>', _AXIS_CSS, None, id="no-viewbox"),
        pytest.param('<svg viewBox="0 0 720 250">axis-text</svg>', _AXIS_CSS, None, id="no-class"),
        pytest.param("<svg>no ticks</svg>", _AXIS_CSS, None, id="no-ticks"),
        pytest.param('<svg class="chart" viewBox="0 0 720 250">axis-text</svg>', ".axis-text{font-size:15px}", None, id="no-min-width"),
        # viewBox を狭く書いて最小幅で描くと、文字が大きく伸びる
        pytest.param('<svg class="chart" viewBox="0 0 400 200">axis-text</svg>', ".chart{min-inline-size:600px}.axis-text{font-size:15px}", "伸びる", id="too-large"),
    ],
)
def test_axis_text_rendered_scale_is_checked(svg: str, css: str, needle: str | None) -> None:
    """目盛り文字の描画時の大きさは、宣言・viewBox・class・最小幅が揃った図でだけ計算し、宣言が読めないか大きく伸びれば違反にする。"""
    violations = check_site._validate_axis_text_scale(svg, css)
    assert (needle in "\n".join(violations)) if needle else violations == [], violations


def test_css_length_returns_none_for_missing_declarations() -> None:
    """セレクタもプロパティも無ければ None を返すこと。"""
    assert check_site._css_length("", ".chart", "min-inline-size") is None
    assert check_site._css_length(".chart{color:red}", ".chart", "min-inline-size") is None


@pytest.mark.parametrize(
    ("html", "reported"),
    [
        # 器が無いと SVG がページごと横へはみ出す（実測: 幅 1269px の画面で文書幅 1422px）
        pytest.param('<div><svg class="chart">', True, id="outside"),
        pytest.param('<div class="chart-scroll"><svg class="chart">', False, id="inside"),
        pytest.param('<div><svg class="chart chart--donut">', False, id="donut"),
        # 免除はクラス名ではなく最小幅を持つかで決まる（横長の積み上げ横棒をスクロールしない .donut-wrap に置くと、最小幅 600px が 265px のカードを溢れる）
        pytest.param('<div class="donut-wrap"><svg class="chart chart--stack">', True, id="stack-in-donut-slot"),
        pytest.param('<div class="chart-scroll"><svg class="chart chart--stack">', False, id="stack-in-scroll"),
    ],
)
def test_chart_without_scroll_wrapper_is_reported(html: str, reported: bool) -> None:
    """最小幅を持つチャートが .chart-scroll の外にあると違反になる（最小幅を持たないドーナツは対象外）。"""
    assert (check_site._validate_chart_scroll_wrapper(html) != []) is reported


def _ticks(**axes: tuple[str, ...]) -> str:
    """揃えごとの目盛りラベルを並べた SVG を返す（start は揃えを省いて書く。省略時の既定）。"""
    cells = []
    for anchor, labels in axes.items():
        align = "" if anchor == "start" else f' text-anchor="{anchor}"'
        cells += [f'<text class="axis-text" x="0" y="0"{align}>{label}</text>' for label in labels]
    return f"<svg>{''.join(cells)}</svg>"


@pytest.mark.parametrize(
    ("svg", "needle"),
    [
        # 最大値を本数で割った端数（150 や 45）の刻みは、散文の規則だけでは防げない
        pytest.param(_ticks(start=("0", "150", "300", "450")), "刻みが 150", id="step-150"),
        pytest.param(_ticks(start=("60", "105", "150")), "刻みが 45", id="step-45"),
        pytest.param(_ticks(start=("0", "100", "200", "300")), None, id="step-100"),
        pytest.param(_ticks(start=("0", "2,000", "4,000")), None, id="step-2000"),
        pytest.param(_ticks(start=("0", "5", "10")), None, id="step-5"),
        pytest.param(_ticks(start=("0", "100", "400")), "等間隔", id="uneven"),
        # 分類ラベルは数値として読めないので対象外。目盛り 1 本以下も対象外
        pytest.param(_ticks(start=("1月", "2月", "3月")), None, id="categories"),
        pytest.param(_ticks(start=("100",)), None, id="single-tick"),
        # 散布図・ヒストグラムの x 目盛り（中央揃え）を y 目盛り（右揃え、揃えの省略を含む）と 1 列にまとめると、正しい図が「等間隔でない」で落ちる
        pytest.param(_ticks(end=("0", "500", "1,000"), middle=("0", "50", "100", "150")), None, id="both-axes"),
        pytest.param(_ticks(end=("0", "500", "1,000"), middle=("0", "45", "90")), "刻みが 45", id="bad-x-axis"),
        pytest.param(_ticks(start=("0", "500", "1,000"), middle=("0", "50", "100")), None, id="unanchored-y-axis"),
    ],
)
def test_tick_steps_must_be_even_nice_numbers_per_axis(svg: str, needle: str | None) -> None:
    """値の目盛りは軸（揃え）ごとに等間隔で、刻みが 1・2・5 の倍数でなければ違反になる。"""
    violations = check_site._validate_tick_steps(svg)
    assert (needle in "\n".join(violations)) if needle else violations == [], violations


def test_nice_step_rejects_non_decade_mantissas() -> None:
    """刻みの仮数判定が 1・2・5（と 10）だけを通すこと。"""
    assert check_site._is_nice_step(0.5) is True
    assert check_site._is_nice_step(20) is True
    assert check_site._is_nice_step(45) is False
    assert check_site._is_nice_step(25) is False
    assert check_site._is_nice_step(0) is False


def test_template_charts_use_nice_tick_steps() -> None:
    """テンプレート同梱の図が刻み規則を満たすこと（模写元が規則を破らない）。"""
    assert check_site._validate_tick_steps(HTML) == []


def test_every_role_img_svg_has_title_and_desc() -> None:
    """`svg[role="img"]` が例外なく `<title>`/`<desc>` を持つこと（design.md はスパークラインのような小さな図も含めて両方を求めており、模写元が破ると散文が効かない）。"""
    bare = [svg[:90] for svg in check_site._iter_svg_blocks(HTML) if 'role="img"' in svg and ("<title" not in svg or "<desc" not in svg)]
    assert bare == [], f"title/desc を欠く role=img の SVG: {bare}"
