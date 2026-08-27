"""追加の hook 分岐と境界値を検証するテスト。"""

from __future__ import annotations

import io
import json
import runpy
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

from bluecore.hooks import commit_quality_scanner as commit_quality_scanner
from bluecore.hooks import pre_bash_commit_quality as pre_bash_commit_quality


def test_pre_bash_commit_quality_detects_file_issues_and_commit_message_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = "\n".join(
        [
            'console.log("hi")',  # nosec
            "// console.log('commented')",  # nosec
            "debugger",  # nosec
            "// TODO: clean this up",  # nosec
            "// TODO: #123 tracked",
            "const " + "api" + "_key" + ' = "abc";',
        ]
    )
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    issues = commit_quality_scanner.find_file_issues("src/app.js")

    assert {issue["type"] for issue in issues} == {"console.log", "debugger", "todo", "secret"}  # nosec
    assert [issue["line"] for issue in issues if issue["type"] == "console.log"] == [1]  # nosec
    assert [issue["line"] for issue in issues if issue["type"] == "todo"] == [4]

    assert pre_bash_commit_quality.validate_commit_message("git status") is None
    message = pre_bash_commit_quality.validate_commit_message('git commit -m "feat(core): Add feature."')
    assert message is not None
    assert message["message"] == "feat(core): Add feature."
    assert {issue["type"] for issue in message["issues"]} == {"capitalization", "punctuation"}


def test_find_file_issues_skips_nosec_marked_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    """# nosec は console.log/debugger/todo を抑制するが、secret 検出は抑制しない。"""
    content = "\n".join(
        [
            'debugger  # nosec',
            "const " + "api" + "_key" + ' = "x";  # nosec',
        ]
    )
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    issues = commit_quality_scanner.find_file_issues("src/app.py")

    # デバッガ文は nosec で抑制される
    assert not any(issue["type"] == "debugger" for issue in issues)  # nosec
    # secret は nosec があっても検出される（バイパス防止）
    assert [issue["type"] for issue in issues] == ["secret"]
    assert issues[0]["line"] == 2


def test_find_file_issues_secret_detection_not_bypassed_by_nosec(monkeypatch: pytest.MonkeyPatch) -> None:
    """`# nosec` を付与しても api_key のようなシークレットパターンはブロック対象として検出され続ける。"""
    content = "api" + "_key" + ' = "hunter2secret"  # nosec'
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    issues = commit_quality_scanner.find_file_issues("src/app.py")

    assert len(issues) == 1
    assert issues[0]["type"] == "secret"
    assert issues[0]["severity"] == "error"


def test_find_file_issues_detects_anthropic_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """`sk-ant-...` 形式（ハイフン区切り）の Anthropic API キーも検出される。

    OpenAI 形式用パターン `sk-[a-zA-Z0-9]{20,}` は `sk-` 直後にハイフンを含む
    Anthropic 形式（`sk-ant-api03-...`）とは不一致だったため、専用パターンで
    検出する回帰。
    """
    content = "sk-" + "ant-" + "abcdefghijklmnopqrstuvwxyz0123456789"
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    issues = commit_quality_scanner.find_file_issues("src/app.py")

    assert len(issues) == 1
    assert issues[0]["type"] == "secret"
    assert issues[0]["severity"] == "error"


def test_find_file_issues_console_log_still_suppressed_by_nosec(monkeypatch: pytest.MonkeyPatch) -> None:
    """secret を含まない行では従来どおり console.log が nosec で抑制されること。"""  # nosec
    content = 'console.log("debug")  # nosec'
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    assert commit_quality_scanner.find_file_issues("src/app.py") == []


def test_find_file_issues_self_check_has_zero_secret_issues(monkeypatch: pytest.MonkeyPatch) -> None:
    """このフック自身のソースを検査しても secret 検出が0件であること（自己検出回避の nosec が secret を隠していないことの担保）。"""
    source_path = Path(commit_quality_scanner.__file__)
    own_source = source_path.read_text(encoding="utf-8")

    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: own_source)
    issues = commit_quality_scanner.find_file_issues(str(source_path))

    secret_issues = [issue for issue in issues if issue["type"] == "secret"]
    assert secret_issues == []


def test_find_file_issues_self_check_on_this_test_file_has_zero_issues() -> None:
    """このテストファイル自身を検査しても issue が0件であること（fixture 文字列の自己マッチ回帰防止）。

    cbabdad で secret 検出が nosec 無視・常時実行になった結果、本ファイルの
    シークレットパターン用 fixture 文字列がソース行として secret パターンに
    自己マッチし、本ファイルをコミットすると exitCode=2 でブロックされる
    回帰が発生した。本テストはその回帰を検知する。
    """
    this_file = Path(__file__)
    own_source = this_file.read_text(encoding="utf-8")

    with mock.patch.object(commit_quality_scanner, "get_staged_file_content", return_value=own_source):
        issues = commit_quality_scanner.find_file_issues(str(this_file))

    assert issues == []


def test_find_file_issues_detects_secret_in_non_lint_extension(monkeypatch: pytest.MonkeyPatch) -> None:
    """.py/.js 等の lint 対象外拡張子（例: .env）でも secret 検出が行われること。"""
    content = "API_" + "KEY" + '="hunter2secret"'
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    issues = commit_quality_scanner.find_file_issues(".env")

    assert [issue["type"] for issue in issues] == ["secret"]


def test_find_file_issues_lint_only_checks_skipped_for_non_lint_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lint 対象外拡張子では console.log/デバッガ文/todo は検出されないこと（secret のみ対象）。"""  # nosec
    content = 'console.log("hi")'  # nosec
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    assert commit_quality_scanner.find_file_issues(".env") == []


def test_find_file_issues_skips_secret_scan_for_lock_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """ロックファイルは内容が secret パターンに一致しても検出しないこと。"""
    content = "api" + "_key" + ' = "abc123"'
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    assert commit_quality_scanner.find_file_issues("package-lock.json") == []


def test_find_file_issues_oversized_files_detect_secret_past_old_1mib_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1MiB を超える位置に置かれた secret も検出されること（A-02 対応: サイズ
    による打ち切りを廃止したため、旧 1MiB cap の後ろにある secret も見逃さない）。
    lint（ログ出力チェック）はサイズに関わらず継続することも合わせて確認する。
    """  # nosec
    padding = "x" * (1024 * 1024 + 1)
    content = padding + "\n" + 'console.log("hi")' + "\n" + "api" + "_key" + ' = "abc123"'  # nosec
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    issues = commit_quality_scanner.find_file_issues("src/app.js")

    types = {issue["type"] for issue in issues}
    # 旧 1MiB cap は廃止済み。境界より後ろの secret も検出される。
    assert "secret" in types
    # lint はサイズに関わらず継続する
    assert "console.log" in types  # nosec


