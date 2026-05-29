"""skill_evolution.tracker のテスト。"""

from __future__ import annotations

import pytest

from devgear.lib.skill_evolution import tracker as tracker


def test_normalize_execution_record_accepts_snake_case(now):
    """正規化されたレコードで 0 の値が保持されること。"""
    record = tracker.normalize_execution_record(
        {
            "skill_id": "alpha",
            "skill_version": "v1",
            "task_description": "Run tests",
            "outcome": "success",
            "tokens_used": 0,
            "duration_ms": 0,
            "user_feedback": "accepted",
            "recorded_at": now,
        }
    )

    assert record["skill_id"] == "alpha"
    assert record["tokens_used"] == 0
    assert record["duration_ms"] == 0


def test_normalize_execution_record_defaults_recorded_at_and_rejects_boolean_numbers(now, monkeypatch):
    """recorded_at の既定値と boolean 数値の拒否を確認する。"""
    monkeypatch.setattr(tracker, "utc_now_iso", lambda: now)

    record = tracker.normalize_execution_record(
        {
            "skill_id": "alpha",
            "skill_version": "v1",
            "task_description": "Run tests",
            "outcome": "success",
            "tokens_used": "1.5",
            "duration_ms": 2,
            "user_feedback": None,
        }
    )

    assert record["recorded_at"] == now
    assert record["tokens_used"] == 1.5

    with pytest.raises(ValueError, match="tokens_used must be a number"):
        tracker.normalize_execution_record(
            {
                "skill_id": "alpha",
                "skill_version": "v1",
                "task_description": "Run tests",
                "outcome": "success",
                "tokens_used": True,
                "recorded_at": now,
            }
        )


def test_to_nullable_number_and_normalize_validation_edges(now):
    with pytest.raises(ValueError, match="tokens_used must be a number"):
        tracker.to_nullable_number("abc", "tokens_used")

    with pytest.raises(ValueError, match="tokens_used must be a number"):
        tracker.to_nullable_number(float("nan"), "tokens_used")

    with pytest.raises(ValueError, match="skill execution payload must be an object"):
        tracker.normalize_execution_record(["not", "a", "dict"])

    with pytest.raises(ValueError, match="user_feedback must be accepted, corrected, rejected, or null"):
        tracker.normalize_execution_record(
            {
                "skill_id": "alpha",
                "skill_version": "v1",
                "task_description": "Run tests",
                "outcome": "success",
                "user_feedback": "wrong",
                "recorded_at": now,
            }
        )

    with pytest.raises(ValueError, match="recorded_at must be an ISO timestamp"):
        tracker.normalize_execution_record(
            {
                "skill_id": "alpha",
                "skill_version": "v1",
                "task_description": "Run tests",
                "outcome": "success",
                "recorded_at": "not-a-timestamp",
            }
        )


def test_normalize_execution_record_accepts_camel_case(now):
    """CamelCase 入力も正規化されること。"""
    record = tracker.normalize_execution_record(
        {
            "skillId": "beta",
            "skillVersion": "v2",
            "taskAttempted": "Fix bug",
            "outcome": "partial",
            "failureReason": "flaky",
            "tokensUsed": 10,
            "durationMs": 20,
            "userFeedback": "corrected",
            "recordedAt": now,
        }
    )

    assert record["skill_id"] == "beta"
    assert record["task_description"] == "Fix bug"
    assert record["failure_reason"] == "flaky"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {"skill_id": "", "skill_version": "v1", "task_description": "x", "outcome": "success"},
            "skill_id is required",
        ),
        (
            {"skill_id": "x", "skill_version": "", "task_description": "x", "outcome": "success"},
            "skill_version is required",
        ),
        (
            {"skill_id": "x", "skill_version": "v1", "task_description": "", "outcome": "success"},
            "task_description is required",
        ),
        (
            {"skill_id": "x", "skill_version": "v1", "task_description": "x", "outcome": "bad"},
            "outcome must be one of success, failure, or partial",
        ),
    ],
)
def test_normalize_execution_record_rejects_invalid_payload(payload, message):
    """不正な実行ペイロードは例外になること。"""
    with pytest.raises(ValueError, match=message):
        tracker.normalize_execution_record(payload)


