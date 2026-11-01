"""Git hosting service ヘルパーのテスト。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from claq.lib import git_hosting
from claq.lib.git_hosting import _remote_host, detect_git_hosting_service


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/owner/repo.git", "github.com"),
        ("http://gitlab.example.com/g/r.git", "gitlab.example.com"),
        ("ssh://git@gitlab.example.com:2222/g/r.git", "gitlab.example.com"),
        ("https://user:pw@gitlab.example.com/g/r.git", "gitlab.example.com"),
        ("ssh://git@[::1]:22/g/r.git", "[::1]"),
        # scp 形式（scheme が無いので専用パターンが要る）
        ("git@gitlab.example.com:group/repo.git", "gitlab.example.com"),
        ("gitlab.example.com:group/repo.git", "gitlab.example.com"),
        # ホストを特定できない形
        ("/srv/git/gitlab/repo.git", ""),
        ("../relative/repo.git", ""),
        ("", ""),
    ],
)
def test_remote_host_is_extracted(url: str, expected: str) -> None:
    """origin URL からホスト部だけを取り出す（URL 全体の部分一致をやめる）。"""
    assert _remote_host(url) == expected


@pytest.mark.parametrize(
    ("stdout", "returncode", "expected"),
    [
        ("git@github.com:owner/repo.git", 0, "github"),
        ("https://github.com/owner/repo.git", 0, "github"),
        ("git@gitlab.com:group/repo.git", 0, "gitlab"),
        ("https://gitlab.example.com/group/repo.git", 0, "gitlab"),
        ("https://bitbucket.org/owner/repo.git", 0, None),  # 未対応は不明
        ("", 1, None),  # origin が無い・git 失敗は不明
        # リポジトリ名に他方の名前を含んでも誤判定しない（判定はホスト部だけ）
        ("https://github.com/acme/gitlab-migration.git\n", 0, "github"),
        ("https://gitlab.com/acme/github-mirror.git\n", 0, "gitlab"),
        # scp 形式の自前 GitLab（urlsplit だけで済ませると host が取れず既定へ落ちる）
        ("git@gitlab.example.com:group/repo.git\n", 0, "gitlab"),
        ("ssh://git@gitlab.example.com:2222/group/repo.git\n", 0, "gitlab"),
        # ホストを取り出せない形は不明（ローカルパスは hosting service ではない）
        ("/srv/git/gitlab/repo.git\n", 0, None),
    ],
)
def test_detect_git_hosting_service_infers_from_remote_host(
    monkeypatch: pytest.MonkeyPatch, stdout: str, returncode: int, expected: str | None
) -> None:
    """`git remote get-url origin` の出力のホスト部から hosting service を推測する（推測できなければ None）。"""
    monkeypatch.setattr(
        git_hosting, "run_text", lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, returncode, stdout, "")
    )

    assert detect_git_hosting_service() == expected


def _raiser(error: Exception) -> object:
    """呼ばれたら ``error`` を送出する run_text の代わりを返す。"""

    def raiser(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        """``error`` を送出する。"""
        raise error

    return raiser


def test_detect_git_hosting_service_without_git_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """git が無ければ不明（None）を返す。"""
    monkeypatch.setattr(git_hosting, "run_text", _raiser(FileNotFoundError("git")))
    assert detect_git_hosting_service() is None


@pytest.mark.parametrize("error", [PermissionError("git"), subprocess.TimeoutExpired("git", 5)])
def test_detect_git_hosting_service_propagates_other_failures(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """起動できない（git 不在以外）・時間切れは、既定値で続けずにそのまま伝える。"""
    monkeypatch.setattr(git_hosting, "run_text", _raiser(error))
    with pytest.raises(type(error)):
        detect_git_hosting_service()


def test_detect_git_hosting_service_accepts_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cwd を ``git -C`` へ渡して判定する。"""
    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """コマンドを記録して gitlab の URL を返す。"""
        captured.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "https://gitlab.com/x/y.git", "")

    monkeypatch.setattr(git_hosting, "run_text", fake_run)
    assert detect_git_hosting_service(cwd=tmp_path) == "gitlab"
    assert captured == [["git", "-C", str(tmp_path), "remote", "get-url", "origin"]]


def test_detect_git_hosting_service_survives_non_ascii_remote_url(tmp_path: Path) -> None:
    """非 ASCII を含む origin URL でも例外を出さないこと。

    `text=True` に encoding 指定が無いと locale 依存のデコードになり、
    `LC_ALL=C`（preferred encoding = US-ASCII）環境で `UnicodeDecodeError` が
    `except (OSError, SubprocessError)` を貫通して docstring の
    「例外は発生しません」が破れる。実測条件は `launcher.force_utf8_streams` の
    docstring が記録している `PYTHONUTF8=0 LC_ALL=C`。

    **子プロセスとして起動する必要がある。** `locale.getpreferredencoding()` は
    インタプリタ起動時に確定するため、実行中の pytest で `monkeypatch.setenv`
    しても再現せず、テストが素通りする（実測: 素通りした）。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    # 日本語グループ名を持つ自前 GitLab の remote
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "https://gitlab.example.com/開発/repo.git"],
        check=True,
        capture_output=True,
    )
    script = (
        "import codecs, locale, sys\n"
        "enc = locale.getpreferredencoding(False)\n"
        "assert codecs.lookup(enc).name == 'ascii', enc\n"
        "from claq.lib.git_hosting import detect_git_hosting_service\n"
        f"sys.stdout.write(detect_git_hosting_service(cwd={str(repo)!r}))\n"
    )
    env = {**os.environ, "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0", "LC_ALL": "C", "LANG": "C"}

    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=30)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "gitlab"


def test_detect_git_hosting_service_routes_through_run_text() -> None:
    """`subprocess.run` 直呼びへ戻っていないこと。

    encoding 指定を集約した `run_text` を迂回する経路を残すと、
    「どちらが正しい呼び方か」が分岐して次の追加でまた迂回側が選ばれる。
    """
    source = Path(git_hosting.__file__).read_text(encoding="utf-8")

    assert "subprocess.run(" not in source
