"""html-gen テンプレートの、ナビのリンク・一覧の検索絞り込み・図の文字の統一に関する契約の検査。"""

from __future__ import annotations

import re
from pathlib import Path

from ._support import _TEMPLATE, _copy_site, _edit, check_site

# --- ナビのリンク ------------------------------------------------------------


def test_template_nav_items_are_links_to_existing_sections() -> None:
    """テンプレートのナビは全項目が文書内の実在する節へ飛ぶ <a href> である。"""
    html = (_TEMPLATE / "index.html").read_text(encoding="utf-8")
    items = re.findall(r'<(\w+) class="nav-item"([^>]*)>', html)
    assert len(items) >= 3
    assert {tag for tag, _ in items} == {"a"}
    for _, attrs in items:
        href = re.search(r'href="#([^"]+)"', attrs)
        assert href is not None
        assert f'id="{href.group(1)}"' in html


def test_nav_button_is_reported(tmp_path: Path) -> None:
    """ナビの項目が <button> だと、押しても移動しないので違反になる。"""
    dest = _copy_site(tmp_path)
    report = _edit(
        dest,
        "index.html",
        ('<a class="nav-item" href="#trend">', '<button class="nav-item" type="button">'),
    )
    assert "<a href" in report


def test_nav_link_without_target_is_reported(tmp_path: Path) -> None:
    """飛び先の id が文書に無いリンクは違反になる。"""
    dest = _copy_site(tmp_path)
    report = _edit(dest, "index.html", ('href="#contracts"', 'href="#missing"'))
    assert "#missing" in report


def test_nav_link_with_external_href_is_reported(tmp_path: Path) -> None:
    """ページ内の節へ飛ばない href は違反になる。"""
    dest = _copy_site(tmp_path)
    report = _edit(dest, "index.html", ('href="#contracts"', 'href="/contracts"'))
    assert "<a href" in report


def test_nav_script_closes_sidebar_and_does_not_steal_scroll() -> None:
    """リンク選択で狭い画面のサイドバーを閉じ、閉じる操作がスクロール位置を戻さない。"""
    script = (_TEMPLATE / "app.js").read_text(encoding="utf-8")
    assert 'data-sidebar="close"' in script
    assert "preventScroll: true" in script
    css = (_TEMPLATE / "styles.css").read_text(encoding="utf-8")
    assert ".pane [id] {\n  scroll-margin-block-start:" in css


# --- 一覧の検索・絞り込み ----------------------------------------------------


def test_template_list_table_is_wired_for_search() -> None:
    """テンプレートの一覧は data-list を持ち、検索欄は HTML に手書きされていない。"""
    html = (_TEMPLATE / "index.html").read_text(encoding="utf-8")
    assert "<table data-list>" in html
    assert 'type="search"' not in html
    script = (_TEMPLATE / "app.js").read_text(encoding="utf-8")
    assert "table[data-list]" in script
    assert "bindList();" in script
    assert 'normalize("NFKC")' in script


def test_handwritten_search_input_is_reported(tmp_path: Path) -> None:
    """手書きの検索欄は結線されないので違反になる。"""
    dest = _copy_site(tmp_path)
    report = _edit(dest, "index.html", ("<main", '<input type="search" aria-label="検索"><main'))
    assert "検索欄を手書き" in report


def test_search_landmark_written_by_hand_is_reported(tmp_path: Path) -> None:
    """role=search の手書きも同様に違反になる。"""
    dest = _copy_site(tmp_path)
    report = _edit(dest, "index.html", ("<main", '<div role="search"></div><main'))
    assert "検索欄を手書き" in report


def test_list_table_without_data_list_is_reported(tmp_path: Path) -> None:
    """一覧の表が data-list を持たないと違反になる。"""
    dest = _copy_site(tmp_path)
    report = _edit(dest, "index.html", ("<table data-list>", "<table>"))
    assert "data-list が無い" in report


def test_list_table_needs_head_and_body(tmp_path: Path) -> None:
    """data-list の表に thead / tbody が無いと、絞り込みの列を読めないので違反になる。"""
    dest = _copy_site(tmp_path)
    report = _edit(
        dest, "index.html", ("<table data-list>", "<table data-list><tr><td>x</td><td>y</td></tr></table><table>")
    )
    assert "<thead> と <tbody>" in report


