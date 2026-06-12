"""bluecore.hooks.harness_probe のテスト。"""

from __future__ import annotations

import io
import json
import sys

import pytest

from bluecore.hooks import harness_probe


class TestSnapshotEnv:
    """_snapshot_env のテスト。"""

    def test_collects_harness_prefixed_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ハーネス関連プレフィックスの環境変数のみ収集する。"""
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/plug")
        monkeypatch.setenv("COPILOT_AGENT_PROMPT", "p")
        monkeypatch.setenv("UNRELATED_VAR", "x")
        monkeypatch.setenv("PLUGIN_ROOT", "/other")
        env = harness_probe._snapshot_env()
        assert env["CLAUDE_PLUGIN_ROOT"] == "/plug"
        assert env["COPILOT_AGENT_PROMPT"] == "p"
        assert "UNRELATED_VAR" not in env
        assert "PLUGIN_ROOT" not in env

    def test_redacts_sensitive_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """認証情報らしきキーと PLUGIN_DATA は値を redact する。"""
        monkeypatch.setenv("COPILOT_TOKEN", "secret-token")
        monkeypatch.setenv("CODEX_API_KEY", "secret-key")
        monkeypatch.setenv("PLUGIN_DATA", '{"auth": "x"}')
        env = harness_probe._snapshot_env()
        assert env["COPILOT_TOKEN"] == "<redacted>"
        assert env["CODEX_API_KEY"] == "<redacted>"
        assert env["PLUGIN_DATA"] == "<redacted>"


class TestSnapshotStdin:
    """_snapshot_stdin のテスト。"""

    def test_default_records_keys_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """既定では JSON キー一覧と長さのみ記録する（値は残さない）。"""
        monkeypatch.delenv("BLUECORE_PROBE_STDIN", raising=False)
        raw = '{"tool_name": "Write", "tool_input": {"content": "API_KEY=x"}}'
        snapshot = harness_probe._snapshot_stdin(raw)
        assert snapshot == {"length": len(raw), "keys": ["tool_input", "tool_name"]}

    def test_default_non_json_records_length_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """JSON でない stdin はキー無し（None）で長さのみ記録する。"""
        monkeypatch.delenv("BLUECORE_PROBE_STDIN", raising=False)
        assert harness_probe._snapshot_stdin("not json") == {"length": 8, "keys": None}

    def test_opt_in_records_full_stdin_truncated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """BLUECORE_PROBE_STDIN 真値時のみ全文（上限つき）を記録する。"""
        monkeypatch.setenv("BLUECORE_PROBE_STDIN", "1")
        snapshot = harness_probe._snapshot_stdin("x" * 10000)
        assert snapshot == "x" * harness_probe._MAX_STDIN_SNAPSHOT


class TestBuildRecord:
    """build_record のテスト。"""

    def test_record_contains_required_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """判定結果・cwd・stdin スナップショットを含む。"""
        monkeypatch.setenv("CLAUDECODE", "1")
        monkeypatch.setenv("BLUECORE_PROBE_STDIN", "1")
        record = harness_probe.build_record('{"tool_name": "Bash"}')
        assert record["detected_harness"] == "claude"
        assert record["stdin"] == '{"tool_name": "Bash"}'
        assert record["cwd"]
        assert record["timestamp"]


class TestMain:
    """main のテスト。"""

    def test_appends_jsonl_record(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        """JSON Lines 形式でレコードを追記し 0 を返す。"""
        log_path = tmp_path / "logs" / "harness_probe.log"
        monkeypatch.setattr(harness_probe, "_LOG_PATH", log_path)
        monkeypatch.setenv("BLUECORE_PROBE_STDIN", "1")
        monkeypatch.setattr(sys, "stdin", io.StringIO('{"k": 1}'))
        assert harness_probe.main() == 0
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["stdin"] == '{"k": 1}'

    def test_truncates_oversized_log(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        """上限超過したログは追記前に truncate する。"""
        log_path = tmp_path / "probe.log"
        log_path.write_text("old\n" * 10, encoding="utf-8")
        monkeypatch.setattr(harness_probe, "_LOG_PATH", log_path)
        monkeypatch.setattr(harness_probe, "_MAX_LOG_BYTES", 10)
        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
        assert harness_probe.main() == 0
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        assert "old" not in lines[0]

    def test_tty_stdin_records_empty(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        """TTY stdin（手動起動）では stdin を読まず空で記録する。"""
        log_path = tmp_path / "probe.log"
        monkeypatch.setattr(harness_probe, "_LOG_PATH", log_path)
        monkeypatch.setenv("BLUECORE_PROBE_STDIN", "1")

        class _Tty(io.StringIO):
            def isatty(self) -> bool:
                return True

        monkeypatch.setattr(sys, "stdin", _Tty())
        assert harness_probe.main() == 0
        assert json.loads(log_path.read_text(encoding="utf-8"))["stdin"] == ""

    def test_error_is_caught_and_returns_zero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """ログ書き込み失敗でも例外を出さず 0 を返す。"""
        monkeypatch.setattr(
            harness_probe, "build_record", lambda raw: (_ for _ in ()).throw(OSError("disk"))
        )
        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
        assert harness_probe.main() == 0
        assert "[HarnessProbe] error" in capsys.readouterr().err

    def test_entrypoint(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        """__main__ 実行で SystemExit(0) する。"""
        import runpy

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
        with pytest.raises(SystemExit) as excinfo:
            runpy.run_module("bluecore.hooks.harness_probe", run_name="__main__")
        assert excinfo.value.code == 0
