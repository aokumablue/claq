"""メモリ圧縮・クリーンアップ — 低品質チャンク除去、DB最適化"""

from __future__ import annotations

import math
import time

from bluecore.mem.database import Database, MemoryChunk
from bluecore.mem.logger import get as _get_logger

log = _get_logger("COMPACT")


def detect_low_quality(db: Database) -> list[str]:
    """削除候補の chunk_id リストを返す"""
    candidates = []

    # 極短チャンク
    rows = db.conn.execute("SELECT id FROM memory_chunks WHERE LENGTH(content) < 30").fetchall()
    candidates.extend(r["id"] for r in rows)

    # 90日以上前の読み取り専用チャンク
    threshold_90d = int(time.time()) - 90 * 86400
    rows = db.conn.execute(
        """SELECT id FROM memory_chunks
       WHERE created_at_epoch < ?
         AND files_modified = '[]'
         AND tool_names NOT LIKE '%Edit%'
         AND tool_names NOT LIKE '%Write%'""",
        (threshold_90d,),
    ).fetchall()
    candidates.extend(r["id"] for r in rows)

    return list(set(candidates))


def merge_chunks(chunks: list[MemoryChunk]) -> MemoryChunk:
    """同一クラスタ内のチャンクを1つに統合する"""
    base = max(chunks, key=lambda c: c.created_at_epoch)
    max_generation = max(c.merged_generation for c in chunks)

    return MemoryChunk(
        origin_user=base.origin_user,
        session_id=base.session_id,
        project=base.project,
        chunk_index=base.chunk_index,
        content=base.content,
        tool_names=list({t for c in chunks for t in c.tool_names}),
        files_read=list({f for c in chunks for f in c.files_read}),
        files_modified=list({f for c in chunks for f in c.files_modified}),
        user_prompt=base.user_prompt,
        created_at_epoch=base.created_at_epoch,
        merged_generation=max_generation + 1,
    )


def prune_candidates(db: Database, base_half_life: float = 30.0) -> list[str]:
    """プルーニング対象の chunk_id リストを返す。

    access_count=0 かつ decay<0.01 の条件を SQL でプッシュダウンし、
    全チャンクのメモリロードを回避する。
    decay < 0.01 ⟺ age_days > base_half_life * log2(100)（access_count=0 の場合）
    """
    age_threshold_seconds = int(base_half_life * math.log2(100) * 86400)
    cutoff_epoch = int(time.time()) - age_threshold_seconds
    rows = db.conn.execute(
        """SELECT id FROM memory_chunks
           WHERE access_count = 0
             AND COALESCE(last_accessed_epoch, created_at_epoch) < ?""",
        (cutoff_epoch,),
    ).fetchall()
    return [r["id"] for r in rows if r["id"] is not None]


def optimize_db(db: Database) -> dict:
    """DB 最適化を実行し、結果を返す"""
    # 1. FTS5 インデックス最適化（セグメント統合）
    try:
        db.conn.execute("INSERT INTO memory_chunks_fts(memory_chunks_fts) VALUES('optimize')")
    except Exception as e:
        log.warning("FTS5 最適化スキップ: %s", e)

    # 2. 統計情報の更新
    db.conn.execute("PRAGMA optimize")

    # 3. 断片化率チェック → 条件付き VACUUM
    free = db.conn.execute("PRAGMA freelist_count").fetchone()[0]
    pages = db.conn.execute("PRAGMA page_count").fetchone()[0]
    fragmentation = free / pages if pages > 0 else 0

    # FTS5 optimize や PRAGMA optimize がトランザクションを開始していることがあるため、
    # VACUUM 実行前に commit して暗黙トランザクションを終了させる。
    # SQLite は VACUUM をトランザクション外でのみ実行可能。
    db.conn.commit()

    vacuumed = False
    if fragmentation > 0.15:
        try:
            db.conn.execute("VACUUM")
            db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            vacuumed = True
        except Exception as e:
            log.warning("VACUUM スキップ: %s", e)

    db.conn.commit()
    return {"fragmentation_before": fragmentation, "vacuumed": vacuumed}


def memory_health_report(db: Database) -> dict:
    """メモリストアの健全性レポートを返す"""
    total = db.conn.execute("SELECT COUNT(*) FROM memory_chunks").fetchone()[0]
    db_size = db.conn.execute("PRAGMA page_count").fetchone()[0] * db.conn.execute("PRAGMA page_size").fetchone()[0]
    short = db.conn.execute("SELECT COUNT(*) FROM memory_chunks WHERE LENGTH(content) < 30").fetchone()[0]
    avg_size = db.conn.execute("SELECT AVG(LENGTH(content)) FROM memory_chunks").fetchone()[0] or 0

    return {
        "total_chunks": total,
        "db_size_mb": round(db_size / (1024 * 1024), 2),
        "short_chunk_pct": round(short / total * 100, 1) if total > 0 else 0,
        "avg_chunk_size": round(avg_size),
    }
