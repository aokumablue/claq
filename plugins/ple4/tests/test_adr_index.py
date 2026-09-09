"""``docs/adr/`` のトピックファイル構成とインデックスの整合を検証する構造テスト。

1 決定 = 1 ファイルだった頃は、ファイル名（``0001-*.md``）そのものが番号の一意性と実在を
保証していた。トピックごとの 1 ファイルへ集約した結果その不変条件はファイルシステムから
消え、``README.md`` の散文と ``skills/adr/SKILL.md`` のワークフローだけが残った。検知器の
無い規約は黙って壊れる（ADR-0023 決定 1 / ADR-0011 決定 6）ため、ここで機械照合する。

``ADR-NNNN`` はテストの docstring・エージェント定義・``CLAUDE.md`` から参照される識別子で
あり、重複・欠落・誤った所在は参照先の喪失に直結する。
"""

from __future__ import annotations

import re
from pathlib import Path

_ADR_DIR = Path(__file__).resolve().parents[3] / "docs" / "adr"
_TOPIC_RE = re.compile(r"^\d{2}-[a-z0-9-]+\.md$")
_SECTION_RE = re.compile(r"^## ADR-(\d{4}):", re.MULTILINE)
_INDEX_ROW_RE = re.compile(r"^\| (\d{4}) \|.*\[\d{2}\]\((\d{2}-[a-z0-9-]+\.md)\) \|$", re.MULTILINE)


def _topic_files() -> list[Path]:
    """``NN-<slug>.md`` 形式のトピックファイルを名前順で返す。"""
    return sorted(p for p in _ADR_DIR.glob("*.md") if _TOPIC_RE.match(p.name))


def _sections_by_number() -> dict[str, list[str]]:
    """``ADR-NNNN`` 番号から、その節を含むトピックファイル名のリストを返す。"""
    found: dict[str, list[str]] = {}
    for path in _topic_files():
        for number in _SECTION_RE.findall(path.read_text(encoding="utf-8")):
            found.setdefault(number, []).append(path.name)
    return found


def _index_rows() -> dict[str, str]:
    """README の「ADR 一覧」から ``ADR-NNNN`` → トピックファイル名の対応を返す。"""
    readme = (_ADR_DIR / "README.md").read_text(encoding="utf-8")
    return dict(_INDEX_ROW_RE.findall(readme))


def test_adr_numbers_are_unique_across_topic_files() -> None:
    """同じ ``ADR-NNNN`` 節が 2 箇所に存在しないこと。

    1 ファイル 1 決定なら重複はファイル名の衝突として現れたが、集約後は 2 つのトピック
    ファイルが同じ番号を名乗っても何も起きない。参照側はどちらが正か判断できなくなる。
    """
    duplicated = {num: files for num, files in _sections_by_number().items() if len(files) > 1}
    assert duplicated == {}, f"番号が重複している ADR: {duplicated}"


def test_adr_numbers_have_no_gaps() -> None:
    """採番が 0001 から連続していること（欠番は削除された決定の痕跡）。"""
    numbers = sorted(int(n) for n in _sections_by_number())
    assert numbers, "ADR 節が 1 つも見つからない"
    assert numbers == list(range(1, numbers[-1] + 1)), f"欠番がある: {numbers}"


def test_readme_index_matches_topic_file_sections() -> None:
    """README の「ADR 一覧」と、ディスク上の ``ADR-NNNN`` 節が過不足なく一致すること。

    節だけ足して README を更新しない（新しい決定が索引から見えない）、README の行だけ残って
    節が消えている（参照が空を指す）のどちらも検出する。
    """
    on_disk = set(_sections_by_number())
    in_index = set(_index_rows())
    assert on_disk == in_index, (
        f"README のみ={sorted(in_index - on_disk)} / トピックファイルのみ={sorted(on_disk - in_index)}"
    )


def test_readme_index_points_at_the_file_that_holds_the_section() -> None:
    """README 各行のファイルリンクが、その ``ADR-NNNN`` 節を実際に含むファイルを指すこと。

    節をトピック間で移動したのに索引を直し忘れると、リンクは切れずに別のトピックへ着地する
    （リンク切れ検査では捕まらない）。
    """
    sections = _sections_by_number()
    mismatched = {
        number: (linked, sections[number][0])
        for number, linked in _index_rows().items()
        if number in sections and linked != sections[number][0]
    }
    assert mismatched == {}, f"索引のリンク先が節の所在と食い違う {{番号: (索引, 実体)}}: {mismatched}"


def test_topic_table_lists_every_topic_file() -> None:
    """README の「トピック」表が、ディスク上のトピックファイルを過不足なく挙げること。"""
    readme = (_ADR_DIR / "README.md").read_text(encoding="utf-8")
    listed = set(re.findall(r"^\| \[(\d{2}-[a-z0-9-]+\.md)\]\(", readme, re.MULTILINE))
    actual = {p.name for p in _topic_files()}
    assert listed == actual, f"トピック表のみ={sorted(listed - actual)} / 実体のみ={sorted(actual - listed)}"
