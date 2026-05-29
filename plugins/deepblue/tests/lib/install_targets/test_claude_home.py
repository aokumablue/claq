"""claude_home アダプターのテスト。"""

from __future__ import annotations

from devgear.lib.install_targets.claude_home import claude_home_adapter


class TestClaudeHomeAdapter:
    """claude_home_adapter のテスト。"""

    def test_adapter_id(self):
        """正しい ID を持つこと。"""
        assert claude_home_adapter.id == "claude-home"

    def test_adapter_target(self):
        """正しい target を持つこと。"""
        assert claude_home_adapter.target == "claude"

    def test_adapter_kind(self):
        """home スコープであること。"""
        assert claude_home_adapter.kind == "home"

    def test_supports_claude(self):
        """'claude' ターゲットをサポートすること。"""
        assert claude_home_adapter.supports("claude") is True

    def test_supports_claude_home(self):
        """'claude-home' ターゲットをサポートすること。"""
        assert claude_home_adapter.supports("claude-home") is True

    def test_does_not_support_other(self):
        """他のターゲットはサポートしないこと。"""
        assert claude_home_adapter.supports("other") is False

    def test_resolve_root(self, tmp_path):
        """root を ~/.claude に解決すること。"""
        result = claude_home_adapter.resolve_root(home_dir=str(tmp_path))
        expected = str(tmp_path / ".claude")
        assert result == expected

    def test_get_install_state_path(self, tmp_path):
        """正しい install state パスを取得すること。"""
        result = claude_home_adapter.get_install_state_path(home_dir=str(tmp_path))
        expected = str(tmp_path / ".claude" / "devgear" / "install-state.json")
        assert result == expected

    def test_native_root_relative_path(self):
        """正しい native root relative path を持つこと。"""
        assert claude_home_adapter.native_root_relative_path == ".claude-plugin"

    def test_determine_strategy_for_plugin(self):
        """.claude-plugin では sync-root-children を使うこと。"""
        strategy = claude_home_adapter.determine_strategy(".claude-plugin")
        assert strategy == "sync-root-children"

    def test_determine_strategy_for_normal_path(self):
        """通常パスでは preserve-relative-path を使うこと。"""
        strategy = claude_home_adapter.determine_strategy("agents/test.md")
        assert strategy == "preserve-relative-path"

    def test_resolve_destination_for_plugin(self, tmp_path):
        """.claude-plugin を root に解決すること。"""
        result = claude_home_adapter.resolve_destination_path(
            ".claude-plugin",
            home_dir=str(tmp_path),
        )
        expected = str(tmp_path / ".claude")
        assert result == expected

    def test_resolve_destination_for_normal_path(self, tmp_path):
        """通常パスを正しく解決すること。"""
        result = claude_home_adapter.resolve_destination_path(
            "agents/test.md",
            home_dir=str(tmp_path),
        )
        expected = str(tmp_path / ".claude" / "agents" / "test.md")
        assert result == expected

    def test_create_scaffold_operation(self, tmp_path):
        """scaffold operation を正しく作成すること。"""
        op = claude_home_adapter.create_scaffold_operation(
            "tdd-module",
            "commands/c-tdd.md",
            repo_root=str(tmp_path / "source"),
            home_dir=str(tmp_path / "home"),
        )
        assert op.module_id == "tdd-module"
        assert op.source_relative_path == "commands/c-tdd.md"
        assert op.destination_path == str(tmp_path / "home" / ".claude" / "commands/c-tdd.md")

    def test_plan_operations(self, tmp_path):
        """モジュール向け operation を計画すること。"""
        modules = [
            {"id": "core", "paths": ["agents/a-plan.md", "skills/s-tdd.md"]},
        ]
        ops = claude_home_adapter.plan_operations(
            modules=modules,
            home_dir=str(tmp_path),
        )
        assert len(ops) == 2
        assert ops[0].module_id == "core"
        assert ops[0].source_relative_path == "agents/a-plan.md"

    def test_validate_success(self, tmp_path):
        """home_dir を指定した場合に正常に検証できること。"""
        issues = claude_home_adapter.validate(home_dir=str(tmp_path))
        assert issues == []
