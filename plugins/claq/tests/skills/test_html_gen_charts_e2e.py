"""html-gen の静的サイトの E2E 検査と、チャート・ドキュメントの契約の検査。"""

from __future__ import annotations

import itertools
import json
import re
from html import unescape
from pathlib import Path

import pytest

from ._support import (
    _SKILL_DIR,
    _TEMPLATE,
    BASES,
    TOKENS,
    _copy_site,
    _edit,
    check_site,
)

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
    # 棒とビンが data-animate の下で grow-y の動きを持つことを、セレクタの並びや改行に依らず見る。
    rules = re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL))
    for mark in (".bar", ".bin"):
        assert any(
            f'[data-animate="in"] {mark}' in {selector.strip() for selector in selectors.split(",")}
            and re.search(r"\banimation:\s*grow-y(?![\w-])", body)
            for selectors, body in rules
        ), mark
    assert '[data-animate="in"] .arc {' in css
    assert ".series-line {\n  fill: none;" in css


def test_e2e_arcs_keep_their_start_and_true_length() -> None:
    """円弧の開始位置を CSS で宣言せず、端を丸めない。

    CSS は presentation attribute より優先されるため、ここで
    `stroke-dashoffset` を宣言すると全セグメントが 12 時から重なって描かれる。
    端を丸めると弧が線幅の半分ずつ伸び、小さい分類ほど大きく見える。
    """
    css = (_TEMPLATE / "styles.css").read_text(encoding="utf-8")
    html = (_TEMPLATE / "index.html").read_text(encoding="utf-8")
    arc_block = css.split(".arc {", 1)[1].split("}", 1)[0]
    assert "stroke-dashoffset" not in arc_block
    # 丸い端は弧を線幅の半分ずつ伸ばし、11% の分類が約 25% 増しに見えていた。
    assert "stroke-linecap" not in arc_block
    # SVG の属性で丸めても同じなので、CSS だけでなくドーナツの SVG も見る。
    donuts = [svg for svg in check_site._iter_svg_blocks(html) if "chart--donut" in svg]
    assert len(donuts) == 1
    assert "stroke-linecap" not in donuts[0]
    assert "@keyframes arc-draw {" in css


def test_e2e_chart_values_are_also_in_a_table_below_the_chart() -> None:
    """値が <title> にしか無い図の直下に、同じ値を同じ順に並べた数値表があること。

    <title> はホバー・タップでしか出ず、role="img" の子は支援技術に出ないので、
    キーボードや読み上げの利用者は表でしか値に届かない。
    """
    html = (_TEMPLATE / "index.html").read_text(encoding="utf-8")
    number = r"[+-]?\d[\d.,]*%?"
    for chart, mark in (('aria-labelledby="mrr-t', "hover-hit"), ('aria-labelledby="ch-t', "bar")):
        svg = next(block for block in check_site._iter_svg_blocks(html) if chart in block)
        card = html.split(svg, 1)[1].split("</section>", 1)[0]
        table = re.search(r'<details class="chart-data">.*?</details>', card, re.DOTALL)
        assert table, chart
        titles = re.findall(rf'<rect class="{mark}"[^>]*><title>([^<]*)</title>', svg)
        rows = re.findall(r"<tr><th scope=\"row\">(.*?)</tr>", table.group(0))
        assert titles and len(rows) == len(titles), chart
        for title, row in zip(titles, rows, strict=True):
            assert re.findall(number, unescape(title)) == re.findall(number, re.sub(r"<[^>]+>", " ", row)), title


