"""ハーネス監査モジュールのテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

import bluecore.ci.harness_audit as harness_audit


def _write_repo_markers(root: Path, *, include_harness: bool = True) -> None:
    """repo モード判定に必要なマーカーファイルを作成する。"""
    (root / ".claude-plugin").mkdir(exist_ok=True)
    (root / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
    (root / "agents").mkdir(exist_ok=True)
    (root / "skills").mkdir(exist_ok=True)
    if include_harness:
        (root / "src" / "bluecore" / "ci").mkdir(parents=True, exist_ok=True)
        (root / "src" / "bluecore" / "ci" / "harness_audit.py").write_text("", encoding="utf-8")


def test_parse_args_supports_positional_scope_and_flags(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)

    args = harness_audit.parse_args(["hooks", "--format=json", "--scope", "skills"])

    assert args["scope"] == "skills"
    assert args["format"] == "json"
    assert args["help"] is False
    assert args["root"] == tmp_path.resolve()


def test_option_value_without_following_token_returns_none() -> None:
    """`--name` の直後に値が無い場合は (None, index + 2) を返す。"""
    assert harness_audit._option_value(["--format"], 0, "--format") == (None, 2)
    assert harness_audit._option_value(["--root"], 0, "--root") == (None, 2)


def test_parse_args_root_flag_without_value_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`--root` に値が続かない場合は cwd へ黙って落とさずエラーにする（N-04 対応）。

    以前は空値で cwd にフォールバックしており、意図しない root を
    監査していることに気付けなかった。
    """
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="--root requires a non-empty path value"):
        harness_audit.parse_args(["--root"])


def test_parse_args_root_flag_with_empty_value_raises() -> None:
    """`--root=`（空文字値）も同様にエラーにする。"""
    with pytest.raises(ValueError, match="--root requires a non-empty path value"):
        harness_audit.parse_args(["--root="])


def test_detect_target_mode_recognizes_repo_markers(tmp_path: Path) -> None:
    _write_repo_markers(tmp_path)

    assert harness_audit.detect_target_mode(tmp_path) == "repo"


def test_detect_target_mode_requires_python_harness_marker(tmp_path: Path) -> None:
    """Python 実装への移行後は harness_audit.py が HARNESS_MARKERS として必要。"""
    _write_repo_markers(tmp_path, include_harness=False)
    # JS マーカーのみでは repo と判定されない
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "harness-audit.js").write_text("", encoding="utf-8")
    assert harness_audit.detect_target_mode(tmp_path) == "consumer"

    # Python マーカーがあれば repo と判定される
    _write_repo_markers(tmp_path, include_harness=True)
    assert harness_audit.detect_target_mode(tmp_path) == "repo"


def test_parse_args_target_kind_accepts_repo_and_consumer() -> None:
    assert harness_audit.parse_args(["--target-kind", "repo"])["target_kind"] == "repo"
    assert harness_audit.parse_args(["--target-kind=consumer"])["target_kind"] == "consumer"


def test_parse_args_target_kind_rejects_unknown_value() -> None:
    with pytest.raises(ValueError, match="Invalid target-kind"):
        harness_audit.parse_args(["--target-kind", "provider"])


def test_parse_args_target_kind_defaults_to_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert harness_audit.parse_args([])["target_kind"] is None


def test_build_report_target_kind_matching_auto_detection_succeeds(tmp_path: Path) -> None:
    """明示 target_kind が自動判定と一致すれば通常どおり動作する。"""
    _write_repo_markers(tmp_path)
    (tmp_path / "commands").mkdir()

    report = harness_audit.build_report("repo", root_dir=tmp_path, target_mode="repo")

    assert report["target_mode"] == "repo"


def test_build_report_target_kind_mismatch_raises(tmp_path: Path) -> None:
    """明示 target_kind が自動判定と食い違えば FAIL する（F-04 対応）。

    consumer 判定になる root（provider の agents/skills が無い）を
    誤って --target-kind repo で監査しようとした場合の再現。
    """
    with pytest.raises(ValueError, match="does not match the auto-detected mode"):
        harness_audit.build_report("repo", root_dir=tmp_path, target_mode="repo")


def test_build_report_defaults_to_repo_mode_with_repo_markers(tmp_path: Path) -> None:
    _write_repo_markers(tmp_path)
    (tmp_path / "commands").mkdir()

    report = harness_audit.build_report("repo", root_dir=tmp_path)

    assert report["target_mode"] == "repo"
    assert report["overall_score"] == 0
    assert report["max_score"] == 65
    assert len(report["checks"]) == 24
    assert report["categories"]["Tool Coverage"]["max"] == 10
    assert report["top_actions"][0]["path"] == "hooks/hooks.json"


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
