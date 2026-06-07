"""mem CLI: team-context handlers."""

from __future__ import annotations

import json
from typing import Any


def handle_team_context(
    settings,
    stdin_data: dict[str, Any],
    *,
    get_project,
    get_git_user_name,
    log,
) -> str:
    """SessionStart: PostgreSQL チーム共有チャンクから ``<team-context>`` を注入。"""
    sync_cfg = settings.sync
    ctx = ""
    if not settings.team.enabled or not sync_cfg.enabled or not sync_cfg.postgres_url:
        return ""

    project = get_project(stdin_data)
    if not project:
        return ""
    if project in settings.excluded_projects:
        return ""

    git_user = get_git_user_name()

    try:
        from bluecore.mem.pg_database import PgDatabase
        from bluecore.mem.team_context import TeamSearchConfig, build_team_context
    except Exception as e:
        log.warning("team-context モジュール読み込み失敗: %s", e)
        return ""

    pg = None
    try:
        pg = PgDatabase(sync_cfg.postgres_url)
        if not pg.test_connection():
            log.warning("team-context: PostgreSQL 接続失敗")
            return ""
        exclude = git_user if settings.team.exclude_self else ""
        ctx = build_team_context(
            pg,
            query=project,
            exclude_origin_user=exclude,
            config=TeamSearchConfig(settings=settings.team, mode="fts"),
        )
    except Exception as e:
        log.warning("team-context 生成失敗: %s", e)
    finally:
        if pg is not None:
            pg.close()
    return ctx


def _build_team_session_context(
    settings: Any,
    pg: Any,
    query: str,
    git_user: str,
) -> str:
    """PgDatabase を使ってチームコンテキスト文字列を生成して返す。失敗時は空文字列。"""
    from bluecore.mem.team_context import TeamSearchConfig, build_team_context

    exclude = git_user if settings.team.exclude_self else ""
    return build_team_context(
        pg,
        query=query,
        exclude_origin_user=exclude,
        config=TeamSearchConfig(
            settings=settings.team,
            mode="hybrid",
            embedding_model=settings.embedding_model,
        ),
    )


def handle_team_session_init(
    settings: Any,
    stdin_data: dict[str, Any],
    *,
    get_project: Any,
    get_git_user_name: Any,
    log: Any,
) -> None:
    """UserPromptSubmit: 過去参照プロンプト検出時にチーム横断ベクトル検索を実行する。"""
    from bluecore.mem.search import should_inject_memory

    sync_cfg = settings.sync
    if not settings.team.enabled or not sync_cfg.enabled or not sync_cfg.postgres_url:
        return

    prompt = str(stdin_data.get("prompt", "") or "")
    if not prompt or not should_inject_memory(prompt):
        return

    project = get_project(stdin_data)
    if project in settings.excluded_projects:
        return

    git_user = get_git_user_name()
    query = f"{project} {prompt}".strip() if project else prompt

    try:
        from bluecore.mem.pg_database import PgDatabase
    except Exception as e:
        log.warning("team-session-init モジュール読み込み失敗: %s", e)
        return

    pg = PgDatabase(sync_cfg.postgres_url)
    try:
        if not pg.test_connection():
            log.warning("team-session-init: PostgreSQL 接続失敗")
            return
        ctx = _build_team_session_context(settings, pg, query, git_user)
        if ctx:
            print(json.dumps({"hookEventName": "UserPromptSubmit", "additionalContext": ctx}))
    except Exception as e:
        log.warning("team-session-init 生成失敗: %s", e)
    finally:
        pg.close()
