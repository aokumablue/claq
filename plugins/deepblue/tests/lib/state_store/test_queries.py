"""state store のクエリに対するテスト。"""

from __future__ import annotations

import sqlite3

import pytest

from devgear.lib.state_store import queries as q
from devgear.lib.state_store.migrations import apply_migrations
from devgear.lib.state_store.queries import (
    Decision,
    QueryApi,
    Session,
    SkillRun,
)


@pytest.fixture
def db():
    """マイグレーション適用済みのインメモリ DB を作成する。"""
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn)
    yield conn
    conn.close()


@pytest.fixture
def api(db):
    """QueryApi インスタンスを作成する。"""
    return QueryApi(db)


@pytest.fixture
def sample_session():
    """サンプルの session dict を作成する。"""
    return {
        "id": "session-1",
        "adapterId": "claude-history",
        "harness": "cli",
        "state": "active",
        "repoRoot": "/home/user/project",
        "startedAt": "2024-01-01T10:00:00Z",
        "endedAt": None,
        "snapshot": {"workers": [{"id": "w1"}, {"id": "w2"}]},
    }


@pytest.fixture
def sample_skill_run():
    """サンプルの skill run dict を作成する。"""
    return {
        "id": "run-1",
        "skillId": "tdd-workflow",
        "skillVersion": "0.0.1",
        "sessionId": "session-1",
        "taskDescription": "Implement user auth",
        "outcome": "success",
        "failureReason": None,
        "tokensUsed": 1500,
        "durationMs": 5000,
        "userFeedback": "good",
        "createdAt": "2024-01-01T10:05:00Z",
    }


class TestSessionOperations:
    """session 操作のテスト。"""

    def test_upsert_session_insert(self, api, sample_session):
        """新しい session を挿入できること。"""
        result = api.upsert_session(sample_session)
        assert result is not None
        assert result.id == "session-1"
        assert result.adapter_id == "claude-history"
        assert result.harness == "cli"
        assert result.state == "active"
        assert result.repo_root == "/home/user/project"
        assert result.worker_count == 2

    def test_upsert_session_update(self, api, sample_session):
        """既存の session を更新できること。"""
        api.upsert_session(sample_session)

        # 状態を更新
        sample_session["state"] = "completed"
        sample_session["endedAt"] = "2024-01-01T11:00:00Z"
        result = api.upsert_session(sample_session)

        assert result.state == "completed"
        assert result.ended_at == "2024-01-01T11:00:00Z"

    def test_get_session_by_id(self, api, sample_session):
        """ID で session を取得できること。"""
        api.upsert_session(sample_session)
        result = api.get_session_by_id("session-1")
        assert result is not None
        assert result.id == "session-1"

    def test_get_session_by_id_not_found(self, api):
        """存在しない session では None を返すこと。"""
        result = api.get_session_by_id("nonexistent")
        assert result is None

    def test_list_recent_sessions(self, api, sample_session):
        """最近の session 一覧を取得できること。"""
        api.upsert_session(sample_session)
        sample_session["id"] = "session-2"
        sample_session["startedAt"] = "2024-01-02T10:00:00Z"
        api.upsert_session(sample_session)

        result = api.list_recent_sessions(limit=10)
        assert result["totalCount"] == 2
        assert len(result["sessions"]) == 2
        # 新しい順
        assert result["sessions"][0].id == "session-2"

    def test_get_session_detail(self, api, sample_session, sample_skill_run):
        """skill runs と decisions を含む session 詳細を取得できること。"""
        api.upsert_session(sample_session)
        api.insert_skill_run(sample_skill_run)
        api.insert_decision(
            {
                "id": "dec-1",
                "sessionId": "session-1",
                "title": "Use JWT",
                "rationale": "Standard approach",
                "alternatives": ["Session cookies"],
                "status": "accepted",
            }
        )

        result = api.get_session_detail("session-1")
        assert result is not None
        assert result["session"].id == "session-1"
        assert len(result["workers"]) == 2
        assert len(result["skillRuns"]) == 1
        assert len(result["decisions"]) == 1


class TestSkillRunOperations:
    """skill run 操作のテスト。"""

    def test_insert_skill_run(self, api, sample_session, sample_skill_run):
        """skill run を挿入できること。"""
        api.upsert_session(sample_session)
        result = api.insert_skill_run(sample_skill_run)

        assert result.id == "run-1"
        assert result.skill_id == "tdd-workflow"
        assert result.outcome == "success"
        assert result.tokens_used == 1500

    def test_insert_skill_run_with_failure(self, api, sample_session, sample_skill_run):
        """失敗した skill run を挿入できること。"""
        api.upsert_session(sample_session)
        sample_skill_run["outcome"] = "failure"
        sample_skill_run["failureReason"] = "Test failed"
        result = api.insert_skill_run(sample_skill_run)

        assert result.outcome == "failure"
        assert result.failure_reason == "Test failed"

    def test_insert_skill_run_auto_created_at(self, api, sample_session, sample_skill_run):
        """createdAt 未指定時に自動生成されること。"""
        api.upsert_session(sample_session)
        del sample_skill_run["createdAt"]
        result = api.insert_skill_run(sample_skill_run)

        assert result.created_at is not None


