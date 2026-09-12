"""実 transcript を走査し、足場タグ denylist から漏れたタグを検出する診断。

``lib/harness.py`` の ``_SCAFFOLD_TAGS`` は denylist であり、列挙漏れは「素通り」
として現れる。ホストが新しい足場タグを追加しても、テストも CI も緑のまま
引き継ぎがノイズで埋まる（ADR-0015 の残存リスク筆頭）。実際に本リポジトリでは
初回の足場漏れと ``agent-message`` の 2 回、この形で実害が出ている。

列挙漏れは定義上「まだ知らないもの」なので、列挙の完全性を静的に検証することは
できない。代わりに実データを見て「知らないタグが来ていないか」を報告する。

判定は除去処理を**通した後**のテキストに対して行う。生テキストを見ると、
``<task-notification>`` のように denylist が既に中身ごと落としているブロックの
内側タグ（``<status>`` ``<task-id>`` ``<usage>`` 等）まで「未知」として並び、
本当の漏れが埋もれる。ここで知りたいのは「除去を通り抜けて残ったもの」だけ。

通すのは**除去だけ**で、無害化（``strip_tags``）は通さない。無害化は細工を
検出すると ``&`` と ``<`` を全て倒す、あるいは本文を ``[REDACTED]`` へ倒すため、
そのメッセージ内の未知タグが 1 つも見えなくなる（実測）。ドリフト診断が最も
見たいのはまさにその種のメッセージであり、ここで見落とすと ADR-0016 の
「素通り」がそのまま残る。

**判定は「対を成す未知タグ」に限る。** ユーザーの依頼本文には
``<key>`` ``<yyyy-mm-dd>`` ``<path>`` のようなプレースホルダが裸で現れるが、
ハーネスが生成する足場は必ず開始と終了が対になっている。実 transcript 273 本での
実測では、未知タグ全件だと 41 種（うち 40 種が誤検知）、対を成すものだけに絞ると
1 種（誤検知 0、真の漏れ ``agent-message`` を検出）だった。

**コードスパン（``` フェンスとバッククォート）は判定前に落とす。** 依頼本文が
コード片や正規表現を引用すると、その中の ``<document>`` ``<crm>`` ``<tag[^>]*>``
がそのまま「対を成す未知タグ」になる。実測では残存 3 種すべてがこの形だった。
ホストの足場はメッセージ直下に生で載るものであり、利用者が打ったコードフェンスの
内側には現れない。フェンスが閉じていない場合はテキストを一切触らない（ADR-0015 で
閉じた「終端の来ない入力を EOF まで飲み込む」型を隣のモジュールで再現しないため）。

CI へは組み込まない。コーパスは各利用者のローカルにしか無く、コミットできない
（未サニタイズかつユーザー固有）。人間が ``maintain`` 等から回す診断として置く。
終了コードは ``0`` = ドリフトなし / ``1`` = ドリフト検出 / ``2`` = 走査対象ゼロ。
``2`` を ``0`` と区別するのは、走査できなかったことを「異常なし」と読ませないため
（ADR-0014 の「skip されるゲートはゲートとして機能しない」と同じ理由）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable, Iterator
from pathlib import Path

from claq.lib.harness import COMMAND_TAGS, SCAFFOLD_TAGS, normalize_user_message
from claq.mem.handoff import TRANSCRIPT_MAX_BYTES, is_trusted_transcript, trusted_transcript_roots
from claq.mem.tag_stripping import STRIPPED_TAGS, drop_known_tag_blocks

# 依頼本文に現れても足場ではないタグ。ここが古びると「誤検知 1 件」として
# 現れ、語を 1 つ足せば解消する。denylist の陳腐化（無言の素通り）と保守量は
# 同じで、失敗の向きだけが反転している——これが本診断の中心的な設計判断。
BENIGN_TAGS: frozenset[str] = frozenset(
    {
        "a", "b", "br", "code", "details", "div", "em", "h1", "h2", "h3",
        "hr", "i", "img", "li", "ol", "p", "pre", "s", "span", "strong",
        "summary", "table", "tbody", "td", "th", "thead", "tr", "ul",
    }
)

# 走査する transcript の件数上限（更新時刻の新しい順）。
DEFAULT_SCAN_LIMIT = 50

# 報告するタグ名の上限。
_REPORT_LIMIT = 20

_TAG_PATTERN = re.compile(r"<(/?)([A-Za-z][\w:-]*)[^>]{0,512}>")

# ``` / ~~~ フェンス。開始と同じ記号列で閉じている対のみを対象にし、内側に
# フェンス行を含まないことを否定先読みで要求する（未閉のフェンスが N 個並んでも
# 走査が EOF まで伸びない）。閉じが無ければマッチせず、本文はそのまま残る。
_FENCE_BLOCK = re.compile(
    r"^[ \t]*(`{3,}|~{3,})[^\n]*\n(?:(?!^[ \t]*\1)[\s\S])*?^[ \t]*\1[ \t]*$",
    re.MULTILINE,
)

# バッククォート 1 個で囲む行内コード。改行を跨がせない。
_INLINE_CODE = re.compile(r"`[^`\n]{1,512}`")


def strip_code_spans(text: str) -> str:
    """コードフェンスと行内コードを取り除く。

    依頼本文がコード片を引用しただけのタグを足場ドリフトとして数えないため。

    Args:
        text: 足場除去を通した後のユーザー発話。

    Returns:
        コードスパンを取り除いたテキスト。

    Raises:
        例外は発生しません。
    """
    return _INLINE_CODE.sub("", _FENCE_BLOCK.sub("", text))


EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_NOTHING_SCANNED = 2


def known_tags() -> frozenset[str]:
    """足場・信頼境界マーカー・良性タグを合わせた既知タグ名の集合を返す。

    値は各定義モジュールから導出する。写経すると片方だけ更新されて誤報が出る。

    Returns:
        小文字化した既知タグ名の集合。

    Raises:
        例外は発生しません。
    """
    names = set(SCAFFOLD_TAGS) | set(COMMAND_TAGS) | set(STRIPPED_TAGS) | set(BENIGN_TAGS)
    return frozenset(name.lower() for name in names)


def find_paired_unknown_tags(text: str, known: frozenset[str]) -> set[str]:
    """開始と終了が対になっている未知タグ名を返す。

    裸のプレースホルダ（``<key>`` 等）を拾わないため、開始タグと閉じタグの
    両方が現れた名前だけを対象にする。

    Args:
        text: ユーザー発話として transcript に載っていた生テキスト。
        known: 既知タグ名の集合（小文字）。

    Returns:
        対を成していて既知でないタグ名の集合。

    Raises:
        例外は発生しません。
    """
    opened: set[str] = set()
    closed: set[str] = set()
    for is_closing, name in _TAG_PATTERN.findall(text):
        (closed if is_closing else opened).add(name.lower())
    return (opened & closed) - known


def _user_texts(raw: str) -> Iterator[str]:
    """transcript 本文から user エントリのテキストを取り出す。

    Args:
        raw: JSONL 形式の transcript 本文。

    Yields:
        user エントリのプレーンテキスト。
    """
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        message = entry.get("message")
        message = message if isinstance(message, dict) else {}
        if "user" not in (entry.get("type"), entry.get("role"), message.get("role")):
            continue
        content = message.get("content") or entry.get("content")
        if isinstance(content, str):
            yield content
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    yield part["text"]


def iter_transcripts(roots: Iterable[Path], limit: int) -> list[Path]:
    """走査対象の transcript を更新時刻の新しい順に集める。

    Args:
        roots: 探索する root ディレクトリ。
        limit: 返す最大件数。

    Returns:
        信頼判定を通った transcript のパス一覧。

    Raises:
        例外は発生しません。
    """
    found: list[tuple[float, Path]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.jsonl"):
            if not is_trusted_transcript(path):
                continue
            try:
                found.append((path.stat().st_mtime, path))
            except OSError:
                continue
    found.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in found[:limit]]


def scan(paths: Iterable[Path]) -> Counter[str]:
    """transcript 群を走査し、未知タグ名ごとの出現ファイル数を数える。

    Args:
        paths: 走査対象の transcript。

    Returns:
        未知タグ名 → それが現れたファイル数の Counter。

    Raises:
        例外は発生しません。
    """
    known = known_tags()
    counts: Counter[str] = Counter()
    for path in paths:
        try:
            with path.open("rb") as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - TRANSCRIPT_MAX_BYTES))
                raw = stream.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        names: set[str] = set()
        for text in _user_texts(raw):
            cleaned = strip_code_spans(drop_known_tag_blocks(normalize_user_message(text)))
            names |= find_paired_unknown_tags(cleaned, known)
        counts.update(names)
    return counts


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """コマンドライン引数を解析する。

    Args:
        argv: コマンド名を除いた引数リスト。

    Returns:
        解析済みの引数。

    Raises:
        SystemExit: 引数が不正な場合（argparse の既定動作）。
    """
    parser = argparse.ArgumentParser(
        description="実 transcript を走査し、足場タグ denylist の漏れを検出する。",
    )
    parser.add_argument("--root", action="append", default=[], help="走査する root（既定: 既知 host の transcript root）")
    parser.add_argument("--limit", type=int, default=DEFAULT_SCAN_LIMIT, help="走査する transcript の最大件数")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI エントリポイント。

    Args:
        argv: コマンド名を除いた引数リスト。``None`` なら ``sys.argv`` を使う。

    Returns:
        0 = ドリフトなし / 1 = ドリフト検出 / 2 = 走査対象ゼロ。

    Raises:
        例外は発生しません。
    """
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    roots = [Path(root).expanduser() for root in args.root] or list(trusted_transcript_roots())
    paths = iter_transcripts(roots, args.limit)
    if not paths:
        print("走査できる transcript がありません（ドリフトなしとは区別されます）", file=sys.stderr)
        return EXIT_NOTHING_SCANNED

    counts = scan(paths)
    if not counts:
        print(f"{len(paths)} 本を走査: 未知の足場タグはありません")
        return EXIT_OK

    print(f"{len(paths)} 本を走査: 未知の足場タグを検出しました", file=sys.stderr)
    for name, files in counts.most_common(_REPORT_LIMIT):
        print(f"  {name}: {files} ファイル", file=sys.stderr)
    print(
        "足場なら lib/harness.py の _SCAFFOLD_TAGS へ、依頼本文に現れる良性タグなら"
        " ci/scan_scaffold_drift.py の BENIGN_TAGS へ追加してください。",
        file=sys.stderr,
    )
    return EXIT_DRIFT


if __name__ == "__main__":
    raise SystemExit(main())
