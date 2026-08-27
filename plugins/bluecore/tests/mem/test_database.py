"""bluecore.mem.database — repos / sessions / knowledge の 3 テーブルのテスト。"""

from __future__ import annotations

import sqlite3
import stat
from pathlib import Path

import pytest

from bluecore.mem.database import Database, DatabaseError
from bluecore.mem.models import Knowledge, Repo, Session, utc_now_iso

_TS = "2026-01-01T00:00:00+00:00"


@pytest.fixture
def db(tmp_path: Path) -> Database:
    """テスト用の空 DB を開いて返す。"""
    database = Database(tmp_path / "mem.db")
    yield database
    database.close()


def _repo(repo_id: str = "bluecore-dev", identity_key: str = "git@github.com:x/bluecore.git") -> Repo:
    """テスト用 Repo を組み立てる。"""
    return Repo(
        id=repo_id,
        identity_key=identity_key,
        root_path="/Users/x/dev/bluecore-dev",
        remote_url="git@github.com:x/bluecore.git",
        first_seen_at=_TS,
        last_seen_at=_TS,
    )


def _knowledge(key: str = "use-python3", **overrides: object) -> Knowledge:
    """テスト用 Knowledge を組み立てる。"""
    params: dict[str, object] = {
        "key": key,
        "scope": "global",
        "kind": "convention",
        "title": "Python は python3 コマンドで実行する",
        "source": "human",
        "created_at": _TS,
        "updated_at": _TS,
    }
    params.update(overrides)
    return Knowledge(**params)


class TestSchema:
    """DDL が意図通りのテーブル・制約を作ることを確認する。"""

    def test_creates_exactly_three_tables(self, db: Database) -> None:
        """作られる実テーブルは repos / sessions / knowledge の 3 つだけ。"""
        rows = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        assert sorted(r["name"] for r in rows) == ["knowledge", "repos", "sessions"]

    def test_no_virtual_tables_or_triggers(self, db: Database) -> None:
        """FTS5 / sqlite-vec 仮想テーブルと同期トリガは残っていない。"""
        rows = db.conn.execute("SELECT sql FROM sqlite_master WHERE type = 'trigger'").fetchall()
        assert rows == []
        virtual = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE sql LIKE '%VIRTUAL TABLE%'"
        ).fetchall()
        assert virtual == []

    def test_foreign_keys_enabled(self, db: Database) -> None:
        """PRAGMA foreign_keys が有効。"""
        assert db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    def test_journal_mode_is_wal(self, db: Database) -> None:
        """PRAGMA journal_mode が WAL。"""
        assert db.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    def test_scope_repo_id_consistency_check(self, db: Database) -> None:
        """scope='repo' で repo_id が NULL の行は CHECK 制約で弾かれる。"""
        with pytest.raises(sqlite3.IntegrityError):
            db.upsert_knowledge(_knowledge(scope="repo", repo_id=None))

    def test_global_scope_rejects_repo_id(self, db: Database) -> None:
        """scope='global' で repo_id を持つ行は CHECK 制約で弾かれる。"""
        db.upsert_repo(_repo())
        with pytest.raises(sqlite3.IntegrityError):
            db.upsert_knowledge(_knowledge(scope="global", repo_id="bluecore-dev"))

    def test_invalid_kind_rejected(self, db: Database) -> None:
        """kind の許容値外は CHECK 制約で弾かれる。"""
        with pytest.raises(sqlite3.IntegrityError):
            db.upsert_knowledge(_knowledge(kind="rumor"))

    def test_invalid_source_rejected(self, db: Database) -> None:
        """source の許容値外は CHECK 制約で弾かれる。"""
        with pytest.raises(sqlite3.IntegrityError):
            db.upsert_knowledge(_knowledge(source="oracle"))

    def test_confidence_range_enforced(self, db: Database) -> None:
        """confidence は 0〜1 の範囲外を弾く。"""
        with pytest.raises(sqlite3.IntegrityError):
            db.upsert_knowledge(_knowledge(confidence=1.5))

    def test_repo_delete_cascades_to_knowledge(self, db: Database) -> None:
        """repos の削除は repo スコープの knowledge を CASCADE 削除する。"""
        db.upsert_repo(_repo())
        db.upsert_knowledge(_knowledge(scope="repo", repo_id="bluecore-dev"))
        db.conn.execute("DELETE FROM repos WHERE id = ?", ("bluecore-dev",))
        db.conn.commit()
        assert db.list_knowledge() == []


