"""claq.hooks.commit_quality_scanner の端の挙動のテスト（find_file_issues の対象判定・nosec・サイズ・バイナリ・例外、staged／worktree の内容取得、自己走査）。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from claq.hooks import commit_message as commit_message
from claq.hooks import commit_quality_scanner as commit_quality_scanner
from claq.hooks import pre_bash_commit_quality as pre_bash_commit_quality

from ._support import _scan_deadline


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

    issues = commit_quality_scanner.find_file_issues("src/app.js", deadline=_scan_deadline())

    assert {issue["type"] for issue in issues} == {"console.log", "debugger", "todo", "secret"}  # nosec
    assert [issue["line"] for issue in issues if issue["type"] == "console.log"] == [1]  # nosec
    assert [issue["line"] for issue in issues if issue["type"] == "todo"] == [4]

    assert commit_message.validate_commit_message("git status") is None
    message = commit_message.validate_commit_message('git commit -m "feat(core): Add feature."')
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

    issues = commit_quality_scanner.find_file_issues("src/app.py", deadline=_scan_deadline())

    # デバッガ文は nosec で抑制される
    assert not any(issue["type"] == "debugger" for issue in issues)  # nosec
    # secret は nosec があっても検出される（バイパス防止）
    assert [issue["type"] for issue in issues] == ["secret"]
    assert issues[0]["line"] == 2


def test_find_file_issues_secret_detection_not_bypassed_by_nosec(monkeypatch: pytest.MonkeyPatch) -> None:
    """`# nosec` を付与しても api_key のようなシークレットパターンはブロック対象として検出され続ける。"""
    content = "api" + "_key" + ' = "hunter2secret"  # nosec'
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    issues = commit_quality_scanner.find_file_issues("src/app.py", deadline=_scan_deadline())

    assert len(issues) == 1
    assert issues[0]["type"] == "secret"
    assert issues[0]["severity"] == "error"


def test_find_file_issues_detects_anthropic_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """`sk-ant-...` 形式（ハイフン区切り）の Anthropic API キーも検出される。

    OpenAI 形式用パターン `sk-[a-zA-Z0-9]{20,}` は `sk-` 直後にハイフンを含む
    Anthropic 形式（`sk-ant-api03-...`）とは一致しないため、専用パターンで
    検出する回帰。
    """
    content = "sk-" + "ant-" + "abcdefghijklmnopqrstuvwxyz0123456789"
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    issues = commit_quality_scanner.find_file_issues("src/app.py", deadline=_scan_deadline())

    assert len(issues) == 1
    assert issues[0]["type"] == "secret"
    assert issues[0]["severity"] == "error"


def test_find_file_issues_console_log_still_suppressed_by_nosec(monkeypatch: pytest.MonkeyPatch) -> None:
    """secret を含まない行では console.log が nosec で抑制されること。"""  # nosec
    content = 'console.log("debug")  # nosec'
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    assert commit_quality_scanner.find_file_issues("src/app.py", deadline=_scan_deadline()) == []


def test_find_file_issues_self_check_has_zero_secret_issues(monkeypatch: pytest.MonkeyPatch) -> None:
    """このフック自身のソースを検査しても secret 検出が0件であること（自己検出回避の nosec が secret を隠していないことの担保）。"""
    source_path = Path(commit_quality_scanner.__file__)
    own_source = source_path.read_text(encoding="utf-8")

    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: own_source)
    issues = commit_quality_scanner.find_file_issues(str(source_path), deadline=_scan_deadline())

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
        issues = commit_quality_scanner.find_file_issues(str(this_file), deadline=_scan_deadline())

    assert issues == []


def test_find_file_issues_detects_secret_in_non_lint_extension(monkeypatch: pytest.MonkeyPatch) -> None:
    """.py/.js 等の lint 対象外拡張子（例: .env）でも secret 検出が行われること。"""
    content = "API_" + "KEY" + '="hunter2secret"'
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    issues = commit_quality_scanner.find_file_issues(".env", deadline=_scan_deadline())

    assert [issue["type"] for issue in issues] == ["secret"]


def test_find_file_issues_lint_only_checks_skipped_for_non_lint_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lint 対象外拡張子では console.log/デバッガ文/todo は検出されないこと（secret のみ対象）。"""  # nosec
    content = 'console.log("hi")'  # nosec
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    assert commit_quality_scanner.find_file_issues(".env", deadline=_scan_deadline()) == []


def test_find_file_issues_skips_secret_scan_for_lock_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """ロックファイルは内容が secret パターンに一致しても検出しないこと。"""
    content = "api" + "_key" + ' = "abc123"'
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    assert commit_quality_scanner.find_file_issues("package-lock.json", deadline=_scan_deadline()) == []