def test_e2e_every_time_point_can_be_read_on_hover_or_tap() -> None:
    """時系列の全時点に当たり判定があり、プロット領域を隙間なく覆うこと。

    点を偶数月だけに置いていたため、奇数月と比較系列の値に届かなかった。当たり判定は
    月ラベルと同じ順に 12 個並び、左端 52 から右端 700 までを隙間なく覆う。
    浮き札の挙動（タッチで出る・閉じると <title> を戻す）はこのテストでは見られないので、
    JS は boot で結線していることだけを確かめ、挙動はブラウザで確かめる。
    """
    html = (_TEMPLATE / "index.html").read_text(encoding="utf-8")
    script = (_TEMPLATE / "app.js").read_text(encoding="utf-8")
    mrr = next(svg for svg in check_site._iter_svg_blocks(html) if 'aria-labelledby="mrr-t' in svg)
    months = re.findall(r'text-anchor="middle">(\d+月)</text>', mrr)
    hits = re.findall(
        r'<rect class="hover-hit" x="([\d.]+)" y="[\d.]+" width="([\d.]+)"[^>]*><title>(\d+月)&#10;', mrr
    )
    assert len(months) == 12
    assert [month for _, _, month in hits] == months
    spans = [(float(x), float(x) + float(width)) for x, width, _ in hits]
    assert spans[0][0] == pytest.approx(52.0)
    assert spans[-1][1] == pytest.approx(700.0)
    assert all(right == pytest.approx(left) for (_, right), (left, _) in itertools.pairwise(spans))
    assert "bindChartTips();" in script


def test_time_series_hit_areas_must_reach_every_point_on_the_line(tmp_path: Path) -> None:
    """線の時点より当たり判定が少ない時系列は違反になること。

    時点は .series-line の線が置く頂点の数で数える。横軸のラベルで数えていたため、
    月の行の下に年の行を置いた正しい図が落ち、ラベルを間引いた図では当たり判定の間引きを
    見逃していた。散布図（ルートの svg に chart--scatter）は点が自身の <title> で値を
    読ませるので、回帰線を series-line で引いても対象外。折れ線に強調の点（.point）を
    置いても対象のまま。潰し描画と線の無い図も対象外。
    """
    dest = _copy_site(tmp_path)
    assert "当たり判定" in _edit(dest, "index.html", ('<rect class="hover-hit" x="52.0"', '<rect class="x" x="52.0"'))

    def line(points: int, hits: int, labels: tuple[str, ...] = ()) -> str:
        """時点数・当たり判定の数・横軸のラベルを指定した折れ線の SVG を返す。"""
        path = "M0 100" + "".join(f"L{point * 10} {100 - point}" for point in range(1, points))
        ticks = "".join(f'<text class="axis-text" x="0" y="236" text-anchor="middle">{label}</text>' for label in labels)
        areas = '<rect class="hover-hit" x="0" y="0" width="1" height="1"/>' * hits
        return f'<svg><path class="series-line" d="{path}"/>{ticks}{areas}</svg>'

    months = tuple(f"{month}月" for month in range(1, 13))
    # 月の行の下に年の行を置くと、ラベル（14 個）が時点（12 個）より多くなる
    assert check_site._validate_time_point_hits(line(12, 12, (*months, "2024", "2025"))) == []
    # ラベルを 1 つおきに間引いても、当たり判定は全時点に要る
    sparse = line(12, 6, months[::2])
    assert "6 個で、線の時点 12 個" in "\n".join(check_site._validate_time_point_hits(sparse))
    # 系列の長さが違うときは最も長い系列で数える。最も長い線（区間コマンド 16 種を 1 つずつ
    # 使う 17 時点）を比較の線にし、8 時点の本系列と 8 時点の比較の線の間に置く。先頭・末尾・
    # 最短の系列や本系列だけで数えると時点が 8 になり、当たり判定 8 個の間引きを見逃す。
    # コマンドを 1 種類でも数え漏らすと時点が 17 にならず、断言で落ちる。
    short = "M0 100" + "".join(f"L{point * 10} {100 - point}" for point in range(1, 8))
    mixed = (
        "M0 100L10 99l10 -1H30h10V97v-1C43 96 47 95 50 95c3 0 7 -1 10 -1"
        "S67 93 70 93s7 -1 10 -1Q85 92 90 91q5 0 10 -1T110 89t10 -1A5 5 0 0 1 130 87a5 5 0 0 1 10 -1"
    )
    paths = (
        f'<path class="series-line" d="{short}"/>'
        f'<path class="series-line series-line--compare" d="{mixed}"/>'
        f'<path class="series-line series-line--compare" d="{short}"/>'
    )
    area = '<rect class="hover-hit" x="0" y="0" width="1" height="1"/>'
    assert check_site._validate_time_point_hits(f"<svg>{paths}{area * 17}</svg>") == []
    overlaid = check_site._validate_time_point_hits(f"<svg>{paths}{area * 8}</svg>")
    assert "8 個で、線の時点 17 個" in "\n".join(overlaid)
    dot = '<circle class="point" r="6"/></svg>'
    scatter = line(2, 0, ("100", "200", "300")).replace("<svg>", '<svg class="chart chart--scatter">')
    assert check_site._validate_time_point_hits(scatter.replace("</svg>", dot)) == []
    marked = line(12, 11).replace("</svg>", dot)
    assert "11 個で、線の時点 12 個" in "\n".join(check_site._validate_time_point_hits(marked))
    stretched = line(12, 0).replace("<svg>", '<svg preserveAspectRatio="none">')
    assert check_site._validate_time_point_hits(stretched) == []
    bars = '<svg><rect class="bar" width="10" height="50"/><text class="axis-text" text-anchor="middle">Q1</text></svg>'
    assert check_site._validate_time_point_hits(bars) == []


