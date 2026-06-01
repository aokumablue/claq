"""
state store クエリ API。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import sqlite3


ACTIVE_SESSION_STATES = ["active", "running", "idle"]
SUCCESS_OUTCOMES = frozenset(["success", "succeeded", "passed"])
FAILURE_OUTCOMES = frozenset(["failure", "failed", "error"])


def _parse_json_column(value: str | None, fallback: Any = None) -> Any:
    """JSON カラム値を解析する。"""
    if value is None or value == "":
        return fallback
    return json.loads(value)


def _stringify_json(value: Any, label: str) -> str:
    """値を JSON 文字列に変換する。"""
    try:
        return json.dumps(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"Failed to serialize {label}: {e}") from e


def _classify_outcome(outcome: str | None) -> str:
    """outcome を success・failure・unknown に分類する。"""
    normalized = str(outcome or "").lower()
    if normalized in SUCCESS_OUTCOMES:
        return "success"
    if normalized in FAILURE_OUTCOMES:
        return "failure"
    return "unknown"


def _to_percent(numerator: int, denominator: int) -> float | None:
    """百分率を計算する。"""
    if denominator == 0:
        return None
    return round((numerator / denominator) * 100, 1)


@dataclass
class Session:
    """セッションレコード。"""

    id: str
    adapter_id: str
    harness: str
    state: str
    repo_root: str | None
    started_at: str | None
    ended_at: str | None
    snapshot: dict
    worker_count: int


@dataclass
class SkillRun:
    """スキル実行レコード。"""

    id: str
    skill_id: str
    skill_version: str
    session_id: str
    task_description: str
    outcome: str
    failure_reason: str | None
    tokens_used: int | None
    duration_ms: int | None
    user_feedback: str | None
    created_at: str


@dataclass
class SkillVersion:
    """スキルバージョンレコード。"""

    skill_id: str
    version: str
    content_hash: str
    amendment_reason: str | None
    promoted_at: str | None
    rolled_back_at: str | None


@dataclass
class Decision:
    """意思決定レコード。"""

    id: str
    session_id: str
    title: str
    rationale: str
    alternatives: list
    supersedes: str | None
    status: str
    created_at: str


@dataclass
class InstallStateRecord:
    """インストール状態レコード。"""

    target_id: str
    target_root: str
    profile: str | None
    modules: list
    operations: list
    installed_at: str
    source_version: str | None
    module_count: int
    operation_count: int
    status: str


@dataclass
class GovernanceEvent:
    """ガバナンスイベントレコード。"""

    id: str
    session_id: str | None
    event_type: str
    payload: Any
    resolved_at: str | None
    resolution: str | None
    created_at: str


def _map_session_row(row: tuple) -> Session:
    """データベース行をセッションにマッピングする。"""
    snapshot = _parse_json_column(row[7], {})
    workers = snapshot.get("workers", []) if isinstance(snapshot, dict) else []
    return Session(
        id=row[0],
        adapter_id=row[1],
        harness=row[2],
        state=row[3],
        repo_root=row[4],
        started_at=row[5],
        ended_at=row[6],
        snapshot=snapshot,
        worker_count=len(workers) if isinstance(workers, list) else 0,
    )


def _map_skill_run_row(row: tuple) -> SkillRun:
    """データベース行を SkillRun にマッピングする。"""
    return SkillRun(
        id=row[0],
        skill_id=row[1],
        skill_version=row[2],
        session_id=row[3],
        task_description=row[4],
        outcome=row[5],
        failure_reason=row[6],
        tokens_used=row[7],
        duration_ms=row[8],
        user_feedback=row[9],
        created_at=row[10],
    )


def _map_skill_version_row(row: tuple) -> SkillVersion:
    """データベース行を SkillVersion にマッピングする。"""
    return SkillVersion(
        skill_id=row[0],
        version=row[1],
        content_hash=row[2],
        amendment_reason=row[3],
        promoted_at=row[4],
        rolled_back_at=row[5],
    )


def _map_decision_row(row: tuple) -> Decision:
    """データベース行を Decision にマッピングする。"""
    return Decision(
        id=row[0],
        session_id=row[1],
        title=row[2],
        rationale=row[3],
        alternatives=_parse_json_column(row[4], []),
        supersedes=row[5],
        status=row[6],
        created_at=row[7],
    )


def _map_install_state_row(row: tuple) -> InstallStateRecord:
    """データベース行を InstallStateRecord にマッピングする。"""
    modules = _parse_json_column(row[3], [])
    operations = _parse_json_column(row[4], [])
    status = "healthy" if row[6] and row[5] else "warning"
    return InstallStateRecord(
        target_id=row[0],
        target_root=row[1],
        profile=row[2],
        modules=modules,
        operations=operations,
        installed_at=row[5],
        source_version=row[6],
        module_count=len(modules) if isinstance(modules, list) else 0,
        operation_count=len(operations) if isinstance(operations, list) else 0,
        status=status,
    )


def _map_governance_event_row(row: tuple) -> GovernanceEvent:
    """データベース行を GovernanceEvent にマッピングする。"""
    return GovernanceEvent(
        id=row[0],
        session_id=row[1],
        event_type=row[2],
        payload=_parse_json_column(row[3]),
        resolved_at=row[4],
        resolution=row[5],
        created_at=row[6],
    )


class QueryApi:
    """state store 用クエリ API。"""

    def __init__(self, conn: sqlite3.Connection):
        """SQLite 接続を受け取りクエリ API を初期化する。"""
        self._conn = conn

    def get_session_by_id(self, session_id: str) -> Session | None:
        """ID でセッションを取得する。"""
        cursor = self._conn.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (session_id,),
        )
        row = cursor.fetchone()
        return _map_session_row(row) if row else None

    def list_recent_sessions(self, limit: int = 10) -> dict:
        """最近のセッション一覧を取得する。"""
        cursor = self._conn.execute("SELECT COUNT(*) FROM sessions")
        total_count = cursor.fetchone()[0]

        cursor = self._conn.execute(
            """
            SELECT * FROM sessions
            ORDER BY COALESCE(started_at, ended_at, '') DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        )
        sessions = [_map_session_row(row) for row in cursor.fetchall()]

        return {"totalCount": total_count, "sessions": sessions}

    def get_session_detail(self, session_id: str) -> dict | None:
        """セッションの詳細情報を取得する。"""
        session = self.get_session_by_id(session_id)
        if not session:
            return None

        workers = session.snapshot.get("workers", []) if isinstance(session.snapshot, dict) else []

        cursor = self._conn.execute(
            """
            SELECT * FROM skill_runs
            WHERE session_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (session_id,),
        )
        skill_runs = [_map_skill_run_row(row) for row in cursor.fetchall()]

        cursor = self._conn.execute(
            """
            SELECT * FROM decisions
            WHERE session_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (session_id,),
        )
        decisions = [_map_decision_row(row) for row in cursor.fetchall()]

        return {
            "session": session,
            "workers": workers,
            "skillRuns": skill_runs,
            "decisions": decisions,
        }

    def upsert_session(self, session: dict) -> Session | None:
        """セッションを挿入または更新する。"""
        normalized = {
            "id": session["id"],
            "adapter_id": session.get("adapterId"),
            "harness": session.get("harness"),
            "state": session.get("state"),
            "repo_root": session.get("repoRoot"),
            "started_at": session.get("startedAt"),
            "ended_at": session.get("endedAt"),
            "snapshot": _stringify_json(session.get("snapshot", {}), "session.snapshot"),
        }

        self._conn.execute(
            """
            INSERT INTO sessions (id, adapter_id, harness, state, repo_root, started_at, ended_at, snapshot)
            VALUES (:id, :adapter_id, :harness, :state, :repo_root, :started_at, :ended_at, :snapshot)
            ON CONFLICT(id) DO UPDATE SET
                adapter_id = excluded.adapter_id,
                harness = excluded.harness,
                state = excluded.state,
                repo_root = excluded.repo_root,
                started_at = excluded.started_at,
                ended_at = excluded.ended_at,
                snapshot = excluded.snapshot
            """,
            normalized,
        )
        self._conn.commit()
        return self.get_session_by_id(session["id"])

    def insert_skill_run(self, skill_run: dict) -> SkillRun:
        """スキル実行を挿入する。"""
        now = datetime.now().isoformat()
        normalized = {
            "id": skill_run["id"],
            "skill_id": skill_run["skillId"],
            "skill_version": skill_run["skillVersion"],
            "session_id": skill_run["sessionId"],
            "task_description": skill_run["taskDescription"],
            "outcome": skill_run["outcome"],
            "failure_reason": skill_run.get("failureReason"),
            "tokens_used": skill_run.get("tokensUsed"),
            "duration_ms": skill_run.get("durationMs"),
            "user_feedback": skill_run.get("userFeedback"),
            "created_at": skill_run.get("createdAt", now),
        }

        self._conn.execute(
            """
            INSERT INTO skill_runs (
                id, skill_id, skill_version, session_id, task_description,
                outcome, failure_reason, tokens_used, duration_ms, user_feedback, created_at
            ) VALUES (
                :id, :skill_id, :skill_version, :session_id, :task_description,
                :outcome, :failure_reason, :tokens_used, :duration_ms, :user_feedback, :created_at
            )
            ON CONFLICT(id) DO UPDATE SET
                skill_id = excluded.skill_id,
                skill_version = excluded.skill_version,
                session_id = excluded.session_id,
                task_description = excluded.task_description,
                outcome = excluded.outcome,
                failure_reason = excluded.failure_reason,
                tokens_used = excluded.tokens_used,
                duration_ms = excluded.duration_ms,
                user_feedback = excluded.user_feedback,
                created_at = excluded.created_at
            """,
            normalized,
        )
        self._conn.commit()

        return SkillRun(**dict(normalized.items()))

    def upsert_skill_version(self, skill_version: dict) -> SkillVersion | None:
        """スキルバージョンを挿入または更新する。"""
        normalized = {
            "skill_id": skill_version["skillId"],
            "version": skill_version["version"],
            "content_hash": skill_version["contentHash"],
            "amendment_reason": skill_version.get("amendmentReason"),
            "promoted_at": skill_version.get("promotedAt"),
            "rolled_back_at": skill_version.get("rolledBackAt"),
        }

        self._conn.execute(
            """
            INSERT INTO skill_versions (
                skill_id, version, content_hash, amendment_reason, promoted_at, rolled_back_at
            ) VALUES (
                :skill_id, :version, :content_hash, :amendment_reason, :promoted_at, :rolled_back_at
            )
            ON CONFLICT(skill_id, version) DO UPDATE SET
                content_hash = excluded.content_hash,
                amendment_reason = excluded.amendment_reason,
                promoted_at = excluded.promoted_at,
                rolled_back_at = excluded.rolled_back_at
            """,
            normalized,
        )
        self._conn.commit()

        cursor = self._conn.execute(
            "SELECT * FROM skill_versions WHERE skill_id = ? AND version = ?",
            (normalized["skill_id"], normalized["version"]),
        )
        row = cursor.fetchone()
        return _map_skill_version_row(row) if row else None

    def insert_decision(self, decision: dict) -> Decision:
        """意思決定を挿入する。"""
        now = datetime.now().isoformat()
        alternatives = decision.get("alternatives")
        if alternatives is None:
            alternatives = []

        normalized = {
            "id": decision["id"],
            "session_id": decision["sessionId"],
            "title": decision["title"],
            "rationale": decision["rationale"],
            "alternatives": _stringify_json(alternatives, "decision.alternatives"),
            "supersedes": decision.get("supersedes"),
            "status": decision["status"],
            "created_at": decision.get("createdAt", now),
        }

        self._conn.execute(
            """
            INSERT INTO decisions (
                id, session_id, title, rationale, alternatives, supersedes, status, created_at
            ) VALUES (
                :id, :session_id, :title, :rationale, :alternatives, :supersedes, :status, :created_at
            )
            ON CONFLICT(id) DO UPDATE SET
                session_id = excluded.session_id,
                title = excluded.title,
                rationale = excluded.rationale,
                alternatives = excluded.alternatives,
                supersedes = excluded.supersedes,
                status = excluded.status,
                created_at = excluded.created_at
            """,
            normalized,
        )
        self._conn.commit()

        return Decision(
            id=normalized["id"],
            session_id=normalized["session_id"],
            title=normalized["title"],
            rationale=normalized["rationale"],
            alternatives=alternatives,
            supersedes=normalized["supersedes"],
            status=normalized["status"],
            created_at=normalized["created_at"],
        )

    def _normalize_install_state(self, install_state: dict) -> tuple[dict, list, list]:
        """インストール状態辞書を正規化し、(normalized, modules, operations) を返す。"""
        now = datetime.now().isoformat()
        modules = install_state.get("modules") or []
        operations = install_state.get("operations") or []
        normalized = {
            "target_id": install_state["targetId"],
            "target_root": install_state["targetRoot"],
            "profile": install_state.get("profile"),
            "modules": _stringify_json(modules, "installState.modules"),
            "operations": _stringify_json(operations, "installState.operations"),
            "installed_at": install_state.get("installedAt", now),
            "source_version": install_state.get("sourceVersion"),
        }
        return normalized, modules, operations

    def _execute_upsert_install_state(self, normalized: dict) -> None:
        """install_state テーブルに upsert クエリを実行する。"""
        self._conn.execute(
            """
            INSERT INTO install_state (
                target_id, target_root, profile, modules, operations, installed_at, source_version
            ) VALUES (
                :target_id, :target_root, :profile, :modules, :operations, :installed_at, :source_version
            )
            ON CONFLICT(target_id, target_root) DO UPDATE SET
                profile = excluded.profile,
                modules = excluded.modules,
                operations = excluded.operations,
                installed_at = excluded.installed_at,
                source_version = excluded.source_version
            """,
            normalized,
        )
        self._conn.commit()

    def upsert_install_state(self, install_state: dict) -> InstallStateRecord:
        """インストール状態を挿入または更新する。"""
        normalized, modules, operations = self._normalize_install_state(install_state)
        self._execute_upsert_install_state(normalized)
        status = "healthy" if normalized["source_version"] and normalized["installed_at"] else "warning"
        return InstallStateRecord(
            target_id=normalized["target_id"],
            target_root=normalized["target_root"],
            profile=normalized["profile"],
            modules=modules,
            operations=operations,
            installed_at=normalized["installed_at"],
            source_version=normalized["source_version"],
            module_count=len(modules),
            operation_count=len(operations),
            status=status,
        )

    def insert_governance_event(self, event: dict) -> GovernanceEvent:
        """ガバナンスイベントを挿入する。"""
        now = datetime.now().isoformat()
        normalized = {
            "id": event["id"],
            "session_id": event.get("sessionId"),
            "event_type": event["eventType"],
            "payload": _stringify_json(event.get("payload"), "governanceEvent.payload"),
            "resolved_at": event.get("resolvedAt"),
            "resolution": event.get("resolution"),
            "created_at": event.get("createdAt", now),
        }

        self._conn.execute(
            """
            INSERT INTO governance_events (
                id, session_id, event_type, payload, resolved_at, resolution, created_at
            ) VALUES (
                :id, :session_id, :event_type, :payload, :resolved_at, :resolution, :created_at
            )
            ON CONFLICT(id) DO UPDATE SET
                session_id = excluded.session_id,
                event_type = excluded.event_type,
                payload = excluded.payload,
                resolved_at = excluded.resolved_at,
                resolution = excluded.resolution,
                created_at = excluded.created_at
            """,
            normalized,
        )
        self._conn.commit()

        return GovernanceEvent(
            id=normalized["id"],
            session_id=normalized["session_id"],
            event_type=normalized["event_type"],
            payload=event.get("payload"),
            resolved_at=normalized["resolved_at"],
            resolution=normalized["resolution"],
            created_at=normalized["created_at"],
        )

    def _query_active_sessions(self, active_limit: int) -> tuple[int, list]:
        """アクティブセッションの件数と一覧を取得する。"""
        cursor = self._conn.execute(
            """
            SELECT COUNT(*) FROM sessions
            WHERE ended_at IS NULL AND state IN ('active', 'running', 'idle')
            """
        )
        active_count = cursor.fetchone()[0]
        cursor = self._conn.execute(
            """
            SELECT * FROM sessions
            WHERE ended_at IS NULL AND state IN ('active', 'running', 'idle')
            ORDER BY COALESCE(started_at, ended_at, '') DESC, id DESC
            LIMIT ?
            """,
            (active_limit,),
        )
        active_sessions = [_map_session_row(row) for row in cursor.fetchall()]
        return active_count, active_sessions

    def _query_skill_summary(self, recent_skill_run_limit: int) -> tuple[list, dict]:
        """最近のスキル実行一覧と集計サマリーを取得する。"""
        cursor = self._conn.execute(
            """
            SELECT * FROM skill_runs
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (recent_skill_run_limit,),
        )
        recent_skill_runs = [_map_skill_run_row(row) for row in cursor.fetchall()]
        skill_summary: dict = {
            "totalCount": len(recent_skill_runs),
            "knownCount": 0,
            "successCount": 0,
            "failureCount": 0,
            "unknownCount": 0,
            "successRate": None,
            "failureRate": None,
        }
        for run in recent_skill_runs:
            classification = _classify_outcome(run.outcome)
            if classification == "success":
                skill_summary["successCount"] += 1
                skill_summary["knownCount"] += 1
            elif classification == "failure":
                skill_summary["failureCount"] += 1
                skill_summary["knownCount"] += 1
            else:
                skill_summary["unknownCount"] += 1
        skill_summary["successRate"] = _to_percent(skill_summary["successCount"], skill_summary["knownCount"])
        skill_summary["failureRate"] = _to_percent(skill_summary["failureCount"], skill_summary["knownCount"])
        return recent_skill_runs, skill_summary

    def _query_install_health(self) -> dict:
        """インストール状態の健全性サマリーを取得する。"""
        cursor = self._conn.execute(
            """
            SELECT * FROM install_state
            ORDER BY installed_at DESC, target_id ASC
            """
        )
        installations = [_map_install_state_row(row) for row in cursor.fetchall()]
        return {
            "status": "missing"
            if not installations
            else ("warning" if any(i.status == "warning" for i in installations) else "healthy"),
            "totalCount": len(installations),
            "healthyCount": sum(1 for i in installations if i.status == "healthy"),
            "warningCount": sum(1 for i in installations if i.status == "warning"),
            "installations": installations,
        }

    def _query_pending_governance(self, pending_limit: int) -> tuple[int, list]:
        """未解決ガバナンスイベントの件数と一覧を取得する。"""
        cursor = self._conn.execute("SELECT COUNT(*) FROM governance_events WHERE resolved_at IS NULL")
        pending_count = cursor.fetchone()[0]
        cursor = self._conn.execute(
            """
            SELECT * FROM governance_events
            WHERE resolved_at IS NULL
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (pending_limit,),
        )
        pending_events = [_map_governance_event_row(row) for row in cursor.fetchall()]
        return pending_count, pending_events

    def get_status(
        self,
        *,
        active_limit: int = 5,
        recent_skill_run_limit: int = 20,
        pending_limit: int = 5,
    ) -> dict:
        """全体ステータスを取得する。"""
        active_count, active_sessions = self._query_active_sessions(active_limit)
        recent_skill_runs, skill_summary = self._query_skill_summary(recent_skill_run_limit)
        install_health = self._query_install_health()
        pending_count, pending_events = self._query_pending_governance(pending_limit)
        return {
            "generatedAt": datetime.now().isoformat(),
            "activeSessions": {
                "activeCount": active_count,
                "sessions": active_sessions,
            },
            "skillRuns": {
                "windowSize": recent_skill_run_limit,
                "summary": skill_summary,
                "recent": recent_skill_runs,
            },
            "installHealth": install_health,
            "governance": {
                "pendingCount": pending_count,
                "events": pending_events,
            },
        }


__all__ = [
    "ACTIVE_SESSION_STATES",
    "FAILURE_OUTCOMES",
    "SUCCESS_OUTCOMES",
    "Decision",
    "GovernanceEvent",
    "InstallStateRecord",
    "QueryApi",
    "Session",
    "SkillRun",
    "SkillVersion",
]
