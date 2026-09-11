"""構造化ロギング — ファイル出力 + stderr"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

from ple4.lib.core_utils import ensure_private_dir


class _RedactingFormatter(logging.Formatter):
    """PII / シークレットを全ログメッセージから除去するフォーマッタ。"""

    def format(self, record: logging.LogRecord) -> str:
        """整形済みログメッセージから PII / シークレットを除去して返す。"""
        from ple4.mem.redaction import redact

        return redact(super().format(record))


_FORMATTER = _RedactingFormatter(
    "[%(asctime)s.%(msecs)03d] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_initialized = False
_lock = threading.Lock()


def _file_handler(log_dir: Path) -> logging.Handler:
    """日次ログファイルへ書く FileHandler を作る。

    ``mkdir(parents=True)`` は mode を**親へ適用しない**ため、`~/.ple4` を最初に
    作るのがこの関数だったとき、親は umask 既定（通常 0755）のまま残っていた。
    ロガーは `_load_settings_or_raise()` の中で走り、どの ``Database()`` よりも
    先に動くので、DB を開かずに終わる経路（例: ``mem list --kind bogus``）では
    `ensure_private_dir` を一度も通らない。実測で `~/.ple4` が
    ``drwxr-xr-x`` のまま残っていた。根から 0700 で作る
    ``ensure_private_dir`` を通す。

    ログファイルも同じ理由で ``O_CREAT|O_EXCL`` で先に 0600 を作る。
    ``FileHandler`` に作らせてから ``chmod`` すると、その間は umask 既定
    （通常 0644）で開けてしまい、その窓で取得された fd は後続の chmod では
    失効しない（``database.py`` が同じ形を避けている理由と同じ）。
    """
    ensure_private_dir(log_dir)
    log_path = log_dir / f"mem-{datetime.now():%Y-%m-%d}.log"
    if not log_path.exists():
        # 競合しても片方が勝てばよい。既存なら下の chmod で mode を揃える。
        with contextlib.suppress(FileExistsError):
            os.close(os.open(log_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    handler = logging.FileHandler(log_path, encoding="utf-8")
    log_path.chmod(0o600)
    handler.setFormatter(_FORMATTER)
    return handler


def _stderr_handler() -> logging.Handler:
    """WARNING 以上を stderr へ出す Handler を作る。"""
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.WARNING)
    handler.setFormatter(_FORMATTER)
    return handler


def setup(log_dir: Path, level: str = "info") -> None:
    """ロガーを初期化する。アプリケーション起動時に1度だけ呼ぶ。

    `_initialized` はハンドラの構築と追加が成功した後にだけ立てる。先に立てると、
    `_file_handler` がディスク不調・権限エラー等で例外を投げた場合に「初期化済み
    だがハンドラが空」という復帰不能な状態が残り、以後の `setup()` が何もせずに
    返るようになる。
    """
    global _initialized
    with _lock:
        if _initialized:
            return

        root = logging.getLogger("ple4.mem")
        file_handler = _file_handler(log_dir)
        stderr_handler = _stderr_handler()
        root.setLevel(getattr(logging, level.upper(), logging.INFO))
        root.addHandler(file_handler)
        root.addHandler(stderr_handler)
        _initialized = True


def reset() -> None:
    """テスト用: ロガーをリセットする。"""
    global _initialized
    with _lock:
        _initialized = False
    root = logging.getLogger("ple4.mem")
    root.handlers.clear()


def get(component: str) -> logging.Logger:
    """コンポーネント名でロガーを取得する。"""
    return logging.getLogger(f"ple4.mem.{component}")
