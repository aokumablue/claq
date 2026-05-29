"""統合検索 — FTS5 + sqlite-vec + RRF + 時間減衰"""

from __future__ import annotations

import math
import re
import time
from typing import NamedTuple

from devgear.mem.database import Database
from devgear.mem.logger import get as _get_logger
from devgear.mem.settings import Settings

log = _get_logger("SEARCH")

# 過去参照パターン（適応的注入の判定用）
_RETROSPECTIVE_PATTERNS = [
    r"前回",
    r"以前",
    r"last time",
    r"before",
    r"どうやって",
    r"how did we",
]


class SearchResult(NamedTuple):
    chunk_id: str
    score: float
    content: str
    user_prompt: str
    project: str
    created_at_epoch: int
    tool_names: list[str]
    files_read: list[str]
    files_modified: list[str]


class SearchService:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    def search(
        self,
        query: str,
        project: str | None = None,
        limit: int = 20,
    ) -> list[SearchResult]:
        """FTS5 + ベクトル検索を RRF で統合し、時間減衰を適用して返す"""
        fetch_limit = limit * 2

        # 1. FTS5 キーワード検索
        keyword_results = self.db.fts_search(query, limit=fetch_limit)

        # 2. sqlite-vec ベクトル検索
        import devgear.mem.embedding as _emb

        query_embedding = _emb.embed_query(query, self.settings.embedding_model)
        # model.onnx 未完了時は embed_query が [] を返すためベクトル検索をスキップ
        vector_results = self.db.vec_search(query_embedding, limit=fetch_limit) if query_embedding else []

        # 3. RRF で統合
        keyword_ids = [cid for cid, _ in keyword_results]
        vector_ids = [cid for cid, _ in vector_results]
        fused = _reciprocal_rank_fusion(keyword_ids, vector_ids)

        # 4. 候補チャンクを一括取得（N+1 回避）
        candidate_ids = [cid for cid, _ in fused]
        chunks = self.db.get_chunks_by_ids(candidate_ids)

        # 5. 適応的時間減衰を適用 + プロジェクトフィルタ
        scored: list[tuple[str, float]] = []
        for chunk_id, rrf_score in fused:
            chunk = chunks.get(chunk_id)
            if chunk is None:
                continue
            if project and chunk.project != project:
                continue
            decay = adaptive_decay(
                chunk.created_at_epoch,
                chunk.last_accessed_epoch,
                chunk.access_count,
                base_half_life=self.settings.search_half_life_days,
            )
            scored.append((chunk_id, rrf_score * decay))

        # 6. スコア降順でソート → 上位 limit 件
        scored.sort(key=lambda x: x[1], reverse=True)

        # 7. 結果を構築
        results: list[SearchResult] = []
        for chunk_id, score in scored[:limit]:
            chunk = chunks[chunk_id]
            results.append(
                SearchResult(
                    chunk_id=chunk_id,
                    score=score,
                    content=chunk.content,
                    user_prompt=chunk.user_prompt,
                    project=chunk.project,
                    created_at_epoch=chunk.created_at_epoch,
                    tool_names=chunk.tool_names,
                    files_read=chunk.files_read,
                    files_modified=chunk.files_modified,
                )
            )

        # 8. アクセス追跡
        hit_ids = [r.chunk_id for r in results]
        if hit_ids:
            self.db.update_access(hit_ids)

        return results

    def search_team(
        self,
        query: str,
        limit: int = 20,
        *,
        exclude_origin_user: str | None = None,
    ) -> list[SearchResult]:
        """PostgreSQL を使ったチーム横断検索（FTS + ベクトル + RRF）。

        ``exclude_origin_user`` を指定すると、PG 側で該当ユーザの行を除外して返す。
        PG 同期が無効・接続失敗・結果ゼロのいずれでも空リストを返す。
        """
        import devgear.mem.embedding as _emb
        from devgear.mem.pg_database import PgDatabase

        sync_cfg = self.settings.sync
        if not sync_cfg.enabled or not sync_cfg.postgres_url:
            log.info("チーム検索にはPG同期の有効化が必要です")
            return []

        pg_db = PgDatabase(sync_cfg.postgres_url)
        try:
            if not pg_db.test_connection():
                log.error("PostgreSQL への接続に失敗しました")
                return []

            query_embedding = _emb.embed_query(query, self.settings.embedding_model)
            # model.onnx 未完了時は embed_query が [] を返す。
            # PG の team_search はベクトル引数が必須のため FTS フォールバックなし。
            if not query_embedding:
                return []
            pg_results = pg_db.team_search(
                query,
                query_embedding,
                limit=limit,
                exclude_origin_user=exclude_origin_user,
            )
            if not pg_results:
                return []

            chunk_ids = [cid for cid, _ in pg_results]
            rows = pg_db.fetch_chunks_by_ids(chunk_ids)
            score_map = dict(pg_results)

            results: list[SearchResult] = []
            for chunk_id in chunk_ids:
                row = rows.get(chunk_id)
                if row is None:
                    continue
                results.append(
                    SearchResult(
                        chunk_id=chunk_id,
                        score=score_map[chunk_id],
                        content=row["content"],
                        user_prompt=row["user_prompt"],
                        project=row["project"],
                        created_at_epoch=row["created_at_epoch"],
                        tool_names=row["tool_names"],
                        files_read=row["files_read"],
                        files_modified=row["files_modified"],
                    )
                )
            return results
        finally:
            pg_db.close()


def adaptive_decay(
    created_at_epoch: int,
    last_accessed_epoch: int | None,
    access_count: int,
    base_half_life: float = 30.0,
) -> float:
    """アクセス頻度で半減期を延長する時間減衰"""
    # アクセスごとに半減期を20%延長（上限180日）
    effective_half_life = min(base_half_life * (1.2**access_count), 180.0)
    # 最終アクセスからの経過で減衰
    ref_epoch = last_accessed_epoch or created_at_epoch
    age_days = (time.time() - ref_epoch) / 86400
    return math.pow(0.5, max(age_days, 0) / effective_half_life)


def should_inject_memory(prompt: str) -> bool:
    """プロンプトが過去の記憶を参照しているか判定する（ルールベース）"""
    return any(re.search(p, prompt, re.IGNORECASE) for p in _RETROSPECTIVE_PATTERNS)


def _reciprocal_rank_fusion(
    keyword_results: list,
    vector_results: list,
    k: int = 60,
) -> list[tuple]:
    """RRF スコアで 2 つのランキングを統合する"""
    scores: dict = {}
    for rank, chunk_id in enumerate(keyword_results):
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
    for rank, chunk_id in enumerate(vector_results):
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)
