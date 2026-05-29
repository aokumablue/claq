"""skill_evolution の health および dashboard モジュールのテスト。"""

from __future__ import annotations

import json

import pytest

from devgear.lib.skill_evolution import dashboard as dashboard
from devgear.lib.skill_evolution import health as health
from devgear.lib.skill_evolution import provenance as provenance
from devgear.lib.skill_evolution import tracker as tracker
from devgear.lib.skill_evolution import versioning as versioning


def _build_skill_data(skill_env, make_skill, append_jsonl, now):
    curated = make_skill(skill_env["skills_root"], "alpha", "# Alpha\n")
    learned = make_skill(skill_env["learned_root"], "beta", "# Beta\n")
    imported = make_skill(skill_env["imported_root"], "gamma", "# Gamma\n")

    provenance.write_provenance(
        imported,
        {
            "source": "https://example.com/gamma",
            "created_at": "2026-03-14T10:00:00.000Z",
            "confidence": 0.92,
            "author": "importer",
        },
        skill_env,
    )

    versioning.create_version(curated, timestamp="2026-03-14T11:00:00.000Z", reason="bootstrap", author="observer")
    versioning.create_version(learned, timestamp="2026-03-14T11:00:00.000Z", reason="bootstrap", author="observer")
    append_jsonl(
        curated / ".evolution" / "amendments.jsonl",
        [
            {"event": "proposal", "status": "pending", "created_at": "2026-03-15T07:00:00.000Z"},
        ],
    )

    append_jsonl(
        skill_env["runs_file"],
        [
            {
                "skill_id": "alpha",
                "skill_version": "v1",
                "task_description": "Recent success",
                "outcome": "success",
                "failure_reason": None,
                "tokens_used": 100,
                "duration_ms": 1000,
                "user_feedback": "accepted",
                "recorded_at": "2026-03-14T10:00:00.000Z",
            },
            {
                "skill_id": "alpha",
                "skill_version": "v1",
                "task_description": "Recent failure",
                "outcome": "failure",
                "failure_reason": "Regression",
                "tokens_used": 100,
                "duration_ms": 1000,
                "user_feedback": "rejected",
                "recorded_at": "2026-03-13T10:00:00.000Z",
            },
            {
                "skill_id": "alpha",
                "skill_version": "v1",
                "task_description": "Older success",
                "outcome": "success",
                "failure_reason": None,
                "tokens_used": 100,
                "duration_ms": 1000,
                "user_feedback": "accepted",
                "recorded_at": "2026-02-20T10:00:00.000Z",
            },
            {
                "skill_id": "beta",
                "skill_version": "v1",
                "task_description": "Beta success",
                "outcome": "success",
                "failure_reason": None,
                "tokens_used": 90,
                "duration_ms": 800,
                "user_feedback": "accepted",
                "recorded_at": "2026-03-15T09:00:00.000Z",
            },
            {
                "skill_id": "beta",
                "skill_version": "v1",
                "task_description": "Beta failure",
                "outcome": "failure",
                "failure_reason": "Bad import",
                "tokens_used": 90,
                "duration_ms": 800,
                "user_feedback": "corrected",
                "recorded_at": "2026-02-20T09:00:00.000Z",
            },
            {
                "skill_id": "delta",
                "skill_version": "v1",
                "task_description": "Unknown skill",
                "outcome": "success",
                "failure_reason": None,
                "tokens_used": 1,
                "duration_ms": 1,
                "user_feedback": "accepted",
                "recorded_at": "2026-03-15T11:00:00.000Z",
            },
        ],
    )

    return curated, learned, imported


def test_discover_skills(skill_env, make_skill):
    """全ルートにまたがってスキルを検出できること。"""
    (skill_env["skills_root"] / "README.txt").write_text("ignore", encoding="utf-8")
    make_skill(skill_env["skills_root"], "alpha")
    make_skill(skill_env["learned_root"], "beta")
    make_skill(skill_env["imported_root"], "gamma")

    skills = health.discover_skills(skill_env)
    assert skills["alpha"]["skill_type"] == provenance.SKILL_TYPES["CURATED"]
    assert skills["beta"]["skill_type"] == provenance.SKILL_TYPES["LEARNED"]
    assert skills["gamma"]["skill_type"] == provenance.SKILL_TYPES["IMPORTED"]


