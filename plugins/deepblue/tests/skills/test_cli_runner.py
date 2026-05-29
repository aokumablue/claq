"""cli_runner モジュールのテスト。

デシジョンテーブル (detect_cli_binary):
  | # | CLAUDECODE | copilot 存在 | 期待値    |
  |---|-----------|-------------|---------|
  | 1 | "1"       | any         | "claude" |
  | 2 | 未設定     | True        | "copilot"|
  | 3 | 未設定     | False       | "claude" |

デシジョンテーブル (build_tools_args):
  | # | binary   | tools          | 期待値                                |
  |---|---------|----------------|--------------------------------------|
  | 1 | "claude" | ["Read","Write"] | ["--allowedTools", "Read,Write"]    |
  | 2 | "copilot"| ["Read","Write"] | ["--allow-tool","Read","--allow-tool","Write"] |

デシジョンテーブル (build_output_format_args):
  | # | binary   | fmt          | 期待値                            |
  |---|---------|--------------|----------------------------------|
  | 1 | "claude" | "stream-json" | ["--output-format", "stream-json"]|
  | 2 | "copilot"| "stream-json" | ["--output-format", "json"]       |
  | 3 | "copilot"| "text"        | ["--output-format", "text"]       |
  | 4 | "claude" | "text"        | ["--output-format", "text"]       |

デシジョンテーブル (run_cli):
  | # | 条件                          | 期待値                            |
  |---|------------------------------|---------------------------------|
  | 1 | CLAUDECODE=1                  | binary="claude" で実行            |
  | 2 | CLAUDECODE 未設定              | binary="copilot" で実行（存在時） |
  | 3 | strip_claudecode_env=True      | CLAUDECODE が env から除去される  |
  | 4 | strip_claudecode_env=False     | CLAUDECODE が env に残る          |
  | 5 | stdin_input あり               | input= に渡される                 |
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from devgear.skills import cli_runner

# ========================
# detect_cli_binary テスト
# ========================


class TestDetectCliBinary:
    """detect_cli_binary のデシジョンテーブルテスト。"""

    def test_claudecode_set_returns_claude(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ケース1: CLAUDECODE=1 のとき必ず "claude" を返す。"""
        monkeypatch.setenv("CLAUDECODE", "1")
        monkeypatch.setattr(cli_runner.shutil, "which", lambda name: "/usr/bin/" + name)
        assert cli_runner.detect_cli_binary() == "claude"

    def test_no_claudecode_copilot_exists_returns_copilot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ケース2: CLAUDECODE 未設定で copilot が PATH に存在するとき "copilot" を返す。"""
        monkeypatch.delenv("CLAUDECODE", raising=False)
        monkeypatch.setattr(cli_runner.shutil, "which", lambda name: "/usr/bin/copilot" if name == "copilot" else None)
        assert cli_runner.detect_cli_binary() == "copilot"

    def test_no_claudecode_no_copilot_returns_claude(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ケース3: CLAUDECODE 未設定で copilot が存在しないとき "claude" にフォールバック。"""
        monkeypatch.delenv("CLAUDECODE", raising=False)
        monkeypatch.setattr(cli_runner.shutil, "which", lambda name: None)
        assert cli_runner.detect_cli_binary() == "claude"


# ========================
# build_tools_args テスト
# ========================


class TestBuildToolsArgs:
    """build_tools_args のテスト。"""

    def test_claude_uses_allowedtools_flag(self) -> None:
        """ケース1: claude では --allowedTools にカンマ区切りで渡す。"""
        result = cli_runner.build_tools_args("claude", ["Read", "Write"])
        assert result == ["--allowedTools", "Read,Write"]

    def test_copilot_uses_per_tool_flags(self) -> None:
        """ケース2: copilot では --allow-tool をツールごとに繰り返す。"""
        result = cli_runner.build_tools_args("copilot", ["Read", "Write"])
        assert result == ["--allow-tool", "Read", "--allow-tool", "Write"]

    def test_empty_tools_list(self) -> None:
        """ツールリストが空のとき claude は空文字列、copilot は空リスト。"""
        assert cli_runner.build_tools_args("claude", []) == ["--allowedTools", ""]
        assert cli_runner.build_tools_args("copilot", []) == []


# ========================
# build_output_format_args テスト
# ========================


