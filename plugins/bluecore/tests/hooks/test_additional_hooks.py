"""低カバレッジの hook モジュールをまとめて検証するテスト。"""

from __future__ import annotations

import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from bluecore.hooks import (
    block_no_verify,
    pre_compact,
    session_end,
)

# ``--no-verify``。テストケース表の横幅を抑えるための別名。
NV = "--no-verify"

# ブロックされなければならないコマンド。
_BYPASS_COMMANDS = [
    # 素の形
    f"git commit {NV}",
    f"git push {NV}",
    "git commit -n",
    f'git commit {NV} -m "fix: x"',
    f"git commit {NV} -m 'wip'",
    # git グローバルオプションを挟むバイパス（実際に突破された形）
    f"git --no-pager commit {NV}",
    f"git -C /tmp/repo commit {NV}",
    f"git -c core.hooksPath=/dev/null commit {NV}",
    f"git --git-dir /x --work-tree /y commit {NV}",
    f"git --git-dir=/x commit {NV}",
    f"git --exec-path /x commit {NV}",
    # segment 先頭が git 以外のバイパス（環境変数プレフィックス・ラッパー・パス付き）
    f"GIT_DIR=.git git commit {NV}",
    f"FOO=bar git commit {NV}",
    f"env git commit {NV}",
    f"sudo git commit {NV}",
    f"nice git commit {NV}",
    f"command git commit {NV}",
    f"/usr/bin/git commit {NV}",
    f"xargs -I% git commit {NV}",
    f"timeout 5 git commit {NV}",
    # 1 segment 内に git トークンが複数（先頭側はユーザー名 / 別コマンドの引数）
    "sudo -u git git commit -n",
    "git submodule foreach git commit -n",
    # 改行区切り（shlex では区切りトークンにならず 1 segment になる）
    "git add -A\ngit commit -n",
    # 複合コマンド（空白あり / なし）
    f"git add -A && git --no-pager commit {NV}",
    f"git add -A&&git commit {NV}",
    "git add -A; git commit -n -m x",
    f"git status | grep x || git commit {NV}",
    f"&& git commit {NV}",
    # 短オプションのクラスタ
    "git commit -an -m x",
    "git commit -u -n",
    "git commit -F - -n",
    # 未知のグローバルオプションで subcommand 解決がずれる形は fail-closed
    f"git --future-option value commit {NV}",
    "git --future-option value commit -n",
    "git -Z value commit -n",
    # shlex 失敗（未閉じクォート）は fail-closed
    f'git commit -m "unclosed {NV}',
]

# 通さなければならないコマンド（誤検知の回帰防止）。
_ALLOWED_COMMANDS = [
    # -n が --dry-run / 件数指定であるサブコマンド
    "git push -n",
    "git log -n 5",
    "git push --dry-run",
    "git clean -n",
    "git bisect run ./t.sh -n",
    # 既知の boolean グローバルオプションは subcommand 解決を乱さない
    "git --no-pager log -n 5",
    "git -p log -n 5",
    "git -c foo.bar=1 log -n 5",
    # クォート内の -n / --no-verify はフラグではない
    'git commit -m "use -n flag"',
    "git commit -m'msg with -n'",
    f"git commit -m 'note: {NV} is banned'",
    f'echo "git commit {NV}"',
    'grep -r "git commit" .',
    # 短縮クラスタの既存挙動
    "git commit -mmsg",
    'git commit -am "x"',
    "git commit -uno -m x",
    "git commit --amend --no-edit",
    "git commit -m x -- -n",
    # git 起動トークンより前の -n は git のフラグではない
    "grep -rn git .",
    "ls -n /usr/bin/git",
    "xargs -n 2 git status",
    # その他
    "git status",
    "git rebase --continue",
    f"gitk {NV}",
    "",
    "git",
]


