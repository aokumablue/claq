"""SQLite ベースの mem ランタイム設定。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from claq.lib.constants import BASE_DIR_NAME
from claq.lib.core_utils import get_home_dir


def _default_data_dir() -> Path:
    """mem のデータディレクトリ既定値を返す。

    `Path.home()` を直接見ていた頃は、`CLAQ_HOME` を設定しても DB だけが本物の
    `~/.claq` を掴んでいた（transcript の trusted root は `get_home_dir()` 経由で
    移るのに DB は移らない、という非対称）。隔離したつもりのテストが利用者の
    実データへ書く事故になるため、`core_utils.get_home_dir()` へ揃える。

    import 時ではなく呼び出し時に評価する。import 時に固定すると、プロセス内で
    環境変数を差し替えても効かない。

    Returns:
        データディレクトリのパス
    """
    override = os.environ.get("CLAQ_DATA_PATH")
    if override:
        return Path(override)
    return get_home_dir() / BASE_DIR_NAME

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
        """データディレクトリ（~/.claq）を返す。"""
        return _default_data_dir()

    @property
    def db_path(self) -> Path:
        """mem.db の絶対パスを返す。"""
        return self.data_path / "mem.db"

    @property
    def log_dir(self) -> Path:
        """ログディレクトリの絶対パスを返す。"""
        return self.data_path / "logs"
