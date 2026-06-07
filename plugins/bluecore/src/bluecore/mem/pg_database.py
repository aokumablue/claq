"""PostgreSQL データベースクライアント（チーム同期用）"""

from __future__ import annotations

import ipaddress
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from bluecore.mem.database import (
    Adr,
    EventLog,
    Instinct,
    InteractionLog,
    MemItemRun,
    MemoryChunk,
    ProjectProfile,
    Session,
)
from bluecore.mem.logger import get as _get_logger

if TYPE_CHECKING:
    import psycopg

log = _get_logger("PG")

def _is_loopback(url: str) -> bool:
    """URL のホストがローカルループバックか判定する。

    localhost（名前解決前の文字列）・127.0.0.0/8・::1・
    IPv4-mapped IPv6（::ffff:127.x.x.x）・Unix ソケットを許容する。
    """
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if not host or host.startswith("/"):  # Unix socket
        return True
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return ip.is_loopback
    except ValueError:
        return False


def _ensure_ssl(url: str) -> str:
    """URL に sslmode を適用する。

    ユーザが明示指定した sslmode は値を変更せず尊重する。安全性が低い値には
    警告ログを出すが、接続自体は拒否しない（最低限のセキュリティ担保）。

    - sslmode 明示指定: 値はそのまま維持
      - disable/allow/prefer: リモートホスト時のみ警告（ローカルは静か）
      - require: verify-full 推奨の警告を出して維持
      - verify-full 等: そのまま維持
    - sslmode 未指定: sslmode=require を自動付与（安全側デフォルト）
    """
    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    existing = qs.get("sslmode", [])
    if existing:
        mode = existing[0].lower()
        if mode in ("disable", "allow", "prefer") and not _is_loopback(url):
            log.warning(
                "sslmode=%s はリモート接続(%s)では安全ではありません。"
                " 本番環境では sslmode=require 以上を推奨します。",
                mode,
                parsed.hostname,
            )
        elif mode == "require":
            log.warning(
                "sslmode=require は証明書検証を行いません。中間者攻撃への完全な保護には"
                " sslmode=verify-full を推奨します。"
            )
        return url
    # sslmode 未指定 → ループバック接続は disable、それ以外は require
    qs["sslmode"] = ["disable" if _is_loopback(url) else "require"]
    new_query = urlencode(qs, doseq=True)
    new_parsed = parsed._replace(query=new_query)
    return urlunparse(new_parsed)


