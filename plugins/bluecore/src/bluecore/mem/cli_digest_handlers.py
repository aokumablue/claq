"""mem CLI: digest-backfill handler(既存セッションの session digest を遡及生成する)。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from bluecore.mem.database import Database
    from bluecore.mem.settings import Settings

    OpenDbFn = Callable[[Settings], AbstractContextManager[Database]]


@dataclass(frozen=True)
class DigestBackfillDeps:
    """digest-backfill ハンドラの外部依存（DB接続・ロガー）。"""

    open_db: OpenDbFn
    log: Any


def _process_session(db: Any, session_id: str, *, force: bool) -> str | None:
    """1セッション分の digest 生成/更新を行い、結果種別を返す。

    既存 digest があり force=false の場合は何もせず 'skipped' を返す。
    トランスクリプトの有無に関わらず、チャンクからの合成を含めて digest
    を構築・保存する。build_session_digest がチャンク無し等で None を
    返した場合も 'skipped' として扱う。

    Args:
        db: `get_digest_by_session` / `upsert_session_digest` を持つ DB。
        session_id: 対象セッション ID。
        force: True の場合、既存 digest があっても再生成して上書きする。

    Returns:
        'created' / 'updated' / 'skipped' / 'degraded-created' / 'degraded-updated' のいずれか。
        トランスクリプトが見つからず chunks のみで合成した場合は 'degraded-' 接頭辞を付ける。

    Raises:
        例外は発生しません。
    """
    from bluecore.mem.digest import build_session_digest, resolve_transcript_path

    existing = db.get_digest_by_session(session_id)
    if existing is not None and not force:
        return "skipped"

    transcript_path, harness = resolve_transcript_path(session_id)
    digest = build_session_digest(
        db,
        session_id,
        transcript_path=str(transcript_path) if transcript_path else None,
        harness=harness,
    )
    if digest is None:
        return "skipped"

    db.upsert_session_digest(digest)
    kind = "updated" if existing is not None else "created"
    return f"degraded-{kind}" if transcript_path is None else kind


def handle_digest_backfill(
    settings: Settings,
    stdin_data: dict[str, Any],
    deps: DigestBackfillDeps,
) -> None:
    """digest-backfill コマンド: 既存セッションの session digest を遡及生成する。

    stdin JSON: ``{"force": false, "project": null, "limit": null}``（キー欠落時は既定値）。

    対象セッションは ``memory_chunks`` に存在する session_id を列挙して決定する
    （project 指定時はそのプロジェクトに絞り込む）。セッション単位の例外は
    握って続行し、1件の破損トランスクリプトで全体を止めない。

    Args:
        settings: mem 設定。
        stdin_data: コマンド入力 JSON（force / project / limit）。
        deps: DB接続・ロガーの外部依存。

    Returns:
        None: 値を返しません（結果は標準出力へ集計行を print する）。

    Raises:
        例外は発生しません。
    """
    force = bool(stdin_data.get("force", False))
    project = stdin_data.get("project")
    limit = stdin_data.get("limit")

    created = updated = degraded = skipped = 0

    try:
        with deps.open_db(settings) as db:
            session_ids = db.get_session_ids_with_chunks(project=project)
            processed = 0
            for session_id in session_ids:
                if limit is not None and processed >= limit:
                    break
                processed += 1
                try:
                    result = _process_session(db, session_id, force=force)
                except Exception as e:
                    deps.log.warning("digest-backfill: session=%s 失敗: %s", session_id, e)
                    continue

                if result == "skipped":
                    skipped += 1
                    continue
                if result.startswith("degraded-"):
                    degraded += 1
                    result = result.removeprefix("degraded-")
                # _process_session はここまでで 'created' か 'updated' のいずれかのみを返す。
                if result == "created":
                    created += 1
                else:
                    updated += 1
    except Exception as e:
        deps.log.warning("digest-backfill 失敗: %s", e)

    print(f"digest-backfill: 生成={created} 更新={updated} 縮退={degraded} スキップ={skipped}")