def test_find_file_issues_oversized_files_scan_prefix_for_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1MB 超のファイルでも、先頭側にある secret は検出されること。"""
    secret_line = "api" + "_key" + ' = "abc123"'
    padding = "x" * (1024 * 1024 + 1)
    content = secret_line + "\n" + padding
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    issues = commit_quality_scanner.find_file_issues("src/app.js")

    assert any(issue["type"] == "secret" and issue["line"] == 1 for issue in issues)


def test_find_file_issues_secret_scan_applies_under_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """1MB 以下のファイルは通常どおり secret スキャンされること（境界値確認）。"""
    padding = "x" * (1024 * 1024 - 100)
    content = padding + "\n" + "api" + "_key" + ' = "abc123"'
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    issues = commit_quality_scanner.find_file_issues("src/app.js")

    assert any(issue["type"] == "secret" for issue in issues)


def test_find_file_issues_binary_file_skips_lint_but_still_scans_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """バイナリ判定（先頭に NUL を含む）は lint だけを抑制し、secret scan は続ける。

    ADR-0013: 先頭に NUL を 1 バイト混ぜてバイナリ判定させるだけで secret 検査を
    まるごと回避できる状態は許容しない。行分割が意味を持たないため、印字可能
    文字列を抽出して同じパターンを当てる。
    """
    content = "\0binary preamble\n" + 'console.log("hi")\n' + "api" + "_key" + ' = "abc123"'  # nosec
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    issues = commit_quality_scanner.find_file_issues("weird.js")

    secrets = [issue for issue in issues if issue["type"] == "secret"]
    assert secrets, issues
    assert secrets[0]["severity"] == "error"
    assert "extracted binary content" in secrets[0]["message"]
    # lint は従来どおり抑制する（バイナリを行単位で lint しても意味がない）。
    assert "console.log" not in {issue["type"] for issue in issues}  # nosec
    # スキップの痕跡 issue は不要になったので出さない。
    assert "secret_scan_skipped" not in {issue["type"] for issue in issues}


def test_find_file_issues_real_binary_is_not_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """真のバイナリ（画像等）は secret パターンに当たらず error を出さないこと。

    バイナリ commit を一律ブロックしないという既存の要件を維持する。
    """
    png = "\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x01\x00\x00\x00\x01\x00\x08\x06"
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: png)

    issues = commit_quality_scanner.find_file_issues("logo.png")

    assert [issue for issue in issues if issue["severity"] == "error"] == []


def test_extract_printable_runs_splits_on_control_characters() -> None:
    """制御文字で区切られた短すぎる断片は走査単位に含めないこと。"""
    runs = commit_quality_scanner._extract_printable_runs("ab\x00longer-run\x01cd")

    assert runs == ["longer-run"]


def test_scan_secret_issues_raises_on_time_budget_exceeded(monkeypatch: pytest.MonkeyPatch) -> None:
    """secret scan の実時間バジェット超過は例外として送出され、
    find_file_issues 側で scan_error（severity error、fail-closed）になること。"""
    calls = iter([0.0, 1_000.0])
    monkeypatch.setattr(commit_quality_scanner, "_monotonic", lambda: next(calls))

    with pytest.raises(commit_quality_scanner.SecretScanBudgetExceeded):
        commit_quality_scanner._scan_secret_issues("line one", ["line one"])


def test_find_file_issues_secret_scan_budget_exceeded_is_scan_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """find_file_issues 経由でも時間バジェット超過は scan_error（error）として報告される。"""
    calls = iter([0.0, 1_000.0])
    monkeypatch.setattr(commit_quality_scanner, "_monotonic", lambda: next(calls))
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: "some text")

    issues = commit_quality_scanner.find_file_issues("src/app.js")

    scan_errors = [issue for issue in issues if issue["type"] == "scan_error"]
    assert len(scan_errors) == 1
    assert scan_errors[0]["severity"] == "error"


def test_find_file_issues_secret_scan_exception_is_reported_as_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """secret scanner が例外を投げても issue が消えず、error として報告される（F-06a 対応）。

    以前は find_file_issues 全体を覆う `except Exception: pass` が例外を
    握り潰しており、secret scanner の内部バグでも commit を許可していた
    （false negative のコストが高い secret 検出が最も危険な形で fail-open
    していた）。lint 側の結果は例外の影響を受けず生存することも確認する。
    """
    content = 'console.log("hi")'  # nosec
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)
    monkeypatch.setattr(
        commit_quality_scanner,
        "_scan_secret_issues",
        mock.Mock(side_effect=RuntimeError("boom")),
    )

    issues = commit_quality_scanner.find_file_issues("src/app.js")

    types = {issue["type"] for issue in issues}
    assert "console.log" in types  # lint 側は secret scanner の例外の影響を受けない  # nosec
    scan_errors = [issue for issue in issues if issue["type"] == "scan_error"]
    assert len(scan_errors) == 1
    assert scan_errors[0]["severity"] == "error"
    assert "src/app.js" in scan_errors[0]["message"]
    assert "RuntimeError" in scan_errors[0]["message"]


def test_find_file_issues_lint_scan_exception_is_reported_as_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lint scanner の例外は warning として報告し、secret 側は生存する。

    lint と secret で severity を非対称にする（lint は false negative の
    コストが secret ほど高くないため warning に留め、commit をブロックしない）。
    """
    content = "api" + "_key" + ' = "abc123"'  # nosec
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)
    monkeypatch.setattr(
        commit_quality_scanner,
        "_scan_lint_issues",
        mock.Mock(side_effect=ValueError("boom")),
    )

    issues = commit_quality_scanner.find_file_issues("src/app.js")

    types = {issue["type"] for issue in issues}
    assert "secret" in types
    scan_errors = [issue for issue in issues if issue["type"] == "scan_error"]
    assert len(scan_errors) == 1
    assert scan_errors[0]["severity"] == "warning"
    assert "ValueError" in scan_errors[0]["message"]


def test_find_file_issues_scan_target_detection_exception_is_reported_as_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """走査対象判定自体（_is_binary_content 等）が例外を投げても検査不能を報告する。"""
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: "content")
    monkeypatch.setattr(
        commit_quality_scanner,
        "_is_binary_content",
        mock.Mock(side_effect=OSError("boom")),
    )

    issues = commit_quality_scanner.find_file_issues("src/app.js")

    assert len(issues) == 1
    assert issues[0]["type"] == "scan_error"
    assert issues[0]["severity"] == "error"
    assert "OSError" in issues[0]["message"]


