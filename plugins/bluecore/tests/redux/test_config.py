"""ReduxConfig のユニットテスト。"""

from __future__ import annotations

from bluecore.redux.config import ReduxConfig


class TestReduxConfig:
    """ReduxConfig のデフォルト値とフィールド。"""

    def test_defaults(self) -> None:
        cfg = ReduxConfig()
        assert cfg.enabled is True
        assert cfg.smart_filter_enabled is True
        assert cfg.group_lint_enabled is True
        assert cfg.dedup_enabled is True
        assert cfg.smart_truncate_enabled is True
        assert cfg.max_output_len == 3000
        assert cfg.head_lines == 30
        assert cfg.tail_lines == 30
        assert cfg.dedup_threshold == 3

    def test_overrides(self) -> None:
        cfg = ReduxConfig(enabled=False, max_output_len=500, dedup_threshold=5)
        assert cfg.enabled is False
        assert cfg.max_output_len == 500
        assert cfg.dedup_threshold == 5
