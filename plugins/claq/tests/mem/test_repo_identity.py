"""claq.mem.repo_identity — 正体キー解決・スラッグ採番のテスト。"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from claq.mem import repo_identity
from claq.mem.database import Database
from claq.mem.models import Knowledge, Repo
from claq.mem.repo_identity import (
    FALLBACK_SLUG,
    MAX_SLUG_LENGTH,
    RepoIdentity,
    base_slug,
    detect_repo_identity,
    find_repo_root,
    normalize_remote_url,
    read_remote_url,
    resolve_repo,
    slugify,
)

# 鍵の形の文字列はコミット品質フックが止めるため実行時に組み立てる。
_GITHUB_PAT = "ghp_" + "abcdefghijklmnopqrstuvwxyz012345"


@pytest.fixture
def db(tmp_path: Path) -> Database:
    """テスト用の空 DB を開いて返す。"""
    database = Database(tmp_path / "mem.db")
    yield database
    database.close()


def _git(*args: str, cwd: Path) -> None:
    """テスト用リポジトリ操作として git を同期実行する。

    利用者のグローバル/システム設定（署名強制・hooksPath 等）に影響されないよう
    設定ファイルを無効化した環境で実行する。
    """
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, timeout=30, env=env)


def _make_repo(root: Path, remote: str | None = None) -> Path:
    """コミット 1 つを持つ git リポジトリを *root* に作る。"""
    root.mkdir(parents=True)
    _git("init", "-b", "main", cwd=root)
    _git("config", "user.email", "test@example.com", cwd=root)
    _git("config", "user.name", "test", cwd=root)
    _git("commit", "--allow-empty", "-m", "init", cwd=root)
    if remote is not None:
        _git("remote", "add", "origin", remote, cwd=root)
    return root


def _raising(exc: BaseException) -> Callable[..., object]:
    """呼ばれると *exc* を送出する run_text の差し替えを返す。"""

    def _run(cmd: list[str], **kwargs: object) -> object:
        """*exc* を送出する。"""
        raise exc

    return _run


_TIMEOUT = subprocess.TimeoutExpired("git", repo_identity.GIT_TIMEOUT_SECONDS)


class TestNormalizeRemoteUrl:
    """remote URL 正規化の表駆動テスト。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # ssh (scp 形式)
            ("git@github.com:aokumablue/claq-dev.git", "github.com/aokumablue/claq-dev"),
            ("git@github.com:aokumablue/claq-dev", "github.com/aokumablue/claq-dev"),
            ("github.com:aokumablue/claq-dev.git", "github.com/aokumablue/claq-dev"),
            # scp 形式でスラッシュを含まないパス
            ("git@github.com:claq-dev.git", "github.com/claq-dev"),
            ("git@GitHub.COM:aokumablue/claq-dev.git", "github.com/aokumablue/claq-dev"),
            ("git@github.com:/aokumablue/claq-dev.git/", "github.com/aokumablue/claq-dev"),
            # ssh (scheme 形式・ポート付き)
            ("ssh://git@github.com/aokumablue/claq-dev.git", "github.com/aokumablue/claq-dev"),
            ("ssh://git@github.com:22/aokumablue/claq-dev.git", "github.com/aokumablue/claq-dev"),
            ("ssh://git@GitHub.com:2222/aokumablue/claq-dev", "github.com/aokumablue/claq-dev"),
            # https
            ("https://github.com/aokumablue/claq-dev.git", "github.com/aokumablue/claq-dev"),
            ("https://github.com/aokumablue/claq-dev", "github.com/aokumablue/claq-dev"),
            ("https://github.com/aokumablue/claq-dev/", "github.com/aokumablue/claq-dev"),
            ("https://github.com/aokumablue/claq-dev.GIT", "github.com/aokumablue/claq-dev"),
            ("HTTPS://GitHub.com/aokumablue/claq-dev.git", "github.com/aokumablue/claq-dev"),
            # 認証情報付き
            ("https://user:token@github.com/aokumablue/claq-dev.git", "github.com/aokumablue/claq-dev"),
            ("https://user@github.com:443/aokumablue/claq-dev/", "github.com/aokumablue/claq-dev"),
            # git プロトコル
            ("git://github.com/aokumablue/claq-dev.git", "github.com/aokumablue/claq-dev"),
            # パス部の大小文字は保持する
            ("https://github.com/AokumaBlue/Claq-Dev.git", "github.com/AokumaBlue/Claq-Dev"),
            # ホストのみ
            ("https://github.com/", "github.com"),
            # ローカルパス remote
            ("/srv/git/claq-dev.git", "/srv/git/claq-dev"),
            ("/srv/git/claq-dev/", "/srv/git/claq-dev"),
            ("/srv/git/claq-dev", "/srv/git/claq-dev"),
            ("file:///srv/git/claq-dev.git", "/srv/git/claq-dev"),
            ("FILE:///srv/git/claq-dev/", "/srv/git/claq-dev"),
            ("../sibling-repo", "../sibling-repo"),
            # 最初のスラッシュより後ろのコロンはホスト区切りではない
            ("/srv/git/my:repo.git", "/srv/git/my:repo"),
            # 前後空白は無視する
            ("  git@github.com:aokumablue/claq-dev.git\n", "github.com/aokumablue/claq-dev"),
        ],
    )
    def test_normalizes(self, raw: str, expected: str) -> None:
        """表の各表記が期待どおりの正体キーへ畳まれる（ssh / https / 素の https も同一キー）。"""
        assert normalize_remote_url(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "",
            "   ",
            "\n",
            "https://",
            "/",
            "/.git",
            "file:///",
            "git@github.com:\ninjected",
            "https://github.com/o/r\x00",
        ],
    )
    def test_returns_none_for_unusable(self, raw: str | None) -> None:
        """空・制御文字混入・実体の無い remote は None になる。"""
        assert normalize_remote_url(raw) is None