def test_find_file_issues_oversized_files_detect_secret_past_old_1mib_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1MiB を超える位置に置かれた secret も検出されること（サイズ
    による打ち切りをしないため、ファイル後方の secret も見逃さない）。
    lint（ログ出力チェック）はサイズに関わらず継続することも合わせて確認する。
    """  # nosec
    padding = "x" * (1024 * 1024 + 1)
    content = padding + "\n" + 'console.log("hi")' + "\n" + "api" + "_key" + ' = "abc123"'  # nosec
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    issues = commit_quality_scanner.find_file_issues("src/app.js", deadline=_scan_deadline())

    types = {issue["type"] for issue in issues}
    # サイズで打ち切らないので、1MiB より後ろの secret も検出される。
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

    issues = commit_quality_scanner.find_file_issues("src/app.js", deadline=_scan_deadline())

    assert any(issue["type"] == "secret" and issue["line"] == 1 for issue in issues)


def test_find_file_issues_secret_scan_applies_under_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """1MB 以下のファイルは通常どおり secret スキャンされること（境界値確認）。"""
    padding = "x" * (1024 * 1024 - 100)
    content = padding + "\n" + "api" + "_key" + ' = "abc123"'
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    issues = commit_quality_scanner.find_file_issues("src/app.js", deadline=_scan_deadline())

    assert any(issue["type"] == "secret" for issue in issues)


def test_find_file_issues_binary_file_skips_lint_but_still_scans_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """バイナリ判定（先頭に NUL を含む）は lint だけを抑制し、secret scan は続ける。

    docs/adr/shell-analysis-boundary.md: 先頭に NUL を 1 バイト混ぜてバイナリ判定させるだけで secret 検査を
    まるごと回避できる状態は許容しない。行分割が意味を持たないため、印字可能
    文字列を抽出して同じパターンを当てる。
    """
    content = "\0binary preamble\n" + 'console.log("hi")\n' + "api" + "_key" + ' = "abc123"'  # nosec
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    issues = commit_quality_scanner.find_file_issues("weird.js", deadline=_scan_deadline())

    secrets = [issue for issue in issues if issue["type"] == "secret"]
    assert secrets, issues
    assert secrets[0]["severity"] == "error"
    assert "extracted binary content" in secrets[0]["message"]
    # lint は抑制する（バイナリを行単位で lint しても意味がない）。
    assert "console.log" not in {issue["type"] for issue in issues}  # nosec
    # スキップの痕跡 issue は不要になったので出さない。
    assert "secret_scan_skipped" not in {issue["type"] for issue in issues}


def test_find_file_issues_real_binary_is_not_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """真のバイナリ（画像等）は secret パターンに当たらず error を出さないこと。

    バイナリ commit を一律ブロックしないという既存の要件を維持する。
    """
    png = "\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x01\x00\x00\x00\x01\x00\x08\x06"
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: png)

    issues = commit_quality_scanner.find_file_issues("logo.png", deadline=_scan_deadline())

    assert [issue for issue in issues if issue["severity"] == "error"] == []


def test_extract_printable_runs_splits_on_control_characters() -> None:
    """制御文字で区切られた短すぎる断片は走査単位に含めないこと。"""
    runs = commit_quality_scanner._extract_printable_runs("ab\x00longer-run\x01cd")

    assert runs == ["longer-run"]


def test_find_file_issues_secret_scan_exception_is_reported_as_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """secret scanner が例外を投げても issue が消えず、error として報告される。

    find_file_issues 全体を `except Exception: pass` で覆うと例外を
    握り潰し、secret scanner の内部バグでも commit を許可してしまう
    （false negative のコストが高い secret 検出が最も危険な形で fail-open
    する）。lint 側の結果は例外の影響を受けず生存することも確認する。
    """
    content = 'console.log("hi")'  # nosec
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)
    monkeypatch.setattr(
        commit_quality_scanner,
        "_scan_secret_issues",
        mock.Mock(side_effect=RuntimeError("boom")),
    )

    issues = commit_quality_scanner.find_file_issues("src/app.js", deadline=_scan_deadline())

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

    issues = commit_quality_scanner.find_file_issues("src/app.js", deadline=_scan_deadline())

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

    issues = commit_quality_scanner.find_file_issues("src/app.js", deadline=_scan_deadline())

    assert len(issues) == 1
    assert issues[0]["type"] == "scan_error"
    assert issues[0]["severity"] == "error"
    assert "OSError" in issues[0]["message"]


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
    def fake_run(command: list[str], **kwargs: object):
        # `get_staged_files` は `subprocess_utils.run_text` 経由になったため
        # `encoding` / `errors` / `env` / `input` も渡ってくる（**kwargs で受ける）。
        if command[:2] == ["git", "diff"]:
            assert kwargs["encoding"] == "utf-8"
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


def test_get_worktree_file_content_returns_none_on_oserror(tmp_path: Path) -> None:
    """作業ツリーにファイルが無い等で読み取れない場合は None を返すこと。"""
    assert commit_quality_scanner.get_worktree_file_content(tmp_path, "missing.py") is None


def test_get_worktree_file_content_reads_real_file(tmp_path: Path) -> None:
    """作業ツリー上の実ファイルを読み取れること。"""
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    content = commit_quality_scanner.get_worktree_file_content(tmp_path, "app.py")
    assert content == "value = 1\n"


def test_get_worktree_file_content_does_not_read_symlink_target_outside_repo_root(tmp_path: Path) -> None:
    """repo 外の実体を指すシンボリックリンクは先を辿らず link 先文字列だけを返すこと。

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

    assert commit_quality_scanner.get_worktree_file_content(repo_root, "evil_link.py") == str(outside_secret)


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


def test_find_file_issues_minified_js_lints_but_skips_secret_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """圧縮生成物（*.min.js）は lint 対象（拡張子は .js）だが secret スキャンは
    対象外であること（do_lint=True かつ do_secrets=False の組み合わせ）。"""
    content = 'console.log("hi")\n' + "api" + "_key" + ' = "abc123"'  # nosec
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    issues = commit_quality_scanner.find_file_issues("dist/app.min.js", deadline=_scan_deadline())

    types = {issue["type"] for issue in issues}
    assert "console.log" in types  # nosec
    assert "secret" not in types
