"""pg_setup.sql の RLS 実効化契約を構造的に検証するテスト。

実 PostgreSQL を必要とせず、セットアップスクリプトが共有 DB の
WRITE 所有モデル（READ 開放 / WRITE は origin_user 一致のみ）を満たすことを
静的に保証する回帰テスト。
"""

from __future__ import annotations

from pathlib import Path

import pytest

_SQL_PATH = Path(__file__).resolve().parents[2] / "sql" / "pg_setup.sql"

# RLS を適用する全 10 テーブル。
_RLS_TABLES = (
    "memory_chunks",
    "sessions",
    "instincts",
    "adrs",
    "event_logs",
    "interaction_logs",
    "project_profiles",
    "mem_item_runs",
    "session_digests",
    "memory_chunks_vec",
)

# origin_user 列を持つ 9 テーブル（統一ポリシーを FOREACH ループで付与する対象）。
_OWNER_COLUMN_TABLES = tuple(t for t in _RLS_TABLES if t != "memory_chunks_vec")


@pytest.fixture(scope="module")
def sql() -> str:
    """pg_setup.sql の全文を返す。"""
    return _SQL_PATH.read_text(encoding="utf-8")


def test_sql_file_exists() -> None:
    """セットアップスクリプトが存在する。"""
    assert _SQL_PATH.is_file()


def test_non_owner_role_created(sql: str) -> None:
    """非所有者ロール bluecore_app を LOGIN NOSUPERUSER NOBYPASSRLS で冪等に作成する。

    BYPASSRLS を絶対に付与しないことが FORCE RLS を無停止移行の前提として
    実効化するための必須条件（所有者ロールと違い bypass できないことを保証）。
    """
    assert "rolname = 'bluecore_app'" in sql
    assert "CREATE ROLE bluecore_app LOGIN NOSUPERUSER NOBYPASSRLS" in sql


def test_policy_correction_is_atomic(sql: str) -> None:
    """既存ポリシーの是正が単一トランザクション（BEGIN/COMMIT）で囲まれる。"""
    assert "BEGIN;" in sql
    assert "COMMIT;" in sql
    assert sql.index("BEGIN;") < sql.index("COMMIT;")


def test_legacy_owner_policies_dropped(sql: str) -> None:
    """旧 current_user ベースの 3 ポリシーを DROP する。"""
    assert "DROP POLICY IF EXISTS chunks_owner_policy ON memory_chunks;" in sql
    assert "DROP POLICY IF EXISTS digests_owner_policy ON session_digests;" in sql
    assert "DROP POLICY IF EXISTS vec_owner_policy ON memory_chunks_vec;" in sql


def test_no_current_user_ownership(sql: str) -> None:
    """所有判定は current_user（PG ロール）ではなく current_setting に一本化される。"""
    # 旧ポリシーの `origin_user = current_user` 形は残っていない
    assert "= current_user" not in sql
    assert "current_setting('app.current_user', true)" in sql


def test_empty_identity_guard(sql: str) -> None:
    """空 identity は NULLIF で NULL 化され WRITE が全拒否される。

    vec 直書きポリシー（シングルクォート版）が USING / WITH CHECK の
    2 箇所に存在することを回数一致で検証する（片側欠落・重複増殖を検知）。
    """
    assert sql.count("NULLIF(current_setting('app.current_user', true), '')") == 2


def test_loop_empty_identity_guard(sql: str) -> None:
    """FOREACH ループ生成ポリシーも空 identity ガードを持つ。

    format() リテラル内はクォートが '' でエスケープされるため
    シングルクォート版とは別文字列になる。二重クォート版が USING /
    WITH CHECK の 2 箇所に存在することを回数一致で検証し、ループ側の
    エスケープ破壊やガード欠落（origin_user 9 テーブルの WRITE ポリシー
    退行）を検知する。
    """
    assert sql.count("NULLIF(current_setting(''app.current_user'', true), '''')") == 2


def test_read_policy_is_open(sql: str) -> None:
    """READ ポリシーは USING (true) でチーム全体に開放される。"""
    assert "FOR SELECT USING (true)" in sql


def test_all_tables_enabled_and_forced(sql: str) -> None:
    """全 10 テーブルで ENABLE + FORCE RLS + REVOKE PUBLIC + bluecore_app GRANT を行う。"""
    # 9 テーブルはループ配列に列挙される
    for table in _OWNER_COLUMN_TABLES:
        assert f"'{table}'" in sql
    # ループ本体が各操作を含む
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "REVOKE ALL ON %I FROM PUBLIC" in sql
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO bluecore_app" in sql
    # vec テーブルは個別に同等の設定を持つ
    assert "ALTER TABLE memory_chunks_vec FORCE ROW LEVEL SECURITY;" in sql
    assert "REVOKE ALL ON memory_chunks_vec FROM PUBLIC;" in sql
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON memory_chunks_vec TO bluecore_app;" in sql


def test_write_policy_checks_ownership(sql: str) -> None:
    """WRITE ポリシーは FOR ALL + USING/WITH CHECK で origin_user 一致を要求する。"""
    # ループ内 format() は '' でクォートをエスケープするため設定名手前までで検証する
    assert "FOR ALL" in sql
    assert "USING (origin_user = NULLIF(current_setting(" in sql
    assert "WITH CHECK (origin_user = NULLIF(current_setting(" in sql


def test_vec_ownership_via_join(sql: str) -> None:
    """origin_user 列を持たない vec は memory_chunks 経由で所有者判定する。"""
    assert "memory_chunks_vec_read" in sql
    assert "memory_chunks_vec_write" in sql
    assert "SELECT id FROM memory_chunks" in sql


def test_vec_comment_states_ownership_not_confidentiality(sql: str) -> None:
    """memory_chunks_vec のコメントは機密隔離でなく WRITE 所有が目的である旨に改訂済み。

    READ ポリシーが USING(true) で全開放されている設計と矛盾する
    「機密扱いとする」という旧コメントが残っていないことを保証する回帰テスト。
    """
    assert "機密扱いとする" not in sql
    assert "目的は機密隔離ではなく WRITE 所有" in sql