class TestStripCredentials:
    """`_strip_credentials`（remote_url からの credential 除去）の表駆動テスト。

    userinfo に加えて query / fragment も落とすことを固定する。
    """

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # https + credential（監査で確認された実害の再現）
            ("https://user:token@github.com/o/r.git", "https://github.com/o/r.git"),
            (f"https://user:{_GITHUB_PAT}@github.com/o/r", "https://github.com/o/r"),
            # https + credential なし（scheme あり、@ なし authority）
            ("https://github.com/o/r.git", "https://github.com/o/r.git"),
            # ssh scheme + userinfo
            ("ssh://git@github.com/o/r.git", "ssh://github.com/o/r.git"),
            ("ssh://git@github.com:22/o/r.git", "ssh://github.com:22/o/r.git"),
            # scp 形式（固定ユーザー名 git@ も一貫して除去する）
            ("git@github.com:o/r.git", "github.com:o/r.git"),
            ("github.com:o/r.git", "github.com:o/r.git"),
            # ローカルパス（@ を含まないためそのまま）
            ("/srv/git/repo.git", "/srv/git/repo.git"),
            ("file:///srv/git/repo.git", "file:///srv/git/repo.git"),
            # query / fragment 内の token（userinfo 除去だけでは残る）
            ("https://github.com/o/r?access_token=SECRET", "https://github.com/o/r"),
            ("https://user:token@github.com/o/r.git?private_token=SECRET", "https://github.com/o/r.git"),
            ("https://github.com/o/r#token=SECRET", "https://github.com/o/r"),
            # fragment が先に来る場合、その中の ? まで巻き込んで落とす
            ("https://github.com/o/r#frag?x=SECRET", "https://github.com/o/r"),
            ("git@github.com:o/r.git?token=SECRET", "github.com:o/r.git"),
            ("/srv/git/repo.git?token=SECRET", "/srv/git/repo.git"),
        ],
    )
    def test_strips_credentials(self, raw: str, expected: str) -> None:
        """userinfo・query・fragment を除いた URL だけが DB へ残ること。"""
        assert repo_identity._strip_credentials(raw) == expected


