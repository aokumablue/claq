"""PostgreSQL 共有メモリへのバッチ書き込み Mixin。

`PgDatabase`（pg_database.py）が継承して各テーブルへの UPSERT/INSERT
メソッドを提供する。接続の取得・返却（`_get_conn` / `_put_conn`）は
継承先の `PgDatabase` が実装しており、本 Mixin はそれらを `self` 経由で
呼び出す（実行時には多重継承先に実在する）。

import 方向は pg_database.py → 本モジュールの一方向のみ。本モジュールは
`PgDatabase` を import しない（循環 import を避けるため）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bluecore.mem.database import (
    Adr,
    EventLog,
    Instinct,
    InteractionLog,
    MemItemRun,
    MemoryChunk,
    ProjectProfile,
    Session,
    SessionDigest,
)

if TYPE_CHECKING:
    import psycopg


class PgWriteMixin:
    """共有メモリ各テーブルへのバッチ書き込みメソッド群を提供する Mixin。

    接続管理メソッド（`_get_conn` / `_put_conn`）は継承先の `PgDatabase`
    が実装する。以下の型スタブは静的型チェッカー向けの宣言であり、実行時
    には存在しない（`PgDatabase` の実装が MRO で解決される）。
    """

    if TYPE_CHECKING:

        def _get_conn(self, *, for_write: bool = True) -> psycopg.Connection: ...

        def _put_conn(self, conn: psycopg.Connection) -> None: ...

    def _executemany_batch(self, sql: str, params_list: list[tuple]) -> int:
        """`sql` を `params_list` でバッチ実行し、コミットして件数を返す。

        `params_list` が空なら接続を取得せず 0 を返す。例外時は rollback して
        再送出し、finally で必ず接続を返却する。`_get_conn` はデフォルトの
        `for_write=True`（WRITE 経路・RLS identity 適用）で呼ぶ。全バッチ
        書き込みメソッドはこのヘルパに委譲し、トランザクション境界と戻り値を
        統一する。
        """
        if not params_list:
            return 0
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.executemany(sql, params_list)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put_conn(conn)
        return len(params_list)

    # --- memory_chunks ---

    def upsert_chunk(self, chunk: MemoryChunk, origin_user: str) -> None:
        """チャンクを UPSERT する。"""
        self.upsert_chunks_batch([chunk], origin_user)

    def upsert_chunks_batch(self, chunks: list[MemoryChunk], origin_user: str) -> int:
        """チャンクをバッチで UPSERT する。"""
        params_list = [
            (
                str(chunk.id),
                origin_user,
                chunk.session_id,
                chunk.project,
                chunk.chunk_index,
                chunk.content,
                _to_json(chunk.tool_names),
                _to_json(chunk.files_read),
                _to_json(chunk.files_modified),
                chunk.user_prompt,
                chunk.created_at_epoch,
                chunk.access_count,
                chunk.last_accessed_epoch,
                chunk.merged_generation,
                str(chunk.merged_into) if chunk.merged_into else None,
            )
            for chunk in chunks
        ]
        return self._executemany_batch(
            """INSERT INTO memory_chunks
             (id, origin_user, session_id, project, chunk_index, content,
              tool_names, files_read, files_modified, user_prompt,
              created_at_epoch, access_count, last_accessed_epoch,
              merged_generation, merged_into, synced_at)
             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
             ON CONFLICT (origin_user, session_id, chunk_index) DO UPDATE SET
                content = EXCLUDED.content,
                tool_names = EXCLUDED.tool_names,
                files_read = EXCLUDED.files_read,
               files_modified = EXCLUDED.files_modified,
               user_prompt = EXCLUDED.user_prompt,
               access_count = EXCLUDED.access_count,
               last_accessed_epoch = EXCLUDED.last_accessed_epoch,
               merged_generation = EXCLUDED.merged_generation,
               merged_into = EXCLUDED.merged_into,
               synced_at = NOW()""",
            params_list,
        )

    # --- sessions ---

    def upsert_session(self, session: Session, origin_user: str) -> None:
        """セッションを UPSERT する。"""
        self.upsert_sessions_batch([session], origin_user)

    def upsert_sessions_batch(self, sessions: list[Session], origin_user: str) -> int:
        """セッションをバッチで UPSERT する。"""
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
        return self._executemany_batch(
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

    # --- instincts ---

    def upsert_instinct(self, instinct: Instinct) -> None:
        """インスティンクトを UPSERT する。"""
        self.upsert_instincts_batch([instinct])

    def upsert_instincts_batch(self, instincts: list[Instinct]) -> int:
        """インスティンクトをバッチで UPSERT する。"""
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
        return self._executemany_batch(
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

    # --- adrs ---

    def upsert_adr(self, adr: Adr) -> None:
        """ADR を UPSERT する。"""
        self.upsert_adrs_batch([adr])

    def upsert_adrs_batch(self, adrs: list[Adr]) -> int:
        """ADR をバッチで UPSERT する。"""
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
        return self._executemany_batch(
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

    # --- event_logs ---

    def insert_event_log(self, event: EventLog) -> None:
        """イベントログを INSERT する（重複は無視）。"""
        self.insert_event_logs_batch([event])

    def insert_event_logs_batch(self, events: list[EventLog]) -> int:
        """イベントログをバッチで INSERT する。"""
        params_list = [
            (
                event.id,
                event.origin_user,
                event.event_type,
                event.project_id,
                event.content,
                event.created_at_epoch,
            )
            for event in events
        ]
        return self._executemany_batch(
            """INSERT INTO event_logs
             (id, origin_user, event_type, project_id, content, created_at_epoch, synced_at)
             VALUES (%s, %s, %s, %s, %s, %s, NOW())
             ON CONFLICT (id) DO NOTHING""",
            params_list,
        )

    # --- embeddings (memory_chunks_vec) ---

    def upsert_embeddings_batch(self, embeddings: list[tuple[str, list[float]]]) -> int:
        """エンベディングをバッチで UPSERT する。

        Args:
            embeddings: (chunk_id, embedding_vector) のリスト

        Returns:
            UPSERT した件数
        """
        # pgvector 形式に変換: [0.1, 0.2, ...] → '[0.1,0.2,...]'
        params_list = [
            (chunk_id, "[" + ",".join(str(v) for v in vec) + "]")
            for chunk_id, vec in embeddings
        ]
        return self._executemany_batch(
            """INSERT INTO memory_chunks_vec (chunk_id, embedding)
               VALUES (%s, %s::vector)
               ON CONFLICT (chunk_id) DO UPDATE SET
                 embedding = EXCLUDED.embedding""",
            params_list,
        )

    # --- interaction_logs ---

    def upsert_interaction_logs_batch(self, logs: list[InteractionLog]) -> int:
        """インタラクションログをバッチで UPSERT する。"""
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
        return self._executemany_batch(
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

    # --- project_profiles ---

    def upsert_project_profiles_batch(self, profiles: list[ProjectProfile]) -> int:
        """プロジェクトプロファイルをバッチで UPSERT する。"""
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
        return self._executemany_batch(
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

    # --- mem_item_runs ---

    def upsert_mem_item_runs_batch(self, runs: list[MemItemRun]) -> int:
        """アイテム実行記録をバッチで UPSERT する。"""
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
        return self._executemany_batch(
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

    # --- session_digests ---

    def upsert_session_digests_batch(self, digests: list[SessionDigest], origin_user: str) -> int:
        """セッション要約をバッチで UPSERT する。"""
        params_list = [
            (
                digest.id,
                origin_user,
                digest.session_id,
                digest.project,
                digest.summary,
                _to_json(digest.key_files),
                _to_json(digest.key_decisions),
                digest.outcome,
                digest.harness,
                digest.source,
                digest.chunk_count,
                digest.started_at_epoch,
                digest.ended_at_epoch,
                digest.created_at_epoch,
            )
            for digest in digests
        ]
        return self._executemany_batch(
            """INSERT INTO session_digests
             (id, origin_user, session_id, project, summary,
              key_files, key_decisions, outcome, harness, source,
              chunk_count, started_at_epoch, ended_at_epoch, created_at_epoch, synced_at)
             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
             ON CONFLICT (origin_user, session_id) DO UPDATE SET
               project = EXCLUDED.project,
               summary = EXCLUDED.summary,
               key_files = EXCLUDED.key_files,
               key_decisions = EXCLUDED.key_decisions,
               outcome = EXCLUDED.outcome,
               harness = EXCLUDED.harness,
               source = EXCLUDED.source,
               chunk_count = EXCLUDED.chunk_count,
               started_at_epoch = EXCLUDED.started_at_epoch,
               ended_at_epoch = EXCLUDED.ended_at_epoch,
               created_at_epoch = EXCLUDED.created_at_epoch,
               synced_at = NOW()""",
            params_list,
        )


def _to_json(val: list | dict | None) -> str | None:
    """リストや辞書を JSON 文字列に変換。"""
    if val is None:
        return None
    import json

    return json.dumps(val, ensure_ascii=False)