def test_collect_skill_health_and_format_report(skill_env, make_skill, append_jsonl, now):
    """ヘルス収集で傾向を計算し、テキスト/JSON を描画できること。"""
    _build_skill_data(skill_env, make_skill, append_jsonl, now)

    report = health.collect_skill_health(
        {
            **skill_env,
            "runs_file_path": str(skill_env["runs_file"]),
            "now": now,
            "warn_threshold": 0.1,
        }
    )

    alpha = next(skill for skill in report["skills"] if skill["skill_id"] == "alpha")
    beta = next(skill for skill in report["skills"] if skill["skill_id"] == "beta")
    gamma = next(skill for skill in report["skills"] if skill["skill_id"] == "gamma")
    delta = next(skill for skill in report["skills"] if skill["skill_id"] == "delta")

    assert alpha["current_version"] == "v1"
    assert alpha["pending_amendments"] == 1
    assert alpha["failure_trend"] == "worsening"
    assert alpha["declining"] is True
    assert beta["failure_trend"] == "improving"
    assert gamma["skill_type"] == provenance.SKILL_TYPES["IMPORTED"]
    assert gamma["current_version"] == "v1"
    assert delta["skill_type"] == provenance.SKILL_TYPES["UNKNOWN"]

    summary = health.summarize_health_report(report)
    assert summary == {
        "total_skills": 4,
        "healthy_skills": 3,
        "declining_skills": 1,
    }

    human = health.format_health_report(report)
    assert "devgear skill health" in human
    assert "alpha" in human
    assert "worsening" in human

    json_report = health.format_health_report(report, json=True)
    parsed = json.loads(json_report)
    assert parsed["generated_at"] == now


def test_health_helpers(skill_env, make_skill, append_jsonl, now):
    """ヘルパー関数が一貫して動作すること。"""
    _build_skill_data(skill_env, make_skill, append_jsonl, now)

    assert health.calculate_success_rate([]) is None
    assert health.calculate_success_rate([{"outcome": "success"}, {"outcome": "failure"}]) == 0.5
    assert health.round_rate(None) is None
    assert health.round_rate(0.12345) == 0.1235
    assert health.format_rate(None) == "n/a"

    records = [
        {"recorded_at": "2026-03-15T10:00:00.000Z"},
        {"recorded_at": "2026-03-14T10:00:00.000Z"},
        {"recorded_at": "2026-03-06T10:00:00.000Z"},
    ]
    filtered = health.filter_records_within_days(
        records, int(__import__("datetime").datetime.fromisoformat(now.replace("Z", "+00:00")).timestamp() * 1000), 7
    )
    assert len(filtered) == 2
    assert health.get_failure_trend(0.4, 0.6, 0.1) == "worsening"
    assert health.get_failure_trend(None, 0.6, 0.1) == "stable"
    assert health.get_failure_trend(0.7, 0.6, 0.1) == "improving"
    assert health.get_failure_trend(0.69, 0.6, 0.1) == "stable"
    assert health.count_pending_amendments(None) == 0
    assert health.get_last_run([]) is None
    assert health.get_last_run([
        {"recorded_at": "invalid"},
        {"recorded_at": "2026-03-15T10:00:00.000Z"},
        {"recordedAt": "2026-03-15T11:00:00.000Z"},
    ]) == "2026-03-15T11:00:00.000Z"
    assert health.filter_records_within_days(records, int(__import__("datetime").datetime.fromisoformat(now.replace("Z", "+00:00")).timestamp() * 1000), 0) == []

    def monkeypatch_round_rate(value):
        return None
    assert health.get_failure_trend(0.7, 0.6, 0.1) == "improving"
    original_round_rate = health.round_rate
    health.round_rate = monkeypatch_round_rate  # type: ignore[assignment]
    try:
        assert health.get_failure_trend(0.7, 0.6, 0.1) == "stable"
    finally:
        health.round_rate = original_round_rate  # type: ignore[assignment]

    assert health._resolve_now_ms(now) > 0
    with pytest.raises(ValueError, match="Invalid now timestamp"):
        health._resolve_now_ms("bad")

    assert health._record_skill_id({}) is None
    assert health._record_skill_id({"skill_id": "   "}) is None

    append_jsonl(
        skill_env["runs_file"],
        [
            {
                "outcome": "success",
                "failure_reason": None,
                "tokens_used": 1,
                "duration_ms": 1,
                "user_feedback": "accepted",
                "recorded_at": now,
            }
        ],
    )
    report = health.collect_skill_health({**skill_env, "runs_file_path": str(skill_env["runs_file"]), "now": now, "warn_threshold": 0.1})
    assert any(skill["skill_id"] == "alpha" for skill in report["skills"])

    with pytest.raises(ValueError, match="Invalid warn threshold: bad"):
        health.collect_skill_health({**skill_env, "runs_file_path": str(skill_env["runs_file"]), "now": now, "warn_threshold": "bad"})
    with pytest.raises(ValueError, match="Invalid warn threshold: -0.1"):
        health.collect_skill_health({**skill_env, "runs_file_path": str(skill_env["runs_file"]), "now": now, "warn_threshold": -0.1})

    assert "No skill execution records found." in health.format_health_report({"generated_at": now, "skills": []})


