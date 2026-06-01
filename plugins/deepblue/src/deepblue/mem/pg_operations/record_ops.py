"""sessions / instincts / adrs / event_logs の UPSERT・INSERT ミックスイン。

単体およびバッチでのレコード投入メソッドをまとめる。接続管理は
``PgDatabase`` が実行時に提供する ``self._get_conn() / self._put_conn()``
を利用する。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from deepblue.mem.logger import get as _get_logger

if TYPE_CHECKING:
    import psycopg

    from deepblue.mem.database import Adr, EventLog, Instinct, Session

log = _get_logger("PG")


class _RecordOpsMixin:
    """sessions / instincts / adrs / event_logs の投入を担うミックスイン。"""

    if TYPE_CHECKING:

        def _get_conn(self) -> psycopg.Connection: ...

        def _put_conn(self, conn: psycopg.Connection) -> None: ...

    # --- sessions ---

    def upsert_session(self, session: Session, origin_user: str) -> None:
        """セッションを UPSERT する。"""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO sessions
           (id, origin_user, session_id, project, started_at_epoch, chunk_count,
            branch, commit_hash, uncommitted_count, ended_at_epoch, project_profile_id, synced_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
           ON CONFLICT (origin_user, session_id) DO UPDATE SET
              chunk_count = EXCLUDED.chunk_count,
              branch = EXCLUDED.branch,
              commit_hash = EXCLUDED.commit_hash,
             uncommitted_count = EXCLUDED.uncommitted_count,
             ended_at_epoch = EXCLUDED.ended_at_epoch,
             project_profile_id = EXCLUDED.project_profile_id,
             synced_at = NOW()""",
                     (
                         str(session.id),
                         origin_user,
                         session.session_id,
                         session.project,
                         session.started_at_epoch,
                        session.chunk_count,
                        session.branch,
                        session.commit_hash,
                        session.uncommitted_count,
                        session.ended_at_epoch,
                        session.project_profile_id,
                    ),
                )
            conn.commit()
        except Exception as e:
            log.error("PostgreSQL 操作に失敗したためロールバックします: %s", e)
            conn.rollback()
            raise
        finally:
            self._put_conn(conn)

    def upsert_sessions_batch(self, sessions: list[Session], origin_user: str) -> int:
        """セッションをバッチで UPSERT する。"""
        if not sessions:
            return 0
        conn = self._get_conn()
        try:
            params_list = [
                (
                    str(session.id),
                    origin_user,
                    session.session_id,
                    session.project,
                    session.started_at_epoch,
                    session.chunk_count,
                    session.branch,
                    session.commit_hash,
                    session.uncommitted_count,
                    session.ended_at_epoch,
                    session.project_profile_id,
                )
                for session in sessions
            ]
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO sessions
             (id, origin_user, session_id, project, started_at_epoch, chunk_count,
              branch, commit_hash, uncommitted_count, ended_at_epoch, project_profile_id, synced_at)
             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
             ON CONFLICT (origin_user, session_id) DO UPDATE SET
                chunk_count = EXCLUDED.chunk_count,
                branch = EXCLUDED.branch,
                commit_hash = EXCLUDED.commit_hash,
               uncommitted_count = EXCLUDED.uncommitted_count,
               ended_at_epoch = EXCLUDED.ended_at_epoch,
               project_profile_id = EXCLUDED.project_profile_id,
               synced_at = NOW()""",
                    params_list,
                )
            conn.commit()
            count = len(params_list)
        except Exception as e:
            log.error("PostgreSQL 操作に失敗したためロールバックします: %s", e)
            conn.rollback()
            raise
        finally:
            self._put_conn(conn)
        return count

    # --- instincts ---

    def upsert_instinct(self, instinct: Instinct) -> None:
        """インスティンクトを UPSERT する。"""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO instincts
           (id, origin_user, instinct_id, scope, project_id, trigger_text,
            confidence, domain, content, created_at_epoch, updated_at_epoch, synced_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
           ON CONFLICT (origin_user, instinct_id, scope, COALESCE(project_id, '')) DO UPDATE SET
              trigger_text = EXCLUDED.trigger_text,
              confidence = EXCLUDED.confidence,
              domain = EXCLUDED.domain,
              content = EXCLUDED.content,
              updated_at_epoch = EXCLUDED.updated_at_epoch,
              synced_at = NOW()""",
                    (
                        instinct.id,
                        instinct.origin_user,
                        instinct.instinct_id,
                        instinct.scope,
                        instinct.project_id,
                        instinct.trigger_text,
                        instinct.confidence,
                        instinct.domain,
                        instinct.content,
                        instinct.created_at_epoch,
                        instinct.updated_at_epoch,
                    ),
                )
            conn.commit()
        except Exception as e:
            log.error("PostgreSQL 操作に失敗したためロールバックします: %s", e)
            conn.rollback()
            raise
        finally:
            self._put_conn(conn)

    def upsert_instincts_batch(self, instincts: list[Instinct]) -> int:
        """インスティンクトをバッチで UPSERT する。"""
        if not instincts:
            return 0
        conn = self._get_conn()
        try:
            params_list = [
                (
                    inst.id,
                    inst.origin_user,
                    inst.instinct_id,
                    inst.scope,
                    inst.project_id,
                    inst.trigger_text,
                    inst.confidence,
                    inst.domain,
                    inst.content,
                    inst.created_at_epoch,
                    inst.updated_at_epoch,
                )
                for inst in instincts
            ]
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO instincts
             (id, origin_user, instinct_id, scope, project_id, trigger_text,
              confidence, domain, content, created_at_epoch, updated_at_epoch, synced_at)
             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
             ON CONFLICT (origin_user, instinct_id, scope, COALESCE(project_id, '')) DO UPDATE SET
                trigger_text = EXCLUDED.trigger_text,
                confidence = EXCLUDED.confidence,
                domain = EXCLUDED.domain,
                content = EXCLUDED.content,
                updated_at_epoch = EXCLUDED.updated_at_epoch,
                synced_at = NOW()""",
                    params_list,
                )
            conn.commit()
            count = len(params_list)
        except Exception as e:
            log.error("PostgreSQL 操作に失敗したためロールバックします: %s", e)
            conn.rollback()
            raise
        finally:
            self._put_conn(conn)
        return count

    # --- adrs ---

    def upsert_adr(self, adr: Adr) -> None:
        """ADR を UPSERT する。"""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO adrs
           (id, origin_user, project, adr_number, title, status, content,
            created_at_epoch, updated_at_epoch, synced_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
           ON CONFLICT (origin_user, project, adr_number) DO UPDATE SET
             title = EXCLUDED.title,
             status = EXCLUDED.status,
             content = EXCLUDED.content,
             updated_at_epoch = EXCLUDED.updated_at_epoch,
             synced_at = NOW()""",
                    (
                        adr.id,
                        adr.origin_user,
                        adr.project,
                        adr.adr_number,
                        adr.title,
                        adr.status,
                        adr.content,
                        adr.created_at_epoch,
                        adr.updated_at_epoch,
                    ),
                )
            conn.commit()
        except Exception as e:
            log.error("PostgreSQL 操作に失敗したためロールバックします: %s", e)
            conn.rollback()
            raise
        finally:
            self._put_conn(conn)

    def upsert_adrs_batch(self, adrs: list[Adr]) -> int:
        """ADR をバッチで UPSERT する。"""
        if not adrs:
            return 0
        conn = self._get_conn()
        try:
            params_list = [
                (
                    adr.id,
                    adr.origin_user,
                    adr.project,
                    adr.adr_number,
                    adr.title,
                    adr.status,
                    adr.content,
                    adr.created_at_epoch,
                    adr.updated_at_epoch,
                )
                for adr in adrs
            ]
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO adrs
             (id, origin_user, project, adr_number, title, status, content,
              created_at_epoch, updated_at_epoch, synced_at)
             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
             ON CONFLICT (origin_user, project, adr_number) DO UPDATE SET
               title = EXCLUDED.title,
               status = EXCLUDED.status,
               content = EXCLUDED.content,
               updated_at_epoch = EXCLUDED.updated_at_epoch,
               synced_at = NOW()""",
                    params_list,
                )
            conn.commit()
            count = len(params_list)
        except Exception as e:
            log.error("PostgreSQL 操作に失敗したためロールバックします: %s", e)
            conn.rollback()
            raise
        finally:
            self._put_conn(conn)
        return count

    # --- event_logs ---

    def insert_event_log(self, event: EventLog) -> None:
        """イベントログを INSERT する（重複は無視）。"""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO event_logs
           (id, origin_user, event_type, project_id, content, created_at_epoch, synced_at)
           VALUES (%s, %s, %s, %s, %s, %s, NOW())
           ON CONFLICT (id) DO NOTHING""",
                    (
                        event.id,
                        event.origin_user,
                        event.event_type,
                        event.project_id,
                        event.content,
                        event.created_at_epoch,
                    ),
                )
            conn.commit()
        except Exception as e:
            log.error("PostgreSQL 操作に失敗したためロールバックします: %s", e)
            conn.rollback()
            raise
        finally:
            self._put_conn(conn)

    def insert_event_logs_batch(self, events: list[EventLog]) -> int:
        """イベントログをバッチで INSERT する。"""
        if not events:
            return 0
        conn = self._get_conn()
        count = 0
        try:
            with conn.cursor() as cur:
                for event in events:
                    cur.execute(
                        """INSERT INTO event_logs
             (id, origin_user, event_type, project_id, content, created_at_epoch, synced_at)
             VALUES (%s, %s, %s, %s, %s, %s, NOW())
             ON CONFLICT (id) DO NOTHING""",
                        (
                            event.id,
                            event.origin_user,
                            event.event_type,
                            event.project_id,
                            event.content,
                            event.created_at_epoch,
                        ),
                    )
                    count += 1
            conn.commit()
        except Exception as e:
            log.error("PostgreSQL 操作に失敗したためロールバックします: %s", e)
            conn.rollback()
            raise
        finally:
            self._put_conn(conn)
        return count
