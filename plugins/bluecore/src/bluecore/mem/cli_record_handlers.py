"""mem CLI: record/profile handlers."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from bluecore.mem.database import Database
    from bluecore.mem.settings import Settings

    OpenDbFn = Callable[[Settings], AbstractContextManager[Database]]
    GetProjectFn = Callable[[dict[str, Any]], str]
    GitUserFn = Callable[[], str]


@dataclass(frozen=True)
class RecordDeps:
    """record系ハンドラの外部依存（DB接続・プロジェクト解決・gitユーザー名・ロガー）。"""

    open_db: OpenDbFn
    get_project: GetProjectFn
    log: Any
    get_git_user_name: GitUserFn


def _build_record_chunk(
    stdin_data: dict[str, Any],
    session_id: str,
    project: str,
) -> Any:
    """handle_record 用のチャンクオブジェクトを stdin_data から構築して返す。"""
    from bluecore.mem.database import MemoryChunk

    event_type = str(stdin_data.get("event_type", "custom") or "custom")
    content = str(stdin_data.get("content", "") or "")
    metadata = stdin_data.get("metadata", {})
    # chunk_index は store_chunk の INSERT（SQL の MAX+1）で確定するためここでは 0 を渡す。
    files_read = metadata.get("files_read", [])
    files_modified = metadata.get("files_modified", [])
    return MemoryChunk(
        session_id=session_id, project=project, chunk_index=0,
        content=content, tool_names=[event_type],
        files_read=files_read if isinstance(files_read, list) else [],
        files_modified=files_modified if isinstance(files_modified, list) else [],
        created_at_epoch=int(time.time()),
    )


def handle_record(
    settings: Settings,
    stdin_data: dict[str, Any],
    deps: RecordDeps,
) -> None:
    """明示的記録: コマンド/スキル/エージェントからの直接記録"""
    from bluecore.mem.database import Session

    session_id = str(stdin_data.get("session_id", "") or f"record-{int(time.time())}")
    project = deps.get_project(stdin_data)
    content = str(stdin_data.get("content", "") or "")

    if not content.strip():
        print(json.dumps({"success": False, "error": "content is required"}))
        return

    try:
        with deps.open_db(settings) as db:
            db.upsert_session(Session(
                session_id=session_id, project=project, started_at_epoch=int(time.time()),
            ))
            chunk = _build_record_chunk(stdin_data, session_id, project)
            chunk_id = db.store_chunk(chunk)
        print(json.dumps({"success": True, "chunk_id": chunk_id}))
    except Exception as e:
        deps.log.warning("記録失敗: %s", e)
        print(json.dumps({"success": False, "error": str(e)}))


def _build_interaction_log(
    stdin_data: dict[str, Any],
    session_id: str,
    project: str,
    interaction_index: int,
    origin_user: str,
) -> Any:
    """InteractionLog オブジェクトを構築して返す。"""
    from bluecore.mem.database import InteractionLog

    return InteractionLog(
        session_id=session_id, project=project,
        user_prompt_full=str(stdin_data.get("user_prompt_full") or stdin_data.get("prompt") or ""),
        interaction_index=interaction_index,
        created_at_epoch=int(time.time()),
        origin_user=origin_user,
    )


def handle_record_interaction(
    settings: Settings,
    stdin_data: dict[str, Any],
    deps: RecordDeps,
) -> None:
    """interaction_logs へのインタラクション記録。"""
    from bluecore.mem.database import Session

    session_id = str(stdin_data.get("session_id", "") or "")
    project = deps.get_project(stdin_data)
    user_prompt_full = str(stdin_data.get("user_prompt_full") or stdin_data.get("prompt") or "")

    if not user_prompt_full.strip():
        print(json.dumps({"success": True, "skipped": True, "reason": "no prompt"}))
        return

    try:
        with deps.open_db(settings) as db:
            db.upsert_session(Session(
                session_id=session_id, project=project, started_at_epoch=int(time.time()),
            ))
            interaction_index = db.get_next_interaction_index(session_id)
            log_entry = _build_interaction_log(
                stdin_data, session_id, project, interaction_index, deps.get_git_user_name()
            )
            log_id = db.store_interaction_log(log_entry)
        print(json.dumps({"success": True, "id": log_id, "interaction_index": interaction_index}))
    except Exception as e:
        deps.log.warning("インタラクション記録失敗: %s", e)
        print(json.dumps({"success": False, "error": str(e)}))


