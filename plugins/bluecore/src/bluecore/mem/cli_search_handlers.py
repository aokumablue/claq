"""mem CLI: search-related handlers and formatting helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bluecore.lib.slim_text import compact_line, first_meaningful_line

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from bluecore.mem.database import Database, MemoryChunk, SessionDigest
    from bluecore.mem.search import DigestSearchResult, SearchResult
    from bluecore.mem.settings import Settings

    OpenDbFn = Callable[[Settings], AbstractContextManager[Database]]
    GetProjectFn = Callable[[dict[str, Any]], str]
    CoerceIntFn = Callable[[object, int], int]


@dataclass(frozen=True)
class SearchDeps:
    """検索系ハンドラの外部依存（DB接続・プロジェクト解決・int変換・ロガー）。"""

    open_db: OpenDbFn
    get_project: GetProjectFn
    coerce_int: CoerceIntFn
    log: Any


def handle_search(
    settings: Settings,
    stdin_data: dict[str, Any],
    deps: SearchDeps,
) -> None:
    """mem 検索結果を JSON で返す"""
    from bluecore.mem.search import SearchService

    query = str(stdin_data.get("query", "") or "")
    if not query.strip():
        print(json.dumps({"results": []}))
        return

    project = stdin_data.get("project") or deps.get_project(stdin_data)
    limit = deps.coerce_int(stdin_data.get("limit"), default=20)

    try:
        with deps.open_db(settings) as db:
            svc = SearchService(db, settings)
            results = svc.search(query=query, project=project, limit=limit)
        print(json.dumps({"results": [r._asdict() for r in results]}))
    except Exception as e:
        deps.log.warning("検索失敗: %s", e)
        print(json.dumps({"results": [], "error": str(e)}))


def render_adaptive_context(db: Database, results: list[SearchResult], max_tokens: int = 400) -> str:
    """検索結果を <mem-context> タグでラップした Markdown 文字列を生成する。"""
    lines = ["<mem-context>", "# 関連メモリ（適応的注入）", ""]
    current_session = ""
    budget = max_tokens * 3.5

    for result in results:
        chunk = db.get_chunk_by_id(result.chunk_id)
        if chunk:
            if chunk.session_id != current_session:
                current_session = chunk.session_id
                ts = format_timestamp(chunk.created_at_epoch)
                lines.append(f"## {chunk.project} ({ts})")
                lines.append("")
            chunk_str = format_chunk(chunk)
        else:
            chunk_str = format_chunk_from_result(result)

        if budget - len(chunk_str) < 0:
            break
        lines.append(chunk_str)
        budget -= len(chunk_str)

    lines.append("</mem-context>")
    return "\n".join(lines)


def _format_digest_entry(digest: SessionDigest) -> str:
    """SessionDigest を「見出し + 要約」の簡潔なブロックとして整形する（render_digest_context 用）。

    context.py の `_format_digest` と見出し体裁を揃えるが、key_files / key_decisions は
    含めない簡潔形式にする。
    """
    date = datetime.fromtimestamp(digest.started_at_epoch, tz=UTC).strftime("%Y-%m-%d")
    return f"## 過去セッション: {digest.project} ({date}) [{digest.outcome}]\n\n**要約**: {digest.summary}\n"


def render_digest_context(results: list[DigestSearchResult], max_tokens: int = 150) -> str:
    """digest 検索結果を <mem-context> でラップした簡潔な Markdown 文字列に整形する。

    予算 max_tokens×3.5 文字を超えるエントリはスキップして後続を継続する
    （先頭が予算超過でも後続の収まるエントリは選択される greedy 継続方式）。
    結果が空、または1件も予算内に収まらない場合は空文字を返す。
    """
    if not results:
        return ""

    lines = ["<mem-context>", "# 関連する過去セッション", ""]
    budget = max_tokens * 3.5
    included = 0

    for result in results:
        entry = _format_digest_entry(result.digest)
        if len(entry) > budget:
            continue
        lines.append(entry)
        budget -= len(entry)
        included += 1

    if included == 0:
        return ""

    lines.append("</mem-context>")
    return "\n".join(lines)


def format_fields(
    user_prompt: str,
    tool_names: list[str],
    files_modified: list[str],
    content: str,
) -> str:
    """プロンプト・ツール・変更ファイル・本文を Markdown 形式にフォーマットする。"""
    parts: list[str] = []
    if user_prompt:
        parts.append(f"**プロンプト**: {slim_prompt(user_prompt)}")
    if tool_names:
        parts.append(f"**ツール**: {', '.join(tool_names)}")
    if files_modified:
        parts.append(f"**変更ファイル**: {', '.join(files_modified[:2])}")
    if content:
        parts.append(slim_context_content(content))
    parts.append("")
    return "\n".join(parts)


def format_chunk_from_result(result: SearchResult) -> str:
    """SearchResult をチャンクフォーマットに変換する（ローカル DB に該当チャンクが無い場合のフォールバック用）。"""
    return format_fields(result.user_prompt, result.tool_names, result.files_modified, result.content)


def format_chunk(chunk: MemoryChunk) -> str:
    """MemoryChunk をチャンクフォーマットに変換する。"""
    return format_fields(chunk.user_prompt, chunk.tool_names, chunk.files_modified, chunk.content)


def format_timestamp(epoch: int) -> str:
    """epoch 秒を `YYYY-MM-DD HH:MM`（UTC）に整形する。"""
    dt = datetime.fromtimestamp(epoch, tz=UTC)
    return dt.strftime("%Y-%m-%d %H:%M")


def truncate(text: str, max_len: int) -> str:
    """max_len を超える文字列を切り詰めて末尾に `...` を付ける。"""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def slim_prompt(text: str, max_len: int = 160) -> str:
    """会話調の前置きを落として、プロンプトを短く直接的に整える。"""
    line = first_meaningful_line(text)
    if line:
        return compact_line(line, max_len)

    in_code_block = False
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block and stripped:
            return compact_line(stripped, max_len)

    return ""


def slim_context_content(
    text: str,
    *,
    max_prose_lines: int = 6,
    max_prose_line_length: int = 160,
    max_code_lines: int = 20,
) -> str:
    """本文を圧縮しつつ、フェンス付きコードブロックは行数上限つきで残す。"""
    if not text:
        return ""

    lines: list[str] = []
    in_code_block = False
    prose_lines = 0
    code_lines = 0

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith("```"):
            in_code_block = not in_code_block
            if in_code_block:
                code_lines = 0
            lines.append(stripped)
            continue

        if in_code_block:
            if code_lines >= max_code_lines:
                if lines and lines[-1] != "...":
                    lines.append("...")
                continue
            # 1 行が極端に長いコードで注入予算を食い潰さないよう行長もクリップする
            lines.append(line[:max_prose_line_length])
            code_lines += 1
            continue

        if prose_lines >= max_prose_lines:
            if lines and lines[-1] != "...":
                lines.append("...")
            continue

        compacted = compact_line(line, max_prose_line_length)
        if compacted:
            lines.append(compacted)
            prose_lines += 1

    # 閉じフェンスなしで終端した場合は補完し、後続 Markdown の崩壊を防ぐ
    if in_code_block:
        lines.append("```")

    return "\n".join(lines)
