"""PostgreSQL データベースクライアント（チーム同期用）"""

from __future__ import annotations

import ipaddress
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from deepblue.mem.logger import get as _get_logger
from deepblue.mem.pg_operations import _AuxOpsMixin, _ChunkOpsMixin, _RecordOpsMixin
from deepblue.mem.pg_operations._helpers import _to_json

if TYPE_CHECKING:
    import psycopg

log = _get_logger("PG")

# テーブル別操作で参照される JSON 変換ヘルパーを従来の import パス
# ``from deepblue.mem.pg_database import _to_json`` で利用できるよう再エクスポートする。
__all__ = ["PgDatabase", "_ensure_ssl", "_is_loopback", "_to_json"]


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

    - ループバックホスト(localhost/127.0.0.1/::1) + sslmode=disable: 警告のみで許可
      （SSL 非対応のローカル PG 向け開発用例外）
    - sslmode=allow/prefer: ループバックでも拒否（中途半端な TLS は意味がない）
    - sslmode 未指定: sslmode=require を自動付与
    - sslmode=require: 警告を出して維持（verify-full 推奨）
    - sslmode=verify-full: そのまま維持
    - リモートホストで sslmode=disable: ValueError（フェイルクローズ）
    """
    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    existing = qs.get("sslmode", [])
    if existing:
        mode = existing[0].lower()
        if mode == "disable":
            if _is_loopback(url):
                # ローカル開発環境: SSL 非対応 PG を許容
                log.warning(
                    "sslmode=disable はローカル接続(%s)のみ許可されます。"
                    "本番環境では sslmode=require 以上を使用してください。",
                    parsed.hostname,
                )
                return url
            raise ValueError(
                f"PostgreSQL URL に安全でない sslmode={mode!r} が指定されています。"
                " sslmode=require 以上を使用してください。"
            )
        if mode in ("allow", "prefer"):
            raise ValueError(
                f"PostgreSQL URL に安全でない sslmode={mode!r} が指定されています。"
                " sslmode=require 以上を使用してください。"
            )
        # verify-full / require 等の安全な値はそのまま使用
        if mode == "require":
            log.warning(
                "sslmode=require は証明書検証を行いません。中間者攻撃への完全な保護には"
                " sslmode=verify-full を推奨します。"
            )
        return url
    # sslmode 未指定 → require を付与
    qs["sslmode"] = ["require"]
    new_query = urlencode(qs, doseq=True)
    new_parsed = parsed._replace(query=new_query)
    return urlunparse(new_parsed)


class PgDatabase(_ChunkOpsMixin, _RecordOpsMixin, _AuxOpsMixin):
    """PostgreSQL データベースクライアント。

    psycopg は遅延インポートで、同期が無効な場合はインストール不要。
    接続プールを使用し、複数接続の効率的な管理を行う。

    テーブル別の UPSERT / 検索メソッドは ``deepblue.mem.pg_operations`` の
    ミックスイン（:class:`_ChunkOpsMixin` / :class:`_RecordOpsMixin` /
    :class:`_AuxOpsMixin`）が提供し、本クラスは接続・トランザクション管理を担う。
    """

    # 接続テスト失敗時のキャッシュ TTL（秒）。
    # 成功時はキャッシュしない（毎回テストする）。
    _PROBE_TTL: float = 300.0

    def __init__(self, postgres_url: str, *, use_pool: bool = True) -> None:
        """接続 URL を保持して初期化する（接続は遅延・任意でプール使用）。"""
        self._url = postgres_url
        self._conn: psycopg.Connection | None = None
        self._pool = None
        self._use_pool = use_pool
        # (result, cached_at) — 失敗時のみ設定する
        self._probe_cache: tuple[bool, float] | None = None

    def _get_conn(self) -> psycopg.Connection:
        """接続を取得（遅延接続）。プールが有効なら ConnectionPool を使用。"""
        if self._use_pool:
            if self._pool is None:
                try:
                    from psycopg_pool import ConnectionPool

                    self._pool = ConnectionPool(_ensure_ssl(self._url), min_size=1, max_size=4)
                except ImportError:
                    # psycopg_pool 未インストール時はフォールバック
                    log.debug("psycopg_pool が見つかりません。単一接続を使用します")
                    self._use_pool = False
                    return self._get_conn()
            return self._pool.getconn()
        # フォールバック: 単一接続
        if self._conn is None or self._conn.closed:
            import psycopg

            self._conn = psycopg.connect(_ensure_ssl(self._url))
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
        except Exception as e:
            log.error("PostgreSQL 操作に失敗したためロールバックします: %s", e)
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
            if conn:
                self._put_conn(conn)

        # 成功時はキャッシュを無効化して以降も毎回テストする
        self._probe_cache = None
        return ok
