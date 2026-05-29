"""mem CLI: search-related handlers and formatting helpers."""

from __future__ import annotations

import fnmatch
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from devgear.lib.slim_text import compact_line, first_meaningful_line

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from devgear.mem.database import Database, MemoryChunk
    from devgear.mem.search import SearchResult
    from devgear.mem.settings import Settings

    OpenDbFn = Callable[[Settings], AbstractContextManager[Database]]
    GetProjectFn = Callable[[dict[str, Any]], str]
    CoerceIntFn = Callable[[object, int], int]


def handle_search(
    settings: Settings,
    stdin_data: dict[str, Any],
    *,
    open_db: OpenDbFn,
    get_project: GetProjectFn,
    coerce_int: CoerceIntFn,
    log: Any,
) -> None:
    """mem 検索結果を JSON で返す"""
    from devgear.mem.search import SearchService

    query = str(stdin_data.get("query", "") or "")
    if not query.strip():
        print(json.dumps({"results": []}))
        return

    project = stdin_data.get("project") or get_project(stdin_data)
    limit = coerce_int(stdin_data.get("limit"), default=20)

    try:
        with open_db(settings) as db:
            svc = SearchService(db, settings)
            results = svc.search(query=query, project=project, limit=limit)
        print(json.dumps({"results": [r._asdict() for r in results]}))
    except Exception as e:
        log.warning("検索失敗: %s", e)
        print(json.dumps({"results": [], "error": str(e)}))


def handle_search_structured(
    settings: Settings,
    stdin_data: dict[str, Any],
    *,
    open_db: OpenDbFn,
    get_project: GetProjectFn,
    coerce_int: CoerceIntFn,
    log: Any,
) -> None:
    """構造化検索: tool_name, files, date_range フィルタをサポート"""
    from devgear.mem.search import SearchService

    query = str(stdin_data.get("query", "") or "")
    project = stdin_data.get("project") or get_project(stdin_data)
    limit = coerce_int(stdin_data.get("limit"), default=20)
    tool_filter = stdin_data.get("tool_name")
    file_pattern = stdin_data.get("file_pattern")
    date_from = stdin_data.get("date_from")
    date_to = stdin_data.get("date_to")

    try:
        with open_db(settings) as db:
            if query.strip():
                svc = SearchService(db, settings)
                candidate_ids = [r.chunk_id for r in svc.search(query=query, project=project, limit=limit * 3)]
            else:
                candidate_ids = [c.id for c in db.get_recent_chunks(limit=limit * 3, project=project) if c.id is not None]

            filtered = apply_structured_filters(db, candidate_ids, tool_filter, file_pattern, date_from, date_to)

            results = []
            for chunk_id in filtered[:limit]:
                chunk = db.get_chunk_by_id(chunk_id)
                if chunk:
                    results.append(
                        {
                            "chunk_id": chunk_id,
                            "content": chunk.content,
                            "user_prompt": chunk.user_prompt,
                            "project": chunk.project,
                            "created_at_epoch": chunk.created_at_epoch,
                            "tool_names": chunk.tool_names,
                            "files_read": chunk.files_read,
                            "files_modified": chunk.files_modified,
                        }
                    )

        print(json.dumps({"results": results, "total": len(results)}))
    except Exception as e:
        log.warning("構造化検索失敗: %s", e)
        print(json.dumps({"results": [], "error": str(e)}))


def apply_structured_filters(
    db: Database,
    candidate_ids: list[int],
    tool_filter: str | None,
    file_pattern: str | None,
    date_from: int | str | None,
    date_to: int | str | None,
) -> list[int]:
    """候補チャンクに構造化フィルタを適用"""
    if not candidate_ids:
        return []

    chunks = db.get_chunks_by_ids(candidate_ids)
    from_epoch = parse_date_to_epoch(date_from) if date_from else None
    to_epoch = parse_date_to_epoch(date_to) if date_to else None
    filtered = []

    for chunk_id in candidate_ids:
        chunk = chunks.get(chunk_id)
        if not chunk:
            continue
        if tool_filter and tool_filter not in chunk.tool_names:
            continue
        if file_pattern:
            all_files = chunk.files_read + chunk.files_modified
            if not any(fnmatch.fnmatch(f, file_pattern) for f in all_files):
                continue
        if from_epoch and chunk.created_at_epoch < from_epoch:
            continue
        if to_epoch and chunk.created_at_epoch > to_epoch:
            continue
        filtered.append(chunk_id)

    return filtered


def parse_date_to_epoch(value: int | str | None) -> int | None:
    """日付（epoch または ISO 8601）をエポックに変換"""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except (ValueError, AttributeError):
        return None


def merge_search_results_rrf(
    local_results: list[SearchResult],
    team_results: list[SearchResult],
    top_k: int = 3,
    k: int = 60,
) -> list[SearchResult]:
    """ローカルとチームの検索結果を RRF で統合して上位 top_k 件を返す。"""
    if not team_results:
        return local_results[:top_k]

    scores: dict[str, float] = {}
    result_map: dict[str, SearchResult] = {}

    for rank, result in enumerate(local_results):
        key = str(result.chunk_id)
        scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
        result_map[key] = result

    for rank, result in enumerate(team_results):
        key = f"team:{result.chunk_id}"
        scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
        result_map[key] = result

    sorted_keys = sorted(scores, key=lambda x: scores[x], reverse=True)
    return [result_map[kk] for kk in sorted_keys[:top_k]]


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
        parts.append(f"**変更ファイル**: {', '.join(files_modified[:5])}")
    if content:
        parts.append(slim_context_content(content))
    parts.append("")
    return "\n".join(parts)


def format_chunk_from_result(result: SearchResult) -> str:
    """SearchResult をチャンクフォーマットに変換する（team 検索結果用）。"""
    return format_fields(result.user_prompt, result.tool_names, result.files_modified, result.content)


def format_chunk(chunk: MemoryChunk) -> str:
    """MemoryChunk をチャンクフォーマットに変換する。"""
    return format_fields(chunk.user_prompt, chunk.tool_names, chunk.files_modified, chunk.content)


def format_timestamp(epoch: int) -> str:
    dt = datetime.fromtimestamp(epoch, tz=UTC)
    return dt.strftime("%Y-%m-%d %H:%M")


def truncate(text: str, max_len: int) -> str:
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


def slim_context_content(text: str, *, max_prose_lines: int = 6, max_prose_line_length: int = 160) -> str:
    """本文を圧縮しつつ、フェンス付きコードブロックはそのまま残す。"""
    if not text:
        return ""

    lines: list[str] = []
    in_code_block = False
    prose_lines = 0

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith("```"):
            in_code_block = not in_code_block
            lines.append(stripped)
            continue

        if in_code_block:
            lines.append(line)
            continue

        if prose_lines >= max_prose_lines:
            if lines and lines[-1] != "...":
                lines.append("...")
            continue

        compacted = compact_line(line, max_prose_line_length)
        if compacted:
            lines.append(compacted)
            prose_lines += 1

    return "\n".join(lines)
