"""
Claude Home 用インストールターゲットアダプター。
"""

from __future__ import annotations

from .install_target_helpers import InstallTargetConfig, create_install_target_adapter

claude_home_adapter = create_install_target_adapter(
    InstallTargetConfig(
        id="claude-home",
        target="claude",
        kind="home",
        root_segments=[".claude"],
        install_state_path_segments=["bluecore", "install-state.json"],
        native_root_relative_path=".claude-plugin",
    )
)

__all__ = ["claude_home_adapter"]
