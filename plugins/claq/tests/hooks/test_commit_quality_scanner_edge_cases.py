"""claq.hooks.commit_quality_scanner の端の挙動のテスト（find_file_issues の対象判定・nosec・サイズ・バイナリ・例外、staged／worktree の内容取得、自己走査）。

このファイル自身を走査して issue 0 件を確かめるテストがあるため、ログ出力・デバッガ文・TODO の
パターンを含む行には `# nosec` を付け、鍵の形の文字列は実行時に連結して組み立てる。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from claq.hooks import commit_quality_scanner as commit_quality_scanner
from claq.hooks import pre_bash_commit_quality as pre_bash_commit_quality

from ._support import _scan_deadline

_API_KEY_LINE = "api" + "_key" + ' = "abc123"'
_SECRET_LINE = "api" + "_key" + ' = "hunter2secret"\n'
_OVER_1MIB = "x" * (1024 * 1024 + 1)


def _issues(file_path: str, content: str, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """INDEX の内容を ``content`` に差し替えて ``find_file_issues`` を満額の予算で呼ぶ。"""
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)
    return commit_quality_scanner.find_file_issues(file_path, deadline=_scan_deadline())


@pytest.mark.parametrize(
    ("file_path", "content", "expected"),
    [
        (
            "src/app.js",
            "\n".join(
                [
                    'console.log("hi")',  # nosec
                    "// console.log('commented')",  # nosec
                    "debugger",  # nosec
                    "// TODO: clean this up",  # nosec
                    "// TODO: #123 tracked",
                    "const " + "api" + "_key" + ' = "abc";',
                ]
            ),
            [
                ("console.log", 1, "warning"),  # nosec
                ("debugger", 3, "error"),  # nosec
                ("todo", 4, "info"),
                ("secret", 6, "error"),
            ],
        ),
        # `# nosec` は lint だけを抑制し、secret 検出は抑制しない（バイパス防止）。
        (
            "src/app.py",
            "debugger  # nosec\n" + "const " + "api" + "_key" + ' = "x";  # nosec',
            [("secret", 2, "error")],
        ),
        ("src/app.py", "api" + "_key" + ' = "hunter2secret"  # nosec', [("secret", 1, "error")]),
        ("src/app.py", 'console.log("debug")  # nosec', []),
        # `sk-ant-...`（`sk-` 直後にハイフン）は OpenAI 形のパターンに一致しないため専用パターンで検出する。
        ("src/app.py", "sk-" + "ant-" + "abcdefghijklmnopqrstuvwxyz0123456789", [("secret", 1, "error")]),
        # lint 対象外の拡張子でも secret は検出し、lint だけを行わない。
        (".env", "API_" + "KEY" + '="hunter2secret"', [("secret", 1, "error")]),
        (".env", 'console.log("hi")', []),  # nosec
        # ロックファイルは secret パターンに一致しても検出しない。
        ("package-lock.json", _API_KEY_LINE, []),
        # 圧縮生成物（*.min.js）は lint 対象（拡張子は .js）だが secret スキャンは対象外。
        ("dist/app.min.js", 'console.log("hi")\n' + _API_KEY_LINE, [("console.log", 1, "warning")]),  # nosec
        # 真のバイナリ（画像等）は secret パターンに当たらない（バイナリ commit を一律ブロックしない）。
        ("logo.png", "\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x01\x00\x00\x00\x01\x00\x08\x06", []),
        # サイズで打ち切らないので、1MiB の前後どちらの secret も検出し、lint も継続する。
        (
            "src/app.js",
            _OVER_1MIB + "\n" + 'console.log("hi")' + "\n" + _API_KEY_LINE,  # nosec
            [("console.log", 2, "warning"), ("secret", 3, "error")],  # nosec
        ),
        ("src/app.js", _API_KEY_LINE + "\n" + _OVER_1MIB, [("secret", 1, "error")]),
        ("src/app.js", "x" * (1024 * 1024 - 100) + "\n" + _API_KEY_LINE, [("secret", 2, "error")]),
    ],
    ids=[
        "lint-mix",
        "nosec-hides-lint-not-secret",
        "nosec-does-not-hide-secret",
        "nosec-hides-console-log",
        "anthropic-key",
        "non-lint-extension-secret",
        "non-lint-extension-skips-lint",
        "lock-file",
        "minified-js",
        "real-binary",
        "secret-past-1mib",
        "secret-before-1mib-padding",
        "secret-under-1mib",
    ],
)
def test_find_file_issues_by_path_and_content(
    monkeypatch: pytest.MonkeyPatch, file_path: str, content: str, expected: list[tuple[str, int, str]]
) -> None:
    """パスと内容ごとに、lint と secret の issue が (種別, 行, severity) の完全一致で出ること。"""
    issues = _issues(file_path, content, monkeypatch)

    assert [(issue["type"], issue["line"], issue["severity"]) for issue in issues] == expected


def test_find_file_issues_binary_file_skips_lint_but_still_scans_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """バイナリ判定（先頭に NUL を含む）は lint だけを抑制し、secret scan は続ける。

    docs/adr/shell-analysis-boundary.md: 先頭に NUL を 1 バイト混ぜてバイナリ判定させるだけで secret 検査を
    まるごと回避できる状態は許容しない。行分割が意味を持たないため、印字可能
    文字列を抽出して同じパターンを当てる。スキップの痕跡 issue も出さない。
    """
    content = "\0binary preamble\n" + 'console.log("hi")\n' + _API_KEY_LINE  # nosec

    issues = _issues("weird.js", content, monkeypatch)

    assert [(issue["type"], issue["line"], issue["severity"]) for issue in issues] == [("secret", 0, "error")]
    assert "extracted binary content" in issues[0]["message"]


@pytest.mark.parametrize(
    ("target", "error", "content", "expected"),
    [
        # secret scanner の例外を握り潰すと、内部バグで commit を許可する最も危険な fail-open になる。
        # lint 側の結果は影響を受けず生存する。
        (
            "_scan_secret_issues",
            RuntimeError("boom"),
            'console.log("hi")',  # nosec
            [("console.log", "warning"), ("scan_error", "error")],  # nosec
        ),
        # lint は false negative のコストが secret ほど高くないため warning に留め、secret 側は生存する。
        ("_scan_lint_issues", ValueError("boom"), _API_KEY_LINE, [("scan_error", "warning"), ("secret", "error")]),
        # 走査対象判定自体が例外を投げても検査不能を報告する。
        ("_is_binary_content", OSError("boom"), "content", [("scan_error", "error")]),
    ],
    ids=["secret-scan", "lint-scan", "scan-target-detection"],
)
def test_find_file_issues_reports_stage_exception_as_scan_error(
    monkeypatch: pytest.MonkeyPatch, target: str, error: Exception, content: str, expected: list[tuple[str, str]]
) -> None:
    """各段階の例外は issue を消さず、段階ごとの severity の scan_error（パスと例外型名入り）として報告されること。"""
    monkeypatch.setattr(commit_quality_scanner, target, mock.Mock(side_effect=error))

    issues = _issues("src/app.js", content, monkeypatch)

    assert [(issue["type"], issue["severity"]) for issue in issues] == expected
    scan_error = next(issue for issue in issues if issue["type"] == "scan_error")
    assert "src/app.js" in scan_error["message"]
    assert type(error).__name__ in scan_error["message"]


def test_find_file_issues_self_check_has_zero_secret_issues(monkeypatch: pytest.MonkeyPatch) -> None:
    """このフック自身のソースを検査しても secret 検出が0件であること（自己検出回避の nosec が secret を隠していないことの担保）。"""
    source_path = Path(commit_quality_scanner.__file__)

    issues = _issues(str(source_path), source_path.read_text(encoding="utf-8"), monkeypatch)

    assert [issue for issue in issues if issue["type"] == "secret"] == []


def test_find_file_issues_self_check_on_this_test_file_has_zero_issues(monkeypatch: pytest.MonkeyPatch) -> None:
    """このテストファイル自身を検査しても issue が0件であること（fixture 文字列の自己マッチ回帰防止）。

    cbabdad で secret 検出が nosec 無視・常時実行になった結果、本ファイルの
    シークレットパターン用 fixture 文字列がソース行として secret パターンに
    自己マッチし、本ファイルをコミットすると exitCode=2 でブロックされる
    回帰が発生した。本テストはその回帰を検知する。
    """
    this_file = Path(__file__)

    assert _issues(str(this_file), this_file.read_text(encoding="utf-8"), monkeypatch) == []


def test_extract_printable_runs_splits_on_control_characters() -> None:
    """制御文字で区切られた短すぎる断片は走査単位に含めないこと。"""
    assert commit_quality_scanner._extract_printable_runs("ab\x00longer-run\x01cd") == ["longer-run"]


def test_should_lint_file_by_extension() -> None:
    """lint 対象の拡張子だけを lint すること。"""
    assert commit_quality_scanner.should_lint_file("src/app.py")
    assert not commit_quality_scanner.should_lint_file("src/app.txt")


@pytest.mark.parametrize(
    ("file_path", "expected"),
    [
        # ロックファイルと圧縮生成物（*.min.js/*.min.css）は対象外。
        ("package-lock.json", False),
        ("yarn.lock", False),
        ("pnpm-lock.yaml", False),
        ("Cargo.lock", False),
        ("poetry.lock", False),
        ("uv.lock", False),
        ("Pipfile.lock", False),
        ("vendor/package-lock.json", False),
        ("dist/bundle.min.js", False),
        ("static/app.min.css", False),
        # lint 対象外の拡張子でも対象（tests/ 配下も除外しない）。
        (".env", True),
        ("config.json", True),
        ("app.yaml", True),
        ("deploy.sh", True),
        ("Dockerfile", True),
        ("src/app.txt", True),
        ("tests/fixtures/secret.env", True),
    ],
)
def test_should_scan_secrets(file_path: str, expected: bool) -> None:
    """secret スキャン対象はロックファイルと圧縮生成物を除く全ファイルであること。"""
    assert pre_bash_commit_quality.should_scan_secrets(file_path) is expected


@pytest.mark.parametrize(
    ("returncode", "stdout", "expected"),
    [
        # git 自体が非0終了した場合は「0 件」ではなく None（検査不能）を返す。
        (1, b"", None),
        (0, b"file content", "file content"),
        # バイナリ判定は find_file_issues 側で lint 抑制にだけ使う。取得層で None にすると
        # NUL 1 個で secret 検査を回避できる抜け道になる。
        (0, b"PNG\x00\x01\x02fake image bytes", "PNG\x00\x01\x02fake image bytes"),
        # 不正な UTF-8 でも UnicodeDecodeError を送出せず置換する（旧 text=True 実装の潜在バグ）。
        (0, b"valid text \xff\xfe invalid bytes", "valid text �� invalid bytes"),
    ],
    ids=["git-fails", "text", "binary-not-filtered", "invalid-utf8-replaced"],
)
def test_get_staged_file_content(
    monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: bytes, expected: str | None
) -> None:
    """`git show` の bytes 出力を復号して返し、git の失敗は None になること。"""

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        """`git show` だけを受け付け、指定の終了コードと bytes を返す。"""
        assert command[:2] == ["git", "show"]
        return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=b"")

    monkeypatch.setattr(commit_quality_scanner.subprocess, "run", fake_run)

    assert commit_quality_scanner.get_staged_file_content("src/app.js") == expected


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    """実ファイル・各種 symlink を置いた repo を返す。repo の隣に secret 入りのファイルとディレクトリを置く。"""
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    (tmp_path / "outside_dir").mkdir()
    for path in (tmp_path / "outside_secret.txt", tmp_path / "outside_dir" / "app.py", repo / "real.txt"):
        path.write_text(_SECRET_LINE, encoding="utf-8")
    (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
    links = {
        "linked_subdir": tmp_path / "outside_dir",
        "link_file.py": "real.txt",
        "link_dir.py": "sub",
        "link_broken.py": "nowhere",
        "link_outside.py": tmp_path / "outside_secret.txt",
    }
    for name, target in links.items():
        (repo / name).symlink_to(target)
    return repo


@pytest.mark.parametrize(
    ("file_path", "expected"),
    [
        ("missing.py", None),
        ("app.py", "value = 1\n"),
        # repo 外への脱出: 絶対パス（pathlib の仕様上 repo_root が無視される）・`..`・symlink の中間ディレクトリ。
        ("{outside}", None),
        ("../outside_secret.txt", None),
        ("linked_subdir/app.py", None),
        # symlink は先を辿らず link 先文字列（git が blob に保存するもの）を返す。repo 外を指していても
        # repo 外の実体（秘密鍵等）を読まない。
        ("link_file.py", "real.txt"),
        ("link_dir.py", "sub"),
        ("link_broken.py", "nowhere"),
        ("link_outside.py", "{outside}"),
    ],
    ids=[
        "missing",
        "regular-file",
        "absolute-path",
        "dotdot-traversal",
        "symlinked-intermediate-dir",
        "symlink-to-file",
        "symlink-to-dir",
        "broken-symlink",
        "symlink-outside-repo",
    ],
)
def test_get_worktree_file_content(worktree: Path, file_path: str, expected: str | None) -> None:
    """作業ツリーの実ファイルは内容、symlink は link 先文字列を返し、読めない・repo 外は None になること。"""
    outside = str(worktree.parent / "outside_secret.txt")

    content = commit_quality_scanner.get_worktree_file_content(worktree, file_path.format(outside=outside))

    assert content == (None if expected is None else expected.format(outside=outside))
