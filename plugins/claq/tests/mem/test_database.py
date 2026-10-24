"""claq.mem.database — repos / sessions / knowledge の 3 テーブルのテスト。"""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from claq.mem.database import Database, DatabaseError
from claq.mem.models import Knowledge, Repo, Session, utc_now_iso

_TS = "2026-01-01T00:00:00+00:00"


@pytest.fixture
def db(tmp_path: Path) -> Database:
    """テスト用の空 DB を開いて返す。"""
    database = Database(tmp_path / "mem.db")
    yield database
    database.close()


def _repo(repo_id: str = "claq-dev", identity_key: str = "git@github.com:x/claq.git") -> Repo:
    """テスト用 Repo を組み立てる。"""
    return Repo(
        id=repo_id,
        identity_key=identity_key,
        root_path="/Users/x/dev/claq-dev",
        remote_url="git@github.com:x/claq.git",
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
        # status は必須引数（既定値なし）。DB 層のテストは注入対象の挙動を見る
        # ものが多いため、ヘルパの既定は 'active' に置く。
        "status": "active",
        "created_at": _TS,
        "updated_at": _TS,
    }
    params.update(overrides)
    return Knowledge(**params)


class TestSchema:
    """DDL が意図通りのテーブル・制約・PRAGMA を作ることを確認する。"""

    def test_only_three_plain_tables(self, db: Database) -> None:
        """実テーブルは repos / sessions / knowledge だけで、FTS5 等の仮想テーブルと同期トリガは無い。"""
        master = db.conn.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        assert sorted(r["name"] for r in master if r["type"] == "table") == ["knowledge", "repos", "sessions"]
        assert [r["name"] for r in master if r["type"] == "trigger" or "VIRTUAL TABLE" in (r["sql"] or "")] == []

    @pytest.mark.parametrize(
        ("pragma", "expected"),
        [("foreign_keys", 1), ("journal_mode", "wal"), ("temp_store", 2), ("mmap_size", 268435456)],
    )
    def test_pragmas_applied(self, db: Database, pragma: str, expected: object) -> None:
        """外部キー・WAL・最適化 PRAGMA が適用されている。"""
        assert db.conn.execute(f"PRAGMA {pragma}").fetchone()[0] == expected

    @pytest.mark.parametrize(
        "overrides",
        [
            {"scope": "repo", "repo_id": None},
            {"scope": "global", "repo_id": "claq-dev"},
            {"kind": "rumor"},
            {"source": "oracle"},
            {"confidence": 1.5},
        ],
        ids=["repo-without-repo_id", "global-with-repo_id", "invalid-kind", "invalid-source", "confidence-over-1"],
    )
    def test_check_constraints_reject(self, db: Database, overrides: dict[str, object]) -> None:
        """scope と repo_id の整合・kind / source の許容値・confidence の 0〜1 を CHECK 制約で弾く。"""
        db.upsert_repo(_repo())
        with pytest.raises(sqlite3.IntegrityError):
            db.upsert_knowledge(_knowledge(**overrides))

    def test_status_column_defaults_to_pending(self, db: Database) -> None:
        """status を省略した INSERT は 'pending'（注入されない側）に落ちる。

        「agent 由来カードは人間の promote を経なければ注入されない」原則を
        支えるのが Python 検証 1 層だけだと、status を省略する経路が 1 本増えた
        瞬間に破れる。永続層の既定を fail-safe 側に据えて多層防御にする。

        本番経路は status を必ず明示するため、この既定に到達するのは直 SQL の
        みである。ゆえに検証も直 SQL で行う。
        """
        db.conn.execute(
            """INSERT INTO knowledge
                 (key, scope, kind, title, source, created_at, updated_at)
               VALUES ('no-status', 'global', 'fact', 'status 省略', 'agent', ?, ?)""",
            (_TS, _TS),
        )
        db.conn.commit()

        row = db.conn.execute("SELECT status FROM knowledge WHERE key = 'no-status'").fetchone()
        assert row["status"] == "pending"

    def test_repo_delete_cascades_to_knowledge_and_sessions(self, db: Database) -> None:
        """repos の削除は repo スコープの knowledge と sessions を CASCADE 削除する。"""
        db.upsert_repo(_repo())
        db.upsert_knowledge(_knowledge(scope="repo", repo_id="claq-dev"))
        db.start_session(Session(session_uid="uid-1", repo_id="claq-dev"))
        db.conn.execute("DELETE FROM repos WHERE id = ?", ("claq-dev",))
        db.conn.commit()
        assert db.list_knowledge() == []
        assert db.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0

    def test_session_delete_nulls_knowledge_link(self, db: Database) -> None:
        """sessions の削除で knowledge.session_id は NULL になる（SET NULL）。"""
        db.upsert_repo(_repo())
        session = db.start_session(Session(session_uid="uid-1", repo_id="claq-dev"))
        db.upsert_knowledge(_knowledge(session_id=session.id))
        db.conn.execute("DELETE FROM sessions WHERE id = ?", (session.id,))
        db.conn.commit()
        assert db.get_knowledge_by_key("use-python3").session_id is None


