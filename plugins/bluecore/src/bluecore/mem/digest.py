"""セッション要約（session digest）の生成ロジック。

チャンク・インタラクションログ・トランスクリプトから短期記憶を圧縮した
`SessionDigest` を組み立て、DB に保存する。hooks 側の
`hooks/session_end.py` とは独立実装であり、hooks → mem の依存方向を
維持するためインポートしない。
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bluecore.lib.harness import detect_harness
from bluecore.lib.slim_text import compact_line
from bluecore.mem.models import MemoryChunk, SessionDigest
from bluecore.mem.redaction import redact

if TYPE_CHECKING:
    from bluecore.mem.models import InteractionLog

# トランスクリプト読み込み上限（20MB 超はスキップしてメモリ枯渇を防ぐ）
_MAX_TRANSCRIPT_BYTES = 20 * 1024 * 1024

# 既知の実行ステータス（outcome 集約の分母に使う）
_KNOWN_STATUSES = ("success", "partial", "failure")

# get_interaction_logs のデフォルト limit=100 だと長セッション（100件超）で
# 末尾のインタラクションが取得できず「先頭1+末尾4」の意味論が壊れるため、
# build_session_digest からは十分大きい limit を明示指定する。
_INTERACTION_LOGS_LIMIT = 10000


def _extract_claude_style_text(entry: dict) -> str:
    """claude/codex 形式のトランスクリプトエントリからテキストを抽出する。

    `type == "assistant"` かつ `message.content` 内の text ブロックを
    連結して返す。対象外エントリや空ブロックのみの場合は空文字列。
    """
    if entry.get("type") != "assistant":
        return ""
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return ""
    texts = [
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return " ".join(t for t in texts if t).strip()


def _extract_copilot_style_text(entry: dict) -> str:
    """copilot 形式のトランスクリプトエントリからテキストを抽出する。

    `type == "assistant.message"` の `data.content` を返す。
    対象外エントリや非文字列の場合は空文字列。
    """
    if entry.get("type") != "assistant.message":
        return ""
    data = entry.get("data")
    content = data.get("content") if isinstance(data, dict) else None
    return content.strip() if isinstance(content, str) else ""


def _extract_entry_text(entry: dict, harness: str) -> str:
    """ハーネス種別に応じたエントリテキスト抽出関数へディスパッチする。"""
    if harness == "copilot":
        return _extract_copilot_style_text(entry)
    return _extract_claude_style_text(entry)


def _extract_final_assistant_text(transcript_path: Path, harness: str) -> str | None:
    """トランスクリプト（JSONL）から最後の非空アシスタント応答テキストを抽出する。

    Args:
        transcript_path: トランスクリプトファイルのパス。
        harness: "claude" / "codex" / "copilot" / "unknown"。
            copilot のみ専用形式、それ以外は claude/codex 共通形式で解析する。

    Returns:
        最後に見つかった非空アシスタント応答テキスト。ファイル不在・
        20MB 超過・アシスタント応答が1件も無い場合は None。

    Raises:
        例外は発生しません（JSON デコードエラーの行はスキップする）。
    """
    try:
        if not transcript_path.exists():
            return None
        if transcript_path.stat().st_size > _MAX_TRANSCRIPT_BYTES:
            return None
    except OSError:
        return None

    last_text: str | None = None
    with transcript_path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            text = _extract_entry_text(entry, harness)
            if text:
                last_text = text
    return last_text


@dataclass(frozen=True)
class ChunkAggregate:
    """_aggregate_chunks の集約結果。"""

    key_files: list[str]
    key_decisions: list[str]
    outcome: str


def _aggregate_key_files(chunks: list[MemoryChunk]) -> list[str]:
    """全チャンクの files_modified を集計し、上位10件（count降順・path昇順）を redact して返す。

    ファイルパスに秘密情報が混入する可能性は低いが、念のため PII/シークレット
    マスキング（``redaction.redact``）を適用してから返す（DB 保存・PG 同期・
    コンテキスト再注入に晒されるため）。
    """
    counter: Counter[str] = Counter()
    for chunk in chunks:
        counter.update(chunk.files_modified)
    ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return [redact(path) for path, _ in ranked[:10]]


def _aggregate_key_decisions(chunks: list[MemoryChunk], interaction_logs: list[InteractionLog]) -> list[str]:
    """ユーザープロンプトを重複排除し、先頭1件+末尾4件（最大5件・redact 後に120字切り詰め）を返す。

    interaction_logs の ``user_prompt_full`` は無 redact の生プロンプトのため、
    切り詰め（``compact_line``）より前に ``redaction.redact`` を適用する
    （切り詰め後だと秘密情報の一部が漏れたりマッチを逃したりし得るため）。
    """
    prompts = [log_entry.user_prompt_full for log_entry in interaction_logs if log_entry.user_prompt_full]
    if not prompts:
        prompts = [chunk.user_prompt for chunk in chunks if chunk.user_prompt]

    deduped = list(dict.fromkeys(prompts))
    if len(deduped) <= 5:
        selected = deduped
    else:
        selected = [deduped[0], *deduped[-4:]]
    return [compact_line(redact(prompt), 120) for prompt in selected]


def _aggregate_outcome(chunks: list[MemoryChunk]) -> str:
    """execution_status を集約して 'success'|'partial'|'failure'|'unknown' を返す。

    known = 既知ステータス（success/partial/failure）のチャンク数。
    failure_score = failure を1、partial を0.5として合計。
    failure_score / known >= 0.5 なら 'failure'、> 0 なら 'partial'、
    known が 0 なら 'unknown'、それ以外は 'success'。
    """
    known = [c.execution_status for c in chunks if c.execution_status in _KNOWN_STATUSES]
    if not known:
        return "unknown"
    failure_score = sum(1.0 if s == "failure" else 0.5 if s == "partial" else 0.0 for s in known)
    ratio = failure_score / len(known)
    if ratio >= 0.5:
        return "failure"
    if ratio > 0:
        return "partial"
    return "success"


def _aggregate_chunks(chunks: list[MemoryChunk], interaction_logs: list[InteractionLog]) -> ChunkAggregate:
    """チャンクとインタラクションログから key_files / key_decisions / outcome を集約する。"""
    return ChunkAggregate(
        key_files=_aggregate_key_files(chunks),
        key_decisions=_aggregate_key_decisions(chunks, interaction_logs),
        outcome=_aggregate_outcome(chunks),
    )


def _compact_whitespace(text: str) -> str:
    """水平空白の連続と連続改行を圧縮する（単一の改行は文境界として保持する）。"""
    compact = re.sub(r"[ \t\r\f\v]+", " ", text.strip())
    compact = re.sub(r" *\n *", "\n", compact)
    return re.sub(r"\n{2,}", "\n", compact)


def _truncate_summary(text: str, max_chars: int = 500) -> str:
    """空白を圧縮し、max_chars 字以内に丸める。

    圧縮後の文字数が max_chars を超える場合、末尾側の (max_chars - 100)
    〜 max_chars の範囲に文境界（「。」または改行）があればそこで切る
    （「。」は含めて残し、改行は除去する。省略記号なし）。
    境界が無ければ max_chars 文字で切って「…」を付与する。
    """
    compact = _compact_whitespace(text)
    if len(compact) <= max_chars:
        return compact

    window_start = max(0, max_chars - 100)
    window = compact[window_start:max_chars]
    period_pos = window.rfind("。")
    newline_pos = window.rfind("\n")
    if period_pos == -1 and newline_pos == -1:
        return compact[:max_chars] + "…"
    if period_pos >= newline_pos:
        cut = window_start + period_pos + 1
    else:
        cut = window_start + newline_pos
    return compact[:cut].rstrip()


def _top_tool_names(chunks: list[MemoryChunk], limit: int = 5) -> list[str]:
    """全チャンクの tool_names を集計し、上位 limit 件（count降順・name昇順）を返す。"""
    counter: Counter[str] = Counter()
    for chunk in chunks:
        counter.update(chunk.tool_names)
    ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return [name for name, _ in ranked[:limit]]


def _build_rule_based_summary(chunks: list[MemoryChunk], aggregate: ChunkAggregate) -> str:
    """トランスクリプトが使えない場合のルール合成サマリーを組み立てる。"""
    first_prompt = compact_line(chunks[0].user_prompt or "", 160) or "(不明)"
    top_files = ", ".join(aggregate.key_files[:3]) or "(なし)"
    top_tools = ", ".join(_top_tool_names(chunks, 5)) or "(なし)"
    return f"目的:{first_prompt} / 結果:{aggregate.outcome} / 主変更:{top_files} / ツール:{top_tools}"


def build_session_digest(
    db: Any,
    session_id: str,
    *,
    transcript_path: str | None = None,
    harness: str = "unknown",
) -> SessionDigest | None:
    """セッションのチャンク・インタラクションログ・トランスクリプトから SessionDigest を構築する。

    Args:
        db: `get_chunks_by_session` / `get_interaction_logs` を持つ DB。
        session_id: 対象セッション ID。
        transcript_path: トランスクリプトファイルパス（無ければ chunks のみで合成）。
        harness: 実行ハーネス（"claude"/"codex"/"copilot"/"unknown"）。そのまま格納する。

    Returns:
        構築した SessionDigest。対象セッションにチャンクが1件も無ければ None。

    Raises:
        例外は発生しません。
    """
    chunks = db.get_chunks_by_session(session_id)
    if not chunks:
        return None

    interaction_logs = db.get_interaction_logs(session_id=session_id, limit=_INTERACTION_LOGS_LIMIT)
    aggregate = _aggregate_chunks(chunks, interaction_logs)

    transcript_text = None
    if transcript_path:
        path = Path(transcript_path)
        if path.exists():
            transcript_text = _extract_final_assistant_text(path, harness)

    if transcript_text:
        raw_summary = transcript_text
        source = "transcript+chunks"
    else:
        raw_summary = _build_rule_based_summary(chunks, aggregate)
        source = "chunks"

    summary = _truncate_summary(redact(raw_summary))

    return SessionDigest(
        session_id=session_id,
        project=chunks[0].project,
        summary=summary,
        key_files=aggregate.key_files,
        key_decisions=aggregate.key_decisions,
        outcome=aggregate.outcome,
        harness=harness,
        source=source,
        chunk_count=len(chunks),
        started_at_epoch=min(c.created_at_epoch for c in chunks),
        ended_at_epoch=max(c.created_at_epoch for c in chunks),
        created_at_epoch=int(time.time()),
    )


def resolve_transcript_path(session_id: str) -> tuple[Path | None, str]:
    """session_id からトランスクリプトファイルのパスとハーネス種別を解決する（digest-backfill 用）。

    1. ``~/.claude/projects/*/{session_id}.jsonl`` を glob 探索する。
       複数ヒット時はパス文字列の昇順ソートで先頭の1件を返す。
    2. 見つからなければ ``~/.copilot/session-state/{session_id}/events.jsonl`` を確認する。
    3. どちらも見つからなければ (None, "unknown") を返す。

    ホームディレクトリは ``Path.home()`` を都度呼び出して解決するため、
    テストから ``monkeypatch.setattr(Path, "home", ...)`` で差し替え可能。

    Args:
        session_id: 対象セッション ID。

    Returns:
        (トランスクリプトパス, ハーネス種別) のタプル。
        ハーネス種別は "claude" / "copilot" / "unknown"。

    Raises:
        例外は発生しません。
    """
    home = Path.home()

    claude_matches = sorted(home.glob(f".claude/projects/*/{session_id}.jsonl"))
    if claude_matches:
        return claude_matches[0], "claude"

    copilot_path = home / ".copilot" / "session-state" / session_id / "events.jsonl"
    if copilot_path.exists():
        return copilot_path, "copilot"

    return None, "unknown"


def generate_and_store_digest(
    db: Any,
    session_id: str,
    *,
    transcript_path: str | None,
    log: Any,
) -> None:
    """セッション要約を構築して保存する（SessionEnd から呼ばれるエントリポイント）。

    Args:
        db: `upsert_session_digest` を持つ DB。
        session_id: 対象セッション ID。
        transcript_path: トランスクリプトファイルパス（無ければ chunks のみで合成）。
        log: info/warning を持つロガー。

    Returns:
        None: 値を返しません。

    Raises:
        例外は発生しません。
    """
    harness = "unknown"
    if transcript_path and Path(transcript_path).exists():
        harness = detect_harness()

    digest = build_session_digest(db, session_id, transcript_path=transcript_path, harness=harness)
    if digest is None:
        return

    db.upsert_session_digest(digest)
    log.info("digest 保存: session=%s outcome=%s harness=%s", session_id, digest.outcome, digest.harness)
