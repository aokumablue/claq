"""html-gen テンプレートの、ナビのリンク・一覧の検索絞り込み・図の文字の統一に関する契約の検査。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ._support import CSS, HTML, JS, _copy_site, _edit, check_site

# --- テンプレート本体 -------------------------------------------------------


def test_template_nav_items_are_links_to_existing_sections() -> None:
    """テンプレートのナビは全項目が文書内の実在する節へ飛ぶ <a href> である。"""
    items = re.findall(r'<(\w+) class="nav-item"([^>]*)>', HTML)
    assert len(items) >= 3
    assert {tag for tag, _ in items} == {"a"}
    for _, attrs in items:
        href = re.search(r'href="#([^"]+)"', attrs)
        assert href is not None
        assert f'id="{href.group(1)}"' in HTML


def test_nav_script_closes_sidebar_and_does_not_steal_scroll() -> None:
    """リンク選択で狭い画面のサイドバーを閉じ、閉じる操作がスクロール位置を戻さない。"""
    assert 'data-sidebar="close"' in JS
    assert "preventScroll: true" in JS
    assert ".pane [id] {\n  scroll-margin-block-start:" in CSS


def test_template_list_table_is_wired_for_search() -> None:
    """テンプレートの一覧は data-list を持ち、検索欄は HTML に手書きされていない。"""
    assert "<table data-list>" in HTML
    assert 'type="search"' not in HTML
    assert "table[data-list]" in JS
    assert "bindList();" in JS
    assert 'normalize("NFKC")' in JS


def test_template_chart_text_is_uniform() -> None:
    """テンプレートの図の文字は、すべて axis-text で、font-size を個別に持たない。"""
    assert check_site._validate_chart_text(HTML) == []
    assert re.search(r"\.chart text,\s*\.axis-text \{[^}]*font-size: 15px", CSS)


# --- 壊すと落ちる契約と検査関数 ---------------------------------------------


@pytest.mark.parametrize(
    ("replacement", "needle"),
    [
        # ナビの項目が <button> だと押しても移動しない。飛び先の id が文書に無いリンクと、ページ内の節へ飛ばない href も違反
        pytest.param(('<a class="nav-item" href="#trend">', '<button class="nav-item" type="button">'), "<a href", id="nav-button"),
        pytest.param(('href="#contracts"', 'href="#missing"'), "#missing", id="nav-missing-target"),
        pytest.param(('href="#contracts"', 'href="/contracts"'), "<a href", id="nav-external-href"),
        # 手書きの検索欄（role=search の手書きを含む）は結線されない
        pytest.param(("<main", '<input type="search" aria-label="検索"><main'), "検索欄を手書き", id="search-input"),
        pytest.param(("<main", '<div role="search"></div><main'), "検索欄を手書き", id="search-landmark"),
        # 一覧の表が data-list を持たない。data-list の表に thead / tbody が無いと絞り込みの列を読めない
        pytest.param(("<table data-list>", "<table>"), "data-list が無い", id="list-without-data-list"),
        pytest.param(("<table data-list>", "<table data-list><tr><td>x</td><td>y</td></tr></table><table>"), "<thead> と <tbody>", id="list-without-head-body"),
        pytest.param(('<text class="axis-text" x="42"', '<text class="axis-text" font-size="22" x="42"'), "font-size を個別に指定", id="chart-text-font-size"),
    ],
)
def test_broken_template_is_reported(tmp_path: Path, replacement: tuple[str, str], needle: str) -> None:
    """テンプレートのナビ・一覧・図の文字を 1 か所壊すと、その違反が報告される。"""
    assert needle in _edit(_copy_site(tmp_path), "index.html", replacement)


@pytest.mark.parametrize(
    ("html", "needle"),
    [
        pytest.param('<details class="chart-data"><table><thead></thead></table></details><table class="heat"><tbody></tbody></table><table class="x heat"><tbody></tbody></table>', None, id="heat-and-figure"),
        pytest.param('<table><tbody><tr><th scope="row">社名</th><td>Nimbus</td></tr><tr><th scope="row">設立</th><td>2020</td></tr></tbody></table>', None, id="key-value"),
        pytest.param('<table><thead><tr><th>a</th></tr></thead><tbody><tr><th scope="row">x</th><td>1</td><td>2</td></tr></tbody></table>', "data-list が無い", id="multi-column"),
    ],
)
def test_only_list_tables_need_data_list(html: str, needle: str | None) -> None:
    """値が 2 列以上ある表は一覧として data-list が要り、ヒートマップ表・図の直下の表・行見出し + 値 1 列の項目表（会社概要など）は要らない。"""
    violations = check_site._validate_lists(html, [])
    assert (needle in "\n".join(violations)) if needle else violations == [], violations


def test_table_without_body_rows_is_not_a_key_value_table() -> None:
    """本体に行の無い表は項目表とみなさない。"""
    assert check_site._is_key_value_table("<tbody></tbody>") is False


@pytest.mark.parametrize(
    ("svg", "needle"),
    [
        # style の font-size も、<text> の子の <tspan> に書いた font-size も、CSS の .chart text が効かない
        pytest.param('<svg><text class="axis-text" style="font-size: 20px">1</text></svg>', "font-size を個別に指定", id="style-font-size"),
        pytest.param('<svg class="chart"><text class="axis-text"><tspan font-size="30">1</tspan></text></svg>', "font-size を個別に指定", id="tspan-font-size"),
        # class を持たない <text> は軸文字と大きさが揃わない
        pytest.param('<svg><text x="1">label</text></svg>', "axis-text を持たない", id="text-without-class"),
        # <text> を持つ SVG のルートに class="chart" が無いと、頭打ちが掛からない
        pytest.param('<svg role="img" viewBox="0 0 720 250"><text class="axis-text">1</text></svg>', 'class="chart" が無い', id="svg-without-chart-class"),
        pytest.param('<svg class="chart chart--bars" viewBox="0 0 470 252"><text class="axis-text">1</text></svg>', None, id="chart-class"),
        # 文字を持たない SVG（アイコン・スパークライン）は class="chart" を要求されない
        pytest.param('<svg viewBox="0 0 24 24"><path d="M0 0"/></svg>', None, id="svg-without-text"),
    ],
)
def test_chart_text_is_axis_text_under_a_chart_root(svg: str, needle: str | None) -> None:
    """図の文字は axis-text で font-size を個別に持たず、文字を持つ SVG はルートに class="chart" を持つ。"""
    violations = check_site._validate_chart_text(svg)
    assert (needle in "\n".join(violations)) if needle else violations == [], violations
