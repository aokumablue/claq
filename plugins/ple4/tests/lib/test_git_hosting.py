"""Git hosting service ヘルパーのテスト。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from ple4.lib import git_hosting
from ple4.lib.git_hosting import (
    _remote_host,
    build_git_hosting_item_url,
    detect_git_hosting_service,
    extract_git_hosting_item_details,
    get_git_hosting_cli_name,
    get_git_hosting_create_command,
    get_git_hosting_item_label,
    get_git_hosting_item_short_label,
    get_git_hosting_review_command,
    normalize_git_hosting_service,
)


def test_normalize_git_hosting_service_defaults_to_github() -> None:
    assert normalize_git_hosting_service("github") == "github"
    assert normalize_git_hosting_service("gitlab") == "gitlab"
    with pytest.warns(UserWarning):
        assert normalize_git_hosting_service("unknown") == "github"


@pytest.mark.parametrize(
    ("remote_url", "returncode", "expected"),
    [
        ("git@github.com:owner/repo.git", 0, "github"),
        ("https://github.com/owner/repo.git", 0, "github"),
        ("git@gitlab.com:group/repo.git", 0, "gitlab"),
        ("https://gitlab.example.com/group/repo.git", 0, "gitlab"),
        ("https://bitbucket.org/owner/repo.git", 0, "github"),  # 未対応は github fallback
        ("", 1, "github"),  # git 失敗は default
    ],
)
def test_detect_git_hosting_service_infers_from_remote_url(
    monkeypatch: pytest.MonkeyPatch,
    remote_url: str,
    returncode: int,
    expected: str,
) -> None:
    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=remote_url, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert detect_git_hosting_service() == expected


def test_detect_git_hosting_service_handles_subprocess_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def raiser(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("git not found")

    monkeypatch.setattr(subprocess, "run", raiser)
    assert detect_git_hosting_service() == "github"


def test_detect_git_hosting_service_accepts_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="https://gitlab.com/x/y.git", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert detect_git_hosting_service(cwd=tmp_path) == "gitlab"
    assert str(tmp_path) in captured["cmd"]  # type: ignore[operator]


def test_git_hosting_helpers_support_github_and_gitlab_urls() -> None:
    github_url = build_git_hosting_item_url("github", "owner/repo", "12")
    gitlab_url = build_git_hosting_item_url("gitlab", "group/subgroup/repo", "34")

    assert github_url == "https://github.com/owner/repo/pull/12"
    assert gitlab_url == "https://gitlab.com/group/subgroup/repo/-/merge_requests/34"

    assert extract_git_hosting_item_details("github", github_url) == ("owner/repo", "12")
    assert extract_git_hosting_item_details("gitlab", gitlab_url) == ("group/subgroup/repo", "34")


def test_git_hosting_commands_and_labels() -> None:
    assert get_git_hosting_create_command("github") == "gh pr create"
    assert get_git_hosting_create_command("gitlab") == "glab mr create"
    assert get_git_hosting_cli_name("github") == "gh"
    assert get_git_hosting_cli_name("gitlab") == "glab"
    assert get_git_hosting_review_command("github") == "gh pr review"
    assert get_git_hosting_review_command("gitlab") == "glab mr view"
    assert get_git_hosting_item_label("github") == "Pull Request"
    assert get_git_hosting_item_label("gitlab") == "Merge Request"
    assert get_git_hosting_item_short_label("github") == "PR"
    assert get_git_hosting_item_short_label("gitlab") == "MR"


class TestNormalizeGitHostingServiceEdgeCases:
    """normalize_git_hosting_service のエッジケースを網羅するテスト。"""

    # デシジョンテーブル:
    # | value     | default   | 期待値   | warn? |
    # |-----------|-----------|----------|-------|
    # | "github"  | any       | "github" | No    |
    # | "gitlab"  | any       | "gitlab" | No    |
    # | "bad"     | "github"  | "github" | Yes (invalid value + valid default) |
    # | ""        | "github"  | "github" | No   (空は warn なし) |
    # | "bad"     | "bad"     | "github" | Yes (invalid value + invalid default) |
    # | ""        | "bad"     | "github" | No   (value が空 → warn なし, default も invalid) |

    def test_invalid_value_with_invalid_default_warns_and_returns_github(self) -> None:
        """value も default も不正な場合、warn して GITHUB を返すこと。"""
        with pytest.warns(UserWarning, match="Invalid git-hosting-service"):
            result = normalize_git_hosting_service("unknown", default="also-invalid")
        assert result == "github"

    def test_empty_value_with_invalid_default_returns_github_no_warn(self) -> None:
        """value が空で default も不正な場合、warn なしで GITHUB を返すこと。"""
        # value が空文字 → normalized が空 → warn は発生しない
        result = normalize_git_hosting_service("", default="bad-default")
        assert result == "github"

    def test_none_value_with_invalid_default_returns_github_no_warn(self) -> None:
        """value が None で default も不正な場合、warn なしで GITHUB を返すこと。"""
        result = normalize_git_hosting_service(None, default="bad-default")
        assert result == "github"

    def test_valid_value_overrides_invalid_default(self) -> None:
        """value が有効なら default は使われないこと。"""
        result = normalize_git_hosting_service("gitlab", default="invalid")
        assert result == "gitlab"

    def test_extract_returns_none_when_no_match(self) -> None:
        """パターンにマッチしない場合 None を返すこと。"""
        result = extract_git_hosting_item_details("github", "no url here")
        assert result is None

    def test_extract_returns_none_for_invalid_service(self) -> None:
        """SERVICE_ITEM_URL_PATTERNS に存在しない service では None を返すこと。

        normalize_git_hosting_service が "github" に正規化するため、
        実際には github パターンが使われるが、マッチしない文字列で None を確認する。
        """
        result = extract_git_hosting_item_details("github", "https://example.com/no-match")
        assert result is None

    def test_extract_returns_none_when_normalization_returns_unknown(self, monkeypatch) -> None:
        monkeypatch.setattr("ple4.lib.git_hosting.normalize_git_hosting_service", lambda value, default="github": "bitbucket")
        assert extract_git_hosting_item_details("github", "https://github.com/owner/repo/pull/1") is None


def test_normalize_invalid_value_and_invalid_default() -> None:
    """値もデフォルトも不正なら GITHUB にフォールバックする。"""
    import warnings

    from ple4.lib.git_hosting import GITHUB, normalize_git_hosting_service

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert normalize_git_hosting_service("invalid", default="alsobad") == GITHUB


def test_normalize_empty_value_with_valid_default() -> None:
    """値が空で有効なデフォルトがあれば警告なしでデフォルトを返す。"""
    from ple4.lib.git_hosting import normalize_git_hosting_service

    assert normalize_git_hosting_service("", default="github") == "github"


class TestRemoteHostExtraction:
    """origin URL からホスト部だけを取り出す（INFO: URL 全体の部分一致をやめる）。"""

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
    def test_host_is_extracted(self, url: str, expected: str) -> None:
        assert _remote_host(url) == expected


@pytest.mark.parametrize(
    ("remote_url", "expected"),
    [
        # INFO の本体: リポジトリ名に他方の名前を含んでも誤判定しない
        ("https://github.com/acme/gitlab-migration.git", "github"),
        ("https://gitlab.com/acme/github-mirror.git", "gitlab"),
        # scp 形式の自前 GitLab（urlsplit だけで済ませると host が取れず既定へ落ちる）
        ("git@gitlab.example.com:group/repo.git", "gitlab"),
        ("ssh://git@gitlab.example.com:2222/group/repo.git", "gitlab"),
        # ホストを取り出せない形は default（ローカルパスは hosting service ではない）
        ("/srv/git/gitlab/repo.git", "github"),
    ],
)
def test_detect_git_hosting_service_uses_host_only(
    monkeypatch: pytest.MonkeyPatch, remote_url: str, expected: str
) -> None:
    """判定は URL 全体ではなくホスト部で行うこと。"""
    monkeypatch.setattr(
        git_hosting,
        "run_text",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, remote_url + "\n", ""),
    )

    assert detect_git_hosting_service() == expected


def test_detect_git_hosting_service_survives_non_ascii_remote_url(tmp_path: Path) -> None:
    """非 ASCII を含む origin URL でも例外を出さないこと（H-19a）。

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
        "import locale, sys\n"
        "assert locale.getpreferredencoding(False).lower() in ('us-ascii', 'ascii'), "
        "locale.getpreferredencoding(False)\n"
        "from ple4.lib.git_hosting import detect_git_hosting_service\n"
        f"sys.stdout.write(detect_git_hosting_service(cwd={str(repo)!r}))\n"
    )
    env = {
        **os.environ,
        "PYTHONUTF8": "0",
        "PYTHONCOERCECLOCALE": "0",
        "LC_ALL": "C",
        "LANG": "C",
    }

    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=30
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "gitlab"


def test_detect_git_hosting_service_routes_through_run_text() -> None:
    """`subprocess.run` 直呼びへ戻っていないこと。

    encoding 指定を集約した `run_text` を迂回する経路を残すと、
    「どちらが正しい呼び方か」が分岐して次の追加でまた迂回側が選ばれる。
    """
    source = Path(git_hosting.__file__).read_text(encoding="utf-8")

    assert "subprocess.run(" not in source
