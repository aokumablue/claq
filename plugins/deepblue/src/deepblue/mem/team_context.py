"""team のコンテキスト注入ユーティリティ。

PostgreSQL 上のチーム共有チャンクを検索し、``<team-context>`` タグでラップされた
Markdown 文字列を生成する。検索は同じ ``team.pg_database.PgDatabase`` に対して
FTS のみの軽量モード（SessionStart 用）と、埋め込みを使うハイブリッドモード
（UserPromptSubmit 用）の 2 系統を提供する。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from devgear.mem.logger import get as _get_logger
from devgear.mem.pg_database import PgDatabase
from devgear.mem.settings import TeamSettings

log = _get_logger("TEAM_CONTEXT")


def build_team_context(
    pg: PgDatabase,
    query: str,
    exclude_origin_user: str,
    *,
    settings: TeamSettings,
    mode: Literal["fts", "hybrid"],
    embedding_model: str | None = None,
) -> str:
    """チーム共有チャンクから ``<team-context>`` 文字列を生成する。

    Args:
        pg: 接続済みの :class:`PgDatabase` インスタンス。
        query: 検索クエリ文字列。
        exclude_origin_user: 除外する origin_user（通常は自分の git user.name）。
        settings: ``TeamSettings``（``max_tokens`` / ``chunk_limit`` などを参照）。
        mode: ``"fts"`` なら pg_trgm のみ、``"hybrid"`` なら FTS + ベクトル検索の RRF。
        embedding_model: ``mode="hybrid"`` のときに使う埋め込みモデル名。

    Returns:
        生成された Markdown 文字列。該当チャンクが無い・クエリ空・エラー時は空文字列。
    """
    if not query.strip():
        return ""

    try:
        if mode == "hybrid":
            if not embedding_model:
                log.warning("hybrid モードには embedding_model が必要です。FTS フォールバック使用")
                ranked = pg.fts_search(
                    query,
                    limit=settings.chunk_limit,
                    exclude_origin_user=exclude_origin_user,
                )
            else:
                import devgear.mem.embedding as _emb

                embedding = _emb.embed_query(query, embedding_model)
                ranked = pg.team_search(
                    query,
                    embedding,
                    limit=settings.chunk_limit,
                    exclude_origin_user=exclude_origin_user,
                )
        else:
            ranked = pg.fts_search(
                query,
                limit=settings.chunk_limit,
                exclude_origin_user=exclude_origin_user,
            )
    except Exception as e:
        log.warning("チーム検索失敗: %s", e)
        return ""

    if not ranked:
        return ""

    chunk_ids = [cid for cid, _ in ranked]
    try:
        rows = pg.fetch_chunks_by_ids(chunk_ids)
    except Exception as e:
        log.warning("チームチャンク取得失敗: %s", e)
        return ""

    ordered_chunks = [rows[cid] for cid in chunk_ids if cid in rows]
    if not ordered_chunks:
        return ""

    selected = _select_within_budget(ordered_chunks, settings.max_tokens)
    if not selected:
        return ""

    lines: list[str] = [
        "<team-context>",
        "# チームメモリコンテキスト（自動注入）",
        "",
    ]
    for chunk in selected:
        lines.append(_format_chunk(chunk))
    lines.append("</team-context>")
    return "\n".join(lines)


def _select_within_budget(chunks: list[dict], max_tokens: int) -> list[dict]:
    """トークン予算内にチャンクを収める（1 トークン ≈ 3.5 文字の近似）。"""
    selected: list[dict] = []
    budget = max_tokens * 3.5
    for chunk in chunks:
        entry = _format_chunk(chunk)
        if len(entry) > budget:
            break
        selected.append(chunk)
        budget -= len(entry)
    return selected


def _format_chunk(chunk: dict) -> str:
    """チャンク辞書を Markdown セクションに整形する。"""
    author = chunk.get("origin_user", "") or "unknown"
    project = chunk.get("project", "") or "unknown"
    ts = _format_timestamp(int(chunk.get("created_at_epoch", 0) or 0))

    parts: list[str] = [f"## {project} (author: {author}, {ts})"]

    user_prompt = chunk.get("user_prompt", "")
    if user_prompt:
        parts.append(f"**プロンプト**: {_truncate(user_prompt, 160)}")

    tool_names = chunk.get("tool_names") or []
    if tool_names:
        parts.append(f"**ツール**: {', '.join(tool_names)}")

    files_modified = chunk.get("files_modified") or []
    if files_modified:
        parts.append(f"**変更ファイル**: {', '.join(files_modified[:3])}")

    content = chunk.get("content", "")
    if content:
        parts.append(f"```\n{_truncate(content, 280)}\n```")

    parts.append("")
    return "\n".join(parts)


def _format_timestamp(epoch: int) -> str:
    if epoch <= 0:
        return "unknown"
    try:
        dt = datetime.fromtimestamp(epoch, tz=UTC)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError):
        return "invalid"


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."
