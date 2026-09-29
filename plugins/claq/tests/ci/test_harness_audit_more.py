"""claq.ci.harness_audit の追加テスト。"""

from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest

import claq.ci.harness_audit as harness_audit
from claq.ci.harness_audit_repo_checks import (
    _event_has_matching_command,
    _has_memory_lifecycle_hooks,
    _hook_command_argv,
    always_on_description_chars,
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

    (tmp_path / "blank.txt").write_text("   \n", encoding="utf-8")

    binary = tmp_path / "binary.bin"
    binary.write_bytes(b"\xff\xfe\x00\x01")

    assert harness_audit.file_has_content(tmp_path, "dir/a.js")
    assert not harness_audit.file_has_content(tmp_path, "binary.bin")
    assert not harness_audit.file_has_content(tmp_path, "blank.txt")
    assert not harness_audit.file_has_content(tmp_path, "missing.txt")
    assert not harness_audit.file_has_content(tmp_path, "dir")
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

    (tmp_path / ".gitlab-ci.yml").write_text("dependency_scanning:\n  stage: test\n", encoding="utf-8")
    assert harness_audit._has_gitlab_security_scanning(tmp_path)
    assert not harness_audit._has_gitlab_security_scanning(tmp_path / "missing")


def test_find_plugin_install_and_build_report_variants(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    local_install = tmp_path / ".claude" / "plugins" / "claq" / ".claude-plugin" / "plugin.json"
    local_install.parent.mkdir(parents=True, exist_ok=True)
    local_install.write_text("{}", encoding="utf-8")
    assert harness_audit.find_plugin_install(tmp_path) == str(local_install)

    root = tmp_path / "repo"
    root.mkdir()
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
    (root / "agents").mkdir()
    (root / "skills").mkdir()
    (root / "src" / "claq" / "ci").mkdir(parents=True)
    (root / "src" / "claq" / "ci" / "harness_audit.py").write_text("", encoding="utf-8")
    (root / "package.json").write_text(json.dumps({"name": "claq", "scripts": {"test": "x"}}), encoding="utf-8")

    report = harness_audit.build_report("repo", root_dir=root)
    assert report["target_mode"] == "repo"
    assert report["overall_score"] >= 0
    assert report["max_score"] >= report["overall_score"]
    assert report["categories"]["Tool Coverage"]["max_points"] >= 0
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


def test_find_plugin_install_detects_marketplace_cache_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """マーケットプレイス配置（cache/<marketplace>/<plugin>/<version>/）を検出する。"""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    cache_root = home / ".claude" / "plugins" / "cache" / "claq" / "claq"
    for version in ("0.9.9", "0.9.48"):
        manifest = cache_root / version / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("{}", encoding="utf-8")

    found = harness_audit.find_plugin_install(tmp_path)

    # 契約は「辞書順で最初」であって「最新版」ではない。0.9.9 と 0.9.48 を
    # 並べると両者が分岐する（辞書順では 0.9.48 が先）。
    assert found == str(cache_root / "0.9.48" / ".claude-plugin" / "plugin.json")


def test_find_plugin_install_returns_none_without_any_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """平置きも cache 配置も無ければ None を返す。"""
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))

    assert harness_audit.find_plugin_install(tmp_path) is None


def test_has_python_tests_skips_vendor_directories(tmp_path: Path) -> None:
    """依存ツリー同梱のテストは加点材料にしない。"""
    vendored = tmp_path / ".venv" / "lib" / "site-packages" / "dateutil"
    vendored.mkdir(parents=True)
    (vendored / "test_parser.py").write_text("def test_x(): pass\n", encoding="utf-8")

    assert not harness_audit.has_python_tests(tmp_path, minimum=1)


def test_has_python_tests_skips_unreadable_directories(tmp_path: Path) -> None:
    """読めないディレクトリはスキップし、例外を送出しない（docstring の契約）。"""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o000)
    try:
        assert harness_audit.has_python_tests(tmp_path, minimum=1) is False
    finally:
        blocked.chmod(0o700)


