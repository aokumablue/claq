"""ハーネス監査の走査・検出ヘルパーと repo チェック用の hooks.json 解析ヘルパーのテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

import claq.ci.harness_audit as harness_audit
from claq.ci.harness_audit_repo_checks import (
    _event_has_matching_command,
    _hook_command_argv,
    always_on_description_chars,
)

from ._support import _isolate_home as _isolate_home
from ._support import _write_files


def test_safe_helpers_and_counting(tmp_path: Path) -> None:
    """読み取り・JSON 解析・件数・拡張子・存在判定の各ヘルパーが中身の有無どおりに答える。"""
    _write_files(tmp_path, {"dir/a.js": "a\n", "dir/b.txt": "b\n", "dir/nested/c.js": "c\n", "blank.txt": "   \n"})
    (tmp_path / "binary.bin").write_bytes(b"\xff\xfe\x00\x01")

    assert harness_audit.file_has_content(tmp_path, "dir/a.js")
    assert not harness_audit.file_has_content(tmp_path, "binary.bin")
    assert not harness_audit.file_has_content(tmp_path, "blank.txt")
    assert not harness_audit.file_has_content(tmp_path, "missing.txt")
    assert not harness_audit.file_has_content(tmp_path, "dir")
    assert harness_audit.read_text(tmp_path, "dir/a.js") == "a\n"
    assert harness_audit.safe_read(tmp_path, "missing.txt") == ""
    assert harness_audit.safe_parse_json("") is None
    assert harness_audit.safe_parse_json("not-json") is None
    assert harness_audit.safe_parse_json('{"x": 1}') == {"x": 1}
    assert harness_audit.count_files(tmp_path, "dir", ".js") == 2
    assert harness_audit.count_files(tmp_path, "missing", ".js") == 0
    assert harness_audit.has_file_with_extension(tmp_path, "dir", ".js")
    assert harness_audit.has_file_with_extension(tmp_path, "dir", [".txt", ".js"])
    assert not harness_audit.has_file_with_extension(tmp_path, "dir", ".py")
    assert harness_audit._has_any_file(tmp_path, ["missing", "dir/a.js"])
    assert not harness_audit._has_any_file(tmp_path, ["missing-a", "missing-b"])

    _write_files(tmp_path, {".gitlab-ci.yml": "dependency_scanning:\n  stage: test\n"})
    assert harness_audit._has_gitlab_security_scanning(tmp_path)
    assert not harness_audit._has_gitlab_security_scanning(tmp_path / "missing")


_FLAT = ".claude/plugins/claq/.claude-plugin/plugin.json"
_CACHE = "home/.claude/plugins/cache/claq/claq/{}/.claude-plugin/plugin.json"


@pytest.mark.parametrize(
    ("home", "manifests", "expected"),
    [
        ("home", [_FLAT], _FLAT),
        ("", [_FLAT], _FLAT),
        ("home", [_CACHE.format(v) for v in ("0.9.9", "0.9.10", "1.0")], _CACHE.format("0.9.10")),
        ("home", [], None),
    ],
    ids=["root-flat", "root-flat-without-home", "home-marketplace-cache", "none"],
)
def test_find_plugin_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: str, manifests: list[str], expected: str | None
) -> None:
    """root 直下の平置きを（HOME が空でも）見つけ、ホームの cache 配置は辞書順で最初の版を返す。

    契約は「辞書順で最初」であって「最新版」ではない。0.9.10 は辞書順で先頭だが最新でも、
    作成順の先頭・末尾でもない。
    """
    monkeypatch.setenv("HOME", str(tmp_path / home) if home else "")
    _write_files(tmp_path, dict.fromkeys(manifests, "{}"))

    found = harness_audit.find_plugin_install(tmp_path)

    assert found == (str(tmp_path / expected) if expected else None)


@pytest.mark.parametrize(
    ("files", "minimum", "expected"),
    [
        ([], 1, False),
        (["src/app.py"], 1, False),
        (["src/app.py", "src/test_app.py"], 1, True),
        (["pkg/app_test.py"], 1, True),
        ([".venv/lib/site-packages/dateutil/test_parser.py"], 1, False),
        (["pkg/test_mod0.py", "pkg/test_mod1.py"], 2, True),
        (["pkg/test_mod0.py", "pkg/test_mod1.py"], 3, False),
        (["pkg/test_mod0.py", "pkg/test_mod1.py", "pkg/test_mod2.py"], 3, True),
    ],
    ids=["missing-root", "plain-py", "test-prefix", "test-suffix", "vendor", "two-of-2", "two-of-3", "three-of-3"],
)
def test_has_python_tests(tmp_path: Path, files: list[str], minimum: int, expected: bool) -> None:
    """`test_*.py` / `*_test.py` を依存ツリーの外で数え、`minimum` 件に届いたときだけ True を返す。

    「1 件で打ち切る」挙動が残ると、閾値 3 を要求する `consumer-eval-coverage` が 1 件で通る。
    """
    root = tmp_path / "root"
    _write_files(root, dict.fromkeys(files, "def test_x(): pass\n"))

    assert harness_audit.has_python_tests(root, minimum=minimum) is expected


def test_has_python_tests_skips_unreadable_directories(tmp_path: Path) -> None:
    """読めないディレクトリはスキップし、例外を送出しない（docstring の契約）。"""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o000)
    try:
        assert harness_audit.has_python_tests(tmp_path, minimum=1) is False
    finally:
        blocked.chmod(0o700)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ('"unterminated', None),
        ("claq-hook", None),
        ("python3 launcher.py a b", None),
        ("other-hook a b", None),
        ("claq-hook a b", ("a", "b")),
        ('"${ROOT}/runtime/claq-hook" a b', ("a", "b")),
        # Windows 側は cmd.exe が PATHEXT で解決する .cmd を直接書いた形式も受ける
        (r'"C:\\plug\\runtime\\claq-hook.cmd" a b', ("a", "b")),
        # 先頭の --bg（非 Claude ハーネス向け detach フラグ）は実体でないため除去する
        ("claq-hook --bg a b", ("a", "b")),
    ],
    ids=["bad-quote", "no-args", "python-launcher", "other-wrapper", "bare-wrapper", "wrapper-path", "windows-cmd", "bg"],
)
def test_hook_command_argv(command: str, expected: tuple[str, ...] | None) -> None:
    """wrapper 起動のコマンドだけから起動後の引数列を取り出し、それ以外は None を返す。"""
    assert _hook_command_argv(command) == expected


@pytest.mark.parametrize(
    ("event_hooks", "expected"),
    [
        ("not-a-list", False),
        ([], False),
        (["not-a-dict"], False),
        ([{"hooks": "not-a-list"}], False),
        ([{"hooks": ["not-a-dict"]}], False),
        ([{"hooks": [{"command": 123}]}], False),
        ([{"hooks": [{"command": "claq-hook"}]}], False),
        ([{"hooks": [{"command": "claq-hook claq.mem.cli other:action"}]}], False),
        ([{"hooks": [{"command": "claq-hook claq.mem.cli session:mem:setup"}]}], True),
    ],
    ids=[
        "event-not-list", "empty", "matcher-not-dict", "hooks-not-list", "hook-not-dict",
        "command-not-str", "no-args", "other-action", "match",
    ],
)
def test_event_has_matching_command(event_hooks: object, expected: bool) -> None:
    """型の崩れたエントリは飛ばし、起動引数がパターンに前方一致したときだけ True を返す。"""
    assert _event_has_matching_command(event_hooks, (("claq.mem.cli", "session:mem:setup"),)) is expected


@pytest.mark.parametrize(
    ("skill_md", "expected"),
    [
        ("# no frontmatter here\njust body\n", 0),
        ("---\nname: nokey\n---\n\n本文\n", 0),
        ("---\nname: a\ndescription: あいうえお\n---\n", 5),
        ("---\nname: a\ndescription: あいうえお\ndisable-model-invocation: true\n---\n", 0),
    ],
    ids=["no-frontmatter", "no-description", "counted", "slash-only"],
)
def test_always_on_description_chars(tmp_path: Path, skill_md: str, expected: int) -> None:
    """description の値を文字数で数え、description の無い定義と `disable-model-invocation: true` は数えない。"""
    _write_files(tmp_path, {"skills/a/SKILL.md": skill_md})

    assert always_on_description_chars(tmp_path) == expected
