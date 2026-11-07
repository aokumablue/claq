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
    read_tail_text,
    strip_ansi,
)

from ._support import _raise


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


def _clear_home_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """ホームを決める環境変数をすべて外す。"""
    for name in ("CLAQ_HOME", "HOME", "USERPROFILE"):
        monkeypatch.delenv(name, raising=False)


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
    _clear_home_env(monkeypatch)
    for name in set_vars:
        monkeypatch.setenv(name, str(tmp_path / name))

    assert get_home_dir() == tmp_path / expected


def test_get_home_dir_raises_oserror_when_home_is_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """HOME を決められないときは cwd へ逃げず OSError を送出する（cwd の `.claq/mem.db` を読んで注入しない）。"""
    _clear_home_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", _raise(RuntimeError("boom")))

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


def test_actor_identity_is_uid_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    """os.getuid がある環境では uid を返し、利用者名は引かない（passwd に無い uid でも失敗しない）。"""
    monkeypatch.setattr(core_utils.os, "getuid", lambda: 4242, raising=False)
    monkeypatch.setattr(core_utils.getpass, "getuser", lambda: pytest.fail("getuser を呼んだ"))

    assert core_utils.actor_identity() == "uid=4242"


def test_actor_identity_uses_user_name_without_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    """os.getuid の無い環境（Windows）では利用者名を返す。"""
    monkeypatch.delattr(core_utils.os, "getuid", raising=False)
    monkeypatch.setattr(core_utils.getpass, "getuser", lambda: "someone")

    assert core_utils.actor_identity() == "someone"


def test_actor_identity_propagates_unresolvable_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """os.getuid が無く利用者名も引けなければ、例外をそのまま伝える（promote は DB を変える前に止まる）。"""
    monkeypatch.delattr(core_utils.os, "getuid", raising=False)
    monkeypatch.setattr(core_utils.getpass, "getuser", _raise(OSError("no user")))

    with pytest.raises(OSError, match="no user"):
        core_utils.actor_identity()


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
    [("\x1b[31mred text\x1b[0m", "red text"), ("plain text", "plain text")],
    ids=["color-codes", "plain"],
)
def test_strip_ansi(value: str, expected: str) -> None:
    """カラーコードを除去し、通常テキストはそのまま返す。"""
    assert strip_ansi(value) == expected


def test_log_writes_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    """stderr に 1 行書き込む。"""
    log("test message")
    assert capsys.readouterr().err == "test message\n"


@pytest.mark.parametrize(
    ("content", "max_bytes", "expected"),
    [(b"hello\n", 4096, "hello\n"), (b"A" * 100 + b"TAIL", 4, "TAIL"), (b"\xe3\x81\x82x", 2, "\ufffdx")],
    ids=["within-limit", "tail-only", "split-multibyte-replaced"],
)
def test_read_tail_text(tmp_path: Path, content: bytes, max_bytes: int, expected: str) -> None:
    """上限以内なら全体、超えれば末尾の上限バイトだけを返し、途中で切れた文字は置換すること。"""
    target = tmp_path / "f.log"
    target.write_bytes(content)

    assert read_tail_text(target, max_bytes) == expected


def test_read_tail_text_propagates_oserror(tmp_path: Path) -> None:
    """読めないファイルは OSError をそのまま呼び出し側へ伝えること。"""
    with pytest.raises(OSError):
        read_tail_text(tmp_path / "missing.log", 4096)