def test_evaluate_scans_non_lint_extension_files_for_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """evaluate() が lint 非対象拡張子（.env 等）もステージ済みファイルの走査対象に含めること。"""
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git commit -m 'feat(core): add'"}},
    )
    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_files", lambda: [".env", "package-lock.json"])

    seen: list[str] = []

    def _fake_find_file_issues(path: str, *, repo_root: Path | None = None) -> list[dict]:
        seen.append(path)
        return []

    monkeypatch.setattr(pre_bash_commit_quality, "find_file_issues", _fake_find_file_issues)
    monkeypatch.setattr(pre_bash_commit_quality, "validate_commit_message", lambda command: None)

    assert pre_bash_commit_quality.evaluate("payload") == {"output": "payload", "exitCode": 0}
    # .env は secret スキャン対象なので走査される。package-lock.json は
    # lint 非対象・secret 対象外の双方であるため走査対象から除外される。
    assert seen == [".env"]


def test_pre_bash_commit_quality_helpers_handle_subprocess_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pre_bash_commit_quality.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        commit_quality_scanner.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, stdout="", stderr=""),
    )

    # git 自体が非0終了した場合は「0 件」ではなく None（検査不能）を返す
    assert pre_bash_commit_quality.get_staged_files() is None
    assert commit_quality_scanner.get_staged_file_content("src/app.js") is None
    assert commit_quality_scanner.should_lint_file("src/app.py")
    assert not commit_quality_scanner.should_lint_file("src/app.txt")


@pytest.mark.parametrize(
    "file_path",
    [
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Cargo.lock",
        "poetry.lock",
        "uv.lock",
        "Pipfile.lock",
        "vendor/package-lock.json",
        "dist/bundle.min.js",
        "static/app.min.css",
    ],
)
def test_should_scan_secrets_excludes_lock_and_minified_files(file_path: str) -> None:
    """ロックファイルと圧縮生成物（*.min.js/*.min.css）は secret スキャン対象外であること。"""
    assert pre_bash_commit_quality.should_scan_secrets(file_path) is False


@pytest.mark.parametrize(
    "file_path",
    [
        ".env",
        "config.json",
        "app.yaml",
        "deploy.sh",
        "Dockerfile",
        "src/app.txt",
        "tests/fixtures/secret.env",
    ],
)
def test_should_scan_secrets_includes_non_lint_extensions(file_path: str) -> None:
    """.env/.yaml/.sh/Dockerfile 等、lint 対象外の拡張子でも secret スキャン対象であること
    （tests/ 配下も除外しない）。"""
    assert pre_bash_commit_quality.should_scan_secrets(file_path) is True


def test_pre_bash_commit_quality_helpers_return_success_outputs(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(
        command: list[str],
        *,
        capture_output: bool,
        check: bool,
        text: bool = False,
        timeout: float | None = None,
    ):
        if command[:2] == ["git", "diff"]:
            return subprocess.CompletedProcess(command, 0, stdout="src/app.py\nsrc/tool.ts\n", stderr="")
        if command[:2] == ["git", "show"]:
            # get_staged_file_content はバイナリ判定のため text=True を付けずに bytes で取得する
            return subprocess.CompletedProcess(command, 0, stdout=b"file content", stderr=b"")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(pre_bash_commit_quality.subprocess, "run", fake_run)
    monkeypatch.setattr(commit_quality_scanner.subprocess, "run", fake_run)

    assert pre_bash_commit_quality.get_staged_files() == ["src/app.py", "src/tool.ts"]
    assert commit_quality_scanner.get_staged_file_content("src/app.js") == "file content"


def test_get_staged_file_content_does_not_filter_binary_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_staged_file_content 自体はバイナリ判定を行わず、NUL を含む内容もそのまま返すこと。

    バイナリ判定（`_is_binary_content`）は `find_file_issues` 側で lint 抑制の
    みに使う設計であり、ここで None を返してしまうと secret 検出まで巻き
    込んで全放棄されてしまう（NUL 1個で secret 検査を回避できる抜け道）ため、
    取得層では判定しないことを保証する。
    """
    binary_bytes = b"PNG\x00\x01\x02fake image bytes"

    monkeypatch.setattr(
        commit_quality_scanner.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout=binary_bytes, stderr=b""),
    )
    content = commit_quality_scanner.get_staged_file_content("image.png")
    assert content is not None
    assert "\0" in content


def test_get_staged_file_content_replaces_invalid_utf8_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不正な UTF-8 バイト列でも UnicodeDecodeError を送出せず置換して返すこと
    （旧 text=True 実装の潜在バグの回帰防止）。"""
    invalid_utf8 = b"valid text \xff\xfe invalid bytes"

    monkeypatch.setattr(
        commit_quality_scanner.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout=invalid_utf8, stderr=b""),
    )
    content = commit_quality_scanner.get_staged_file_content("weird_encoding.txt")
    assert content is not None
    assert "valid text" in content


def test_pre_bash_commit_quality_finds_parser_and_reading_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """ファイル読み取り自体が例外を投げても、握り潰さず scan_error として報告する（F-06a 対応）。

    get_staged_file_content は自前で例外を握り潰し None を返す契約だが、
    find_file_issues 側でもその契約破りに備えたガードを持つ。以前は
    ここも含めて find_file_issues 全体を覆う except Exception: pass が
    無条件に [] を返しており、検査したのか・対象外だったのかが区別
    できなかった。
    """
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: (_ for _ in ()).throw(RuntimeError("boom")))
    issues = commit_quality_scanner.find_file_issues("src/app.js")
    assert len(issues) == 1
    assert issues[0]["type"] == "scan_error"
    assert issues[0]["severity"] == "error"
    assert "RuntimeError" in issues[0]["message"]

    long_message = "git commit -m \"bad message with no conventional format and a very long subject line that keeps going.\""
    parsed = pre_bash_commit_quality.validate_commit_message(long_message)
    assert parsed is not None
    assert {issue["type"] for issue in parsed["issues"]} >= {"format", "length"}


def test_pre_bash_commit_quality_run_wrapper_and_main_success(monkeypatch: pytest.MonkeyPatch) -> None:
    assert pre_bash_commit_quality.run("payload") == pre_bash_commit_quality.evaluate("payload")

    monkeypatch.setattr(
        "bluecore.hooks.hook_common.read_raw_stdin_with_truncation", lambda: ("payload", False)
    )
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "evaluate",
        lambda raw: {"output": raw, "exitCode": 0},
    )
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        assert pre_bash_commit_quality.main() == 0

    assert stdout.getvalue() == ""

    monkeypatch.setattr(
        pre_bash_commit_quality,
        "evaluate",
        lambda raw: {"output": raw, "exitCode": 2, "reason": "[Hook] BLOCKED: boom"},
    )
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        assert pre_bash_commit_quality.main() == 2

    # ブロック時は stdout に permissionDecision: deny の合併 JSON を出す。
    deny = json.loads(stdout.getvalue())
    assert deny["permissionDecision"] == "deny"


