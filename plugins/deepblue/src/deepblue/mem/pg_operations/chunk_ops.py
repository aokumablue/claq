"""memory_chunks 関連の UPSERT / 検索メソッドを提供するミックスイン。

memory_chunks テーブルへのチャンク投入とベクトル / 全文 / チーム検索、
および chunk_id からの行取得をまとめる。接続管理は ``PgDatabase`` が
実行時に提供する ``self._get_conn() / self._put_conn()`` を利用する。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from deepblue.mem.logger import get as _get_logger
from deepblue.mem.pg_operations._helpers import _to_json

if TYPE_CHECKING:
    import psycopg

    from deepblue.mem.database import MemoryChunk

log = _get_logger("PG")


class _ChunkOpsMixin:
    """memory_chunks テーブルの操作と検索を担うミックスイン。"""

    if TYPE_CHECKING:

        def _get_conn(self) -> psycopg.Connection: ...

        def _put_conn(self, conn: psycopg.Connection) -> None: ...

    # --- memory_chunks ---

    def upsert_chunk(self, chunk: MemoryChunk, origin_user: str) -> None:
        """チャンクを UPSERT する。"""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
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
                    ),
                )
            conn.commit()
        except Exception as e:
            log.error("PostgreSQL 操作に失敗したためロールバックします: %s", e)
            conn.rollback()
            raise
        finally:
            self._put_conn(conn)

    def upsert_chunks_batch(self, chunks: list[MemoryChunk], origin_user: str) -> int:
        """チャンクをバッチで UPSERT する。"""
        if not chunks:
            return 0
        conn = self._get_conn()
        try:
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
            with conn.cursor() as cur:
                cur.executemany(
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
            conn.commit()
            count = len(params_list)
        except Exception as e:
            log.error("PostgreSQL 操作に失敗したためロールバックします: %s", e)
            conn.rollback()
            raise
        finally:
            self._put_conn(conn)
        return count

    # --- 検索メソッド ---

    def vec_search(
        self,
        embedding: list[float],
        limit: int = 20,
        *,
        exclude_origin_user: str | None = None,
    ) -> list[tuple[str, float]]:
        """pgvector を使ったベクトル近傍検索。

        Args:
            embedding: クエリベクトル
            limit: 結果件数
            exclude_origin_user: 除外する origin_user（チーム検索で自分を除く）

        Returns:
            (chunk_id, distance) のリスト（距離が小さいほど類似）
        """
        conn = self._get_conn()
        try:
            vec_str = "[" + ",".join(str(v) for v in embedding) + "]"
            with conn.cursor() as cur:
                if exclude_origin_user is not None:
                    # memory_chunks_vec には origin_user が無いので JOIN で絞り込む
                    cur.execute(
                        """SELECT v.chunk_id, v.embedding <-> %s::vector AS distance
             FROM memory_chunks_vec v
             JOIN memory_chunks c ON v.chunk_id = c.id
             WHERE c.origin_user <> %s
             ORDER BY distance
             LIMIT %s""",
                        (vec_str, exclude_origin_user, limit),
                    )
                else:
                    cur.execute(
                        """SELECT chunk_id, embedding <-> %s::vector AS distance
             FROM memory_chunks_vec
             ORDER BY distance
             LIMIT %s""",
                        (vec_str, limit),
                    )
                return [(row[0], row[1]) for row in cur.fetchall()]
        finally:
            self._put_conn(conn)

    def fts_search(
        self,
        query: str,
        limit: int = 20,
        *,
        exclude_origin_user: str | None = None,
    ) -> list[tuple[str, float]]:
        """pg_trgm を使った全文類似検索。

        Args:
            query: 検索クエリ
            limit: 結果件数
            exclude_origin_user: 除外する origin_user（チーム検索で自分を除く）

        Returns:
            (chunk_id, similarity) のリスト（類似度が高いほど関連）
        """
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                if exclude_origin_user is not None:
                    cur.execute(
                        """SELECT id, similarity(content, %s) AS sim
             FROM memory_chunks
             WHERE content %% %s AND origin_user <> %s
             ORDER BY sim DESC
             LIMIT %s""",
                        (query, query, exclude_origin_user, limit),
                    )
                else:
                    cur.execute(
                        """SELECT id, similarity(content, %s) AS sim
             FROM memory_chunks
             WHERE content %% %s
             ORDER BY sim DESC
             LIMIT %s""",
                        (query, query, limit),
                    )
                return [(row[0], row[1]) for row in cur.fetchall()]
        finally:
            self._put_conn(conn)

    def team_search(
        self,
        query: str,
        embedding: list[float],
        limit: int = 20,
        *,
        exclude_origin_user: str | None = None,
    ) -> list[tuple[str, float]]:
        """FTS + ベクトル検索を RRF で統合したチーム横断検索。

        Args:
            query: 検索テキスト
            embedding: クエリのエンベディング
            limit: 結果件数
            exclude_origin_user: 除外する origin_user（自分を除外してチームの経験だけを返す）

        Returns:
            (chunk_id, rrf_score) のリスト
        """
        fts_results = self.fts_search(query, limit=limit * 2, exclude_origin_user=exclude_origin_user)
        vec_results = self.vec_search(embedding, limit=limit * 2, exclude_origin_user=exclude_origin_user)

        # RRF 統合（距離→類似度に変換してランク統合）
        k = 60
        scores: dict[str, float] = {}
        for rank, (chunk_id, _) in enumerate(fts_results):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
        for rank, (chunk_id, _) in enumerate(vec_results):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return ranked[:limit]

    def fetch_chunks_by_ids(self, chunk_ids: list[str]) -> dict[str, dict]:
        """chunk_id 群に対応する memory_chunks の行を一括取得する。

        Args:
            chunk_ids: 取得対象の chunk_id リスト

        Returns:
            chunk_id → 行の辞書（キーは id, origin_user, content, user_prompt, project,
            created_at_epoch, tool_names, files_read, files_modified）。
            空入力の場合は空の辞書を返す。
        """
        import json as _json

        if not chunk_ids:
            return {}

        placeholders = ",".join(["%s"] * len(chunk_ids))
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""SELECT id, origin_user, content, user_prompt, project,
                 created_at_epoch, tool_names, files_read, files_modified
           FROM memory_chunks WHERE id IN ({placeholders})""",
                    list(chunk_ids),
                )
                rows = cur.fetchall()
        finally:
            self._put_conn(conn)

        def _parse_list(val: object) -> list[str]:
            """list か JSON 文字列を文字列リストに正規化する（失敗時は空リスト）。"""
            if isinstance(val, list):
                return [str(x) for x in val]
            if isinstance(val, str) and val:
                try:
                    parsed = _json.loads(val)
                    return [str(x) for x in parsed] if isinstance(parsed, list) else []
                except (ValueError, TypeError):
                    return []
            return []

        result: dict[str, dict] = {}
        for row in rows:
            cid = str(row[0])
            result[cid] = {
                "id": cid,
                "origin_user": row[1] or "",
                "content": row[2] or "",
                "user_prompt": row[3] or "",
                "project": row[4] or "",
                "created_at_epoch": int(row[5]) if row[5] is not None else 0,
                "tool_names": _parse_list(row[6]),
                "files_read": _parse_list(row[7]),
                "files_modified": _parse_list(row[8]),
            }
        return result
