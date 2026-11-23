"""構造化ロギング — ファイル出力 + stderr"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from claq.lib.core_utils import ensure_private_dir


class _RedactingFormatter(logging.Formatter):
    """PII / シークレットを全ログメッセージから除去するフォーマッタ。"""

    def format(self, record: logging.LogRecord) -> str:
        """整形済みログメッセージから PII / シークレットを除去して返す。"""
        from claq.lib.redaction import redact

        return redact(super().format(record))


_FORMATTER = _RedactingFormatter(
    "[%(asctime)s.%(msecs)03d] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_PACKAGE_LOGGER_NAME = "claq.mem"
"""ハンドラを持つ親ロガーの名前。``get`` が返すロガーはこの子なので、ハンドラへ伝播する。"""

_FILE_ONLY_ATTR = "claq_file_only"

FILE_ONLY = {_FILE_ONLY_ATTR: True}
"""ログ呼び出しの ``extra`` に渡すと、その記録をファイルにだけ書き stderr へは出さない。

呼び出し側が利用者向けの 1 行を自分で stderr へ出す経路で使う。stderr へも流すと同じ失敗が
2 行になり、host が通知に使う 1 行目が時刻付きの記録に置き換わる。
"""


def _file_handler(log_dir: Path) -> logging.Handler:
    """日次ログファイルへ書く FileHandler を作る。

    ``mkdir(parents=True)`` は mode を**親へ適用しない**ため、`~/.claq` を最初に
    作るのがこの関数だと、親は umask 既定（通常 0755）のまま残る。
    ロガーは `_load_settings_or_raise()` の中で走り、どの ``Database()`` よりも
    先に動くので、DB を開かずに終わる経路（例: ``mem list --kind bogus``）では
    `ensure_private_dir` を一度も通らない。そのため根から 0700 で作る
    ``ensure_private_dir`` を通す。

    ログファイルも同じ理由で ``O_CREAT|O_EXCL`` で先に 0600 を作る。
    ``FileHandler`` に作らせてから ``chmod`` すると、その間は umask 既定
    （通常 0644）で開けてしまい、その窓で取得された fd は後続の chmod では
    失効しない（``database.py`` が同じ形を避けている理由と同じ）。
    """
    ensure_private_dir(log_dir)
    log_path = log_dir / f"mem-{datetime.now():%Y-%m-%d}.log"
    # 既存なら（競合で片方が負けた場合も）下の chmod で mode を揃える。
    with contextlib.suppress(FileExistsError):
        os.close(os.open(log_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    handler = logging.FileHandler(log_path, encoding="utf-8")
    log_path.chmod(0o600)
    handler.setFormatter(_FORMATTER)
    return handler


def _stderr_handler() -> logging.Handler:
    """WARNING 以上を stderr へ出す Handler を作る。``FILE_ONLY`` を付けた記録は出さない。"""
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.WARNING)
    handler.addFilter(lambda record: not getattr(record, _FILE_ONLY_ATTR, False))
    handler.setFormatter(_FORMATTER)
    return handler


def setup(log_dir: Path) -> None:
    """ロガーを INFO レベルで初期化する。プロセスの起動時に 1 度だけ呼ぶ（`cli.main` が呼ぶ）。

    Args:
        log_dir: 日次ログファイルを置くディレクトリ。
    """
    package_logger = logging.getLogger(_PACKAGE_LOGGER_NAME)
    file_handler = _file_handler(log_dir)
    stderr_handler = _stderr_handler()
    package_logger.setLevel(logging.INFO)
    package_logger.addHandler(file_handler)
    package_logger.addHandler(stderr_handler)


def get(component: str) -> logging.Logger:
    """コンポーネント名でロガーを取得する。"""
    return logging.getLogger(f"{_PACKAGE_LOGGER_NAME}.{component}")