def test_pre_bash_commit_quality_evaluate_handles_commit_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs: list[str] = []
    monkeypatch.setattr(pre_bash_commit_quality, "log", logs.append)

    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git status"}},
    )
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "get_staged_files",
        lambda: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    assert pre_bash_commit_quality.evaluate("payload") == {"output": "payload", "exitCode": 0}

    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git commit -m 'feat(core): add'"}},
    )
    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_files", lambda: [])
    assert pre_bash_commit_quality.evaluate("payload") == {"output": "payload", "exitCode": 0}
    assert any("No staged files found" in message for message in logs)


def test_pre_bash_commit_quality_blocks_on_error_and_allows_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git commit -m 'feat(core): add'"}},
    )
    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_files", lambda: ["src/app.js"])
    monkeypatch.setattr(pre_bash_commit_quality, "should_lint_file", lambda path: True)

    monkeypatch.setattr(
        pre_bash_commit_quality,
        "find_file_issues",
        lambda path, *, repo_root=None: [{"severity": "error", "line": 1, "message": "boom"}],
    )
    monkeypatch.setattr(pre_bash_commit_quality, "validate_commit_message", lambda command: None)
    assert pre_bash_commit_quality.evaluate("payload") == {
        "output": "payload",
        "exitCode": 2,
        "reason": "[Hook] BLOCKED: 1 error(s) found in staged files. Fix them before committing.",
    }

    warning_logs: list[str] = []
    monkeypatch.setattr(pre_bash_commit_quality, "log", warning_logs.append)
    monkeypatch.setattr(pre_bash_commit_quality, "find_file_issues", lambda path, *, repo_root=None: [])
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "validate_commit_message",
        lambda command: {
            "message": "feat(core): Add feature.",
            "issues": [{"message": "warn", "suggestion": "tip"}],
        },
    )
    assert pre_bash_commit_quality.evaluate("payload") == {"output": "payload", "exitCode": 0}
    assert any("Commit Message Issues" in message for message in warning_logs)
    assert any("WARNING" in message for message in warning_logs)


def test_pre_bash_commit_quality_counts_warning_and_info_issues(monkeypatch: pytest.MonkeyPatch) -> None:
    logs: list[str] = []
    monkeypatch.setattr(pre_bash_commit_quality, "log", logs.append)
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git commit -m 'feat(core): add'"}},
    )
    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_files", lambda: ["src/app.js"])
    monkeypatch.setattr(pre_bash_commit_quality, "should_lint_file", lambda path: True)
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "find_file_issues",
        lambda path, *, repo_root=None: [
            {"severity": "warning", "line": 1, "message": "warn"},
            {"severity": "info", "line": 2, "message": "info"},
        ],
    )
    monkeypatch.setattr(pre_bash_commit_quality, "validate_commit_message", lambda command: None)

    assert pre_bash_commit_quality.evaluate("payload") == {"output": "payload", "exitCode": 0}
    assert any("1 warning(s), 1 info" in message for message in logs)


def test_pre_bash_commit_quality_evaluate_logs_and_recovers_from_parser_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    logs: list[str] = []
    monkeypatch.setattr(pre_bash_commit_quality, "log", logs.append)
    monkeypatch.setattr(pre_bash_commit_quality, "parse_json_object", lambda raw: (_ for _ in ()).throw(RuntimeError("boom")))

    assert pre_bash_commit_quality.evaluate("payload") == {"output": "payload", "exitCode": 0}
    assert any("Error: boom" in message for message in logs)


def test_pre_bash_commit_quality_main_handles_reader_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "bluecore.hooks.hook_common.read_raw_stdin",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    assert pre_bash_commit_quality.main() == 0


def test_pre_bash_commit_quality_helpers_and_pass_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pre_bash_commit_quality.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("missing")),
    )
    monkeypatch.setattr(
        commit_quality_scanner.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("missing")),
    )

    assert pre_bash_commit_quality.get_staged_files() is None
    assert commit_quality_scanner.get_staged_file_content("src/app.js") is None
    # content 取得不能（None）は「検査したが問題なし」ではなく scan_error
    # として積まれ、severity=error でブロック対象になる
    issues = commit_quality_scanner.find_file_issues("src/app.js")
    assert len(issues) == 1
    assert issues[0]["type"] == "scan_error"
    assert issues[0]["severity"] == "error"

    logs: list[str] = []
    monkeypatch.setattr(pre_bash_commit_quality, "log", logs.append)
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git commit -m 'feat(core): add'" }},
    )
    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_files", lambda: ["src/app.js"])
    monkeypatch.setattr(pre_bash_commit_quality, "should_lint_file", lambda path: True)
    monkeypatch.setattr(pre_bash_commit_quality, "find_file_issues", lambda path, *, repo_root=None: [])
    monkeypatch.setattr(pre_bash_commit_quality, "validate_commit_message", lambda command: None)

    assert pre_bash_commit_quality.evaluate("payload") == {"output": "payload", "exitCode": 0}
    assert any("PASS: All checks passed" in message for message in logs)


def test_pre_bash_commit_quality_entrypoint_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("bluecore.hooks.pre_bash_commit_quality", run_name="__main__")

    assert excinfo.value.code == 0


def test_validate_commit_message_lowercase_no_period() -> None:
    """conventional commit で小文字始まり・末尾ピリオド無しなら指摘なし。"""
    from bluecore.hooks.pre_bash_commit_quality import validate_commit_message

    result = validate_commit_message('git commit -m "feat: add new feature"')
    assert result is not None
    types = {i["type"] for i in result["issues"]}
    assert "capitalization" not in types
    assert "punctuation" not in types


def test_count_file_issues_unknown_severity(monkeypatch) -> None:
    """未知の severity は安全側に倒し error として加算する（黙って見逃さない）。"""
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    monkeypatch.setattr(
        pbcq, "find_file_issues", lambda fp, *, repo_root=None: [{"severity": "unknown", "line": 1, "message": "x"}]
    )
    monkeypatch.setattr(pbcq, "log", lambda *a, **k: None)
    total, err, warn, info = pbcq._count_file_issues(["f.py"])
    assert (total, err, warn, info) == (1, 1, 0, 0)


def test_apply_commit_message_issues_without_suggestion(monkeypatch) -> None:
    """suggestion の無い issue でもカウントを加算する。"""
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    monkeypatch.setattr(pbcq, "log", lambda *a, **k: None)
    monkeypatch.setattr(pbcq, "validate_commit_message", lambda cmd: {"issues": [{"message": "m"}]})
    total, warn = pbcq._apply_commit_message_issues("git commit -m x", 0, 0)
    assert (total, warn) == (1, 1)


