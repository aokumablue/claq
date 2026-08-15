"""bluecore.ci.harness_audit の追加テスト。"""

from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest

import bluecore.ci.harness_audit as harness_audit
from bluecore.ci.harness_audit_repo_checks import (
    _event_has_matching_command,
    _has_memory_lifecycle_hooks,
    _hook_command_argv,
    get_repo_checks,
)


def test_parse_args_and_normalize_scope(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    parsed = harness_audit.parse_args(["--scope=skills", "--format=json", "--root", str(tmp_path)])
    assert parsed["scope"] == "skills"
    assert parsed["format"] == "json"
    assert parsed["root"] == tmp_path.resolve()

    assert harness_audit.normalize_scope(None) == "repo"
    assert harness_audit.normalize_scope("Hooks") == "hooks"
    with pytest.raises(ValueError, match="Invalid scope"):
        harness_audit.normalize_scope("bad")
    with pytest.raises(ValueError, match="Invalid format"):
        harness_audit.parse_args(["--format=xml"])
    with pytest.raises(ValueError, match="Unknown argument: --bogus"):
        harness_audit.parse_args(["--bogus"])


def test_parse_args_covers_help_and_long_form_flags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)

    parsed = harness_audit.parse_args(["--help", "--format", "json", "--root=" + str(tmp_path)])

    assert parsed["help"] is True
    assert parsed["format"] == "json"
    assert parsed["root"] == tmp_path.resolve()


def test_safe_helpers_and_counting(tmp_path: Path) -> None:
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "a.js").write_text("a\n", encoding="utf-8")
    (tmp_path / "dir" / "b.txt").write_text("b\n", encoding="utf-8")
    (tmp_path / "dir" / "nested").mkdir()
    (tmp_path / "dir" / "nested" / "c.js").write_text("c\n", encoding="utf-8")

    assert harness_audit.file_exists(tmp_path, "dir/a.js")
    assert harness_audit.read_text(tmp_path, "dir/a.js") == "a\n"
    assert harness_audit.safe_read(tmp_path, "missing.txt") == ""
    assert harness_audit.safe_parse_json("") is None
    assert harness_audit.safe_parse_json("not-json") is None
    assert harness_audit.safe_parse_json("{\"x\": 1}") == {"x": 1}
    assert harness_audit.count_files(tmp_path, "dir", ".js") == 2
    assert harness_audit.count_files(tmp_path, "missing", ".js") == 0
    assert harness_audit.has_file_with_extension(tmp_path, "dir", ".js")
    assert harness_audit.has_file_with_extension(tmp_path, "dir", [".txt", ".js"])
    assert not harness_audit.has_file_with_extension(tmp_path, "dir", ".py")
    assert harness_audit._has_any_file(tmp_path, ["missing", "dir/a.js"])
    assert not harness_audit._has_any_file(tmp_path, ["missing-a", "missing-b"])

    (tmp_path / ".opencode" / "commands").mkdir(parents=True)
    (tmp_path / "commands").mkdir()
    (tmp_path / "commands" / "harness.md").write_text("same\n", encoding="utf-8")
    (tmp_path / ".opencode" / "commands" / "harness.md").write_text("same\n", encoding="utf-8")
    assert harness_audit._command_parity_matches(tmp_path)

    (tmp_path / ".gitlab-ci.yml").write_text("dependency_scanning:\n  stage: test\n", encoding="utf-8")
    assert harness_audit._has_gitlab_security_scanning(tmp_path)
    assert not harness_audit._has_gitlab_security_scanning(tmp_path / "missing")


