"""_install_lock のテスト。"""

from __future__ import annotations

import fcntl
import os
import stat
from pathlib import Path

import pytest

from devgear.hooks._install_lock import install_lock


def test_install_lock_creates_file_and_releases_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []
    real_close = os.close

    def fake_flock(fd: int, operation: int) -> None:
        calls.append((fd, operation))

    def tracked_close(fd: int) -> None:
        calls.append((fd, -1))
        real_close(fd)

    monkeypatch.setattr(fcntl, "flock", fake_flock)
    monkeypatch.setattr(os, "close", tracked_close)

    lock_path = tmp_path / "install.lock"
    with install_lock(lock_path):
        assert lock_path.exists()
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600

    assert calls[0][1] == fcntl.LOCK_EX
    assert calls[1][1] == fcntl.LOCK_UN
    assert calls[-1][1] == -1


def test_install_lock_releases_lock_when_body_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    operations: list[int] = []

    def fake_flock(fd: int, operation: int) -> None:
        operations.append(operation)

    monkeypatch.setattr(fcntl, "flock", fake_flock)

    lock_path = tmp_path / "install.lock"
    with pytest.raises(RuntimeError):
        with install_lock(lock_path):
            raise RuntimeError("boom")

    assert operations == [fcntl.LOCK_EX, fcntl.LOCK_UN]


def test_install_lock_closes_file_when_flock_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[int] = []
    real_close = os.close

    def fake_flock(fd: int, operation: int) -> None:
        raise OSError("flock failed")

    def tracked_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(fcntl, "flock", fake_flock)
    monkeypatch.setattr(os, "close", tracked_close)

    with pytest.raises(OSError):
        with install_lock(tmp_path / "install.lock"):
            pass

    assert closed