# ─────────────────────────────────────────────
# _is_git_commit_command / _is_commit_all_flag（トークン化）
# ─────────────────────────────────────────────


def test_is_git_commit_command_detects_double_space() -> None:
    """連続空白（git  commit）でも commit 検出が成立すること。"""
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    is_commit, args = pbcq._is_git_commit_command("git  commit -m x")
    assert is_commit is True
    assert args == ["-m", "x"]


def test_is_git_commit_command_detects_newline_separated() -> None:
    """改行区切り（git\\ncommit）でも commit 検出が成立すること。"""
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    is_commit, _args = pbcq._is_git_commit_command("git\ncommit -m x")
    assert is_commit is True


def test_is_git_commit_command_skips_global_dash_c_option() -> None:
    """`git -C <path> commit` のようなグローバルオプション付きでも検出できること。"""
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    is_commit, args = pbcq._is_git_commit_command("git -C /tmp/repo commit -m x")
    assert is_commit is True
    assert args == ["-m", "x"]


def test_is_git_commit_command_skips_unknown_global_option_with_value() -> None:
    """allowlist に無いグローバルオプション（--exec-path 等）でも検出漏れしないこと。

    旧実装は既知オプション（-C/-c 等）のみ値トークンをスキップしていたため、
    `--exec-path <path>` のような未知オプションの値トークンで走査が
    打ち切られ検出漏れになっていた（フェイルセーフ違反）。新実装は
    オプション・値を区別せず commit まで読み飛ばす。
    """
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    is_commit, args = pbcq._is_git_commit_command("git --exec-path /foo commit -m x")
    assert is_commit is True
    assert args == ["-m", "x"]


def test_is_git_commit_command_skips_unknown_global_option_detects_dash_a() -> None:
    """未知グローバルオプション経由でも -a 相当のフラグが検出できること。"""
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    is_commit, args = pbcq._is_git_commit_command("git --super-prefix /x commit -am y")
    assert is_commit is True
    assert pbcq._is_commit_all_flag(args) is True


def test_is_git_commit_command_detects_within_composite_command() -> None:
    """`&&` で連結された複合コマンド中の git commit も検出できること。"""
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    is_commit, args = pbcq._is_git_commit_command("echo hi && git commit -m x")
    assert is_commit is True
    assert args == ["-m", "x"]


def test_is_git_commit_command_returns_false_for_non_commit() -> None:
    """git commit を含まないコマンドは False を返すこと。"""
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    is_commit, args = pbcq._is_git_commit_command("git status")
    assert is_commit is False
    assert args == []


def test_is_git_commit_command_falls_back_on_shlex_failure() -> None:
    """クォート不整合（heredoc 等）で shlex.split が失敗しても検出を継続すること。"""
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    broken_command = "git commit -m 'unterminated quote"
    is_commit, _args = pbcq._is_git_commit_command(broken_command)
    assert is_commit is True


def test_is_git_commit_command_fallback_non_commit_returns_false() -> None:
    """フォールバック経路でも git commit を含まなければ False を返すこと。"""
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    broken_command = "echo 'unterminated quote"
    is_commit, args = pbcq._is_git_commit_command(broken_command)
    assert is_commit is False
    assert args == []


def test_is_git_commit_command_returns_false_when_only_options_follow_git() -> None:
    """git の後がオプションのみで commit が現れなければ False を返すこと。"""
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    is_commit, args = pbcq._is_git_commit_command("git --version")
    assert is_commit is False
    assert args == []


def test_is_git_commit_command_regex_fallback_true_when_tokens_miss_it() -> None:
    """トークン走査では見つからなくても、引用符内のテキスト等で `git commit` が
    文字列として現れれば保守的に True を返すこと（過剰検出側のフェイルセーフ）。
    """
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    # 実際は `git status` だが、引用符内のメッセージに "git commit" という
    # 語が偶然含まれるケース。トークン走査では検出できないため、regex
    # フォールバックが保守的に True を返す。
    is_commit, args = pbcq._is_git_commit_command("git status -m 'please run git commit later'")
    assert is_commit is True
    assert args == []


@pytest.mark.parametrize(
    "command",
    [
        "git.exe commit -m 'feat: x'",
        "/usr/bin/git commit -m 'feat: x'",
        "GIT commit -m 'feat: x'",
        "Git.EXE commit -m 'feat: x'",
        "'C:\\Program Files\\Git\\bin\\git.exe' commit -m 'feat: x'",
    ],
)
def test_is_git_commit_command_detects_exe_and_absolute_path_tokens(command: str) -> None:
    """basename 化により `.exe`・絶対パス・大文字表記の git 実行ファイルもトークン走査で検出する（R-12）。

    以前は `token != "git"` の完全一致だったため、`/usr/bin/git` は regex
    フォールバックにのみ救われ、`git.exe` は `.exe` が `\\s` を破るため
    regex フォールバックからもすり抜けていた。
    """
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    is_commit, _args = pbcq._is_git_commit_command(command)
    assert is_commit is True


def test_is_git_commit_command_exe_token_walk_not_regex_fallback() -> None:
    """`git.exe commit` はトークン走査自体で捕まる（regex フォールバック頼みではない）。"""
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    args = pbcq._find_git_commit_args_in_segment(["git.exe", "commit", "-m", "x"])
    assert args == ["-m", "x"]


def test_is_commit_all_flag_detects_short_long_and_combined() -> None:
    """-a / --all / -am のような結合短形式を検出すること。"""
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    assert pbcq._is_commit_all_flag(["-a", "-m", "x"]) is True
    assert pbcq._is_commit_all_flag(["--all", "-m", "x"]) is True
    assert pbcq._is_commit_all_flag(["-am", "x"]) is True
    assert pbcq._is_commit_all_flag(["-m", "x"]) is False
    assert pbcq._is_commit_all_flag(["--amend", "-m", "x"]) is False


def test_evaluate_malformed_json_with_commit_text_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """JSON が壊れていても raw 文字列上に `git commit` が見えれば deny する（R-01a）。

    matcher が Bash 全体（commit と無関係な呼び出しも含む）のため、
    malformed というだけで一律 deny すると commit と無関係な Bash 呼び出し
    まで巻き込む。raw 文字列に commit が見える場合のみブロックする。
    """
    logs: list[str] = []
    monkeypatch.setattr(pre_bash_commit_quality, "log", logs.append)

    raw = '{"tool_input": {"command": "git commit -m \'feat: x\'"'  # 閉じ括弧欠落で壊れた JSON
    result = pre_bash_commit_quality.evaluate(raw)

    assert result["exitCode"] == 2
    assert "reason" in result
    assert any("git commit" in message for message in logs)


