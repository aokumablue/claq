"""mem CLI: search-related handlers and formatting helpers."""

from __future__ import annotations

import fnmatch
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


@dataclass(frozen=True)
class StructuredFilter:
    """構造化検索のフィルタ条件（ツール名・ファイルパターン・日付範囲）。"""

    tool_filter: str | None
    file_pattern: str | None
    date_from: int | str | None
    date_to: int | str | None


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


def _get_candidate_ids(db: Any, settings: Any, query: str, project: Any, limit: int) -> list:
    """クエリがあれば FTS 検索、なければ最近のチャンクから候補 ID を返す。"""
    from bluecore.mem.search import SearchService

    if query.strip():
        svc = SearchService(db, settings)
        return [r.chunk_id for r in svc.search(query=query, project=project, limit=limit * 3)]
    return [c.id for c in db.get_recent_chunks(limit=limit * 3, project=project) if c.id is not None]


def _build_chunk_result(db: Any, chunk_id: str) -> dict | None:
    """chunk_id からチャンク辞書を構築して返す。存在しない場合は None。"""
    chunk = db.get_chunk_by_id(chunk_id)
    if chunk is None:
        return None
    return {
        "chunk_id": chunk_id,
        "content": chunk.content,
        "user_prompt": chunk.user_prompt,
        "project": chunk.project,
        "created_at_epoch": chunk.created_at_epoch,
        "tool_names": chunk.tool_names,
        "files_read": chunk.files_read,
        "files_modified": chunk.files_modified,
    }


def handle_search_structured(
    settings: Settings,
    stdin_data: dict[str, Any],
    deps: SearchDeps,
) -> None:
    """構造化検索: tool_name, files, date_range フィルタをサポート"""
    query = str(stdin_data.get("query", "") or "")
    project = stdin_data.get("project") or deps.get_project(stdin_data)
    limit = deps.coerce_int(stdin_data.get("limit"), default=20)
    filt = StructuredFilter(
        tool_filter=stdin_data.get("tool_name"),
        file_pattern=stdin_data.get("file_pattern"),
        date_from=stdin_data.get("date_from"),
        date_to=stdin_data.get("date_to"),
    )

    try:
        with deps.open_db(settings) as db:
            candidate_ids = _get_candidate_ids(db, settings, query, project, limit)
            filtered = apply_structured_filters(db, candidate_ids, filt)
            results = [r for cid in filtered[:limit] if (r := _build_chunk_result(db, cid)) is not None]
        print(json.dumps({"results": results, "total": len(results)}))
    except Exception as e:
        deps.log.warning("構造化検索失敗: %s", e)
        print(json.dumps({"results": [], "error": str(e)}))


def apply_structured_filters(
    db: Database,
    candidate_ids: list[int],
    filt: StructuredFilter,
) -> list[int]:
    """候補チャンクに構造化フィルタを適用"""
    if not candidate_ids:
        return []

    chunks = db.get_chunks_by_ids(candidate_ids)
    from_epoch = parse_date_to_epoch(filt.date_from) if filt.date_from else None
    to_epoch = parse_date_to_epoch(filt.date_to) if filt.date_to else None
    filtered = []

    for chunk_id in candidate_ids:
        chunk = chunks.get(chunk_id)
        if not chunk:
            continue
        if filt.tool_filter and filt.tool_filter not in chunk.tool_names:
            continue
        if filt.file_pattern:
            all_files = chunk.files_read + chunk.files_modified
            if not any(fnmatch.fnmatch(f, filt.file_pattern) for f in all_files):
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
    """SearchResult をチャンクフォーマットに変換する（team 検索結果用）。"""
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
