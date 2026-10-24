"""ハーネス監査の CLI（引数解析・main・テキスト出力）とレポート組み立てのテスト。"""

from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest

import claq.ci.harness_audit as harness_audit
from claq.ci.harness_audit_utils import make_check

from ._support import _isolate_home as _isolate_home
from ._support import _write_files, _write_repo_markers


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], {}),
        (["hooks", "--format=json", "--scope", "skills"], {"scope": "skills", "format": "json"}),
        (["--scope=skills", "--format=json", "--root", "sub"], {"scope": "skills", "format": "json", "root": "sub"}),
        (["--help", "--format", "json", "--root=sub"], {"help": True, "format": "json", "root": "sub"}),
        (["--target-kind", "repo"], {"target_kind": "repo"}),
        (["--target-kind=consumer"], {"target_kind": "consumer"}),
    ],
    ids=["defaults", "positional-then-flag", "equals-and-space", "help-and-long-form", "kind-repo", "kind-consumer"],
)
def test_parse_args_accepts_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str], expected: dict[str, object]
) -> None:
    """値の 2 形式・位置引数・ヘルプを解釈し、未指定は既定値になる（root は cwd 基準で解決）。"""
    monkeypatch.chdir(tmp_path)
    defaults = {"scope": "repo", "format": "text", "help": False, "target_kind": None}

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
        (["--target-kind"], "--target-kind requires a non-empty value"),
        (["--root="], "--root requires a non-empty value"),
        (["--root", "--help"], "--root requires a non-empty value"),
        (["--scope", "--format"], "--scope requires a non-empty value"),
        (["--target-kind", "--help"], "--target-kind requires a non-empty value"),
        (["--format", "--help"], "--format requires a non-empty value"),
        (["--format=xml"], "Invalid format"),
        (["--bogus"], "Unknown argument: --bogus"),
        (["--target-kind", "provider"], "Invalid target-kind"),
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


@pytest.mark.parametrize(("include_harness", "expected"), [(True, "repo"), (False, "consumer")])
def test_detect_target_mode_requires_python_harness_marker(
    tmp_path: Path, include_harness: bool, expected: str
) -> None:
    """repo 判定には harness_audit.py が要り、JS 版のマーカーは数えない。"""
    _write_repo_markers(tmp_path, include_harness=include_harness)
    _write_files(tmp_path, {"scripts/harness-audit.js": ""})

    assert harness_audit.detect_target_mode(tmp_path) == expected


@pytest.mark.parametrize("target_mode", [None, "repo"], ids=["auto", "explicit-match"])
def test_build_report_scores_repo_markers_in_repo_mode(tmp_path: Path, target_mode: str | None) -> None:
    """repo マーカーだけの root は repo モードで採点され、明示指定が自動判定と一致しても同じ結果になる。

    surface が 1 つも無い fixture では予算系 2 件（context-always-on-budget /
    cost-no-oversized-surface）が自明に通る。surface の欠落は tool-agent-count / tool-skill-count が見る。
    """
    _write_repo_markers(tmp_path)
    (tmp_path / "commands").mkdir()

    report = harness_audit.build_report("repo", root_dir=tmp_path, target_mode=target_mode)

    assert report["target_mode"] == "repo"
    assert (report["overall_score"], report["max_score"], len(report["checks"])) == (5, 58, 22)
    assert report["categories"]["Tool Coverage"]["max_points"] == 10
    assert report["top_actions"][0]["path"] == "hooks/hooks.json"


def test_build_report_scores_empty_root_in_consumer_mode(tmp_path: Path) -> None:
    """空の root は consumer モードで 0 点になり、配点の高い不合格から top_actions に並ぶ。"""
    report = harness_audit.build_report("repo", root_dir=tmp_path)

    assert report["target_mode"] == "consumer"
    assert (report["overall_score"], report["max_score"], len(report["checks"])) == (0, 29, 11)
    assert report["categories"]["Tool Coverage"]["max_points"] == 7
    assert [action["path"] for action in report["top_actions"]] == ["~/.claude/plugins/", "tests/", ".claude/"]


def test_build_report_consumer_checks_follow_project_files(tmp_path: Path) -> None:
    """consumer の各チェックが、対応するプロジェクトファイルの有無どおりに合否を返す。"""
    _write_files(
        tmp_path,
        {
            ".gitignore": ".env\n",
            ".github/workflows/ci.yml": "name: ci\n",
            ".github/dependabot.yml": "version: 2\n",
            ".claude/settings.json": '{"PreToolUse": []}',
            "tests/a.test.js": "test\n",
        },
    )

    report = harness_audit.build_report("repo", root_dir=tmp_path, target_mode="consumer")

    assert {check["id"] for check in report["checks"] if check["pass"]} == {
        "consumer-project-overrides",
        "consumer-project-config",
        "consumer-test-suite",
        "consumer-ci-workflow",
        "consumer-security-policy",
        "consumer-secret-hygiene",
        "consumer-hook-guardrails",
    }


