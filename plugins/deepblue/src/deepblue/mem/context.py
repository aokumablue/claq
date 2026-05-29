"""SessionStart コンテキスト生成 — ティアード・メモリで過去のメモリを注入する"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from devgear.mem.database import Database, MemoryChunk
from devgear.mem.search import adaptive_decay
from devgear.mem.settings import Settings


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


def build_context(
    db: Database,
    settings: Settings,
    project: str | None = None,
) -> str:
    """ティアード・メモリでコンテキスト文字列を生成する。

    Layer 1 (ホット): 直近 N 時間のチャンク — 即時性の高い作業コンテキスト
    Layer 2 (ウォーム): 過去 N 日のアクセス頻度上位 — 定着した知識
    """
    chunks = db.get_recent_chunks(
        limit=settings.context_chunk_count,
        project=project,
    )
    if not chunks:
        return ""

    now = time.time()
    hot_cutoff = now - settings.context_hot_hours * 3600
    warm_cutoff = now - settings.context_warm_days * 86400

    # チャンクをスコアリング
    scored = [
        (
            c,
            importance_score(c)
            * adaptive_decay(
                c.created_at_epoch,
                c.last_accessed_epoch,
                c.access_count,
                base_half_life=settings.search_half_life_days,
            ),
        )
        for c in chunks
    ]

    # Layer 1: ホット — 直近のチャンク
    hot = [(c, s) for c, s in scored if c.created_at_epoch >= hot_cutoff]
    hot.sort(key=lambda x: x[1], reverse=True)

    # Layer 2: ウォーム — ホットに含まれず、期間内で頻度上位
    hot_ids = {id(c) for c, _ in hot}
    warm = [(c, s) for c, s in scored if id(c) not in hot_ids and c.created_at_epoch >= warm_cutoff]
    warm.sort(key=lambda x: x[1], reverse=True)

    # Layer 3: アーカイブ — どちらにも当てはまらないが、全体予算から選択
    hot_warm_ids = hot_ids | {id(c) for c, _ in warm}
    archive = [(c, s) for c, s in scored if id(c) not in hot_warm_ids]
    archive.sort(key=lambda x: x[1], reverse=True)

    # トークン予算で選択（1トークン ≈ 3.5文字で近似）
    hot_selected = _select_within_budget(hot, settings.context_hot_tokens)
    warm_selected = _select_within_budget(warm, settings.context_warm_tokens)
    # アーカイブは残り予算を使用
    remaining = settings.context_max_tokens - settings.context_hot_tokens - settings.context_warm_tokens
    archive_selected = _select_within_budget(archive, max(remaining, 0)) if remaining > 0 else []

    selected = hot_selected + warm_selected + archive_selected
    if not selected:
        return ""

    # 古い順にソート（時系列で表示）
    selected.sort(key=lambda c: c.created_at_epoch)

    lines: list[str] = []
    lines.append("<mem-context>")
    lines.append("# メモリコンテキスト（自動注入）")
    lines.append("")

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
            break
        selected.append(chunk)
        budget -= len(entry)
    return selected


def _format_chunk(chunk: MemoryChunk) -> str:
    """チャンクを文字列にフォーマットする"""
    parts: list[str] = []

    if chunk.user_prompt:
        parts.append(f"**プロンプト**: {_truncate(chunk.user_prompt, 120)}")

    if chunk.tool_names:
        parts.append(f"**ツール**: {', '.join(chunk.tool_names)}")

    if chunk.files_modified:
        parts.append(f"**変更ファイル**: {', '.join(chunk.files_modified[:3])}")

    if chunk.content:
        parts.append(f"```\n{_truncate(chunk.content, 300)}\n```")

    parts.append("")
    return "\n".join(parts)


def _format_timestamp(epoch: int) -> str:
    dt = datetime.fromtimestamp(epoch, tz=UTC)
    return dt.strftime("%Y-%m-%d %H:%M")


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."
