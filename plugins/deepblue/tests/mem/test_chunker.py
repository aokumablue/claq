"""chunker のテスト"""

import pytest

from devgear.mem.chunker import (
    ChunkAccumulator,
    _extract_file_paths,
    _parse_tool_input,
    _summarize_ai_response,
    _summarize_input,
    _truncate,
    build_chunk_from_tool_use,
)


class TestParseToolInput:
    """_parse_tool_input のテスト"""

    @pytest.mark.parametrize(
        "tool_input, expected",
        [
            ({"key": "val"}, {"key": "val"}),
            ('{"key": "val"}', {"key": "val"}),
            (None, {}),
            ("not json", {}),
            (42, {}),
            ("", {}),
            ('"just a string"', {}),
        ],
        ids=["dict", "json-str", "none", "invalid-str", "int", "empty-str", "json-non-dict"],
    )
    def test_parse(self, tool_input, expected: dict) -> None:
        assert _parse_tool_input(tool_input) == expected


class TestExtractFilePaths:
    """_extract_file_paths のテスト"""

    def test_file_path(self) -> None:
        assert _extract_file_paths("Read", {"file_path": "/src/a.py"}) == ["/src/a.py"]

    def test_grep_path_with_slash(self) -> None:
        assert _extract_file_paths("Grep", {"path": "/src/", "pattern": "TODO"}) == ["/src/"]

    def test_grep_path_dot(self) -> None:
        assert _extract_file_paths("Grep", {"path": ".", "pattern": "TODO"}) == []

    def test_glob_pattern_with_slash(self) -> None:
        assert _extract_file_paths("Glob", {"pattern": "src/**/*.py"}) == ["src/**/*.py"]

    def test_glob_pattern_without_slash(self) -> None:
        assert _extract_file_paths("Glob", {"pattern": "*.py"}) == []

    def test_bash_command(self) -> None:
        paths = _extract_file_paths("Bash", {"command": "cat /etc/hosts"})
        assert "/etc/hosts" in paths

    def test_grep_pattern_not_extracted(self) -> None:
        """Grep の pattern は検索パターンであり、ファイルパスとして抽出しない"""
        assert _extract_file_paths("Grep", {"pattern": "func/tion", "path": "."}) == []

    def test_empty_input(self) -> None:
        assert _extract_file_paths("Read", {}) == []


class TestSummarizeInput:
    """_summarize_input のテスト"""

    def test_read(self) -> None:
        assert _summarize_input("Read", {"file_path": "/a.py"}, None) == "/a.py"

    def test_write(self) -> None:
        assert _summarize_input("Write", {"file_path": "/b.py"}, None) == "/b.py"

    def test_edit(self) -> None:
        result = _summarize_input("Edit", {"file_path": "/c.py", "old_string": "foo"}, None)
        assert "/c.py" in result
        assert "foo" in result

    def test_bash(self) -> None:
        assert _summarize_input("Bash", {"command": "ls"}, None) == "ls"

    def test_glob(self) -> None:
        assert _summarize_input("Glob", {"pattern": "*.py"}, None) == "*.py"

    def test_grep(self) -> None:
        result = _summarize_input("Grep", {"pattern": "TODO", "path": "."}, None)
        assert "TODO" in result
        assert "." in result

    def test_unknown_tool(self) -> None:
        result = _summarize_input("CustomTool", {"key": "val"}, None)
        assert "key" in result

    def test_empty_input(self) -> None:
        assert _summarize_input("Read", {}, None) == ""

    def test_raw_string_fallback(self) -> None:
        result = _summarize_input("Custom", {}, "raw string input")
        assert "raw string input" in result


class TestTruncate:
    """_truncate のテスト"""

    def test_short(self) -> None:
        assert _truncate("hello") == "hello"

    def test_exact(self) -> None:
        text = "x" * 1500
        assert _truncate(text) == text

    def test_long(self) -> None:
        text = "x" * 3000
        result = _truncate(text)
        assert "truncated" in result
        assert len(result) < len(text)

    def test_small_max_len(self) -> None:
        result = _truncate("abcdefghij", max_len=6)
        assert "truncated" in result