def test_evaluate_malformed_json_without_commit_text_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JSON が壊れていて commit とも無関係なら非ブロッキングで通過する。"""
    logs: list[str] = []
    monkeypatch.setattr(pre_bash_commit_quality, "log", logs.append)

    result = pre_bash_commit_quality.evaluate("not-json")

    assert result == {"output": "not-json", "exitCode": 0}
    assert any("could not parse hook input" in message for message in logs)


def test_evaluate_empty_input_passes_through_silently(monkeypatch: pytest.MonkeyPatch) -> None:
    """空/空白入力は従来どおりログ無しで非ブロッキング。"""
    logs: list[str] = []
    monkeypatch.setattr(pre_bash_commit_quality, "log", logs.append)

    assert pre_bash_commit_quality.evaluate("") == {"output": "", "exitCode": 0}
    assert logs == []


def test_evaluate_valid_empty_json_object_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """壊れてはいないが空の JSON（`{}`）は commit 情報が取れず非ブロッキング。"""
    assert pre_bash_commit_quality.evaluate("{}") == {"output": "{}", "exitCode": 0}


def test_evaluate_confirmed_commit_staged_files_unavailable_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_staged_files が None（git 失敗/timeout）を返すと deny する（0 件とは区別）。"""
    logs: list[str] = []
    monkeypatch.setattr(pre_bash_commit_quality, "log", logs.append)
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git commit -m 'feat: x'"}},
    )
    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_files", lambda: None)

    result = pre_bash_commit_quality.evaluate("payload")

    assert result["exitCode"] == 2
    assert "reason" in result
    assert any("could not determine staged files" in message for message in logs)


def test_evaluate_confirmed_commit_scan_exception_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """commit と確定した後の想定外例外は fail-open ではなく fail-closed にする（R-01b）。"""
    logs: list[str] = []
    monkeypatch.setattr(pre_bash_commit_quality, "log", logs.append)
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git commit -m 'feat: x'"}},
    )

    def _boom() -> list[str]:
        raise RuntimeError("unexpected scanner crash")

    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_files", _boom)

    result = pre_bash_commit_quality.evaluate("payload")

    assert result["exitCode"] == 2
    assert "reason" in result
    assert any("unexpected scanner crash" in message for message in logs)


def test_evaluate_commit_amend_with_nothing_staged_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--amend --no-edit` で staged が空なら検査対象ゼロで通過する（正常系。gap ではない）。

    連続空白を含む --amend コマンドでも commit 検出が成立することも併せて確認。
    """
    logs: list[str] = []
    monkeypatch.setattr(pre_bash_commit_quality, "log", logs.append)
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git   commit  --amend --no-edit"}},
    )
    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_files", lambda: [])

    assert pre_bash_commit_quality.evaluate("payload") == {"output": "payload", "exitCode": 0}
    assert any("No staged files found" in message for message in logs)


def test_evaluate_commit_amend_scans_staged_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--amend` は HEAD のファイル一覧を巻き込まず、staged 集合だけを検査する（F-06 対応）。

    以前は --amend を検出した時点で lint/secret/message 検証を無条件スキップして
    おり、amend でこっそり secret を混入させても検査を通過できていた
    （F-01 とは別の fail-open）。
    """
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git commit --amend -m 'feat(core): add'"}},
    )
    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_files", lambda: ["src/staged.py"])
    monkeypatch.setattr(pre_bash_commit_quality, "should_lint_file", lambda path: True)
    monkeypatch.setattr(pre_bash_commit_quality, "should_scan_secrets", lambda path: False)

    seen: list[str] = []

    def _fake_find_file_issues(path: str, *, repo_root: Path | None = None) -> list[dict]:
        seen.append(path)
        return [{"type": "secret", "severity": "error", "message": "hardcoded secret detected", "line": 1}]

    monkeypatch.setattr(pre_bash_commit_quality, "find_file_issues", _fake_find_file_issues)

    result = pre_bash_commit_quality.evaluate("payload")

    # staged 集合は検査され、amend であることを理由にブロックが回避されない。
    assert seen == ["src/staged.py"]
    assert result["exitCode"] == 2


def test_evaluate_commit_dash_a_unions_unstaged_modified_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """`git commit -a` では未ステージの変更ファイルもスキャン対象に加わり、
    その分は作業ツリー（repo_root 経由）から読まれること
    （INDEX を読んで機能していなかった実欠陥の回帰防止）。"""
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git commit -am 'feat(core): add'"}},
    )
    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_files", lambda: ["src/staged.py"])
    monkeypatch.setattr(
        pre_bash_commit_quality, "get_unstaged_modified_files", lambda: ["src/unstaged.py"]
    )
    monkeypatch.setattr(pre_bash_commit_quality, "should_lint_file", lambda path: True)

    dummy_repo_root = Path("/dummy/repo")
    monkeypatch.setattr(pre_bash_commit_quality, "resolve_repo_root", lambda: dummy_repo_root)

    seen: list[tuple[str, Path | None]] = []

    def _fake_find_file_issues(path: str, *, repo_root: Path | None = None) -> list[dict]:
        seen.append((path, repo_root))
        return []

    monkeypatch.setattr(pre_bash_commit_quality, "find_file_issues", _fake_find_file_issues)
    monkeypatch.setattr(pre_bash_commit_quality, "validate_commit_message", lambda command: None)

    assert pre_bash_commit_quality.evaluate("payload") == {"output": "payload", "exitCode": 0}
    assert dict(seen) == {"src/staged.py": None, "src/unstaged.py": dummy_repo_root}


def test_evaluate_commit_dash_a_worktree_blocked_when_repo_root_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """repo root が解決できない場合、-a の未ステージ分は fail-closed で deny されること。

    `-a` で実際にコミットされる未ステージ変更を一切検査できないまま
    「問題なし」を返すと、R-01 で塞いだ get_staged_files() の fail-open と
    同じ穴が worktree 側に残る。staged 側と対称に fail-closed にする。
    """
    logs: list[str] = []
    monkeypatch.setattr(pre_bash_commit_quality, "log", logs.append)
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git commit -am 'feat(core): add'"}},
    )
    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_files", lambda: ["src/staged.py"])
    monkeypatch.setattr(
        pre_bash_commit_quality, "get_unstaged_modified_files", lambda: ["src/unstaged.py"]
    )
    monkeypatch.setattr(pre_bash_commit_quality, "should_lint_file", lambda path: True)
    monkeypatch.setattr(pre_bash_commit_quality, "resolve_repo_root", lambda: None)

    seen: list[str] = []

    def _fake_find_file_issues(path: str, *, repo_root: Path | None = None) -> list[dict]:
        seen.append(path)
        return []

    monkeypatch.setattr(pre_bash_commit_quality, "find_file_issues", _fake_find_file_issues)
    monkeypatch.setattr(pre_bash_commit_quality, "validate_commit_message", lambda command: None)

    result = pre_bash_commit_quality.evaluate("payload")

    assert result["exitCode"] == 2
    assert "reason" in result
    # index 側は検査済みだが worktree 側（未ステージ分）には repo root 解決前に到達しない
    assert seen == ["src/staged.py"]
    assert any("could not resolve repo root" in message for message in logs)


def test_evaluate_commit_without_dash_a_ignores_unstaged_modified_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`-a` の無い通常コミットでは未ステージの変更ファイルを取得しないこと。"""
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git commit -m 'feat(core): add'"}},
    )
    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_files", lambda: ["src/staged.py"])
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "get_unstaged_modified_files",
        lambda: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    monkeypatch.setattr(pre_bash_commit_quality, "should_lint_file", lambda path: True)
    monkeypatch.setattr(pre_bash_commit_quality, "find_file_issues", lambda path, *, repo_root=None: [])
    monkeypatch.setattr(pre_bash_commit_quality, "validate_commit_message", lambda command: None)

    assert pre_bash_commit_quality.evaluate("payload") == {"output": "payload", "exitCode": 0}


