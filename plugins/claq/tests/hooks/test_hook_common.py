"""hook_common のフック出力ヘルパー（SessionStart）・リポジトリルート解決・保護対象判定の共有正規化のテスト。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from claq.hooks import block_no_verify, hook_common
from claq.hooks.hook_common import emit_session_start_output, print_session_start_output


@pytest.mark.parametrize(
    "context",
    [None, "", "simple context", "改行\n含む\nテキスト", "unicode: 日本語テスト 🐍", "a" * 5000],
    ids=["default", "empty", "simple", "newlines", "unicode", "long"],
)
def test_session_start_output(context: str | None, capsys: pytest.CaptureFixture[str]) -> None:
    """emit は末尾改行なしの合併 JSON 文字列を返し、print は同じ JSON を 1 行で stdout へ書くこと（None は引数省略）。"""
    args = () if context is None else (context,)
    result = emit_session_start_output(*args)
    print_session_start_output(*args)

    expected = context or ""
    assert isinstance(result, str)
    assert not result.endswith("\n")
    assert json.loads(result) == {
        "additionalContext": expected,
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": expected},
    }
    assert capsys.readouterr().out == result + "\n"


@pytest.mark.parametrize("value", [None, "", "0", " no "])
def test_is_truthy_rejects_falsey_values(value: str | None) -> None:
    """None・空文字・"0"・前後に空白のある " no " を真値とみなさないこと。"""
    assert hook_common.is_truthy(value) is False


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (subprocess.CompletedProcess([], 128, stdout="", stderr="fatal"), None),
        (subprocess.CompletedProcess([], 0, stdout="", stderr=""), None),
        # タイムアウト・git 実行ファイル自体が無い場合も非ブロッキングで None。
        (subprocess.TimeoutExpired(cmd="git", timeout=5), None),
        (FileNotFoundError("no git"), None),
        (subprocess.CompletedProcess([], 0, stdout="/repo/root\n", stderr=""), Path("/repo/root")),
    ],
    ids=["git-fails", "empty-output", "timeout", "git-missing", "success"],
)
def test_resolve_repo_root(monkeypatch: pytest.MonkeyPatch, outcome: object, expected: Path | None) -> None:
    """git rev-parse の結果ごとの解決値を返し、同一プロセス内では git を 1 回しか起動しないこと（lru_cache）。"""
    fake_run = mock.Mock(side_effect=[outcome])
    monkeypatch.setattr(hook_common.subprocess, "run", fake_run)
    hook_common.resolve_repo_root.cache_clear()
    try:
        assert [hook_common.resolve_repo_root(), hook_common.resolve_repo_root()] == [expected, expected]
    finally:
        hook_common.resolve_repo_root.cache_clear()
    assert fake_run.call_count == 1


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("git", True),
        ("/usr/bin/git", True),
        ("./git", True),
        # 実行ファイル表記ゆれ: 大小文字・.exe・Windows パス区切り。
        ("GIT", True),
        ("Git", True),
        ("git.exe", True),
        ("GIT.EXE", True),
        (r"C:\Program Files\Git\bin\git.exe", True),
        ("gitk", False),
        ("GIT_DIR=.git", False),
        (".git", False),
        ("git/", False),
        ("git commit --no-verify", False),
    ],
)
def test_git_executable_token(token: str, expected: bool) -> None:
    """basename が git のトークンだけを git 起動とみなし、block_no_verify の判定も同じ共有正規化に揃うこと。"""
    assert hook_common.is_git_executable_token(token) is expected
    assert block_no_verify.is_git_invocation(token) is expected


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("cp", "cp"),
        ("CP", "cp"),
        ("Rm", "rm"),
        ("TEE", "tee"),
        ("cp.exe", "cp"),
        ("RM.EXE", "rm"),
        ("/usr/bin/rm", "rm"),
        # クォートで守られた Windows 絶対パスは POSIX トークン化を `\` 付きで生き延びるため、`\` も区切りにする。
        (r"C:\bin\rm.exe", "rm"),
        ("Remove-Item", "remove-item"),
        ("", ""),
    ],
)
def test_normalize_executable_name(token: str, expected: str) -> None:
    """実行トークンを basename・大小無視・`.exe` 除去へ畳むこと（兄弟フックの実行名照合が同じ規則からずれない）。"""
    assert hook_common.normalize_executable_name(token) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("ruff.toml", "ruff.toml"),
        ("Ruff.toml", "ruff.toml"),
        ("RUFF.TOML", "ruff.toml"),
        ("sub/dir/PyProject.toml", "pyproject.toml"),
        (r"sub\dir\Package.json", "package.json"),
        (".GIT", ".git"),
        ("ruff.toml.exe", "ruff.toml.exe"),
    ],
)
def test_normalize_protected_name(path: str, expected: str) -> None:
    """実行名と同じ case 演算と区切りで畳み、`.exe` は落とさないこと（保護対象は設定ファイルで同一視の根拠が無い）。"""
    assert hook_common.normalize_protected_name(path) == expected


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        # 短形式（結合位置を問わない）。
        ("-i", True),
        ("-i.bak", True),
        ("-0pi", True),
        ("-ni", True),
        ("-pi", True),
        # GNU 長形式と、getopt が受け付ける非曖昧な短縮。
        ("--in-place", True),
        ("--in-place=.bak", True),
        ("--in-pl", True),
        ("--i", True),
        # 別の長形式は `in-place` のプレフィックスではない。
        ("--expression", False),
        ("--silent", False),
        ("--include", False),
        # 区切りの `--` は名前部分が空なので in-place ではない。
        ("--", False),
        ("--=x", False),
        # 単独の `-` と非オプション。
        ("-", False),
        ("-n", False),
        ("-E", False),
        ("s/a/b/", False),
        ("ruff.toml", False),
        ("", False),
    ],
)
def test_is_inplace_edit_flag(token: str, expected: bool) -> None:
    """in-place 編集フラグを短形式と長形式で判定すること（兄弟フックで語彙が割れると `sed --in-place … && git commit -am x` が不発）。"""
    assert hook_common.is_inplace_edit_flag(token) is expected


@pytest.mark.parametrize(
    ("raw_path", "expected"),
    [
        ("pyproject.toml", "pyproject.toml"),
        # basename だけの判定では symlink 経由の書き込みがすり抜けるため、実体を辿る。
        ("alias-file", "pyproject.toml"),
        # strict=False なので存在しないパスも例外にせず解決する。
        ("does-not-exist.toml", "does-not-exist.toml"),
        # シェルの `~` 展開に合わせてホーム基準で解決する（`<cwd>/~/...` のリテラルなフォルダ名にしない）。
        ("~/.claq/logs/bg-1.log", ".claq/logs/bg-1.log"),
        # 単独サロゲートは UnicodeEncodeError（ValueError）で解決できない。貫通させると保護フックが exit 1 で落ちる。
        ("\ud800x", None),
    ],
    ids=["relative", "symlink", "nonexistent", "leading-tilde", "lone-surrogate"],
)
def test_resolve_effective_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw_path: str, expected: str | None
) -> None:
    """cwd（tmp_path。HOME も同じ）基準で解決して symlink を辿った実体を返し、解決できない入力は None を返すこと。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAQ_HOME", raising=False)
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    (tmp_path / "alias-file").symlink_to(tmp_path / "pyproject.toml")

    resolved = hook_common.resolve_effective_target(raw_path)

    assert resolved == (None if expected is None else (tmp_path / expected).resolve())


@pytest.mark.parametrize("error", [OSError("elooped"), RuntimeError("symlink loop")], ids=["os-error", "runtime-error"])
def test_resolve_effective_target_returns_none_when_resolve_raises(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    """壊れた・循環した symlink 等で解決できないときは、クラッシュせず None を返すこと。"""
    monkeypatch.setattr(Path, "resolve", mock.Mock(side_effect=error))

    assert hook_common.resolve_effective_target("whatever") is None


@pytest.mark.parametrize(
    ("raw_path", "expected"),
    [
        ("~", "{home}"),
        ("~/.claq/logs/x", "{home}/.claq/logs/x"),
        ("plugins/claq/pyproject.toml", "plugins/claq/pyproject.toml"),
        # `~otheruser` は標準ライブラリに対応 API が無いため展開しない（既定範囲外）。
        ("~otheruser/x", "~otheruser/x"),
    ],
    ids=["bare-tilde", "tilde-slash", "no-tilde", "other-user"],
)
def test_expand_leading_tilde(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw_path: str, expected: str) -> None:
    """先頭の `~` / `~/` だけをホーム（`{home}`）へ展開すること。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAQ_HOME", raising=False)

    assert hook_common._expand_leading_tilde(raw_path) == expected.format(home=tmp_path)