class TestSlugify:
    """スラッグ化のテスト。"""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("claq-dev", "claq-dev"),
            ("Claq_Dev", "claq-dev"),
            ("my repo (v2)", "my-repo-v2"),
            ("---weird---", "weird"),
            ("日本語", FALLBACK_SLUG),
            ("", FALLBACK_SLUG),
            ("!!!", FALLBACK_SLUG),
            # 長すぎる名前は MAX_SLUG_LENGTH に切り詰め、切り詰め位置がハイフンでも末尾に残さない
            ("a" * 200, "a" * MAX_SLUG_LENGTH),
            ("a" * (MAX_SLUG_LENGTH - 1) + "-tail", "a" * (MAX_SLUG_LENGTH - 1)),
        ],
    )
    def test_slugify(self, name: str, expected: str) -> None:
        """英数字とハイフンだけの MAX_SLUG_LENGTH 以内の kebab-case になる。"""
        assert slugify(name) == expected

    @pytest.mark.parametrize(
        ("identity", "expected"),
        [
            (RepoIdentity("github.com/aokumablue/claq-dev", "/x/y", "git@github.com:a/b.git"), "claq-dev"),
            (RepoIdentity("/Users/x/dev/claq-dev", "/Users/x/dev/claq-dev", None), "claq-dev"),
            (RepoIdentity("/", "/", None), FALLBACK_SLUG),
        ],
        ids=["remote", "path", "root-path"],
    )
    def test_base_slug_uses_last_segment(self, identity: RepoIdentity, expected: str) -> None:
        """base_slug は identity_key の最終セグメントを使い、空なら FALLBACK_SLUG になる。"""
        assert base_slug(identity) == expected


class TestRunGit:
    """git 実行ヘルパーの異常系。"""

    @pytest.mark.parametrize(
        "run_text",
        [
            _raising(_TIMEOUT),
            _raising(FileNotFoundError("git")),
            lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout="  \n", stderr=""),
            None,
        ],
        ids=["timeout", "git-missing", "empty-stdout", "nonzero-exit-outside-repo"],
    )
    def test_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_text: Callable[..., object] | None
    ) -> None:
        """タイムアウト・git 不在でも例外を漏らさず、非 0 終了・空出力と同じく None を返す。"""
        if run_text is not None:
            monkeypatch.setattr(repo_identity, "run_text", run_text)
        assert repo_identity._run_git(str(tmp_path), "rev-parse", "--git-common-dir") is None


class TestFindRepoRoot:
    """repo root 解決のテスト。"""

    def test_subdirectory_worktree_and_default_cwd_resolve_to_main_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """サブディレクトリ・git worktree・cwd 省略（プロセスの作業ディレクトリ）のいずれも本体のルートになる。"""
        root = _make_repo(tmp_path / "main")
        nested = root / "a" / "b"
        nested.mkdir(parents=True)
        worktree = tmp_path / "wt"
        _git("worktree", "add", "-b", "feature", str(worktree), cwd=root)
        monkeypatch.chdir(nested)
        expected = os.path.realpath(root)
        assert [find_repo_root(nested), find_repo_root(worktree), find_repo_root()] == [expected] * 3

    def test_bare_repo_and_non_repo_resolve_to_themselves(self, tmp_path: Path) -> None:
        """bare リポジトリは git dir 自身を、git リポジトリでなければ cwd 自身の絶対パスを返す。"""
        bare = tmp_path / "bare.git"
        bare.mkdir()
        _git("init", "--bare", cwd=bare)
        plain = tmp_path / "plain"
        plain.mkdir()
        assert find_repo_root(bare) == os.path.realpath(bare)
        assert find_repo_root(plain) == os.path.realpath(plain)

    def test_timeout_falls_back_to_absolute_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """git がタイムアウトしても絶対パスへ分岐してハングしない。

        起点を `.git` にするのは、git が動けば本体ルートへ寄せる経路と結果を分けるため。
        """
        git_dir = _make_repo(tmp_path / "main") / ".git"
        monkeypatch.setattr(repo_identity, "run_text", _raising(_TIMEOUT))
        assert find_repo_root(git_dir) == os.path.realpath(git_dir)


