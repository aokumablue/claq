"""mem CLI: session/context/compaction handlers."""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bluecore.hooks.hook_common import emit_user_prompt_submit_output
from bluecore.lib.core_utils import get_git_user_name
from bluecore.lib.harness import normalize_tool_name
from bluecore.mem.cli_search_handlers import merge_search_results_rrf, render_adaptive_context

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from bluecore.mem.database import Database
    from bluecore.mem.settings import Settings

    OpenDbFn = Callable[[Settings], AbstractContextManager[Database]]
    GetProjectFn = Callable[[dict[str, Any]], str]
    EmbedFn = Callable[[list[str]], list[list[float]]]


@dataclass(frozen=True)
class SessionEndDeps:
    """handle_session_end の外部依存（DB接続・埋め込み関数・ロガー・timeモジュール）。"""

    open_db: OpenDbFn
    embed_fn: EmbedFn
    log: Any
    time_module: Any = None


def handle_context(
    settings: Settings,
    stdin_data: dict[str, Any],
    *,
    open_db: OpenDbFn,
    get_project: GetProjectFn,
    log: Any,
) -> str:
    """SessionStart: コンテキスト注入"""
    from bluecore.mem.context import build_context

    project = get_project(stdin_data)
    ctx = ""
    try:
        with open_db(settings) as db:
            ctx = build_context(db, settings, project=project)
    except Exception as e:
        log.warning("コンテキスト生成失敗: %s", e)
    return ctx


def _search_and_inject_context(
    db: Any,
    settings: Settings,
    prompt: str,
    project: str,
    *,
    log: Any,
) -> None:
    """プロンプトに関連するメモリを検索してコンテキストとして print する。"""
    from bluecore.mem.search import SearchService

    svc = SearchService(db, settings)
    local_results = svc.search(query=prompt, project=project, limit=3)

    team_results = []
    if settings.sync.enabled and settings.sync.postgres_url:
        git_user = get_git_user_name()
        exclude = git_user if settings.team.exclude_self else None
        try:
            team_results = svc.search_team(query=prompt, limit=3, exclude_origin_user=exclude)
        except Exception as e:
            log.warning("チーム検索失敗（ローカルのみ使用）: %s", e)

    merged = merge_search_results_rrf(local_results, team_results, top_k=3)
    if merged:
        ctx = render_adaptive_context(db, merged)
        print(emit_user_prompt_submit_output(ctx))


def handle_session_init(
    settings: Settings,
    stdin_data: dict[str, Any],
    *,
    open_db: OpenDbFn,
    get_project: GetProjectFn,
    log: Any,
) -> None:
    """UserPromptSubmit: セッション初期化 + 適応的検索注入"""
    from bluecore.mem.database import Session
    from bluecore.mem.search import should_inject_memory

    session_id = str(stdin_data.get("session_id", "") or "")
    project = get_project(stdin_data)
    prompt = str(stdin_data.get("prompt", "") or "")

    if project in settings.excluded_projects:
        return

    try:
        with open_db(settings) as db:
            db.upsert_session(Session(
                session_id=session_id,
                project=project,
                started_at_epoch=int(time.time()),
            ))
            if prompt and should_inject_memory(prompt):
                _search_and_inject_context(db, settings, prompt, project, log=log)
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
    from bluecore.mem.chunker import ToolUseParams, build_chunk_from_tool_use

    session_id = str(stdin_data.get("session_id", "") or "")
    project = get_project(stdin_data)
    tool_name = normalize_tool_name(str(stdin_data.get("tool_name", "") or ""))

    tool_input = stdin_data.get("tool_input")
    tool_response = stdin_data.get("tool_response")
    user_prompt = str(stdin_data.get("prompt", "") or "")

    try:
        with open_db(settings) as db:
            params = ToolUseParams(
                tool_name=tool_name,
                tool_input=tool_input,
                tool_response=str(tool_response) if tool_response else None,
                chunk_max_length=settings.chunk_max_length,
            )
            chunk = build_chunk_from_tool_use(
                session_id=session_id,
                project=project,
                chunk_index=0,  # store_chunk が MAX+1 で自動採番するため不要
                user_prompt=user_prompt,
                params=params,
            )
            db.store_chunk(chunk)
    except Exception as e:
        log.warning("チャンク保存失敗: %s", e)


