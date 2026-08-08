"""メモリ圧縮・クリーンアップ — 低品質チャンク除去、DB最適化"""

from __future__ import annotations

import time

from bluecore.mem.database import Database
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
