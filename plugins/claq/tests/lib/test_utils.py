"""claq.lib.core_utils モジュールのテスト。"""

import os
import re
import stat
from pathlib import Path

import pytest

from claq.lib import core_utils
from claq.lib.core_utils import (
    append_file,
    ensure_dir,
    ensure_private_dir,
    get_claq_dir,
    get_datetime_string,
    get_home_dir,
    get_sessions_dir,
    log,
    strip_ansi,
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """HOME を tmp へ向け、ディレクトリ種別ごとの見本を置く。

    file（0644・中身 data）・filelink（file への symlink）・dangling（壊れた symlink）・
    realdir（0755）・dirlink（realdir への symlink）。
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAQ_HOME", raising=False)
    (tmp_path / "file").write_text("data")
    (tmp_path / "file").chmod(0o644)
    (tmp_path / "filelink").symlink_to(tmp_path / "file")
    (tmp_path / "dangling").symlink_to(tmp_path / "missing")
    (tmp_path / "realdir").mkdir()
    (tmp_path / "realdir").chmod(0o755)
    (tmp_path / "dirlink").symlink_to(tmp_path / "realdir", target_is_directory=True)
    return tmp_path


def _mode(path: Path) -> int:
    """パーミッションビットを返す。"""
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.parametrize(
    ("set_vars", "expected"),
    [(("CLAQ_HOME", "HOME"), "CLAQ_HOME"), (("HOME", "USERPROFILE"), "HOME"), (("USERPROFILE",), "USERPROFILE")],
    ids=["claq-home-first", "home-next", "userprofile-last"],
)
def test_get_home_dir_env_priority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, set_vars: tuple[str, ...], expected: str
) -> None:
    """CLAQ_HOME → HOME → USERPROFILE の順で、最初に設定された環境変数を使う。"""
    for name in ("CLAQ_HOME", "HOME", "USERPROFILE"):
        monkeypatch.delenv(name, raising=False)
    for name in set_vars:
        monkeypatch.setenv(name, str(tmp_path / name))

    assert get_home_dir() == tmp_path / expected


def test_get_home_dir_raises_oserror_when_home_is_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """HOME を決められないときは cwd へ逃げず OSError を送出する（cwd の `.claq/mem.db` を読んで注入しない）。"""
    for name in ("CLAQ_HOME", "HOME", "USERPROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(OSError):
        get_home_dir()


def test_claq_and_sessions_dirs(home: Path) -> None:
    """claq ディレクトリはホーム配下の .claq、セッションはその下の session-data。"""
    assert get_claq_dir() == home / ".claq"
    assert get_sessions_dir() == home / ".claq" / "session-data"


@pytest.mark.parametrize(
    ("relative", "error"),
    [
        ("new/nested/dir", None),
        ("realdir", None),
        ("dirlink", None),
        ("file", FileExistsError),
        ("filelink", FileExistsError),
        ("dangling", FileExistsError),
        ("file/sub", NotADirectoryError),
    ],
)
def test_ensure_dir(home: Path, relative: str, error: type[OSError] | None) -> None:
    """ディレクトリ（への symlink）は作成・既存とも成功し、ディレクトリ以外の場所はその例外を伝えて中身を変えない。"""
    target = home / relative
    if error is None:
        assert ensure_dir(target) == target
        assert target.is_dir()
    else:
        with pytest.raises(error):
            ensure_dir(target)
    assert (home / "file").read_text() == "data"


@pytest.mark.parametrize(
    ("relative", "precreate_claq", "expected_modes"),
    [
        (".claq/nested", False, {".claq/nested": 0o700, ".claq": 0o700}),
        (".claq", True, {".claq": 0o700}),
        ("other", True, {"other": 0o700, ".claq": 0o755}),
        ("dirlink", False, {"realdir": 0o700}),
    ],
    ids=["creates-and-tightens-claq", "tightens-existing-0755", "outside-claq-keeps-claq", "dir-symlink-tightens-target"],
)
def test_ensure_private_dir(
    home: Path, relative: str, precreate_claq: bool, expected_modes: dict[str, int]
) -> None:
    """umask 022 でも対象を 0700 にし、~/.claq 配下なら ~/.claq も締める（配下でなければ触らない）。"""
    if precreate_claq:
        (home / ".claq").mkdir()
        (home / ".claq").chmod(0o755)
    old_umask = os.umask(0o022)
    try:
        assert ensure_private_dir(home / relative) == home / relative
    finally:
        os.umask(old_umask)

    assert {name: _mode(home / name) for name in expected_modes} == expected_modes


@pytest.mark.parametrize("relative", ["file", "filelink", "dangling", "file/sub"])
def test_ensure_private_dir_non_directory_raises_without_chmod(home: Path, relative: str) -> None:
    """自身や祖先がディレクトリ以外なら FileExistsError を伝え、ファイル（symlink のリンク先）を chmod しない。"""
    with pytest.raises(FileExistsError):
        ensure_private_dir(home / relative)
    assert _mode(home / "file") == 0o644


@pytest.mark.parametrize(
    ("error", "expected"),
    [(None, "someone"), (OSError("no uid"), "unknown"), (KeyError("no pwd entry"), "unknown")],
    ids=["resolved", "oserror", "keyerror"],
)
def test_actor_identity(monkeypatch: pytest.MonkeyPatch, error: Exception | None, expected: str) -> None:
    """監査ログ用のユーザー名を getpass で返し、解決できなくても例外にせず "unknown" を返す。

    `os.getuid()` は Windows に無い。取れないことを理由に promote 自体を失敗させない（部分成功を作らない）。
    """

    def fake_getuser() -> str:
        """``error`` があれば送出し、無ければ名前を返す。"""
        if error is not None:
            raise error
        return "someone"

    monkeypatch.setattr(core_utils.getpass, "getuser", fake_getuser)

    assert core_utils.actor_identity() == expected


def test_get_datetime_string_format() -> None:
    """YYYY-MM-DD HH:MM:SS 形式で返す。"""
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", get_datetime_string())


def test_append_file_creates_parent_dirs_and_appends(tmp_path: Path) -> None:
    """親ディレクトリを作り、既存内容の後ろへ追記する。"""
    test_file = tmp_path / "a" / "b" / "append.txt"
    append_file(test_file, "first")
    append_file(test_file, "second")
    assert test_file.read_text(encoding="utf-8") == "firstsecond"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("\x1b[31mred text\x1b[0m", "red text"), ("plain text", "plain text"), (None, ""), (123, "")],
    ids=["color-codes", "plain", "none", "int"],
)
def test_strip_ansi(value: object, expected: str) -> None:
    """カラーコードを除去し、通常テキストはそのまま、文字列以外は空文字列を返す。"""
    assert strip_ansi(value) == expected


def test_log_writes_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    """stderr に 1 行書き込む。"""
    log("test message")
    assert capsys.readouterr().err == "test message\n"
