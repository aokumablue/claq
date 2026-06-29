"""mem CLI: record/profile/item-run handlers."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bluecore.lib.harness import detect_harness

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
    user_prompt = str(stdin_data.get("user_prompt", "") or "")
    metadata = stdin_data.get("metadata", {})
    # chunk_index は store_chunk の INSERT（SQL の MAX+1）で確定するためここでは 0 を渡す。
    files_read = metadata.get("files_read", [])
    files_modified = metadata.get("files_modified", [])
    return MemoryChunk(
        session_id=session_id, project=project, chunk_index=0,
        content=content, tool_names=[event_type],
        files_read=files_read if isinstance(files_read, list) else [],
        files_modified=files_modified if isinstance(files_modified, list) else [],
        user_prompt=user_prompt, created_at_epoch=int(time.time()),
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
        ai_response_summary=str(stdin_data.get("ai_response_summary", "") or "") or None,
        ai_response_tool_plan=str(stdin_data.get("ai_response_tool_plan", "") or "") or None,
        chunk_id=str(stdin_data.get("chunk_id", "") or "") or None,
        execution_outcome=str(stdin_data.get("execution_outcome", "unknown") or "unknown"),
        tool_error_count=int(stdin_data.get("tool_error_count", 0) or 0),
    )


def _resolve_profile_list_field(
    stdin_data: dict[str, Any],
    key: str,
    fallback: list[str],
) -> list[str]:
    """プロジェクトプロファイルの list[str] 項目を解決する。"""
    if key not in stdin_data:
        return list(fallback)
    raw_value = stdin_data.get(key)
    if not isinstance(raw_value, list):
        return []
    return [str(item).strip() for item in raw_value if str(item).strip()]


def _resolve_profile_optional_string(
    stdin_data: dict[str, Any],
    key: str,
    fallback: str | None,
) -> str | None:
    """プロジェクトプロファイルの任意文字列項目を解決する。"""
    if key not in stdin_data:
        return fallback
    value = str(stdin_data.get(key, "") or "").strip()
    return value or None


def _resolve_scope_hint(
    stdin_data: dict[str, Any],
    fallback: str,
) -> str:
    """scope_hint を解決する。未指定時は既存値を維持する。"""
    if "scope_hint" not in stdin_data:
        return fallback
    value = str(stdin_data.get("scope_hint", "") or "").strip()
    return value or fallback


def _should_preserve_existing_profile_fields() -> bool:
    """Copilot CLI では sparse な SessionStart payload による上書きを防ぐ。"""
    return detect_harness() == "copilot"


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


def handle_record_project_profile(
    settings: Settings,
    stdin_data: dict[str, Any],
    deps: RecordDeps,
) -> str:
    """project_profiles のアップサート。"""
    from bluecore.mem.database import ProjectProfile

    project = stdin_data.get("project") or deps.get_project(stdin_data)
    now = int(time.time())
    origin_user = deps.get_git_user_name()

    try:
        with deps.open_db(settings) as db:
            existing = (
                db.get_project_profile(project, origin_user=origin_user)
                if _should_preserve_existing_profile_fields()
                else None
            )
            profile = ProjectProfile(
                project=project,
                detected_at_epoch=existing.detected_at_epoch if existing is not None else now,
                last_updated_epoch=now,
                origin_user=origin_user,
                project_path=_resolve_profile_optional_string(
                    stdin_data,
                    "project_path",
                    existing.project_path if existing is not None else None,
                ),
                languages=_resolve_profile_list_field(
                    stdin_data,
                    "languages",
                    existing.languages if existing is not None else [],
                ),
                frameworks=_resolve_profile_list_field(
                    stdin_data,
                    "frameworks",
                    existing.frameworks if existing is not None else [],
                ),
                primary_language=_resolve_profile_optional_string(
                    stdin_data,
                    "primary_language",
                    existing.primary_language if existing is not None else None,
                ),
                test_command=_resolve_profile_optional_string(
                    stdin_data,
                    "test_command",
                    existing.test_command if existing is not None else None,
                ),
                build_command=_resolve_profile_optional_string(
                    stdin_data,
                    "build_command",
                    existing.build_command if existing is not None else None,
                ),
                scope_hint=_resolve_scope_hint(
                    stdin_data,
                    existing.scope_hint if existing is not None else "project",
                ),
                detection_confidence=(
                    existing.detection_confidence if existing is not None else 1.0
                ),
            )
            profile_id = db.upsert_project_profile(profile)
        deps.log.info("project profile saved: %s (id=%s)", project, profile_id)
    except Exception as e:
        deps.log.warning("プロジェクトプロファイル保存失敗: %s", e)
    return ""


def handle_get_project_profile(
    settings: Settings,
    stdin_data: dict[str, Any],
    deps: RecordDeps,
) -> None:
    """project_profiles の取得。"""
    project = stdin_data.get("project") or deps.get_project(stdin_data)

    try:
        with deps.open_db(settings) as db:
            profile = db.get_project_profile(project, origin_user=deps.get_git_user_name())
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
        deps.log.warning("プロジェクトプロファイル取得失敗: %s", e)
        print(json.dumps({"found": False, "error": str(e)}))


def _extract_item_name_and_type(
    stdin_data: dict[str, Any], *, log: Any
) -> tuple[str, str] | None:
    """stdin_data からスキル名と item_type を抽出して返す。無効な場合は None。"""
    tool_input = stdin_data.get("tool_input", {})
    if isinstance(tool_input, dict) and tool_input.get("skill"):
        skill_name = str(tool_input["skill"])
        item_type = "skill"
    else:
        skill_name = str(stdin_data.get("skill_name", "") or "")
        item_type = stdin_data.get("item_type", "skill")

    if not skill_name:
        return None
    if item_type not in ("skill", "command", "agent"):
        log.warning("record-item-run: 不正な item_type=%s", item_type)
        return None
    return skill_name, item_type


def handle_record_item_run(
    settings: Settings,
    stdin_data: dict[str, Any],
    deps: RecordDeps,
) -> None:
    """スキル・コマンド・エージェントの実行記録を mem_item_runs に保存する。"""
    from bluecore.mem.database import MemItemRun

    extracted = _extract_item_name_and_type(stdin_data, log=deps.log)
    if extracted is None:
        return
    skill_name, item_type = extracted

    run = MemItemRun(
        session_id=str(stdin_data.get("session_id", "") or ""),
        project=deps.get_project(stdin_data),
        skill_name=skill_name,
        created_at_epoch=int(time.time()),
        origin_user=deps.get_git_user_name(),
        item_type=item_type,
        outcome=stdin_data.get("outcome", "unknown"),
        skill_trigger=stdin_data.get("skill_trigger"),
        duration_seconds=stdin_data.get("duration_seconds"),
    )

    try:
        with deps.open_db(settings) as db:
            run_id = db.store_mem_item_run(run)
        deps.log.info("item_run 記録: %s (%s) id=%s", skill_name, item_type, run_id)
        print(json.dumps({"success": True, "id": run_id}))
    except Exception as e:
        deps.log.warning("record-item-run 失敗: %s", e)
        print(json.dumps({"success": False, "error": str(e)}))
