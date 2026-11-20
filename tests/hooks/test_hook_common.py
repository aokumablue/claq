"""hook_common のフック出力ヘルパー（SessionStart・deny）・リポジトリルート解決・保護対象判定の共有正規化のテスト。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from claq.hooks import hook_common, shell_tokens
from claq.lib.output_adapter import print_session_start_output


@pytest.mark.parametrize(
    "context",
    ["", "simple context", "改行\n含む\nテキスト", "unicode: 日本語テスト 🐍", "a" * 5000],
    ids=["empty", "simple", "newlines", "unicode", "long"],
)
def test_session_start_output(context: str, capsys: pytest.CaptureFixture[str]) -> None:
    """print は additionalContext（トップレベル）と hookSpecificOutput を同時に含む合併 JSON を 1 行で stdout へ書くこと。"""
    print_session_start_output(context)

    out = capsys.readouterr().out
    assert out.endswith("\n")
    assert json.loads(out) == {
        "additionalContext": context,
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context},
    }


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
    """basename が git のトークンだけを git 起動とみなすこと。"""
    assert hook_common.is_git_executable_token(token) is expected


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
    assert shell_tokens.normalize_executable_name(token) == expected


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
        # Win32 の名前正規化（末尾のドット・代替データストリーム）を突く名前は意図して組み立てる回避形なので畳まない。
        ("ruff.toml.", "ruff.toml."),
        ("ruff.toml:x", "ruff.toml:x"),
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
        # GNU 長形式。
        ("--in-place", True),
        ("--in-place=.bak", True),
        # 長形式の短縮は意図して組み立てる回避形なので対象外。
        ("--in-pl", False),
        ("--i", False),
        # 別の長形式。
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


def test_memoized_in_scan_reuses_results_only_inside_scan_memo() -> None:
    """with の中では引数ごとに 1 回だけ計算し、with の外と with を出た後は毎回計算する。"""
    calls: list[tuple[object, ...]] = []

    @hook_common.memoized_in_scan
    def probe(value: str, *, flag: bool = False) -> str:
        """呼ばれた引数を記録して返す。"""
        calls.append((value, flag))
        return f"{value}:{flag}"

    assert probe("a") == "a:False"
    assert probe("a") == "a:False"
    assert len(calls) == 2

    with hook_common.scan_memo():
        assert probe("a") == "a:False"
        assert probe("a") == "a:False"
        assert probe("a", flag=True) == "a:True"
        assert probe("b") == "b:False"
    assert calls[2:] == [("a", False), ("a", True), ("b", False)]

    probe("a")
    assert len(calls) == 6


def test_memoized_in_scan_does_not_reuse_exceptions() -> None:
    """例外は置き場に残さず、次の呼び出しでも計算し直して同じ例外を送る。"""
    calls: list[str] = []

    @hook_common.memoized_in_scan
    def failing(value: str) -> str:
        """呼ばれた引数を記録して例外を送る。"""
        calls.append(value)
        raise OSError(value)

    with hook_common.scan_memo():
        for _ in range(2):
            with pytest.raises(OSError, match="x"):
                failing("x")
    assert calls == ["x", "x"]


def test_scan_memo_discards_results_when_body_raises() -> None:
    """with の本体が例外で抜けても置き場を捨てる（次の判定へ持ち越さない）。"""
    with pytest.raises(RuntimeError), hook_common.scan_memo():
        raise RuntimeError("boom")
    assert hook_common._scan_memo is None


class _FakeOs:
    """`hook_common` が参照する os の差し替え。devnull の open・dup2・close を記録だけする。

    本物の ``os.dup2`` を呼ぶと pytest の fd 捕捉が壊れるため、`hook_common` の参照だけを差し替える。
    """

    devnull = "/dev/null"
    O_WRONLY = 1

    def __init__(self) -> None:
        """呼び出しの記録を空で始める。"""
        self.calls: list[tuple[object, ...]] = []

    def open(self, path: str, flags: int) -> int:
        """devnull を開いたことを記録し、固定の fd を返す。"""
        self.calls.append(("open", path, flags))
        return 99

    def dup2(self, fd: int, fd2: int) -> None:
        """fd の付け替えを記録する。"""
        self.calls.append(("dup2", fd, fd2))

    def close(self, fd: int) -> None:
        """fd を閉じたことを記録する。"""
        self.calls.append(("close", fd))


class _Stream:
    """flush の成否と fileno の有無を選べる出力ストリーム。"""

    def __init__(self, fd: int, *, broken: bool, has_fileno: bool = True) -> None:
        """fd・flush が失敗するか・fileno を持つかを決める。"""
        self.fd = fd
        self.broken = broken
        self.has_fileno = has_fileno
        self.written: list[str] = []

    def write(self, text: str) -> int:
        """バッファへ溜める（パイプへの書込みと同じく、ここでは失敗しない）。"""
        self.written.append(text)
        return len(text)

    def flush(self) -> None:
        """読み手の居ないパイプなら BrokenPipeError を送る。"""
        if self.broken:
            raise BrokenPipeError("pipe")

    def fileno(self) -> int:
        """fd を返す。持たないストリームは io.UnsupportedOperation 相当の OSError を送る。"""
        if not self.has_fileno:
            raise OSError("fileno")
        return self.fd


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected_calls"),
    [
        (_Stream(1, broken=False), _Stream(2, broken=False), []),
        (_Stream(1, broken=True), _Stream(2, broken=False), [("open", "/dev/null", 1), ("dup2", 99, 1), ("close", 99)]),
        (_Stream(1, broken=False), _Stream(2, broken=True), [("open", "/dev/null", 1), ("dup2", 99, 2), ("close", 99)]),
        (_Stream(1, broken=True, has_fileno=False), _Stream(2, broken=False), []),
        (None, None, []),
    ],
    ids=["both-readable", "stdout-reader-gone", "stderr-reader-gone", "no-fileno", "closed-at-startup"],
)
def test_emit_block_output_discards_unflushable_output(
    monkeypatch: pytest.MonkeyPatch, stdout: _Stream | None, stderr: _Stream | None, expected_calls: list[tuple]
) -> None:
    """flush できない出力は fd を devnull へ付け替えて捨て、deny の 2 を返すこと。

    パイプへの書込みはバッファに溜まり、読み手が居ないことは終了時の flush で初めて失敗になる。Python は
    そこで終了コードを 120 に書き換え、PreToolUse では non-blocking error ＝ ツール実行へ反転する。
    """
    fake_os = _FakeOs()
    monkeypatch.setattr(hook_common, "os", fake_os)
    monkeypatch.setattr(hook_common.sys, "stdout", stdout)
    monkeypatch.setattr(hook_common.sys, "stderr", stderr)

    assert hook_common.emit_block_output("[Hook] BLOCKED: test") == 2
    assert fake_os.calls == expected_calls
