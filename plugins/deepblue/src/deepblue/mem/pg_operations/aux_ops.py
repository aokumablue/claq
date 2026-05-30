"""embeddings / interaction_logs / project_profiles / mem_item_runs ミックスイン。

補助テーブルへのバッチ UPSERT メソッドをまとめる。接続管理は
``PgDatabase`` が実行時に提供する ``self._get_conn() / self._put_conn()``
を利用し、JSON 変換は :mod:`._helpers` の ``_to_json`` を用いる。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from deepblue.mem.logger import get as _get_logger
from deepblue.mem.pg_operations._helpers import _to_json

if TYPE_CHECKING:
    import psycopg

    from deepblue.mem.database import InteractionLog, MemItemRun, ProjectProfile

log = _get_logger("PG")


class _AuxOpsMixin:
    """補助テーブル（embeddings 等）のバッチ UPSERT を担うミックスイン。"""

    if TYPE_CHECKING:

        def _get_conn(self) -> psycopg.Connection: ...

        def _put_conn(self, conn: psycopg.Connection) -> None: ...

    # --- embeddings (memory_chunks_vec) ---

    def upsert_embeddings_batch(self, embeddings: list[tuple[str, list[float]]]) -> int:
        """エンベディングをバッチで UPSERT する。

        Args:
            embeddings: (chunk_id, embedding_vector) のリスト

        Returns:
            UPSERT した件数
        """
        if not embeddings:
            return 0
        conn = self._get_conn()
        try:
            # pgvector 形式に変換: [0.1, 0.2, ...] → '[0.1,0.2,...]'
            params_list = [
                (chunk_id, "[" + ",".join(str(v) for v in vec) + "]")
                for chunk_id, vec in embeddings
            ]
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO memory_chunks_vec (chunk_id, embedding)
               VALUES (%s, %s::vector)
               ON CONFLICT (chunk_id) DO UPDATE SET
                 embedding = EXCLUDED.embedding""",
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

    # --- interaction_logs ---

    def upsert_interaction_logs_batch(self, logs: list[InteractionLog]) -> int:
        """インタラクションログをバッチで UPSERT する。"""
        if not logs:
            return 0
        conn = self._get_conn()
        try:
            params_list = [
                (
                    entry.id,
                    entry.origin_user,
                    entry.session_id,
                    entry.project,
                    entry.user_prompt_full,
                    entry.user_prompt_hash,
                    entry.ai_response_summary,
                    entry.ai_response_tool_plan,
                    entry.chunk_id,
                    entry.execution_outcome,
                    entry.tool_error_count,
                    entry.interaction_index,
                    entry.created_at_epoch,
                )
                for entry in logs
            ]
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO interaction_logs
             (id, origin_user, session_id, project,
              user_prompt_full, user_prompt_hash,
              ai_response_summary, ai_response_tool_plan,
              chunk_id, execution_outcome, tool_error_count,
              interaction_index, created_at_epoch, synced_at)
             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
             ON CONFLICT (origin_user, session_id, interaction_index) DO UPDATE SET
               ai_response_summary = EXCLUDED.ai_response_summary,
               ai_response_tool_plan = EXCLUDED.ai_response_tool_plan,
               execution_outcome = EXCLUDED.execution_outcome,
               tool_error_count = EXCLUDED.tool_error_count,
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

    # --- project_profiles ---

    def upsert_project_profiles_batch(self, profiles: list[ProjectProfile]) -> int:
        """プロジェクトプロファイルをバッチで UPSERT する。"""
        if not profiles:
            return 0
        conn = self._get_conn()
        try:
            params_list = [
                (
                    profile.id,
                    profile.origin_user,
                    profile.project,
                    profile.project_path,
                    _to_json(profile.languages),
                    _to_json(profile.frameworks),
                    profile.primary_language,
                    profile.test_command,
                    profile.build_command,
                    profile.scope_hint,
                    profile.detected_at_epoch,
                    profile.last_updated_epoch,
                    profile.detection_confidence,
                )
                for profile in profiles
            ]
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO project_profiles
             (id, origin_user, project, project_path,
              languages, frameworks, primary_language,
              test_command, build_command, scope_hint,
              detected_at_epoch, last_updated_epoch, detection_confidence, synced_at)
             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
             ON CONFLICT (origin_user, project) DO UPDATE SET
               project_path = EXCLUDED.project_path,
               languages = EXCLUDED.languages,
               frameworks = EXCLUDED.frameworks,
               primary_language = EXCLUDED.primary_language,
               test_command = EXCLUDED.test_command,
               build_command = EXCLUDED.build_command,
               scope_hint = EXCLUDED.scope_hint,
               last_updated_epoch = EXCLUDED.last_updated_epoch,
               detection_confidence = EXCLUDED.detection_confidence,
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

    # --- mem_item_runs ---

    def upsert_mem_item_runs_batch(self, runs: list[MemItemRun]) -> int:
        """アイテム実行記録をバッチで UPSERT する。"""
        if not runs:
            return 0
        conn = self._get_conn()
        try:
            params_list = [
                (
                    run.id,
                    run.origin_user,
                    run.session_id,
                    run.project,
                    run.skill_name,
                    run.skill_trigger,
                    run.outcome,
                    _to_json(run.tools_used),
                    run.files_modified_count,
                    run.duration_seconds,
                    run.interaction_log_id,
                    run.created_at_epoch,
                    run.item_type,
                )
                for run in runs
            ]
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO mem_item_runs
             (id, origin_user, session_id, project,
              skill_name, skill_trigger, outcome,
              tools_used, files_modified_count, duration_seconds,
              interaction_log_id, created_at_epoch, item_type, synced_at)
             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
             ON CONFLICT (id) DO UPDATE SET
               item_type = EXCLUDED.item_type,
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