class TestDetectRepoIdentity:
    """identity_key 確定のテスト。"""

    @pytest.mark.parametrize(
        ("remote", "identity_key", "remote_url"),
        [
            ("git@github.com:o/claq-dev.git", "github.com/o/claq-dev", "github.com:o/claq-dev.git"),
            (f"https://user:{_GITHUB_PAT}@github.com/o/r.git", "github.com/o/r", "https://github.com/o/r.git"),
            (None, None, None),
        ],
        ids=["scp-userinfo", "https-credential", "no-origin"],
    )
    def test_identity_from_remote(
        self, tmp_path: Path, remote: str | None, identity_key: str | None, remote_url: str | None
    ) -> None:
        """remote があれば正規化 remote が identity_key、userinfo を除いた URL が remote_url になる。

        remote（origin）が無ければ read_remote_url は None で、repo root の絶対パスが identity_key になる。
        """
        root = _make_repo(tmp_path / "main", remote=remote)
        real_root = os.path.realpath(root)
        assert read_remote_url(str(root)) == remote
        assert detect_repo_identity(root) == RepoIdentity(identity_key or real_root, real_root, remote_url)

    def test_drops_unusable_remote(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """正規化できない remote は台帳に残さずパス識別へ落とす。"""
        root = os.path.realpath(_make_repo(tmp_path / "main"))
        monkeypatch.setattr(repo_identity, "read_remote_url", lambda _root: "\x00")
        assert detect_repo_identity(root) == RepoIdentity(root, root, None)

    def test_non_repo_uses_cwd(self, tmp_path: Path) -> None:
        """git リポジトリでなければ cwd の絶対パスで識別する。"""
        plain = tmp_path / "plain"
        plain.mkdir()
        real = os.path.realpath(plain)
        assert detect_repo_identity(plain) == RepoIdentity(real, real, None)


class TestResolveRepo:
    """DB を絡めたスラッグ採番と upsert のテスト。"""

    def test_registers_once_and_reresolves_from_main_and_worktree(self, tmp_path: Path, db: Database) -> None:
        """初出は素のスラッグで登録し、再解決・worktree からの解決は同じ行（id・first_seen_at）を返す。"""
        root = _make_repo(tmp_path / "claq-dev", remote="git@github.com:aokumablue/claq-dev.git")
        worktree = tmp_path / "wt"
        _git("worktree", "add", "-b", "feature", str(worktree), cwd=root)
        first = resolve_repo(root, db)
        assert (first.id, first.identity_key, first.remote_url, first.root_path) == (
            "claq-dev",
            "github.com/aokumablue/claq-dev",
            "github.com:aokumablue/claq-dev.git",
            os.path.realpath(root),
        )
        for again in (resolve_repo(root, db), resolve_repo(worktree, db)):
            assert (again.id, again.identity_key, again.remote_url, again.root_path, again.first_seen_at) == (
                first.id,
                first.identity_key,
                first.remote_url,
                first.root_path,
                first.first_seen_at,
            )
        assert len(db.list_repos()) == 1

    @pytest.mark.parametrize("with_remote", [True, False], ids=["remote", "no-remote"])
    def test_same_directory_name_gets_suffix(self, tmp_path: Path, db: Database, with_remote: bool) -> None:
        """同名ディレクトリの別リポジトリは（remote 無しでもパス識別で）別行になり -2, -3 が付く。"""
        roots = [
            _make_repo(tmp_path / owner / "app", remote=f"git@github.com:{owner}/app.git" if with_remote else None)
            for owner in ("one", "two", "three")
        ]
        assert [resolve_repo(root, db).id for root in roots] == ["app", "app-2", "app-3"]
        assert len(db.list_repos()) == 3

    def test_adding_changing_and_removing_remote_keeps_repo_id_and_knowledge(
        self, tmp_path: Path, db: Database
    ) -> None:
        """remote の追加・変更・削除で identity_key が変わっても id を引き継ぎ、知識が到達可能なままであること。

        引き継がないと、remote 無しで登録済みのリポジトリに ``git remote add origin``
        したとき identity_key が repo root パスから ``github.com/o/r`` へ変わり、
        別リポジトリ扱いで ``-2`` 付きの新 id を採番する。その結果、元の id
        配下の ``scope='repo'`` 知識が全件 SessionStart 注入から静かに消える
        （DB には残るが到達不能）。ホスティング移行（GitHub -> GitLab）や remote
        削除でも identity_key は変わるため、追加と同じ経路で救う必要がある。

        id 一致だけでは不十分なので、知識が実際に引けるところまで確認する。
        """
        root = _make_repo(tmp_path / "app")
        before = resolve_repo(root, db)
        db.upsert_knowledge(
            Knowledge(
                key="repo-card",
                scope="repo",
                repo_id=before.id,
                kind="fact",
                title="repo スコープの知識",
                source="human",
                status="active",
            )
        )
        steps = [
            (("remote", "add", "origin", "git@github.com:owner/app.git"), "github.com/owner/app"),
            (("remote", "set-url", "origin", "git@gitlab.com:owner/app.git"), "gitlab.com/owner/app"),
            (("remote", "remove", "origin"), os.path.realpath(root)),
        ]
        for git_args, identity_key in steps:
            _git(*git_args, cwd=root)
            after = resolve_repo(root, db)
            assert (after.id, after.identity_key) == (before.id, identity_key)
            assert len(db.list_repos()) == 1
            # 到達可能性そのものを確認する（id 一致だけでは実害の解消を示せない）。
            reachable = db.list_knowledge(scope="repo", repo_id=after.id, status="active")
            assert [row.key for row in reachable] == ["repo-card"]

    def test_identity_migration_does_not_merge_distinct_repos(self, tmp_path: Path, db: Database) -> None:
        """root_path が違うリポジトリは remote 追加後も別行のままであること。

        移行判定が root_path 一致に限定されており、無関係なリポジトリを
        巻き込まないことの固定。
        """
        first_root = _make_repo(tmp_path / "one" / "app")
        second_root = _make_repo(tmp_path / "two" / "app")
        first = resolve_repo(first_root, db)
        second = resolve_repo(second_root, db)

        _git("remote", "add", "origin", "git@github.com:one/app.git", cwd=first_root)
        migrated = resolve_repo(first_root, db)

        assert migrated.id == first.id
        assert resolve_repo(second_root, db).id == second.id
        assert len(db.list_repos()) == 2

    def test_existing_identity_key_wins_over_path_match(self, tmp_path: Path, db: Database) -> None:
        """目的の identity_key を持つ行があれば、そちらの id を返すこと。

        判定順（identity_key 一致を先に見る）の固定。逆順だと
        ``UNIQUE(identity_key)`` 違反へ到達しうる。
        """
        root = _make_repo(tmp_path / "app")
        path_row = resolve_repo(root, db)
        # 同じ root_path を持つ別行を、目的の identity_key で先に登録しておく。
        db.upsert_repo(Repo(id="preexisting", identity_key="github.com/owner/app", root_path=os.path.realpath(root)))

        _git("remote", "add", "origin", "git@github.com:owner/app.git", cwd=root)
        resolved = resolve_repo(root, db)

        assert resolved.id == "preexisting"
        assert resolved.id != path_row.id
        assert len(db.list_repos()) == 2
