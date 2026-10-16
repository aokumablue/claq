"""hook_common のフック出力ヘルパー（SessionStart）・保護対象判定ヘルパーのテスト。"""

from __future__ import annotations

import io
import json
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from claq.hooks import hook_common
from claq.hooks.hook_common import (
    emit_session_start_output,
    print_session_start_output,
)


def _parse_session_start(output: str) -> dict:
    """出力が有効な SessionStart hookSpecificOutput JSON かを検証して返す。"""
    payload = json.loads(output)
    assert "hookSpecificOutput" in payload
    inner = payload["hookSpecificOutput"]
    assert inner["hookEventName"] == "SessionStart"
    assert "additionalContext" in inner
    return inner


class TestEmitSessionStartOutput:
    @pytest.mark.parametrize(
        "additional_context",
        [
            "",
            "simple context",
            "改行\n含む\nテキスト",
            "unicode: 日本語テスト 🐍",
            "a" * 5000,
        ],
        ids=["empty", "simple", "newlines", "unicode", "long"],
    )
    def test_returns_valid_json(self, additional_context: str) -> None:
        result = emit_session_start_output(additional_context)
        inner = _parse_session_start(result)
        assert inner["additionalContext"] == additional_context

    def test_default_empty_context(self) -> None:
        result = emit_session_start_output()
        inner = _parse_session_start(result)
        assert inner["additionalContext"] == ""

    def test_is_string(self) -> None:
        assert isinstance(emit_session_start_output(), str)

    def test_no_trailing_newline(self) -> None:
        result = emit_session_start_output()
        assert not result.endswith("\n")


class TestPrintSessionStartOutput:
    def test_prints_to_stdout(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_session_start_output("hello")
        output = buf.getvalue()
        inner = _parse_session_start(output.strip())
        assert inner["additionalContext"] == "hello"

    def test_default_empty_context(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_session_start_output()
        inner = _parse_session_start(buf.getvalue().strip())
        assert inner["additionalContext"] == ""


class TestResolveRepoRoot:
    """`resolve_repo_root`（共有ヘルパー）の解決・プロセス内キャッシュのテスト。

    `resolve_repo_root` は `functools.lru_cache` を持つため、各テストの
    前後で `cache_clear()` してテスト間の漏れ込みを防ぐ。
    """

    @pytest.fixture(autouse=True)
    def _clear_cache(self) -> None:
        hook_common.resolve_repo_root.cache_clear()
        yield
        hook_common.resolve_repo_root.cache_clear()

    def test_returns_none_on_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """git rev-parse が失敗（returncode != 0）した場合は None を返すこと。"""
        monkeypatch.setattr(
            hook_common.subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 128, stdout="", stderr="fatal"),
        )
        assert hook_common.resolve_repo_root() is None

    def test_returns_none_on_empty_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """git rev-parse の出力が空の場合は None を返すこと。"""
        monkeypatch.setattr(
            hook_common.subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="", stderr=""),
        )
        assert hook_common.resolve_repo_root() is None

    def test_returns_none_on_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """git rev-parse がタイムアウトした場合は非ブロッキングで None を返すこと。"""
        monkeypatch.setattr(
            hook_common.subprocess,
            "run",
            lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd="git", timeout=5)),
        )
        assert hook_common.resolve_repo_root() is None

    def test_returns_none_on_file_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """git 実行ファイル自体が無い場合も非ブロッキングで None を返すこと。"""
        monkeypatch.setattr(
            hook_common.subprocess,
            "run",
            lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("no git")),
        )
        assert hook_common.resolve_repo_root() is None

    def test_returns_path_on_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """git rev-parse が成功すればそのパスを返すこと。"""
        monkeypatch.setattr(
            hook_common.subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="/repo/root\n", stderr=""),
        )
        assert hook_common.resolve_repo_root() == Path("/repo/root")

    def test_caches_within_process_calls_git_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """同一プロセス内では git rev-parse を最大 1 回しか実行しない。"""
        call_count = {"n": 0}

        def _fake_run(*args, **kwargs):  # noqa: ANN002, ANN003
            call_count["n"] += 1
            return subprocess.CompletedProcess(args[0], 0, stdout="/repo/root\n", stderr="")

        monkeypatch.setattr(hook_common.subprocess, "run", _fake_run)

        first = hook_common.resolve_repo_root()
        second = hook_common.resolve_repo_root()

        assert first == second == Path("/repo/root")
        assert call_count["n"] == 1


class TestIsGitExecutableToken:
    """is_git_executable_token（共有正規化。block_no_verify /
    pre_bash_commit_quality が共に使う）のテスト。
    """

    @pytest.mark.parametrize(
        ("token", "expected"),
        [
            ("git", True),
            ("GIT", True),
            ("Git", True),
            ("/usr/bin/git", True),
            ("git.exe", True),
            ("GIT.EXE", True),
            (r"C:\Program Files\Git\bin\git.exe", True),
            ("gitk", False),
            (".git", False),
            ("git/", False),
        ],
    )
    def test_normalizes_case_path_and_exe_suffix(self, token: str, expected: bool) -> None:
        assert hook_common.is_git_executable_token(token) is expected


