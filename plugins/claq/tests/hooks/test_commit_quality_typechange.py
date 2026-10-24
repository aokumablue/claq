"""typechange（通常ファイル <-> symlink）を commit 品質フックが扱えることを実 git で固定する。

`--diff-filter=ACMR` は T（typechange）を落とすため、symlink を通常ファイルへ
置き換える commit が secret スキャンを素通りしていた。T を足すと `git commit -a`
の作業ツリー側が symlink 先を辿って読むため、通常ファイルを symlink（ディレクトリ・
壊れた先・repo 外）へ置き換えた正当な commit が scan_error で止まる。symlink は
内容でなく link 先文字列が blob になる（git の保存形式）ので、その文字列を読む。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claq.hooks import commit_quality_scanner, pre_bash_commit_quality

from ._support import _evaluate_in_repo, _git, _init_git_repo

_SECRET_LINE = "api" + "_key" + ' = "hunter2secret"\n'


def _make_repo(tmp_path: Path) -> Path:
    """a.py・real.txt・sub/f.txt を 1 回コミット済みの使い捨て git リポジトリを作る。"""
    return _init_git_repo(tmp_path / "repo", {"a.py": "x = 1\n", "real.txt": "hi\n", "sub/f.txt": "f\n"})


def _replace_committed_symlink_with_file(tmp_path: Path, content: str) -> Path:
    """a.py を symlink にしてコミットした後、作業ツリーで ``content`` の通常ファイルへ置き換えた repo を返す。"""
    repo = _make_repo(tmp_path)
    (repo / "a.py").unlink()
    (repo / "a.py").symlink_to("real.txt")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "link")
    (repo / "a.py").unlink()
    (repo / "a.py").write_text(content, encoding="utf-8")
    return repo


def test_staged_and_unstaged_listings_include_typechange(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """symlink -> 通常ファイルの typechange が staged / unstaged の両列挙に載ること。"""
    repo = _replace_committed_symlink_with_file(tmp_path, "y = 2\n")
    monkeypatch.chdir(repo)

    assert pre_bash_commit_quality.get_unstaged_modified_files() == ["a.py"]
    _git(repo, "add", "a.py")
    assert pre_bash_commit_quality.get_staged_files() == ["a.py"]


@pytest.mark.parametrize("command", ["git commit -m x", "git commit -a -m x"])
def test_secret_hidden_behind_symlink_to_file_typechange_is_blocked(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """symlink を通常ファイルへ置き換えて secret を入れる commit は止まること。"""
    repo = _replace_committed_symlink_with_file(tmp_path, _SECRET_LINE)
    _git(repo, "add", "a.py")

    assert _evaluate_in_repo(command, repo, monkeypatch)["exitCode"] == 2


@pytest.mark.parametrize("command", ["git commit -m x", "git commit -a -m x"])
@pytest.mark.parametrize("kind", ["file", "dir", "broken", "outside"])
def test_regular_file_to_symlink_typechange_is_not_blocked(
    kind: str, command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """通常ファイルを symlink（ファイル・ディレクトリ・壊れた先・repo 外）へ置き換えた正当な commit は通すこと。"""
    repo = _make_repo(tmp_path)
    targets = {"file": "real.txt", "dir": "sub", "broken": "nowhere", "outside": str(tmp_path / "outside.txt")}
    (repo / "a.py").unlink()
    (repo / "a.py").symlink_to(targets[kind])
    _git(repo, "add", "a.py")

    assert _evaluate_in_repo(command, repo, monkeypatch)["exitCode"] == 0


def test_staged_content_of_stage_prefixed_name_is_read_from_that_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``0:README.md`` を「stage 0 の README.md」と読み違えず、その名前の blob を読む。

    ``git show :0:README.md`` 形の stage 前置が無いと、git は ``:0:README.md`` を
    stage 番号付きの指定として解釈し、別ファイルの中身を走査する。
    """
    repo = _init_git_repo(tmp_path / "repo", {"README.md": "readme\n"})
    (repo / "0:README.md").write_text(_SECRET_LINE, encoding="utf-8")
    _git(repo, "add", "0:README.md")
    monkeypatch.chdir(repo)

    assert commit_quality_scanner.get_staged_file_content("0:README.md") == _SECRET_LINE
    assert commit_quality_scanner.get_staged_file_content("README.md") == "readme\n"