class TestConnection:
    """接続・初期化・後始末・パス検証の挙動。"""

    def test_db_file_is_0600_on_create_and_reopen(self, tmp_path: Path) -> None:
        """新規作成時の DB ファイルは 0600。既存 DB が 0600 以外なら開き直すたびに 0600 へ補正する。

        新規作成時にしか chmod しないと、既存 DB は 0644 のまま
        放置される。他ユーザーが読めるファイルを残さないよう、
        接続のたびに mode を検証・補正する。
        """
        db_path = tmp_path / "nested" / "mem.db"
        Database(db_path).close()
        assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
        db_path.chmod(0o644)
        Database(db_path).close()
        assert stat.S_IMODE(db_path.stat().st_mode) == 0o600

    def test_parent_dir_under_claq_is_0700(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """~/.claq 配下に作る DB の親ディレクトリは umask 022 でも 0700。"""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CLAQ_HOME", raising=False)
        old_umask = os.umask(0o022)
        try:
            Database(tmp_path / ".claq" / "mem.db").close()
            assert stat.S_IMODE((tmp_path / ".claq").stat().st_mode) == 0o700
        finally:
            os.umask(old_umask)

    def test_context_manager_closes_connection(self, tmp_path: Path) -> None:
        """with を抜けると接続が閉じている。"""
        with Database(tmp_path / "mem.db") as database:
            assert isinstance(database, Database)
        with pytest.raises(sqlite3.ProgrammingError):
            database.conn.execute("SELECT 1")

    @pytest.mark.parametrize("live", [True, False], ids=["live-symlink", "dangling-symlink"])
    def test_symlink_is_rejected_before_connect(self, tmp_path: Path, live: bool) -> None:
        """`mem.db -> target.db` を張られた状態で開くと拒否し、リンク先を汚染も作成もしないこと。

        sqlite3.connect は symlink を追うため、拒否しないとリンク先へ
        repos/sessions/knowledge が作られ、無関係な DB を壊す。O_EXCL は
        ぶら下がりリンクにも FileExistsError を投げるため、生きたリンクと同じ分岐へ落ちる。
        """
        target = tmp_path / "target.db"
        if live:
            target.write_text("", encoding="utf-8")
        link = tmp_path / "mem.db"
        link.symlink_to(target)

        with pytest.raises(DatabaseError, match="symlink"):
            Database(link)

        assert target.exists() is live
        assert not live or target.stat().st_size == 0

    def test_directory_is_rejected(self, tmp_path: Path) -> None:
        """regular file 以外（ディレクトリ等）も拒否すること。"""
        target = tmp_path / "mem.db"
        target.mkdir()

        with pytest.raises(DatabaseError, match="通常ファイルではありません"):
            Database(target)


class TestRepos:
    """repos の CRUD。"""

    def test_upsert_inserts_then_resolves_conflict_by_identity_key(self, db: Database) -> None:
        """新規は格納値を返し、identity_key が一致すれば id と first_seen_at を保持して観測情報のみ更新する。"""
        assert db.upsert_repo(_repo()) == _repo()
        moved = Repo(
            id="別のスラッグ",
            identity_key="git@github.com:x/claq.git",
            root_path="/Users/x/worktrees/feature",
            remote_url=None,
            first_seen_at="2026-06-01T00:00:00+00:00",
            last_seen_at="2026-06-01T00:00:00+00:00",
        )
        stored = db.upsert_repo(moved)
        assert stored.id == "claq-dev"
        assert stored.first_seen_at == _TS
        assert stored.root_path == "/Users/x/worktrees/feature"
        assert stored.remote_url is None
        assert stored.last_seen_at == "2026-06-01T00:00:00+00:00"
        assert len(db.list_repos()) == 1

    def test_list_repos_orders_by_last_seen_desc(self, db: Database) -> None:
        """1 件も無ければ空リスト、あれば最終観測の新しい順に返す（挿入順・id 順とは独立）。"""
        assert db.list_repos() == []
        for repo_id, month in (("a-old", "01"), ("b-newest", "07"), ("c-mid", "03")):
            repo = _repo(repo_id, f"key-{repo_id}")
            repo.last_seen_at = f"2026-{month}-01T00:00:00+00:00"
            db.upsert_repo(repo)
        assert [repo.id for repo in db.list_repos()] == ["b-newest", "c-mid", "a-old"]

    def test_relink_repo_identity_reports_missing_row(self, db: Database) -> None:
        """存在しない id の載せ替えは False を返すこと。"""
        assert db.relink_repo_identity("no-such-repo", "github.com/o/r") is False


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

    @pytest.mark.parametrize("scope", ["global", "repo"])
    def test_upsert_updates_on_key_conflict_keeping_id_and_created_at(self, db: Database, scope: str) -> None:
        """同一スコープ（同一 repo）で同じ key の再投入は 1 行に集約し、id と created_at を保持して内容を更新する。"""
        db.upsert_repo(_repo())
        repo_id = "claq-dev" if scope == "repo" else None
        first = db.upsert_knowledge(_knowledge(scope=scope, repo_id=repo_id))
        second = db.upsert_knowledge(
            _knowledge(
                scope=scope,
                repo_id=repo_id,
                title="上書き",
                body="venv は ~/.claq/.venv のみ",
                confidence=0.9,
                updated_at="2026-02-02T00:00:00+00:00",
            )
        )
        assert second.id == first.id
        assert second.created_at == _TS
        assert second.updated_at == "2026-02-02T00:00:00+00:00"
        assert second.confidence == 0.9
        assert [row.title for row in db.list_knowledge()] == ["上書き"]

    def test_same_key_coexists_across_scopes_and_get_by_key_picks_scope(self, db: Database) -> None:
        """global と repo で同じ key を並存でき（式インデックスの境界）、get は repo_id でスコープを選ぶ。"""
        db.upsert_repo(_repo())
        db.upsert_knowledge(_knowledge(scope="repo", repo_id="claq-dev", title="repo 側"))
        db.upsert_knowledge(_knowledge(title="global 側"))
        assert len(db.list_knowledge()) == 2
        global_row = db.get_knowledge_by_key("use-python3")
        assert (global_row.scope, global_row.title) == ("global", "global 側")
        assert db.get_knowledge_by_key("use-python3", repo_id="claq-dev").title == "repo 側"
        assert db.get_knowledge_by_key("nope") is None

    def test_upsert_persists_all_optional_columns(self, db: Database) -> None:
        """任意カラムがすべて往復する。"""
        db.upsert_repo(_repo())
        session = db.start_session(Session(session_uid="uid-1", repo_id="claq-dev"))
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
                source_ref="plugins/claq/src/claq/mem/database.py",
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

    def test_list_without_filters_returns_all_newest_first(self, db: Database) -> None:
        """絞り込み無しは全件を更新の新しい順で返す（挿入順・id 順とは独立）。"""
        db.upsert_knowledge(_knowledge("a", updated_at=_TS))
        db.upsert_knowledge(_knowledge("b", updated_at="2026-03-01T00:00:00+00:00"))
        db.upsert_knowledge(_knowledge("c", updated_at="2026-02-01T00:00:00+00:00"))
        assert [row.key for row in db.list_knowledge()] == ["b", "c", "a"]

    def test_list_with_all_filters(self, db: Database) -> None:
        """scope・repo_id・status のすべてで絞り込む。"""
        db.upsert_repo(_repo())
        db.upsert_repo(_repo("other", "key-other"))
        db.upsert_knowledge(_knowledge("hit", scope="repo", repo_id="claq-dev"))
        db.upsert_knowledge(_knowledge("wrong-repo", scope="repo", repo_id="other"))
        db.upsert_knowledge(_knowledge("wrong-scope"))
        db.upsert_knowledge(_knowledge("wrong-status", scope="repo", repo_id="claq-dev", status="archived"))
        rows = db.list_knowledge(scope="repo", repo_id="claq-dev", status="active")
        assert [row.key for row in rows] == ["hit"]

    @pytest.mark.parametrize("updated_at", ["2026-05-05T00:00:00+00:00", None], ids=["explicit", "default-now"])
    def test_set_status_updates_row(self, db: Database, updated_at: str | None) -> None:
        """status と updated_at（省略時は現在時刻）を更新して True を返す。"""
        stored = db.upsert_knowledge(_knowledge())
        before = utc_now_iso()
        assert db.set_knowledge_status(stored.id, "archived", updated_at=updated_at) is True
        after = db.get_knowledge_by_key("use-python3")
        assert after.status == "archived"
        if updated_at is None:
            assert before <= after.updated_at <= utc_now_iso()
        else:
            assert after.updated_at == updated_at

    def test_set_status_returns_false_when_missing(self, db: Database) -> None:
        """該当行が無ければ False。"""
        assert db.set_knowledge_status(9999, "archived") is False


class TestSessions:
    """sessions の CRUD。"""

    def test_start_session_inserts_then_is_idempotent(self, db: Database) -> None:
        """開始登録で id を採番し、同一 session_uid の再入は started_at を保持し harness だけ更新する。"""
        db.upsert_repo(_repo())
        first = db.start_session(Session(session_uid="uid-1", repo_id="claq-dev", harness="claude", started_at=_TS))
        assert first.id is not None
        assert first.harness == "claude"
        assert first.handoff == ""
        assert first.ended_at is None
        second = db.start_session(
            Session(session_uid="uid-1", repo_id="claq-dev", harness="codex", started_at="2026-09-09T00:00:00+00:00")
        )
        assert second.id == first.id
        assert second.started_at == _TS
        assert second.harness == "codex"

    def test_start_session_requires_existing_repo(self, db: Database) -> None:
        """未登録 repo_id は外部キー制約で弾かれる。"""
        with pytest.raises(sqlite3.IntegrityError):
            db.start_session(Session(session_uid="uid-1", repo_id="missing"))

    @pytest.mark.parametrize("ended_at", ["2026-01-02T00:00:00+00:00", None], ids=["explicit", "default-now"])
    def test_finish_session_records_handoff(self, db: Database, ended_at: str | None) -> None:
        """handoff と ended_at（省略時は現在時刻）を記録して True を返す。"""
        db.upsert_repo(_repo())
        db.start_session(Session(session_uid="uid-1", repo_id="claq-dev"))
        before = utc_now_iso()
        assert db.finish_session("uid-1", "次はテストを書く", ended_at=ended_at) is True
        latest = db.get_latest_session("claq-dev")
        assert latest.handoff == "次はテストを書く"
        if ended_at is None:
            assert before <= latest.ended_at <= utc_now_iso()
        else:
            assert latest.ended_at == ended_at

    def test_finish_session_returns_false_when_missing(self, db: Database) -> None:
        """未登録の session_uid では False。"""
        assert db.finish_session("unknown", "引き継ぎ") is False

    def test_get_latest_session_returns_newest_with_handoff(self, db: Database) -> None:
        """handoff が空のセッションは対象外で、引き継ぎを持つ中で開始の最も新しい 1 件を返す。"""
        db.upsert_repo(_repo())
        db.start_session(Session(session_uid="uid-empty", repo_id="claq-dev", started_at="2026-04-01T00:00:00+00:00"))
        assert db.get_latest_session("claq-dev") is None
        for uid, month in (("uid-1", "01"), ("uid-2", "03"), ("uid-3", "02")):
            db.start_session(Session(session_uid=uid, repo_id="claq-dev", started_at=f"2026-{month}-01T00:00:00+00:00"))
            db.finish_session(uid, f"handoff for {uid}")
        assert db.get_latest_session("claq-dev").session_uid == "uid-2"
