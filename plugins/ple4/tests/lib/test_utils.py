"""ple4.lib.core_utils モジュールのテスト。"""

import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from ple4.lib import core_utils
from ple4.lib.core_utils import (
    append_file,
    ensure_dir,
    ensure_private_dir,
    get_datetime_string,
    get_home_dir,
    get_ple4_dir,
    get_sessions_dir,
    log,
    strip_ansi,
)


class TestDirectoryFunctions:
    """ディレクトリ関連関数のテスト。"""

    def test_get_home_dir(self):
        """ホームディレクトリを返すこと。"""
        home = get_home_dir()
        assert isinstance(home, Path)
        assert home.exists()

    def test_get_home_dir_prefers_explicit_env(self, monkeypatch, tmp_path):
        """明示的な環境変数があればそれを優先すること。"""
        monkeypatch.setenv("PLE4_HOME", str(tmp_path))
        assert get_home_dir() == tmp_path

    def test_get_home_dir_falls_back_to_home_when_ple4_home_is_unset(self, monkeypatch, tmp_path):
        """PLE4_HOME がなければ HOME を使うこと。"""
        monkeypatch.delenv("PLE4_HOME", raising=False)
        monkeypatch.delenv("CLAUDE_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert get_home_dir() == tmp_path

    def test_get_home_dir_falls_back_to_cwd_when_home_is_unavailable(self, monkeypatch, tmp_path):
        """Path.home() が失敗しても cwd にフォールバックすること。"""
        monkeypatch.delenv("PLE4_HOME", raising=False)
        monkeypatch.delenv("CLAUDE_HOME", raising=False)
        monkeypatch.delenv("HOME", raising=False)
        monkeypatch.delenv("USERPROFILE", raising=False)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

        assert get_home_dir() == tmp_path

    def test_get_ple4_dir(self):
        """ホーム配下の .ple4 を返すこと。"""
        assert get_ple4_dir() == get_home_dir() / ".ple4"

    def test_get_sessions_dir(self):
        """ple4 ディレクトリ配下の session-data を返すこと。"""
        sessions = get_sessions_dir()
        assert sessions == get_ple4_dir() / "session-data"


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


class TestEnsurePrivateDir:
    """ensure_private_dir の 0700 作成と既存締め直し。"""

    def test_creates_0700_and_tightens_ple4_parent(self, tmp_path: Path, monkeypatch) -> None:
        """umask 022 でも対象と ~/.ple4 を 0700 にする。"""
        import stat

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("PLE4_HOME", raising=False)
        old_umask = os.umask(0o022)
        try:
            target = tmp_path / ".ple4" / "nested"
            result = ensure_private_dir(target)
            assert result == target
            assert stat.S_IMODE(target.stat().st_mode) == 0o700
            assert stat.S_IMODE((tmp_path / ".ple4").stat().st_mode) == 0o700
        finally:
            os.umask(old_umask)

    def test_tightens_existing_0755(self, tmp_path: Path, monkeypatch) -> None:
        """既存 0755 ディレクトリを 0700 に締め直す。"""
        import stat

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("PLE4_HOME", raising=False)
        ple4 = tmp_path / ".ple4"
        ple4.mkdir(mode=0o755)
        ple4.chmod(0o755)
        ensure_private_dir(ple4)
        assert stat.S_IMODE(ple4.stat().st_mode) == 0o700

    def test_outside_ple4_does_not_chmod_ple4(self, tmp_path: Path, monkeypatch) -> None:
        """~/.ple4 配下でなければ親の .ple4 は触らない。"""
        import stat

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("PLE4_HOME", raising=False)
        ple4 = tmp_path / ".ple4"
        ple4.mkdir(mode=0o755)
        ple4.chmod(0o755)
        other = tmp_path / "other"
        ensure_private_dir(other)
        assert stat.S_IMODE(other.stat().st_mode) == 0o700
        assert stat.S_IMODE(ple4.stat().st_mode) == 0o755

    def test_file_exists_error_is_swallowed(self, tmp_path: Path) -> None:
        """mkdir が FileExistsError でも chmod まで進む。"""
        import stat

        target = tmp_path / "existing"
        target.mkdir()
        with patch.object(Path, "mkdir", side_effect=FileExistsError):
            result = ensure_private_dir(target)
        assert result == target
        assert stat.S_IMODE(target.stat().st_mode) == 0o700


class TestActorIdentity:
    """`actor_identity`（監査ログ用のユーザー識別子）のテスト。

    `os.getuid()` は Windows に存在しないため、`mem promote` の監査ログは
    POSIX 専用だった（P1-005）。`getpass.getuser()` は 3 プラットフォームで
    同じ 1 本の実装になる。
    """

    def test_returns_resolved_user_name(self, monkeypatch):
        """解決できたユーザー名をそのまま返すこと。"""
        monkeypatch.setattr(core_utils.getpass, "getuser", lambda: "someone")

        assert core_utils.actor_identity() == "someone"

    @pytest.mark.parametrize("error", [OSError("no uid"), KeyError("no pwd entry")])
    def test_falls_back_to_unknown(self, monkeypatch, error):
        """識別子を解決できない環境でも例外にせず "unknown" を返すこと。

        これは監査ログのための値であり、取れないことを理由に promote 自体を
        失敗させない（部分成功を作らない）。
        """

        def _raise():
            raise error

        monkeypatch.setattr(core_utils.getpass, "getuser", _raise)

        assert core_utils.actor_identity() == "unknown"


class TestDateTimeFunctions:
    """日付・時刻関連関数のテスト。"""

    def test_get_datetime_string_format(self):
        """YYYY-MM-DD HH:MM:SS 形式で返すこと。"""
        dt_str = get_datetime_string()
        assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", dt_str)


class TestAppendFile:
    """append_file 関数のテスト。"""

    def test_creates_parent_dirs_and_appends(self, tmp_path):
        """親ディレクトリを作り、既存内容の後ろへ追記すること。"""
        test_file = tmp_path / "a" / "b" / "append.txt"
        append_file(test_file, "first")
        append_file(test_file, "second")
        assert test_file.read_text(encoding="utf-8") == "firstsecond"


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


class TestLog:
    """log 関数のテスト。"""

    def test_log_writes_to_stderr(self, capsys):
        """stderr に書き込むこと。"""
        log("test message")
        captured = capsys.readouterr()
        assert captured.err == "test message\n"