class TestSkillVersionOperations:
    """skill version 操作のテスト。"""

    def test_upsert_skill_version_insert(self, api):
        """skill version を挿入できること。"""
        result = api.upsert_skill_version(
            {
                "skillId": "tdd-workflow",
                "version": "0.0.1",
                "contentHash": "abc123",
                "amendmentReason": None,
                "promotedAt": "2024-01-01T10:00:00Z",
            }
        )

        assert result.skill_id == "tdd-workflow"
        assert result.version == "0.0.1"
        assert result.content_hash == "abc123"

    def test_upsert_skill_version_update(self, api):
        """既存の skill version を更新できること。"""
        api.upsert_skill_version(
            {
                "skillId": "tdd-workflow",
                "version": "0.0.1",
                "contentHash": "abc123",
            }
        )

        result = api.upsert_skill_version(
            {
                "skillId": "tdd-workflow",
                "version": "0.0.1",
                "contentHash": "def456",
                "rolledBackAt": "2024-01-02T10:00:00Z",
            }
        )

        assert result.content_hash == "def456"
        assert result.rolled_back_at == "2024-01-02T10:00:00Z"


class TestDecisionOperations:
    """decision 操作のテスト。"""

    def test_insert_decision(self, api, sample_session):
        """decision を挿入できること。"""
        api.upsert_session(sample_session)
        result = api.insert_decision(
            {
                "id": "dec-1",
                "sessionId": "session-1",
                "title": "Use JWT",
                "rationale": "Industry standard for stateless auth",
                "alternatives": ["Session cookies", "Basic auth"],
                "status": "accepted",
            }
        )

        assert result.id == "dec-1"
        assert result.title == "Use JWT"
        assert result.alternatives == ["Session cookies", "Basic auth"]

    def test_insert_decision_with_supersedes(self, api, sample_session):
        """別の decision を supersede する decision を挿入できること。"""
        api.upsert_session(sample_session)
        api.insert_decision(
            {
                "id": "dec-1",
                "sessionId": "session-1",
                "title": "Use Session cookies",
                "rationale": "Simple approach",
                "alternatives": [],
                "status": "superseded",
            }
        )

        result = api.insert_decision(
            {
                "id": "dec-2",
                "sessionId": "session-1",
                "title": "Use JWT",
                "rationale": "Better for API",
                "alternatives": [],
                "supersedes": "dec-1",
                "status": "accepted",
            }
        )

        assert result.supersedes == "dec-1"


class TestInstallStateOperations:
    """install state 操作のテスト。"""

    def test_upsert_install_state(self, api):
        """install state を挿入できること。"""
        result = api.upsert_install_state(
            {
                "targetId": "claude-home",
                "targetRoot": "/home/user/.claude",
                "profile": "standard",
                "modules": ["tdd", "planning"],
                "operations": [{"op": "copy", "file": "a.md"}],
                "sourceVersion": "0.0.1",
            }
        )

        assert result.target_id == "claude-home"
        assert result.profile == "standard"
        assert result.modules == ["tdd", "planning"]
        assert result.module_count == 2
        assert result.operation_count == 1
        assert result.status == "healthy"

    def test_upsert_install_state_warning_status(self, api):
        """source_version 欠落時に warning 状態になること。"""
        result = api.upsert_install_state(
            {
                "targetId": "claude-home",
                "targetRoot": "/home/user/.claude",
                "modules": [],
                "operations": [],
            }
        )

        assert result.status == "warning"


class TestGovernanceEventOperations:
    """governance event 操作のテスト。"""

    def test_insert_governance_event(self, api, sample_session):
        """governance event を挿入できること。"""
        api.upsert_session(sample_session)
        result = api.insert_governance_event(
            {
                "id": "event-1",
                "sessionId": "session-1",
                "eventType": "security_warning",
                "payload": {"message": "Potential vulnerability"},
            }
        )

        assert result.id == "event-1"
        assert result.event_type == "security_warning"
        assert result.payload == {"message": "Potential vulnerability"}

    def test_insert_governance_event_resolved(self, api, sample_session):
        """解決済みの governance event を挿入できること。"""
        api.upsert_session(sample_session)
        result = api.insert_governance_event(
            {
                "id": "event-1",
                "sessionId": "session-1",
                "eventType": "security_warning",
                "payload": {"message": "Resolved"},
                "resolvedAt": "2024-01-01T11:00:00Z",
                "resolution": "Fixed in commit abc123",
            }
        )

        assert result.resolved_at == "2024-01-01T11:00:00Z"
        assert result.resolution == "Fixed in commit abc123"