def test_time_series_hit_check_counts_points_however_the_line_is_written() -> None:
    """線の書き方を変えても、当たり判定が 1 個足りなければ違反になること。

    ブラウザは単引用符の d も、改行をはさんだ d / points も、polyline も同じ線として描く。
    1 つのコマンドの後に座標を続けると同じコマンドの繰り返しになり、コマンドの文字の数では
    時点を数え漏らす。欠測の時点で線を途切れさせると M が 2 回現れ、区間の数 + 1 では
    時点を 1 つ数え漏らす。
    """
    area = '<rect class="hover-hit" x="0" y="0" width="1" height="1"/>'
    for line, points in (
        ("<path class=\"series-line\" d='M0 100L10 99L20 98L30 97'/>", 4),
        ('<path class="series-line" d="M0 100\n  L10 99\n  L20 98"/>', 3),
        ('<polyline class="series-line" points="52.0,113.6 110.9,108.9\n  169.8,103.5 228.7,98.8"/>', 4),
        # 3 時点目（x=20）が欠測
        ('<path class="series-line" d="M0 100L10 99M30 97L40 96L50 95"/>', 5),
        # M の後に続けた座標は L の繰り返しになる
        ('<path class="series-line" d="M0,100 10,99 20,98 30,97"/>', 4),
        ('<path class="series-line" d="M0 100L10 99 20 98 30 97"/>', 4),
        ('<path class="series-line" d="M0 100C3 100 7 99 10 99 13 99 17 98 20 98 23 98 27 97 30 97"/>', 4),
        # どのコマンドも引数を 2 組続けて 2 時点を置く
        (
            '<path class="series-line" d="M0 100 5 100L10 99 15 99H20 25V98 97'
            "C27 97 28 96 30 96 32 96 33 95 35 95S38 94 40 94 43 93 45 93Q48 92 50 92 53 91 55 91"
            'T60 90 65 89A5 5 0 0 1 70 88 5 5 0 0 1 75 87"/>',
            18,
        ),
        # 円弧のフラグは区切らずに書ける（0110 は 0・1・10）。数値が引数の数に満たなくても 1 時点を置く
        ('<path class="series-line" d="M0 100A5 5 0 0110 99"/>', 2),
    ):
        assert check_site._validate_time_point_hits(f"<svg>{line}{area * points}</svg>") == [], line
        thinned = check_site._validate_time_point_hits(f"<svg>{line}{area * (points - 1)}</svg>")
        assert f"{points - 1} 個で、線の時点 {points} 個" in "\n".join(thinned), line
    # d の無い線は何も描かないので、時点を数えずに飛ばす
    assert check_site._validate_time_point_hits(f'<svg><path class="series-line"/>{area}</svg>') == []


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
    # （docs/adr/staleness-detection.md: 検知器のある固定値は二重に書かない）。
    assert re.search(r"\.chart \{[^}]*min-inline-size:\s*\d+px", css, re.DOTALL)