class PgDatabase:
    """PostgreSQL データベースクライアント。

    psycopg は遅延インポートで、同期が無効な場合はインストール不要。
    接続プールを使用し、複数接続の効率的な管理を行う。
    """

    # 接続テスト失敗時のキャッシュ TTL（秒）。
    # 成功時はキャッシュしない（毎回テストする）。
    _PROBE_TTL: float = 300.0

    def __init__(self, postgres_url: str, *, use_pool: bool = True) -> None:
        self._url = postgres_url
        self._conn: psycopg.Connection | None = None
        self._pool = None
        self._use_pool = use_pool
        # (result, cached_at) — 失敗時のみ設定する
        self._probe_cache: tuple[bool, float] | None = None

    def _get_conn(self) -> psycopg.Connection:
        """接続を取得（遅延接続）。プールが有効なら ConnectionPool を使用。

        パスワードは settings.json から除去され <data_dir>/.pgpass に分離されるため、
        接続時に passfile パラメータでそのファイルを参照させる（不在時も無害）。
        """
        from bluecore.mem.settings import pgpass_path

        passfile = str(pgpass_path())
        if self._use_pool:
            if self._pool is None:
                try:
                    from psycopg_pool import ConnectionPool

                    self._pool = ConnectionPool(_ensure_ssl(self._url), kwargs={"passfile": passfile}, min_size=1, max_size=4)
                except ImportError:
                    # psycopg_pool 未インストール時はフォールバック
                    log.debug("psycopg_pool が見つかりません。単一接続を使用します")
                    self._use_pool = False
                    return self._get_conn()
            return self._pool.getconn()
        # フォールバック: 単一接続
        if self._conn is None or self._conn.closed:
            import psycopg

            self._conn = psycopg.connect(_ensure_ssl(self._url), passfile=passfile)
        return self._conn

    def _put_conn(self, conn: psycopg.Connection) -> None:
        """プール使用時に接続を返却する。"""
        if self._use_pool and self._pool is not None:
            self._pool.putconn(conn)

    @contextmanager
    def transaction(self) -> Generator[psycopg.Connection, None, None]:
        """トランザクションコンテキスト。接続を yield する。"""
        conn = self._get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put_conn(conn)

    def close(self) -> None:
        """接続を閉じる。"""
        if self._pool is not None:
            self._pool.close()
            self._pool = None
        if self._conn and not self._conn.closed:
            self._conn.close()
            self._conn = None

    def test_connection(self) -> bool:
        """接続テスト。

        失敗時は _PROBE_TTL 秒間キャッシュして ERROR ログを初回のみ出す。
        成功時はキャッシュせず以降も毎回テストを行う。
        """
        # キャッシュヒット確認（失敗キャッシュのみ）
        if self._probe_cache is not None:
            result, cached_at = self._probe_cache
            if not result and time.monotonic() - cached_at < self._PROBE_TTL:
                return False

        conn = None
        try:
            conn = self._get_conn()
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                ok = cur.fetchone() is not None
        except Exception as e:
            log.error("PostgreSQL 接続テスト失敗: %s", e)
            # 失敗をキャッシュして TTL 内は再試行しない
            self._probe_cache = (False, time.monotonic())
            return False
        finally:
            if conn is not None:  # pragma: no branch  # conn None は except 経路のみで finally 後に成功 return しない
                self._put_conn(conn)

        # 成功時はキャッシュを無効化して以降も毎回テストする
        self._probe_cache = None
        return ok

    # --- memory_chunks ---

    def upsert_chunk(self, chunk: MemoryChunk, origin_user: str) -> None:
        """チャンクを UPSERT する。"""
        self.upsert_chunks_batch([chunk], origin_user)

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
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put_conn(conn)
        return count

    # --- sessions ---

    def upsert_session(self, session: Session, origin_user: str) -> None:
        """セッションを UPSERT する。"""
        self.upsert_sessions_batch([session], origin_user)

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
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put_conn(conn)
        return count

    # --- instincts ---

    def upsert_instinct(self, instinct: Instinct) -> None:
        """インスティンクトを UPSERT する。"""
        self.upsert_instincts_batch([instinct])

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
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put_conn(conn)
        return count

    # --- adrs ---

    def upsert_adr(self, adr: Adr) -> None:
        """ADR を UPSERT する。"""
        self.upsert_adrs_batch([adr])

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
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put_conn(conn)
        return count

    # --- event_logs ---

    def insert_event_log(self, event: EventLog) -> None:
        """イベントログを INSERT する（重複は無視）。"""
        self.insert_event_logs_batch([event])

    def insert_event_logs_batch(self, events: list[EventLog]) -> int:
        """イベントログをバッチで INSERT する。"""
        if not events:
            return 0
        conn = self._get_conn()
        try:
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
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO event_logs
             (id, origin_user, event_type, project_id, content, created_at_epoch, synced_at)
             VALUES (%s, %s, %s, %s, %s, %s, NOW())
             ON CONFLICT (id) DO NOTHING""",
                    params_list,
                )
            conn.commit()
            count = len(params_list)
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put_conn(conn)
        return count

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
        except Exception:
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
                return [(str(row[0]), row[1]) for row in cur.fetchall()]
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
                return [(str(row[0]), row[1]) for row in cur.fetchall()]
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

        # chunk_ids は呼び出し側で _SYNC_BATCH_SIZE 以下にバッチ分割されるためプレースホルダ数は安全
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
        except Exception:
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
        except Exception:
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
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put_conn(conn)
        return count


def _to_json(val: list | dict | None) -> str | None:
    """リストや辞書を JSON 文字列に変換。"""
    if val is None:
        return None
    import json

    return json.dumps(val, ensure_ascii=False)
