"""mem_helper のテスト"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from devgear.lib.mem_helper import (
    _truncate,
    format_context_for_prompt,
    get_project_stats,
    record_event,
    search_similar_context,
)


class TestSearchSimilarContext:
    """search_similar_context のテスト"""

    @patch("devgear.lib.mem_helper._run_mem_cli")
    def test_basic_search(self, mock_run: MagicMock) -> None:
        """基本的な検索が正しく動作する"""
        mock_run.return_value = {"results": [{"chunk_id": 1, "content": "test content", "user_prompt": "test"}]}

        results = search_similar_context("test query")

        mock_run.assert_called_once_with("search", {"query": "test query", "limit": 5})
        assert len(results) == 1
        assert results[0]["content"] == "test content"

    @patch("devgear.lib.mem_helper._run_mem_cli")
    def test_search_with_filters(self, mock_run: MagicMock) -> None:
        """フィルタ付き検索が search-structured を使用する"""
        mock_run.return_value = {"results": []}

        search_similar_context(
            "test query",
            project="myproject",
            limit=10,
            tool_filter="Edit",
            file_pattern="*.py",
        )

        mock_run.assert_called_once_with(
            "search-structured",
            {
                "query": "test query",
                "limit": 10,
                "project": "myproject",
                "tool_name": "Edit",
                "file_pattern": "*.py",
            },
        )

    @patch("devgear.lib.mem_helper._run_mem_cli")
    def test_search_returns_empty_on_error(self, mock_run: MagicMock) -> None:
        """エラー時は空リストを返す"""
        mock_run.return_value = {"error": "Database unavailable"}

        results = search_similar_context("test query")

        assert results == []


class TestRecordEvent:
    """record_event のテスト"""

    @patch("devgear.lib.mem_helper._run_mem_cli")
    def test_basic_record(self, mock_run: MagicMock) -> None:
        """基本的な記録が正しく動作する"""
        mock_run.return_value = {"success": True, "chunk_id": 42}

        result = record_event(
            event_type="review",
            content="Code review completed",
            user_prompt="review code",
        )

        assert result["success"] is True
        assert result["chunk_id"] == 42
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0]
        assert call_args[0] == "record"
        assert call_args[1]["event_type"] == "review"
        assert call_args[1]["content"] == "Code review completed"

    @patch("devgear.lib.mem_helper._run_mem_cli")
    def test_record_with_files(self, mock_run: MagicMock) -> None:
        """ファイル情報付きの記録"""
        mock_run.return_value = {"success": True, "chunk_id": 43}

        record_event(
            event_type="tdd",
            content="Tests added",
            files_read=["src/main.py"],
            files_modified=["tests/test_main.py"],
            project="myproject",
        )

        call_args = mock_run.call_args[0][1]
        assert call_args["project"] == "myproject"
        assert call_args["metadata"]["files_read"] == ["src/main.py"]
        assert call_args["metadata"]["files_modified"] == ["tests/test_main.py"]


class TestGetProjectStats:
    """get_project_stats のテスト"""

    @patch("devgear.lib.mem_helper.time.time", return_value=1704067200)
    @patch("devgear.lib.mem_helper.Database")
    @patch("devgear.lib.mem_helper.Settings.load")
    @patch("devgear.lib.mem_helper.Path.cwd")
    def test_basic_stats(
        self,
        mock_cwd: MagicMock,
        mock_load: MagicMock,
        mock_db: MagicMock,
        mock_time: MagicMock,  # noqa: ARG002
    ) -> None:
        """基本的な統計取得"""
        mock_cwd.return_value = Path("/workspace/myproject")
        mock_load.return_value = SimpleNamespace(db_path=Path("/tmp/mem.db"))
        mock_db.return_value = SimpleNamespace(
            get_all_chunks=lambda: [
                SimpleNamespace(
                    project="myproject",
                    created_at_epoch=1704067200,
                    tool_names=["Edit", "Bash"],
                    files_modified=["src/app.py"],
                    access_count=1,
                ),
                SimpleNamespace(
                    project="myproject",
                    created_at_epoch=1600000000,
                    tool_names=["Read"],
                    files_modified=["README.md"],
                    access_count=0,
                ),
            ],
            close=lambda: None,
        )

        result = get_project_stats()

        assert result["project"] == "myproject"
        assert result["total_chunks"] == 2
        assert result["recent_chunks"] == 1
        assert result["top_tools"] == {"Edit": 1, "Bash": 1}
        assert result["top_files"] == {"src/app.py": 1}

    @patch("devgear.lib.mem_helper.Database")
    @patch("devgear.lib.mem_helper.Settings.load")
    @patch("devgear.lib.mem_helper.Path.cwd")
    def test_stats_with_project(self, mock_cwd: MagicMock, mock_load: MagicMock, mock_db: MagicMock) -> None:
        """プロジェクト指定での統計取得"""
        mock_cwd.return_value = Path("/workspace/ignored")
        mock_load.return_value = SimpleNamespace(db_path=Path("/tmp/mem.db"))
        mock_db.return_value = SimpleNamespace(get_all_chunks=lambda: [], close=lambda: None)

        result = get_project_stats(project="myproject", days=7)

        assert result["project"] == "myproject"


class TestFormatContextForPrompt:
    """format_context_for_prompt のテスト"""

    def test_empty_results(self) -> None:
        """空の結果に対しては空文字列を返す"""
        assert format_context_for_prompt([]) == ""

    def test_basic_format(self) -> None:
        """基本的なフォーマット"""
        results = [
            {
                "user_prompt": "implement feature X",
                "content": "Added new feature",
                "tool_names": ["Edit", "Bash"],
                "files_modified": ["src/feature.py"],
            }
        ]

        output = format_context_for_prompt(results)

        assert "## 関連する過去の作業" in output
        assert "implement feature X" in output
        assert "Edit" in output
        assert "src/feature.py" in output
        assert "Added new feature" in output

    def test_max_results(self) -> None:
        """max_results で結果数を制限"""
        results = [{"user_prompt": f"prompt {i}", "content": f"content {i}"} for i in range(10)]

        output = format_context_for_prompt(results, max_results=3)

        assert "prompt 0" in output
        assert "prompt 2" in output
        assert "prompt 3" not in output

    def test_content_truncation(self) -> None:
        """長いコンテンツは切り詰められる"""
        results = [
            {
                "user_prompt": "test",
                "content": "A" * 1000,
            }
        ]

        output = format_context_for_prompt(results, max_content_length=100)

        # 100文字 + "..." = 100文字（_truncateの実装通り）
        assert "A" * 97 + "..." in output
        assert "A" * 1000 not in output


class TestTruncate:
    """_truncate のテスト"""

    def test_short_text(self) -> None:
        """短いテキストはそのまま"""
        assert _truncate("hello", 10) == "hello"

    def test_exact_length(self) -> None:
        """ちょうどの長さはそのまま"""
        assert _truncate("hello", 5) == "hello"

    def test_long_text(self) -> None:
        """長いテキストは切り詰め"""
        assert _truncate("hello world", 8) == "hello..."

    def test_very_short_max(self) -> None:
        """非常に短い最大長"""
        assert _truncate("hello", 3) == "..."


class TestRunMemCliIntegration:
    """_run_mem_cli の統合テスト（実際のサブプロセスを使用した動作確認）"""

    def test_invalid_command(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """無効なコマンドに対してエラーを返す"""
        import os

        from devgear.lib.mem_helper import _run_mem_cli

        monkeypatch.setitem(os.environ, "DEVGEAR_DATA_PATH", str(tmp_path))
        result = _run_mem_cli("invalid_command", {})

        # エラーが返されるか、空の結果が返される
        assert "error" in result or result == {}

    def test_timeout_handling(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """タイムアウト処理"""
        import os

        from devgear.lib.mem_helper import _run_mem_cli

        monkeypatch.setitem(os.environ, "DEVGEAR_DATA_PATH", str(tmp_path))
        # 正常なコマンドはタイムアウトしない
        result = _run_mem_cli("sync-status", {})
        # タイムアウトエラーではないことを確認
        assert result.get("error") != "Timeout"


class TestRunMemCliPaths:
    """_run_mem_cli の各パス（成功・各例外）のユニットテスト"""

    def test_success_path(self, monkeypatch) -> None:
        """正常終了時はJSONをパースして返す"""
        from devgear.lib import mem_helper

        monkeypatch.setattr(
            mem_helper.subprocess,
            "run",
            lambda *args, **kwargs: type("Result", (), {"returncode": 0, "stdout": '{"results": [1]}', "stderr": ""})(),
        )
        assert mem_helper._run_mem_cli("search", {}) == {"results": [1]}

    def test_timeout_path(self, monkeypatch) -> None:
        """TimeoutExpired 発生時は {"error": "Timeout"} を返す"""
        from devgear.lib import mem_helper

        monkeypatch.setattr(
            mem_helper.subprocess,
            "run",
            lambda *args, **kwargs: (_ for _ in ()).throw(mem_helper.subprocess.TimeoutExpired(cmd="mem", timeout=30)),
        )
        assert mem_helper._run_mem_cli("search", {}) == {"error": "Timeout"}

    def test_invalid_json_path(self, monkeypatch) -> None:
        """JSONデコード失敗時は {"error": "Invalid JSON response"} を返す"""
        from devgear.lib import mem_helper

        monkeypatch.setattr(
            mem_helper.subprocess,
            "run",
            lambda *args, **kwargs: type("Result", (), {"returncode": 0, "stdout": "not json", "stderr": ""})(),
        )
        assert mem_helper._run_mem_cli("search", {}) == {"error": "Invalid JSON response"}

    def test_generic_exception_path(self, monkeypatch) -> None:
        """その他の例外発生時はエラーメッセージを返す"""
        from devgear.lib import mem_helper

        monkeypatch.setattr(
            mem_helper.subprocess,
            "run",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert mem_helper._run_mem_cli("search", {}) == {"error": "boom"}
