"""``docs/adr/`` のトピックファイル構成とインデックスの整合を検証する構造テスト。

ファイル名は役割ごとの slug（連番なし）。README のトピック表とディスク上の
ファイルがずれても、検知器が無ければ黙って壊れる。
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ADR_DIR = _REPO_ROOT / "docs" / "adr"
_TOPIC_RE = re.compile(r"^[a-z][a-z0-9-]+\.md$")
_EXCLUDED_TOPIC_NAMES = {"readme.md", "template.md"}
_SERIAL_RE = re.compile(r"ADR-\d{4}")
_SKIP_DIR_NAMES = {".git", ".venv", "__pycache__", "node_modules"}
_SCAN_SUFFIXES = {".md", ".py", ".sh", ".cmd", ".json", ".toml", ".txt", ".yml", ".yaml"}


def _topic_files() -> list[Path]:
    """``<slug>.md`` 形式のトピックファイルを名前順で返す。"""
    return sorted(
        p for p in _ADR_DIR.glob("*.md") if _TOPIC_RE.match(p.name) and p.name.lower() not in _EXCLUDED_TOPIC_NAMES
    )


def test_topic_table_lists_every_topic_file() -> None:
    """README の「トピック」表が、ディスク上のトピックファイルを過不足なく挙げること。"""
    readme = (_ADR_DIR / "README.md").read_text(encoding="utf-8")
    listed = set(re.findall(r"^\| \[([a-z0-9-]+\.md)\]\(", readme, re.MULTILINE))
    actual = {p.name for p in _topic_files()}
    assert listed == actual, f"トピック表のみ={sorted(listed - actual)} / 実体のみ={sorted(actual - listed)}"


def test_topic_files_have_common_principles_and_a_decision_section() -> None:
    """各トピックファイルが「共通原則」と、それ以外の決定節を持つこと。"""
    missing: list[str] = []
    for path in _topic_files():
        headings = re.findall(r"^## (.+)$", path.read_text(encoding="utf-8"), re.MULTILINE)
        if "共通原則" not in headings:
            missing.append(f"{path.name}: 「共通原則」が無い")
        if all(h == "共通原則" for h in headings):
            missing.append(f"{path.name}: 決定節が無い")
    assert missing == [], "トピックファイルの見出し不足:\n" + "\n".join(missing)


def test_topic_files_are_self_contained() -> None:
    """各トピックファイルの本文が、他のトピックファイル名へ言及しないこと。

    トピックを分割・統合するたびに他ファイルへの言及が追従を要し、追従を忘れた言及から古びる。
    1 ファイルで完結させ、追従対象を無くす。
    """
    topics = {path: path.read_text(encoding="utf-8") for path in _topic_files()}
    violations = [
        f"{path.name}: 他トピック `{other.name}` へ言及している"
        for path, text in topics.items()
        for other in topics
        if other != path and other.name in text
    ]
    assert violations == [], "トピック間参照が残っている:\n" + "\n".join(violations)


def _serial_id_hits(path: Path) -> list[str]:
    """採番 ID を含む行を「相対パス:行番号: 本文」の形で返す（UTF-8 で読めないファイルは対象外）。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return []
    return [
        f"{path.relative_to(_REPO_ROOT)}:{number}: {line.strip()}"
        for number, line in enumerate(lines, 1)
        if _SERIAL_RE.search(line)
    ]


def test_repo_does_not_use_adr_serial_ids() -> None:
    """採番 ID をリポジトリへ残さないこと。参照はトピックファイル名で行う。"""
    hits = [
        hit
        for path in _REPO_ROOT.rglob("*")
        if path.is_file() and path.suffix.lower() in _SCAN_SUFFIXES and not _SKIP_DIR_NAMES.intersection(path.parts)
        for hit in _serial_id_hits(path)
    ]
    assert hits == [], "採番 ID が残っている:\n" + "\n".join(hits)
