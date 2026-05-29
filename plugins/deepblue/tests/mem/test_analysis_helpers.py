"""Tests for memory analysis helpers."""

from __future__ import annotations

from datetime import date

from devgear.mem import dashboard_queries as dq
from devgear.mem import skill_analyzer as sa
from devgear.mem import skill_proposal as sp


class FakeCursor:
    def __init__(self, results: list[object]):
        self.results = results
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.index = 0

    def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
        self.executed.append((query, params))

    def fetchall(self):
        result = self.results[self.index]
        self.index += 1
        return result

    def fetchone(self):
        result = self.results[self.index]
        self.index += 1
        return result

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeConn:
    def __init__(self, cursor: FakeCursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class FakePg:
    def __init__(self, results: list[object]):
        self.cursor = FakeCursor(results)
        self.conn = FakeConn(self.cursor)
        self.put_calls = 0

    def _get_conn(self):
        return self.conn

    def _put_conn(self, conn):
        assert conn is self.conn
        self.put_calls += 1


def test_activity_by_user():
    pg = FakePg([[("alice", 3), ("bob", 1)]])

    result = dq.activity_by_user(pg, days=7)

    assert result == [{"user": "alice", "chunks": 3}, {"user": "bob", "chunks": 1}]
    assert pg.cursor.executed[0][1] == (7,)
    assert pg.put_calls == 1


def test_activity_by_project():
    pg = FakePg([[("repo-a", 4), ("repo-b", 2)]])

    result = dq.activity_by_project(pg, days=14)

    assert result == [{"project": "repo-a", "chunks": 4}, {"project": "repo-b", "chunks": 2}]
    assert pg.cursor.executed[0][1] == (14,)
    assert pg.put_calls == 1


def test_tool_usage_distribution():
    pg = FakePg([[("bash", 5), ("read", 2)]])

    result = dq.tool_usage_distribution(pg, days=30)

    assert result == [{"tool": "bash", "count": 5}, {"tool": "read", "count": 2}]
    assert pg.cursor.executed[0][1] == (30,)
    assert pg.put_calls == 1


def test_session_timeline():
    pg = FakePg([[(date(2024, 1, 2), 3, 7), (date(2024, 1, 3), 2, 4)]])

    result = dq.session_timeline(pg, days=21)

    assert result == [
        {"date": "2024-01-02", "sessions": 3, "chunks": 7},
        {"date": "2024-01-03", "sessions": 2, "chunks": 4},
    ]
    assert pg.cursor.executed[0][1] == (21,)
    assert pg.put_calls == 1


def test_instinct_growth():
    pg = FakePg([[(date(2024, 2, 1), 5, 1.2345), (date(2024, 2, 2), 2, 0.5)]])

    result = dq.instinct_growth(pg, days=60)

    assert result == [
        {"date": "2024-02-01", "count": 5, "avg_confidence": 1.23},
        {"date": "2024-02-02", "count": 2, "avg_confidence": 0.5},
    ]
    assert pg.cursor.executed[0][1] == (60,)
    assert pg.put_calls == 1


def test_memory_quality_metrics_returns_empty_when_no_row():
    pg = FakePg([None])

    result = dq.memory_quality_metrics(pg)

    assert result == {}
    assert pg.put_calls == 1


def test_memory_quality_metrics_calculates_rates():
    pg = FakePg([(20, 5, 7, 2.345, 8, 3, 2)])

    result = dq.memory_quality_metrics(pg)

    assert result == {
        "total_chunks": 20,
        "short_chunks": 5,
        "short_chunk_rate": 25.0,
        "accessed_chunks": 7,
        "access_rate": 35.0,
        "avg_access_count": 2.3,
        "total_sessions": 8,
        "total_users": 3,
        "total_projects": 2,
    }
    assert pg.put_calls == 1


def test_file_change_heatmap():
    pg = FakePg([[("src/app.py", 9), ("README.md", 2)]])

    result = dq.file_change_heatmap(pg, days=90)

    assert result == [{"file": "src/app.py", "changes": 9}, {"file": "README.md", "changes": 2}]
    assert pg.cursor.executed[0][1] == (90,)
    assert pg.put_calls == 1


def test_detect_repeated_patterns():
    pg = FakePg([[
        ("bash,read,write", 4, 2, 3, ("repo-a", "repo-b"), ("alice", "bob")),
    ]])

    result = sa.detect_repeated_patterns(pg, min_count=3, days=90)

    assert result == [
        {
            "tool_combo": "bash,read,write",
            "tools": ["bash", "read", "write"],
            "count": 4,
            "project_count": 2,
            "user_count": 3,
            "projects": ["repo-a", "repo-b"],
            "users": ["alice", "bob"],
        }
    ]
    assert pg.cursor.executed[0][1] == (90, 3)
    assert pg.put_calls == 1


def test_detect_skill_gaps():
    pg = FakePg([[
        ("fix bug", 3, 2, ("alice", "bob"), "Fix bug in app.py"),
        ("refactor module", 2, 1, ("carol",), "Refactor module"),
    ]])

    result = sa.detect_skill_gaps(pg, days=7, top_n=10)

    assert result == [
        {
            "prompt_key": "fix bug",
            "count": 3,
            "user_count": 2,
            "users": ["alice", "bob"],
            "sample_prompt": "Fix bug in app.py",
        },
        {
            "prompt_key": "refactor module",
            "count": 2,
            "user_count": 1,
            "users": ["carol"],
            "sample_prompt": "Refactor module",
        },
    ]
    assert pg.cursor.executed[0][1] == (7, 10)
    assert pg.put_calls == 1


def test_analyze_skill_usage_returns_empty_when_no_usage():
    pg = FakePg([None])

    result = sa.analyze_skill_usage(pg, "quick-edit", days=30)

    assert result == {
        "total_uses": 0,
        "unique_users": 0,
        "unique_projects": 0,
        "users": [],
        "projects": [],
        "avg_access_count": 0.0,
        "last_used_epoch": None,
    }
    assert len(pg.cursor.executed) == 1
    assert pg.put_calls == 1


def test_analyze_skill_usage_builds_timeline():
    pg = FakePg(
        [
            (5, 2, 1, ("alice", "bob"), ("repo-a",), 1.2345, 1700000000),
            [(date(2024, 3, 1), 3), (date(2024, 3, 2), 2)],
        ]
    )

    result = sa.analyze_skill_usage(pg, "quick-edit", days=30)

    assert result == {
        "total_uses": 5,
        "unique_users": 2,
        "unique_projects": 1,
        "users": ["alice", "bob"],
        "projects": ["repo-a"],
        "avg_access_count": 1.23,
        "last_used_epoch": 1700000000,
        "timeline": [
            {"date": "2024-03-01", "uses": 3},
            {"date": "2024-03-02", "uses": 2},
        ],
    }
    assert pg.cursor.executed[0][1] == (30, "%quick-edit%", "%quick-edit%")
    assert pg.cursor.executed[1][1] == (30, "%quick-edit%", "%quick-edit%")
    assert pg.put_calls == 1


def test_suggest_skill_improvements_returns_usage_low():
    pg = FakePg([[], []])

    result = sa.suggest_skill_improvements(pg, "quick-edit", days=30)

    assert result == [
        {
            "type": "usage_low",
            "description": "スキル 'quick-edit' の使用データが少なく十分な分析ができません。より多くのデータ蓄積が必要です",
            "evidence": [],
            "priority": "info",
        }
    ]
    assert pg.put_calls == 1


def test_suggest_skill_improvements_includes_tool_and_file_patterns():
    pg = FakePg(
        [
            [("Bash", 4), ("Read", 2), ("Edit", 1)],
            [("src/app.py", 5), ("src/utils.py", 2)],
        ]
    )

    result = sa.suggest_skill_improvements(pg, "quick-edit", days=30)

    assert result == [
        {
            "type": "tool_coverage",
            "description": "スキル 'quick-edit' のセッションで頻繁に使用されるツールを明示的に案内に追加することを検討してください",
            "evidence": [{"tool": "Bash", "count": 4}],
            "priority": "medium",
        },
        {
            "type": "file_pattern",
            "description": "スキル 'quick-edit' 使用時に頻繁に変更されるファイルパターンをドキュメントに追記することを検討してください",
            "evidence": [{"file": "src/app.py", "count": 5}, {"file": "src/utils.py", "count": 2}],
            "priority": "low",
        },
    ]
    assert pg.put_calls == 1


def test_infer_skill_name_variants():
    cases = [
        (["bash", "write", "read"], "s-file-workflow"),
        (["bash"], "s-shell-automation"),
        (["read", "grep", "glob"], "s-code-search"),
        (["edit", "write"], "s-code-edit"),
        (["bash", "edit"], "s-build-run"),
        (["custom_tool"], "s-custom-tool"),
    ]

    for tools, expected in cases:
        assert sp._infer_skill_name(tools) == expected


def test_classify_priority_boundaries():
    assert sp._classify_priority(20) == "high"
    assert sp._classify_priority(5) == "medium"
    assert sp._classify_priority(4) == "low"


def test_generate_proposal_builds_candidates_and_actions():
    patterns = [
        {
            "tools": ["custom_tool"],
            "count": 1,
            "users": ["u1"],
            "projects": [],
        },
        {
            "tools": ["read", "grep", "glob"],
            "count": 3,
            "users": ["u1", "u2"],
            "projects": ["repo-a"],
        },
        {
            "tools": ["bash", "edit", "write", "read"],
            "count": 5,
            "users": [f"user-{idx}" for idx in range(6)],
            "projects": [f"repo-{idx}" for idx in range(6)],
        },
    ]
    gaps = [
        {"sample_prompt": "first long gap prompt for packaging workflows", "count": 6, "users": ["u1", "u2"]},
        {"sample_prompt": "second long gap prompt for code search", "count": 5, "users": ["u3"]},
    ]
    gaps.extend(
        {"sample_prompt": f"extra gap {idx}", "count": 1, "users": [f"user-{idx}"]} for idx in range(9)
    )

    proposal = sp.generate_proposal(patterns, gaps)

    assert proposal["summary"] == {
        "total_patterns": 3,
        "total_gaps": 11,
        "skill_candidates": 3,
        "gap_candidates": 10,
    }
    assert [candidate["suggested_name"] for candidate in proposal["skill_candidates"]] == [
        "s-file-workflow",
        "s-code-search",
        "s-custom-tool",
    ]
    assert proposal["skill_candidates"][0]["priority"] == "high"
    assert proposal["skill_candidates"][0]["evidence"]["users"] == [f"user-{idx}" for idx in range(5)]
    assert proposal["skill_candidates"][0]["evidence"]["projects"] == [f"repo-{idx}" for idx in range(5)]
    assert "s-file-workflow" in proposal["skill_candidates"][0]["skillmaster_prompt"]
    assert proposal["skill_candidates"][0]["skillmaster_prompt"].endswith("SKILL.md に定義してください。")
    assert proposal["gap_candidates"][0]["sample_prompt"] == "first long gap prompt for packaging workflows"
    assert len(proposal["gap_candidates"]) == 10
    assert [item["action"] for item in proposal["action_items"]] == [
        "create_skill",
        "create_skill",
        "fill_gap",
        "fill_gap",
    ]
    assert proposal["action_items"][0]["target"] == "s-file-workflow"
    assert proposal["action_items"][2]["target"].startswith("first long gap prompt")
