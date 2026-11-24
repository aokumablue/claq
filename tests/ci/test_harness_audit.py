"""ハーネス監査の CLI（引数解析・main・テキスト出力）とレポート組み立てのテスト。"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from claq.ci import harness_audit
from claq.ci.harness_audit_utils import make_check
from claq_dev import harness_repo_checks

from ._support import _exit_code_as_main, _write_files
from ._support import _isolate_home as _isolate_home


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], {}),
        (["hooks", "--format=json", "--scope", "skills"], {"scope": "skills", "format": "json"}),
        (["--scope=skills", "--format=json", "--root", "sub"], {"scope": "skills", "format": "json", "root": "sub"}),
        (["--help", "--format", "json", "--root=sub"], {"help": True, "format": "json", "root": "sub"}),
    ],
    ids=["defaults", "positional-then-flag", "equals-and-space", "help-and-long-form"],
)
def test_parse_args_accepts_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str], expected: dict[str, object]
) -> None:
    """値の 2 形式・位置引数・ヘルプを解釈し、未指定は既定値になる（root は cwd 基準で解決）。"""
    monkeypatch.chdir(tmp_path)
    defaults = {"scope": "repo", "format": "text", "help": False}

    assert harness_audit.parse_args(argv) == {
        **defaults,
        **expected,
        "root": tmp_path.resolve() / str(expected.get("root", ".")),
    }


def test_option_value_returns_value_and_next_index() -> None:
    """スペース区切り・`=` 区切りの双方で値と次インデックスを返す。"""
    assert harness_audit._option_value(["--format", "json"], 0, "--format") == ("json", 2)
    assert harness_audit._option_value(["--format=json"], 0, "--format") == ("json", 1)


@pytest.mark.parametrize(
    ("argv", "match"),
    [
        (["--format"], "--format requires a non-empty value"),
        (["--root"], "--root requires a non-empty value"),
        (["--scope"], "--scope requires a non-empty value"),
        (["--root="], "--root requires a non-empty value"),
        (["--root", "--help"], "--root requires a non-empty value"),
        (["--scope", "--format"], "--scope requires a non-empty value"),
        (["--format", "--help"], "--format requires a non-empty value"),
        (["--format=xml"], "Invalid format"),
        (["--bogus"], "Unknown argument: --bogus"),
    ],
    ids=[
        "format-missing", "root-missing", "scope-missing", "root-empty",
        "root-eats-help", "scope-eats-format", "format-eats-help",
        "format-invalid", "unknown-flag",
    ],
)
def test_parse_args_rejects_invalid_arguments(argv: list[str], match: str) -> None:
    """値なし・空値・後続フラグを値として食う形・不正値・未知フラグはエラーにする。

    値なしを既定へ落とすと ``--scope`` は黙って repo になり、``--root --help`` は
    ``root=<cwd>/--help`` かつ ``help=False`` になって存在しない root を監査する。
    """
    with pytest.raises(ValueError, match=match):
        harness_audit.parse_args(argv)


def test_normalize_scope_defaults_lowercases_and_rejects_unknown() -> None:
    """None は repo、大文字は小文字化し、未知の scope はエラーにする。"""
    assert harness_audit.normalize_scope(None) == "repo"
    assert harness_audit.normalize_scope("Hooks") == "hooks"
    with pytest.raises(ValueError, match="Invalid scope"):
        harness_audit.normalize_scope("bad")


def test_build_report_scores_empty_root_in_consumer_mode(tmp_path: Path) -> None:
    """空の root は 0 点になり、配点の高い不合格から top_actions に並ぶ。"""
    report = harness_audit.build_report("repo", root_dir=tmp_path)

    assert report["target_mode"] == "consumer"
    assert (report["overall_score"], report["max_score"], len(report["checks"])) == (0, 25, 9)
    assert report["categories"]["Tool Coverage"]["max_points"] == 7
    assert [action["path"] for action in report["top_actions"]] == ["~/.claude/plugins/", "tests/", ".claude/"]


def test_build_report_consumer_checks_follow_project_files(tmp_path: Path) -> None:
    """consumer の各チェックが、対応するプロジェクトファイルの有無どおりに合否を返す。"""
    _write_files(
        tmp_path,
        {
            ".gitignore": ".env\n",
            ".github/workflows/ci.yml": "name: ci\n",
            ".claude/settings.json": "{}",
            "tests/a.test.js": "test\n",
        },
    )

    report = harness_audit.build_report("repo", root_dir=tmp_path)

    assert {check["id"] for check in report["checks"] if check["pass"]} == {
        "consumer-project-overrides",
        "consumer-project-config",
        "consumer-test-suite",
        "consumer-ci-workflow",
        "consumer-secret-hygiene",
    }


@pytest.mark.parametrize(
    ("service", "files", "ci_path", "label"),
    [
        (
            "github",
            {".github/workflows/ci.yml": "name: ci\n"},
            ".github/workflows/",
            "GitHub",
        ),
        (
            "gitlab",
            {".gitlab-ci.yml": "build:\n  script: echo build\n"},
            ".gitlab-ci.yml",
            "GitLab",
        ),
    ],
    ids=["github", "gitlab"],
)
def test_build_report_uses_git_hosting_service_for_consumer_checks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, service: str, files: dict[str, str], ci_path: str, label: str
) -> None:
    """ホスティング種別ごとの CI 設定の置き場所で consumer チェックが通る。"""
    monkeypatch.setattr(harness_audit, "detect_git_hosting_service", lambda *_args, **_kwargs: service)
    _write_files(tmp_path, files)

    checks = {check["id"]: check for check in harness_audit.build_report("repo", root_dir=tmp_path)["checks"]}

    assert checks["consumer-ci-workflow"]["path"] == ci_path
    assert checks["consumer-ci-workflow"]["description"] == f"プロジェクトが {label} CI 設定をチェックインしている"
    assert checks["consumer-ci-workflow"]["pass"] is True


def test_build_report_top_actions_pick_highest_failing_points(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """top_actions は不合格のうち配点の高い順に 3 件で、合格した高配点や 4 番手以降は出さない。"""
    rows = [
        ("Tool Coverage", 1, False),
        ("Quality Gates", 5, True),
        ("Security Guardrails", 4, False),
        ("Eval Coverage", 2, False),
        ("Cost Efficiency", 3, False),
        ("Memory Persistence", 1, False),
    ]
    checks = [
        make_check(
            check_id=f"c{index}", category=category, points=value, scopes=["repo"],
            path=f"p{index}", description="", passed=passed, fix=f"fix{index}",
        )
        for index, (category, value, passed) in enumerate(rows)
    ]
    monkeypatch.setattr(harness_audit, "get_consumer_checks", lambda *_args: checks)

    report = harness_audit.build_report("repo", root_dir=tmp_path)

    assert [(action["path"], action["category"], action["points"]) for action in report["top_actions"]] == [
        ("p2", "Security Guardrails", 4),
        ("p4", "Cost Efficiency", 3),
        ("p3", "Eval Coverage", 2),
    ]
    assert (report["overall_score"], report["max_score"]) == (5, 16)


@pytest.mark.parametrize(
    "get_checks",
    [
        lambda root: harness_audit.get_consumer_checks(root, None),
        harness_repo_checks.get_repo_checks,
    ],
    ids=["consumer", "repo"],
)
def test_check_categories_and_scopes_are_declared(get_checks: Callable[[Path], list[dict[str, Any]]], tmp_path: Path) -> None:
    """全チェックのカテゴリが CATEGORIES に、スコープが VALID_SCOPES に載る。

    カテゴリ集計が取りこぼさず合計が overall_score と一致する前提と、スコープの綴り違い（``"skill"``）で
    その項目が黙って採点から消えないことの両方を固定する。
    """
    checks = get_checks(tmp_path)
    assert {check["category"] for check in checks} <= set(harness_audit.CATEGORIES)
    assert {scope for check in checks for scope in check["scopes"]} <= harness_audit.VALID_SCOPES


_CHECKS_WITH_FAILURE = [
    {"category": "Tool Coverage", "points": 2, "pass": True},
    {"category": "Tool Coverage", "points": 2, "pass": False},
    {"category": "Security Guardrails", "points": 3, "pass": True},
]


def _report(checks: list[dict[str, object]], top_actions: list[dict[str, str]]) -> dict[str, object]:
    """main・print_text が読むキーだけを持つ監査レポートを返す。"""
    return {
        "scope": "repo",
        "target_mode": "repo",
        "overall_score": sum(check["points"] for check in checks if check["pass"]),
        "max_score": sum(check["points"] for check in checks),
        "root_dir": "/tmp/root",
        "categories": harness_audit.summarize_category_scores(checks),
        "checks": checks,
        "top_actions": top_actions,
    }


_FAILING = _report(_CHECKS_WITH_FAILURE, [{"category": "Tool Coverage", "action": "fix", "path": "x"}])
_PASSING = _report([{"category": "Tool Coverage", "points": 2, "pass": True}], [])


def test_summarize_category_scores_separates_points_and_normalized_score() -> None:
    """カテゴリごとに獲得点・配点・10 点満点の正規化値を分けて返す。"""
    scores = harness_audit.summarize_category_scores(_CHECKS_WITH_FAILURE)

    assert scores["Tool Coverage"] == {"earned_points": 2, "max_points": 4, "normalized_score": 5}
    assert scores["Security Guardrails"] == {"earned_points": 3, "max_points": 3, "normalized_score": 10}
    assert scores["Cost Efficiency"] == {"earned_points": 0, "max_points": 0, "normalized_score": 0}


@pytest.mark.parametrize(("report", "code"), [(_PASSING, 0), (_FAILING, 1)], ids=["passing", "failing"])
def test_main_prints_json_and_exits_nonzero_on_failing_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], report: dict[str, object], code: int
) -> None:
    """`--format=json` はレポートをそのまま JSON で出し、不合格が 1 件でもあれば 1 を返す。"""
    monkeypatch.setattr(harness_audit, "build_report", lambda *_args, **_kwargs: report)

    assert harness_audit.main(["--scope", "repo", "--format=json"]) == code
    assert json.loads(capsys.readouterr().out) == report


@pytest.mark.parametrize(
    ("report", "code", "header", "has_actions"),
    [(_PASSING, 0, "2/2", False), (_FAILING, 1, "5/7", True)],
    ids=["passing", "failing"],
)
def test_main_text_lists_top_actions_only_when_failing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    report: dict[str, object],
    code: int,
    header: str,
    has_actions: bool,
) -> None:
    """テキスト出力は見出しに点数を出し、不合格があるときだけ Top 3 Actions を並べる。"""
    monkeypatch.setattr(harness_audit, "build_report", lambda *_args, **_kwargs: report)

    assert harness_audit.main(["--scope", "repo"]) == code
    output = capsys.readouterr().out
    assert f"Harness Audit (repo, repo): {header}" in output
    assert ("Top 3 Actions:" in output) is has_actions


def test_main_help_exits_zero_and_errors_return_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--help` は使い方を出して 0 を返し、レポート組み立ての失敗は stderr に出して 1 を返す。"""
    assert harness_audit.main(["--help"]) == 0
    assert "Usage" in capsys.readouterr().out

    def _boom(*_args: object, **_kwargs: object) -> None:
        """常に ValueError を送出する build_report の代役。"""
        raise ValueError("boom")

    monkeypatch.setattr(harness_audit, "build_report", _boom)
    assert harness_audit.main(["--scope", "repo", "--format=json"]) == 1
    assert "Error: boom" in capsys.readouterr().err


def test_main_reports_git_timeout_as_error(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """origin の判定で git が時間切れになっても、トレースバックではなく Error 行を出して 1 を返す。"""

    def _timeout(*_args: object, **_kwargs: object) -> None:
        """常に git の時間切れを送出する build_report の代役。"""
        raise subprocess.TimeoutExpired(["git", "remote", "get-url", "origin"], 5)

    monkeypatch.setattr(harness_audit, "build_report", _timeout)
    assert harness_audit.main(["--scope", "repo"]) == 1
    assert "Error: Command" in capsys.readouterr().err


def test_main_entrypoint_exits_with_audit_result(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`python -m` 起動は main の戻り値で終了し、不合格のあるプロジェクトでは 1 になる。"""
    monkeypatch.setattr(sys, "argv", ["harness_audit.py", "--scope", "repo", "--format=json", "--root", str(tmp_path)])

    assert _exit_code_as_main("claq.ci.harness_audit") == 1