def test_filter_records_within_days_and_bucket_by_day_cutoffs(now):
    """cutoff 境界と不正値を確認する。"""
    now_ms = int(__import__("datetime").datetime.fromisoformat(now.replace("Z", "+00:00")).timestamp() * 1000)
    records = [
        {"recorded_at": "2026-03-15T12:00:00.000Z"},
        {"recorded_at": "2026-03-08T12:00:00.000Z"},
        {"recorded_at": "2026-03-08T11:59:59.999Z"},
        {"recorded_at": "not-a-timestamp"},
    ]

    filtered = health.filter_records_within_days(records, now_ms, 7)
    assert len(filtered) == 1
    assert filtered[0]["recorded_at"] == "2026-03-15T12:00:00.000Z"

    assert health.filter_records_within_days(records, now_ms, 0) == []
    assert health.filter_records_within_days(records, now_ms, -1) == []

    from devgear.lib.skill_evolution.dashboard import bucket_by_day

    buckets = bucket_by_day(
        [
            {"recorded_at": now},
            {"recorded_at": "2026-03-14T12:00:00.001Z"},
            {"recorded_at": "2026-03-08T12:00:00.000Z"},
            {"recorded_at": "2026-03-08T11:59:59.999Z"},
            {"recorded_at": "invalid"},
        ],
        now_ms,
        7,
    )
    assert len(buckets) == 7
    assert buckets[-1]["runs"] == 2
    assert buckets[0]["runs"] == 0
    assert sum(bucket["runs"] for bucket in buckets) == 2


def test_dashboard_primitives_and_panels(skill_env, make_skill, append_jsonl, now):
    """ダッシュボードの基本要素とパネルが期待どおりに描画されること。"""
    _build_skill_data(skill_env, make_skill, append_jsonl, now)

    assert dashboard.sparkline([1, 0.5, 0]) == "█▅▁"
    assert dashboard.sparkline([None, "bad", -1, 2]) == "░░▁█"
    assert dashboard.sparkline([]) == ""
    assert dashboard.horizontal_bar(5, 10, 10).count("█") == 5
    assert dashboard.horizontal_bar(5, 0, 10) == "░" * 10
    assert dashboard.horizontal_bar(5, 10, 0) == ""
    assert "Test Panel" in dashboard.panel_box("Test Panel", ["line one", "line two"], 30)
    assert dashboard.panel_box("X", ["abcdef"], 4).count("abcdef") == 0

    now_ms = int(__import__("datetime").datetime.fromisoformat(now.replace("Z", "+00:00")).timestamp() * 1000)
    buckets = dashboard.bucket_by_day(
        [
            {"skill_id": "alpha", "outcome": "success", "recorded_at": "2026-03-15T10:00:00.000Z"},
            {"skill_id": "alpha", "outcome": "failure", "recorded_at": "2026-03-15T08:00:00.000Z"},
            {"skill_id": "alpha", "outcome": "success", "recorded_at": "2026-03-14T10:00:00.000Z"},
        ],
        now_ms,
        3,
    )
    assert len(buckets) == 3
    assert buckets[-1]["runs"] == 2
    assert buckets[-1]["rate"] == 0.5

    report = health.collect_skill_health({**skill_env, "runs_file_path": str(skill_env["runs_file"]), "now": now, "warn_threshold": 0.1})
    records = tracker.read_skill_execution_records({**skill_env, "runs_file_path": str(skill_env["runs_file"]), "now": now})

    success_panel = dashboard.render_success_rate_panel(records, report["skills"], {"now": now})
    assert "Success Rate" in success_panel["text"]
    assert any(skill["skill_id"] == "alpha" for skill in success_panel["data"]["skills"])

    failure_panel = dashboard.render_failure_cluster_panel(records)
    assert "Failure Patterns" in failure_panel["text"]
    assert failure_panel["data"]["total_failures"] == 2

    amendment_panel = dashboard.render_amendment_panel(health.discover_skills(skill_env))
    assert "Pending Amendments" in amendment_panel["text"]
    assert amendment_panel["data"]["total"] == 1

    version_panel = dashboard.render_version_timeline_panel(health.discover_skills(skill_env))
    assert "Version History" in version_panel["text"]
    assert any(skill["skill_id"] == "alpha" for skill in version_panel["data"]["skills"])

    dashboard_result = dashboard.render_dashboard({**skill_env, "runs_file_path": str(skill_env["runs_file"]), "now": now, "warn_threshold": 0.1})
    assert "devgear Skill Health Dashboard" in dashboard_result["text"]
    assert "Success Rate" in dashboard_result["text"]
    assert dashboard_result["data"]["summary"]["declining_skills"] == 1

    failures_only = dashboard.render_dashboard({**skill_env, "runs_file_path": str(skill_env["runs_file"]), "now": now, "warn_threshold": 0.1, "panel": "failures"})
    assert "Failure Patterns" in failures_only["text"]
    assert "Version History" not in failures_only["text"]