def test_e2e_axis_text_is_capped_on_wide_screens() -> None:
    """広い画面で目盛り文字が、宣言より小さい上限で頭打ちになること。

    SVG の文字は viewBox の倍率で一緒に伸びるので、幅 1920px では宣言 15px の目盛りが
    約 20px になり、カード見出しより大きくなっていた。app.js が描画倍率を
    --chart-scale として渡し、CSS がそれを割って描画後の大きさに上限を掛ける。
    上限は下限（_MIN_RENDERED_AXIS_PX）と宣言サイズの間に置き、JS が動かない環境と
    印刷では宣言のまま伸ばす。
    """
    css = (_TEMPLATE / "styles.css").read_text(encoding="utf-8")
    script = (_TEMPLATE / "app.js").read_text(encoding="utf-8")
    cap = re.search(
        r'@media screen \{\s*\.chart\[style\*="--chart-scale"\] \.axis-text \{\s*'
        r"font-size: min\((\d+)px, calc\((\d+)px / var\(--chart-scale\)\)\);",
        css,
    )
    assert cap is not None
    declared = check_site._css_length(css, ".axis-text", "font-size")
    assert float(cap.group(1)) == declared
    assert check_site._MIN_RENDERED_AXIS_PX <= float(cap.group(2)) < declared
    assert 'style.setProperty("--chart-scale"' in script
    assert "bindChartScale();" in script