class TestGetStatus:
    """get_status のテスト。"""

    def test_get_status_empty_db(self, api):
        """空の DB に対するステータスを返すこと。"""
        result = api.get_status()

        assert "generatedAt" in result
        assert result["activeSessions"]["activeCount"] == 0
        assert result["skillRuns"]["summary"]["totalCount"] == 0
        assert result["installHealth"]["status"] == "missing"
        assert result["governance"]["pendingCount"] == 0

    def test_get_status_with_data(self, api, sample_session, sample_skill_run):
        """包括的なステータスを返すこと。"""
        api.upsert_session(sample_session)
        api.insert_skill_run(sample_skill_run)
        api.upsert_install_state(
            {
                "targetId": "claude-home",
                "targetRoot": "/home/user/.claude",
                "modules": ["tdd"],
                "operations": [],
                "sourceVersion": "0.0.1",
            }
        )
        api.insert_governance_event(
            {
                "id": "event-1",
                "sessionId": "session-1",
                "eventType": "warning",
                "payload": {},
            }
        )

        result = api.get_status()

        assert result["activeSessions"]["activeCount"] == 1
        assert result["skillRuns"]["summary"]["totalCount"] == 1
        assert result["skillRuns"]["summary"]["successCount"] == 1
        assert result["installHealth"]["status"] == "healthy"
        assert result["governance"]["pendingCount"] == 1

    def test_get_status_skill_run_rates(self, api, sample_session, sample_skill_run):
        """成功率と失敗率を計算できること。"""
        api.upsert_session(sample_session)

        # 成功を追加
        api.insert_skill_run(sample_skill_run)

        # 失敗を追加
        sample_skill_run["id"] = "run-2"
        sample_skill_run["outcome"] = "failure"
        api.insert_skill_run(sample_skill_run)

        result = api.get_status()
        summary = result["skillRuns"]["summary"]

        assert summary["successCount"] == 1
        assert summary["failureCount"] == 1
        assert summary["successRate"] == 50.0
        assert summary["failureRate"] == 50.0


class TestDataclassModels:
    """データクラスモデルのテスト。"""

    def test_session_dataclass(self):
        """Session データクラスが期待属性を持つこと。"""
        session = Session(
            id="test",
            adapter_id="adapter",
            harness="cli",
            state="active",
            repo_root="/path",
            started_at="2024-01-01",
            ended_at=None,
            snapshot={},
            worker_count=0,
        )
        assert session.id == "test"
        assert session.adapter_id == "adapter"

    def test_skill_run_dataclass(self):
        """SkillRun データクラスが期待属性を持つこと。"""
        run = SkillRun(
            id="test",
            skill_id="skill",
            skill_version="1.0",
            session_id="session",
            task_description="task",
            outcome="success",
            failure_reason=None,
            tokens_used=100,
            duration_ms=1000,
            user_feedback=None,
            created_at="2024-01-01",
        )
        assert run.id == "test"
        assert run.outcome == "success"

    def test_decision_dataclass(self):
        """Decision データクラスが期待属性を持つこと。"""
        dec = Decision(
            id="test",
            session_id="session",
            title="Test",
            rationale="Reason",
            alternatives=[],
            supersedes=None,
            status="accepted",
            created_at="2024-01-01",
        )
        assert dec.id == "test"
        assert dec.status == "accepted"


class TestJsonHandling:
    """JSON 取り扱いのテスト。"""

    def test_handles_complex_json_in_snapshot(self, api, sample_session):
        """session snapshot 内の複雑な JSON を扱えること。"""
        sample_session["snapshot"] = {
            "workers": [{"id": "w1", "nested": {"key": "value"}}],
            "config": {"debug": True},
        }
        api.upsert_session(sample_session)
        result = api.get_session_by_id("session-1")

        assert result.snapshot["config"]["debug"] is True
        assert result.snapshot["workers"][0]["nested"]["key"] == "value"

    def test_handles_empty_json(self, api, sample_session):
        """空の JSON オブジェクトを扱えること。"""
        sample_session["snapshot"] = {}
        api.upsert_session(sample_session)
        result = api.get_session_by_id("session-1")

        assert result.snapshot == {}
        assert result.worker_count == 0


def test_internal_json_and_classification_helpers_cover_error_paths() -> None:
    assert q._parse_json_column(None, {"fallback": True}) == {"fallback": True}
    assert q._parse_json_column("", {"fallback": True}) == {"fallback": True}
    assert q._classify_outcome("maybe") == "unknown"

    with pytest.raises(ValueError):
        q._stringify_json({"bad": {1}}, "bad")


def test_query_api_covers_remaining_empty_and_none_branches(api, sample_session):
    assert api.get_session_detail("missing") is None

    api.upsert_session(sample_session)
    decision = api.insert_decision(
        {
            "id": "dec-no-alternatives",
            "sessionId": "session-1",
            "title": "Use cache",
            "rationale": "Needed",
            "status": "accepted",
        }
    )
    assert decision.alternatives == []

    record = api.upsert_install_state(
        {
            "targetId": "target-1",
            "targetRoot": "/root",
            "modules": None,
            "operations": None,
            "sourceVersion": "0.0.1",
        }
    )
    assert record.module_count == 0
    assert record.operation_count == 0

    api.insert_skill_run(
        {
            "id": "run-unknown",
            "skillId": "tdd-workflow",
            "skillVersion": "0.0.1",
            "sessionId": "session-1",
            "taskDescription": "Unknown outcome",
            "outcome": "mystery",
        }
    )
    status = api.get_status()
    assert status["skillRuns"]["summary"]["unknownCount"] >= 1
