"""PostgreSQL データベースクライアント（チーム同期用）"""

from __future__ import annotations

import ipaddress
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from bluecore.lib.core_utils import get_git_user_name
from bluecore.mem.logger import get as _get_logger
from bluecore.mem.pg_write_mixin import PgWriteMixin

if TYPE_CHECKING:
    import psycopg

log = _get_logger("PG")

# PG 到達不能（パケット drop 等）時に OS の TCP タイムアウト（Linux で 130 秒前後）
# までフックがブロックするのを防ぐ接続タイムアウト（秒）。
_CONNECT_TIMEOUT = 5

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
    - sslmode 未指定: ループバック接続は sslmode=disable、それ以外は
      sslmode=require を自動付与（安全側デフォルト）
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


class PgDatabase(PgWriteMixin):
    """PostgreSQL データベースクライアント。

    psycopg は遅延インポートで、同期が無効な場合はインストール不要。
    接続プールを使用し、複数接続の効率的な管理を行う。

    各テーブルへのバッチ書き込みメソッドは `PgWriteMixin`
    （pg_write_mixin.py）が提供する。接続管理・検索メソッドは本クラスに残す。
    """

    # 接続テスト失敗時のキャッシュ TTL（秒）。
    # 成功時はキャッシュしない（毎回テストする）。
    _PROBE_TTL: float = 300.0

    def __init__(
        self,
        postgres_url: str,
        *,
        use_pool: bool = True,
        identity: str | None = None,
    ) -> None:
        self._url = postgres_url
        self._conn: psycopg.Connection | None = None
        self._pool = None
        self._use_pool = use_pool
        # RLS ポリシーが参照する current_setting('app.current_user') の値。
        # 明示指定が無ければ git user.name に一本化する（共有 DB の WRITE 所有判定）。
        self._identity = identity if identity is not None else get_git_user_name()
        # (result, cached_at) — 失敗時のみ設定する
        self._probe_cache: tuple[bool, float] | None = None

    def _apply_identity(self, conn: psycopg.Connection) -> None:
        """接続に RLS 用のアプリユーザー識別子を session-local で設定する。

        RLS ポリシーが参照する current_setting('app.current_user') を
        git user.name（self._identity）に一本化する。is_local=true により
        設定は現在のトランザクション終了時に失効し、プール接続へ漏れない。
        """
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.current_user', %s, true)", (self._identity,))

    def _get_conn(self, *, for_write: bool = True) -> psycopg.Connection:
        """接続を取得（遅延接続）。プールが有効なら ConnectionPool を使用。

        パスワードは settings.json から除去され <data_dir>/.pgpass に分離されるため、
        接続時に passfile パラメータでそのファイルを参照させる（不在時も無害）。

        Args:
            for_write: True（デフォルト、安全側）の場合は RLS 用の
                identity（`_apply_identity`）を必ず DB ラウンドトリップ込みで
                適用する。この DB は READ 隔離ではなく WRITE 所有
                （origin_user 詐称防止）のために RLS を使う設計であるため、
                READ 専用経路（vec_search/fts_search/fetch_chunks_by_ids/
                test_connection）に限り呼び出し側が明示的に False を渡すと
                identity 適用を省略し、不要な DB ラウンドトリップを避ける。
                フラグを付け忘れても安全な側（identity 適用あり）に倒れる。
        """
        from bluecore.mem.settings import pgpass_path

        passfile = str(pgpass_path())
        if self._use_pool:
            if self._pool is None:
                try:
                    from psycopg_pool import ConnectionPool

                    self._pool = ConnectionPool(
                        _ensure_ssl(self._url),
                        kwargs={"passfile": passfile, "connect_timeout": _CONNECT_TIMEOUT},
                        min_size=1,
                        max_size=4,
                    )
                except ImportError:
                    # psycopg_pool 未インストール時はフォールバック
                    log.debug("psycopg_pool が見つかりません。単一接続を使用します")
                    self._use_pool = False
                    return self._get_conn(for_write=for_write)
            conn = self._pool.getconn()
        else:
            # フォールバック: 単一接続
            if self._conn is None or self._conn.closed:
                import psycopg

                self._conn = psycopg.connect(
                    _ensure_ssl(self._url), passfile=passfile, connect_timeout=_CONNECT_TIMEOUT
                )
            conn = self._conn
        if for_write:
            try:
                self._apply_identity(conn)
            except Exception:
                if self._use_pool:
                    self._pool.putconn(conn)
                else:
                    self._conn.close()
                    self._conn = None
                raise
        return conn

    def _put_conn(self, conn: psycopg.Connection) -> None:
        """プール使用時に接続を返却する。"""
        if self._use_pool and self._pool is not None:
            self._pool.putconn(conn)

    @contextmanager
    def transaction(self, *, for_write: bool = True) -> Generator[psycopg.Connection, None, None]:
        """トランザクションコンテキスト。接続を yield する。

        Args:
            for_write: `_get_conn` にそのまま渡す。デフォルト True（安全側）で
                RLS identity を必ず適用する。READ 専用処理でのみ呼び出し側が
                明示的に False を指定できる。
        """
        conn = self._get_conn(for_write=for_write)
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
            conn = self._get_conn(for_write=False)
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
        conn = self._get_conn(for_write=False)
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
        conn = self._get_conn(for_write=False)
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
        conn = self._get_conn(for_write=False)
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
