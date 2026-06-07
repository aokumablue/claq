"""redux 圧縮パイプラインの設定。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ReduxConfig:
    """トークン圧縮パイプラインの実行時設定。

    宣言的フィルタ（TOML）が参照するアルゴリズム戦略のパラメータを保持する。
    値はすべてハードコード既定で、:class:`bluecore.mem.settings.ReduxSettings`
    から構築される。
    """

    enabled: bool = True
    smart_filter_enabled: bool = True
    group_lint_enabled: bool = True
    dedup_enabled: bool = True
    smart_truncate_enabled: bool = True
    max_output_len: int = 3000
    head_lines: int = 30
    tail_lines: int = 30
    dedup_threshold: int = 3