class TestNormalizeExecutableName:
    """normalize_executable_name（共有正規化）のテスト。

    `is_git_executable_token` / `bash_config_protection` の実行名照合 /
    `pre_bash_commit_quality` の mutation 語彙が同じ規則を使うことを固定する。
    片方だけ `.exe` を落とす・片方だけ大小を区別する状態を防ぐ。
    """

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
            # クォートで守られた Windows 絶対パスは POSIX トークン化を `\` 付きで
            # 生き延びるため、`\` も区切りとして扱う必要がある。
            (r"C:\bin\rm.exe", "rm"),
            ("Remove-Item", "remove-item"),
            ("", ""),
        ],
    )
    def test_folds_case_separators_and_exe(self, token: str, expected: str) -> None:
        assert hook_common.normalize_executable_name(token) == expected


class TestNormalizeProtectedName:
    """normalize_protected_name（共有正規化）のテスト。

    実行名側と**同じ case 演算**を使うこと、および `.exe` は落とさないこと
    （保護対象は設定ファイルであり `ruff.toml.exe` を同一視する根拠が無い）を
    固定する。
    """

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("ruff.toml", "ruff.toml"),
            ("Ruff.toml", "ruff.toml"),
            ("RUFF.TOML", "ruff.toml"),
            ("sub/dir/PyProject.toml", "pyproject.toml"),
            (r"sub\dir\Package.json", "package.json"),
            (".GIT", ".git"),
            # 実行名と違い `.exe` は保持する。
            ("ruff.toml.exe", "ruff.toml.exe"),
        ],
    )
    def test_folds_case_and_separators(self, path: str, expected: str) -> None:
        assert hook_common.normalize_protected_name(path) == expected


class TestIsInplaceEditFlag:
    """is_inplace_edit_flag（共有述語）のテスト。

    `bash_config_protection`（保護対象への in-place 書き込み）と
    `pre_bash_commit_quality`（commit 前の作業ツリー変更）が同じ語彙を使うことを
    固定する。どちらかが長形式を除外すると、
    `sed --in-place ... && git commit -am x` でガードが不発になる。
    """

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
    def test_classifies_short_and_long_forms(self, token: str, expected: bool) -> None:
        assert hook_common.is_inplace_edit_flag(token) is expected


class TestResolveEffectiveTarget:
    """resolve_effective_target（共有 helper。config_protection /
    bash_config_protection が symlink 解決に使う）のテスト。
    """

    def test_resolves_relative_path_against_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "pyproject.toml"
        target.write_text("", encoding="utf-8")

        resolved = hook_common.resolve_effective_target("pyproject.toml")

        assert resolved == target.resolve()

    def test_follows_symlink_to_real_target(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        real = tmp_path / "pyproject.toml"
        real.write_text("", encoding="utf-8")
        alias = tmp_path / "alias-file"
        alias.symlink_to(real)

        assert hook_common.resolve_effective_target("alias-file") == real.resolve()

    def test_nonexistent_path_still_resolves(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """strict=False なので存在しないパスでも例外にせず解決した Path を返す。"""
        monkeypatch.chdir(tmp_path)

        resolved = hook_common.resolve_effective_target("does-not-exist.toml")

        assert resolved == tmp_path / "does-not-exist.toml"

    def test_returns_none_on_os_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """壊れた・循環した symlink 等で解決不能な場合は None を返す（クラッシュさせない）。"""

        def _boom(self, strict=False):  # noqa: ANN001, ANN002, ARG001
            raise OSError("elooped")

        monkeypatch.setattr(Path, "resolve", _boom)

        assert hook_common.resolve_effective_target("whatever") is None

    def test_returns_none_on_runtime_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(self, strict=False):  # noqa: ANN001, ANN002, ARG001
            raise RuntimeError("symlink loop")

        monkeypatch.setattr(Path, "resolve", _boom)

        assert hook_common.resolve_effective_target("whatever") is None

    def test_returns_none_on_lone_surrogate(self) -> None:
        """単独サロゲートは ``UnicodeEncodeError``（``ValueError``）で解決不能になり、None を返す。

        例外を貫通させると呼び出し側の保護フックが exit 1（＝ツール実行）で落ちる。
        """
        assert hook_common.resolve_effective_target("\ud800x") is None

    def test_expands_leading_tilde_before_resolving(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """シェルの `~` 展開に合わせ、``~/...`` はホーム基準で解決すること。

        `Path.cwd() / raw_path` はそのままでは ``~`` を展開せず、
        ``<cwd>/~/.claq/logs/x`` のようなカレントディレクトリ配下の
        リテラルなフォルダ名として扱ってしまう。
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CLAQ_HOME", raising=False)
        monkeypatch.chdir(tmp_path)

        resolved = hook_common.resolve_effective_target("~/.claq/logs/bg-1.log")

        assert resolved == (tmp_path / ".claq" / "logs" / "bg-1.log").resolve()


class TestExpandLeadingTilde:
    """`_expand_leading_tilde`（シェルのチルダ展開相当）のテスト。"""

    def test_bare_tilde_expands_to_home_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CLAQ_HOME", raising=False)

        assert hook_common._expand_leading_tilde("~") == str(tmp_path)

    def test_tilde_slash_prefix_expands_to_home_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CLAQ_HOME", raising=False)

        assert hook_common._expand_leading_tilde("~/.claq/logs/x") == str(tmp_path) + "/.claq/logs/x"

    def test_path_without_leading_tilde_is_unchanged(self) -> None:
        assert hook_common._expand_leading_tilde("plugins/claq/pyproject.toml") == "plugins/claq/pyproject.toml"

    def test_other_user_tilde_is_not_expanded(self) -> None:
        """``~otheruser`` 形式は展開しない（標準ライブラリに対応 API が無いための既定範囲外）。"""
        assert hook_common._expand_leading_tilde("~otheruser/x") == "~otheruser/x"
