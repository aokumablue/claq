"""learn の観測ログ保存先 — ``repos`` 台帳の id をディレクトリ名に使う。

旧実装は ``sha256(remote or repo root)[:12]`` を ``~/.bluecore/projects/<hash>/``
のディレクトリ名にし、``~/.bluecore/projects.json`` に別台帳を持っていた。
リポジトリ識別が ``repos`` テーブルと二重管理になるため、保存先も
``repos.id``（人間可読スラッグ）配下の ``~/.bluecore/repos/<repo-id>/`` へ寄せる。
これで識別系統は ``repos`` の 1 本だけになり、``projects.json`` は不要になる。

このディレクトリに置くのは **観測の生ログだけ**。観測から抽出した知識は
``knowledge`` テーブルが唯一の正であり、ファイルには一切書かない。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from bluecore.mem.database import Database
from bluecore.mem.repo_identity import resolve_repo
from bluecore.mem.settings import Settings

REPOS_DIR_NAME = "repos"
"""観測ログを置くルートディレクトリ名（``~/.bluecore/repos``）。"""

OBSERVATIONS_FILE_NAME = "observations.jsonl"
"""リポジトリごとの観測ログファイル名。"""

ARCHIVE_DIR_NAME = "observations.archive"
"""解析済み・肥大化した観測ログの退避先ディレクトリ名。"""


@dataclass(frozen=True)
class ObservationTarget:
    """観測ログの書き込み先を確定した文脈情報。

    Attributes:
        repo_id: ``repos.id``（人間可読スラッグ）。表示名も兼ねる。
        repo_root: リポジトリルートの絶対パス。
        storage_dir: ``~/.bluecore/repos/<repo_id>``。
        observations_file: 観測ログ JSONL のパス。
    """

    repo_id: str
    repo_root: Path
    storage_dir: Path
    observations_file: Path

    @property
    def archive_dir(self) -> Path:
        """観測ログのアーカイブディレクトリを返す。

        Returns:
            ``storage_dir/observations.archive`` のパス。
        """
        return self.storage_dir / ARCHIVE_DIR_NAME


def repos_root() -> Path:
    """観測ログのルートディレクトリを返す。

    ``Settings`` 経由で解決するため ``BLUECORE_DATA_PATH`` による隔離が効く。

    Returns:
        ``<data_path>/repos`` のパス。
    """
    return Settings().data_path / REPOS_DIR_NAME


def observation_target(repo_id: str, repo_root: Path) -> ObservationTarget:
    """解決済みの ``repos.id`` から観測ログの保存先を組み立てる。

    Args:
        repo_id: ``repos.id``。
        repo_root: リポジトリルートの絶対パス。

    Returns:
        組み立てた ObservationTarget。ディレクトリの作成は行わない。
    """
    storage_dir = repos_root() / repo_id
    return ObservationTarget(
        repo_id=repo_id,
        repo_root=repo_root,
        storage_dir=storage_dir,
        observations_file=storage_dir / OBSERVATIONS_FILE_NAME,
    )


def resolve_observation_target(cwd: str | Path | None = None) -> ObservationTarget:
    """*cwd* のリポジトリを ``repos`` 台帳で解決し保存先を返す。

    ``resolve_repo`` が ``repos`` 行を作る（または更新する）ため、観測が
    始まった時点でリポジトリは台帳に載る。knowledge の外部キーもこれで満たされる。

    Args:
        cwd: 起点ディレクトリ。None なら現在の作業ディレクトリ。

    Returns:
        解決した ObservationTarget。
    """
    with Database(Settings().db_path) as db:
        repo = resolve_repo(cwd, db)
    return observation_target(repo.id, Path(repo.root_path))


def ensure_storage_dirs(target: ObservationTarget) -> None:
    """観測ログとアーカイブの保存先ディレクトリを作成する。

    Args:
        target: 作成対象の保存先。
    """
    target.archive_dir.mkdir(parents=True, exist_ok=True)