@pytest.mark.parametrize(
    ("service", "files", "ci_path", "label"),
    [
        (
            "github",
            {".github/workflows/ci.yml": "name: ci\n", ".github/codeql.yml": "name: codeql\n"},
            ".github/workflows/",
            "GitHub",
        ),
        (
            "gitlab",
            {".gitlab-ci.yml": "build:\n  script: echo build\ndependency_scanning:\n  stage: test\n"},
            ".gitlab-ci.yml",
            "GitLab",
        ),
    ],
)
def test_build_report_uses_git_hosting_service_for_consumer_checks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, service: str, files: dict[str, str], ci_path: str, label: str
) -> None:
    """ホスティング種別ごとの CI 設定・セキュリティスキャンの置き場所で consumer チェックが通る。"""
    monkeypatch.setattr(harness_audit, "detect_git_hosting_service", lambda *args, **kwargs: service)
    _write_files(tmp_path, files)

    checks = {check["id"]: check for check in harness_audit.build_report("repo", root_dir=tmp_path)["checks"]}

    assert checks["consumer-ci-workflow"]["path"] == ci_path
    assert checks["consumer-ci-workflow"]["description"] == f"プロジェクトが {label} CI 設定をチェックインしている"
    assert checks["consumer-ci-workflow"]["pass"] is True
    assert checks["consumer-security-policy"]["pass"] is True


def test_build_report_top_actions_pick_highest_failing_points(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """top_actions は不合格のうち配点の高い順に 3 件で、合格した高配点や 4 番手以降は出さない。"""
    points = [(1, False), (5, True), (4, False), (2, False), (3, False), (1, False)]
    checks = [
        make_check(
            check_id=f"c{index}", category="Tool Coverage", points=value, scopes=["repo"],
            path=f"p{index}", description="", passed=passed, fix=f"fix{index}",
        )
        for index, (value, passed) in enumerate(points)
    ]
    monkeypatch.setattr(harness_audit, "get_consumer_checks", lambda *_args: checks)

    report = harness_audit.build_report("repo", root_dir=tmp_path)

    assert [action["path"] for action in report["top_actions"]] == ["p2", "p4", "p3"]
    assert (report["overall_score"], report["max_score"]) == (5, 16)


@pytest.mark.parametrize(
    ("with_provider", "match"),
    [
        (False, "does not match the auto-detected mode"),
        (True, r"does not match the auto-detected mode.*--root plugins/demo"),
    ],
    ids=["plain", "provider-hint"],
)
def test_build_report_target_kind_mismatch_raises(tmp_path: Path, with_provider: bool, match: str) -> None:
    """明示 target_kind が自動判定と食い違えば FAIL し、提供元ディレクトリがあれば --root の指定先を案内する。

    プラグイン提供元リポジトリのルートはそれ自体はプラグインではないので consumer と判定される。
    """
    if with_provider:
        _write_files(tmp_path, {"plugins/demo/.claude-plugin/plugin.json": "{}"})

    with pytest.raises(ValueError, match=match):
        harness_audit.build_report("repo", root_dir=tmp_path, target_mode="repo")


def test_summarize_self_check_detects_broken_aggregation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """カテゴリ earned_points の合計が overall_score と一致しなければ落とす（取りこぼし・二重計上の検出）。"""
    zero = {"earned_points": 0, "max_points": 0, "normalized_score": 0}
    monkeypatch.setattr(
        harness_audit, "summarize_category_scores", lambda checks: dict.fromkeys(harness_audit.CATEGORIES, zero)
    )
    _write_repo_markers(tmp_path)

    with pytest.raises(AssertionError, match="一致しません"):
        harness_audit.build_report("repo", root_dir=tmp_path)


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
    """`--help` は exit 0 で抜け、レポート組み立ての失敗は stderr に出して 1 を返す。"""
    with pytest.raises(SystemExit) as excinfo:
        harness_audit.main(["--help"])
    assert excinfo.value.code == 0

    def _boom(*_args: object, **_kwargs: object) -> None:
        """常に ValueError を送出する build_report の代役。"""
        raise ValueError("boom")

    monkeypatch.setattr(harness_audit, "build_report", _boom)
    assert harness_audit.main(["--scope", "repo", "--format=json"]) == 1
    assert "Error: boom" in capsys.readouterr().err


def test_main_entrypoint_exits_with_audit_result(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`python -m` 起動は main の戻り値で終了し、不合格のある repo では 1 になる。"""
    _write_repo_markers(tmp_path)
    monkeypatch.setattr(
        harness_audit.sys, "argv", ["harness_audit.py", "--scope", "repo", "--format=json", "--root", str(tmp_path)]
    )

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("claq.ci.harness_audit", run_name="__main__")

    assert excinfo.value.code == 1