def _capture_io(monkeypatch: pytest.MonkeyPatch, module, payload: str) -> tuple[list[str], list[str]]:
    stdout: list[str] = []
    stderr: list[str] = []
    if hasattr(module, "read_raw_stdin_with_truncation"):
        # config_protection は自己完結した truncation guard を持つため
        # (raw, truncated) タプルを返す read_raw_stdin_with_truncation を使う。
        monkeypatch.setattr(module, "read_raw_stdin_with_truncation", lambda: (payload, False))
    else:
        monkeypatch.setattr(module, "read_raw_stdin", lambda: payload)
    if hasattr(module, "write_stdout"):
        monkeypatch.setattr(module, "write_stdout", stdout.append)
    if hasattr(module, "write_stderr"):
        monkeypatch.setattr(module, "write_stderr", stderr.append)
    return stdout, stderr


def _run_entrypoint(module_name: str) -> int:
    """モジュールを __main__ として実行し、終了コードを返す。"""
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module(module_name, run_name="__main__")

    return excinfo.value.code


class TestBlockNoVerify:
    @pytest.mark.parametrize(
        ("command", "expected_code", "blocked"),
        [
            ("git commit --no-verify", 2, True),
            # git push の -n は --dry-run（フックバイパスではない）ため通す。
            ("git push -n", 0, False),
            ("git push --no-verify", 2, True),
            ("git status", 0, False),
            ('git commit -m "docs: explain -n flag usage"', 0, False),
            ("git commit -m 'note: --no-verify is banned'", 0, False),
            ('git commit --no-verify -m "fix: x"', 2, True),
            ('echo "git commit --no-verify"', 0, False),
        ],
    )
    def test_main(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        command: str,
        expected_code: int,
        blocked: bool,
    ) -> None:
        payload = json.dumps({"tool_input": {"command": command}})
        monkeypatch.setattr(block_no_verify, "read_raw_stdin", lambda: payload)

        assert block_no_verify.main() == expected_code
        captured = capsys.readouterr()
        assert captured.out == ""
        assert bool(captured.err) is blocked

    @pytest.mark.parametrize("command", _BYPASS_COMMANDS)
    def test_has_bypass_flag_blocks(self, command: str) -> None:
        """ラッパー・パス付き起動・グローバルオプション経由のバイパスを検出する。"""
        assert block_no_verify.has_bypass_flag(command) is True

    @pytest.mark.parametrize("command", _ALLOWED_COMMANDS)
    def test_has_bypass_flag_allows(self, command: str) -> None:
        """バイパスでないコマンドを誤検知しない。"""
        assert block_no_verify.has_bypass_flag(command) is False

    def test_tokenize_falls_back_to_whitespace_split(self) -> None:
        """クォート不整合時は空白分割へフォールバックする（fail-closed）。"""
        assert block_no_verify.tokenize('git commit -m "x') == ["git", "commit", "-m", '"x']

    def test_split_segments_drops_empty_segments(self) -> None:
        """連続する区切りトークンで空セグメントを作らない。"""
        tokens = ["&&", "git", "status", ";", ";", "git", "commit"]
        assert block_no_verify.split_segments(tokens) == [["git", "status"], ["git", "commit"]]

    def test_parse_git_segment_returns_subcommand_and_flags(self) -> None:
        """グローバルオプションを読み飛ばしてサブコマンドとフラグを返す。"""
        segment = ["git", "-C", "/tmp", "--no-pager", "commit", "-am", "msg", "--no-verify"]

        invocation = block_no_verify.parse_git_segment(segment)

        assert invocation.subcommand == "commit"
        assert invocation.flags == ["-C", "--no-pager", "-a", "-m", "--no-verify"]
        assert invocation.subcommand_certain is True

    @pytest.mark.parametrize(
        ("segment", "certain"),
        [
            # 既知の boolean グローバルオプションはサブコマンド解決を乱さない。
            (["git", "--no-pager", "log"], True),
            (["git", "-p", "log"], True),
            # 未知の long オプションは値を取るか不明なので解決を信用しない。
            (["git", "--future-option", "value", "commit"], False),
            # 未知の short オプションも同様（クラスタ内の 1 文字でも未知なら不確定）。
            (["git", "-Z", "value", "commit"], False),
            # サブコマンド解決後の未知オプションは解決結果に影響しない。
            (["git", "commit", "--future-option", "value"], True),
            (["git", "push", "-Z"], True),
            # 値が = で結合された未知オプションは次トークンを食わないので確定。
            (["git", "--future-option=value", "commit"], True),
        ],
    )
    def test_parse_git_segment_subcommand_certainty(self, segment: list[str], certain: bool) -> None:
        """未知グローバルオプションの有無でサブコマンド確度を切り替える。"""
        assert block_no_verify.parse_git_segment(segment).subcommand_certain is certain

    @pytest.mark.parametrize(
        ("token", "expected"),
        [
            ("git", True),
            ("/usr/bin/git", True),
            ("./git", True),
            ("gitk", False),
            ("GIT_DIR=.git", False),
            (".git", False),
            ("git/", False),
            ("git commit --no-verify", False),
        ],
    )
    def test_is_git_invocation(self, token: str, expected: bool) -> None:
        """basename が git のトークンだけを git 起動として扱う。"""
        assert block_no_verify.is_git_invocation(token) is expected

    @pytest.mark.parametrize(
        ("command", "expected_code"),
        [(command, 2) for command in _BYPASS_COMMANDS] + [(command, 0) for command in _ALLOWED_COMMANDS],
    )
    def test_end_to_end_via_module_subprocess(self, command: str, expected_code: int) -> None:
        """実 JSON を stdin へ流す ``python -m`` 実行で判定を end-to-end 検証する。"""
        payload = json.dumps({"tool_input": {"command": command}})
        src_root = Path(block_no_verify.__file__).resolve().parents[2]
        completed = subprocess.run(
            [sys.executable, "-m", "bluecore.hooks.block_no_verify"],
            input=payload,
            capture_output=True,
            text=True,
            check=False,
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(src_root), "CLAUDECODE": "1"},
        )

        assert completed.returncode == expected_code
        assert completed.stdout == ""
        assert bool(completed.stderr) is (expected_code != 0)

    def test_blocked_on_copilot_emits_deny_json(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Copilot では deny JSON + exit 0 でブロックする。"""
        from bluecore.lib import harness

        monkeypatch.delenv("CLAUDECODE", raising=False)
        monkeypatch.setenv("COPILOT_AGENT_PROMPT", "x")
        harness.detect_harness.cache_clear()
        payload = json.dumps({"tool_input": {"command": "git commit --no-verify"}})
        monkeypatch.setattr(block_no_verify, "read_raw_stdin", lambda: payload)
        try:
            assert block_no_verify.main() == 0
        finally:
            harness.detect_harness.cache_clear()
        deny = json.loads(capsys.readouterr().out)
        assert deny["permissionDecision"] == "deny"


class TestSessionEndHelpers:
    def test_get_session_metadata_uses_git_branch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(session_end, "get_project_name", lambda: "repo")
        monkeypatch.setattr(session_end, "run_command", lambda cmd: {"success": True, "output": "feature/test"})

        assert session_end.get_session_metadata() == {
            "project": "repo",
            "branch": "feature/test",
        }

    def test_get_session_metadata_falls_back_when_branch_lookup_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(session_end, "get_project_name", lambda: None)
        monkeypatch.setattr(session_end, "run_command", lambda cmd: {"success": False, "output": ""})

        assert session_end.get_session_metadata()["branch"] == "unknown"

    def test_extract_session_summary_counts_user_messages(self, tmp_path: Path) -> None:
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            "\n".join(
                [
                    json.dumps({"type": "user", "content": "ご質問ありがとうございます。  修正お願いします。"}),
                    json.dumps({"type": "user", "content": "   second line"}),
                    json.dumps({"type": "user", "content": ""}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        summary = session_end.extract_session_summary(str(transcript))

        assert summary == {"filesModified": [], "totalMessages": 2}

    def test_extract_session_summary_returns_none_for_empty_content(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(session_end, "read_file", lambda path: "")

        assert session_end.extract_session_summary("missing.jsonl") is None

    def test_extract_session_summary_logs_parse_errors_when_no_user_messages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        logs: list[str] = []
        content = "\n".join(
            [
                "not-json",
                json.dumps(
                    {
                        "type": "tool_use",
                        "tool_name": "Edit",
                        "tool_input": {"file_path": "README.md"},
                    }
                ),
            ]
        )
        monkeypatch.setattr(session_end, "read_file", lambda path: content)
        monkeypatch.setattr(session_end, "log", logs.append)

        assert session_end.extract_session_summary("transcript.jsonl") is None
        assert any("Skipped 1/2 unparseable transcript lines" in message for message in logs)


class TestSessionEndMain:
    def test_run_skips_when_transcript_path_is_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """transcript_path が無ければ何もしない（checkpoint も書かない）。"""
        monkeypatch.setattr(
            session_end, "extract_session_summary", lambda path: pytest.fail("走査してはならない")
        )

        assert session_end.run("{}") is None

    def test_run_logs_when_transcript_is_missing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """transcript_path が実在しなければログを残して終わる。"""
        logs: list[str] = []
        monkeypatch.setattr(session_end, "log", logs.append)
        monkeypatch.setattr(
            session_end, "extract_session_summary", lambda path: pytest.fail("走査してはならない")
        )

        session_end.run(json.dumps({"transcript_path": str(tmp_path / "missing.jsonl")}))

        assert any("Transcript not found" in message for message in logs)

    def test_run_skips_checkpoint_below_threshold(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """閾値未満のセッションでは checkpoint を書かない。"""
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(json.dumps({"type": "user", "content": "one"}) + "\n", encoding="utf-8")
        monkeypatch.setattr(
            session_end, "_auto_save_checkpoint", lambda *a: pytest.fail("保存してはならない")
        )

        session_end.run(json.dumps({"transcript_path": str(transcript)}))

    def test_main_logs_on_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        logs: list[str] = []

        monkeypatch.setattr(session_end, "read_raw_stdin", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        monkeypatch.setattr(session_end, "log", logs.append)

        assert session_end.main() == 0
        assert any("Error: boom" in message for message in logs)

    def test_run_logs_on_outer_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        logs: list[str] = []
        monkeypatch.setattr(session_end, "parse_json_object", lambda raw: (_ for _ in ()).throw(RuntimeError("boom")))
        monkeypatch.setattr(session_end, "log", logs.append)

        assert session_end.run("{}") is None
        assert any("Error: boom" in message for message in logs)

    def test_main_entrypoint_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("bluecore.hooks.hook_common.read_raw_stdin", lambda: "{}")

        assert _run_entrypoint("bluecore.hooks.session_end") == 0


class TestPreCompact:
    def test_main_logs_compaction(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        appended: list[tuple[Path, str]] = []
        logs: list[str] = []

        monkeypatch.setattr(pre_compact, "get_sessions_dir", lambda: tmp_path)
        monkeypatch.setattr(pre_compact, "ensure_dir", lambda path: Path(path))
        monkeypatch.setattr(pre_compact, "append_file", lambda path, content: appended.append((Path(path), content)))
        monkeypatch.setattr(pre_compact, "log", logs.append)
        monkeypatch.setattr(pre_compact, "get_datetime_string", lambda: "2026-01-01 00:00:00")

        assert pre_compact.main() == 0
        assert appended == [(tmp_path / "compaction-log.txt", "[2026-01-01 00:00:00] Context compaction triggered\n")]
        assert logs == ["[PreCompact] State saved before compaction"]

    def test_main_logs_on_exception(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        logs: list[str] = []
        monkeypatch.setattr(pre_compact, "get_sessions_dir", lambda: tmp_path)
        monkeypatch.setattr(pre_compact, "ensure_dir", lambda path: Path(path))
        monkeypatch.setattr(pre_compact, "append_file", lambda path, content: (_ for _ in ()).throw(RuntimeError("boom")))
        monkeypatch.setattr(pre_compact, "log", logs.append)

        assert pre_compact.main() == 0
        assert any("Error: boom" in message for message in logs)

    def test_main_entrypoint_exits_zero(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr("bluecore.lib.core_utils.get_sessions_dir", lambda: tmp_path)
        monkeypatch.setattr("bluecore.lib.core_utils.ensure_dir", lambda path: Path(path))
        monkeypatch.setattr("bluecore.lib.core_utils.append_file", lambda path, content: None)
        monkeypatch.setattr("bluecore.lib.core_utils.get_datetime_string", lambda: "2026-01-01 00:00:00")
        monkeypatch.setattr("bluecore.lib.core_utils.log", lambda message: None)

        assert _run_entrypoint("bluecore.hooks.pre_compact") == 0


class TestSimpleHookEntrypoints:
    def test_block_no_verify_entrypoint_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "bluecore.hooks.hook_common.read_raw_stdin",
            lambda: json.dumps({"tool_input": {"command": "git status"}}),
        )

        assert _run_entrypoint("bluecore.hooks.block_no_verify") == 0


class TestSessionStartRubyLog:
    """session_start フックが Ruby プロジェクトでログを出すことを確認するテスト。"""

class TestCheckpointInjection:
    """session_start がアクティブなチェックポイントを注入するテスト。"""

    def _make_session_start_base_patches(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """session_start.run() の共通モックを設定する。"""
        from bluecore.hooks import session_start
        from bluecore.lib.package_manager import PackageManagerResult

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(session_start, "log", lambda _: None)
        monkeypatch.setattr(session_start, "get_package_manager", lambda: PackageManagerResult(name=None, config=None, source="none"))
        monkeypatch.setattr(session_start, "ensure_dir", lambda _: None)
        monkeypatch.setattr(session_start, "extract_coverage_hint_lines", lambda _: None)

    def test_active_checkpoint_is_injected(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """completed: false のチェックポイントが additionalContext に注入されること。"""
        from bluecore.hooks import session_start

        sessions_dir = tmp_path / "session-data"
        sessions_dir.mkdir()
        checkpoint = sessions_dir / "checkpoint-2026-05-09-test.md"
        checkpoint.write_text(
            "---\ntask: test\ncompleted: false\n---\n\n## 目標\nテスト中\n",
            encoding="utf-8",
        )

        self._make_session_start_base_patches(monkeypatch, tmp_path)
        monkeypatch.setattr(session_start, "get_sessions_dir", lambda: sessions_dir)
        monkeypatch.setattr(session_start, "get_learned_skills_dir", lambda: tmp_path / "learned")
        monkeypatch.setattr(session_start, "find_files", lambda path, pattern, **kw: (
            [{"path": str(checkpoint), "mtime": 9999}] if "checkpoint-" in pattern else []
        ))
        monkeypatch.setattr(session_start, "read_file", lambda p: checkpoint.read_text(encoding="utf-8") if str(p) == str(checkpoint) else "")

        output = session_start.run("{}")
        payload = json.loads(output)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "Active checkpoint:" in context

    def test_active_checkpoint_is_capped_at_500_chars(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """チェックポイント注入は 500 文字上限に切り詰めること（注入トークン削減）。"""
        from bluecore.hooks import session_start

        sessions_dir = tmp_path / "session-data"
        sessions_dir.mkdir()
        checkpoint = sessions_dir / "checkpoint-2026-06-11-cap.md"
        checkpoint.write_text(
            "---\ntask: test\ncompleted: false\n---\n\n## 目標\n" + "ながいほんぶん " * 200 + "\n",
            encoding="utf-8",
        )

        self._make_session_start_base_patches(monkeypatch, tmp_path)
        monkeypatch.setattr(session_start, "get_sessions_dir", lambda: sessions_dir)
        monkeypatch.setattr(session_start, "get_learned_skills_dir", lambda: tmp_path / "learned")
        monkeypatch.setattr(session_start, "find_files", lambda path, pattern, **kw: (
            [{"path": str(checkpoint), "mtime": 9999}] if "checkpoint-" in pattern else []
        ))
        monkeypatch.setattr(session_start, "read_file", lambda p: checkpoint.read_text(encoding="utf-8") if str(p) == str(checkpoint) else "")

        output = session_start.run("{}")
        payload = json.loads(output)
        context = payload["hookSpecificOutput"]["additionalContext"]
        marker = "Active checkpoint:\n"
        assert marker in context
        injected = context.split(marker, 1)[1].split("\n\n", 1)[0]
        assert len(injected) <= 510  # 500 + "..." 分のマージン

    def test_completed_checkpoint_is_not_injected(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """completed: true のチェックポイントは注入されないこと。"""
        from bluecore.hooks import session_start

        sessions_dir = tmp_path / "session-data"
        sessions_dir.mkdir()
        checkpoint = sessions_dir / "checkpoint-2026-05-09-done.md"
        checkpoint.write_text(
            "---\ntask: done\ncompleted: true\n---\n\n## 目標\n完了済み\n",
            encoding="utf-8",
        )

        self._make_session_start_base_patches(monkeypatch, tmp_path)
        monkeypatch.setattr(session_start, "get_sessions_dir", lambda: sessions_dir)
        monkeypatch.setattr(session_start, "get_learned_skills_dir", lambda: tmp_path / "learned")
        monkeypatch.setattr(session_start, "find_files", lambda path, pattern, **kw: (
            [{"path": str(checkpoint), "mtime": 9999}] if "checkpoint-" in pattern else []
        ))
        monkeypatch.setattr(session_start, "read_file", lambda p: checkpoint.read_text(encoding="utf-8") if str(p) == str(checkpoint) else "")

        output = session_start.run("{}")
        payload = json.loads(output)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "Active checkpoint:" not in context


class TestAutoCheckpointSave:
    """session_end がメッセージ閾値超過時にチェックポイントを自動保存するテスト。"""

    def _make_summary(self, total_messages: int = 35) -> dict:
        """テスト用サマリーを生成する。"""
        return {
            "filesModified": ["path/to/file.py"],
            "totalMessages": total_messages,
        }

    def test_auto_checkpoint_created_when_threshold_exceeded(self, tmp_path: Path) -> None:
        """メッセージ数が閾値以上のとき新規チェックポイントが作成されること。"""
        sessions_dir = tmp_path / "session-data"
        sessions_dir.mkdir()
        metadata = {"project": "bluecore", "branch": "develop"}
        summary = self._make_summary(35)

        session_end._auto_save_checkpoint(summary, metadata, sessions_dir)

        checkpoints = list(sessions_dir.glob("checkpoint-*.md"))
        assert len(checkpoints) == 1
        content = checkpoints[0].read_text(encoding="utf-8")
        assert "completed: false" in content
        assert "path/to/file.py" in content

    def test_auto_checkpoint_updates_existing(self, tmp_path: Path) -> None:
        """既存のアクティブなチェックポイントがある場合は更新されること。"""
        sessions_dir = tmp_path / "session-data"
        sessions_dir.mkdir()
        today = session_end.get_date_string()
        existing = sessions_dir / f"checkpoint-{today}-bluecore.md"
        existing.write_text(
            "---\ntask: bluecore\ncompleted: false\n---\n\n## 変更済みファイル\n- old.py\n\n## 再開コンテキスト\nold\n",
            encoding="utf-8",
        )
        metadata = {"project": "bluecore", "branch": "main"}
        summary = self._make_summary(40)
        summary["filesModified"] = ["new.py"]

        session_end._auto_save_checkpoint(summary, metadata, sessions_dir)

        content = existing.read_text(encoding="utf-8")
        assert "new.py" in content
        assert "old.py" not in content

    def test_auto_checkpoint_skips_completed(self, tmp_path: Path) -> None:
        """completed: true の既存チェックポイントは更新しないこと。"""
        sessions_dir = tmp_path / "session-data"
        sessions_dir.mkdir()
        today = session_end.get_date_string()
        existing = sessions_dir / f"checkpoint-{today}-bluecore.md"
        existing.write_text(
            "---\ntask: bluecore\ncompleted: true\n---\n\n## 変更済みファイル\n- old.py\n",
            encoding="utf-8",
        )
        metadata = {"project": "bluecore", "branch": "main"}
        summary = self._make_summary(40)

        session_end._auto_save_checkpoint(summary, metadata, sessions_dir)

        content = existing.read_text(encoding="utf-8")
        assert "completed: true" in content

    def test_run_triggers_checkpoint_when_threshold_met(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """run() がメッセージ数閾値以上のとき _auto_save_checkpoint を呼ぶこと。"""
        sessions_dir = tmp_path / "session-data"
        sessions_dir.mkdir()
        transcript = tmp_path / "transcript.jsonl"
        lines = [json.dumps({"type": "user", "content": f"msg{i}"}) for i in range(35)]
        transcript.write_text("\n".join(lines), encoding="utf-8")

        called: list[bool] = []

        def fake_auto_save(summary: dict, metadata: dict, sd: Path) -> None:
            called.append(True)

        monkeypatch.setattr(session_end, "_auto_save_checkpoint", fake_auto_save)
        monkeypatch.setattr(session_end, "get_sessions_dir", lambda: sessions_dir)

        raw = json.dumps({"transcript_path": str(transcript)})
        session_end.run(raw)

        assert called, "Expected _auto_save_checkpoint to be called"


@pytest.mark.parametrize(
    "module_name",
    [
        "block_no_verify",
        "config_protection",
        "pre_agent_nudge",
    ],
)
def test_empty_input_passthrough(monkeypatch: pytest.MonkeyPatch, module_name: str) -> None:
    """空入力（data None）では本処理をスキップして 0 を返す。"""
    import importlib

    mod = importlib.import_module(f"bluecore.hooks.{module_name}")
    _capture_io(monkeypatch, mod, "")
    assert mod.main() == 0


def test_config_protection_blank_file_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """file_path が空なら保護判定をスキップする。"""
    from bluecore.hooks import config_protection

    payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": ""}})
    _capture_io(monkeypatch, config_protection, payload)
    assert config_protection.main() == 0


def test_collect_user_message_non_text_content() -> None:
    """content が文字列でもリストでもなければ空文字を返す。"""
    assert session_end._collect_user_message({"type": "user", "content": 123}) == ""


def test_collect_user_message_ignores_non_dict_message() -> None:
    """message が dict でないエントリはユーザー発話として扱わない。"""
    assert session_end._collect_user_message({"message": "not-a-dict"}) == ""


def test_collect_modified_files_empty_name_and_non_tool_block() -> None:
    """ツール名が空・非 tool_use ブロックでも例外なく走査する。"""
    files: set[str] = set()
    session_end._collect_modified_files({"type": "tool_use"}, files)  # tool_name 空
    session_end._collect_modified_files(
        {"type": "assistant", "message": {"content": [{"type": "text"}, {"type": "tool_use", "name": "", "input": {}}]}},
        files,
    )
    assert files == set()


def test_collect_modified_files_ignores_non_edit_tools() -> None:
    """編集系でないツールはファイルを収集しない。"""
    files: set[str] = set()
    session_end._collect_modified_files(
        {"type": "tool_use", "tool_name": "Read", "tool_input": {"file_path": "a.py"}}, files
    )
    assert files == set()


def test_collect_modified_files_normalizes_apply_patch() -> None:
    """apply_patch は Edit へ正規化し、パッチ内の全対象ファイルを収集する。"""
    files: set[str] = set()
    patch = "*** Begin Patch\n*** Add File: a.py\n+x\n*** Update File: b.py\n*** End Patch"
    session_end._collect_modified_files(
        {"type": "tool_use", "tool_name": "apply_patch", "tool_input": {"input": patch}},
        files,
    )
    assert files == {"a.py", "b.py"}


def test_collect_modified_files_apply_patch_in_assistant_block_without_paths() -> None:
    """assistant ブロック経由の apply_patch でパス不明なら収集しない。"""
    files: set[str] = set()
    session_end._collect_modified_files(
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "apply_patch", "input": {"input": "no markers"}}]},
        },
        files,
    )
    assert files == set()


def test_record_modified_files_non_dict_input() -> None:
    """tool_input が dict 以外でも例外なく空入力として扱う。"""
    files: set[str] = set()
    session_end._record_modified_files("Write", "not-a-dict", files)
    assert files == set()