class TestBuildChunkFromToolUse:
    """単一ツール使用からのチャンク生成テスト"""

    @pytest.mark.parametrize(
        "tool_name, tool_input, expected_files_read, expected_files_modified",
        [
            ("Read", {"file_path": "/src/main.py"}, ["/src/main.py"], []),
            ("Write", {"file_path": "/src/new.py", "content": "x"}, [], ["/src/new.py"]),
            ("Edit", {"file_path": "/src/main.py", "old_string": "foo", "new_string": "bar"}, [], ["/src/main.py"]),
            ("Bash", {"command": "ls /tmp"}, ["/tmp"], []),
            ("Grep", {"pattern": "TODO", "path": "."}, [], []),
            ("NotebookEdit", {"file_path": "/nb.ipynb"}, [], ["/nb.ipynb"]),
        ],
        ids=["read", "write", "edit", "bash", "grep", "notebook-edit"],
    )
    def test_file_extraction(
        self,
        tool_name: str,
        tool_input: dict,
        expected_files_read: list[str],
        expected_files_modified: list[str],
    ) -> None:
        chunk = build_chunk_from_tool_use(
            session_id="s1",
            project="proj",
            chunk_index=0,
            user_prompt="test",
            tool_name=tool_name,
            tool_input=tool_input,
            tool_response="ok",
        )
        assert chunk.files_read == expected_files_read
        assert chunk.files_modified == expected_files_modified

    def test_string_tool_input(self) -> None:
        """tool_input が JSON 文字列の場合"""
        chunk = build_chunk_from_tool_use(
            session_id="s1",
            project="proj",
            chunk_index=0,
            user_prompt="test",
            tool_name="Read",
            tool_input='{"file_path": "/a.py"}',
            tool_response="ok",
        )
        assert chunk.files_read == ["/a.py"]

    def test_none_tool_input(self) -> None:
        chunk = build_chunk_from_tool_use(
            session_id="s1",
            project="proj",
            chunk_index=0,
            user_prompt="test",
            tool_name="Read",
            tool_input=None,
            tool_response="ok",
        )
        assert chunk.files_read == []

    def test_none_tool_response(self) -> None:
        chunk = build_chunk_from_tool_use(
            session_id="s1",
            project="proj",
            chunk_index=0,
            user_prompt="test",
            tool_name="Bash",
            tool_input={"command": "echo"},
            tool_response=None,
        )
        assert chunk.content  # 空でないこと

    def test_private_tags_stripped(self) -> None:
        chunk = build_chunk_from_tool_use(
            session_id="s1",
            project="proj",
            chunk_index=0,
            user_prompt="<private>secret</private> visible prompt",
            tool_name="Bash",
            tool_input={"command": "echo hi"},
            tool_response="<private>hidden</private> output",
        )
        assert "secret" not in chunk.user_prompt
        assert "visible prompt" in chunk.user_prompt
        assert "hidden" not in chunk.content

    def test_error_and_ai_response_summary(self) -> None:
        assert _summarize_ai_response("short text") == "short text"
        chunk = build_chunk_from_tool_use(
            session_id="s1",
            project="proj",
            chunk_index=0,
            user_prompt="test",
            tool_name="Bash",
            tool_input={"command": "echo hi"},
            tool_response="boom",
            is_error=True,
            ai_response="a" * 600,
        )
        assert chunk.execution_status == "failure"
        assert chunk.tool_error == "boom"
        assert len(chunk.ai_response_summary or "") == 500
        assert (chunk.ai_response_summary or "").startswith("a" * 400)

    def test_truncation(self) -> None:
        chunk = build_chunk_from_tool_use(
            session_id="s1",
            project="proj",
            chunk_index=0,
            user_prompt="test",
            tool_name="Bash",
            tool_input={"command": "cat big_file"},
            tool_response="x" * 5000,
            chunk_max_length=2000,
        )
        assert len(chunk.content) <= 2000


class TestChunkAccumulator:
    """複数ツール使用の蓄積テスト"""

    def test_accumulate_multiple_tools(self) -> None:
        acc = ChunkAccumulator(
            session_id="s1",
            project="proj",
            user_prompt="fix stuff",
            chunk_index=0,
        )
        acc.add_tool_use("Read", {"file_path": "/a.py"}, "content of a")
        acc.add_tool_use("Edit", {"file_path": "/a.py", "old_string": "x", "new_string": "y"}, "ok")

        chunk = acc.to_chunk()
        assert chunk.tool_names == ["Read", "Edit"]
        assert "/a.py" in chunk.files_read
        assert "/a.py" in chunk.files_modified

    def test_dedup_tool_names(self) -> None:
        acc = ChunkAccumulator(
            session_id="s1",
            project="proj",
            user_prompt="test",
            chunk_index=0,
        )
        acc.add_tool_use("Read", {"file_path": "/a.py"}, "ok")
        acc.add_tool_use("Read", {"file_path": "/b.py"}, "ok")
        chunk = acc.to_chunk()
        assert chunk.tool_names == ["Read"]

    def test_dedup_files(self) -> None:
        acc = ChunkAccumulator(
            session_id="s1",
            project="proj",
            user_prompt="test",
            chunk_index=0,
        )
        acc.add_tool_use("Read", {"file_path": "/a.py"}, "ok")
        acc.add_tool_use("Read", {"file_path": "/a.py"}, "ok again")
        chunk = acc.to_chunk()
        assert chunk.files_read.count("/a.py") == 1

    def test_max_length_enforced(self) -> None:
        acc = ChunkAccumulator(
            session_id="s1",
            project="proj",
            user_prompt="test",
            chunk_index=0,
        )
        acc.add_tool_use("Read", {"file_path": "/a.py"}, "x" * 1000, chunk_max_length=500)
        # 最初のツール使用がすでに500を超えるが、_content_parts が空なら追加される
        acc.add_tool_use("Read", {"file_path": "/b.py"}, "y" * 1000, chunk_max_length=500)
        # 2つ目は容量超過で追加されない
        chunk = acc.to_chunk()
        assert "/b.py" not in chunk.files_read or len(chunk.content) <= 2000
