"""リポジトリごとの生ログ保存先 — ``repos`` 台帳の id をディレクトリ名に使う。

旧実装は ``sha256(remote or repo root)[:12]`` を ``~/.bluecore/projects/<hash>/``
のディレクトリ名にし、``~/.bluecore/projects.json`` に別台帳を持っていた。
リポジトリ識別が ``repos`` テーブルと二重管理になるため、保存先も
``repos.id``（人間可読スラッグ）配下の ``~/.bluecore/repos/<repo-id>/`` へ寄せる。
これで識別系統は ``repos`` の 1 本だけになり、``projects.json`` は不要になる。

このディレクトリに置くのは **生ログだけ**（learn の観測ログ、loop-dev の反復
テレメトリ）。生ログから抽出した知識は ``knowledge`` テーブルが唯一の正であり、
逆に生ログを ``knowledge`` へ入れることもしない。DB には知識だけを置く。

生ログはどれも「JSONL へ 1 行追記 → 10MB でアーカイブへローテーション →
アーカイブは 30 日で削除」という同じ運用に乗るため、その機構は ``JsonlLog``
に 1 つだけ実装し、観測ログもテレメトリも同じ実装を共有する。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from bluecore.mem.database import Database
from bluecore.mem.repo_identity import resolve_repo
from bluecore.mem.settings import Settings

REPOS_DIR_NAME = "repos"
"""生ログを置くルートディレクトリ名（``~/.bluecore/repos``）。"""

OBSERVATIONS_FILE_NAME = "observations.jsonl"
"""リポジトリごとの観測ログファイル名。"""

ARCHIVE_DIR_NAME = "observations.archive"
"""解析済み・肥大化した観測ログの退避先ディレクトリ名。"""

LOOP_TELEMETRY_FILE_NAME = "loop-dev.jsonl"
"""リポジトリごとの loop-dev 反復テレメトリのファイル名。"""

LOOP_TELEMETRY_ARCHIVE_DIR_NAME = "loop-dev.archive"
"""肥大化した loop-dev テレメトリの退避先ディレクトリ名。"""

MAX_LOG_BYTES = 10 * 1024 * 1024
"""この閾値を超えた生ログはアーカイブへローテーションする。"""

ARCHIVE_RETENTION_SECONDS = 30 * 24 * 60 * 60
"""アーカイブ済みログを保持する秒数（30 日）。"""

PURGE_INTERVAL_SECONDS = 24 * 60 * 60
"""古いアーカイブの削除を試みる最短間隔（1 日）。"""


@dataclass(frozen=True)
class JsonlLog:
    """追記専用 JSONL 生ログ 1 本と、そのローテーション運用をまとめた値オブジェクト。

    Attributes:
        path: JSONL 本体のパス。ファイル名の stem がログの識別名を兼ねる。
        archive_dir: ローテーション先ディレクトリ。
    """

    path: Path
    archive_dir: Path

    @property
    def name(self) -> str:
        """ログの識別名（ファイル名から拡張子を除いたもの）を返す。

        Returns:
            アーカイブ名・purge マーカー名の接頭辞に使う文字列。
        """
        return self.path.stem

    @property
    def purge_marker(self) -> Path:
        """purge の最終実行時刻を保持するマーカーファイルのパスを返す。

        ログごとに別ファイルにする。共有すると片方の purge がもう片方の
        purge を 1 日抑止してしまう。

        Returns:
            ``<log の親ディレクトリ>/.last-purge-<name>`` のパス。
        """
        return self.path.parent / f".last-purge-{self.name}"

    def ensure_dirs(self) -> None:
        """ログ本体の親ディレクトリとアーカイブディレクトリを作成する。"""
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, payload: dict) -> None:
        """ペイロードを JSONL の 1 行としてアトミックに追記する。

        ``O_APPEND`` を立てた単一の ``write(2)`` で書くため、複数プロセスが
        同時に追記しても行が混ざらない（改行込みで 1 回の write に収める）。

        Args:
            payload: JSON 化して書き込む 1 レコード。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)

    def rotate_if_too_large(self) -> None:
        """ログが ``MAX_LOG_BYTES`` を超えていればアーカイブへ退避する。"""
        try:
            if not self.path.exists() or self.path.stat().st_size < MAX_LOG_BYTES:
                return
        except OSError:
            return

        self.archive_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        archive_path = self.archive_dir / f"{self.name}-{stamp}-{os.getpid()}.jsonl"
        try:
            self.path.replace(archive_path)
        except OSError:
            pass

    def purge_old_archives(self) -> None:
        """1 日 1 回、``ARCHIVE_RETENTION_SECONDS`` より古いアーカイブを削除する。"""
        marker = self.purge_marker
        try:
            if not marker.exists():
                stale = True
            else:
                age = datetime.now(UTC).timestamp() - marker.stat().st_mtime
                stale = age > PURGE_INTERVAL_SECONDS
        except OSError:
            stale = True

        if not stale:
            return

        self.archive_dir.mkdir(parents=True, exist_ok=True)
        cutoff = datetime.now(UTC).timestamp() - ARCHIVE_RETENTION_SECONDS
        try:
            archived = list(self.archive_dir.glob(f"{self.name}-*.jsonl"))
        except OSError:
            archived = []
        for path in archived:
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue

        try:
            marker.touch()
        except OSError:
            pass

    def maintain(self) -> None:
        """追記前の定常メンテ（古いアーカイブの削除とローテーション）を行う。"""
        self.purge_old_archives()
        self.rotate_if_too_large()

    def read_records(self) -> list[dict]:
        """アーカイブを含む全レコードを古い順に読み出す。

        JSON として解析できない行、および JSON オブジェクトでない行は捨てる。

        Returns:
            レコードの一覧。ファイルが無ければ空リスト。
        """
        try:
            files = sorted(self.archive_dir.glob(f"{self.name}-*.jsonl"))
        except OSError:  # pragma: no cover - glob は存在しないディレクトリでも空を返す
            files = []
        files.append(self.path)

        records: list[dict] = []
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    records.append(parsed)
        return records


@dataclass(frozen=True)
class ObservationTarget:
    """リポジトリごとの生ログ書き込み先を確定した文脈情報。

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

    @property
    def observations_log(self) -> JsonlLog:
        """観測ログを ``JsonlLog`` として返す。

        Returns:
            ``observations.jsonl`` を指す JsonlLog。
        """
        return JsonlLog(self.observations_file, self.archive_dir)

    @property
    def loop_telemetry_log(self) -> JsonlLog:
        """loop-dev 反復テレメトリを ``JsonlLog`` として返す。

        Returns:
            ``loop-dev.jsonl`` を指す JsonlLog。
        """
        return JsonlLog(
            self.storage_dir / LOOP_TELEMETRY_FILE_NAME,
            self.storage_dir / LOOP_TELEMETRY_ARCHIVE_DIR_NAME,
        )


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
    target.observations_log.ensure_dirs()
