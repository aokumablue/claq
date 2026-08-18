"""`bash_config_protection` フックのテスト（A-06: Bash 経由の設定ファイル直接書き換え保護）。"""

from __future__ import annotations

import json

import pytest

from bluecore.hooks import bash_config_protection


def _bash_payload(command: str) -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


class TestFindProtectedWrite:
    @pytest.mark.parametrize(
        "command",
        [
            "printf x > pyproject.toml",
            "printf x >> pyproject.toml",
            "echo '[testenv]' > tox.ini",
            "cat file.txt > .eslintrc.json",
            "tee pyproject.toml <<< 'x'",
            "tee -a pyproject.toml <<< 'x'",
            "echo x | tee package.json",
            "tee notes.txt pyproject.toml <<< 'x'",
            "sed -i 's/a/b/' pyproject.toml",
            "sed -i '' pyproject.toml",
            "sed -i.bak 's/a/b/' setup.cfg",
            "cat <<'EOF' > pyproject.toml\nx\nEOF",
            "true && printf x > pyproject.toml",
            "printf x > /tmp/repo/pyproject.toml",
        ],
    )
    def test_detects_protected_write(self, command: str) -> None:
        assert bash_config_protection.find_protected_write(command) is not None

    @pytest.mark.parametrize(
        "command",
        [
            "printf x > notes.txt",
            "cat pyproject.toml",
            "echo pyproject.toml",
            "grep -n commands tox.ini",
            "sed -n '1,5p' pyproject.toml",
            "tee",
            "sed -i",
            "",
        ],
    )
    def test_allows_non_write(self, command: str) -> None:
        assert bash_config_protection.find_protected_write(command) is None


class TestMain:
    def test_main_blocks_redirect_to_protected_file(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(
            bash_config_protection,
            "read_raw_stdin_with_truncation",
            lambda: (_bash_payload("printf x > pyproject.toml"), False),
        )
        assert bash_config_protection.main() == 2
        assert "BLOCKED: Modifying pyproject.toml is not allowed." in capsys.readouterr().err

    def test_main_allows_non_protected_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            bash_config_protection,
            "read_raw_stdin_with_truncation",
            lambda: (_bash_payload("printf x > notes.txt"), False),
        )
        assert bash_config_protection.main() == 0

    def test_main_allows_read_only_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            bash_config_protection,
            "read_raw_stdin_with_truncation",
            lambda: (_bash_payload("cat pyproject.toml"), False),
        )
        assert bash_config_protection.main() == 0

    def test_main_ignores_non_bash_tool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = json.dumps(
            {"tool_name": "Write", "tool_input": {"file_path": "pyproject.toml", "content": "x"}}
        )
        monkeypatch.setattr(
            bash_config_protection, "read_raw_stdin_with_truncation", lambda: (payload, False)
        )
        assert bash_config_protection.main() == 0

    def test_main_empty_input_is_non_blocking(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            bash_config_protection, "read_raw_stdin_with_truncation", lambda: ("", False)
        )
        assert bash_config_protection.main() == 0

    def test_main_truncated_input_is_fail_closed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(
            bash_config_protection,
            "read_raw_stdin_with_truncation",
            lambda: ("{not-even-json", True),
        )
        assert bash_config_protection.main() == 2
        assert "exceeded" in capsys.readouterr().err

    def test_main_malformed_json_with_write_risk_is_blocked(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """JSON が壊れていても生テキストに保護対象 + 書き込み指示が見えれば deny する。"""
        monkeypatch.setattr(
            bash_config_protection,
            "read_raw_stdin_with_truncation",
            lambda: ("not-json but printf x > pyproject.toml appears here", False),
        )
        assert bash_config_protection.main() == 2

    def test_main_malformed_json_without_write_risk_passes_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            bash_config_protection, "read_raw_stdin_with_truncation", lambda: ("not-json at all", False)
        )
        assert bash_config_protection.main() == 0

    def test_main_malformed_json_with_indicator_but_no_protected_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """書き込み指示だけあっても保護対象ファイル名が無ければ通す。"""
        monkeypatch.setattr(
            bash_config_protection,
            "read_raw_stdin_with_truncation",
            lambda: ("not-json but > notes.txt appears here", False),
        )
        assert bash_config_protection.main() == 0

    def test_module_entrypoint_runs_main(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import runpy

        # runpy が bash_config_protection を fresh に再実行するため、
        # hook_common（sys.modules にキャッシュされ再利用される側）を patch する。
        monkeypatch.setattr(
            "bluecore.hooks.hook_common.read_raw_stdin_with_truncation", lambda: ("", False)
        )
        with pytest.raises(SystemExit) as excinfo:
            runpy.run_module("bluecore.hooks.bash_config_protection", run_name="__main__")
        assert excinfo.value.code == 0
