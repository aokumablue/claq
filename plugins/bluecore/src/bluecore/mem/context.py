"""SessionStart コンテキスト生成 — 2層メモリ（hot 生チャンク + digest 要約）で過去のメモリを注入する"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from bluecore.mem.cli_search_handlers import slim_context_content, slim_prompt
from bluecore.mem.database import Database, MemoryChunk, SessionDigest
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


def _filter_hot_chunks(
    scored: list[tuple[MemoryChunk, float]],
    hot_cutoff: float,
) -> list[tuple[MemoryChunk, float]]:
    """スコア付きチャンクから直近 hot_cutoff 以降のみを抽出し、スコア降順で返す。"""
    return sorted(
        [(c, s) for c, s in scored if c.created_at_epoch >= hot_cutoff],
        key=lambda x: x[1],
        reverse=True,
    )


def _select_digests_within_budget(
    digests: list[SessionDigest],
    max_tokens: int,
) -> list[SessionDigest]:
    """トークン予算内でセッション要約を選択する（生チャンクと同じ文字予算方式）。"""
    selected: list[SessionDigest] = []
    budget = max_tokens * 3.5
    for digest in digests:
        entry = _format_digest(digest)
        if len(entry) > budget:
            continue
        selected.append(digest)
        budget -= len(entry)
    return selected


def _render_context_lines(
    digest_selected: list[SessionDigest],
    hot_selected: list[MemoryChunk],
) -> str:
    """digest 層 + hot 層の選択済みリストを mem-context XML 文字列に整形する。"""
    lines: list[str] = ["<mem-context>", "# メモリコンテキスト（自動注入）", ""]
    for digest in digest_selected:
        lines.append(_format_digest(digest))
    current_session = ""
    for chunk in hot_selected:
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
    """2層メモリ（hot + digest）でコンテキスト文字列を生成する。

    Layer 1 (ホット): 直近 hot_hours 時間の生チャンク — 即時性の高い作業コンテキスト。
    Layer 2 (ダイジェスト): session_digests の要約 — 長期の文脈。
    hot 層に採用されたセッションの digest は重複注入防止のため除外する。
    """
    chunks = db.get_recent_chunks(limit=settings.context_chunk_count, project=project)

    hot_selected: list[MemoryChunk] = []
    if chunks:
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
        hot = _filter_hot_chunks(scored, now - settings.context_hot_hours * 3600)
        hot_selected = _select_within_budget(hot, settings.context_hot_tokens)

    hot_session_ids = {c.session_id for c in hot_selected}
    digests = [
        d
        for d in db.get_recent_digests(project=project, limit=settings.context_digest_count)
        if d.session_id not in hot_session_ids
    ]
    digest_selected = _select_digests_within_budget(digests, settings.context_digest_tokens)

    if not hot_selected and not digest_selected:
        return ""

    hot_selected.sort(key=lambda c: c.created_at_epoch)
    digest_selected.sort(key=lambda d: d.created_at_epoch)

    return _render_context_lines(digest_selected, hot_selected)


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


def _format_date(epoch: int) -> str:
    """epoch 秒を `YYYY-MM-DD`（UTC）に整形する。"""
    dt = datetime.fromtimestamp(epoch, tz=UTC)
    return dt.strftime("%Y-%m-%d")


def _format_digest(digest: SessionDigest) -> str:
    """SessionDigest を mem-context 内の1ブロックとして整形する。

    key_files / key_decisions が空の場合は該当行を省略する。
    """
    ts = _format_date(digest.started_at_epoch)
    parts: list[str] = [
        f"## 過去セッション: {digest.project} ({ts}) [{digest.outcome}]",
        "",
        f"**要約**: {digest.summary}",
    ]

    if digest.key_files:
        parts.append(f"**変更**: {', '.join(digest.key_files[:3])}")

    if digest.key_decisions:
        parts.append(f"**論点**: {' / '.join(digest.key_decisions[:2])}")

    parts.append("")
    return "\n".join(parts)
