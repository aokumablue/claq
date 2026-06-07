"""slim_fallback の環境判定付きフォールバック注入に関するテスト。

Claude Code（CLAUDECODE あり）では output-style が適用されるため注入しない。
output-styles 非対応環境（CLAUDECODE なし、GitHub Copilot 等）では
output-styles/slim.md の本文を frontmatter 除去して注入する。
"""

from __future__ import annotations

import pytest

from bluecore.hooks import slim_fallback


class TestOutputStylesSupported:
    """output_styles_supported のテスト"""

    def test_true_when_claudecode_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CLAUDECODE が設定されていれば True。"""
        monkeypatch.setenv("CLAUDECODE", "1")
        assert slim_fallback.output_styles_supported() is True

    def test_false_when_claudecode_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CLAUDECODE が未設定なら False。"""
        monkeypatch.delenv("CLAUDECODE", raising=False)
        assert slim_fallback.output_styles_supported() is False

    def test_false_when_claudecode_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CLAUDECODE が空文字なら False。"""
        monkeypatch.setenv("CLAUDECODE", "")
        assert slim_fallback.output_styles_supported() is False


class TestStripFrontmatter:
    """strip_frontmatter のテスト"""

    def test_removes_frontmatter_block(self) -> None:
        """先頭の frontmatter を除去し本文のみ返す（先頭空行も除去）。"""
        text = "---\nname: x\nkeep-coding-instructions: true\n---\n\n本文ここ\n二行目"
        assert slim_fallback.strip_frontmatter(text) == "本文ここ\n二行目"

    def test_returns_as_is_without_frontmatter(self) -> None:
        """先頭が --- でなければ元のテキストをそのまま返す。"""
        text = "本文だけ\n2行目"
        assert slim_fallback.strip_frontmatter(text) == text

    def test_returns_as_is_when_unclosed(self) -> None:
        """閉じの --- が無ければ元のテキストをそのまま返す。"""
        text = "---\nname: x\n本文（閉じなし）"
        assert slim_fallback.strip_frontmatter(text) == text


class TestInjectSlimSkill:
    """inject_slim_skill のテスト"""

    def test_skips_when_output_styles_supported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLAUDECODE あり（Claude Code）では output-style 任せで注入しない。"""
        monkeypatch.setenv("CLAUDECODE", "1")
        assert slim_fallback.inject_slim_skill() == []

    def test_injects_body_when_unsupported(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """CLAUDECODE なしでは slim.md 本文を frontmatter 除去して注入する。"""
        monkeypatch.delenv("CLAUDECODE", raising=False)
        style = tmp_path / "slim.md"
        style.write_text(
            "---\nname: slim\n---\n\n原始人口調\n対象外: ツール呼び出し", encoding="utf-8"
        )
        monkeypatch.setattr(slim_fallback, "_SLIM_STYLE_PATH", style)

        result = slim_fallback.inject_slim_skill()

        assert len(result) == 1
        assert result[0] == "原始人口調\n対象外: ツール呼び出し"
        assert "name: slim" not in result[0]

    def test_missing_file_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """slim.md が存在しなければ空リストを返す。"""
        monkeypatch.delenv("CLAUDECODE", raising=False)
        monkeypatch.setattr(slim_fallback, "_SLIM_STYLE_PATH", tmp_path / "nope.md")
        assert slim_fallback.inject_slim_skill() == []

    def test_exception_logs_and_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """読み込み例外時はサニタイズログ（改行/制御文字除去）を出し空リストを返す。"""
        monkeypatch.delenv("CLAUDECODE", raising=False)

        class _Boom:
            def exists(self) -> bool:
                return True

            def read_text(self, encoding: str = "utf-8") -> str:
                raise RuntimeError("boom\nbad\x1b[31m")

        monkeypatch.setattr(slim_fallback, "_SLIM_STYLE_PATH", _Boom())
        logged: list[str] = []
        monkeypatch.setattr(slim_fallback, "log", lambda msg: logged.append(msg))

        assert slim_fallback.inject_slim_skill() == []
        assert len(logged) == 1
        assert "Slim injection error" in logged[0]
        assert "\n" not in logged[0]
        assert "\x1b" not in logged[0]