def _init_repo_with_initial_commit(repo: Path, initial_content: str) -> Path:
    """テスト用に実 git リポジトリを初期化し、app.py を1回コミットする。"""
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
    app_py = repo / "app.py"
    app_py.write_text(initial_content, encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "feat(core): initial"], cwd=repo, check=True, capture_output=True)
    return app_py


def test_evaluate_commit_dash_a_detects_secret_in_unstaged_worktree_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`git commit -a` は未ステージの作業ツリー変更を読み、新規シークレットを
    検出すること（INDEX（git show :file）を読んでいたため未ステージの
    シークレットが検出できなかった実欠陥の回帰防止。空サンドボックスで
    CRITICAL として実証済みのシナリオ）。"""
    repo = tmp_path / "repo"
    app_py = _init_repo_with_initial_commit(repo, "value = 'clean'\n")

    # 作業ツリーのみ変更（git add しない）— INDEX は "clean" のまま
    secret_line = "api" + "_key" + ' = "hunter2secret"'
    app_py.write_text("value = 'clean'\n" + secret_line + "\n", encoding="utf-8")

    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git commit -am 'feat(core): update'"}},
    )

    result = pre_bash_commit_quality.evaluate("payload")

    assert result["exitCode"] == 2


def test_evaluate_commit_without_dash_a_reads_index_when_worktree_diverges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`-a` を伴わない通常コミットでは、ステージ後に作業ツリーがさらに変更
    されても INDEX（ステージ済み内容）のシークレットを検出すること
    （読み取り元が誤って作業ツリーに切り替わっていないことの回帰防止＝
    従来の staged 経路の不変性確認）。"""
    repo = tmp_path / "repo"
    app_py = _init_repo_with_initial_commit(repo, "value = 'clean'\n")

    secret_line = "api" + "_key" + ' = "hunter2secret"'
    app_py.write_text("value = 'clean'\n" + secret_line + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True, capture_output=True)

    # ステージ後、作業ツリーだけをさらに変更して secret を除去（再ステージしない）
    app_py.write_text("value = 'clean'\n", encoding="utf-8")

    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git commit -m 'feat(core): update'"}},
    )

    result = pre_bash_commit_quality.evaluate("payload")

    # INDEX（ステージ済み secret 版）を読んで検出する。誤って作業ツリー
    # （secret 除去後）を読んでいれば exitCode は 0 になってしまう。
    assert result["exitCode"] == 2


def test_get_unstaged_modified_files_returns_success_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """`git diff HEAD` の成功出力からファイル一覧を返すこと。"""

    def fake_run(
        command: list[str],
        *,
        capture_output: bool,
        text: bool,
        check: bool,
        timeout: float | None = None,
    ):
        assert command == ["git", "diff", "HEAD", "--name-only", "--diff-filter=ACMR"]
        return subprocess.CompletedProcess(command, 0, stdout="src/a.py\nsrc/b.py\n", stderr="")

    monkeypatch.setattr(pre_bash_commit_quality.subprocess, "run", fake_run)
    assert pre_bash_commit_quality.get_unstaged_modified_files() == ["src/a.py", "src/b.py"]


def _fake_git(results: dict[tuple[str, ...], subprocess.CompletedProcess]):
    """argv の先頭 3 語をキーに CompletedProcess を返す subprocess.run の差し替えを作る。"""

    def fake_run(argv, *args, **kwargs):
        return results[tuple(argv[:3])]

    return fake_run


def test_get_unstaged_modified_files_returns_empty_when_head_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """初回コミット（HEAD 不在）で diff が失敗した場合は空リストを返すこと。

    HEAD が無いのは正常系なので、ここだけは fail-open のままにする。
    """
    monkeypatch.setattr(
        pre_bash_commit_quality.subprocess,
        "run",
        _fake_git(
            {
                ("git", "diff", "HEAD"): subprocess.CompletedProcess([], 128, stdout="", stderr="fatal"),
                ("git", "rev-parse", "--verify"): subprocess.CompletedProcess([], 1, stdout="", stderr=""),
            }
        ),
    )
    assert pre_bash_commit_quality.get_unstaged_modified_files() == []


def test_get_unstaged_modified_files_returns_none_when_git_fails_with_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HEAD はあるのに diff が失敗した場合は None を返すこと（fail-closed）。

    「対象ファイルなし」と「検査できなかった」を区別しないと、実際にコミット
    される未ステージ変更を 1 件も検査せずに通してしまう（ADR-0001）。
    """
    monkeypatch.setattr(
        pre_bash_commit_quality.subprocess,
        "run",
        _fake_git(
            {
                ("git", "diff", "HEAD"): subprocess.CompletedProcess([], 128, stdout="", stderr="fatal"),
                ("git", "rev-parse", "--verify"): subprocess.CompletedProcess([], 0, stdout="abc123\n", stderr=""),
            }
        ),
    )
    assert pre_bash_commit_quality.get_unstaged_modified_files() is None


def test_get_unstaged_modified_files_returns_none_when_head_check_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HEAD の有無自体を判定できない場合も None を返すこと。"""
    monkeypatch.setattr(
        pre_bash_commit_quality.subprocess,
        "run",
        _fake_git(
            {
                ("git", "diff", "HEAD"): subprocess.CompletedProcess([], 128, stdout="", stderr="fatal"),
                ("git", "rev-parse", "--verify"): subprocess.CompletedProcess([], 128, stdout="", stderr="fatal"),
            }
        ),
    )
    assert pre_bash_commit_quality.get_unstaged_modified_files() is None


