"""devgear.lib.inspection のテスト。"""

from __future__ import annotations

import pytest

from devgear.lib import inspection as inspection


def make_skill_run(**overrides):
    """正規化済みの失敗レコードを作成する。"""
    run = {
        "id": overrides.get("id", "run-1"),
        "skillId": overrides.get("skillId", "skill-a"),
        "skillVersion": overrides.get("skillVersion", "0.0.1"),
        "sessionId": overrides.get("sessionId", "session-1"),
        "taskDescription": overrides.get("taskDescription", "test task"),
        "outcome": overrides.get("outcome", "failure"),
        "failureReason": overrides.get("failureReason", "generic error"),
        "tokensUsed": overrides.get("tokensUsed", 500),
        "durationMs": overrides.get("durationMs", 1000),
        "userFeedback": overrides.get("userFeedback"),
        "createdAt": overrides.get("createdAt", "2026-03-15T08:00:00.000Z"),
    }
    run.update(overrides)
    return run


def test_normalize_failure_reason_strips_metadata():
    """タイムスタンプ、UUID、パスが正規化されること。"""
    normalized = inspection.normalize_failure_reason(
        "Error at 2026-03-15T08:00:00.000Z for id 550e8400-e29b-41d4-a716-446655440000 in /usr/local/bin/node"
    )
    assert "<timestamp>" in normalized
    assert "<uuid>" in normalized
    assert "<path>" in normalized


def test_normalize_failure_reason_unknown_for_non_string() -> None:
    assert inspection.normalize_failure_reason(None) == "unknown"
    assert inspection.normalize_failure_reason(123) == "unknown"


def test_group_failures_groups_by_skill_and_reason():
    """失敗がスキルと正規化済み理由ごとにグループ化されること。"""
    groups = inspection.group_failures(
        [
            make_skill_run(id="r1", skillId="skill-a", failureReason="timeout"),
            make_skill_run(id="r2", skillId="skill-a", failureReason="timeout"),
            make_skill_run(id="r3", skillId="skill-b", failureReason="parse error"),
            make_skill_run(id="r4", skillId="skill-a", outcome="success"),
        ]
    )

    assert len(groups) == 2
    assert groups["skill-a::timeout"]["runs"][0]["id"] == "r1"
    assert groups["skill-b::parse error"]["runs"][0]["id"] == "r3"


def test_detect_patterns_threshold_and_sorting():
    """パターンが閾値を満たし、件数→新しさの順で並ぶこと。"""
    patterns = inspection.detect_patterns(
        [
            make_skill_run(id="r1", skillId="skill-a", failureReason="timeout", createdAt="2026-03-15T08:00:00Z"),
            make_skill_run(id="r2", skillId="skill-a", failureReason="timeout", createdAt="2026-03-15T08:01:00Z"),
            make_skill_run(id="r3", skillId="skill-a", failureReason="timeout", createdAt="2026-03-15T08:02:00Z"),
            make_skill_run(id="r4", skillId="skill-b", failureReason="parse error", createdAt="2026-03-15T09:00:00Z"),
            make_skill_run(id="r5", skillId="skill-b", failureReason="parse error", createdAt="2026-03-15T09:01:00Z"),
            make_skill_run(id="r6", skillId="skill-b", failureReason="parse error", createdAt="2026-03-15T09:02:00Z"),
            make_skill_run(id="r7", skillId="skill-b", failureReason="parse error", createdAt="2026-03-15T09:03:00Z"),
        ],
        threshold=3,
    )

    assert [pattern["skillId"] for pattern in patterns] == ["skill-b", "skill-a"]
    assert patterns[0]["count"] == 4
    assert patterns[1]["count"] == 3


def test_detect_patterns_skips_groups_below_threshold():
    patterns = inspection.detect_patterns(
        [make_skill_run(id="r1", skillId="skill-a", failureReason="timeout")],
        threshold=2,
    )
    assert patterns == []


def test_generate_report_and_suggest_action():
    """レポートに対応案と要約メタデータが含まれること。"""
    report = inspection.generate_report(
        [
            {
                "skillId": "skill-a",
                "normalizedReason": "timeout after 30s",
                "count": 3,
                "firstSeen": "2026-03-15T08:00:00Z",
                "lastSeen": "2026-03-15T08:02:00Z",
                "sessionIds": ["session-1"],
                "versions": ["0.0.1"],
                "rawReasons": ["timeout"],
                "runIds": ["r1", "r2", "r3"],
            }
        ],
        generatedAt="2026-03-15T09:00:00Z",
    )

    assert report["status"] == "attention_needed"
    assert report["patternCount"] == 1
    assert report["totalFailures"] == 3
    assert report["summary"].startswith("Found 1 recurring failure pattern")
    assert "timeout" in report["patterns"][0]["suggestedAction"].lower()


def test_suggest_action_default_branch():
    assert inspection.suggest_action({"normalizedReason": "misc", "versions": ["1"]}).startswith("Investigate")


def test_generate_report_clean_state():
    """空のパターン一覧ではクリーンなレポートになること。"""
    report = inspection.generate_report([])
    assert report["status"] == "clean"
    assert report["patternCount"] == 0
    assert "No recurring" in report["summary"]


def test_inspect_pipeline_with_state_store():
    """inspect() がストアを問い合わせてレポートを生成すること。"""

    class Store:
        def get_status(self, recent_skill_run_limit):
            assert recent_skill_run_limit == 2
            return {
                "generatedAt": "2026-03-15T12:00:00Z",
                "skillRuns": {
                    "recent": [
                        make_skill_run(id="r1", failureReason="timeout"),
                        make_skill_run(id="r2", failureReason="timeout"),
                        make_skill_run(id="r3", failureReason="timeout"),
                    ]
                },
            }

    report = inspection.inspect(Store(), windowSize=2, threshold=3)
    assert report["patternCount"] == 1
    assert report["generatedAt"] == "2026-03-15T12:00:00Z"


def test_inspect_handles_typeerror_fallback_and_object_container():
    class Store:
        def get_status(self, payload):  # noqa: ANN001
            assert payload == {"recentSkillRunLimit": 2}
            return {
                "generatedAt": "2026-03-15T12:00:00Z",
                "skillRuns": type("RecentRuns", (), {"recent": [make_skill_run(id="r1", failureReason="timeout")]})(),
            }

    report = inspection.inspect(Store(), windowSize=2, threshold=1)
    assert report["patternCount"] == 1


def test_inspect_requires_status_method():
    with pytest.raises(AttributeError, match="get_status or getStatus"):
        inspection.inspect(object())


@pytest.mark.parametrize(
    ("pattern", "needle"),
    [
        ({"normalizedReason": "timeout while running"}, "timeout"),
        ({"normalizedReason": "permission denied"}, "permission"),
        ({"normalizedReason": "missing file"}, "verify"),
        ({"normalizedReason": "json parse failure"}, "review"),
        ({"normalizedReason": "other", "versions": ["1", "2"]}, "rollback"),
    ],
)
def test_suggest_action(pattern, needle):
    """提案アクションが失敗タイプに一致すること。"""
    assert needle in inspection.suggest_action(pattern).lower()
