"""ple4.mem.repo_identity — 正体キー解決・スラッグ採番のテスト。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ple4.mem import repo_identity
from ple4.mem.database import Database
from ple4.mem.models import Repo
from ple4.mem.repo_identity import (
    FALLBACK_SLUG,
    MAX_SLUG_LENGTH,
    RepoIdentity,
    allocate_repo_slug,
    base_slug,
    detect_repo_identity,
    find_repo_root,
    normalize_remote_url,
    read_remote_url,
    resolve_repo,
    slugify,
)


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
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


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


class TestNormalizeRemoteUrl:
    """remote URL 正規化の表駆動テスト。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # ssh (scp 形式)
            ("git@github.com:aokumablue/ple4-dev.git", "github.com/aokumablue/ple4-dev"),
            ("git@github.com:aokumablue/ple4-dev", "github.com/aokumablue/ple4-dev"),
            ("github.com:aokumablue/ple4-dev.git", "github.com/aokumablue/ple4-dev"),
            # scp 形式でスラッシュを含まないパス
            ("git@github.com:ple4-dev.git", "github.com/ple4-dev"),
            ("git@GitHub.COM:aokumablue/ple4-dev.git", "github.com/aokumablue/ple4-dev"),
            ("git@github.com:/aokumablue/ple4-dev.git/", "github.com/aokumablue/ple4-dev"),
            # ssh (scheme 形式・ポート付き)
            ("ssh://git@github.com/aokumablue/ple4-dev.git", "github.com/aokumablue/ple4-dev"),
            ("ssh://git@github.com:22/aokumablue/ple4-dev.git", "github.com/aokumablue/ple4-dev"),
            ("ssh://git@GitHub.com:2222/aokumablue/ple4-dev", "github.com/aokumablue/ple4-dev"),
            # https
            ("https://github.com/aokumablue/ple4-dev.git", "github.com/aokumablue/ple4-dev"),
            ("https://github.com/aokumablue/ple4-dev", "github.com/aokumablue/ple4-dev"),
            ("https://github.com/aokumablue/ple4-dev/", "github.com/aokumablue/ple4-dev"),
            ("https://github.com/aokumablue/ple4-dev.GIT", "github.com/aokumablue/ple4-dev"),
            ("HTTPS://GitHub.com/aokumablue/ple4-dev.git", "github.com/aokumablue/ple4-dev"),
            # 認証情報付き
            ("https://user:token@github.com/aokumablue/ple4-dev.git", "github.com/aokumablue/ple4-dev"),
            ("https://user@github.com:443/aokumablue/ple4-dev/", "github.com/aokumablue/ple4-dev"),
            # git プロトコル
            ("git://github.com/aokumablue/ple4-dev.git", "github.com/aokumablue/ple4-dev"),
            # パス部の大小文字は保持する
            ("https://github.com/AokumaBlue/Ple4-Dev.git", "github.com/AokumaBlue/Ple4-Dev"),
            # ホストのみ
            ("https://github.com/", "github.com"),
            # ローカルパス remote
            ("/srv/git/ple4-dev.git", "/srv/git/ple4-dev"),
            ("/srv/git/ple4-dev/", "/srv/git/ple4-dev"),
            ("/srv/git/ple4-dev", "/srv/git/ple4-dev"),
            ("file:///srv/git/ple4-dev.git", "/srv/git/ple4-dev"),
            ("FILE:///srv/git/ple4-dev/", "/srv/git/ple4-dev"),
            ("../sibling-repo", "../sibling-repo"),
            # 最初のスラッシュより後ろのコロンはホスト区切りではない
            ("/srv/git/my:repo.git", "/srv/git/my:repo"),
            # 前後空白は無視する
            ("  git@github.com:aokumablue/ple4-dev.git\n", "github.com/aokumablue/ple4-dev"),
        ],
    )
    def test_normalizes(self, raw: str, expected: str) -> None:
        """表の各表記が期待どおりの正体キーへ畳まれる。"""
        assert normalize_remote_url(raw) == expected

    def test_ssh_https_and_bare_https_collapse_to_one_key(self) -> None:
        """タスクが要求する 3 表記が同一キーになる。"""
        keys = {
            normalize_remote_url("git@github.com:aokumablue/ple4-dev.git"),
            normalize_remote_url("https://github.com/aokumablue/ple4-dev.git"),
            normalize_remote_url("https://github.com/aokumablue/ple4-dev"),
        }
        assert keys == {"github.com/aokumablue/ple4-dev"}

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

    userinfo（§7-4）に加えて query / fragment（P1-009）も落とすことを固定する。
    """

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # https + credential（監査で確認された実害の再現）
            (
                "https://user:token@github.com/o/r.git",
                "https://github.com/o/r.git",
            ),
            (
                "https://user:ghp_abcdefghijklmnopqrstuvwxyz012345@github.com/o/r",
                "https://github.com/o/r",
            ),
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
            (
                "file:///srv/git/repo.git",
                "file:///srv/git/repo.git",
            ),
            # query / fragment 内の token（P1-009。userinfo 除去だけでは残っていた）
            (
                "https://github.com/o/r?access_token=SECRET",
                "https://github.com/o/r",
            ),
            (
                "https://user:token@github.com/o/r.git?private_token=SECRET",
                "https://github.com/o/r.git",
            ),
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
            ("ple4-dev", "ple4-dev"),
            ("Ple4_Dev", "ple4-dev"),
            ("my repo (v2)", "my-repo-v2"),
            ("---weird---", "weird"),
            ("日本語", FALLBACK_SLUG),
            ("", FALLBACK_SLUG),
            ("!!!", FALLBACK_SLUG),
        ],
    )
    def test_slugify(self, name: str, expected: str) -> None:
        """英数字とハイフンだけの kebab-case になる。"""
        assert slugify(name) == expected

    def test_truncates_long_names(self) -> None:
        """長すぎる名前は MAX_SLUG_LENGTH に切り詰められる。"""
        slug = slugify("a" * 200)
        assert slug == "a" * MAX_SLUG_LENGTH

    def test_truncation_does_not_leave_trailing_hyphen(self) -> None:
        """切り詰め位置がハイフンでも末尾ハイフンを残さない。"""
        slug = slugify("a" * (MAX_SLUG_LENGTH - 1) + "-tail")
        assert slug == "a" * (MAX_SLUG_LENGTH - 1)

    def test_base_slug_uses_last_segment(self) -> None:
        """base_slug は identity_key の最終セグメントを使う。"""
        remote_identity = RepoIdentity("github.com/aokumablue/ple4-dev", "/x/y", "git@github.com:a/b.git")
        path_identity = RepoIdentity("/Users/x/dev/ple4-dev", "/Users/x/dev/ple4-dev", None)
        assert base_slug(remote_identity) == "ple4-dev"
        assert base_slug(path_identity) == "ple4-dev"

    def test_base_slug_falls_back_for_root_path(self) -> None:
        """最終セグメントが空なら FALLBACK_SLUG になる。"""
        assert base_slug(RepoIdentity("/", "/", None)) == FALLBACK_SLUG


class TestRunGit:
    """git 実行ヘルパーの異常系。"""

    def test_returns_none_on_timeout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """タイムアウトしても例外を漏らさず None を返す。"""

        def _timeout(cmd: list[str], **kwargs: object) -> object:
            raise subprocess.TimeoutExpired(cmd, repo_identity.GIT_TIMEOUT_SECONDS)

        monkeypatch.setattr(repo_identity, "run_text", _timeout)
        assert repo_identity._run_git(str(tmp_path), "rev-parse") is None

    def test_returns_none_when_git_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """git バイナリが無くても None を返す。"""

        def _missing(cmd: list[str], **kwargs: object) -> object:
            raise FileNotFoundError("git")

        monkeypatch.setattr(repo_identity, "run_text", _missing)
        assert repo_identity._run_git(str(tmp_path), "rev-parse") is None

    def test_returns_none_on_nonzero_exit(self, tmp_path: Path) -> None:
        """git リポジトリ外では None を返す。"""
        assert repo_identity._run_git(str(tmp_path), "rev-parse", "--git-common-dir") is None

    def test_returns_none_on_empty_stdout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """成功しても出力が空なら None を返す。"""

        def _empty(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 0, stdout="  \n", stderr="")

        monkeypatch.setattr(repo_identity, "run_text", _empty)
        assert repo_identity._run_git(str(tmp_path), "rev-parse") is None


class TestFindRepoRoot:
    """repo root 解決のテスト。"""

    def test_resolves_repo_root_from_subdirectory(self, tmp_path: Path) -> None:
        """サブディレクトリから呼んでもルートに解決される。"""
        root = _make_repo(tmp_path / "main")
        nested = root / "a" / "b"
        nested.mkdir(parents=True)
        assert find_repo_root(nested) == os.path.realpath(root)

    def test_worktree_resolves_to_main_repo(self, tmp_path: Path) -> None:
        """git worktree 配下から呼ぶと本体リポジトリのルートになる。"""
        root = _make_repo(tmp_path / "main")
        worktree = tmp_path / "wt"
        _git("worktree", "add", "-b", "feature", str(worktree), cwd=root)
        assert find_repo_root(worktree) == os.path.realpath(root)

    def test_bare_repo_uses_git_dir_itself(self, tmp_path: Path) -> None:
        """bare リポジトリでは git dir 自身をルートとして扱う。"""
        bare = tmp_path / "bare.git"
        bare.mkdir()
        _git("init", "--bare", cwd=bare)
        assert find_repo_root(bare) == os.path.realpath(bare)

    def test_non_repo_falls_back_to_cwd(self, tmp_path: Path) -> None:
        """git リポジトリでなければ cwd 自身の絶対パスを返す。"""
        plain = tmp_path / "plain"
        plain.mkdir()
        assert find_repo_root(plain) == os.path.realpath(plain)

    def test_defaults_to_process_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """cwd 省略時はプロセスの作業ディレクトリを使う。"""
        root = _make_repo(tmp_path / "main")
        monkeypatch.chdir(root)
        assert find_repo_root() == os.path.realpath(root)

    def test_timeout_falls_back_to_absolute_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """git がタイムアウトしても絶対パスへ分岐してハングしない。"""
        root = _make_repo(tmp_path / "main")

        def _timeout(cmd: list[str], **kwargs: object) -> object:
            raise subprocess.TimeoutExpired(cmd, repo_identity.GIT_TIMEOUT_SECONDS)

        monkeypatch.setattr(repo_identity, "run_text", _timeout)
        assert find_repo_root(root) == os.path.realpath(root)


class TestDetectRepoIdentity:
    """identity_key 確定のテスト。"""

    def test_uses_normalized_remote(self, tmp_path: Path) -> None:
        """remote があれば正規化 remote が identity_key になる。remote_url は
        userinfo（scp 形式の固定ユーザー名 ``git@``）を除去した値になる
        （§7-4 対応）。"""
        root = _make_repo(tmp_path / "main", remote="git@github.com:aokumablue/ple4-dev.git")
        identity = detect_repo_identity(root)
        assert identity.identity_key == "github.com/aokumablue/ple4-dev"
        assert identity.remote_url == "github.com:aokumablue/ple4-dev.git"
        assert identity.root_path == os.path.realpath(root)

    def test_strips_credential_userinfo_from_remote_url(self, tmp_path: Path) -> None:
        """https の `user:token@` credential は identity_key だけでなく
        remote_url からも除去される（§7-4 対応、監査で確認された実害）。"""
        root = _make_repo(
            tmp_path / "main",
            remote="https://user:ghp_abcdefghijklmnopqrstuvwxyz012345@github.com/o/r.git",
        )
        identity = detect_repo_identity(root)
        assert identity.identity_key == "github.com/o/r"
        assert identity.remote_url == "https://github.com/o/r.git"
        assert "ghp_" not in identity.remote_url
        assert "user" not in identity.remote_url

    def test_falls_back_to_root_path_without_remote(self, tmp_path: Path) -> None:
        """remote が無ければ repo root の絶対パスが identity_key になる。"""
        root = _make_repo(tmp_path / "main")
        identity = detect_repo_identity(root)
        assert identity.identity_key == os.path.realpath(root)
        assert identity.remote_url is None

    def test_drops_unusable_remote(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """正規化できない remote は台帳に残さずパス識別へ落とす。"""
        root = _make_repo(tmp_path / "main")
        monkeypatch.setattr(repo_identity, "read_remote_url", lambda _root: "\x00")
        identity = detect_repo_identity(root)
        assert identity.identity_key == os.path.realpath(root)
        assert identity.remote_url is None

    def test_non_repo_uses_cwd(self, tmp_path: Path) -> None:
        """git リポジトリでなければ cwd の絶対パスで識別する。"""
        plain = tmp_path / "plain"
        plain.mkdir()
        identity = detect_repo_identity(plain)
        assert identity.identity_key == os.path.realpath(plain)
        assert identity.remote_url is None

    def test_read_remote_url_returns_none_without_origin(self, tmp_path: Path) -> None:
        """origin 未設定なら read_remote_url は None を返す。"""
        root = _make_repo(tmp_path / "main")
        assert read_remote_url(str(root)) is None

    def test_worktree_shares_identity_with_main(self, tmp_path: Path) -> None:
        """worktree と本体は同一 identity_key になる。"""
        root = _make_repo(tmp_path / "main", remote="git@github.com:aokumablue/ple4-dev.git")
        worktree = tmp_path / "wt"
        _git("worktree", "add", "-b", "feature", str(worktree), cwd=root)
        assert detect_repo_identity(worktree) == detect_repo_identity(root)


class TestResolveRepo:
    """DB を絡めたスラッグ採番と upsert のテスト。"""

    def test_registers_new_repo(self, tmp_path: Path, db: Database) -> None:
        """初出リポジトリは素のスラッグで登録される。"""
        root = _make_repo(tmp_path / "ple4-dev", remote="git@github.com:aokumablue/ple4-dev.git")
        repo = resolve_repo(root, db)
        assert repo.id == "ple4-dev"
        assert repo.identity_key == "github.com/aokumablue/ple4-dev"
        assert repo.remote_url == "github.com:aokumablue/ple4-dev.git"
        assert repo.root_path == os.path.realpath(root)

    def test_reresolve_keeps_id_and_first_seen(self, tmp_path: Path, db: Database) -> None:
        """同一 identity_key の再解決は id と first_seen_at を保持する。"""
        root = _make_repo(tmp_path / "ple4-dev", remote="git@github.com:aokumablue/ple4-dev.git")
        first = resolve_repo(root, db)
        again = resolve_repo(root, db)
        assert again.id == first.id
        assert again.first_seen_at == first.first_seen_at
        assert len(db.list_repos()) == 1

    def test_worktree_does_not_create_second_row(self, tmp_path: Path, db: Database) -> None:
        """worktree から解決しても行は増えない。"""
        root = _make_repo(tmp_path / "ple4-dev", remote="git@github.com:aokumablue/ple4-dev.git")
        worktree = tmp_path / "wt"
        _git("worktree", "add", "-b", "feature", str(worktree), cwd=root)
        main_repo = resolve_repo(root, db)
        worktree_repo = resolve_repo(worktree, db)
        assert worktree_repo.id == main_repo.id
        assert worktree_repo.root_path == os.path.realpath(root)
        assert len(db.list_repos()) == 1

    def test_same_directory_name_different_remote_gets_suffix(self, tmp_path: Path, db: Database) -> None:
        """同名ディレクトリで別 remote なら -2 が付く。"""
        first_root = _make_repo(tmp_path / "one" / "app", remote="git@github.com:one/app.git")
        second_root = _make_repo(tmp_path / "two" / "app", remote="git@github.com:two/app.git")
        third_root = _make_repo(tmp_path / "three" / "app", remote="git@github.com:three/app.git")
        assert resolve_repo(first_root, db).id == "app"
        assert resolve_repo(second_root, db).id == "app-2"
        assert resolve_repo(third_root, db).id == "app-3"
        assert len(db.list_repos()) == 3

    def test_same_directory_name_no_remote_gets_suffix(self, tmp_path: Path, db: Database) -> None:
        """remote 無しの同名ディレクトリもパス識別で別行になる。"""
        first_root = _make_repo(tmp_path / "one" / "app")
        second_root = _make_repo(tmp_path / "two" / "app")
        assert resolve_repo(first_root, db).id == "app"
        assert resolve_repo(second_root, db).id == "app-2"

    def test_allocate_reuses_existing_id_for_known_identity(self, db: Database) -> None:
        """登録済み identity_key にはその id をそのまま返す。"""
        identity = RepoIdentity("github.com/one/app", "/one/app", "git@github.com:one/app.git")
        allocated = allocate_repo_slug(identity, db)
        stored = db.upsert_repo(
            Repo(
                id=allocated,
                identity_key=identity.identity_key,
                root_path=identity.root_path,
                remote_url=identity.remote_url,
            )
        )
        assert allocate_repo_slug(identity, db) == stored.id
