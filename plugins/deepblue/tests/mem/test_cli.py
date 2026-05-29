"""devgear.mem.cli のテスト"""

from __future__ import annotations

import io
import json
import runpy
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from devgear.mem import cli
from devgear.mem.database import Database, MemoryChunk
from devgear.mem.search import SearchResult


def _run_cli(
    monkeypatch,
    tmp_path: Path,
    argv: list[str],
    stdin_payload: dict,
) -> tuple[str, str]:
    import devgear.mem.settings as settings_mod

    monkeypatch.setattr(settings_mod, "_DEFAULT_DATA_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["python", *argv])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(stdin_payload)))

    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            cli.main()
        except SystemExit as exc:
            if exc.code not in (0, None):
                raise
    return stdout.getvalue(), stderr.getvalue()


def test_context_command_uses_local_db(monkeypatch, tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    db = Database(tmp_path / "mem.db")
    db.store_chunk(
        MemoryChunk(
            session_id="s1",
            project="repo",
            chunk_index=0,
            content="did some work",
            tool_names=["Edit"],
            files_read=[],
            files_modified=["file.py"],
            user_prompt="fix the bug",
            created_at_epoch=1700000000,
        )
    )
    db.close()

    stdout, stderr = _run_cli(monkeypatch, tmp_path, ["context"], {"cwd": str(repo_dir)})
    assert stderr == ""
    payload = json.loads(stdout)
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "<mem-context>" in payload["hookSpecificOutput"]["additionalContext"]
    assert "did some work" in payload["hookSpecificOutput"]["additionalContext"]


def test_search_command_returns_results(monkeypatch, tmp_path: Path) -> None:
    fake_result = SearchResult(
        chunk_id=1,
        score=0.99,
        content="direct db result",
        user_prompt="prompt",
        project="repo",
        created_at_epoch=1700000000,
        tool_names=["Read"],
        files_read=["README.md"],
        files_modified=[],
    )
    import devgear.mem.search as search_mod
    monkeypatch.setattr(search_mod.SearchService, "search", lambda self, **kwargs: [fake_result])

    stdout, stderr = _run_cli(
        monkeypatch,
        tmp_path,
        ["search"],
        {"query": "direct db", "project": "repo", "limit": 5},
    )
    assert stderr == ""
    payload = json.loads(stdout)
    assert payload["results"][0]["content"] == "direct db result"
    assert payload["results"][0]["project"] == "repo"


def test_session_init_injects_context_from_local_db(monkeypatch, tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    db = Database(tmp_path / "mem.db")
    chunk_id = db.store_chunk(
        MemoryChunk(
            session_id="s1",
            project="repo",
            chunk_index=0,
            content="previous work",
            tool_names=["Write"],
            files_read=[],
            files_modified=["src/app.py"],
            user_prompt="before",
            created_at_epoch=1700000000,
        )
    )
    db.close()

    fake_result = SearchResult(
        chunk_id=chunk_id,
        score=0.99,
        content="previous work",
        user_prompt="before",
        project="repo",
        created_at_epoch=1700000000,
        tool_names=["Write"],
        files_read=[],
        files_modified=["src/app.py"],
    )
    import devgear.mem.search as search_mod
    monkeypatch.setattr(search_mod.SearchService, "search", lambda self, **kwargs: [fake_result])

    stdout, stderr = _run_cli(
        monkeypatch,
        tmp_path,
        ["session-init"],
        {"cwd": str(repo_dir), "session_id": "session-1", "prompt": "前回のやり方を教えて"},
    )
    assert stderr == ""
    payload = json.loads(stdout)
    assert payload["hookEventName"] == "UserPromptSubmit"
    assert "<mem-context>" in payload["additionalContext"]
    assert "previous work" in payload["additionalContext"]


def test_init_command_recreates_local_db(monkeypatch, tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    db_path = tmp_path / "mem.db"
    db = Database(db_path)
    db.store_chunk(
        MemoryChunk(
            session_id="s1",
            project="repo",
            chunk_index=0,
            content="old data",
            tool_names=["Edit"],
            files_read=[],
            files_modified=["src/app.py"],
            user_prompt="before",
            created_at_epoch=1700000000,
        )
    )
    db.close()

    for suffix in ("-wal", "-shm", "-journal"):
        (tmp_path / f"mem.db{suffix}").write_text("stale", encoding="utf-8")

    stdout, stderr = _run_cli(monkeypatch, tmp_path, ["init"], {"cwd": str(repo_dir)})
    assert stderr == ""
    assert stdout == ""

    assert not (tmp_path / "mem.db-wal").exists()
    assert not (tmp_path / "mem.db-shm").exists()
    assert not (tmp_path / "mem.db-journal").exists()

    recreated = Database(db_path)
    assert recreated.get_all_chunks() == []
    recreated.close()


def test_remove_db_artifacts_handles_missing_and_directory(tmp_path: Path) -> None:
    db_path = tmp_path / "mem.db"
    db_path.write_text("db", encoding="utf-8")
    wal_dir = Path(f"{db_path}-wal")
    wal_dir.mkdir()
    (tmp_path / "mem.db-shm").unlink(missing_ok=True)
    (tmp_path / "mem.db-journal").write_text("stale", encoding="utf-8")

    cli._remove_db_artifacts(db_path)

    assert not db_path.exists()
    assert not wal_dir.exists()
    assert not (tmp_path / "mem.db-journal").exists()


def test_search_structured_with_tool_filter(monkeypatch, tmp_path: Path) -> None:
    """構造化検索: tool_name フィルタのテスト"""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    db = Database(tmp_path / "mem.db")
    # Edit ツールのチャンク
    db.store_chunk(
        MemoryChunk(
            session_id="s1",
            project="repo",
            chunk_index=0,
            content="edit work",
            tool_names=["Edit"],
            files_read=[],
            files_modified=["file.py"],
            user_prompt="edit file",
            created_at_epoch=1700000000,
        )
    )
    # Bash ツールのチャンク
    db.store_chunk(
        MemoryChunk(
            session_id="s1",
            project="repo",
            chunk_index=1,
            content="bash work",
            tool_names=["Bash"],
            files_read=[],
            files_modified=[],
            user_prompt="run test",
            created_at_epoch=1700000001,
        )
    )
    db.close()

    stdout, stderr = _run_cli(
        monkeypatch,
        tmp_path,
        ["search-structured"],
        {"cwd": str(repo_dir), "tool_name": "Edit", "limit": 10},
    )
    assert stderr == ""
    payload = json.loads(stdout)
    assert len(payload["results"]) == 1
    assert payload["results"][0]["content"] == "edit work"
    assert "Edit" in payload["results"][0]["tool_names"]


def test_search_structured_with_file_pattern(monkeypatch, tmp_path: Path) -> None:
    """構造化検索: file_pattern フィルタのテスト"""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    db = Database(tmp_path / "mem.db")
    db.store_chunk(
        MemoryChunk(
            session_id="s1",
            project="repo",
            chunk_index=0,
            content="python work",
            tool_names=["Edit"],
            files_read=[],
            files_modified=["src/main.py"],
            user_prompt="edit python",
            created_at_epoch=1700000000,
        )
    )
    db.store_chunk(
        MemoryChunk(
            session_id="s1",
            project="repo",
            chunk_index=1,
            content="js work",
            tool_names=["Edit"],
            files_read=[],
            files_modified=["src/app.js"],
            user_prompt="edit js",
            created_at_epoch=1700000001,
        )
    )
    db.close()

    stdout, stderr = _run_cli(
        monkeypatch,
        tmp_path,
        ["search-structured"],
        {"cwd": str(repo_dir), "file_pattern": "*.py", "limit": 10},
    )
    assert stderr == ""
    payload = json.loads(stdout)
    assert len(payload["results"]) == 1
    assert payload["results"][0]["content"] == "python work"


def test_record_command(monkeypatch, tmp_path: Path) -> None:
    """record コマンドのテスト"""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    stdout, stderr = _run_cli(
        monkeypatch,
        tmp_path,
        ["record"],
        {
            "cwd": str(repo_dir),
            "event_type": "review",
            "content": "Found 3 issues: XSS, SQL injection, hardcoded secret",
            "user_prompt": "review the auth module",
            "metadata": {
                "files_read": ["src/auth.py"],
                "files_modified": [],
            },
        },
    )
    assert stderr == ""
    payload = json.loads(stdout)
    assert payload["success"] is True
    assert payload["chunk_id"] is not None

    # 記録されたチャンクを確認
    db = Database(tmp_path / "mem.db")
    chunks = db.get_all_chunks()
    db.close()
    assert len(chunks) == 1
    assert chunks[0].content == "Found 3 issues: XSS, SQL injection, hardcoded secret"
    assert "review" in chunks[0].tool_names


def test_record_command_requires_content(monkeypatch, tmp_path: Path) -> None:
    """record コマンド: content 必須のテスト"""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    stdout, stderr = _run_cli(
        monkeypatch,
        tmp_path,
        ["record"],
        {"cwd": str(repo_dir), "event_type": "test", "content": ""},
    )
    assert stderr == ""
    payload = json.loads(stdout)
    assert payload["success"] is False
    assert "content is required" in payload["error"]


def test_mem_main_module_invokes_cli_main(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["python", "--help"])
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("devgear.mem.__main__", run_name="__main__")

    assert excinfo.value.code == 0


# -----------------------------------------------------------------------
# _merge_search_results_rrf のテスト
# -----------------------------------------------------------------------


def _make_result(chunk_id: str, score: float = 0.5) -> SearchResult:
    return SearchResult(
        chunk_id=chunk_id,
        score=score,
        content="",
        user_prompt="",
        project="p",
        created_at_epoch=0,
        tool_names=[],
        files_read=[],
        files_modified=[],
    )


class TestMergeSearchResultsRrf:
    def test_empty_team_returns_local_top_k(self) -> None:
        local = [_make_result(f"l{i}") for i in range(5)]
        result = cli._merge_search_results_rrf(local, [], top_k=3)
        assert [r.chunk_id for r in result] == ["l0", "l1", "l2"]

    def test_empty_both_returns_empty(self) -> None:
        result = cli._merge_search_results_rrf([], [], top_k=3)
        assert result == []

    def test_combines_local_and_team(self) -> None:
        local = [_make_result("l1"), _make_result("l2")]
        team = [_make_result("t1"), _make_result("t2")]
        result = cli._merge_search_results_rrf(local, team, top_k=3)
        assert len(result) == 3
        ids = {r.chunk_id for r in result}
        # 全部で4件のうち上位3件が選ばれる
        assert len(ids) == 3

    def test_top_k_limits_result(self) -> None:
        local = [_make_result(f"l{i}") for i in range(3)]
        team = [_make_result(f"t{i}") for i in range(3)]
        result = cli._merge_search_results_rrf(local, team, top_k=2)
        assert len(result) == 2

    def test_team_prefix_prevents_id_collision(self) -> None:
        # ローカルとチームで同じ chunk_id の文字列を持つ場合も混在する
        local = [_make_result("abc")]
        team = [_make_result("abc")]  # 同名だが "team:abc" キーになる
        result = cli._merge_search_results_rrf(local, team, top_k=2)
        assert len(result) == 2


# -----------------------------------------------------------------------
# _handle_session_init の PG 統合テスト
# -----------------------------------------------------------------------


def _patch_pg_enabled(monkeypatch, settings_mod) -> None:
    """Settings.load をパッチして sync.enabled=True / postgres_url を設定する。"""
    original_load = settings_mod.Settings.load.__func__  # type: ignore[attr-defined]

    def patched_load(cls):  # noqa: ANN001
        s = original_load(cls)
        s.sync.enabled = True
        s.sync.postgres_url = "postgresql://fake"
        return s

    monkeypatch.setattr(settings_mod.Settings, "load", classmethod(patched_load))


class TestSessionInitPgIntegration:
    def test_team_search_called_when_pg_enabled(self, monkeypatch, tmp_path: Path) -> None:
        import devgear.mem.settings as settings_mod

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        db = Database(tmp_path / "mem.db")
        chunk_id = db.store_chunk(
            MemoryChunk(
                session_id="s1",
                project="repo",
                chunk_index=0,
                content="local work",
                tool_names=["Edit"],
                files_read=[],
                files_modified=["src/x.py"],
                user_prompt="before",
                created_at_epoch=1700000000,
            )
        )
        db.close()

        fake_local = SearchResult(
            chunk_id=chunk_id,
            score=0.9,
            content="local work",
            user_prompt="before",
            project="repo",
            created_at_epoch=1700000000,
            tool_names=["Edit"],
            files_read=[],
            files_modified=["src/x.py"],
        )
        team_calls: list[bool] = []

        import devgear.mem.search as search_mod
        monkeypatch.setattr(search_mod.SearchService, "search", lambda self, **kwargs: [fake_local])
        monkeypatch.setattr(search_mod.SearchService, "search_team", lambda self, **kwargs: team_calls.append(True) or [])
        monkeypatch.setattr(settings_mod, "_DEFAULT_DATA_DIR", tmp_path)
        _patch_pg_enabled(monkeypatch, settings_mod)

        _run_cli(monkeypatch, tmp_path, ["session-init"], {"cwd": str(repo_dir), "session_id": "s2", "prompt": "前回どうしたっけ"})
        assert team_calls == [True]

    def test_local_only_when_pg_disabled(self, monkeypatch, tmp_path: Path) -> None:
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        db = Database(tmp_path / "mem.db")
        chunk_id = db.store_chunk(
            MemoryChunk(
                session_id="s1",
                project="repo",
                chunk_index=0,
                content="local only",
                tool_names=["Bash"],
                files_read=[],
                files_modified=[],
                user_prompt="before",
                created_at_epoch=1700000000,
            )
        )
        db.close()

        fake_local = SearchResult(
            chunk_id=chunk_id,
            score=0.9,
            content="local only",
            user_prompt="before",
            project="repo",
            created_at_epoch=1700000000,
            tool_names=["Bash"],
            files_read=[],
            files_modified=[],
        )
        team_calls: list[bool] = []

        import devgear.mem.search as search_mod
        monkeypatch.setattr(search_mod.SearchService, "search", lambda self, **kwargs: [fake_local])
        monkeypatch.setattr(search_mod.SearchService, "search_team", lambda self, **kwargs: team_calls.append(True) or [])

        stdout, stderr = _run_cli(
            monkeypatch,
            tmp_path,
            ["session-init"],
            {"cwd": str(repo_dir), "session_id": "s2", "prompt": "以前やった方法は？"},
        )
        assert stderr == ""
        assert team_calls == []

    def test_falls_back_to_local_on_team_error(self, monkeypatch, tmp_path: Path) -> None:
        import devgear.mem.settings as settings_mod

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        db = Database(tmp_path / "mem.db")
        chunk_id = db.store_chunk(
            MemoryChunk(
                session_id="s1",
                project="repo",
                chunk_index=0,
                content="fallback work",
                tool_names=["Write"],
                files_read=[],
                files_modified=["src/fallback.py"],
                user_prompt="before",
                created_at_epoch=1700000000,
            )
        )
        db.close()

        fake_local = SearchResult(
            chunk_id=chunk_id,
            score=0.9,
            content="fallback work",
            user_prompt="before",
            project="repo",
            created_at_epoch=1700000000,
            tool_names=["Write"],
            files_read=[],
            files_modified=["src/fallback.py"],
        )

        monkeypatch.setattr(settings_mod, "_DEFAULT_DATA_DIR", tmp_path)
        _patch_pg_enabled(monkeypatch, settings_mod)
        import devgear.mem.search as search_mod
        monkeypatch.setattr(search_mod.SearchService, "search", lambda self, **kwargs: [fake_local])
        monkeypatch.setattr(search_mod.SearchService, "search_team", lambda self, **kwargs: (_ for _ in ()).throw(RuntimeError("PG down")))

        stdout, stderr = _run_cli(
            monkeypatch,
            tmp_path,
            ["session-init"],
            {"cwd": str(repo_dir), "session_id": "s3", "prompt": "前回のやり方は？"},
        )
        assert stdout != ""
        payload = json.loads(stdout)
        assert payload["hookEventName"] == "UserPromptSubmit"
        assert "fallback work" in payload["additionalContext"]


class TestMainExitCode:
    """main() が例外発生時に exit_code=1 を返すことを確認する。"""

    def test_settings_load_failure_returns_0_for_session_start(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SESSION_START コマンドで設定失敗時はフック継続のため exit_code=0 を返す。"""
        monkeypatch.setattr(sys, "argv", ["python", "context"])
        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
        monkeypatch.setattr(
            cli, "_load_settings_or_raise", lambda: (_ for _ in ()).throw(RuntimeError("設定失敗"))
        )
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = cli.main()
        assert result == 0
        assert "設定/ログ初期化失敗" in stderr.getvalue()

    def test_settings_load_failure_returns_1_for_normal_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """通常コマンドで設定失敗時は exit_code=1 を返す。"""
        monkeypatch.setattr(sys, "argv", ["python", "search"])
        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
        monkeypatch.setattr(
            cli, "_load_settings_or_raise", lambda: (_ for _ in ()).throw(RuntimeError("設定失敗"))
        )
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = cli.main()
        assert result == 1
        assert "設定/ログ初期化失敗" in stderr.getvalue()

    def test_command_exception_returns_1(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """_run_normal_command が例外を送出すると exit_code=1 を返す。"""
        import devgear.mem.settings as settings_mod
        monkeypatch.setattr(settings_mod, "_DEFAULT_DATA_DIR", tmp_path)
        # "search" は _run_normal_command 経由（_SESSION_START_COMMANDS 外）
        monkeypatch.setattr(sys, "argv", ["python", "search"])
        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
        monkeypatch.setattr(
            cli, "_run_normal_command", lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("コマンド失敗"))
        )
        result = cli.main()
        assert result == 1

    def test_session_start_command_success_returns_0(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """SESSION_START_COMMANDS 例外でも _SESSION_START_COMMANDS 経路は exit_code=0 を維持する。"""
        import devgear.mem.settings as settings_mod
        monkeypatch.setattr(settings_mod, "_DEFAULT_DATA_DIR", tmp_path)
        monkeypatch.setattr(sys, "argv", ["python", "session-init"])
        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
        monkeypatch.setattr(
            cli, "_run_session_start_command", lambda *_a, **_kw: ""
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = cli.main()
        assert result == 0
