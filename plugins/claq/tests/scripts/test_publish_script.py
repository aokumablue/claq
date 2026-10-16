"""`scripts/publish.sh` の配布除外リストが開発専用アーティファクトを網羅することを検証する。

配布ツリーへ「除去済みパスを指す設定ファイル」が残ると、実機監査が誤検出を
起こす。docs/adr/verification-scope-release-gates.md が警告する形で、``tests/`` を除去しつつ
``testpaths=["tests"]`` と ``fail_under=100`` を持つ ``pyproject.toml`` を
同梱すると、配布ツリーでの ``pytest`` が「coverage 0% で FAIL」という
回帰そっくりの出力を返し、「テストが消失した」と誤報告される。

散文（ADR・README）だけでは誤検出を止められないので、
ここでは除外リストの定義そのものを機械的に固定する。

あわせて、公開先 ``CLAQ_PUBLISH_REMOTE`` が dev 自身と同じリポジトリ（git common dir が
同一）を指すとき、公開先の ``update-ref`` / ``reset --hard`` が dev の ``main`` を
書き換える前に中断するガードを、tmp 上に作った実リポジトリで検証する。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_PUBLISH_SH = _REPO_ROOT / "scripts" / "publish.sh"
# 配布ツリーへ持ち出してはいけないパスと、その理由。
_REQUIRED_EXCLUSIONS = {
    "plugins/claq/tests/": "開発専用。配布物を最小化する（docs/adr/verification-scope-release-gates.md）",
    "plugins/claq/pyproject.toml": (
        "除去済みの tests/ を testpaths/fail_under が指すため、"
        "配布ツリーでの pytest が回帰そっくりに失敗する。"
        "plugin assets を含まない wheel の build 設定も同梱しない"
    ),
    "scripts/": "開発・リリース用スクリプト",
    "CLAUDE.md": "開発リポジトリ固有の指示",
    "conftest.py": "pytest 専用",
}


def _filter_repo_exclusions() -> set[str]:
    """publish.sh の `git filter-repo --invert-paths` が除外するパス集合を返す。"""
    text = _PUBLISH_SH.read_text(encoding="utf-8")
    block = re.search(r"git filter-repo\b(.*?)--force", text, re.DOTALL)
    assert block is not None, "publish.sh に git filter-repo ブロックが見つからない"
    assert "--invert-paths" in block.group(1), "--invert-paths が無いと除外ではなく抽出になる"
    return set(re.findall(r"--path\s+(\S+)", block.group(1)))


def test_publish_excludes_all_development_only_artifacts() -> None:
    """開発専用アーティファクトが 1 つ残らず配布除外されていること。"""
    excluded = _filter_repo_exclusions()
    missing = {path: reason for path, reason in _REQUIRED_EXCLUSIONS.items() if path not in excluded}
    assert not missing, f"配布除外から漏れている: {missing}"


def test_publish_does_not_exclude_paths_that_no_longer_exist() -> None:
    """除外リストに実在しないパスが残っていないこと。

    実体が消えた後も除外エントリが残ると、リストが「いま何を配布しないか」の
    正確な記述でなくなり、レビュー時の判断材料として信用できなくなる。
    """
    stale = [path for path in _filter_repo_exclusions() if not (_REPO_ROOT / path).exists()]
    assert not stale, f"実在しないパスが除外リストに残っている: {stale}"


# 配布ツリーの pytest を「回帰そっくりに失敗」させうる設定キー。
#
# `testpaths` だけを走査すると、``setup.cfg`` に
# ``[coverage:report] fail_under = 100`` だけを置けば本テストが緑のまま通る
# （かつ setup.cfg は publish.sh の除外 5 件に含まれない）。docstring の
# 「両方を守る」という宣言に合わせ、走査側をキーの列挙へ揃える。`addopts` も同じ経路で
# ``--cov`` / ``--cov-fail-under`` を持ち込めるため含める。
_TEST_CONFIG_KEYS = ("testpaths", "fail_under", "addopts")


@pytest.mark.parametrize("key", _TEST_CONFIG_KEYS)
def test_distributed_pyproject_is_the_only_carrier_of_test_config(key: str) -> None:
    """テスト設定キーが、配布除外される pyproject.toml にしか無いこと。

    別ファイル（setup.cfg / tox.ini / pytest.ini 等）へ同じ設定が移ると、
    除外リストを 1 つ足しただけでは誤検出の再発を防げなくなる。キーごとに
    独立ケースとして回し、1 キーの漏れが他キーの一致に紛れないようにする。
    """
    plugin_root = _REPO_ROOT / "plugins" / "claq"
    carriers = sorted(
        path.relative_to(_REPO_ROOT).as_posix()
        for path in plugin_root.glob("*")
        if path.is_file() and (key in path.read_text(encoding="utf-8", errors="ignore"))
    )
    assert carriers == ["plugins/claq/pyproject.toml"]


_BASH = "/bin/bash"
_RELEASE_SUBJECT = "release: v1.0.0"
_SELF_REMOTE_ERROR = "公開先が dev 自身と同じリポジトリです"
_PRE_EXISTING_REMOTE_ERROR = "公開先リポジトリが見つかりません"
_FETCH_MARKER = "Fetching publish origin..."
_GATE_FAILED_MARKER = "テストゲート失敗（再開モード、push なし）"


def _git(cwd: Path, *args: str, env: dict[str, str]) -> str:
    """隔離環境で git を実行し、標準出力（末尾空白除去）を返す。

    Args:
        cwd: git を実行するディレクトリ。
        *args: git のサブコマンドと引数。
        env: 隔離済みの環境変数。

    Returns:
        標準出力。

    Raises:
        subprocess.CalledProcessError: git が失敗した場合。
    """
    result = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True, timeout=60)
    return result.stdout.strip()


def _isolated_env(tmp_path: Path) -> dict[str, str]:
    """実ユーザーの git 設定・hook・venv を一切拾わない環境変数を作る。

    ``CLAQ_VENV`` を存在しないパスにするので、ガードが無くても ``run_gate`` が
    ``filter-repo`` / ``update-ref`` / push より前に失敗し、テストが副作用を持たない。

    Args:
        tmp_path: HOME と空の gitconfig の置き場。

    Returns:
        subprocess に渡す環境変数。
    """
    home = tmp_path / "home"
    home.mkdir()
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("", encoding="utf-8")
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": str(gitconfig),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "CLAQ_VENV": str(tmp_path / "no-such-venv"),
    }


def _init_repo(path: Path, env: dict[str, str]) -> Path:
    """版 1.0.0 の plugin.json を持ち ``release: v1.0.0`` で commit 済みのリポジトリを作る。

    Args:
        path: 作成先（親は既存であること）。
        env: 隔離済みの環境変数。

    Returns:
        ``path`` そのもの。
    """
    path.mkdir()
    _git(path, "init", "--quiet", "-b", "main", env=env)
    manifest = path / "plugins" / "claq" / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"version": "1.0.0"}), encoding="utf-8")
    _git(path, "add", ".", env=env)
    _git(path, "commit", "--quiet", "-m", _RELEASE_SUBJECT, env=env)
    return path


def _run_publish(cwd: Path, remote: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """``publish.sh --no-push`` を ``cwd`` で実行する。

    Args:
        cwd: dev 側の作業ディレクトリ。
        remote: ``CLAQ_PUBLISH_REMOTE`` に渡す公開先。
        env: 隔離済みの環境変数。

    Returns:
        完了したプロセス。
    """
    return subprocess.run(
        [_BASH, str(_PUBLISH_SH), "--no-push"],
        cwd=cwd,
        env={**env, "CLAQ_PUBLISH_REMOTE": str(remote)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.skipif(sys.platform == "win32", reason="publish.sh は POSIX bash 専用")
class TestPublishRemoteGuard:
    """公開先が dev 自身と refs を共有するとき、dev の main を書き換える前に中断する。"""

    @pytest.fixture
    def env(self, tmp_path: Path) -> dict[str, str]:
        """隔離環境変数。"""
        return _isolated_env(tmp_path)

    @pytest.fixture
    def dev(self, tmp_path: Path, env: dict[str, str]) -> Path:
        """dev 側のリポジトリ。"""
        return _init_repo(tmp_path / "dev", env)

    def _assert_aborted_before_touching_remote(
        self, result: subprocess.CompletedProcess[str], dev: Path, before: str, env: dict[str, str], message: str
    ) -> None:
        """指定メッセージで中断し、fetch 以降へ進まず dev の main が不変であること。"""
        assert result.returncode != 0
        assert message in result.stderr
        assert _FETCH_MARKER not in result.stdout
        assert _git(dev, "rev-parse", "refs/heads/main", env=env) == before

    def test_aborts_when_remote_is_the_dev_checkout(self, dev: Path, env: dict[str, str]) -> None:
        """公開先が dev 自身なら中断する。"""
        before = _git(dev, "rev-parse", "refs/heads/main", env=env)
        result = _run_publish(dev, dev, env)
        self._assert_aborted_before_touching_remote(result, dev, before, env, _SELF_REMOTE_ERROR)

    def test_aborts_when_remote_is_a_symlink_to_the_dev_checkout(
        self, tmp_path: Path, dev: Path, env: dict[str, str]
    ) -> None:
        """symlink 経由で dev 自身を指す公開先でも中断する。"""
        link = tmp_path / "link-to-dev"
        link.symlink_to(dev, target_is_directory=True)
        before = _git(dev, "rev-parse", "refs/heads/main", env=env)
        result = _run_publish(dev, link, env)
        self._assert_aborted_before_touching_remote(result, dev, before, env, _SELF_REMOTE_ERROR)

    def test_aborts_when_dev_is_a_linked_worktree_of_the_remote(
        self, tmp_path: Path, dev: Path, env: dict[str, str]
    ) -> None:
        """dev が公開先の linked worktree でも、refs を共有するので中断する。"""
        worktree = tmp_path / "dev-worktree"
        _git(dev, "worktree", "add", "--quiet", "-b", "work", str(worktree), env=env)
        before = _git(dev, "rev-parse", "refs/heads/main", env=env)
        result = _run_publish(worktree, dev, env)
        self._assert_aborted_before_touching_remote(result, worktree, before, env, _SELF_REMOTE_ERROR)

    def test_aborts_when_remote_is_a_linked_worktree_of_dev(
        self, tmp_path: Path, dev: Path, env: dict[str, str]
    ) -> None:
        """公開先が dev の linked worktree なら中断する。

        worktree の ``.git`` はファイルなので、ガードより前の「公開先リポジトリが
        見つかりません」で止まる。この経路を許可対象へ広げない（worktree 側が別ブランチを
        checkout していると ``reset --hard`` がそのブランチを動かすため）。
        """
        worktree = tmp_path / "remote-worktree"
        _git(dev, "worktree", "add", "--quiet", "-b", "work", str(worktree), env=env)
        before = _git(dev, "rev-parse", "refs/heads/main", env=env)
        result = _run_publish(dev, worktree, env)
        self._assert_aborted_before_touching_remote(result, dev, before, env, _PRE_EXISTING_REMOTE_ERROR)

    def test_aborts_when_remote_differs_from_dev_only_by_case(
        self, tmp_path: Path, dev: Path, env: dict[str, str]
    ) -> None:
        """大文字小文字を区別しない FS で、綴りの大小だけが違う公開先でも中断する。

        ``pwd -P`` は入力の綴りを保つので、文字列比較では同一実体を別物と誤判定する。
        """
        flipped = dev.with_name(dev.name.upper())
        if not flipped.exists():
            pytest.skip("大文字小文字を区別する FS")
        before = _git(dev, "rev-parse", "refs/heads/main", env=env)
        result = _run_publish(dev, flipped, env)
        self._assert_aborted_before_touching_remote(result, dev, before, env, _SELF_REMOTE_ERROR)

    def test_cdpath_does_not_make_a_different_repository_look_identical(
        self, tmp_path: Path, dev: Path, env: dict[str, str]
    ) -> None:
        """``CDPATH`` に別の ``.git`` があっても、相対 ``.git`` の解決がそれに引きずられない。

        ``cd .git`` が ``CDPATH`` を引くと、dev 側も公開先側も decoy を指して一致し、
        別リポジトリなのに「同一」と誤判定して中断してしまう。
        """
        decoy_root = tmp_path / "decoy"
        (decoy_root / ".git").mkdir(parents=True)
        remote = _init_repo(tmp_path / "remote", env)
        result = _run_publish(dev, remote, {**env, "CDPATH": str(decoy_root)})
        assert _SELF_REMOTE_ERROR not in result.stderr
        assert _GATE_FAILED_MARKER in result.stderr

    def test_proceeds_when_remote_is_a_different_repository(
        self, tmp_path: Path, dev: Path, env: dict[str, str]
    ) -> None:
        """別リポジトリなら従来どおりガードを通過し、後続のテストゲートまで進む。"""
        remote = _init_repo(tmp_path / "remote", env)
        result = _run_publish(dev, remote, env)
        assert _SELF_REMOTE_ERROR not in result.stderr
        assert _FETCH_MARKER in result.stdout
        assert _GATE_FAILED_MARKER in result.stderr
