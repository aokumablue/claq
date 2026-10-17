"""typechange（通常ファイル <-> symlink）を commit 品質フックが扱えることを実 git で固定する。

`--diff-filter=ACMR` は T（typechange）を落とすため、symlink を通常ファイルへ
置き換える commit が secret スキャンを素通りしていた。T を足すと `git commit -a`
の作業ツリー側が symlink 先を辿って読むため、通常ファイルを symlink（ディレクトリ・
壊れた先・repo 外）へ置き換えた正当な commit が scan_error で止まる。symlink は
内容でなく link 先文字列が blob になる（git の保存形式）ので、その文字列を読む。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from claq.hooks import commit_quality_scanner, pre_bash_commit_quality
from claq.hooks.hook_common import resolve_repo_root

_SECRET_LINE = "api" + "_key" + ' = "hunter2secret"\n'


def _git(repo: Path, *args: str) -> None:
    """repo で git を実行し、失敗は例外にする。"""
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _make_repo(tmp_path: Path) -> Path:
    """a.py を 1 回コミット済みの使い捨て git リポジトリを作る。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "real.txt").write_text("hi\n", encoding="utf-8")
    (repo / "sub").mkdir()
    (repo / "sub" / "f.txt").write_text("f\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def _evaluate(command: str, repo: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """repo を cwd にして commit 品質フックを評価する。

    `resolve_repo_root` は lru_cache で前のテストの repo を覚えているため、前後で捨てる。
    """
    monkeypatch.chdir(repo)
    resolve_repo_root.cache_clear()
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": command}},
    )
    # コミットメッセージ検証はこのファイルの関心外なので「問題なし」(None) のスパイに替える。
    # None を返すだけではパッチが外れても結果が変わらないため、検証段へ実際に入ったことを記録で固定する。
    validated: list[str] = []
    monkeypatch.setattr(
        pre_bash_commit_quality, "validate_commit_message", lambda message_command: validated.append(message_command)
    )
    try:
        result = pre_bash_commit_quality.evaluate("payload")
        assert validated == [command]
        return result
    finally:
        resolve_repo_root.cache_clear()


def test_staged_and_unstaged_listings_include_typechange(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """symlink -> 通常ファイルの typechange が staged / unstaged の両列挙に載ること。"""
    repo = _make_repo(tmp_path)
    (repo / "a.py").unlink()
    (repo / "a.py").symlink_to("real.txt")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "link")
    (repo / "a.py").unlink()
    (repo / "a.py").write_text("y = 2\n", encoding="utf-8")
    monkeypatch.chdir(repo)

    assert pre_bash_commit_quality.get_unstaged_modified_files() == ["a.py"]
    _git(repo, "add", "a.py")
    assert pre_bash_commit_quality.get_staged_files() == ["a.py"]


@pytest.mark.parametrize("command", ["git commit -m x", "git commit -a -m x"])
def test_secret_hidden_behind_symlink_to_file_typechange_is_blocked(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """symlink を通常ファイルへ置き換えて secret を入れる commit は止まること。"""
    repo = _make_repo(tmp_path)
    (repo / "a.py").unlink()
    (repo / "a.py").symlink_to("real.txt")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "link")
    (repo / "a.py").unlink()
    (repo / "a.py").write_text(_SECRET_LINE, encoding="utf-8")
    _git(repo, "add", "a.py")

    assert _evaluate(command, repo, monkeypatch)["exitCode"] == 2


@pytest.mark.parametrize("command", ["git commit -m x", "git commit -a -m x"])
@pytest.mark.parametrize("kind", ["file", "dir", "broken", "outside"])
def test_regular_file_to_symlink_typechange_is_not_blocked(
    kind: str, command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """通常ファイルを symlink（ファイル・ディレクトリ・壊れた先・repo 外）へ置き換えた正当な commit は通すこと。"""
    repo = _make_repo(tmp_path)
    targets = {
        "file": "real.txt",
        "dir": "sub",
        "broken": "nowhere",
        "outside": str(tmp_path / "outside.txt"),
    }
    (repo / "a.py").unlink()
    (repo / "a.py").symlink_to(targets[kind])
    _git(repo, "add", "a.py")

    assert _evaluate(command, repo, monkeypatch)["exitCode"] == 0


@pytest.mark.parametrize("kind", ["file", "dir", "broken"])
def test_worktree_symlink_content_is_link_target_string(kind: str, tmp_path: Path) -> None:
    """symlink は先の内容でなく link 先文字列（git が blob に保存するもの）を返すこと。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "real.txt").write_text(_SECRET_LINE, encoding="utf-8")
    (repo / "sub").mkdir()
    link_target = {"file": "real.txt", "dir": "sub", "broken": "nowhere"}[kind]
    (repo / "link.py").symlink_to(link_target)

    assert commit_quality_scanner.get_worktree_file_content(repo, "link.py") == link_target


def test_worktree_symlink_to_outside_returns_link_string_not_target_content(tmp_path: Path) -> None:
    """repo 外を指す symlink でも repo 外の内容は読まず、link 先文字列だけを返すこと。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside_secret.txt"
    outside.write_text(_SECRET_LINE, encoding="utf-8")
    (repo / "evil.py").symlink_to(outside)

    content = commit_quality_scanner.get_worktree_file_content(repo, "evil.py")

    assert content == str(outside)
    assert "hunter2" not in content
