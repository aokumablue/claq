"""SQLite Row → データクラス変換ヘルパー（database.py から分離）。"""

from __future__ import annotations

import json
import sqlite3

from bluecore.mem.logger import get as _get_logger
from bluecore.mem.models import (
    Adr,
    EventLog,
    Instinct,
    InteractionLog,
    MemItemRun,
    MemoryChunk,
    ProjectProfile,
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
        user_prompt=row["user_prompt"] or "",
        created_at_epoch=row["created_at_epoch"],
        access_count=row["access_count"] if "access_count" in keys else 0,
        last_accessed_epoch=row["last_accessed_epoch"] if "last_accessed_epoch" in keys else None,
        merged_generation=row["merged_generation"] if "merged_generation" in keys else 0,
        merged_into=row["merged_into"] if "merged_into" in keys else None,
        execution_status=row["execution_status"] if "execution_status" in keys else "unknown",
        tool_error=row["tool_error"] if "tool_error" in keys else None,
        ai_response_summary=row["ai_response_summary"] if "ai_response_summary" in keys else None,
        tool_sequence=_parse_json_list(row["tool_sequence"]) if "tool_sequence" in keys else [],
    )


def _row_to_instinct(row: sqlite3.Row) -> Instinct:
    """instincts の Row を Instinct に変換する。"""
    keys = row.keys()
    return Instinct(
        id=row["id"],
        origin_user=row["origin_user"],
        instinct_id=row["instinct_id"],
        scope=row["scope"],
        project_id=row["project_id"],
        trigger_text=row["trigger_text"],
        confidence=row["confidence"],
        domain=row["domain"],
        content=row["content"],
        created_at_epoch=row["created_at_epoch"],
        updated_at_epoch=row["updated_at_epoch"],
        observation_count=row["observation_count"] if "observation_count" in keys else 0,
        confidence_reasons=_parse_json_dict_list(row["confidence_reasons"]) if "confidence_reasons" in keys else [],
        source_interaction_ids=_parse_json_list(row["source_interaction_ids"]) if "source_interaction_ids" in keys else [],
        last_activated_epoch=row["last_activated_epoch"] if "last_activated_epoch" in keys else None,
    )


def _row_to_adr(row: sqlite3.Row) -> Adr:
    """adrs の Row を Adr に変換する。"""
    return Adr(
        id=row["id"],
        origin_user=row["origin_user"],
        project=row["project"],
        adr_number=row["adr_number"],
        title=row["title"],
        status=row["status"],
        content=row["content"],
        created_at_epoch=row["created_at_epoch"],
        updated_at_epoch=row["updated_at_epoch"],
    )


def _row_to_event_log(row: sqlite3.Row) -> EventLog:
    """event_logs の Row を EventLog に変換する。"""
    return EventLog(
        id=row["id"],
        origin_user=row["origin_user"],
        event_type=row["event_type"],
        project_id=row["project_id"],
        content=row["content"],
        created_at_epoch=row["created_at_epoch"],
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


def _row_to_mem_item_run(row: sqlite3.Row) -> MemItemRun:
    """mem_item_runs の Row を MemItemRun に変換する。"""
    keys = row.keys()
    return MemItemRun(
        id=row["id"],
        origin_user=row["origin_user"],
        session_id=row["session_id"],
        project=row["project"],
        skill_name=row["skill_name"],
        skill_trigger=row["skill_trigger"],
        outcome=row["outcome"],
        tools_used=_parse_json_list(row["tools_used"]),
        files_modified_count=row["files_modified_count"],
        duration_seconds=row["duration_seconds"],
        interaction_log_id=row["interaction_log_id"],
        created_at_epoch=row["created_at_epoch"],
        item_type=row["item_type"] if "item_type" in keys else "skill",
    )