def test_e2e_side_aligned_axis_text_is_centered_on_its_line() -> None:
    """線や棒へ横に寄せる軸文字は、y に書いた線の中心へ文字の中心が合うこと。

    縦軸の目盛りを目盛り線 +5 のベースラインで置いていたため、広い画面で文字を 13px で
    頭打ちにすると、目盛りが線から下へずれた（幅 1920 の実測で 2.4〜3.3px）。中央揃え
    以外の文字は CSS の dominant-baseline: central で中心を y に合わせ、目盛りの y には
    目盛り線と同じ値を書く。規則は印刷と JS 無効（15px のまま）でも効くよう、
    @media の外（行頭）に置く。
    """
    css = (_TEMPLATE / "styles.css").read_text(encoding="utf-8")
    html = (_TEMPLATE / "index.html").read_text(encoding="utf-8")
    assert re.search(
        r'^\.axis-text:not\(\[text-anchor="middle"\]\) \{\s*dominant-baseline: central;\s*\}', css, re.MULTILINE
    )
    for chart in ("mrr-t", "ch-t"):
        svg = next(svg for svg in check_site._iter_svg_blocks(html) if f'aria-labelledby="{chart}' in svg)
        grids = {float(y) for y in re.findall(r'<line class="grid-line" x1="[\d.]+" y1="([\d.]+)"', svg)}
        ticks = [float(y) for y in re.findall(r'<text class="axis-text" x="[\d.]+" y="([\d.]+)" text-anchor="end"', svg)]
        assert len(ticks) >= check_site._MIN_AXIS_TICKS, chart
        assert [tick for tick in ticks if tick not in grids] == [], chart


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
    軸が落ちても検知器が無ければ全項目が合格する。両方向を固定する。
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

    # ヒストグラムのビンは分類ではないので、縦棒の分類上限に数えない
    bins = "<svg>" + '<rect class="bin" width="10" height="50" />' * 12 + "</svg>"
    assert check_site._validate_chart_form(bins) == []


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

    `chart-forms.md` へ図の形を足したときにマーク class の一覧（`point` / `seg`
    など）へ足し忘れると、その形だけが軸ゼロでも素通りする。形の語彙を広げて
    検知器を広げないと、新しい形だけが無検査になる。
    """
    scatter = '<svg class="chart chart--scatter" viewBox="0 0 720 380"><circle class="point" r="6"/></svg>'
    stack = '<svg class="chart chart--stack" viewBox="0 0 720 128"><rect class="seg" width="100"/></svg>'
    hbar = '<svg class="chart chart--hbars" viewBox="0 0 720 422"><rect class="bar--h" width="500"/></svg>'
    hist = '<svg class="chart" viewBox="0 0 720 280"><rect class="bin" width="52" height="120"/></svg>'
    for name, svg in (("散布図", scatter), ("積み上げ", stack), ("横棒", hbar), ("ヒストグラム", hist)):
        assert check_site._is_cartesian_chart(svg) is True, name
        assert len(check_site._validate_chart_axes(svg)) == 2, name


def test_tick_steps_must_be_nice_numbers() -> None:
    """目盛りの刻みが 1・2・5 の倍数でなければ違反になること。

    最大値を本数で割った端数（45 や 150）の刻みは、散文の規則だけでは防げない。
    """
    def svg(*labels: str) -> str:
        """揃えを省いた（start の）目盛りラベルを並べた SVG を返す。"""
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


def test_tick_steps_are_compared_per_axis() -> None:
    """両軸が数値の図では、縦軸と横軸の目盛りを別々に比べること。

    散布図・ヒストグラムの x 目盛りを y 目盛りと 1 列にまとめると、正しい図が
    「等間隔でない」で落ちる。縦軸は右揃え、横軸は中央揃えで見分ける。
    """
    def axis(anchor: str, *labels: str) -> str:
        """指定の揃えで目盛りラベルを並べた断片を返す。"""
        return "".join(
            f'<text class="axis-text" x="0" y="0" text-anchor="{anchor}">{label}</text>' for label in labels
        )

    both = f'<svg>{axis("end", "0", "500", "1,000")}{axis("middle", "0", "50", "100", "150")}</svg>'
    assert check_site._validate_tick_steps(both) == []
    bad_x = f'<svg>{axis("end", "0", "500", "1,000")}{axis("middle", "0", "45", "90")}</svg>'
    assert "45" in "\n".join(check_site._validate_tick_steps(bad_x))
    # 縦軸を左揃え（揃えの省略を含む）で書いた図も、中央揃えの横軸と混ぜない
    unanchored = "".join(f'<text class="axis-text" x="0" y="0">{label}</text>' for label in ("0", "500", "1,000"))
    assert check_site._validate_tick_steps(f'<svg>{unanchored}{axis("middle", "0", "50", "100")}</svg>') == []


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


def test_every_role_img_svg_has_title_and_desc() -> None:
    """`svg[role="img"]` が例外なく `<title>`/`<desc>` を持つこと。

    design.md は `svg[role="img"]` 全てに両方を求めている（スパークラインのような
    小さな図も含む）。模写元が破っていると散文が効かない。
    """
    html = (_TEMPLATE / "index.html").read_text(encoding="utf-8")
    bare = [
        svg[:90]
        for svg in check_site._iter_svg_blocks(html)
        if 'role="img"' in svg and ("<title" not in svg or "<desc" not in svg)
    ]
    assert bare == [], f"title/desc を欠く role=img の SVG: {bare}"


def test_stacked_bar_in_the_donut_slot_is_reported() -> None:
    """積み上げ横棒をドーナツ用の狭い枠へ置いたら違反になること。

    `.chart--donut` だけが最小幅を持たないので検査の対象外になる。横長の
    `.chart--stack` が同じ枠（`.donut-wrap` はスクロールコンテナではない）に
    入ると、最小幅 600px が 265px のカードを溢れてページごと横へ伸びる。
    免除がクラス名ではなく「最小幅を持つか」で決まっていることを固定する。
    """
    assert check_site._validate_chart_scroll_wrapper(
        '<div class="donut-wrap"><svg class="chart chart--stack">'
    ) != []
    # .chart-scroll の中なら、溢れはカード内の横スクロールに閉じるので通す
    assert check_site._validate_chart_scroll_wrapper(
        '<div class="chart-scroll"><svg class="chart chart--stack">'
    ) == []
