"""mem CLI: session/context/compaction handlers."""

from __future__ import annotations

import json
import sys
import time
from typing import TYPE_CHECKING, Any

from devgear.lib.core_utils import get_git_user_name
from devgear.mem.cli_search_handlers import merge_search_results_rrf, render_adaptive_context

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from devgear.mem.database import Database
    from devgear.mem.settings import Settings

    OpenDbFn = Callable[[Settings], AbstractContextManager[Database]]
    GetProjectFn = Callable[[dict[str, Any]], str]
    EmbedFn = Callable[[list[str]], list[list[float]]]


def handle_context(
    settings: Settings,
    stdin_data: dict[str, Any],
    *,
    open_db: OpenDbFn,
    get_project: GetProjectFn,
    log: Any,
) -> str:
    """SessionStart: コンテキスト注入"""
    from devgear.mem.context import build_context

    project = get_project(stdin_data)
    ctx = ""
    try:
        with open_db(settings) as db:
            ctx = build_context(db, settings, project=project)
    except Exception as e:
        log.warning("コンテキスト生成失敗: %s", e)
    return ctx


def handle_session_init(
    settings: Settings,
    stdin_data: dict[str, Any],
    *,
    open_db: OpenDbFn,
    get_project: GetProjectFn,
    log: Any,
) -> None:
    """UserPromptSubmit: セッション初期化 + 適応的検索注入"""
    from devgear.mem.database import Session
    from devgear.mem.search import SearchService, should_inject_memory

    session_id = str(stdin_data.get("session_id", "") or "")
    project = get_project(stdin_data)
    prompt = str(stdin_data.get("prompt", "") or "")

    if project in settings.excluded_projects:
        return

    try:
        with open_db(settings) as db:
            db.upsert_session(
                Session(
                    session_id=session_id,
                    project=project,
                    started_at_epoch=int(time.time()),
                )
            )

            if not prompt or not should_inject_memory(prompt):
                return

            svc = SearchService(db, settings)
            local_results = svc.search(query=prompt, project=project, limit=3)

            team_results = []
            if settings.sync.enabled and settings.sync.postgres_url:
                git_user = get_git_user_name()
                exclude = git_user if settings.team.exclude_self else None
                try:
                    team_results = svc.search_team(
                        query=prompt,
                        limit=3,
                        exclude_origin_user=exclude,
                    )
                except Exception as e:
                    log.warning("チーム検索失敗（ローカルのみ使用）: %s", e)

            merged = merge_search_results_rrf(local_results, team_results, top_k=3)

            if merged:
                ctx = render_adaptive_context(db, merged)
                print(
                    json.dumps(
                        {
                            "hookEventName": "UserPromptSubmit",
                            "additionalContext": ctx,
                        }
                    )
                )
    except Exception as e:
        log.warning("セッション初期化失敗: %s", e)


def handle_observe(
    settings: Settings,
    stdin_data: dict[str, Any],
    *,
    open_db: OpenDbFn,
    get_project: GetProjectFn,
    log: Any,
) -> None:
    """PostToolUse: ツール使用をチャンクとして保存"""
    from devgear.mem.chunker import build_chunk_from_tool_use

    session_id = str(stdin_data.get("session_id", "") or "")
    project = get_project(stdin_data)
    tool_name = str(stdin_data.get("tool_name", "") or "")

    tool_input = stdin_data.get("tool_input")
    tool_response = stdin_data.get("tool_response")
    user_prompt = str(stdin_data.get("prompt", "") or "")

    try:
        with open_db(settings) as db:
            chunk = build_chunk_from_tool_use(
                session_id=session_id,
                project=project,
                chunk_index=0,  # store_chunk が MAX+1 で自動採番するため不要
                user_prompt=user_prompt,
                tool_name=tool_name,
                tool_input=tool_input,
                tool_response=str(tool_response) if tool_response else None,
                chunk_max_length=settings.chunk_max_length,
            )
            db.store_chunk(chunk)
    except Exception as e:
        log.warning("チャンク保存失敗: %s", e)


def handle_session_end(
    settings: Settings,
    stdin_data: dict[str, Any],
    *,
    open_db: OpenDbFn,
    embed_fn: EmbedFn,
    log: Any,
    time_module: Any = time,
) -> None:
    """SessionEnd: 埋め込み一括生成 + FTS5 最適化"""
    from devgear.mem.bridge import sync_session_to_observations
    from devgear.mem.compaction import detect_low_quality, find_near_duplicates, optimize_db

    session_id = str(stdin_data.get("session_id", "") or "")

    try:
        with open_db(settings) as db:
            chunks = db.get_chunks_by_session(session_id)
            if not chunks:
                return

            from devgear.mem.redaction import redact
            texts = [redact(c.content) for c in chunks]
            embeddings = embed_fn(texts)
            chunk_ids = [c.id for c in chunks if c.id is not None]
            db.store_embeddings(chunk_ids, embeddings)
            log.info("埋め込み保存: session=%s chunks=%d", session_id, len(chunk_ids))

            try:
                db.conn.execute("INSERT INTO memory_chunks_fts(memory_chunks_fts) VALUES('optimize')")
                db.conn.commit()
            except Exception as e:
                log.warning("FTS5 最適化失敗: %s", e)

            try:
                synced = sync_session_to_observations(db, session_id)
                log.info("s-learn 同期: session=%s synced=%d", session_id, synced)
            except Exception as e:
                log.warning("s-learn 同期失敗: %s", e)

            if settings.auto_compact_enabled:
                interval_sec = settings.auto_compact_interval_days * 86400
                if time_module.time() - settings.last_compacted_at >= interval_sec:
                    try:
                        low_quality_ids = detect_low_quality(db)
                        near_dup_pairs = find_near_duplicates(db)
                        # TODO: near_dup_pairs の重複削除処理を実装する
                        if low_quality_ids:
                            placeholders = ",".join("?" * len(low_quality_ids))
                            db.conn.execute(
                                f"DELETE FROM memory_chunks WHERE id IN ({placeholders})",
                                low_quality_ids,
                            )
                            db.conn.commit()
                        optimize_db(db)
                        settings.last_compacted_at = time_module.time()
                        settings.save_sync_state()
                        log.info(
                            "自動圧縮完了: 削除=%d 重複ペア=%d",
                            len(low_quality_ids),
                            len(near_dup_pairs),
                        )
                    except Exception as e:
                        log.warning("自動圧縮エラー: %s", e)
    except Exception as e:
        log.warning("セッション終了失敗: %s", e)


def handle_compact(
    settings: Settings,
    *,
    open_db: OpenDbFn,
    log: Any,
) -> None:
    """メモリ圧縮コマンド（既定で実行）"""
    from devgear.mem.compaction import detect_low_quality, find_near_duplicates, optimize_db

    try:
        with open_db(settings) as db:
            low_quality_ids = detect_low_quality(db)
            near_dup_pairs = find_near_duplicates(db)

            print(f"削除候補: {len(low_quality_ids)} 件")
            print(f"重複ペア: {len(near_dup_pairs)} 件")

            if low_quality_ids:
                placeholders = ",".join("?" * len(low_quality_ids))
                db.conn.execute(
                    f"DELETE FROM memory_chunks WHERE id IN ({placeholders})",
                    low_quality_ids,
                )
                db.conn.commit()
            opt = optimize_db(db)
            print(f"実行済み（断片化率: {opt.get('fragmentation_before', 0):.1%}）")
    except Exception as e:
        print("DB に接続できません", file=sys.stderr)
        log.warning("圧縮失敗: %s", e)
