"""SQLite データベース管理 — repos / sessions / knowledge の 3 テーブル。"""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path
from types import TracebackType

from claq.lib.core_utils import ensure_private_dir
from claq.mem.models import Knowledge, Repo, Session, utc_now_iso
from claq.mem.schema import _SCHEMA_SQL


class DatabaseError(Exception):
    """mem.db のパス検証に失敗したことを示す。"""


def _reject_non_regular_file(path: Path) -> None:
    """既存パスが symlink または regular file 以外なら接続前に拒否する。

    ``sqlite3.connect`` は symlink を追うため、``mem.db -> other.db`` を張られた
    状態で開くとリンク先へスキーマを作ってしまう。``lstat`` はリンク自身を見る
    ので、追う前に検出できる。

    Args:
        path: 検証対象のパス。

    Returns:
        何も返しません。

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
        """DB へ接続し、スキーマ初期化と最適化 PRAGMA を適用する。

        新規作成時は ``os.open`` の O_CREAT|O_EXCL で最初から 0600 で
        作る（sqlite3.connect に作らせて後から chmod すると、作成直後
        から chmod までの間だけ他ユーザーに読めるファイルが存在する
        窓ができるため）。既存 DB（他ツールが作った・過去バージョンが
        作った等で 0600 以外の mode を持つ場合を含む）も、接続のたびに
        mode を検証して補正する（F-08 対応。以前は新規作成時にしか
        補正しておらず、既存の 0644 DB は開き直しても放置されていた）。

        この mode 補正は POSIX でのみ収束する。Windows の ``chmod`` は
        読み取り専用属性しか動かさず ``st_mode`` は 0o666 のままなので、
        検証は毎回不一致となり ``chmod`` が空振りする（害は無く、読み取り
        専用属性の解除だけが起きる）。Windows でのアクセス制御を
        ``%USERPROFILE%`` の既定 ACL に委ねる判断とその理由は
        `core_utils.ensure_private_dir` の docstring に集約してある
        （release-verify 2026-09-03 の P1-008）。

        WAL/SHM sidecar は本メソッドの時点ではまだ存在しない（SQLite が
        WAL モードで最初の書き込み時に遅延作成するため、ここで chmod
        しても no-op）。親ディレクトリは ``ensure_private_dir`` が
        無条件に 0700 へ揃えるため、sidecar が 0644 でも他ユーザーから
        到達できない。

        既存パスは接続前に ``lstat`` で symlink と非 regular file を拒否する
        （F-25 対応）。``O_EXCL`` はぶら下がり symlink に対しても
        ``FileExistsError`` を投げるため、以前は生きたリンクもぶら下がりリンクも
        同じ分岐へ落ち、``sqlite3.connect`` がリンクを追ってリンク先へ
        ``repos``/``sessions``/``knowledge`` を作っていた。二次被害として
        直後の ``path.stat()`` もリンクを追うため、0600 の chmod がリンク先へ
        着弾していた。``~/.claq`` は 0700 に締められるので権限昇格ではないが、
        復元事故や誤設定で無関係な DB を壊しうる完全性の問題である。

        この検査は TOCTOU を完全には防がない（lstat と connect の間に差し替え
        られる余地は残る）。同一 UID に対する真正性は docs/adr/plugin-root-resolution.md のとおり保証
        対象外であり、ここで防ぐのは誤設定・復元事故による取り違えである。

        Args:
            db_path: mem.db のパス。親ディレクトリが無ければ作成する。

        Raises:
            DatabaseError: 既存パスが symlink または regular file 以外の場合。
        """
        path = Path(db_path)
        ensure_private_dir(path.parent)
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        except FileExistsError:
            _reject_non_regular_file(path)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            path.chmod(0o600)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()
        for pragma in (
            "PRAGMA temp_store = MEMORY",
            "PRAGMA mmap_size = 268435456",
            "PRAGMA cache_size = -64000",
        ):
            self.conn.execute(pragma)

    def _init_schema(self) -> None:
        """repos → sessions → knowledge の順にスキーマを作成する。"""
        self.conn.executescript(_SCHEMA_SQL)
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

    def _returning(self, sql: str, params: tuple[object, ...]) -> sqlite3.Row:
        """変更系 SQL を実行してコミットし、RETURNING の先頭行を返す。

        Args:
            sql: ``RETURNING *`` を含む SQL。
            params: プレースホルダに渡す値。

        Returns:
            先頭の ``sqlite3.Row``。
        """
        row = self.conn.execute(sql, params).fetchone()
        self.conn.commit()
        return row

    def _updated(self, sql: str, params: tuple[object, ...]) -> bool:
        """UPDATE を実行してコミットし、1 行以上更新できたかを返す。

        Args:
            sql: UPDATE 文。
            params: プレースホルダに渡す値。

        Returns:
            対象行があれば True、無ければ False。
        """
        cursor = self.conn.execute(sql, params)
        self.conn.commit()
        return cursor.rowcount > 0

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
        row = self._returning(
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
        )
        return Repo.from_row(row)

    def relink_repo_identity(self, repo_id: str, identity_key: str) -> bool:
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

        Returns:
            対象行が存在して更新できた場合 True、該当なしなら False。
        """
        return self._updated(
            "UPDATE repos SET identity_key = ? WHERE id = ?",
            (identity_key, repo_id),
        )

    def list_repos(self) -> list[Repo]:
        """登録済みリポジトリを最終観測の新しい順に返す。

        Returns:
            Repo のリスト。1 件も無ければ空リスト。
        """
        rows = self.conn.execute("SELECT * FROM repos ORDER BY last_seen_at DESC, id").fetchall()
        return [Repo.from_row(row) for row in rows]

    # --- knowledge ---

    def upsert_knowledge(self, knowledge: Knowledge) -> Knowledge:
        """知識カードを挿入または更新する。

        衝突解決は式インデックス ``(COALESCE(repo_id,''), key)`` で行う。
        既存行の ``id`` と ``created_at`` は保持する。

        ``status`` は衝突時も ``excluded.status`` で上書きする。**これは意図的で
        あり、ここへ「既存 status を維持する」ガードを足してはならない** ——
        ``cli._handle_forget`` が ``archived`` を書き込むのに本メソッドを使って
        おり、維持ガードを入れると forget が黙って効かなくなる。

        「人間が promote した active カードを agent の再 learn が pending へ
        戻す」問題（H-3）は、本メソッドではなく ``cli._handle_learn`` で塞ぐ。
        DB 層は汎用の upsert のままにし、誰の書込みを拒むかという判断は
        経路ごとに持たせる。

        Args:
            knowledge: 登録したい知識カード。

        Returns:
            DB に格納された最新状態の Knowledge。
        """
        row = self._returning(
            """INSERT INTO knowledge
                 (key, scope, repo_id, kind, title, body, domain, confidence,
                  status, source, source_ref, session_id, superseded_by,
                  created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                 session_id = excluded.session_id,
                 superseded_by = excluded.superseded_by,
                 updated_at = excluded.updated_at
               RETURNING *""",
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
                knowledge.session_id,
                knowledge.superseded_by,
                knowledge.created_at,
                knowledge.updated_at,
            ),
        )
        return Knowledge.from_row(row)

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

    def list_knowledge(
        self,
        scope: str | None = None,
        repo_id: str | None = None,
        status: str | None = None,
    ) -> list[Knowledge]:
        """条件に一致する知識カードを更新の新しい順に返す。

        Args:
            scope: ``global`` / ``repo``。None なら絞り込まない。
            repo_id: 所属リポジトリ。None なら絞り込まない。
            status: ``active`` / ``pending`` / ``archived``。None なら絞り込まない。

        Returns:
            Knowledge のリスト。該当が無ければ空リスト。
        """
        clauses: list[str] = []
        params: list[str] = []
        for column, value in (("scope", scope), ("repo_id", repo_id), ("status", status)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM knowledge{where} ORDER BY updated_at DESC, id DESC",
            params,
        ).fetchall()
        return [Knowledge.from_row(row) for row in rows]

    def set_knowledge_status(self, knowledge_id: int, status: str, updated_at: str | None = None) -> bool:
        """知識カードの status を更新する。

        Args:
            knowledge_id: 対象の ``knowledge.id``。
            status: ``active`` / ``pending`` / ``archived``。
            updated_at: 更新時刻（ISO8601）。省略時は現在時刻。

        Returns:
            対象行が存在して更新できた場合 True、該当なしなら False。
        """
        return self._updated(
            "UPDATE knowledge SET status = ?, updated_at = ? WHERE id = ?",
            (status, updated_at or utc_now_iso(), knowledge_id),
        )

    # --- sessions ---

    def start_session(self, session: Session) -> Session:
        """セッションを開始登録する。

        同一 ``session_uid`` の再入は ``harness`` の更新のみ行い、
        ``started_at`` は初回の値を保持する。

        Args:
            session: 開始するセッション。

        Returns:
            DB に格納された最新状態の Session。
        """
        row = self._returning(
            """INSERT INTO sessions
                 (session_uid, repo_id, harness, handoff, started_at, ended_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_uid) DO UPDATE SET
                 harness = excluded.harness
               RETURNING *""",
            (
                session.session_uid,
                session.repo_id,
                session.harness,
                session.handoff,
                session.started_at,
                session.ended_at,
            ),
        )
        return Session.from_row(row)

    def finish_session(self, session_uid: str, handoff: str, ended_at: str | None = None) -> bool:
        """セッションに引き継ぎと終了時刻を記録する。

        Args:
            session_uid: ハーネスが渡した session_id。
            handoff: 次セッションへの引き継ぎ（人間可読の散文）。
            ended_at: 終了時刻（ISO8601）。省略時は現在時刻。

        Returns:
            対象セッションが存在して更新できた場合 True、該当なしなら False。
        """
        return self._updated(
            "UPDATE sessions SET handoff = ?, ended_at = ? WHERE session_uid = ?",
            (handoff, ended_at or utc_now_iso(), session_uid),
        )

    def get_latest_session(self, repo_id: str) -> Session | None:
        """引き継ぎを持つ最新セッションを 1 件返す。

        ``handoff`` が空の行は次セッションへ渡すものが無いため除外する。

        Args:
            repo_id: 対象リポジトリの ``repos.id``。

        Returns:
            該当する Session。存在しなければ None。
        """
        row = self.conn.execute(
            """SELECT * FROM sessions
               WHERE repo_id = ? AND handoff != ''
               ORDER BY started_at DESC, id DESC
               LIMIT 1""",
            (repo_id,),
        ).fetchone()
        return Session.from_row(row) if row else None
