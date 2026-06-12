"""run_with_flags のハーネス別分岐（copilot ブロック変換・非 claude detach）のテスト。"""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from bluecore.hooks import run_with_flags


def _fake_result(returncode: int, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    """subprocess.run の戻り値を模した SimpleNamespace を返す。"""
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


class TestRunTargetBlockConversion:
    """_run_target の exit code 2 ブロック変換のテスト。"""

    def test_claude_pre_hook_block_keeps_exit_2(self, monkeypatch, capsys):
        """Claude では pre: フックの exit 2 がそのまま伝播する。"""
        monkeypatch.setenv("CLAUDECODE", "1")
        monkeypatch.setattr(
            run_with_flags.subprocess, "run", lambda *a, **k: _fake_result(2, stderr="BLOCKED\n")
        )
        code = run_with_flags._run_target("pre:config-protection", "mod", [], "{}")
        assert code == 2
        captured = capsys.readouterr()
        assert "BLOCKED" in captured.err
        assert captured.out == ""

    def test_copilot_pre_hook_block_converts_to_deny_json(self, monkeypatch, capsys):
        """Copilot では pre: フックの exit 2 が deny JSON + exit 0 に変換される。"""
        monkeypatch.setenv("COPILOT_AGENT_PROMPT", "x")
        monkeypatch.setattr(
            run_with_flags.subprocess, "run", lambda *a, **k: _fake_result(2, stderr="BLOCKED: no\n")
        )
        code = run_with_flags._run_target("pre:bash:commit-quality", "mod", [], "{}")
        assert code == 0
        captured = capsys.readouterr()
        assert json.loads(captured.out) == {
            "permissionDecision": "deny",
            "permissionDecisionReason": "BLOCKED: no",
        }

    def test_copilot_block_without_stderr_uses_default_reason(self, monkeypatch, capsys):
        """子の stderr が空なら既定のブロック理由を使う。"""
        monkeypatch.setenv("COPILOT_AGENT_PROMPT", "x")
        monkeypatch.setattr(run_with_flags.subprocess, "run", lambda *a, **k: _fake_result(2))
        code = run_with_flags._run_target("pre:observe", "mod", [], "{}")
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["permissionDecisionReason"] == "Blocked by hook pre:observe"

    def test_copilot_non_pre_hook_exit_2_unchanged(self, monkeypatch, capsys):
        """pre: 以外のフックは Copilot でも exit 2 を変換しない。"""
        monkeypatch.setenv("COPILOT_AGENT_PROMPT", "x")
        monkeypatch.setattr(
            run_with_flags.subprocess, "run", lambda *a, **k: _fake_result(2, stderr="err\n")
        )
        code = run_with_flags._run_target("post:redux:filter", "mod", [], "{}")
        assert code == 2


class TestTruncationGuardConversion:
    """main の truncation guard ブロック変換のテスト。"""

    def _invoke_main(self, monkeypatch) -> int:
        """巨大 stdin で pre:config-protection を起動して終了コードを返す。"""
        monkeypatch.setattr(
            run_with_flags.sys,
            "argv",
            ["run_with_flags", "pre:config-protection", "bluecore.hooks.config_protection"],
        )
        monkeypatch.setattr(run_with_flags, "is_hook_enabled", lambda *a, **k: True)
        big = "x" * (run_with_flags.MAX_STDIN_BYTES + 10)
        monkeypatch.setattr(run_with_flags.sys, "stdin", io.StringIO(big))
        return run_with_flags.main()

    def test_claude_truncation_blocks_with_exit_2(self, monkeypatch, capsys):
        """Claude では切り捨て時に exit 2 でブロックする。"""
        monkeypatch.setenv("CLAUDECODE", "1")
        assert self._invoke_main(monkeypatch) == 2
        assert "BLOCKED" in capsys.readouterr().err

    def test_copilot_truncation_blocks_with_deny_json(self, monkeypatch, capsys):
        """Copilot では切り捨て時に deny JSON + exit 0 でブロックする。"""
        monkeypatch.setenv("COPILOT_AGENT_PROMPT", "x")
        assert self._invoke_main(monkeypatch) == 0
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["permissionDecision"] == "deny"
        assert "BLOCKED" in captured.err


class TestBackgroundDetach:
    """非 claude ハーネスでの BACKGROUND_HOOK_IDS detach のテスト。"""

    def _invoke_main(self, monkeypatch, hook_id: str) -> int:
        """指定 hook_id で main を起動して終了コードを返す。"""
        monkeypatch.setattr(
            run_with_flags.sys, "argv", ["run_with_flags", hook_id, "bluecore.mem.cli"]
        )
        monkeypatch.setattr(run_with_flags, "is_hook_enabled", lambda *a, **k: True)
        monkeypatch.setattr(run_with_flags.sys, "stdin", io.StringIO("{}"))
        return run_with_flags.main()

    def test_codex_background_hook_is_detached(self, monkeypatch):
        """Codex では async フックが detach され即 0 を返す。"""
        monkeypatch.setenv("PLUGIN_DATA", "/tmp/data")
        detached: list[str] = []
        monkeypatch.setattr(
            run_with_flags,
            "_detach_target",
            lambda hook_id, *a, **k: detached.append(hook_id) or 0,
        )
        assert self._invoke_main(monkeypatch, "session:mem:end") == 0
        assert detached == ["session:mem:end"]

    def test_claude_background_hook_runs_normally(self, monkeypatch):
        """Claude では async フックも通常どおり同期実行される（ホストが非同期化）。"""
        monkeypatch.setenv("CLAUDECODE", "1")
        ran: list[str] = []
        monkeypatch.setattr(
            run_with_flags, "_run_target", lambda hook_id, *a, **k: ran.append(hook_id) or 0
        )
        monkeypatch.setattr(
            run_with_flags, "_detach_target", lambda *a, **k: pytest.fail("detach されてはならない")
        )
        assert self._invoke_main(monkeypatch, "session:mem:end") == 0
        assert ran == ["session:mem:end"]

    def test_codex_non_background_hook_runs_normally(self, monkeypatch):
        """非 async フックは Codex でも同期実行される。"""
        monkeypatch.setenv("PLUGIN_DATA", "/tmp/data")
        ran: list[str] = []
        monkeypatch.setattr(
            run_with_flags, "_run_target", lambda hook_id, *a, **k: ran.append(hook_id) or 0
        )
        assert self._invoke_main(monkeypatch, "session:start") == 0
        assert ran == ["session:start"]


class TestDetachTarget:
    """_detach_target のテスト。"""

    def test_detaches_with_stdin_tempfile(self, monkeypatch, tmp_path):
        """stdin を一時ファイル経由で渡し detached 起動する。"""
        monkeypatch.setenv("HOME", str(tmp_path))
        popen_calls: list[dict] = []

        def fake_popen(cmd, **kwargs):
            popen_calls.append({"cmd": cmd, **kwargs})
            assert kwargs["stdin"].read() == '{"k": "v"}'
            return SimpleNamespace(pid=12345)

        monkeypatch.setattr(run_with_flags.subprocess, "Popen", fake_popen)
        assert run_with_flags._detach_target("session:mem:end", "bluecore.mem.cli", [], '{"k": "v"}') == 0
        assert len(popen_calls) == 1
        assert popen_calls[0]["start_new_session"] is True

    def test_tempfile_is_removed_after_launch(self, monkeypatch, tmp_path):
        """stdin 一時ファイルは ~/.bluecore 配下に作成され、起動後に削除される。"""
        monkeypatch.setenv("HOME", str(tmp_path))
        seen_dirs: list[str] = []
        monkeypatch.setattr(
            run_with_flags.subprocess,
            "Popen",
            lambda *a, **k: seen_dirs.append(k["stdin"].name) or SimpleNamespace(pid=1),
        )
        run_with_flags._detach_target("session:mem:end", "bluecore.mem.cli", [], "{}")
        assert len(seen_dirs) == 1
        assert seen_dirs[0].startswith(str(tmp_path / ".bluecore"))
        assert list((tmp_path / ".bluecore").glob("*.stdin")) == []

    def test_tempfile_creation_failure_returns_zero(self, monkeypatch, capsys):
        """一時ファイル作成失敗（ディスク不可等）でも非ブロッキングで 0 を返す。"""
        from bluecore.hooks import hook_common

        def raise_oserror(*a, **k):
            raise OSError("no space")

        monkeypatch.setattr(hook_common.tempfile, "NamedTemporaryFile", raise_oserror)
        assert run_with_flags._detach_target("session:mem:end", "bluecore.mem.cli", [], "{}") == 0
        assert "Error detaching session:mem:end" in capsys.readouterr().err

    def test_popen_oserror_returns_zero_nonblocking(self, monkeypatch, capsys, tmp_path):
        """Popen の OSError は非ブロッキングエラーとして 0 を返す。"""
        monkeypatch.setenv("HOME", str(tmp_path))

        def raise_oserror(*a, **k):
            raise OSError("spawn failed")

        monkeypatch.setattr(run_with_flags.subprocess, "Popen", raise_oserror)
        assert run_with_flags._detach_target("session:mem:end", "bluecore.mem.cli", [], "{}") == 0
        assert "Error detaching session:mem:end" in capsys.readouterr().err

    def test_unlink_failure_is_ignored(self, monkeypatch, tmp_path):
        """一時ファイル削除失敗は無視される。

        os.unlink のモックは os モジュール共有のため hook_common 側にも波及し
        実ファイルが残留する。HOME を tmp_path に隔離して実環境汚染を防ぐ。
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(
            run_with_flags.subprocess, "Popen", lambda *a, **k: SimpleNamespace(pid=1)
        )
        def raise_unlink(_path):
            raise OSError("unlink failed")

        monkeypatch.setattr(run_with_flags.os, "unlink", raise_unlink)
        assert run_with_flags._detach_target("session:mem:end", "bluecore.mem.cli", [], "{}") == 0


def test_background_hook_ids_match_hooks_json():
    """BACKGROUND_HOOK_IDS が hooks.json の async: true エントリと一致する。"""
    import re
    from pathlib import Path

    hooks_json = (
        Path(run_with_flags.__file__).resolve().parents[3] / "hooks" / "hooks.json"
    ).read_text(encoding="utf-8")
    async_ids = {
        re.search(r'run_with_flags "([^"]+)"', entry["command"]).group(1)
        for event_hooks in json.loads(hooks_json)["hooks"].values()
        for matcher_entry in event_hooks
        for entry in matcher_entry["hooks"]
        if entry.get("async") is True and "run_with_flags" in entry["command"]
    }
    assert async_ids == set(run_with_flags.BACKGROUND_HOOK_IDS)
