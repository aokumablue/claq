"""リポジトリの正体キー解決 — ``repos`` テーブルへの唯一の入口。

リポジトリ識別は本モジュールの 1 系統に統一する（識別方法が複数あると
同じリポジトリを突合できない）:

* ``identity_key`` — 正規化した remote URL。remote が無ければ repo root の絶対パス。
* ``id`` — 人間可読 kebab-case スラッグ。別リポジトリと衝突したときだけ ``-2`` を付す。

``git worktree`` 配下で起動しても ``--git-common-dir`` 経由で本体へ寄せるため、
本体と worktree は同一の ``identity_key`` になる。git 呼び出しはすべて
``GIT_TIMEOUT_SECONDS`` のハードタイムアウト付きで、git 不在・タイムアウト・
リポジトリ外のいずれでも絶対パスによる識別へ分岐してハングしない。
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from claq.lib.subprocess_utils import run_text
from claq.mem.database import Database
from claq.mem.models import Repo, utc_now_iso

GIT_TIMEOUT_SECONDS = 3.0
"""git サブプロセスのハードタイムアウト秒数。"""

MAX_SLUG_LENGTH = 64
"""``repos.id`` スラッグの最大長。"""

FALLBACK_SLUG = "repo"
"""スラッグ化した結果が空になったときに使う名前。"""

_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*)://(.*)$", re.DOTALL)
_PORT_RE = re.compile(r":\d+$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_NON_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _run_git(cwd: str, *args: str) -> str | None:
    """git コマンドをハードタイムアウト付きで実行し標準出力を返す。

    Args:
        cwd: ``git -C`` に渡す作業ディレクトリ。
        *args: ``git`` に続けて渡す引数。

    Returns:
        成功時は strip 済みの標準出力。終了コードが非 0・出力が空・
        タイムアウト・git 不在のいずれでも None。
    """
    try:
        result = run_text(["git", "-C", cwd, *args], timeout=GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _trim_git_suffix(path: str, *, keep_leading_slash: bool) -> str:
    """パスから末尾 ``.git`` と前後（または末尾だけ）のスラッシュを除去する。

    Args:
        path: ホスト名より後ろのパス、またはローカルパス表記の remote。
        keep_leading_slash: 先頭スラッシュを絶対パスの一部として残すなら True。

    Returns:
        整理したパス文字列。空になることがある。
    """
    strip = str.rstrip if keep_leading_slash else str.strip
    cleaned = strip(path, "/")
    if cleaned.lower().endswith(".git"):
        cleaned = cleaned[:-4]
    return strip(cleaned, "/")


def _is_scp_like(remote: str) -> bool:
    """scp 形式（``user@host:owner/repo``）か。最初のコロンがスラッシュより前に来る。

    Args:
        remote: scheme を持たない remote 表記。

    Returns:
        scp 形式なら True。
    """
    colon = remote.find(":")
    slash = remote.find("/")
    return colon != -1 and (slash == -1 or colon < slash)


def _join_host_path(authority: str, path: str, *, strip_port: bool) -> str | None:
    """authority とパスを ``host/path`` 形式の正体キーへ組み立てる。

    Args:
        authority: ``user:token@host:port`` 形式を取りうる authority 部。
        path: ホスト名より後ろのパス文字列。
        strip_port: 末尾の ``:数字`` をポートとして落とすなら True。
            scp 形式（``git@host:owner/repo``）では ``:`` 以降がパスなので False。

    Returns:
        正規化済みの ``host/path``。host もパスも空なら None。
    """
    host = authority.rpartition("@")[2]
    if strip_port:
        host = _PORT_RE.sub("", host)
    host = host.lower()
    cleaned = _trim_git_suffix(path, keep_leading_slash=False)
    if not cleaned:
        return host or None
    return f"{host}/{cleaned}"


def _strip_credentials(remote_url: str) -> str:
    """remote URL から credential を運びうる部分（userinfo・query・fragment）を除去する。

    DB へ保存する ``repos.remote_url`` に credential 付き authority
    （``https://user:token@github.com/o/r``）を残さないため、identity_key と
    同じ ``rpartition("@")`` を scheme・パス・大小文字を保ったまま適用する。
    scp 形式も一貫して ``host:owner/repo`` にする。

    query（``?``）と fragment（``#``）は先に現れた方から丸ごと捨てる。
    ``?access_token=SECRET`` は userinfo の除去だけでは残り、git の remote URL
    において query / fragment は取得先を特定しない。鍵名の列挙方式は新しい
    鍵名を取りこぼすため採らない。

    Args:
        remote_url: 生の remote URL（strip 済み・非空を想定）。

    Returns:
        userinfo・query・fragment を除去した remote URL。
    """
    trimmed = re.split(r"[?#]", remote_url, maxsplit=1)[0]
    scheme_match = _SCHEME_RE.match(trimmed)
    if scheme_match:
        authority, sep, path = scheme_match.group(2).partition("/")
        return f"{scheme_match.group(1)}://{authority.rpartition('@')[2]}{sep}{path}"
    if _is_scp_like(trimmed):
        authority, sep, path = trimmed.partition(":")
        return f"{authority.rpartition('@')[2]}{sep}{path}"
    return trimmed


def normalize_remote_url(remote_url: str | None) -> str | None:
    """remote URL を表記ゆれを吸収した正体キーへ正規化する。

    scheme・認証情報・ポート・末尾 ``.git``・末尾スラッシュを取り除き、
    ホスト名を小文字化する。これにより ``git@github.com:owner/repo.git`` /
    ``ssh://git@GitHub.com:22/owner/repo.git`` / ``https://github.com/owner/repo.git`` /
    ``https://user:token@github.com/owner/repo/`` はすべて ``github.com/owner/repo``
    へ畳まれる。パス部の大小文字は情報として保持する。
    ``file://`` を含むローカルパス remote は絶対パス表記のまま返す。

    Args:
        remote_url: ``git remote get-url origin`` の生出力。None や空文字も許容する。

    Returns:
        正規化済みの正体キー文字列。remote が無い・制御文字を含む・
        正規化した結果が空になる場合は None。
    """
    if not remote_url:
        return None
    raw = remote_url.strip()
    if not raw or _CONTROL_RE.search(raw):
        return None

    scheme_match = _SCHEME_RE.match(raw)
    if scheme_match:
        scheme, rest = scheme_match.group(1).lower(), scheme_match.group(2)
        if scheme == "file":
            return _trim_git_suffix(rest, keep_leading_slash=True) or None
        authority, _, path = rest.partition("/")
        return _join_host_path(authority, path, strip_port=True)
    if _is_scp_like(raw):
        authority, _, path = raw.partition(":")
        return _join_host_path(authority, path, strip_port=False)
    return _trim_git_suffix(raw, keep_leading_slash=True) or None


@dataclass(frozen=True)
class RepoIdentity:
    """DB へ書く前に確定したリポジトリの正体情報。

    Attributes:
        identity_key: 正規化 remote URL。remote が無ければ repo root 絶対パス。
        root_path: シンボリックリンク解決済みの絶対パス（worktree は本体へ寄せる）。
        remote_url: userinfo（`user:token@`）除去済みの remote URL。remote が
            無い・使えない場合は None（`_strip_credentials` 適用済み）。
    """

    identity_key: str
    root_path: str
    remote_url: str | None


def find_repo_root(cwd: str | Path | None = None) -> str:
    """*cwd* が属する git リポジトリ本体のルート絶対パスを返す。

    ``git rev-parse --path-format=absolute --git-common-dir`` を使うため、
    ``git worktree`` 配下で呼び出しても本体リポジトリのルートへ解決される。
    git が使えない・リポジトリ外・タイムアウトの場合は *cwd* 自身を返す。

    Args:
        cwd: 起点ディレクトリ。None なら現在の作業ディレクトリ。

    Returns:
        シンボリックリンク解決済みの絶対パス。
    """
    start = os.path.realpath(str(cwd) if cwd is not None else Path.cwd())
    common_dir = _run_git(start, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common_dir is None:
        return start
    resolved = os.path.realpath(common_dir)
    if os.path.basename(resolved) == ".git":
        return os.path.dirname(resolved)
    return resolved


def read_remote_url(root_path: str) -> str | None:
    """*root_path* の ``origin`` remote URL を取得する。

    Args:
        root_path: git リポジトリのルート絶対パス。

    Returns:
        生の remote URL。origin 未設定・git 実行失敗・タイムアウト時は None。
    """
    return _run_git(root_path, "remote", "get-url", "origin")


def detect_repo_identity(cwd: str | Path | None = None) -> RepoIdentity:
    """*cwd* から identity_key / root_path / remote_url を確定する。

    remote が取得でき正規化にも成功した場合のみ remote 由来の identity_key を使う。
    それ以外は repo root の絶対パスを identity_key とし、remote_url は None にする
    （使えない remote を台帳に残さない）。保存する remote_url には
    `_strip_credentials` を適用する。

    Args:
        cwd: 起点ディレクトリ。None なら現在の作業ディレクトリ。

    Returns:
        確定した RepoIdentity。
    """
    root_path = find_repo_root(cwd)
    remote_url = read_remote_url(root_path)
    normalized = normalize_remote_url(remote_url)
    if normalized is None:
        return RepoIdentity(identity_key=root_path, root_path=root_path, remote_url=None)
    # normalize_remote_url は falsy な remote_url に None を返すので、ここでは str。
    return RepoIdentity(identity_key=normalized, root_path=root_path, remote_url=_strip_credentials(remote_url))


def slugify(name: str) -> str:
    """任意の名前を SQLite の識別子として安全な kebab-case スラッグへ変換する。

    Args:
        name: リポジトリ名またはディレクトリ名。

    Returns:
        ``[a-z0-9-]`` のみからなる ``MAX_SLUG_LENGTH`` 以内の文字列。
        変換結果が空になる場合は ``FALLBACK_SLUG``。
    """
    slug = _NON_SLUG_RE.sub("-", name.lower()).strip("-")
    if len(slug) > MAX_SLUG_LENGTH:
        slug = slug[:MAX_SLUG_LENGTH].strip("-")
    return slug or FALLBACK_SLUG


def base_slug(identity: RepoIdentity) -> str:
    """衝突回避前の素のスラッグを作る。

    ``identity_key`` の最終セグメントを使う。remote 由来なら
    ``github.com/owner/repo`` の ``repo``、パス由来なら repo root の
    ディレクトリ名がそのまま最終セグメントになる。

    Args:
        identity: 確定済みの正体情報。

    Returns:
        スラッグ化した文字列。
    """
    return slugify(identity.identity_key.rsplit("/", 1)[-1])


def allocate_repo_slug(identity: RepoIdentity, db: Database) -> str:
    """既存 ``repos`` 行を見て ``repos.id`` を決める。

    決定順は次の 3 段。

    1. ``identity_key`` 一致 — その ``id`` をそのまま再利用する。
    2. ``identity_key`` 不一致でも ``root_path`` 一致 — 同一リポジトリの
       **identity 移行**とみなし、既存行を新しい ``identity_key`` へ載せ替えて
       ``id`` を引き継ぐ。
    3. どちらも無い — 新規スラッグを採番し、別リポジトリが同じスラッグを
       占有している場合のみ ``-2`` ``-3`` … を付す。

    2 が無いと、remote 無しで登録済みのリポジトリに後から
    ``git remote add origin`` した瞬間に ``identity_key`` が repo root パスから
    ``github.com/o/r`` へ変わり、別リポジトリとして ``-2`` 付きの新 ``id`` を
    採番してしまう。その結果、旧 ``id`` 配下の ``scope='repo'`` 知識が
    **全件 SessionStart 注入から静かに消える**（DB には残るが到達不能。
    実測で再現済み）。remote の変更（GitHub から GitLab への移行等）や削除でも
    同じことが起きる。

    判定順は重要で、1 を先に見ることで ``UNIQUE(identity_key)`` 違反を
    到達不能にしている。目的の ``identity_key`` を既に持つ行があれば 1 で
    返ってしまい、2 の載せ替えには来ないため。

    受容するトレードオフ: 同じパスへ別プロジェクトを clone し直した場合、
    ``root_path`` 一致だけを見る本判定は 2 つを同一リポジトリとして繋いでしまい、
    旧プロジェクトの知識が新プロジェクトへ引き継がれる。両者を区別する手段は
    無く（区別できるのは identity_key だけで、それが変わったことこそが本件の
    前提）、頻度は「remote を後から足す」より低い一方、被害は「無関係な知識が
    混じる」に留まり、塞がない場合の「知識が全件到達不能になる」より軽い。
    ``root_path`` は ``find_repo_root`` が解決した絶対パス（リポジトリ外では
    cwd 自身）なので、無関係なディレクトリ同士が衝突することはない。

    Args:
        identity: 確定済みの正体情報。
        db: 参照する mem データベース。

    Returns:
        このリポジトリに割り当てる ``repos.id``。
    """
    taken: set[str] = set()
    path_match: Repo | None = None
    for repo in db.list_repos():
        if repo.identity_key == identity.identity_key:
            return repo.id
        if path_match is None and repo.root_path == identity.root_path:
            path_match = repo
        taken.add(repo.id)

    if path_match is not None:
        db.relink_repo_identity(path_match.id, identity.identity_key)
        return path_match.id

    base = base_slug(identity)
    candidate, suffix = base, 1
    while candidate in taken:
        suffix += 1
        candidate = f"{base}-{suffix}"
    return candidate


def resolve_repo(cwd: str | Path | None, db: Database) -> Repo:
    """*cwd* のリポジトリを解決し ``repos`` へ登録して最新状態を返す。

    Args:
        cwd: 起点ディレクトリ。None なら現在の作業ディレクトリ。
        db: 登録先の mem データベース。

    Returns:
        DB に格納された最新状態の Repo。初出なら新規行、既知なら ``id`` と
        ``first_seen_at`` を保持したまま観測情報を更新した行。
    """
    identity = detect_repo_identity(cwd)
    now = utc_now_iso()
    return db.upsert_repo(
        Repo(
            id=allocate_repo_slug(identity, db),
            identity_key=identity.identity_key,
            root_path=identity.root_path,
            remote_url=identity.remote_url,
            first_seen_at=now,
            last_seen_at=now,
        )
    )
