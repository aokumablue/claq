"""install.lock の取得と解放を管理するヘルパー。"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def install_lock(lock_path: Path) -> Iterator[None]:
    """排他ロックを取得し、終了時に必ず解放する。

    Args:
        lock_path: ロックファイルのパス。

    Yields:
        なし。

    Raises:
        OSError: ファイル作成または flock に失敗した場合。
    """
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