class TestConnection:
    """接続・初期化・後始末の挙動。"""

    def test_new_db_is_chmod_0600(self, tmp_path: Path) -> None:
        """新規作成時の DB ファイルは 0600。"""
        db_path = tmp_path / "nested" / "mem.db"
        with Database(db_path):
            pass
        assert stat.S_IMODE(db_path.stat().st_mode) == 0o600

    def test_parent_dir_under_bluecore_is_0700(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """~/.bluecore 配下に作る DB の親ディレクトリは umask 022 でも 0700。"""
        import os

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("BLUECORE_HOME", raising=False)
        old_umask = os.umask(0o022)
        try:
            db_path = tmp_path / ".bluecore" / "mem.db"
            with Database(db_path):
                pass
            assert stat.S_IMODE((tmp_path / ".bluecore").stat().st_mode) == 0o700
        finally:
            os.umask(old_umask)

    def test_existing_db_permission_is_corrected(self, tmp_path: Path) -> None:
        """既存 DB が 0600 以外なら開き直すたびに 0600 へ補正する（F-08）。

        以前は新規作成時にしか chmod せず、既存 DB は 0644 のまま
        放置していた。他ユーザーが読めるファイルを残さないよう、
        接続のたびに mode を検証・補正する。
        """
        db_path = tmp_path / "mem.db"
        with Database(db_path):
            pass
        db_path.chmod(0o644)
        with Database(db_path):
            pass
        assert stat.S_IMODE(db_path.stat().st_mode) == 0o600

    def test_context_manager_closes_connection(self, tmp_path: Path) -> None:
        """with を抜けると接続が閉じている。"""
        with Database(tmp_path / "mem.db") as database:
            assert isinstance(database, Database)
        with pytest.raises(sqlite3.ProgrammingError):
            database.conn.execute("SELECT 1")

    def test_performance_pragmas_applied(self, db: Database) -> None:
        """最適化 PRAGMA が適用されている。"""
        assert db.conn.execute("PRAGMA temp_store").fetchone()[0] == 2
        assert db.conn.execute("PRAGMA mmap_size").fetchone()[0] == 268435456


class TestRepos:
    """repos の CRUD。"""

    def test_upsert_inserts_new_repo(self, db: Database) -> None:
        """新規リポジトリを挿入して格納値を返す。"""
        stored = db.upsert_repo(_repo())
        assert stored.id == "bluecore-dev"
        assert stored.identity_key == "git@github.com:x/bluecore.git"
        assert stored.first_seen_at == _TS

    def test_upsert_resolves_conflict_by_identity_key(self, db: Database) -> None:
        """identity_key が一致すれば id と first_seen_at を保持して観測情報のみ更新する。"""
        db.upsert_repo(_repo())
        moved = Repo(
            id="別のスラッグ",
            identity_key="git@github.com:x/bluecore.git",
            root_path="/Users/x/worktrees/feature",
            remote_url=None,
            first_seen_at="2026-06-01T00:00:00+00:00",
            last_seen_at="2026-06-01T00:00:00+00:00",
        )
        stored = db.upsert_repo(moved)
        assert stored.id == "bluecore-dev"
        assert stored.first_seen_at == _TS
        assert stored.root_path == "/Users/x/worktrees/feature"
        assert stored.remote_url is None
        assert stored.last_seen_at == "2026-06-01T00:00:00+00:00"
        assert len(db.list_repos()) == 1

    def test_list_repos_orders_by_last_seen_desc(self, db: Database) -> None:
        """最終観測の新しい順に返す。"""
        db.upsert_repo(_repo("old", "key-old"))
        newer = _repo("new", "key-new")
        newer.last_seen_at = "2026-07-01T00:00:00+00:00"
        db.upsert_repo(newer)
        assert [repo.id for repo in db.list_repos()] == ["new", "old"]

    def test_list_repos_empty(self, db: Database) -> None:
        """1 件も無ければ空リスト。"""
        assert db.list_repos() == []


class TestKnowledge:
    """knowledge の CRUD。"""

    def test_upsert_inserts_global_knowledge(self, db: Database) -> None:
        """global スコープの知識カードを挿入する。"""
        stored = db.upsert_knowledge(_knowledge())
        assert stored.id is not None
        assert stored.repo_id is None
        assert stored.status == "active"
        assert stored.confidence == 0.5
        assert stored.body == ""

    def test_upsert_updates_on_key_conflict_keeping_created_at(self, db: Database) -> None:
        """同一 key の再投入は id と created_at を保持して内容を更新する。"""
        first = db.upsert_knowledge(_knowledge())
        second = db.upsert_knowledge(
            _knowledge(
                title="Python は python3 で実行する（改訂）",
                body="venv は ~/.bluecore/.venv のみ",
                confidence=0.9,
                updated_at="2026-02-02T00:00:00+00:00",
            )
        )
        assert second.id == first.id
        assert second.created_at == _TS
        assert second.updated_at == "2026-02-02T00:00:00+00:00"
        assert second.confidence == 0.9
        assert len(db.list_knowledge()) == 1

    def test_same_key_allowed_across_scopes(self, db: Database) -> None:
        """global と repo で同じ key を並存できる（式インデックスの境界）。"""
        db.upsert_repo(_repo())
        db.upsert_knowledge(_knowledge())
        db.upsert_knowledge(_knowledge(scope="repo", repo_id="bluecore-dev"))
        assert len(db.list_knowledge()) == 2

    def test_same_key_conflicts_within_same_repo(self, db: Database) -> None:
        """同一 repo 内で同じ key は 1 行に集約される。"""
        db.upsert_repo(_repo())
        db.upsert_knowledge(_knowledge(scope="repo", repo_id="bluecore-dev"))
        db.upsert_knowledge(_knowledge(scope="repo", repo_id="bluecore-dev", title="上書き"))
        rows = db.list_knowledge(scope="repo")
        assert len(rows) == 1
        assert rows[0].title == "上書き"

    def test_upsert_persists_all_optional_columns(self, db: Database) -> None:
        """任意カラムがすべて往復する。"""
        db.upsert_repo(_repo())
        session = db.start_session(Session(session_uid="uid-1", repo_id="bluecore-dev"))
        base = db.upsert_knowledge(_knowledge("old-way"))
        stored = db.upsert_knowledge(
            _knowledge(
                "new-way",
                kind="pitfall",
                body="理由の説明",
                domain="testing",
                confidence=0.8,
                status="pending",
                source="observer",
                source_ref="plugins/bluecore/src/bluecore/mem/database.py",
                session_id=session.id,
                superseded_by=base.id,
            )
        )
        assert stored.kind == "pitfall"
        assert stored.body == "理由の説明"
        assert stored.domain == "testing"
        assert stored.confidence == 0.8
        assert stored.status == "pending"
        assert stored.source == "observer"
        assert stored.source_ref.endswith("database.py")
        assert stored.session_id == session.id
        assert stored.superseded_by == base.id

    def test_get_by_key_global(self, db: Database) -> None:
        """repo_id 省略で global スコープの行を引く。"""
        db.upsert_knowledge(_knowledge())
        found = db.get_knowledge_by_key("use-python3")
        assert found is not None
        assert found.scope == "global"

    def test_get_by_key_scoped_to_repo(self, db: Database) -> None:
        """repo_id 指定で repo スコープの行だけを引く。"""
        db.upsert_repo(_repo())
        db.upsert_knowledge(_knowledge(scope="repo", repo_id="bluecore-dev", title="repo 側"))
        db.upsert_knowledge(_knowledge(title="global 側"))
        found = db.get_knowledge_by_key("use-python3", repo_id="bluecore-dev")
        assert found is not None
        assert found.title == "repo 側"

    def test_get_by_key_returns_none_when_missing(self, db: Database) -> None:
        """該当なしなら None。"""
        assert db.get_knowledge_by_key("nope") is None

    def test_list_without_filters_returns_all(self, db: Database) -> None:
        """絞り込み無しは全件を更新の新しい順で返す。"""
        db.upsert_knowledge(_knowledge("a", updated_at=_TS))
        db.upsert_knowledge(_knowledge("b", updated_at="2026-03-01T00:00:00+00:00"))
        assert [row.key for row in db.list_knowledge()] == ["b", "a"]

    def test_list_with_all_filters(self, db: Database) -> None:
        """scope・repo_id・status のすべてで絞り込む。"""
        db.upsert_repo(_repo())
        db.upsert_repo(_repo("other", "key-other"))
        db.upsert_knowledge(_knowledge("hit", scope="repo", repo_id="bluecore-dev"))
        db.upsert_knowledge(_knowledge("wrong-repo", scope="repo", repo_id="other"))
        db.upsert_knowledge(_knowledge("wrong-scope"))
        db.upsert_knowledge(
            _knowledge("wrong-status", scope="repo", repo_id="bluecore-dev", status="archived")
        )
        rows = db.list_knowledge(scope="repo", repo_id="bluecore-dev", status="active")
        assert [row.key for row in rows] == ["hit"]

    def test_set_status_updates_row(self, db: Database) -> None:
        """status と updated_at を更新して True を返す。"""
        stored = db.upsert_knowledge(_knowledge())
        assert db.set_knowledge_status(stored.id, "archived", updated_at="2026-05-05T00:00:00+00:00") is True
        after = db.get_knowledge_by_key("use-python3")
        assert after.status == "archived"
        assert after.updated_at == "2026-05-05T00:00:00+00:00"

    def test_set_status_defaults_updated_at_to_now(self, db: Database) -> None:
        """updated_at 省略時は現在時刻を書き込む。"""
        stored = db.upsert_knowledge(_knowledge())
        assert db.set_knowledge_status(stored.id, "pending") is True
        after = db.get_knowledge_by_key("use-python3")
        assert after.updated_at > _TS

    def test_set_status_returns_false_when_missing(self, db: Database) -> None:
        """該当行が無ければ False。"""
        assert db.set_knowledge_status(9999, "archived") is False


class TestSessions:
    """sessions の CRUD。"""

    def test_start_session_inserts(self, db: Database) -> None:
        """セッションを開始登録して id を採番する。"""
        db.upsert_repo(_repo())
        stored = db.start_session(
            Session(session_uid="uid-1", repo_id="bluecore-dev", harness="claude", started_at=_TS)
        )
        assert stored.id is not None
        assert stored.harness == "claude"
        assert stored.handoff == ""
        assert stored.ended_at is None

    def test_start_session_is_idempotent(self, db: Database) -> None:
        """同一 session_uid の再入は started_at を保持し harness だけ更新する。"""
        db.upsert_repo(_repo())
        first = db.start_session(
            Session(session_uid="uid-1", repo_id="bluecore-dev", started_at=_TS)
        )
        second = db.start_session(
            Session(
                session_uid="uid-1",
                repo_id="bluecore-dev",
                harness="codex",
                started_at="2026-09-09T00:00:00+00:00",
            )
        )
        assert second.id == first.id
        assert second.started_at == _TS
        assert second.harness == "codex"

    def test_start_session_requires_existing_repo(self, db: Database) -> None:
        """未登録 repo_id は外部キー制約で弾かれる。"""
        with pytest.raises(sqlite3.IntegrityError):
            db.start_session(Session(session_uid="uid-1", repo_id="missing"))

    def test_finish_session_records_handoff(self, db: Database) -> None:
        """handoff と ended_at を記録して True を返す。"""
        db.upsert_repo(_repo())
        db.start_session(Session(session_uid="uid-1", repo_id="bluecore-dev"))
        assert db.finish_session("uid-1", "次はテストを書く", ended_at="2026-01-02T00:00:00+00:00") is True
        latest = db.get_latest_session("bluecore-dev")
        assert latest.handoff == "次はテストを書く"
        assert latest.ended_at == "2026-01-02T00:00:00+00:00"

    def test_finish_session_defaults_ended_at_to_now(self, db: Database) -> None:
        """ended_at 省略時は現在時刻を書き込む。"""
        db.upsert_repo(_repo())
        db.start_session(Session(session_uid="uid-1", repo_id="bluecore-dev"))
        assert db.finish_session("uid-1", "引き継ぎ") is True
        latest = db.get_latest_session("bluecore-dev")
        assert latest.ended_at is not None
        assert latest.ended_at <= utc_now_iso()

    def test_finish_session_returns_false_when_missing(self, db: Database) -> None:
        """未登録の session_uid では False。"""
        assert db.finish_session("unknown", "引き継ぎ") is False

    def test_get_latest_session_skips_empty_handoff(self, db: Database) -> None:
        """handoff が空のセッションは対象外。"""
        db.upsert_repo(_repo())
        db.start_session(
            Session(session_uid="uid-1", repo_id="bluecore-dev", started_at=_TS)
        )
        assert db.get_latest_session("bluecore-dev") is None

    def test_get_latest_session_returns_newest_with_handoff(self, db: Database) -> None:
        """引き継ぎを持つ中で最新の 1 件を返す。"""
        db.upsert_repo(_repo())
        for uid, started in (("uid-1", _TS), ("uid-2", "2026-02-01T00:00:00+00:00")):
            db.start_session(Session(session_uid=uid, repo_id="bluecore-dev", started_at=started))
            db.finish_session(uid, f"handoff for {uid}")
        latest = db.get_latest_session("bluecore-dev")
        assert latest.session_uid == "uid-2"

    def test_repo_delete_cascades_to_sessions(self, db: Database) -> None:
        """repos の削除は sessions を CASCADE 削除する。"""
        db.upsert_repo(_repo())
        db.start_session(Session(session_uid="uid-1", repo_id="bluecore-dev"))
        db.conn.execute("DELETE FROM repos WHERE id = ?", ("bluecore-dev",))
        db.conn.commit()
        assert db.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0

    def test_session_delete_nulls_knowledge_link(self, db: Database) -> None:
        """sessions の削除で knowledge.session_id は NULL になる（SET NULL）。"""
        db.upsert_repo(_repo())
        session = db.start_session(Session(session_uid="uid-1", repo_id="bluecore-dev"))
        db.upsert_knowledge(_knowledge(session_id=session.id))
        db.conn.execute("DELETE FROM sessions WHERE id = ?", (session.id,))
        db.conn.commit()
        assert db.get_knowledge_by_key("use-python3").session_id is None


class TestPathValidation:
    """F-25: 既存パスが symlink / 非 regular file なら接続前に拒否する。"""

    def test_symlink_is_rejected_before_connect(self, tmp_path: Path) -> None:
        """`mem.db -> target.db` を張られた状態で開くとリンク先を汚染しないこと。

        sqlite3.connect は symlink を追うため、拒否しないとリンク先へ
        repos/sessions/knowledge が作られ、無関係な DB を壊す。
        """
        target = tmp_path / "target.db"
        target.write_text("", encoding="utf-8")
        link = tmp_path / "mem.db"
        link.symlink_to(target)

        with pytest.raises(DatabaseError, match="symlink"):
            Database(link)

        assert target.read_text(encoding="utf-8") == ""

    def test_dangling_symlink_is_rejected(self, tmp_path: Path) -> None:
        """ぶら下がり symlink も拒否すること。

        O_EXCL はぶら下がりリンクに対しても FileExistsError を投げるため、
        生きたリンクと同じ分岐へ落ちる。
        """
        link = tmp_path / "mem.db"
        link.symlink_to(tmp_path / "missing.db")

        with pytest.raises(DatabaseError, match="symlink"):
            Database(link)

    def test_directory_is_rejected(self, tmp_path: Path) -> None:
        """regular file 以外（ディレクトリ等）も拒否すること。"""
        target = tmp_path / "mem.db"
        target.mkdir()

        with pytest.raises(DatabaseError, match="通常ファイルではありません"):
            Database(target)

    def test_regular_file_reopen_still_works(self, tmp_path: Path) -> None:
        """通常の再オープン経路を壊していないこと。"""
        path = tmp_path / "mem.db"
        Database(path).close()

        Database(path).close()