def test_has_python_tests_recognizes_both_pytest_conventions(tmp_path: Path) -> None:
    """`test_*.py` と `*_test.py` の双方を検出し、通常の .py では発火しない。"""
    assert not harness_audit.has_python_tests(tmp_path / "missing", minimum=1)

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    assert not harness_audit.has_python_tests(tmp_path, minimum=1)

    (tmp_path / "src" / "test_app.py").write_text("def test_x(): pass\n", encoding="utf-8")
    assert harness_audit.has_python_tests(tmp_path, minimum=1)

    suffix_root = tmp_path / "suffix"
    (suffix_root / "pkg").mkdir(parents=True)
    (suffix_root / "pkg" / "app_test.py").write_text("def test_x(): pass\n", encoding="utf-8")
    assert harness_audit.has_python_tests(suffix_root, minimum=1)


def test_has_python_tests_counts_until_minimum_is_reached(tmp_path: Path) -> None:
    """件数が `minimum` に届くまでは False を返し、届いた時点で True になる。

    1 件で打ち切っていた頃の挙動が既定値として残っていると、閾値 3 を要求する
    `consumer-eval-coverage` が 1 件のリポジトリで通ってしまう。
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    for index in range(2):
        (pkg / f"test_mod{index}.py").write_text("def test_x(): pass\n", encoding="utf-8")

    assert harness_audit.has_python_tests(tmp_path, minimum=2)
    assert not harness_audit.has_python_tests(tmp_path, minimum=3)

    (pkg / "test_mod2.py").write_text("def test_x(): pass\n", encoding="utf-8")

    assert harness_audit.has_python_tests(tmp_path, minimum=3)


def test_consumer_test_suite_passes_for_pytest_only_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JS の規約を一切持たない pytest 専用リポジトリでも consumer-test-suite が通る。

    HOME を tmp 配下へ固定するのは、`find_plugin_install` が実 HOME の
    プラグイン導入状態を読んで結果が開発者の環境に依存するのを防ぐため。
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("def test_x(): pass\n", encoding="utf-8")

    report = harness_audit.build_report("repo", root_dir=tmp_path, target_mode="consumer")

    check = next(c for c in report["checks"] if c["id"] == "consumer-test-suite")
    assert check["pass"] is True


def _eval_coverage_pass(tmp_path: Path) -> bool:
    """`consumer-eval-coverage` の合否だけを取り出す。"""
    report = harness_audit.build_report("repo", root_dir=tmp_path, target_mode="consumer")
    return next(c for c in report["checks"] if c["id"] == "consumer-eval-coverage")["pass"]


def test_consumer_eval_coverage_uses_python_tests_at_the_js_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pytest だけのリポジトリでも JS と同じ 3 件で `consumer-eval-coverage` が通る。

    「複数の自動テスト」の側が `tests/*.test.js` >= 3 しか見ていなかったため、
    2,466 件の pytest を持つリポジトリでも `evals/` が無ければ 0 点だった。
    閾値は JS 側と揃えてあり、2 件では通らないことまで固定する。

    HOME を tmp 配下へ固定するのは、`find_plugin_install` が実 HOME の
    プラグイン導入状態を読んで結果が開発者の環境に依存するのを防ぐため。
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    for index in range(2):
        (tests_dir / f"test_mod{index}.py").write_text("def test_x(): pass\n", encoding="utf-8")

    assert _eval_coverage_pass(tmp_path) is False

    (tests_dir / "test_mod2.py").write_text("def test_x(): pass\n", encoding="utf-8")

    assert _eval_coverage_pass(tmp_path) is True


def test_consumer_eval_coverage_fails_without_tests_or_evals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """テストも `evals/` も無いリポジトリは従来どおり落ちる（判定の緩みすぎ防止）。"""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")

    assert _eval_coverage_pass(tmp_path) is False


def test_consumer_eval_coverage_still_passes_via_evals_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Python テストが 1 件も無くても `evals/` 経路は従来どおり通る（非退行）。"""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "evals").mkdir()
    (tmp_path / "evals" / "case.yaml").write_text("name: case\n", encoding="utf-8")

    assert _eval_coverage_pass(tmp_path) is True


