"""cli_digest_handlers（digest-backfill コマンド）のテスト。"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import bluecore.mem.cli as cli
import bluecore.mem.digest as digest_mod
from bluecore.mem.cli_digest_handlers import DigestBackfillDeps, handle_digest_backfill
from bluecore.mem.database import Database, MemoryChunk
from tests.mem.conftest import make_settings

_LOG = SimpleNamespace(warning=lambda *a, **k: None, error=lambda *a, **k: None, info=lambda *a, **k: None)


@contextmanager
def _keep_open(db: Database):
    """既存の Database 接続をクローズせずに再利用する（テスト専用の open_db 差し替え）。"""
    yield db


def _seed_chunk(db: Database, session_id: str, *, project: str = "proj", epoch: int = 1700000000) -> None:
    """digest-backfill 対象になるチャンクを1件保存する。"""
    db.store_chunk(
        MemoryChunk(
            session_id=session_id,
            project=project,
            chunk_index=0,
            content="did some work",
            tool_names=["Edit"],
            files_read=[],
            files_modified=["a.py"],
            user_prompt="fix the bug",
            created_at_epoch=epoch,
        )
    )


def _make_deps(db: Database) -> DigestBackfillDeps:
    """テスト用の DigestBackfillDeps を組み立てる。"""
    return DigestBackfillDeps(open_db=lambda settings: _keep_open(db), log=_LOG)


def _write_claude_transcript(home: Path, session_id: str, text: str = "final response") -> Path:
    """偽の claude transcript（~/.claude/projects/<dir>/<sid>.jsonl）を作成する。"""
    project_dir = home / ".claude" / "projects" / "some-project"
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / f"{session_id}.jsonl"
    path.write_text(
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}),
        encoding="utf-8",
    )
    return path


def _write_copilot_transcript(home: Path, session_id: str, text: str = "copilot response") -> Path:
    """偽の copilot transcript（~/.copilot/session-state/<sid>/events.jsonl）を作成する。"""
    session_dir = home / ".copilot" / "session-state" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / "events.jsonl"
    path.write_text(
        json.dumps({"type": "assistant.message", "data": {"content": text}}),
        encoding="utf-8",
    )
    return path


@pytest.fixture(autouse=True)
def _patch_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Path.home() をテスト用ディレクトリ（トランスクリプト置き場）に固定する。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "mem.db")
    yield database
    database.close()


class TestHandleDigestBackfillCounts:
    """作成/更新/縮退/スキップの各カウント検証。"""

    def test_created_with_claude_transcript(
        self, db: Database, tmp_path: Path, capsys: pytest.CaptureFixture[str], _patch_home: Path
    ) -> None:
        """claude transcript が見つかる場合、生成され source=transcript+chunks になる。"""
        _seed_chunk(db, "sess-1")
        _write_claude_transcript(_patch_home, "sess-1", text="final claude response")

        handle_digest_backfill(make_settings(tmp_path), {}, _make_deps(db))

        out = capsys.readouterr().out
        assert "生成=1 更新=0 縮退=0 スキップ=0" in out
        digest = db.get_digest_by_session("sess-1")
        assert digest is not None
        assert digest.source == "transcript+chunks"
        assert digest.harness == "claude"

    def test_created_degraded_without_transcript(
        self, db: Database, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """トランスクリプトが見つからない場合、生成はされるが縮退カウントされる。"""
        _seed_chunk(db, "sess-1")

        handle_digest_backfill(make_settings(tmp_path), {}, _make_deps(db))

        out = capsys.readouterr().out
        assert "生成=1 更新=0 縮退=1 スキップ=0" in out
        digest = db.get_digest_by_session("sess-1")
        assert digest is not None
        assert digest.source == "chunks"
        assert digest.harness == "unknown"

    def test_created_with_copilot_transcript(
        self, db: Database, tmp_path: Path, capsys: pytest.CaptureFixture[str], _patch_home: Path
    ) -> None:
        """copilot transcript が見つかる場合、生成され harness=copilot になる。"""
        _seed_chunk(db, "sess-1")
        _write_copilot_transcript(_patch_home, "sess-1", text="final copilot response")

        handle_digest_backfill(make_settings(tmp_path), {}, _make_deps(db))

        digest = db.get_digest_by_session("sess-1")
        assert digest is not None
        assert digest.source == "transcript+chunks"
        assert digest.harness == "copilot"

    def test_skipped_when_existing_and_not_force(
        self, db: Database, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """既存 digest があり force=false ならスキップされる。"""
        _seed_chunk(db, "sess-1")
        handle_digest_backfill(make_settings(tmp_path), {}, _make_deps(db))
        capsys.readouterr()  # 1回目の出力を捨てる

        handle_digest_backfill(make_settings(tmp_path), {"force": False}, _make_deps(db))

        out = capsys.readouterr().out
        assert "生成=0 更新=0 縮退=0 スキップ=1" in out

    def test_force_updates_existing_and_preserves_row(
        self, db: Database, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """force=true なら既存 digest を更新し、行は1件のまま（重複しない）。"""
        _seed_chunk(db, "sess-1")
        handle_digest_backfill(make_settings(tmp_path), {}, _make_deps(db))
        capsys.readouterr()
        original = db.get_digest_by_session("sess-1")
        assert original is not None

        handle_digest_backfill(make_settings(tmp_path), {"force": True}, _make_deps(db))

        out = capsys.readouterr().out
        assert "生成=0 更新=1 縮退=1 スキップ=0" in out
        updated = db.get_digest_by_session("sess-1")
        assert updated is not None
        assert updated.id == original.id
        count = db.conn.execute(
            "SELECT COUNT(*) as c FROM session_digests WHERE session_id = ?", ("sess-1",)
        ).fetchone()["c"]
        assert count == 1

    def test_force_updates_multiple_sessions(
        self, db: Database, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """force=true で複数セッションが続けて更新される（ループ継続分岐のカバレッジ）。"""
        _seed_chunk(db, "sess-1")
        _seed_chunk(db, "sess-2", epoch=1700000001)
        handle_digest_backfill(make_settings(tmp_path), {}, _make_deps(db))
        capsys.readouterr()

        handle_digest_backfill(make_settings(tmp_path), {"force": True}, _make_deps(db))

        out = capsys.readouterr().out
        assert "更新=2" in out

    def test_project_filter(self, db: Database, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """project 指定時はそのプロジェクトのセッションのみ処理する。"""
        _seed_chunk(db, "sess-a", project="proj-a")
        _seed_chunk(db, "sess-b", project="proj-b")

        handle_digest_backfill(make_settings(tmp_path), {"project": "proj-a"}, _make_deps(db))

        out = capsys.readouterr().out
        assert "生成=1" in out
        assert db.get_digest_by_session("sess-a") is not None
        assert db.get_digest_by_session("sess-b") is None

    def test_limit_stops_processing(self, db: Database, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """limit 指定時はその件数で打ち切る。"""
        for i in range(3):
            _seed_chunk(db, f"sess-{i}", epoch=1700000000 + i)

        handle_digest_backfill(make_settings(tmp_path), {"limit": 1}, _make_deps(db))

        total_digests = db.conn.execute("SELECT COUNT(*) as c FROM session_digests").fetchone()["c"]
        assert total_digests == 1

    def test_session_level_exception_continues(
        self, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """1セッションの build_session_digest が例外を投げても他セッションの処理は継続する。"""
        _seed_chunk(db, "sess-good-1")
        _seed_chunk(db, "sess-bad")
        _seed_chunk(db, "sess-good-2")

        real_build = digest_mod.build_session_digest

        def _flaky_build(db_arg, session_id, **kwargs):  # noqa: ANN001
            if session_id == "sess-bad":
                raise RuntimeError("broken transcript")
            return real_build(db_arg, session_id, **kwargs)

        monkeypatch.setattr(digest_mod, "build_session_digest", _flaky_build)

        warnings: list[str] = []
        log = SimpleNamespace(
            warning=lambda msg, *a, **k: warnings.append(msg % a if a else msg),
            info=lambda *a, **k: None,
            error=lambda *a, **k: None,
        )
        deps = DigestBackfillDeps(open_db=lambda settings: _keep_open(db), log=log)

        handle_digest_backfill(make_settings(tmp_path), {}, deps)

        assert any("sess-bad" in warning for warning in warnings)
        assert db.get_digest_by_session("sess-good-1") is not None
        assert db.get_digest_by_session("sess-good-2") is not None
        assert db.get_digest_by_session("sess-bad") is None
        out = capsys.readouterr().out
        assert "生成=2" in out

    def test_build_session_digest_returns_none_counts_as_skipped(
        self, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """build_session_digest が None を返す場合（例: 競合でチャンク消滅）はスキップ扱い。"""
        _seed_chunk(db, "sess-1")
        monkeypatch.setattr(digest_mod, "build_session_digest", lambda *a, **k: None)

        handle_digest_backfill(make_settings(tmp_path), {}, _make_deps(db))

        out = capsys.readouterr().out
        assert "生成=0 更新=0 縮退=0 スキップ=1" in out
        assert db.get_digest_by_session("sess-1") is None

    def test_idempotent_rerun_all_skipped(
        self, db: Database, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """2回目の実行（force なし）はすべてスキップされる（冪等性）。"""
        _seed_chunk(db, "sess-1")
        _seed_chunk(db, "sess-2", epoch=1700000001)

        handle_digest_backfill(make_settings(tmp_path), {}, _make_deps(db))
        first_out = capsys.readouterr().out
        assert "生成=2" in first_out

        handle_digest_backfill(make_settings(tmp_path), {}, _make_deps(db))
        second_out = capsys.readouterr().out
        # スキップ済みセッションはトランスクリプト有無を再判定しないため縮退カウントされない
        assert "生成=0 更新=0 縮退=0 スキップ=2" in second_out

    def test_no_sessions_at_all(self, db: Database, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """対象セッションが1件も無い場合は全カウント0。"""
        handle_digest_backfill(make_settings(tmp_path), {}, _make_deps(db))
        out = capsys.readouterr().out
        assert "生成=0 更新=0 縮退=0 スキップ=0" in out

    def test_open_db_failure_logs_warning(self, tmp_path: Path) -> None:
        """DB オープン自体が失敗しても例外を伝播させず warning のみ。"""
        warnings: list[str] = []
        log = SimpleNamespace(
            warning=lambda msg, *a, **k: warnings.append(msg % a if a else msg),
            info=lambda *a, **k: None,
            error=lambda *a, **k: None,
        )

        @contextmanager
        def _boom(settings):  # noqa: ANN001
            raise RuntimeError("db boom")
            yield  # pragma: no cover

        deps = DigestBackfillDeps(open_db=_boom, log=log)
        handle_digest_backfill(make_settings(tmp_path), {}, deps)

        assert any("digest-backfill 失敗" in warning for warning in warnings)


class TestCliDigestBackfillWiring:
    """cli.py への登録（ハンドラ配線）のテスト。"""

    def test_registered_in_command_handlers(self) -> None:
        assert "digest-backfill" in cli._COMMAND_HANDLERS

    def test_help_text_mentions_digest_backfill(self) -> None:
        assert "digest-backfill" in cli.HELP_TEXT

    def test_handle_digest_backfill_wrapper_invokes_handler(
        self, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """cli._handle_digest_backfill が実際のハンドラを呼び出す。"""
        _seed_chunk(db, "sess-1")
        monkeypatch.setattr(cli, "_open_db", lambda settings: _keep_open(db))

        cli._handle_digest_backfill(make_settings(tmp_path), {})

        out = capsys.readouterr().out
        assert "digest-backfill:" in out
        assert db.get_digest_by_session("sess-1") is not None
