"""mem CLI: record/profile/item-run handlers."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from devgear.mem.database import Database
    from devgear.mem.settings import Settings

    OpenDbFn = Callable[[Settings], AbstractContextManager[Database]]
    GetProjectFn = Callable[[dict[str, Any]], str]
    GitUserFn = Callable[[], str]


def handle_record(
    settings: Settings,
    stdin_data: dict[str, Any],
    *,
    open_db: OpenDbFn,
    get_project: GetProjectFn,
    log: Any,
) -> None:
    """明示的記録: コマンド/スキル/エージェントからの直接記録"""
    from devgear.mem.database import MemoryChunk, Session

    session_id = str(stdin_data.get("session_id", "") or f"record-{int(time.time())}")
    project = get_project(stdin_data)
    event_type = str(stdin_data.get("event_type", "custom") or "custom")
    content = str(stdin_data.get("content", "") or "")
    user_prompt = str(stdin_data.get("user_prompt", "") or "")
    metadata = stdin_data.get("metadata", {})

    if not content.strip():
        print(json.dumps({"success": False, "error": "content is required"}))
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

            chunk_index = db.get_next_chunk_index(session_id)
            files_read = metadata.get("files_read", [])
            files_modified = metadata.get("files_modified", [])

            chunk = MemoryChunk(
                session_id=session_id,
                project=project,
                chunk_index=chunk_index,
                content=content,
                tool_names=[event_type],
                files_read=files_read if isinstance(files_read, list) else [],
                files_modified=files_modified if isinstance(files_modified, list) else [],
                user_prompt=user_prompt,
                created_at_epoch=int(time.time()),
            )
            chunk_id = db.store_chunk(chunk)

        print(json.dumps({"success": True, "chunk_id": chunk_id}))
    except Exception as e:
        log.warning("記録失敗: %s", e)
        print(json.dumps({"success": False, "error": str(e)}))


def handle_record_interaction(
    settings: Settings,
    stdin_data: dict[str, Any],
    *,
    open_db: OpenDbFn,
    get_project: GetProjectFn,
    get_git_user_name: GitUserFn,
    log: Any,
) -> None:
    """interaction_logs へのインタラクション記録。"""
    from devgear.mem.database import InteractionLog, Session

    session_id = str(stdin_data.get("session_id", "") or "")
    project = get_project(stdin_data)
    # Claude Code UserPromptSubmit は "prompt" キーで渡す（"user_prompt_full" は旧名）
    user_prompt_full = str(
        stdin_data.get("user_prompt_full") or stdin_data.get("prompt") or ""
    )

    if not user_prompt_full.strip():
        print(json.dumps({"success": True, "skipped": True, "reason": "no prompt"}))
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
            interaction_index = db.get_next_interaction_index(session_id)
            log_entry = InteractionLog(
                session_id=session_id,
                project=project,
                user_prompt_full=user_prompt_full,
                interaction_index=interaction_index,
                created_at_epoch=int(time.time()),
                origin_user=get_git_user_name(),
                ai_response_summary=str(stdin_data.get("ai_response_summary", "") or "") or None,
                ai_response_tool_plan=str(stdin_data.get("ai_response_tool_plan", "") or "") or None,
                chunk_id=str(stdin_data.get("chunk_id", "") or "") or None,
                execution_outcome=str(stdin_data.get("execution_outcome", "unknown") or "unknown"),
                tool_error_count=int(stdin_data.get("tool_error_count", 0) or 0),
            )
            log_id = db.store_interaction_log(log_entry)
        print(json.dumps({"success": True, "id": log_id, "interaction_index": interaction_index}))
    except Exception as e:
        log.warning("インタラクション記録失敗: %s", e)
        print(json.dumps({"success": False, "error": str(e)}))


def handle_record_project_profile(
    settings: Settings,
    stdin_data: dict[str, Any],
    *,
    open_db: OpenDbFn,
    get_project: GetProjectFn,
    get_git_user_name: GitUserFn,
    log: Any,
) -> str:
    """project_profiles のアップサート。"""
    from devgear.mem.database import ProjectProfile

    project = stdin_data.get("project") or get_project(stdin_data)
    now = int(time.time())

    try:
        with open_db(settings) as db:
            profile = ProjectProfile(
                project=project,
                detected_at_epoch=now,
                last_updated_epoch=now,
                origin_user=get_git_user_name(),
                project_path=str(stdin_data.get("project_path", "") or "") or None,
                languages=stdin_data.get("languages", []) or [],
                frameworks=stdin_data.get("frameworks", []) or [],
                primary_language=str(stdin_data.get("primary_language", "") or "") or None,
                test_command=str(stdin_data.get("test_command", "") or "") or None,
                build_command=str(stdin_data.get("build_command", "") or "") or None,
                scope_hint=str(stdin_data.get("scope_hint", "project") or "project"),
            )
            profile_id = db.upsert_project_profile(profile)
        log.info("project profile saved: %s (id=%s)", project, profile_id)
    except Exception as e:
        log.warning("プロジェクトプロファイル保存失敗: %s", e)
    return ""


def handle_get_project_profile(
    settings: Settings,
    stdin_data: dict[str, Any],
    *,
    open_db: OpenDbFn,
    get_project: GetProjectFn,
    get_git_user_name: GitUserFn,
    log: Any,
) -> None:
    """project_profiles の取得。"""
    project = stdin_data.get("project") or get_project(stdin_data)

    try:
        with open_db(settings) as db:
            profile = db.get_project_profile(project, origin_user=get_git_user_name())
        if profile:
            print(
                json.dumps(
                    {
                        "found": True,
                        "project": profile.project,
                        "languages": profile.languages,
                        "frameworks": profile.frameworks,
                        "primary_language": profile.primary_language,
                        "scope_hint": profile.scope_hint,
                        "last_updated_epoch": profile.last_updated_epoch,
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(json.dumps({"found": False}))
    except Exception as e:
        log.warning("プロジェクトプロファイル取得失敗: %s", e)
        print(json.dumps({"found": False, "error": str(e)}))


def handle_record_item_run(
    settings: Settings,
    stdin_data: dict[str, Any],
    *,
    open_db: OpenDbFn,
    get_project: GetProjectFn,
    get_git_user_name: GitUserFn,
    log: Any,
) -> None:
    """スキル・コマンド・エージェントの実行記録を mem_item_runs に保存する。"""
    from devgear.mem.database import MemItemRun

    tool_input = stdin_data.get("tool_input", {})
    if isinstance(tool_input, dict) and tool_input.get("skill"):
        skill_name = str(tool_input["skill"])
        item_type = "skill"
    else:
        skill_name = str(stdin_data.get("skill_name", "") or "")
        item_type = stdin_data.get("item_type", "skill")

    if not skill_name:
        # Agent ツール呼び出し等、スキル以外の Skill マッチャー発火時はスキップ
        return

    if item_type not in ("skill", "command", "agent"):
        log.warning("record-item-run: 不正な item_type=%s", item_type)
        return

    session_id = str(stdin_data.get("session_id", "") or "")
    project = get_project(stdin_data)
    created_at_epoch = int(time.time())

    run = MemItemRun(
        session_id=session_id,
        project=project,
        skill_name=skill_name,
        created_at_epoch=created_at_epoch,
        origin_user=get_git_user_name(),
        item_type=item_type,
        outcome=stdin_data.get("outcome", "unknown"),
        skill_trigger=stdin_data.get("skill_trigger"),
        duration_seconds=stdin_data.get("duration_seconds"),
    )

    try:
        with open_db(settings) as db:
            run_id = db.store_mem_item_run(run)
        log.info("item_run 記録: %s (%s) id=%s", skill_name, item_type, run_id)
        print(json.dumps({"success": True, "id": run_id}))
    except Exception as e:
        log.warning("record-item-run 失敗: %s", e)
        print(json.dumps({"success": False, "error": str(e)}))
