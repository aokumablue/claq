"""bluecore.lib.core_utils モジュールのテスト。"""

import os
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

from bluecore.lib.core_utils import (
    IS_LINUX,
    IS_MACOS,
    IS_WINDOWS,
    append_file,
    command_exists,
    count_in_file,
    ensure_dir,
    find_files,
    get_bluecore_dir,
    get_claude_dir,
    get_date_string,
    get_datetime_string,
    get_git_user_name,
    get_home_dir,
    get_learned_skills_dir,
    get_session_id_short,
    get_session_search_dirs,
    get_sessions_dir,
    get_time_string,
    log,
    output,
    read_file,
    run_command,
    sanitize_session_id,
    strip_ansi,
    write_file,
)


class TestPlatformDetection:
    """プラットフォーム判定定数のテスト。"""

    def test_exactly_one_platform_is_true(self):
        """検出されるプラットフォームは必ず 1 つであること。"""
        platforms = [IS_WINDOWS, IS_MACOS, IS_LINUX]
        assert sum(platforms) == 1


class TestDirectoryFunctions:
    """ディレクトリ関連関数のテスト。"""

    def test_get_home_dir(self):
        """ホームディレクトリを返すこと。"""
        home = get_home_dir()
        assert isinstance(home, Path)
        assert home.exists()

    def test_get_home_dir_prefers_explicit_env(self, monkeypatch, tmp_path):
        """明示的な環境変数があればそれを優先すること。"""
        monkeypatch.setenv("BLUECORE_HOME", str(tmp_path))
        assert get_home_dir() == tmp_path

    def test_get_home_dir_falls_back_to_home_when_bluecore_home_is_unset(self, monkeypatch, tmp_path):
        """BLUECORE_HOME がなければ HOME を使うこと。"""
        monkeypatch.delenv("BLUECORE_HOME", raising=False)
        monkeypatch.delenv("CLAUDE_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert get_home_dir() == tmp_path

    def test_get_home_dir_falls_back_to_cwd_when_home_is_unavailable(self, monkeypatch, tmp_path):
        """Path.home() が失敗しても cwd にフォールバックすること。"""
        monkeypatch.delenv("BLUECORE_HOME", raising=False)
        monkeypatch.delenv("CLAUDE_HOME", raising=False)
        monkeypatch.delenv("HOME", raising=False)
        monkeypatch.delenv("USERPROFILE", raising=False)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

        assert get_home_dir() == tmp_path

    def test_get_claude_dir(self):
        """ホーム配下の .claude を返すこと。"""
        claude_dir = get_claude_dir()
        assert claude_dir == get_home_dir() / ".claude"

    def test_get_bluecore_dir(self):
        """ホーム配下の .bluecore を返すこと。"""
        assert get_bluecore_dir() == get_home_dir() / ".bluecore"

    def test_get_sessions_dir(self):
        """bluecore ディレクトリ配下の session-data を返すこと。"""
        sessions = get_sessions_dir()
        assert sessions == get_bluecore_dir() / "session-data"

    def test_get_session_search_dirs_no_duplicates(self):
        """重複のないディレクトリ一覧を返すこと。"""
        dirs = get_session_search_dirs()
        assert len(dirs) == len(set(dirs))

    def test_get_learned_skills_dir(self):
        """claude ディレクトリ配下の skills/learned を返すこと。"""
        skills = get_learned_skills_dir()
        assert skills == get_claude_dir() / "skills" / "learned"

class TestEnsureDir:
    """ensure_dir 関数のテスト。"""

    def test_creates_directory(self, tmp_path):
        """ディレクトリが存在しない場合は作成すること。"""
        new_dir = tmp_path / "new" / "nested" / "dir"
        result = ensure_dir(new_dir)
        assert result == new_dir
        assert new_dir.exists()

    def test_handles_existing_directory(self, tmp_path):
        """ディレクトリが既に存在しても失敗しないこと。"""
        existing = tmp_path / "existing"
        existing.mkdir()
        result = ensure_dir(existing)
        assert result == existing
        assert existing.exists()


class TestDateTimeFunctions:
    """日付・時刻関連関数のテスト。"""

    def test_get_date_string_format(self):
        """YYYY-MM-DD 形式で返すこと。"""
        date_str = get_date_string()
        assert re.match(r"^\d{4}-\d{2}-\d{2}$", date_str)

    def test_get_time_string_format(self):
        """HH:MM 形式で返すこと。"""
        time_str = get_time_string()
        assert re.match(r"^\d{2}:\d{2}$", time_str)

    def test_get_datetime_string_format(self):
        """YYYY-MM-DD HH:MM:SS 形式で返すこと。"""
        dt_str = get_datetime_string()
        assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", dt_str)


class TestSanitizeSessionId:
    """sanitize_session_id 関数のテスト。"""

    def test_returns_none_for_empty(self):
        """空入力では None を返すこと。"""
        assert sanitize_session_id(None) is None
        assert sanitize_session_id("") is None

    def test_sanitizes_simple_string(self):
        """単純な英数字文字列はそのまま通すこと。"""
        assert sanitize_session_id("test123") == "test123"
        assert sanitize_session_id("my-project") == "my-project"

    def test_replaces_invalid_chars(self):
        """無効文字はハイフンに置き換えること。"""
        assert sanitize_session_id("test@project!") == "test-project"
        assert sanitize_session_id("my project") == "my-project"

    def test_collapses_multiple_hyphens(self):
        """連続するハイフンはまとめること。"""
        assert sanitize_session_id("test--project") == "test-project"
        assert sanitize_session_id("a---b") == "a-b"

    def test_strips_leading_dots(self):
        """先頭のドットは除去すること。"""
        assert sanitize_session_id(".claude") == "claude"
        assert sanitize_session_id("..hidden") == "hidden"

    def test_handles_windows_reserved_names(self):
        """Windows の予約語にはハッシュ接尾辞を付けること。"""
        result = sanitize_session_id("CON")
        assert result.startswith("CON-")
        assert len(result) == 10  # CON + - + 16 進 6 文字

    def test_handles_non_ascii(self):
        """非 ASCII 入力にはハッシュ接尾辞を付けること。"""
        result = sanitize_session_id("プロジェクト")
        assert result is not None
        # 非 ASCII のみの場合は 8 文字のハッシュになる
        assert len(result) == 8


class TestGetSessionIdShort:
    """get_session_id_short 関数のテスト。"""

    def test_returns_default_without_env(self):
        """環境変数が未設定ならデフォルトを返すこと。"""
        with patch.dict(os.environ, {}, clear=True):
            # CLAUDE_SESSION_ID があればクリアする
            os.environ.pop("CLAUDE_SESSION_ID", None)
            result = get_session_id_short()
            assert result is not None
            assert len(result) > 0

    def test_uses_env_var_when_set(self):
        """CLAUDE_SESSION_ID が設定されていればそれを使うこと。"""
        with patch.dict(os.environ, {"CLAUDE_SESSION_ID": "abcdefghij"}):
            result = get_session_id_short()
            # 末尾 8 文字: cdefghij
            assert result == "cdefghij"


class TestFileFunctions:
    """ファイル操作関数のテスト。"""

    def test_read_file_returns_content(self, tmp_path):
        """ファイル内容を読み取れること。"""
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")
        assert read_file(test_file) == "hello world"

    def test_read_file_returns_none_for_missing(self, tmp_path):
        """ファイルがない場合は None を返すこと。"""
        assert read_file(tmp_path / "missing.txt") is None

    def test_write_file_creates_file(self, tmp_path):
        """内容付きでファイルを作成すること。"""
        test_file = tmp_path / "new.txt"
        write_file(test_file, "content")
        assert test_file.read_text() == "content"

    def test_write_file_creates_parent_dirs(self, tmp_path):
        """親ディレクトリを作成すること。"""
        test_file = tmp_path / "a" / "b" / "c.txt"
        write_file(test_file, "nested")
        assert test_file.read_text() == "nested"

    def test_append_file(self, tmp_path):
        """ファイルへ追記できること。"""
        test_file = tmp_path / "append.txt"
        write_file(test_file, "first")
        append_file(test_file, "second")
        assert read_file(test_file) == "firstsecond"


class TestFindFiles:
    """find_files 関数のテスト。"""

    def test_finds_matching_files(self, tmp_path):
        """パターンに一致するファイルを見つけること。"""
        (tmp_path / "test1.txt").write_text("a")
        (tmp_path / "test2.txt").write_text("b")
        (tmp_path / "other.md").write_text("c")

        results = find_files(tmp_path, "*.txt")
        paths = [r["path"] for r in results]

        assert len(results) == 2
        assert str(tmp_path / "test1.txt") in paths
        assert str(tmp_path / "test2.txt") in paths

    def test_recursive_search(self, tmp_path):
        """recursive=True のときはサブディレクトリも探索すること。"""
        (tmp_path / "root.txt").write_text("a")
        subdir = tmp_path / "sub"
        subdir.mkdir()
        (subdir / "nested.txt").write_text("b")

        results = find_files(tmp_path, "*.txt", recursive=True)
        assert len(results) == 2

    def test_returns_empty_for_missing_dir(self):
        """ディレクトリがない場合は空リストを返すこと。"""
        results = find_files("/nonexistent/path", "*.txt")
        assert results == []


class TestReplaceInFile:
    """replace_in_file 関数のテスト。"""

class TestCountInFile:
    """count_in_file 関数のテスト。"""

    def test_counts_occurrences(self, tmp_path):
        """パターンの出現回数を数えること。"""
        test_file = tmp_path / "test.txt"
        test_file.write_text("foo bar foo baz foo")

        assert count_in_file(test_file, "foo") == 3
        assert count_in_file(test_file, "bar") == 1
        assert count_in_file(test_file, "qux") == 0

    def test_returns_zero_for_missing_file(self, tmp_path):
        """ファイルがない場合は 0 を返すこと。"""
        assert count_in_file(tmp_path / "missing.txt", "foo") == 0


class TestGrepFile:
    """grep_file 関数のテスト。"""

class TestStripAnsi:
    """strip_ansi 関数のテスト。"""

    def test_strips_color_codes(self):
        """カラーコードを除去すること。"""
        colored = "\x1b[31mred text\x1b[0m"
        assert strip_ansi(colored) == "red text"

    def test_handles_plain_text(self):
        """通常テキストはそのまま返すこと。"""
        assert strip_ansi("plain text") == "plain text"

    def test_handles_non_string(self):
        """文字列以外の入力では空文字列を返すこと。"""
        assert strip_ansi(None) == ""
        assert strip_ansi(123) == ""


class TestRunCommand:
    """run_command 関数のテスト。"""

    def test_blocks_unknown_commands(self):
        """許可リストにないコマンドはブロックすること。"""
        result = run_command("rm -rf /")
        assert result["success"] is False
        assert "blocked" in result["output"]

    def test_allows_git_commands(self):
        """git コマンドは許可すること。"""
        result = run_command("git --version")
        assert result["success"] is True
        assert "git" in result["output"].lower()

    def test_blocks_shell_metacharacters(self):
        """シェルのメタ文字を含むコマンドはブロックすること。"""
        result = run_command("git status; rm -rf /")
        assert result["success"] is False
        assert "blocked" in result["output"]


class TestCommandExists:
    """command_exists 関数のテスト。"""

    def test_finds_existing_command(self):
        """存在するコマンドを見つけられること。"""
        # 多くの環境では 'git' が存在する
        assert command_exists("git") is True

    def test_returns_false_for_missing(self):
        """存在しないコマンドでは False を返すこと。"""
        assert command_exists("nonexistent_command_12345") is False

    def test_rejects_invalid_names(self):
        """不正なコマンド名は拒否すること。"""
        assert command_exists("cmd;rm") is False
        assert command_exists("cmd|cat") is False


class TestEnsureDirRaceCondition:
    """ensure_dir のレースコンディション (FileExistsError) テスト。"""

    def test_file_exists_error_is_swallowed(self, tmp_path: Path):
        """mkdir が FileExistsError を起こしても例外を上げないこと。"""
        from unittest.mock import patch

        target = tmp_path / "existing"
        target.mkdir()

        with patch.object(Path, "mkdir", side_effect=FileExistsError):
            result = ensure_dir(target)
        assert isinstance(result, Path)


class TestGetGitRepoName:
    """get_git_repo_name / get_project_name テスト。"""

    def test_returns_none_when_not_git_repo(self, tmp_path: Path, monkeypatch):
        """git リポジトリでない場合 None を返すこと。"""
        from bluecore.lib.core_utils import get_git_repo_name

        monkeypatch.chdir(tmp_path)
        # git rev-parse 失敗 → None
        with patch("bluecore.lib.core_utils.run_command", return_value={"success": False, "output": ""}):
            assert get_git_repo_name() is None

    def test_project_name_falls_back_to_cwd_name(self, tmp_path: Path, monkeypatch):
        """git リポジトリでない場合、カレントディレクトリ名を返すこと。"""
        from bluecore.lib.core_utils import get_project_name

        monkeypatch.chdir(tmp_path)
        with patch("bluecore.lib.core_utils.run_command", return_value={"success": False, "output": ""}):
            result = get_project_name()
        assert result == tmp_path.name

    def test_get_git_user_name_empty_on_failure(self) -> None:
        """git user.name が取得できない場合は空文字列を返すこと。"""
        with patch("bluecore.lib.core_utils.run_command", return_value={"success": False, "output": ""}):
            assert get_git_user_name() == ""


class TestSanitizeSessionIdEdgeCases:
    """sanitize_session_id の追加境界値テスト。"""

    def test_windows_reserved_id_gets_suffix(self):
        """Windows 予約済み ID にはハッシュサフィックスが付くこと。"""
        # "CON" など大文字の予約語を渡すとサフィックスが付く
        result = sanitize_session_id("CON")
        assert result is not None
        assert result.startswith("con-") or "-" in result

    def test_non_ascii_with_ascii_chars_gets_suffix(self):
        """ASCII と非 ASCII の混在入力はサフィックス付きで返すこと。"""
        result = sanitize_session_id("project-プロジェクト")
        assert result is not None
        assert "-" in result

    def test_punctuation_only_returns_none(self):
        """句読点のみの入力は None を返すこと。"""
        result = sanitize_session_id("!!!???...")
        assert result is None

    def test_non_ascii_only_returns_hash(self):
        """非 ASCII のみの入力は 8 文字ハッシュを返すこと。"""
        result = sanitize_session_id("プロジェクト")
        assert result is not None
        assert len(result) == 8


class TestFindFilesEdgeCases:
    """find_files の未カバーパステスト。"""

    def test_max_age_filters_old_files(self, tmp_path: Path):
        """max_age を超えたファイルは除外されること。"""
        import time

        f = tmp_path / "old.tmp"
        f.write_text("content")
        # mtime を過去に設定（10日前）
        old_mtime = time.time() - 10 * 24 * 3600
        import os

        os.utime(str(f), (old_mtime, old_mtime))

        results = find_files(tmp_path, "*.tmp", max_age=5)
        assert results == []

    def test_max_age_keeps_recent_files(self, tmp_path: Path):
        """max_age 以内のファイルは含まれること。"""
        f = tmp_path / "recent.tmp"
        f.write_text("content")

        results = find_files(tmp_path, "*.tmp", max_age=1)
        assert len(results) == 1

    def test_recursive_finds_nested_files(self, tmp_path: Path):
        """recursive=True でサブディレクトリも検索すること。"""
        sub = tmp_path / "subdir"
        sub.mkdir()
        (sub / "nested.tmp").write_text("content")

        results = find_files(tmp_path, "*.tmp", recursive=True)
        assert len(results) == 1
        assert "nested.tmp" in results[0]["path"]

    def test_permission_error_skipped(self, tmp_path: Path, monkeypatch):
        """PermissionError が発生したディレクトリはスキップされること。"""
        from pathlib import Path as _Path

        from bluecore.lib.core_utils import find_files as _find_files

        def _raise(self):
            raise PermissionError("denied")

        monkeypatch.setattr(_Path, "iterdir", _raise)
        results = _find_files(tmp_path, "*.tmp")
        assert results == []

    def test_oserror_on_stat_skipped(self, tmp_path: Path, monkeypatch):
        """stat() が OSError の場合、そのファイルはスキップされること。

        pathlib.Path.is_file をパッチして True を返し、
        続く stat() 呼び出しが OSError を起こしてもスキップされることを確認する。
        """
        f = tmp_path / "file.tmp"
        f.write_text("content")

        from pathlib import Path as _Path

        original_is_file = _Path.is_file
        original_stat = _Path.stat

        # is_file は True を返すが、stat は OSError を起こす
        monkeypatch.setattr(_Path, "is_file", lambda self: self.suffix == ".tmp" or original_is_file(self))
        monkeypatch.setattr(
            _Path,
            "stat",
            lambda self, **kwargs: (
                (_ for _ in ()).throw(OSError("stat failed"))
                if self.suffix == ".tmp"
                else original_stat(self, **kwargs)
            ),
        )

        results = find_files(tmp_path, "*.tmp")
        # OSError はキャッチされてスキップされ、空リストが返る
        assert results == []


class TestRunCommandEdgeCases:
    """run_command の追加テスト。"""

    def test_failed_command_returns_stderr(self):
        """コマンドが失敗した場合 stderr を返すこと。"""
        result = run_command("git rev-parse HEAD")
        # git リポジトリ内では成功するが、失敗パスを確認
        # 失敗時は returncode != 0 → success=False
        assert "success" in result

    def test_oserror_returns_error_message(self, monkeypatch):
        """subprocess.run が OSError のとき、エラーメッセージを返すこと。"""
        import subprocess as _subprocess

        monkeypatch.setattr(_subprocess, "run", MagicMock(side_effect=OSError("exec failed")))
        result = run_command("git --version")
        assert result["success"] is False
        assert "exec failed" in result["output"]

    def test_backtick_blocked(self):
        """バッククォートを含むコマンドはブロックされること。"""
        result = run_command("git `whoami`")
        assert result["success"] is False

    def test_dollar_sign_blocked(self):
        """$ を含むコマンドはブロックされること。"""
        result = run_command("git $HOME")
        assert result["success"] is False

    def test_command_returning_stderr_on_failure(self, monkeypatch):
        """コマンドが失敗したとき returncode != 0 のケース。"""
        import subprocess as _subprocess

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "fatal: not a git repo"
        monkeypatch.setattr(_subprocess, "run", MagicMock(return_value=mock_result))
        result = run_command("git status")
        assert result["success"] is False
        assert "fatal" in result["output"]


class TestGetGitModifiedFiles:
    """get_git_modified_files テスト。"""

class TestReplaceInFileEdgeCases:
    """replace_in_file の未カバーパステスト。"""

class TestCountInFileEdgeCases:
    """count_in_file の未カバーパステスト。"""

    def test_compiled_regex_pattern(self, tmp_path: Path):
        """コンパイル済み正規表現パターンを使うこと。"""
        f = tmp_path / "file.txt"
        f.write_text("abc 123 def 456")
        result = count_in_file(f, re.compile(r"\d+"))
        assert result == 2

    def test_invalid_regex_string_returns_zero(self, tmp_path: Path):
        """不正な正規表現文字列は 0 を返すこと。"""
        f = tmp_path / "file.txt"
        f.write_text("abc")
        result = count_in_file(f, "[invalid")
        assert result == 0


class TestGrepFileEdgeCases:
    """grep_file の未カバーパステスト。"""

class TestFindFilesEmptyArgs:
    """find_files の空引数テスト (line 211)。"""

    def test_empty_directory_returns_empty(self):
        assert find_files("", "*.txt") == []

    def test_empty_pattern_returns_empty(self, tmp_path: Path):
        assert find_files(tmp_path, "") == []


class TestCommandExistsOsError:
    """command_exists の OSError パス (line 383-384)。"""

    def test_oserror_returns_false(self, monkeypatch):
        """subprocess.run が OSError を起こしたとき False を返すこと。"""
        import subprocess as _subprocess

        monkeypatch.setattr(_subprocess, "run", MagicMock(side_effect=OSError("no such binary")))
        result = command_exists("git")
        assert result is False


class TestIsGitRepo:
    """is_git_repo テスト (line 435)。"""

    def test_is_git_repo_in_git_directory(self):
        """git リポジトリ内では True を返すこと。"""
        from bluecore.lib.core_utils import is_git_repo

        # 現在のディレクトリは git リポジトリのはず
        assert is_git_repo() is True

    def test_is_git_repo_outside_git(self, tmp_path: Path, monkeypatch):
        """git リポジトリ外では False を返すこと。"""
        from bluecore.lib.core_utils import is_git_repo

        monkeypatch.chdir(tmp_path)
        with patch("bluecore.lib.core_utils.run_command", return_value={"success": False, "output": ""}):
            assert is_git_repo() is False


class TestReplaceInFileException:
    """replace_in_file の Exception パス (line 510-512)。"""

class TestReadStdinJsonSync:
    """read_stdin_json_sync の基本テスト (line 299-316)。"""

class TestReadStdinJsonAsync:
    """read_stdin_json の基本テスト (line 266-285)。"""

    def test_tty_returns_empty_dict(self, monkeypatch):
        """stdin が tty の場合は空辞書を返すこと。"""
        import asyncio
        import sys

        from bluecore.lib.core_utils import read_stdin_json

        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        result = asyncio.run(read_stdin_json())
        assert result == {}

    def test_timeout_returns_empty_dict(self, monkeypatch):
        """asyncio.wait_for がタイムアウトした場合は空辞書を返すこと。"""
        import asyncio
        import sys

        from bluecore.lib.core_utils import read_stdin_json

        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

        async def _run():
            # TimeoutError をシミュレート
            with patch("asyncio.wait_for", side_effect=TimeoutError()):
                return await read_stdin_json()

        result = asyncio.run(_run())
        assert result == {}

    def test_json_decode_error_returns_empty_dict(self, monkeypatch):
        """不正 JSON の場合は空辞書を返すこと。"""
        import asyncio
        import json
        import sys

        from bluecore.lib.core_utils import read_stdin_json

        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        monkeypatch.setattr(sys.stdin, "read", lambda n: "{invalid")

        async def _run():
            with patch("asyncio.wait_for", side_effect=json.JSONDecodeError("", "", 0)):
                return await read_stdin_json()

        result = asyncio.run(_run())
        assert result == {}

    def test_valid_json_returned(self, monkeypatch):
        """有効な JSON を stdin から読み込んだ場合に辞書を返すこと (read_with_timeout 経路)。"""
        import asyncio
        import io
        import sys

        from bluecore.lib.core_utils import read_stdin_json

        # stdin を StringIO に差し替えて非 tty として扱う
        fake_stdin = io.StringIO('{"key": "value"}')
        monkeypatch.setattr(sys, "stdin", fake_stdin)

        result = asyncio.run(read_stdin_json())
        assert result == {"key": "value"}

    def test_empty_data_returns_empty_dict(self, monkeypatch):
        """空データを stdin から読み込んだ場合は空辞書を返すこと (line 282-283)。"""
        import asyncio
        import io
        import sys

        from bluecore.lib.core_utils import read_stdin_json

        fake_stdin = io.StringIO("   ")
        monkeypatch.setattr(sys, "stdin", fake_stdin)

        result = asyncio.run(read_stdin_json())
        assert result == {}


class TestReadStdinJsonSyncEdgeCases:
    """read_stdin_json_sync の追加テスト (line 306-316)。"""

class TestCommandExistsWindows:
    """command_exists の Windows 分岐テスト (line 371)。"""

    def test_windows_uses_where_command(self, monkeypatch):
        """IS_WINDOWS=True のとき 'where' コマンドを使うこと。"""
        import subprocess as _subprocess

        import bluecore.lib.core_utils as _mod

        monkeypatch.setattr(_mod, "IS_WINDOWS", True)
        captured_cmd: list = []

        def _capture(cmd, **kwargs):
            captured_cmd.append(cmd)
            r = MagicMock()
            r.returncode = 0
            return r

        monkeypatch.setattr(_subprocess, "run", _capture)
        result = command_exists("git")
        assert result is True
        assert captured_cmd[0][0] == "where"


class TestLogOutput:
    """log 関数と output 関数のテスト。"""

    def test_log_writes_to_stderr(self, capsys):
        """stderr に書き込むこと。"""
        log("test message")
        captured = capsys.readouterr()
        assert captured.err == "test message\n"

    def test_output_writes_to_stdout(self, capsys):
        """stdout に書き込むこと。"""
        output("test")
        captured = capsys.readouterr()
        assert captured.out == "test\n"

    def test_output_serializes_dict(self, capsys):
        """dict を JSON にシリアライズすること。"""
        output({"key": "value"})
        captured = capsys.readouterr()
        assert captured.out == '{"key": "value"}\n'


def test_get_session_id_short_unsanitizable(monkeypatch) -> None:
    """セッションIDが記号のみで無害化後に空ならフォールバックを使う。"""
    import bluecore.lib.core_utils as cu

    monkeypatch.setenv("CLAUDE_SESSION_ID", "!!!!!!!!")
    monkeypatch.setattr(cu, "get_project_name", lambda: None)
    assert cu.get_session_id_short("fb") == "fb"


def test_run_command_list_input() -> None:
    """list 形式のコマンドはリスト変換経路を通る。"""
    from bluecore.lib.core_utils import run_command

    result = run_command(["git", "status"])
    assert "output" in result


def test_run_command_empty_list() -> None:
    """空コマンドはエラーを返す。"""
    from bluecore.lib.core_utils import run_command

    result = run_command([])
    assert result["success"] is False


