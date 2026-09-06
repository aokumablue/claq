"""リポジトリの正体キー解決 — ``repos`` テーブルへの唯一の入口。

旧実装ではリポジトリ識別が ``basename(cwd)`` / ``sha256(remote)[:12]`` /
``~/.ple4/projects/<id>`` / ``projects.json`` のキーへ分裂しており突合できなかった。
本モジュールは識別を 1 系統に統一する:

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

from ple4.lib.subprocess_utils import run_text
from ple4.mem.database import Database
from ple4.mem.models import Repo, utc_now_iso

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


def _strip_git_suffix(path: str) -> str:
    """URL のパス部から前後のスラッシュと末尾 ``.git`` を除去する。

    Args:
        path: ホスト名より後ろのパス文字列。

    Returns:
        前後スラッシュと末尾 ``.git`` を落とした文字列。
    """
    cleaned = path.strip("/")
    if cleaned.lower().endswith(".git"):
        cleaned = cleaned[:-4]
    return cleaned.strip("/")


def _normalize_path_remote(path: str) -> str:
    """ローカルパス remote から末尾スラッシュと ``.git`` を除去する。

    先頭のスラッシュは絶対パスの一部なので保持する。

    Args:
        path: ローカルパス表記の remote。

    Returns:
        末尾を整理したパス文字列。空になることがある。
    """
    cleaned = path.rstrip("/")
    if cleaned.lower().endswith(".git"):
        cleaned = cleaned[:-4]
    return cleaned.rstrip("/")


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
    cleaned = _strip_git_suffix(path)
    if not cleaned:
        return host or None
    return f"{host}/{cleaned}"


def _strip_credentials(remote_url: str) -> str:
    """remote URL から credential を運びうる部分（userinfo・query・fragment）を除去する。

    `_join_host_path` が identity_key を組み立てる際に使う
    ``authority.rpartition("@")[2]`` と同じロジックを、scheme・パス・大小文字
    を保ったまま remote_url 全体に適用する小関数です。DB へ保存する
    ``repos.remote_url`` に credential 付き authority
    （``https://user:token@github.com/o/r``）がそのまま残っていた問題
    （§7-4 対応）を解消します。

    ``https://user:token@github.com/o/r`` → ``https://github.com/o/r``。
    scp 形式（``git@host:owner/repo``）にも同じロジックを一貫して適用し
    ``host:owner/repo`` にします（``git@`` は SSH の固定ユーザー名で秘密は
    含みませんが、identity_key 側の扱いと一貫させます）。

    userinfo に加えて **query（``?``）と fragment（``#``）も丸ごと捨てます**。
    ``https://github.com/o/r?access_token=SECRET`` のような形は
    userinfo の除去だけでは残り、``repos.remote_url`` として DB とログへ
    永続化されていました（release-verify 2026-09-03 の P1-009）。git の
    remote URL において query / fragment は取得先を特定する情報を持たない
    ため、鍵名を列挙して redact するのではなく丸ごと落とす（列挙方式は
    新しい鍵名を取りこぼすうえ、鍵名自体が事前に分からない）。

    Args:
        remote_url: 生の remote URL（strip 済み・非空を想定）。

    Returns:
        userinfo・query・fragment を除去した remote URL。

    Raises:
        例外は発生しません。
    """
    trimmed = _strip_query_and_fragment(remote_url)
    scheme_match = _SCHEME_RE.match(trimmed)
    if scheme_match:
        scheme_token, rest = scheme_match.group(1), scheme_match.group(2)
        authority, sep, path = rest.partition("/")
        host = authority.rpartition("@")[2]
        return f"{scheme_token}://{host}{sep}{path}"

    colon = trimmed.find(":")
    slash = trimmed.find("/")
    # scp 形式（user@host:owner/repo）は、最初のコロンがスラッシュより前に来る。
    # normalize_remote_url の scp 判定条件と同一にする。
    if colon != -1 and (slash == -1 or colon < slash):
        authority, sep, path = trimmed.partition(":")
        host = authority.rpartition("@")[2]
        return f"{host}{sep}{path}"

    return trimmed


def _strip_query_and_fragment(remote_url: str) -> str:
    """remote URL から最初の ``?`` / ``#`` 以降を落とす。

    先に現れた方を境界にする（``...#frag?x`` のように fragment 内へ ``?`` が
    現れる形でも取りこぼさない）。

    Args:
        remote_url: 生の remote URL。

    Returns:
        query / fragment を除いた文字列。どちらも無ければ元の文字列。

    Raises:
        例外は発生しません。
    """
    cut = min(
        (index for index in (remote_url.find("?"), remote_url.find("#")) if index != -1),
        default=-1,
    )
    return remote_url if cut == -1 else remote_url[:cut]


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
            return _normalize_path_remote(rest) or None
        authority, _, path = rest.partition("/")
        return _join_host_path(authority, path, strip_port=True)

    colon = raw.find(":")
    slash = raw.find("/")
    # scp 形式（git@host:owner/repo）は、最初のコロンがスラッシュより前に来る。
    if colon != -1 and (slash == -1 or colon < slash):
        authority, _, path = raw.partition(":")
        return _join_host_path(authority, path, strip_port=False)

    return _normalize_path_remote(raw) or None


@dataclass(frozen=True)
class RepoIdentity:
    """DB へ書く前に確定したリポジトリの正体情報。

    Attributes:
        identity_key: 正規化 remote URL。remote が無ければ repo root 絶対パス。
        root_path: シンボリックリンク解決済みの絶対パス（worktree は本体へ寄せる）。
        remote_url: userinfo（`user:token@`）除去済みの remote URL。remote が
            無い・使えない場合は None（§7-4 対応。`_strip_credentials` 適用済み）。
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
    （使えない remote を台帳に残さない）。保存する remote_url は
    `_strip_userinfo` で userinfo（`user:token@`）を除去した後の文字列にする
    （§7-4 対応。identity_key の正規化とは独立に、DB へそのまま保存される
    生の remote_url にも credential 除去を適用する）。

    Args:
        cwd: 起点ディレクトリ。None なら現在の作業ディレクトリ。

    Returns:
        確定した RepoIdentity。

    Raises:
        例外は発生しません。
    """
    root_path = find_repo_root(cwd)
    remote_url = read_remote_url(root_path)
    normalized = normalize_remote_url(remote_url)
    if normalized is None:
        return RepoIdentity(identity_key=root_path, root_path=root_path, remote_url=None)
    # normalize_remote_url は remote_url が falsy なら必ず None を返す契約
    # （`if not remote_url: return None`）。ここに到達した時点で remote_url
    # は truthy な文字列であることが保証される。
    sanitized_remote_url = _strip_credentials(remote_url)
    return RepoIdentity(identity_key=normalized, root_path=root_path, remote_url=sanitized_remote_url)


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
    candidate = base
    suffix = 1
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