def test_heat_and_figure_tables_are_not_lists() -> None:
    """ヒートマップ表と図の直下の表は一覧ではなく、data-list を要求しない。"""
    html = (
        '<details class="chart-data"><table><thead></thead></table></details>'
        '<table class="heat"><tbody></tbody></table>'
        '<table class="x heat"><tbody></tbody></table>'
    )
    assert check_site._validate_lists(html, []) == []


# --- 図の文字の統一 ----------------------------------------------------------


def test_template_chart_text_is_uniform() -> None:
    """テンプレートの図の文字は、すべて axis-text で、font-size を個別に持たない。"""
    html = (_TEMPLATE / "index.html").read_text(encoding="utf-8")
    assert check_site._validate_chart_text(html) == []
    css = (_TEMPLATE / "styles.css").read_text(encoding="utf-8")
    assert re.search(r"\.chart text,\s*\.axis-text \{[^}]*font-size: 15px", css)


def test_chart_text_with_own_font_size_is_reported(tmp_path: Path) -> None:
    """図の中の文字が font-size を属性で書くと違反になる。"""
    dest = _copy_site(tmp_path)
    report = _edit(
        dest, "index.html", ('<text class="axis-text" x="42"', '<text class="axis-text" font-size="22" x="42"')
    )
    assert "font-size を個別に指定" in report


def test_chart_text_with_style_font_size_is_reported() -> None:
    """style の font-size も同じく違反になる。"""
    svg = '<svg><text class="axis-text" style="font-size: 20px">1</text></svg>'
    assert "font-size を個別に指定" in "".join(check_site._validate_chart_text(svg))


def test_chart_text_without_axis_text_class_is_reported() -> None:
    """class を持たない <text> は軸文字と大きさが揃わないので違反になる。"""
    svg = '<svg><text x="1">label</text></svg>'
    assert "axis-text を持たない" in "".join(check_site._validate_chart_text(svg))


def test_svg_with_text_but_no_chart_class_is_reported() -> None:
    """<text> を持つ SVG のルートに class="chart" が無いと、頭打ちが掛からないので違反になる。"""
    svg = '<svg role="img" viewBox="0 0 720 250"><text class="axis-text">1</text></svg>'
    assert 'class="chart" が無い' in "".join(check_site._validate_chart_text(svg))
    ok = '<svg class="chart chart--bars" viewBox="0 0 470 252"><text class="axis-text">1</text></svg>'
    assert check_site._validate_chart_text(ok) == []


def test_font_size_on_a_tspan_is_reported() -> None:
    """<text> の子の <tspan> に書いた font-size も、CSS の .chart text が効かない経路なので違反になる。"""
    svg = '<svg class="chart"><text class="axis-text"><tspan font-size="30">1</tspan></text></svg>'
    assert "font-size を個別に指定" in "".join(check_site._validate_chart_text(svg))


def test_svg_without_text_is_not_a_chart_text_target() -> None:
    """文字を持たない SVG（アイコン・スパークライン）は class="chart" を要求されない。"""
    assert check_site._validate_chart_text('<svg viewBox="0 0 24 24"><path d="M0 0"/></svg>') == []


def test_key_value_table_is_not_a_list() -> None:
    """行見出し + 値 1 列の項目表（会社概要など）は、data-list を要求されない。"""
    table = '<table><tbody><tr><th scope="row">社名</th><td>Nimbus</td></tr><tr><th scope="row">設立</th><td>2020</td></tr></tbody></table>'
    assert check_site._validate_lists(table, []) == []


def test_multi_column_table_is_still_a_list() -> None:
    """値が 2 列以上ある表は一覧で、data-list が要る。"""
    table = '<table><thead><tr><th>a</th></tr></thead><tbody><tr><th scope="row">x</th><td>1</td><td>2</td></tr></tbody></table>'
    assert "data-list が無い" in "".join(check_site._validate_lists(table, []))
    assert check_site._is_key_value_table("<tbody></tbody>") is False
