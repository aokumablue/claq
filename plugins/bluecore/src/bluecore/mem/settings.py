"""SQLite ベースの mem ランタイム設定。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from bluecore.lib.constants import BASE_DIR_NAME

_DEFAULT_DATA_DIR = Path(os.environ["BLUECORE_DATA_PATH"]) if "BLUECORE_DATA_PATH" in os.environ else Path.home() / BASE_DIR_NAME
_DEFAULT_EMBEDDING_MODEL = "hotchpotch/static-embedding-japanese"
# HF Hub commit SHA をピン留めし、サプライチェーン攻撃（名前空間再利用・改竄プッシュ）を防ぐ
_DEFAULT_EMBEDDING_REVISION = "95b3d9c80a7ccf604e2b5daee7b1b3eed6b1a9d3"

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
class ReduxSettings:
    """redux コマンド別トークン圧縮の設定（すべてハードコード既定値）"""

    enabled: bool = True
    smart_filter_enabled: bool = True
    group_lint_enabled: bool = True
    dedup_enabled: bool = True
    smart_truncate_enabled: bool = True
    max_output_len: int = 3000
    head_lines: int = 30
    tail_lines: int = 30
    dedup_threshold: int = 3


@dataclass
class Settings:
    """mem のローカルランタイム設定。

    値はすべてハードコード既定値であり、永続化するランタイム状態は持たない。
    """

    log_level: str = "info"
    embedding_model: str = _DEFAULT_EMBEDDING_MODEL
    redux: ReduxSettings = field(default_factory=ReduxSettings)

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