def _auto_compact_if_needed(
    db: Any, settings: Settings, *, log: Any, time_module: Any
) -> None:
    """自動圧縮インターバルが経過していれば低品質チャンクを削除して DB を最適化する。"""
    from bluecore.mem.compaction import detect_low_quality, optimize_db

    if not settings.auto_compact_enabled:
        return
    interval_sec = settings.auto_compact_interval_days * 86400
    if time_module.time() - settings.last_compacted_at < interval_sec:
        return
    try:
        low_quality_ids = detect_low_quality(db)
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
        log.info("自動圧縮完了: 削除=%d", len(low_quality_ids))
    except Exception as e:
        log.warning("自動圧縮エラー: %s", e)


def handle_session_end(
    settings: Settings,
    stdin_data: dict[str, Any],
    deps: SessionEndDeps,
) -> None:
    """SessionEnd: 埋め込み一括生成 + FTS5 最適化"""
    from bluecore.mem.bridge import sync_session_to_observations

    time_module = deps.time_module if deps.time_module is not None else time
    session_id = str(stdin_data.get("session_id", "") or "")

    try:
        with deps.open_db(settings) as db:
            chunks = db.get_chunks_by_session(session_id)
            if not chunks:
                return

            from bluecore.mem.redaction import redact

            # id を持つチャンクだけを対象にし、texts と chunk_ids のインデックスを一致させる。
            embeddable = [c for c in chunks if c.id is not None]
            texts = [redact(c.content) for c in embeddable]
            embeddings = deps.embed_fn(texts)
            chunk_ids = [c.id for c in embeddable]
            db.store_embeddings(chunk_ids, embeddings)
            deps.log.info("埋め込み保存: session=%s chunks=%d", session_id, len(chunk_ids))

            try:
                db.conn.execute("INSERT INTO memory_chunks_fts(memory_chunks_fts) VALUES('optimize')")
                db.conn.commit()
            except Exception as e:
                deps.log.warning("FTS5 最適化失敗: %s", e)

            try:
                synced = sync_session_to_observations(db, session_id)
                deps.log.info("learn 同期: session=%s synced=%d", session_id, synced)
            except Exception as e:
                deps.log.warning("learn 同期失敗: %s", e)

            _auto_compact_if_needed(db, settings, log=deps.log, time_module=time_module)
    except Exception as e:
        deps.log.warning("セッション終了失敗: %s", e)


def handle_reembed(
    settings: Settings,
    deps: SessionEndDeps,
) -> None:
    """reembed: vec テーブルを再作成し、全チャンクの埋め込みを再生成する。

    埋め込みモデル（次元）変更後に既存チャンクの埋め込みを現行モデルで再生成する。
    バッチ処理のためチャンク数に依らずメモリ使用量は一定。
    """
    from bluecore.mem.redaction import redact

    batch_size = 256
    with deps.open_db(settings) as db:
        if not db.recreate_vec_table():
            print("reembed: sqlite-vec が利用できないためスキップしました")
            return
        embeddable = [c for c in db.get_all_chunks() if c.id is not None]
        total = 0
        for start in range(0, len(embeddable), batch_size):
            batch = embeddable[start:start + batch_size]
            embeddings = deps.embed_fn([redact(c.content) for c in batch])
            if not embeddings:
                print("reembed: 埋め込みモデルが未配置のため中断しました", file=sys.stderr)
                return
            db.store_embeddings([c.id for c in batch], embeddings)
            total += len(batch)
        deps.log.info("reembed 完了: chunks=%d", total)
        print(f"reembed: {total} 件の埋め込みを再生成しました")


def handle_compact(
    settings: Settings,
    *,
    open_db: OpenDbFn,
    log: Any,
) -> None:
    """メモリ圧縮コマンド（既定で実行）"""
    from bluecore.mem.compaction import detect_low_quality, optimize_db

    try:
        with open_db(settings) as db:
            low_quality_ids = detect_low_quality(db)

            print(f"削除候補: {len(low_quality_ids)} 件")

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