def test_record_and_read_jsonl(skill_env):
    """レコードが JSONL 保存を通じて往復できること。"""
    result = tracker.record_skill_execution(
        {
            "skill_id": "alpha",
            "skill_version": "v1",
            "task_description": "Run tests",
            "outcome": "success",
            "recorded_at": "2026-03-15T11:00:00.000Z",
        },
        runs_file_path=skill_env["runs_file"],
    )

    assert result["storage"] == "jsonl"
    records = tracker.read_skill_execution_records(runs_file_path=skill_env["runs_file"])
    assert len(records) == 1
    assert records[0]["skill_id"] == "alpha"


def test_record_skill_execution_uses_state_store(now):
    """state-store アダプターがある場合はそれを使用すること。"""

    class StateStore:
        def __init__(self):
            self.payload = None

        def insert_skill_run(self, payload):
            self.payload = payload
            return {"ok": True}

    store = StateStore()
    result = tracker.record_skill_execution(
        {
            "skill_id": "beta",
            "skill_version": "v2",
            "task_description": "Import skill",
            "outcome": "partial",
            "recorded_at": now,
        },
        state_store=store,
    )

    assert result["storage"] == "state-store"
    assert store.payload["skillId"] == "beta"
    assert store.payload["taskDescription"] == "Import skill"


def test_record_skill_execution_supports_alternate_state_store_methods(now):
    """state-store の別メソッド名も吸収できること。"""

    class RecordStore:
        def __init__(self) -> None:
            self.payload = None

        def recordSkillExecution(self, payload):  # noqa: N802
            self.payload = payload
            return {"ok": "record"}

    class InsertStore:
        def __init__(self) -> None:
            self.payload = None

        def insertSkillRun(self, payload):  # noqa: N802
            self.payload = payload
            return {"ok": "insert"}

    record_store = RecordStore()
    insert_store = InsertStore()

    record_result = tracker.record_skill_execution(
        {
            "skill_id": "beta",
            "skill_version": "v2",
            "task_description": "Record",
            "outcome": "partial",
            "recorded_at": now,
        },
        state_store=record_store,
    )
    insert_result = tracker.record_skill_execution(
        {
            "skill_id": "gamma",
            "skill_version": "v3",
            "task_description": "Insert",
            "outcome": "failure",
            "recorded_at": now,
        },
        state_store=insert_store,
    )

    assert record_result["result"] == {"ok": "record"}
    assert record_store.payload["skill_version"] == "v2"
    assert insert_result["result"] == {"ok": "insert"}
    assert insert_store.payload["skillId"] == "gamma"


def test_read_jsonl_skips_malformed_rows(skill_env):
    """壊れた JSONL 行は無視されること。"""
    skill_env["runs_file"].write_text(
        '{"skill_id":"alpha","skill_version":"v1","task_description":"ok","outcome":"success","recorded_at":"2026-03-15T11:00:00.000Z"}\n'
        "\n"
        "{bad-json}\n",
        encoding="utf-8",
    )

    records = tracker.read_jsonl(skill_env["runs_file"])
    assert len(records) == 1
    assert records[0]["skill_id"] == "alpha"


def test_get_runs_file_path_defaults_to_devgear(monkeypatch, tmp_path):
    """既定の runs ファイルパスが ~/.devgear/state/ 配下になること。"""
    monkeypatch.setattr(tracker, "get_devgear_dir", lambda: tmp_path / ".devgear")
    path = tracker.get_runs_file_path()
    assert path.endswith("skill-runs.jsonl")
    assert str(tmp_path / ".devgear") in path


def test_read_skill_execution_records_supports_state_store_read_methods(skill_env):
    """state-store の read/list 系メソッドを順に利用できること。"""

    class ReadStore:
        def readSkillExecutionRecords(self):  # noqa: N802
            return [{"skill_id": "alpha"}]

    class ListStore:
        def listSkillExecutionRecords(self):  # noqa: N802
            return [{"skill_id": "beta"}]

    assert tracker.read_skill_execution_records(state_store=ReadStore()) == [{"skill_id": "alpha"}]
    assert tracker.read_skill_execution_records(state_store=ListStore()) == [{"skill_id": "beta"}]
    assert tracker.read_skill_execution_records(runs_file_path=skill_env["runs_file"]) == []


def test_get_runs_file_path_accepts_explicit_path(tmp_path):
    """明示的な runs ファイルパスを返せること。"""
    path = tracker.get_runs_file_path(runs_file_path=tmp_path / "custom.jsonl")
    assert path == str((tmp_path / "custom.jsonl").resolve())