def test_find_plugin_install_and_build_report_variants(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    local_install = tmp_path / ".claude" / "plugins" / "everything-claude-code" / ".claude-plugin" / "plugin.json"
    local_install.parent.mkdir(parents=True, exist_ok=True)
    local_install.write_text("{}", encoding="utf-8")
    assert harness_audit.find_plugin_install(tmp_path) == str(local_install)

    root = tmp_path / "repo"
    root.mkdir()
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
    (root / "agents").mkdir()
    (root / "skills").mkdir()
    (root / "src" / "bluecore" / "ci").mkdir(parents=True)
    (root / "src" / "bluecore" / "ci" / "harness_audit.py").write_text("", encoding="utf-8")
    (root / "package.json").write_text(json.dumps({"name": "everything-claude-code", "scripts": {"test": "x"}}), encoding="utf-8")

    report = harness_audit.build_report("repo", root_dir=root)
    assert report["target_mode"] == "repo"
    assert report["overall_score"] >= 0
    assert report["max_score"] >= report["overall_score"]
    assert report["categories"]["Tool Coverage"]["max"] >= 0
    assert report["top_actions"]

    consumer_root = tmp_path / "consumer"
    consumer_root.mkdir()
    (consumer_root / ".gitignore").write_text(".env\n", encoding="utf-8")
    (consumer_root / ".github").mkdir()
    (consumer_root / ".github" / "workflows").mkdir()
    (consumer_root / ".github" / "workflows" / "ci.yml").write_text("name: ci\n", encoding="utf-8")
    (consumer_root / ".github" / "dependabot.yml").write_text("version: 2\n", encoding="utf-8")
    (consumer_root / ".claude").mkdir()
    (consumer_root / ".claude" / "settings.json").write_text("{\"PreToolUse\": []}", encoding="utf-8")
    (consumer_root / "tests").mkdir()
    (consumer_root / "tests" / "a.test.js").write_text("test\n", encoding="utf-8")

    consumer_report = harness_audit.build_report("repo", root_dir=consumer_root, target_mode="consumer")
    assert consumer_report["target_mode"] == "consumer"
    assert consumer_report["checks"]


def test_find_plugin_install_without_home_still_searches_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HOME が空でも root_dir 配下の plugin.json を探す。"""
    monkeypatch.setenv("HOME", "")
    local_install = (
        tmp_path / ".claude" / "plugins" / "everything-claude-code" / ".claude-plugin" / "plugin.json"
    )
    local_install.parent.mkdir(parents=True, exist_ok=True)
    local_install.write_text("{}", encoding="utf-8")
    assert harness_audit.find_plugin_install(tmp_path) == str(local_install)


def test_summarize_category_scores_and_print_text(capsys: pytest.CaptureFixture[str]) -> None:
    checks = [
        {"category": "Tool Coverage", "points": 2, "pass": True},
        {"category": "Tool Coverage", "points": 2, "pass": False},
        {"category": "Security Guardrails", "points": 3, "pass": True},
    ]
    scores = harness_audit.summarize_category_scores(checks)
    assert scores["Tool Coverage"] == {"score": 5, "earned": 2, "max": 4}
    assert scores["Security Guardrails"] == {"score": 10, "earned": 3, "max": 3}

    harness_audit.print_text(
        {
            "scope": "repo",
            "target_mode": "repo",
            "overall_score": 2,
            "max_score": 4,
            "root_dir": "/tmp/root",
            "categories": scores,
            "checks": checks,
            "top_actions": [{"category": "Tool Coverage", "action": "fix", "path": "x"}],
        }
    )
    output = capsys.readouterr().out
    assert "Harness Audit (repo, repo): 2/4" in output
    assert "Top 3 Actions:" in output

    harness_audit.print_text(
        {
            "scope": "repo",
            "target_mode": "repo",
            "overall_score": 4,
            "max_score": 4,
            "root_dir": "/tmp/root",
            "categories": scores,
            "checks": [{"category": "Tool Coverage", "points": 2, "pass": True}],
            "top_actions": [],
        }
    )
    assert "Top 3 Actions:" not in capsys.readouterr().out


def test_show_help_and_main_error(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit, match="0") as excinfo:
        harness_audit.show_help(0)
    assert excinfo.value.code == 0

    with pytest.raises(SystemExit) as help_excinfo:
        harness_audit.main(["--help"])
    assert help_excinfo.value.code == 0

    monkeypatch.setattr(harness_audit, "build_report", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("boom")))
    assert harness_audit.main(["--scope", "repo", "--format=json"]) == 1
    assert "Error: boom" in capsys.readouterr().err


def test_main_covers_json_text_and_help_paths(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    categories = {
        category: {"score": 1, "earned": 1, "max": 1}
        for category in harness_audit.CATEGORIES
    }
    success_report = {
        "scope": "repo",
        "target_mode": "repo",
        "overall_score": 1,
        "max_score": 1,
        "root_dir": str(tmp_path),
        "categories": categories,
        "checks": [{"pass": True, "category": "Tool Coverage", "points": 1}],
        "top_actions": [],
    }
    failing_report = {
        **success_report,
        "checks": [{"pass": False, "category": "Tool Coverage", "points": 1}],
    }

    monkeypatch.setattr(harness_audit, "build_report", lambda *_args, **_kwargs: success_report)

    assert harness_audit.main(["--scope", "repo", "--format=json"]) == 0
    json_output = json.loads(capsys.readouterr().out)
    assert json_output["overall_score"] == 1

    assert harness_audit.main(["--scope", "repo"]) == 0
    text_output = capsys.readouterr().out
    assert "Harness Audit (repo, repo): 1/1" in text_output

    monkeypatch.setattr(harness_audit, "build_report", lambda *_args, **_kwargs: failing_report)
    assert harness_audit.main(["--scope", "repo", "--format=json"]) == 1


def test_main_entrypoint_exits_zero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".claude-plugin").mkdir()
    (root / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
    (root / "agents").mkdir()
    (root / "skills").mkdir()
    (root / "src" / "bluecore" / "ci").mkdir(parents=True)
    (root / "src" / "bluecore" / "ci" / "harness_audit.py").write_text("", encoding="utf-8")
    (root / "package.json").write_text(
        json.dumps({"name": "everything-claude-code", "scripts": {"test": "x"}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        harness_audit.sys,
        "argv",
        ["harness_audit.py", "--scope", "repo", "--format=json", "--root", str(root)],
    )

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("bluecore.ci.harness_audit", run_name="__main__")

    assert excinfo.value.code == 1


def test_get_consumer_checks_accepts_dict_package_json(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"name": "my-project"}), encoding="utf-8")

    checks = harness_audit.get_consumer_checks(tmp_path)

    assert isinstance(checks, list)
    assert checks


def test_get_repo_checks_python_structure_checks_pass_on_real_repo() -> None:
    """JS 前提から Python 構造ベースへ書き換えた8チェックが実リポジトリで pass すること。

    ハーネス監査は元々 Node.js 実装(everything-claude-code)のルーブリックを
    引き継いでいたため、tool-hooks-impl-count 等8件が scripts/hooks/*.js の
    ような JS 専用パスを前提にしていた。本リポジトリは Python 実装のため
    該当チェックは常に false-negative になっていた。実際の Python 構造
    （src/bluecore/hooks/ 等）を対象にした変換後、実リポジトリに対して
    pass=True になることを確認する。
    """
    plugin_root = Path(__file__).resolve().parents[2]
    checks = {check["id"]: check for check in get_repo_checks(plugin_root)}

    python_structure_check_ids = [
        "tool-hooks-impl-count",
        "quality-test-runner",
        "quality-ci-validations",
        "quality-hook-tests",
        "quality-doctor-script",
        "memory-session-hooks",
        "eval-tests-presence",
    ]
    for check_id in python_structure_check_ids:
        assert checks[check_id]["pass"] is True, f"{check_id} should pass on the real repo structure"


def _write_hooks_json(root_dir: Path, hooks: dict) -> None:
    """テスト用の hooks/hooks.json を書き出す。"""
    hooks_dir = root_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "hooks.json").write_text(json.dumps({"hooks": hooks}), encoding="utf-8")


def test_memory_hooks_lifecycle_check_fails_when_hooks_json_missing(tmp_path: Path) -> None:
    """回帰テスト: hooks/hooks.json が存在しない場合は不合格になる。

    旧実装(id=memory-hooks-dir)は hooks/memory-persistence/ ディレクトリの存在
    だけで合格していたため、hooks.json の中身が空・欠落していても満点扱いに
    なる誤判定があった。
    """
    checks = {check["id"]: check for check in get_repo_checks(tmp_path)}

    assert "memory-hooks-lifecycle" in checks
    assert checks["memory-hooks-lifecycle"]["pass"] is False


def test_memory_hooks_lifecycle_check_fails_when_dir_exists_but_commands_are_stale(
    tmp_path: Path,
) -> None:
    """回帰テスト: フックディレクトリ相当が残っていても実コマンドが不正なら不合格。"""
    (tmp_path / "hooks" / "memory-persistence").mkdir(parents=True)
    _write_hooks_json(
        tmp_path,
        {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "python3 launcher.py bluecore.hooks.unrelated"}]}
            ],
            "Stop": [],
            "SessionEnd": [],
        },
    )

    checks = {check["id"]: check for check in get_repo_checks(tmp_path)}

    assert checks["memory-hooks-lifecycle"]["pass"] is False


def test_memory_hooks_lifecycle_check_fails_when_hooks_value_not_dict(tmp_path: Path) -> None:
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks" / "hooks.json").write_text(json.dumps({"hooks": ["not", "a", "dict"]}), encoding="utf-8")

    checks = {check["id"]: check for check in get_repo_checks(tmp_path)}

    assert checks["memory-hooks-lifecycle"]["pass"] is False


def test_memory_hooks_lifecycle_check_fails_when_only_partial_lifecycle_present(tmp_path: Path) -> None:
    """SessionStart の mem context は正しいが session_start/Stop が欠けている場合は不合格。"""
    _write_hooks_json(
        tmp_path,
        {
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": (
                                'python3 "${CLAUDE_PLUGIN_ROOT}/src/bluecore/launcher.py" '
                                "bluecore.mem.cli context"
                            ),
                        }
                    ]
                }
            ],
        },
    )

    checks = {check["id"]: check for check in get_repo_checks(tmp_path)}

    assert checks["memory-hooks-lifecycle"]["pass"] is False


def test_memory_hooks_lifecycle_check_passes_with_real_lifecycle_commands(tmp_path: Path) -> None:
    """実際の hooks/hooks.json と同形式のライフサイクル定義が揃っていれば合格する。

    Stop / SessionEnd 側は非ブロッキングの --bg フラグ付きで登録される想定
    （launcher --bg の実際の使い方に合わせる）。
    """
    _write_hooks_json(
        tmp_path,
        {
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": (
                                'python3 "${CLAUDE_PLUGIN_ROOT}/src/bluecore/launcher.py" '
                                "bluecore.mem.cli context"
                            ),
                        }
                    ]
                },
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": (
                                'python3 "${CLAUDE_PLUGIN_ROOT}/src/bluecore/launcher.py" '
                                "bluecore.hooks.session_start"
                            ),
                        }
                    ]
                },
            ],
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": (
                                'python3 "${CLAUDE_PLUGIN_ROOT}/src/bluecore/launcher.py" '
                                "--bg bluecore.hooks.session_end"
                            ),
                        }
                    ]
                }
            ],
            "SessionEnd": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": (
                                'python3 "${CLAUDE_PLUGIN_ROOT}/src/bluecore/launcher.py" '
                                "--bg bluecore.mem.cli handoff"
                            ),
                        }
                    ]
                }
            ],
        },
    )

    checks = {check["id"]: check for check in get_repo_checks(tmp_path)}

    assert checks["memory-hooks-lifecycle"]["pass"] is True
    assert checks["memory-hooks-lifecycle"]["path"] == "hooks/hooks.json"


def test_memory_hooks_lifecycle_check_passes_on_real_repo_hooks_json() -> None:
    """実リポジトリの hooks/hooks.json に対して合格することを確認する。"""
    plugin_root = Path(__file__).resolve().parents[2]
    checks = {check["id"]: check for check in get_repo_checks(plugin_root)}

    assert checks["memory-hooks-lifecycle"]["pass"] is True


def test_hook_command_argv_covers_parse_edge_cases() -> None:
    """_hook_command_argv の分岐（不正引用符・トークン不足・起動形式不一致等）を網羅する。"""
    assert _hook_command_argv('python3 "unterminated') is None
    assert _hook_command_argv("python3 launcher.py") is None
    assert _hook_command_argv("node launcher.py a b") is None
    assert _hook_command_argv("python3 other.py a b") is None
    assert _hook_command_argv("python3 launcher.py a b") == ("a", "b")
    assert _hook_command_argv('python3 "${ROOT}/launcher.py" a b') == ("a", "b")
    # 先頭の --bg（非 Claude ハーネス向け detach フラグ）は実体でないため除去する
    assert _hook_command_argv("python3 launcher.py --bg a b") == ("a", "b")


def test_event_has_matching_command_covers_branches() -> None:
    """_event_has_matching_command の全分岐（型不正・不一致・一致）を網羅する。"""
    patterns = (("bluecore.mem.cli", "session:mem:setup"),)

    assert _event_has_matching_command("not-a-list", patterns) is False
    assert _event_has_matching_command([], patterns) is False
    assert _event_has_matching_command(["not-a-dict"], patterns) is False
    assert _event_has_matching_command([{"hooks": "not-a-list"}], patterns) is False
    assert _event_has_matching_command([{"hooks": ["not-a-dict"]}], patterns) is False
    assert _event_has_matching_command([{"hooks": [{"command": 123}]}], patterns) is False
    assert _event_has_matching_command([{"hooks": [{"command": "python3 launcher.py"}]}], patterns) is False
    assert (
        _event_has_matching_command(
            [{"hooks": [{"command": "python3 launcher.py bluecore.mem.cli other:action"}]}],
            patterns,
        )
        is False
    )
    assert (
        _event_has_matching_command(
            [{"hooks": [{"command": "python3 launcher.py bluecore.mem.cli session:mem:setup"}]}],
            patterns,
        )
        is True
    )


def test_has_memory_lifecycle_hooks_returns_false_on_malformed_json(tmp_path: Path) -> None:
    """_has_memory_lifecycle_hooks が不正 JSON・hooks 欠落時に False を返すことを確認する。"""
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks" / "hooks.json").write_text("not-json", encoding="utf-8")
    assert _has_memory_lifecycle_hooks(tmp_path) is False