def test_get_unstaged_modified_files_returns_none_on_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    """subprocess が OSError 系例外を投げた場合は None を返すこと（fail-closed）。"""
    monkeypatch.setattr(
        pre_bash_commit_quality.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("missing")),
    )
    assert pre_bash_commit_quality.get_unstaged_modified_files() is None


def test_get_worktree_file_content_returns_none_on_oserror(tmp_path: Path) -> None:
    """作業ツリーにファイルが無い等で読み取れない場合は None を返すこと。"""
    assert commit_quality_scanner.get_worktree_file_content(tmp_path, "missing.py") is None


def test_get_worktree_file_content_reads_real_file(tmp_path: Path) -> None:
    """作業ツリー上の実ファイルを読み取れること。"""
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    content = commit_quality_scanner.get_worktree_file_content(tmp_path, "app.py")
    assert content == "value = 1\n"


def test_get_worktree_file_content_rejects_symlink_escaping_repo_root(tmp_path: Path) -> None:
    """repo 外の実体を指すシンボリックリンクは None を返すこと（realpath 包含チェック）。

    OS レベルでシンボリックリンクを追跡すると、repo 内の悪意あるリンクが
    `git commit -a` の対象に入った場合に repo 外の実体（秘密鍵等）を読み、
    secret パターン一致でログに一部露出しうる欠陥の回帰防止。
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside_secret = tmp_path / "outside_secret.txt"
    outside_secret.write_text("api" + "_key" + ' = "hunter2secret"', encoding="utf-8")

    link = repo_root / "evil_link.py"
    link.symlink_to(outside_secret)

    assert commit_quality_scanner.get_worktree_file_content(repo_root, "evil_link.py") is None


def test_get_worktree_file_content_rejects_absolute_file_path(tmp_path: Path) -> None:
    """file_path が絶対パスの場合は None を返すこと（pathlib の仕様上 repo_root が無視される経路）。"""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside_secret = tmp_path / "outside_secret.txt"
    outside_secret.write_text("secret content", encoding="utf-8")

    assert commit_quality_scanner.get_worktree_file_content(repo_root, str(outside_secret)) is None


def test_get_worktree_file_content_rejects_dotdot_traversal(tmp_path: Path) -> None:
    """`..` を含む file_path で repo_root 外へ脱出しようとした場合は None を返すこと。"""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside_secret = tmp_path / "outside_secret.txt"
    outside_secret.write_text("secret content", encoding="utf-8")

    assert commit_quality_scanner.get_worktree_file_content(repo_root, "../outside_secret.txt") is None


def test_get_worktree_file_content_rejects_symlinked_intermediate_directory(tmp_path: Path) -> None:
    """中間ディレクトリがシンボリックリンクで repo 外へ脱出する場合も None を返すこと。"""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "app.py").write_text("secret content", encoding="utf-8")

    linked_subdir = repo_root / "linked_subdir"
    linked_subdir.symlink_to(outside_dir)

    assert commit_quality_scanner.get_worktree_file_content(repo_root, "linked_subdir/app.py") is None


def test_find_git_commit_args_stops_at_separator_before_commit() -> None:
    """git の直後にシェル区切りが現れた場合、その git 呼び出しは commit と
    みなさないこと（区切り記号でセグメントが分かれ、`commit` が別セグメントに
    孤立するため見つからない分岐）。
    """
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    is_commit, args = pbcq._is_git_commit_command("git && echo hi")
    assert is_commit is False
    assert args == []


def test_git_commit_args_stop_at_shell_operator() -> None:
    """`git commit` 後の引数収集は `&&` / `;` / `|` 等のセグメント境界で打ち切る。"""
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    is_commit, args = pbcq._is_git_commit_command("git commit -m x && echo hi")
    assert is_commit is True
    assert args == ["-m", "x"]

    is_commit, args = pbcq._is_git_commit_command("git commit -m x ; true")
    assert is_commit is True
    assert args == ["-m", "x"]

    is_commit, args = pbcq._is_git_commit_command("&& echo hi")
    assert is_commit is False
    assert args == []


def test_find_file_issues_minified_js_lints_but_skips_secret_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """圧縮生成物（*.min.js）は lint 対象（拡張子は .js）だが secret スキャンは
    対象外であること（do_lint=True かつ do_secrets=False の組み合わせ）。"""
    content = 'console.log("hi")\n' + "api" + "_key" + ' = "abc123"'  # nosec
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    issues = commit_quality_scanner.find_file_issues("dist/app.min.js")

    types = {issue["type"] for issue in issues}
    assert "console.log" in types  # nosec
    assert "secret" not in types


_SELF_SCAN_KNOWN_SYNTHETIC_SECRET_HITS = frozenset(
    {
        # 監査再現手順として合成 secret を意図的に引用した凍結済み報告書（書き換えない方針）。
        # ファイル単位ではなく path:line で許可し、同ファイル内の別行に将来 secret が
        # 混入した場合は検出できるようにする。
        "docs/reports/PLUGIN_RUNTIME_AUDIT_2026-08-18_VERIFICATION.md:190",
    }
)


def test_repo_wide_self_scan_has_zero_secret_issues() -> None:
    """自リポジトリの全追跡ファイルを新ロジック（secret は原則全ファイル対象）で走査しても
    既知の合成 secret（凍結済み監査報告書の再現手順）以外の secret 検出が0件であること。

    should_scan_secrets が lock ファイル・圧縮生成物以外の原則すべてのファイルを
    対象にするため、リポジトリ全体に secret パターンへ自己マッチする行が
    存在しないことを保証する回帰テスト（既存の自己走査テストの全リポジトリ版）。
    """
    repo_root_result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(Path(__file__).parent),
        capture_output=True,
        text=True,
        check=True,
    )
    repo_root = Path(repo_root_result.stdout.strip())

    tracked_result = subprocess.run(
        ["git", "ls-files"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    tracked_files = [f for f in tracked_result.stdout.splitlines() if f]

    secret_hits: list[str] = []
    for rel_path in tracked_files:
        if not commit_quality_scanner.should_scan_secrets(rel_path):
            continue

        abs_path = repo_root / rel_path
        try:
            raw = abs_path.read_bytes()
        except OSError:
            continue

        # バイナリファイルは対象外（find_file_issues 内部の binary 判定と同じ基準）
        if b"\0" in raw[:8192]:
            continue

        content = raw.decode("utf-8", errors="replace")
        with mock.patch.object(commit_quality_scanner, "get_staged_file_content", return_value=content):
            issues = commit_quality_scanner.find_file_issues(rel_path)

        secret_hits.extend(f"{rel_path}:{issue['line']}" for issue in issues if issue["type"] == "secret")

    assert set(secret_hits) - _SELF_SCAN_KNOWN_SYNTHETIC_SECRET_HITS == set()