class TestBuildOutputFormatArgs:
    """build_output_format_args のテスト。"""

    def test_claude_stream_json_unchanged(self) -> None:
        """ケース1: claude + stream-json はそのまま。"""
        result = cli_runner.build_output_format_args("claude", "stream-json")
        assert result == ["--output-format", "stream-json"]

    def test_copilot_stream_json_becomes_json(self) -> None:
        """ケース2: copilot + stream-json は json に変換される。"""
        result = cli_runner.build_output_format_args("copilot", "stream-json")
        assert result == ["--output-format", "json"]

    def test_copilot_text_unchanged(self) -> None:
        """ケース3: copilot + text はそのまま。"""
        result = cli_runner.build_output_format_args("copilot", "text")
        assert result == ["--output-format", "text"]

    def test_claude_text_unchanged(self) -> None:
        """ケース4: claude + text はそのまま。"""
        result = cli_runner.build_output_format_args("claude", "text")
        assert result == ["--output-format", "text"]


# ========================
# run_cli テスト
# ========================


class TestRunCli:
    """run_cli のデシジョンテーブルテスト。"""

    def test_claudecode_uses_claude_binary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ケース1: CLAUDECODE=1 のとき claude バイナリで実行する。"""
        monkeypatch.setenv("CLAUDECODE", "1")
        captured = {}

        def fake_run(cmd, **kwargs):  # noqa: ANN001
            captured["cmd"] = cmd
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(cli_runner.subprocess, "run", fake_run)
        cli_runner.run_cli(["-p", "hello"])
        assert captured["cmd"][0] == "claude"

    def test_no_claudecode_copilot_uses_copilot_binary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ケース2: CLAUDECODE 未設定で copilot が存在するとき copilot バイナリで実行する。"""
        monkeypatch.delenv("CLAUDECODE", raising=False)
        monkeypatch.setattr(cli_runner.shutil, "which", lambda name: "/usr/bin/copilot" if name == "copilot" else None)
        captured = {}

        def fake_run(cmd, **kwargs):  # noqa: ANN001
            captured["cmd"] = cmd
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(cli_runner.subprocess, "run", fake_run)
        cli_runner.run_cli(["-p", "hello"])
        assert captured["cmd"][0] == "copilot"

    def test_strip_claudecode_env_removes_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ケース3: strip_claudecode_env=True のとき CLAUDECODE が env から除去される。"""
        monkeypatch.setenv("CLAUDECODE", "1")
        monkeypatch.setenv("OTHER", "keep")
        captured = {}

        def fake_run(cmd, **kwargs):  # noqa: ANN001
            captured["env"] = kwargs.get("env", {})
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(cli_runner.subprocess, "run", fake_run)
        cli_runner.run_cli(["-p", "hello"], strip_claudecode_env=True)
        assert "CLAUDECODE" not in captured["env"]
        assert captured["env"].get("OTHER") == "keep"

    def test_no_strip_claudecode_env_keeps_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ケース4: strip_claudecode_env=False のとき CLAUDECODE が env に残る。"""
        monkeypatch.setenv("CLAUDECODE", "1")
        captured = {}

        def fake_run(cmd, **kwargs):  # noqa: ANN001
            captured["env"] = kwargs.get("env", {})
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(cli_runner.subprocess, "run", fake_run)
        cli_runner.run_cli(["-p", "hello"], strip_claudecode_env=False)
        assert "CLAUDECODE" in captured["env"]

    def test_stdin_input_passed_to_subprocess(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ケース5: stdin_input が subprocess の input= に渡される。"""
        monkeypatch.setenv("CLAUDECODE", "1")
        captured = {}

        def fake_run(cmd, **kwargs):  # noqa: ANN001
            captured["input"] = kwargs.get("input")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(cli_runner.subprocess, "run", fake_run)
        cli_runner.run_cli(["-p"], stdin_input="my prompt")
        assert captured["input"] == "my prompt"

    def test_args_appended_after_binary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """binary の後に引数が付加されること。"""
        monkeypatch.setenv("CLAUDECODE", "1")
        captured = {}

        def fake_run(cmd, **kwargs):  # noqa: ANN001
            captured["cmd"] = cmd
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(cli_runner.subprocess, "run", fake_run)
        cli_runner.run_cli(["-p", "hello", "--model", "haiku"])
        assert captured["cmd"] == ["claude", "-p", "hello", "--model", "haiku"]
