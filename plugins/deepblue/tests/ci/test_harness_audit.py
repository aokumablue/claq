"""ハーネス監査モジュールのテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

import devgear.ci.harness_audit as harness_audit


def test_parse_args_supports_positional_scope_and_flags(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)

    args = harness_audit.parse_args(["hooks", "--format=json", "--scope", "skills"])

    assert args["scope"] == "skills"
    assert args["format"] == "json"
    assert args["help"] is False
    assert args["root"] == tmp_path.resolve()


def test_detect_target_mode_recognizes_repo_markers(tmp_path: Path) -> None:
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
    (tmp_path / "agents").mkdir()
    (tmp_path / "skills").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "devgear").mkdir()
    (tmp_path / "src" / "devgear" / "ci").mkdir()
    (tmp_path / "src" / "devgear" / "ci" / "harness_audit.py").write_text("", encoding="utf-8")

    assert harness_audit.detect_target_mode(tmp_path) == "repo"


def test_detect_target_mode_requires_python_harness_marker(tmp_path: Path) -> None:
    """Python 実装への移行後は harness_audit.py が HARNESS_MARKERS として必要。"""
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
    (tmp_path / "agents").mkdir()
    (tmp_path / "skills").mkdir()
    # JS マーカーのみでは repo と判定されない
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "harness-audit.js").write_text("", encoding="utf-8")
    assert harness_audit.detect_target_mode(tmp_path) == "consumer"

    # Python マーカーがあれば repo と判定される
    (tmp_path / "src" / "devgear" / "ci").mkdir(parents=True)
    (tmp_path / "src" / "devgear" / "ci" / "harness_audit.py").write_text("", encoding="utf-8")
    assert harness_audit.detect_target_mode(tmp_path) == "repo"


def test_build_report_defaults_to_repo_mode_with_repo_markers(tmp_path: Path) -> None:
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
    (tmp_path / "agents").mkdir()
    (tmp_path / "skills").mkdir()
    (tmp_path / "commands").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "devgear").mkdir()
    (tmp_path / "src" / "devgear" / "ci").mkdir()
    (tmp_path / "src" / "devgear" / "ci" / "harness_audit.py").write_text("", encoding="utf-8")

    report = harness_audit.build_report("repo", root_dir=tmp_path)

    assert report["target_mode"] == "repo"
    assert report["overall_score"] == 0
    assert report["max_score"] == 70
    assert len(report["checks"]) == 26
    assert report["categories"]["Tool Coverage"]["max"] == 10
    assert report["top_actions"][0]["path"] == "hooks/memory-persistence/"


def test_build_report_defaults_to_consumer_mode_on_empty_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()

    report = harness_audit.build_report("repo", root_dir=tmp_path)

    assert report["target_mode"] == "consumer"
    assert report["overall_score"] == 0
    assert report["max_score"] == 29
    assert len(report["checks"]) == 11
    assert report["categories"]["Tool Coverage"]["max"] == 7
    assert report["top_actions"][0]["path"] == "~/.claude/plugins/everything-claude-code/"
    assert report["top_actions"][1]["path"] == "tests/"
    assert report["top_actions"][2]["path"] == ".claude/"


@pytest.mark.parametrize(
    ("service", "ci_path", "ci_file", "ci_contents", "security_contents", "expected_description"),
    [
        (
            "github",
            ".github/workflows/",
            ".github/workflows/ci.yml",
            "name: ci\n",
            "name: codeql\n",
            "プロジェクトが GitHub CI 設定をチェックインしている",
        ),
        (
            "gitlab",
            ".gitlab-ci.yml",
            ".gitlab-ci.yml",
            "build:\n  script: echo build\n",
            "dependency_scanning:\n  stage: test\n",
            "プロジェクトが GitLab CI 設定をチェックインしている",
        ),
    ],
)
def test_build_report_uses_git_hosting_service_for_consumer_checks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    service: str,
    ci_path: str,
    ci_file: str,
    ci_contents: str,
    security_contents: str,
    expected_description: str,
) -> None:
    monkeypatch.setattr(harness_audit, "detect_git_hosting_service", lambda *args, **kwargs: service)

    ci_file_path = tmp_path / ci_file
    ci_file_path.parent.mkdir(parents=True, exist_ok=True)
    ci_file_path.write_text(ci_contents, encoding="utf-8")

    if service == "github":
        security_path = tmp_path / ".github" / "codeql.yml"
        security_path.parent.mkdir(parents=True, exist_ok=True)
        security_path.write_text(security_contents, encoding="utf-8")
    else:
        ci_file_path.write_text(f"{ci_contents}{security_contents}", encoding="utf-8")

    report = harness_audit.build_report("repo", root_dir=tmp_path)
    checks = {check["id"]: check for check in report["checks"]}

    assert checks["consumer-ci-workflow"]["path"] == ci_path
    assert checks["consumer-ci-workflow"]["description"] == expected_description
    assert checks["consumer-ci-workflow"]["pass"] is True
    assert checks["consumer-security-policy"]["pass"] is True
