"""SessionStart コンテキスト生成 — ティアード・メモリで過去のメモリを注入する"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from bluecore.mem.cli_search_handlers import slim_context_content, slim_prompt
from bluecore.mem.database import Database, MemoryChunk
from bluecore.mem.search import adaptive_decay
from bluecore.mem.settings import Settings


def importance_score(chunk: MemoryChunk) -> float:
    """ルールベースの重要度スコア（0.0〜1.0）を付与する"""
    scores = {}

    # (a) 情報密度: コンテンツ長
    scores["density"] = min(len(chunk.content) / 500, 1.0)

    # (b) アクション性: ファイル変更を伴うか
    scores["actionable"] = 1.0 if chunk.files_modified else 0.3

    # (c) ツール多様性: 複数ツール使用 = 複合的な作業
    scores["tool_diversity"] = min(len(chunk.tool_names) / 3, 1.0)

    # (d) アクセス頻度: 検索でヒットした回数
    scores["popularity"] = min(chunk.access_count / 5, 1.0)

    weights = {
        "density": 0.15,
        "actionable": 0.30,
        "tool_diversity": 0.15,
        "popularity": 0.40,
    }
    return sum(scores[k] * weights[k] for k in weights)


def _tier_chunks(
    scored: list[tuple[MemoryChunk, float]],
    hot_cutoff: float,
    warm_cutoff: float,
) -> tuple[list, list, list]:
    """スコア付きチャンクをホット・ウォーム・アーカイブの3層に分類する。"""
    hot = sorted(
        [(c, s) for c, s in scored if c.created_at_epoch >= hot_cutoff],
        key=lambda x: x[1],
        reverse=True,
    )
    hot_ids = {id(c) for c, _ in hot}
    warm = sorted(
        [(c, s) for c, s in scored if id(c) not in hot_ids and c.created_at_epoch >= warm_cutoff],
        key=lambda x: x[1],
        reverse=True,
    )
    hot_warm_ids = hot_ids | {id(c) for c, _ in warm}
    archive = sorted(
        [(c, s) for c, s in scored if id(c) not in hot_warm_ids],
        key=lambda x: x[1],
        reverse=True,
    )
    return hot, warm, archive


def _render_context_lines(selected: list[MemoryChunk]) -> str:
    """選択済みチャンクリストを mem-context XML 文字列に整形する。"""
    lines: list[str] = ["<mem-context>", "# メモリコンテキスト（自動注入）", ""]
    current_session = ""
    for chunk in selected:
        if chunk.session_id != current_session:
            current_session = chunk.session_id
            ts = _format_timestamp(chunk.created_at_epoch)
            lines.append(f"## セッション: {chunk.project} ({ts})")
            lines.append("")
        lines.append(_format_chunk(chunk))
    lines.append("</mem-context>")
    return "\n".join(lines)


def build_context(
    db: Database,
    settings: Settings,
    project: str | None = None,
) -> str:
    """ティアード・メモリでコンテキスト文字列を生成する。

    Layer 1 (ホット): 直近 N 時間のチャンク — 即時性の高い作業コンテキスト
    Layer 2 (ウォーム): 過去 N 日のアクセス頻度上位 — 定着した知識
    """
    chunks = db.get_recent_chunks(limit=settings.context_chunk_count, project=project)
    if not chunks:
        return ""

    now = time.time()
    scored = [
        (
            c,
            importance_score(c) * adaptive_decay(
                c.created_at_epoch,
                c.last_accessed_epoch,
                c.access_count,
                base_half_life=settings.search_half_life_days,
            ),
        )
        for c in chunks
    ]

    hot, warm, archive = _tier_chunks(
        scored,
        now - settings.context_hot_hours * 3600,
        now - settings.context_warm_days * 86400,
    )

    hot_selected = _select_within_budget(hot, settings.context_hot_tokens)
    warm_selected = _select_within_budget(warm, settings.context_warm_tokens)
    remaining = settings.context_max_tokens - settings.context_hot_tokens - settings.context_warm_tokens
    archive_selected = _select_within_budget(archive, max(remaining, 0)) if remaining > 0 else []

    selected = hot_selected + warm_selected + archive_selected
    if not selected:
        return ""

    selected.sort(key=lambda c: c.created_at_epoch)
    return _render_context_lines(selected)


def _select_within_budget(
    scored: list[tuple[MemoryChunk, float]],
    max_tokens: int,
) -> list[MemoryChunk]:
    """トークン予算内でチャンクを選択する"""
    selected: list[MemoryChunk] = []
    budget = max_tokens * 3.5
    for chunk, _score in scored:
        entry = _format_chunk(chunk)
        if len(entry) > budget:
            continue
        selected.append(chunk)
        budget -= len(entry)
    return selected


def _format_chunk(chunk: MemoryChunk) -> str:
    """チャンクを文字列にフォーマットする"""
    parts: list[str] = []

    if chunk.user_prompt:
        parts.append(f"**プロンプト**: {slim_prompt(chunk.user_prompt)}")

    if chunk.tool_names:
        parts.append(f"**ツール**: {', '.join(chunk.tool_names)}")

    if chunk.files_modified:
        parts.append(f"**変更ファイル**: {', '.join(chunk.files_modified[:2])}")

    if chunk.content:
        parts.append(f"```\n{slim_context_content(chunk.content)}\n```")

    parts.append("")
    return "\n".join(parts)


def _format_timestamp(epoch: int) -> str:
    """epoch 秒を `YYYY-MM-DD HH:MM`（UTC）に整形する。"""
    dt = datetime.fromtimestamp(epoch, tz=UTC)
    return dt.strftime("%Y-%m-%d %H:%M")