def test_dashboard_panels_cover_empty_and_invalid_paths(skill_env, make_skill, append_jsonl, now):
    """空状態や不正入力の分岐を確認する。"""
    skill_dir = make_skill(skill_env["skills_root"], "alpha")
    versioning.create_version(skill_dir, timestamp="2026-03-14T11:00:00.000Z", reason="bootstrap", author="observer")
    amendments_path = skill_dir / ".evolution" / "amendments.jsonl"
    amendments_path.write_text(
        amendments_path.read_text(encoding="utf-8")
        + '{"event": "proposal", "created_at": "2026-03-15T11:00:00.000Z"}\n'
        + '{"event": "proposal", "status": "queued", "created_at": "2026-03-15T12:00:00.000Z"}\n'
        + '{"event": "snapshot", "version": "bad", "reason": 123, "created_at": "2026-03-14T11:00:00.000Z"}\n',
        encoding="utf-8",
    )

    empty_success = dashboard.render_success_rate_panel([], [], {"now": now, "days": 0, "width": 20})
    assert "No skill execution" in empty_success["text"]

    failure_panel = dashboard.render_failure_cluster_panel(
        [
            {"outcome": "failure", "failure_reason": "X", "skill_id": "alpha"},
            {"outcome": "failure", "failureReason": "X", "skillId": "beta"},
            {"outcome": "success", "failure_reason": "ignored"},
        ],
        {"width": 20},
    )
    assert failure_panel["data"]["total_failures"] == 2
    assert failure_panel["data"]["clusters"][0]["count"] == 2

    amendment_panel = dashboard.render_amendment_panel(
        {"alpha": {"skill_dir": skill_dir}, "beta": {"skill_dir": None}},
        {"width": 40},
    )
    assert amendment_panel["data"]["total"] == 2
    assert amendment_panel["data"]["amendments"][0]["status"] == "queued"

    version_panel = dashboard.render_version_timeline_panel({"alpha": {"skill_dir": skill_dir}}, {"width": 40})
    assert version_panel["data"]["skills"][0]["versions"][0]["reason"] == "bootstrap"

    with pytest.raises(ValueError, match="Unknown panel"):
        dashboard.render_dashboard({**skill_env, "now": now, "panel": "invalid"})


def test_dashboard_and_health_helpers_cover_remaining_edges(skill_env, make_skill, append_jsonl, now):
    """未到達の分岐をまとめて確認する。"""
    (skill_env["skills_root"] / "README.txt").write_text("ignore", encoding="utf-8")
    skill_dir = make_skill(skill_env["skills_root"], "alpha")

    assert dashboard.bucket_by_day([], 1_000, 0) == []
    assert dashboard._iter_skills(None) == []
    assert dashboard._iter_skills([{"skill_id": "alpha"}]) == [{"skill_id": "alpha"}]
    assert dashboard._iter_skills({"alpha": {"skill_id": "alpha"}}) == [{"skill_id": "alpha"}]
    assert dashboard._iter_skill_items(None) == []
    assert dashboard._iter_skill_items([{"skill_id": "alpha"}, {"name": "missing"}]) == [
        ("alpha", {"skill_id": "alpha"})
    ]
    assert dashboard._group_records_by_skill([{"task_description": "missing id"}, {"skill_id": "alpha"}]) == {
        "alpha": [{"skill_id": "alpha"}]
    }

    with pytest.raises(ValueError, match="Invalid now timestamp"):
        dashboard.render_success_rate_panel([], [], {"now": "bad"})

    assert "No failure patterns detected." in dashboard.render_failure_cluster_panel([])["text"]
    assert "No pending amendments." in dashboard.render_amendment_panel({})["text"]
    assert "No version history available." in dashboard.render_version_timeline_panel({"alpha": {"skill_dir": None}})["text"]

    with pytest.raises(ValueError, match="Invalid now timestamp"):
        dashboard.render_dashboard({**skill_env, "now": "bad"})

    assert health.discover_skills(skill_env)["alpha"]["skill_dir"] == str(skill_dir)
