"""SQLite データベース管理 — repos / sessions / knowledge の 3 テーブル。"""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path
from types import TracebackType

from claq.lib.core_utils import ensure_private_dir
from claq.mem.models import Knowledge, Repo, Session, utc_now_iso
from claq.mem.schema import SCHEMA_SQL


class DatabaseError(Exception):
    """mem.db のパス検証に失敗したことを示す。"""


def _reject_non_regular_file(path: Path) -> None:
    """既存パスが symlink または regular file 以外なら接続前に拒否する。

    ``sqlite3.connect`` は symlink を追うため、``mem.db -> other.db`` を張られた
    状態で開くとリンク先へスキーマを作ってしまう。``lstat`` はリンク自身を見る
    ので、追う前に検出できる。

    Args:
        path: 検証対象のパス。

    Raises:
        DatabaseError: symlink または regular file 以外の場合。
    """
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise DatabaseError(
            f"{path} は symlink です。mem.db を symlink 経由で開くと、"
            "リンク先の DB へ書き込んでしまうため拒否します。"
        )
    if not stat.S_ISREG(info.st_mode):
        raise DatabaseError(f"{path} は通常ファイルではありません。mem.db として開けません。")


class Database:
    """``~/.claq/mem.db`` を扱う永続メモリストア。

    ``with Database(path) as db:`` で開くと、ブロック終了時に自動で close する。
    """

    def __init__(self, db_path: str | Path) -> None:
        """DB へ接続し、スキーマを初期化する。

        新規作成は ``os.open`` の O_CREAT|O_EXCL で最初から 0600 にする
        （sqlite3.connect に作らせて後から chmod すると、その間だけ他ユーザーに
        読める窓ができる）。既存 DB も接続のたびに mode を検証して補正する
        （新規作成時だけだと既存の 0644 DB が放置される）。Windows の ``chmod`` は
        読み取り専用属性しか動かさず補正は空振りするが害は無い。Windows の
        アクセス制御は ``core_utils.ensure_private_dir`` の判断に委ねる。
        WAL/SHM sidecar は最初の書き込みで遅延作成されるため、ここでは補正
        しない。親ディレクトリが 0700 なので他ユーザーから到達できない。

        既存パスは接続前に ``lstat`` で symlink と非 regular file を拒否する。
        ``O_EXCL`` はぶら下がり symlink にも ``FileExistsError`` を投げるため、
        この検査が無いと ``sqlite3.connect`` と後続の chmod がリンクを追い、
        無関係な DB を壊しうる。lstat と connect の間の差し替え（TOCTOU）は
        防がない。同一 UID に対する真正性は保証対象外で、ここで防ぐのは
        誤設定・復元事故による取り違えである。

        Args:
            db_path: mem.db のパス。親ディレクトリが無ければ作成する。

        Raises:
            DatabaseError: 既存パスが symlink または regular file 以外の場合。
        """
        path = Path(db_path)
        ensure_private_dir(path.parent)
        try:
            os.close(os.open(str(path), os.O_CREAT | os.O_EXCL, 0o600))
        except FileExistsError:
            _reject_non_regular_file(path)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            path.chmod(0o600)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()

    def close(self) -> None:
        """DB 接続を閉じる。"""
        self.conn.close()

    def __enter__(self) -> Database:
        """コンテキストマネージャとして自身を返す。

        Returns:
            この Database インスタンス。
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """コンテキスト終了時に DB 接続を閉じる。

        Args:
            exc_type: 送出された例外の型。無ければ None。
            exc: 送出された例外。無ければ None。
            tb: 例外のトレースバック。無ければ None。
        """
        self.close()

    # --- repos ---

    def upsert_repo(self, repo: Repo) -> Repo:
        """リポジトリ台帳を挿入または更新する。

        衝突解決は ``identity_key`` で行う。既登録なら ``id`` と
        ``first_seen_at`` を保持したまま観測情報だけを更新する。

        Args:
            repo: 登録したいリポジトリ。

        Returns:
            DB に格納された最新状態の Repo。
        """
        row = self.conn.execute(
            """INSERT INTO repos
                 (id, identity_key, root_path, remote_url, first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(identity_key) DO UPDATE SET
                 root_path = excluded.root_path,
                 remote_url = excluded.remote_url,
                 last_seen_at = excluded.last_seen_at
               RETURNING *""",
            (
                repo.id,
                repo.identity_key,
                repo.root_path,
                repo.remote_url,
                repo.first_seen_at,
                repo.last_seen_at,
            ),
        ).fetchone()
        self.conn.commit()
        return Repo.from_row(row)

    def relink_repo_identity(self, repo_id: str, identity_key: str) -> None:
        """既存リポジトリ行を新しい ``identity_key`` へ載せ替える。

        remote の追加・変更・削除で ``identity_key`` が変わっても、同一
        リポジトリなら ``repos.id`` を引き継ぐために使う。``upsert_repo`` では
        代用できない —— 旧 ``id`` と新 ``identity_key`` の組で INSERT すると、
        ``ON CONFLICT(identity_key)`` は識別子違いで発火せず、``id``
        （TEXT PRIMARY KEY）側の衝突が ``IntegrityError`` になるため。先に
        ``identity_key`` を書き換えておけば、後続の ``upsert_repo`` が
        ``identity_key`` 衝突として正しく UPDATE へ落ちる。

        Args:
            repo_id: 載せ替える既存行の ``repos.id``。
            identity_key: 新しい正体キー。
        """
        self.conn.execute("UPDATE repos SET identity_key = ? WHERE id = ?", (identity_key, repo_id))
        self.conn.commit()

    def list_repos(self) -> list[Repo]:
        """登録済みリポジトリを最終観測の新しい順に返す。

        Returns:
            Repo のリスト。1 件も無ければ空リスト。
        """
        rows = self.conn.execute("SELECT * FROM repos ORDER BY last_seen_at DESC, id").fetchall()
        return [Repo.from_row(row) for row in rows]

    # --- knowledge ---

    def upsert_knowledge(self, knowledge: Knowledge) -> None:
        """知識カードを挿入または更新する。

        衝突解決は式インデックス ``(COALESCE(repo_id,''), key)`` で行う。
        既存行の ``id`` と ``created_at`` は保持する。

        ``status`` は衝突時も ``excluded.status`` で上書きする。**これは意図的で
        あり、ここへ「既存 status を維持する」ガードを足してはならない** ——
        ``cmd_knowledge.handle_forget`` が ``archived`` を書き込むのに本メソッドを使って
        おり、維持ガードを入れると forget が黙って効かなくなる。

        「人間が promote した active カードを agent の再 learn が pending へ
        戻す」問題は、本メソッドではなく ``cmd_knowledge.handle_learn`` で塞ぐ。
        DB 層は汎用の upsert のままにし、誰の書込みを拒むかという判断は
        経路ごとに持たせる。

        Args:
            knowledge: 登録したい知識カード。
        """
        self.conn.execute(
            """INSERT INTO knowledge
                 (key, scope, repo_id, kind, title, body, domain, confidence,
                  status, source, source_ref, superseded_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(COALESCE(repo_id, ''), key) DO UPDATE SET
                 scope = excluded.scope,
                 kind = excluded.kind,
                 title = excluded.title,
                 body = excluded.body,
                 domain = excluded.domain,
                 confidence = excluded.confidence,
                 status = excluded.status,
                 source = excluded.source,
                 source_ref = excluded.source_ref,
                 superseded_by = excluded.superseded_by,
                 updated_at = excluded.updated_at""",
            (
                knowledge.key,
                knowledge.scope,
                knowledge.repo_id,
                knowledge.kind,
                knowledge.title,
                knowledge.body,
                knowledge.domain,
                knowledge.confidence,
                knowledge.status,
                knowledge.source,
                knowledge.source_ref,
                knowledge.superseded_by,
                knowledge.created_at,
                knowledge.updated_at,
            ),
        )
        self.conn.commit()

    def get_knowledge_by_key(self, key: str, repo_id: str | None = None) -> Knowledge | None:
        """``key`` と所属リポジトリで知識カードを 1 件取得する。

        Args:
            key: kebab-case スラッグ。
            repo_id: repo スコープの所属リポジトリ。global スコープは None。

        Returns:
            該当する Knowledge。存在しなければ None。
        """
        row = self.conn.execute(
            "SELECT * FROM knowledge WHERE COALESCE(repo_id, '') = COALESCE(?, '') AND key = ?",
            (repo_id, key),
        ).fetchone()
        return Knowledge.from_row(row) if row else None

    def list_knowledge(self, scope: str, status: str, repo_id: str | None = None) -> list[Knowledge]:
        """スコープと status が一致する知識カードを更新の新しい順に返す。

        Args:
            scope: ``global`` / ``repo``。
            status: ``active`` / ``pending`` / ``archived``。
            repo_id: 所属リポジトリ。None なら絞り込まない（global スコープの行は repo_id を持たない）。

        Returns:
            Knowledge のリスト。該当が無ければ空リスト。
        """
        sql = "SELECT * FROM knowledge WHERE scope = ? AND status = ?"
        params = [scope, status]
        if repo_id is not None:
            sql += " AND repo_id = ?"
            params.append(repo_id)
        rows = self.conn.execute(f"{sql} ORDER BY updated_at DESC, id DESC", params).fetchall()
        return [Knowledge.from_row(row) for row in rows]

    def promote_knowledge(self, knowledge_id: int) -> None:
        """知識カードを ``active`` にし、``updated_at`` を現在時刻にする。

        Args:
            knowledge_id: 対象の ``knowledge.id``。
        """
        self.conn.execute(
            "UPDATE knowledge SET status = 'active', updated_at = ? WHERE id = ?",
            (utc_now_iso(), knowledge_id),
        )
        self.conn.commit()

    # --- sessions ---

    def start_session(self, session: Session) -> None:
        """セッションを開始登録する。

        同一 ``session_uid`` の再入は既存行を書き換えない（``started_at`` は初回の値のまま）。

        Args:
            session: 開始するセッション。
        """
        self.conn.execute(
            """INSERT INTO sessions
                 (session_uid, repo_id, handoff, started_at, ended_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(session_uid) DO NOTHING""",
            (
                session.session_uid,
                session.repo_id,
                session.handoff,
                session.started_at,
                session.ended_at,
            ),
        )
        self.conn.commit()

    def finish_session(self, session_uid: str, handoff: str, ended_at: str) -> bool:
        """セッションに引き継ぎと終了時刻を記録する。

        Args:
            session_uid: ハーネスが渡した session_id。
            handoff: 次セッションへの引き継ぎ（人間可読の散文）。
            ended_at: 終了時刻（ISO8601）。

        Returns:
            対象セッションが存在して更新できた場合 True、該当なしなら False。
        """
        cursor = self.conn.execute(
            "UPDATE sessions SET handoff = ?, ended_at = ? WHERE session_uid = ?",
            (handoff, ended_at, session_uid),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def get_latest_session(self, repo_id: str) -> Session | None:
        """引き継ぎを持つセッションのうち、終了が最も新しい 1 件を返す。

        ``handoff`` が空の行は次セッションへ渡すものが無いため除外する。並行セッションでは
        開始順と終了順が食い違い、早く始まって遅く終わったセッションの引き継ぎが最新の作業を
        表すため、終了時刻で選ぶ（注入の見出しに出す時刻も終了時刻）。

        Args:
            repo_id: 対象リポジトリの ``repos.id``。

        Returns:
            該当する Session。存在しなければ None。
        """
        row = self.conn.execute(
            """SELECT * FROM sessions
               WHERE repo_id = ? AND handoff != ''
               ORDER BY ended_at DESC, id DESC
               LIMIT 1""",
            (repo_id,),
        ).fetchone()
        return Session.from_row(row) if row else None
