"""SQLite ベースの mem ランタイム設定。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from bluecore.lib.constants import BASE_DIR_NAME

if "BLUECORE_DATA_PATH" in os.environ:
    _DEFAULT_DATA_DIR = Path(os.environ["BLUECORE_DATA_PATH"])
else:
    _DEFAULT_DATA_DIR = Path.home() / BASE_DIR_NAME

# --- context 注入の予算 ---

_CHARS_PER_TOKEN = 3.5
"""トークン概算の換算係数。予算はトークンで設計し、文字数で強制する。"""

CONTEXT_GLOBAL_CHAR_BUDGET = int(600 * _CHARS_PER_TOKEN)
"""共通知識節の出力予算（文字数）。600 トークン相当。"""

CONTEXT_REPO_CHAR_BUDGET = int(1000 * _CHARS_PER_TOKEN)
"""リポジトリ節の出力予算（文字数）。1000 トークン相当。"""

CONTEXT_HANDOFF_CHAR_BUDGET = int(300 * _CHARS_PER_TOKEN)
"""前回の続き節の出力予算（文字数）。300 トークン相当。"""

CONTEXT_ITEM_CHAR_LIMIT = 200
"""知識 1 件が占めてよい行の長さ（文字数）。超えるなら body を落として title だけ出す。"""


@dataclass
class Settings:
    """mem のローカルランタイム設定。

    値はすべてハードコード既定値であり、永続化するランタイム状態は持たない。
    """

    log_level: str = "info"

    # --- 導出プロパティ ---

    @property
    def data_path(self) -> Path:
        """データディレクトリ（~/.bluecore）を返す。"""
        return _DEFAULT_DATA_DIR

    @property
    def db_path(self) -> Path:
        """mem.db の絶対パスを返す。"""
        return self.data_path / "mem.db"

    @property
    def log_dir(self) -> Path:
        """ログディレクトリの絶対パスを返す。"""
        return self.data_path / "logs"
