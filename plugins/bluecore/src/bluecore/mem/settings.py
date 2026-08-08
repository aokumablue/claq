"""SQLite ベースの mem ランタイム設定。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from bluecore.lib.constants import BASE_DIR_NAME

_DEFAULT_DATA_DIR = Path(os.environ["BLUECORE_DATA_PATH"]) if "BLUECORE_DATA_PATH" in os.environ else Path.home() / BASE_DIR_NAME
_DEFAULT_EMBEDDING_MODEL = "hotchpotch/static-embedding-japanese"
# HF Hub commit SHA をピン留めし、サプライチェーン攻撃（名前空間再利用・改竄プッシュ）を防ぐ
_DEFAULT_EMBEDDING_REVISION = "95b3d9c80a7ccf604e2b5daee7b1b3eed6b1a9d3"


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

    閾値類はすべてハードコード既定値。永続化するのは ``last_compacted_at``
    （自動圧縮の実行時刻）のみで、``state_path`` に保存する。
    """

    log_level: str = "info"
    excluded_projects: list[str] = field(default_factory=list)
    embedding_model: str = _DEFAULT_EMBEDDING_MODEL
    search_half_life_days: float = 30.0
    chunk_max_length: int = 2000
    context_chunk_count: int = 30
    # 2層メモリ設定（hot=400: 直近生チャンク, digest=800: セッション要約）
    context_hot_tokens: int = 400
    context_hot_hours: int = 24
    context_digest_tokens: int = 800
    context_digest_count: int = 12
    auto_compact_enabled: bool = True
    auto_compact_interval_days: int = 7
    last_compacted_at: float = 0.0  # ランタイム状態
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

    @property
    def state_path(self) -> Path:
        """ランタイム状態（last_compacted_at）ファイルの絶対パスを返す。"""
        return self.data_path / "mem_state.json"

    # --- 永続化 ---

    def save(self) -> None:
        """ランタイム状態（last_compacted_at）を state_path に保存する。

        他の設定値はすべてハードコード既定値のため永続化しない。
        """
        self.data_path.mkdir(parents=True, exist_ok=True)
        state = {"last_compacted_at": self.last_compacted_at}
        tmp_path = self.state_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp_path.replace(self.state_path)
        self.state_path.chmod(0o600)

    @classmethod
    def load(cls, state_path: Path | None = None) -> Settings:
        """既定値で Settings を構築し、保存済みのランタイム状態があれば復元する。

        Args:
            state_path: テスト用に state ファイルのパスを直接指定する場合に使う。
                省略時は既定データディレクトリの ``mem_state.json`` を読む。

        Returns:
            構築された Settings インスタンス。state ファイルが存在しない・
            読み込みに失敗した場合はハードコード既定値のみを持つ。
        """
        settings = cls()
        path = state_path or settings.state_path
        if not path.exists():
            return settings

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return settings

        if isinstance(raw, dict):
            settings.last_compacted_at = float(raw.get("last_compacted_at", 0.0) or 0.0)
        return settings
