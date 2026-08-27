"""構造化ロギング — ファイル出力 + stderr"""

from __future__ import annotations

import logging
import sys
import threading
from datetime import datetime
from pathlib import Path


class _RedactingFormatter(logging.Formatter):
    """PII / シークレットを全ログメッセージから除去するフォーマッタ。"""

    def format(self, record: logging.LogRecord) -> str:
        """整形済みログメッセージから PII / シークレットを除去して返す。"""
        from bluecore.mem.redaction import redact

        return redact(super().format(record))


_FORMATTER = _RedactingFormatter(
    "[%(asctime)s.%(msecs)03d] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_initialized = False
_lock = threading.Lock()


def _file_handler(log_dir: Path) -> logging.Handler:
    """日次ログファイルへ書く FileHandler を作る。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_dir.chmod(0o700)
    log_path = log_dir / f"mem-{datetime.now():%Y-%m-%d}.log"
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

        root = logging.getLogger("bluecore.mem")
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
    root = logging.getLogger("bluecore.mem")
    root.handlers.clear()


def get(component: str) -> logging.Logger:
    """コンポーネント名でロガーを取得する。"""
    return logging.getLogger(f"bluecore.mem.{component}")
