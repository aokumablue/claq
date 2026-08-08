"""SQLite Row → データクラス変換ヘルパー（database.py から分離）。"""

from __future__ import annotations

import json
import sqlite3

from bluecore.mem.logger import get as _get_logger
from bluecore.mem.models import (
    InteractionLog,
    MemoryChunk,
    ProjectProfile,
    SessionDigest,
)

log = _get_logger("DB")


def _parse_json_list(val: str | None) -> list[str]:
    """JSON エンコードされた list を Python list にデシリアライズする。"""
    if not val:
        return []
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError) as e:
        log.debug("JSON パース失敗（list）: %r → %s", val[:50] if val else val, e)
        return []


def _parse_json_dict_list(val: str | None) -> list[dict]:
    """JSON エンコードされた dict のリストを Python list にデシリアライズする。"""
    if not val:
        return []
    try:
        result = json.loads(val)
        return result if isinstance(result, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _row_to_chunk(row: sqlite3.Row) -> MemoryChunk:
    """memory_chunks の Row を MemoryChunk に変換する。"""
    keys = row.keys()
    return MemoryChunk(
        id=row["id"],
        origin_user=row["origin_user"] if "origin_user" in keys else "",
        session_id=row["session_id"],
        project=row["project"],
        chunk_index=row["chunk_index"],
        content=row["content"],
        tool_names=_parse_json_list(row["tool_names"]),
        files_read=_parse_json_list(row["files_read"]),
        files_modified=_parse_json_list(row["files_modified"]),
        created_at_epoch=row["created_at_epoch"],
        access_count=row["access_count"] if "access_count" in keys else 0,
        last_accessed_epoch=row["last_accessed_epoch"] if "last_accessed_epoch" in keys else None,
    )


def _row_to_interaction_log(row: sqlite3.Row) -> InteractionLog:
    """interaction_logs の Row を InteractionLog に変換する。"""
    return InteractionLog(
        id=row["id"],
        origin_user=row["origin_user"],
        session_id=row["session_id"],
        project=row["project"],
        user_prompt_full=row["user_prompt_full"],
        user_prompt_hash=row["user_prompt_hash"],
        ai_response_summary=row["ai_response_summary"],
        ai_response_tool_plan=row["ai_response_tool_plan"],
        chunk_id=row["chunk_id"],
        execution_outcome=row["execution_outcome"],
        tool_error_count=row["tool_error_count"],
        interaction_index=row["interaction_index"],
        created_at_epoch=row["created_at_epoch"],
    )


def _row_to_project_profile(row: sqlite3.Row) -> ProjectProfile:
    """project_profiles の Row を ProjectProfile に変換する。"""
    return ProjectProfile(
        id=row["id"],
        origin_user=row["origin_user"],
        project=row["project"],
        project_path=row["project_path"],
        languages=_parse_json_list(row["languages"]),
        frameworks=_parse_json_list(row["frameworks"]),
        primary_language=row["primary_language"],
        test_command=row["test_command"],
        build_command=row["build_command"],
        scope_hint=row["scope_hint"],
        detected_at_epoch=row["detected_at_epoch"],
        last_updated_epoch=row["last_updated_epoch"],
        detection_confidence=row["detection_confidence"],
    )


def _row_to_session_digest(row: sqlite3.Row) -> SessionDigest:
    """session_digests の Row を SessionDigest に変換する。"""
    return SessionDigest(
        id=row["id"],
        origin_user=row["origin_user"],
        session_id=row["session_id"],
        project=row["project"],
        summary=row["summary"],
        key_files=_parse_json_list(row["key_files"]),
        key_decisions=_parse_json_list(row["key_decisions"]),
        harness=row["harness"],
        source=row["source"],
        chunk_count=row["chunk_count"],
        started_at_epoch=row["started_at_epoch"],
        ended_at_epoch=row["ended_at_epoch"],
        created_at_epoch=row["created_at_epoch"],
    )
