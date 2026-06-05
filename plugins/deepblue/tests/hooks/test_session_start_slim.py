"""SessionStart の slim スキル注入に関するテスト。

frontmatter を除去し、恒久的な応答文体命令としてヘッダー/フッターで
囲んで注入することを検証する。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from deepblue.hooks import session_start


class TestStripFrontmatter:
    """_strip_frontmatter のテスト"""

    def test_removes_frontmatter_block(self) -> None:
        """先頭の frontmatter を除去し本文のみ返す（先頭空行も除去）。"""
        text = "---\nname: x\ncontext: fork\n---\n\n本文ここ\n二行目"
        assert session_start._strip_frontmatter(text) == "本文ここ\n二行目"

    def test_returns_as_is_without_frontmatter(self) -> None:
        """先頭が --- でなければ元のテキストをそのまま返す。"""
        text = "本文だけ\n2行目"
        assert session_start._strip_frontmatter(text) == text

    def test_returns_as_is_when_unclosed(self) -> None:
        """閉じの --- が無ければ元のテキストをそのまま返す。"""
        text = "---\nname: x\n本文（閉じなし）"
        assert session_start._strip_frontmatter(text) == text


class TestInjectSlimSkill:
    """_inject_slim_skill のテスト"""

    def test_enabled_wraps_body_as_directive(self) -> None:
        """有効時、frontmatter を除去し命令ヘッダー/フッターで囲んだ本文を返す。"""
        with patch.object(session_start, "Settings") as mock_settings:
            mock_settings.load.return_value.slim.enabled = True
            result = session_start._inject_slim_skill()

        assert len(result) == 1
        block = result[0]
        # 命令ヘッダー/フッターで囲まれている
        assert '<assistant-output-style id="slim"' in block
        assert "無視不可" in block
        assert "</assistant-output-style>" in block
        # frontmatter のメタ行は除去されている
        assert "user-invocable:" not in block
        assert "context: fork" not in block
        assert "name: slim" not in block
        # SKILL.md 本文は保持されている
        assert "原始人" in block
        assert "削除対象" in block

    def test_disabled_returns_empty(self) -> None:
        """slim 無効時は空リストを返す。"""
        with patch.object(session_start, "Settings") as mock_settings:
            mock_settings.load.return_value.slim.enabled = False
            assert session_start._inject_slim_skill() == []

    def test_missing_file_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """SKILL.md が存在しなければ空リストを返す。"""
        monkeypatch.setattr(session_start, "_SLIM_SKILL_PATH", tmp_path / "nope.md")
        with patch.object(session_start, "Settings") as mock_settings:
            mock_settings.load.return_value.slim.enabled = True
            assert session_start._inject_slim_skill() == []

    def test_exception_logs_and_returns_empty(self) -> None:
        """例外時はサニタイズログを出し空リストを返す。"""
        with (
            patch.object(session_start, "Settings") as mock_settings,
            patch.object(session_start, "_log_sanitized_exception") as mock_log,
        ):
            mock_settings.load.side_effect = RuntimeError("boom")
            assert session_start._inject_slim_skill() == []
            mock_log.assert_called_once()
