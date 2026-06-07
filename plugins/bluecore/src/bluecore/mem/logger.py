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


def setup(log_dir: Path, level: str = "info") -> None:
    """ロガーを初期化する。アプリケーション起動時に1度だけ呼ぶ。"""
    global _initialized
    with _lock:
        if _initialized:
            return
        _initialized = True

    root = logging.getLogger("bluecore.mem")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # ファイルハンドラ
    log_dir.mkdir(parents=True, exist_ok=True)
    log_dir.chmod(0o700)
    log_path = log_dir / f"mem-{datetime.now():%Y-%m-%d}.log"
    fh = logging.FileHandler(log_path, encoding="utf-8")
    log_path.chmod(0o600)
    fh.setFormatter(_FORMATTER)
    root.addHandler(fh)

    # stderr ハンドラ（WARNINGなど）
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.WARNING)
    sh.setFormatter(_FORMATTER)
    root.addHandler(sh)


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