def test_find_plugin_install_without_home_still_searches_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HOME が空でも root_dir 配下の plugin.json を探す。"""
    monkeypatch.setenv("HOME", "")
    local_install = (
        tmp_path / ".claude" / "plugins" / "claq" / ".claude-plugin" / "plugin.json"
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
    assert scores["Tool Coverage"] == {"earned_points": 2, "max_points": 4, "normalized_score": 5}
    assert scores["Security Guardrails"] == {"earned_points": 3, "max_points": 3, "normalized_score": 10}

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
        category: {"normalized_score": 1, "earned_points": 1, "max_points": 1}
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
    (root / "src" / "claq" / "ci").mkdir(parents=True)
    (root / "src" / "claq" / "ci" / "harness_audit.py").write_text("", encoding="utf-8")
    (root / "package.json").write_text(
        json.dumps({"name": "claq", "scripts": {"test": "x"}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        harness_audit.sys,
        "argv",
        ["harness_audit.py", "--scope", "repo", "--format=json", "--root", str(root)],
    )

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("claq.ci.harness_audit", run_name="__main__")

    assert excinfo.value.code == 1


def test_get_consumer_checks_accepts_dict_package_json(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"name": "my-project"}), encoding="utf-8")

    checks = harness_audit.get_consumer_checks(tmp_path)

    assert isinstance(checks, list)
    assert checks


def test_get_repo_checks_python_structure_checks_pass_on_real_repo() -> None:
    """JS 前提から Python 構造ベースへ書き換えたチェック群が実リポジトリで pass すること。

    ハーネス監査は元々 Node.js 実装(claq)のルーブリックを
    引き継いでいたため、対象チェックが scripts/hooks/*.js のような JS 専用
    パスを前提にしていた。本リポジトリは Python 実装のため該当チェックは
    常に false-negative になっていた。実際の Python 構造
    （src/claq/hooks/ 等）を対象にした変換後、実リポジトリに対して
    pass=True になることを確認する。

    tool-hooks-impl-count（最低12モジュール）・eval-tests-presence（最低60
    テストファイル）は、claq 自身が過剰処理を廃止し src/tests を意図的に
    最小化したため、本テストの対象から除外している（「多いほど良い」という
    前提自体が本リファクタの方針と矛盾するため）。
    """
    plugin_root = Path(__file__).resolve().parents[2]
    checks = {check["id"]: check for check in get_repo_checks(plugin_root)}

    python_structure_check_ids = [
        "quality-test-runner",
        "quality-ci-validations",
        "quality-hook-tests",
        "memory-session-hooks",
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
                {"hooks": [{"type": "command", "command": "claq-hook claq.hooks.unrelated"}]}
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
                                '"${CLAUDE_PLUGIN_ROOT}/runtime/claq-hook" '
                                "claq.mem.cli context"
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
                                '"${CLAUDE_PLUGIN_ROOT}/runtime/claq-hook" '
                                "claq.mem.cli context"
                            ),
                        }
                    ]
                },
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": (
                                '"${CLAUDE_PLUGIN_ROOT}/runtime/claq-hook" '
                                "claq.hooks.session_start"
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
                                '"${CLAUDE_PLUGIN_ROOT}/runtime/claq-hook" '
                                "--bg claq.hooks.session_end"
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
                                '"${CLAUDE_PLUGIN_ROOT}/runtime/claq-hook" '
                                "--bg claq.mem.cli handoff"
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
    assert _hook_command_argv('"unterminated') is None
    assert _hook_command_argv("claq-hook") is None
    assert _hook_command_argv("python3 launcher.py a b") is None
    assert _hook_command_argv("other-hook a b") is None
    assert _hook_command_argv("claq-hook a b") == ("a", "b")
    assert _hook_command_argv('"${ROOT}/runtime/claq-hook" a b') == ("a", "b")
    # Windows 側は cmd.exe が PATHEXT で解決する .cmd を直接書いた形式も受ける
    assert _hook_command_argv(r'"C:\\plug\\runtime\\claq-hook.cmd" a b') == ("a", "b")
    # 先頭の --bg（非 Claude ハーネス向け detach フラグ）は実体でないため除去する
    assert _hook_command_argv("claq-hook --bg a b") == ("a", "b")


def test_event_has_matching_command_covers_branches() -> None:
    """_event_has_matching_command の全分岐（型不正・不一致・一致）を網羅する。"""
    patterns = (("claq.mem.cli", "session:mem:setup"),)

    assert _event_has_matching_command("not-a-list", patterns) is False
    assert _event_has_matching_command([], patterns) is False
    assert _event_has_matching_command(["not-a-dict"], patterns) is False
    assert _event_has_matching_command([{"hooks": "not-a-list"}], patterns) is False
    assert _event_has_matching_command([{"hooks": ["not-a-dict"]}], patterns) is False
    assert _event_has_matching_command([{"hooks": [{"command": 123}]}], patterns) is False
    assert _event_has_matching_command([{"hooks": [{"command": "claq-hook"}]}], patterns) is False
    assert (
        _event_has_matching_command(
            [{"hooks": [{"command": "claq-hook claq.mem.cli other:action"}]}],
            patterns,
        )
        is False
    )
    assert (
        _event_has_matching_command(
            [{"hooks": [{"command": "claq-hook claq.mem.cli session:mem:setup"}]}],
            patterns,
        )
        is True
    )


def test_has_memory_lifecycle_hooks_returns_false_on_malformed_json(tmp_path: Path) -> None:
    """_has_memory_lifecycle_hooks が不正 JSON・hooks 欠落時に False を返すことを確認する。"""
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks" / "hooks.json").write_text("not-json", encoding="utf-8")
    assert _has_memory_lifecycle_hooks(tmp_path) is False


def test_summarize_self_check_detects_broken_aggregation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """カテゴリ earned_points の合計が overall_score と一致しなければ落とすこと。

    単位を分けた（earned_points / max_points / normalized_score）以上、合計の
    一致は機械的に保証できる。集計経路の取りこぼし・二重計上をここで検出する。
    """
    monkeypatch.setattr(
        harness_audit,
        "summarize_category_scores",
        lambda checks: {category: {"earned_points": 0, "max_points": 0, "normalized_score": 0} for category in harness_audit.CATEGORIES},
    )
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".claude-plugin").mkdir()
    (root / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
    (root / "agents").mkdir()
    (root / "skills").mkdir()
    (root / "src" / "claq" / "ci").mkdir(parents=True)
    (root / "src" / "claq" / "ci" / "harness_audit.py").write_text("", encoding="utf-8")

    with pytest.raises(AssertionError, match="一致しません"):
        harness_audit.build_report("repo", root_dir=root)


def test_always_on_description_chars_skips_files_without_description(tmp_path: Path) -> None:
    """description を持たない定義ファイルは加算対象にならないこと。

    frontmatter が無い場合と、frontmatter はあるが ``description`` が無い場合の
    両方を置く。前者だけだと「frontmatter が読めなければ 0」の分岐しか通らず、
    ``description`` が文字列でない経路が未検査のまま残る。
    """
    nofm = tmp_path / "skills" / "nodesc"
    nofm.mkdir(parents=True)
    (nofm / "SKILL.md").write_text("# no frontmatter here\njust body\n", encoding="utf-8")

    nokey = tmp_path / "skills" / "nokey"
    nokey.mkdir(parents=True)
    (nokey / "SKILL.md").write_text("---\nname: nokey\n---\n\n本文\n", encoding="utf-8")

    assert always_on_description_chars(tmp_path) == 0


def test_always_on_description_chars_skips_slash_only_surfaces(tmp_path: Path) -> None:
    """``disable-model-invocation: true`` の surface は加算対象にならないこと。

    同じ description を宣言の有無だけ変えて 2 度測り、差が宣言 1 行に由来する
    ことを固定する。
    """
    counted = tmp_path / "counted" / "skills" / "a"
    counted.mkdir(parents=True)
    (counted / "SKILL.md").write_text("---\nname: a\ndescription: あいうえお\n---\n", encoding="utf-8")

    skipped = tmp_path / "skipped" / "skills" / "a"
    skipped.mkdir(parents=True)
    (skipped / "SKILL.md").write_text(
        "---\nname: a\ndescription: あいうえお\ndisable-model-invocation: true\n---\n",
        encoding="utf-8",
    )

    assert always_on_description_chars(tmp_path / "counted") == 5
    assert always_on_description_chars(tmp_path / "skipped") == 0


def test_target_kind_mismatch_points_at_the_plugin_provider_dir(tmp_path: Path) -> None:
    """提供元ディレクトリがある場合、--root の指定先を案内すること。

    プラグイン提供元リポジトリのルートは、それ自体はプラグインではないので
    consumer と判定される。「repo で測りたいのに consumer の点が出る」を自力で
    解けるよう、実際に指すべきパスを提示する。
    """
    provider = tmp_path / "plugins" / "demo" / ".claude-plugin"
    provider.mkdir(parents=True)
    (provider / "plugin.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match=r"--root plugins/demo"):
        harness_audit.build_report("repo", root_dir=tmp_path, target_mode="repo")
