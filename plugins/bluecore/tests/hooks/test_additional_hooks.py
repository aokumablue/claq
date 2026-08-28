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
    # core.hooksPath オーバーライド（--no-verify を使わずに git 自身の
    # フックを無効化できるため、単独でもブロックする。A-07）
    "git -c core.hooksPath=/dev/null commit",
    "git -ccore.hooksPath=/dev/null commit",
    "git -c CORE.HOOKSPATH=/dev/null commit",
    "git --config-env=core.hooksPath=MYVAR commit",
    "git --config-env core.hooksPath=MYVAR commit",
    # sh -c ラッパー 1 段（A-07）
    f"sh -c 'git commit {NV}'",
    f"bash -c 'git commit {NV}'",
    # -c より前に他オプションが挟まっても -c を探し続ける
    f"sh -x -c 'git commit {NV}'",
    # 未知のグローバルオプションで subcommand 解決がずれる形は fail-closed
    f"git --future-option value commit {NV}",
    "git --future-option value commit -n",
    "git -Z value commit -n",
    # shlex 失敗（未閉じクォート）は fail-closed
    f'git commit -m "unclosed {NV}',
    # 実行ファイル表記ゆれ（H-04）: macOS 既定 APFS は大小文字を区別しないため
    # `GIT` は実 git を起動する。Windows の `.exe` サフィックスも同一視する。
    f"GIT commit {NV}",
    f"Git commit {NV}",
    f"git.exe commit {NV}",
    f"GIT.EXE commit {NV}",
    "GIT commit -n",
    # literal GIT_CONFIG_* 環境変数プレフィックス（H-05）: --no-verify を
    # 使わずに core.hooksPath を上書きし git 自身のフックを無効化する。
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/tmp/x git commit -m x",
    "GIT_CONFIG_KEY_0=core.hooksPath git commit -m x",
    "GIT_CONFIG_KEY_0=CORE.HOOKSPATH git commit -m x",
    "env GIT_CONFIG_KEY_0=core.hooksPath git commit -m x",
    "env -i GIT_CONFIG_KEY_0=core.hooksPath git commit -m x",
    "env -u FOO GIT_CONFIG_KEY_0=core.hooksPath git commit -m x",
    "env -uFOO GIT_CONFIG_KEY_0=core.hooksPath git commit -m x",
    "env --ignore-environment GIT_CONFIG_KEY_0=core.hooksPath git commit -m x",
    "GIT_CONFIG_PARAMETERS=x git commit -m x",
    # `git config` サブコマンド経由の core.hooksPath 書込み・削除（H-06）。
    "git config core.hooksPath /tmp/evil-hooks",
    "git config CORE.HOOKSPATH /tmp/evil-hooks",
    "git config --unset core.hooksPath",
    "git config --unset-all core.hooksPath",
    "git config set core.hooksPath /tmp/evil-hooks",
    "git config unset core.hooksPath",
    "git config add core.hooksPath /tmp/evil-hooks",
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
    # core.hooksPath 以外の -c/--config-env は対象外
    "git -c user.name=x commit -m y",
    # sh -c の 2 段以上のネストは非目標（1 段のみ再帰）
    f"sh -c \"sh -c 'git commit {NV}'\"",
    # -c を伴わないシェル起動はラッパー対象外
    "sh script.sh",
    # その他
    "git status",
    "git rebase --continue",
    f"gitk {NV}",
    "",
    "git",
    # literal 環境変数プレフィックスがあっても core.hooksPath と無関係なら allow。
    "FOO=bar git commit -m x",
    "env FOO git commit -m x",
    "env GIT_CONFIG_KEY_0=user.name git commit -m x",
    # `git config` の read-only 操作・無関係キーは allow（H-06）。
    "git config --get core.hooksPath",
    "git config --get-all core.hooksPath",
    "git config --get-regexp core.hooksPath",
    "git config --show-origin --get core.hooksPath",
    "git config --list",
    "git config core.hooksPath",
    "git config get core.hooksPath",
    "git config list",
    "git config get user.name",
    "git config user.name x",
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
        monkeypatch.setattr(block_no_verify, "read_raw_stdin_with_truncation", lambda: (payload, False))

        assert block_no_verify.main() == expected_code
        captured = capsys.readouterr()
        # ブロック時のみ stdout に permissionDecision: deny の合併 JSON を出す。
        assert bool(captured.out) is blocked
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
            # 実行ファイル表記ゆれ（H-04）: 大小文字・.exe・Windows パス区切り。
            ("GIT", True),
            ("Git", True),
            ("git.exe", True),
            ("GIT.EXE", True),
            (r"C:\Program Files\Git\bin\git.exe", True),
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
        assert bool(completed.stdout) is (expected_code != 0)
        assert bool(completed.stderr) is (expected_code != 0)

    def test_oversized_payload_is_fail_closed_end_to_end(self) -> None:
        """1 MiB を超える実 stdin ペイロードは launcher 経由でも deny する（監査の再現手順そのもの）。"""
        payload = json.dumps(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "git commit --no-verify"},
                "filler": "x" * (1024 * 1024 + 100),
            }
        )
        src_root = Path(block_no_verify.__file__).resolve().parents[2]
        completed = subprocess.run(
            [sys.executable, "-m", "bluecore.hooks.block_no_verify"],
            input=payload,
            capture_output=True,
            text=True,
            check=False,
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(src_root), "CLAUDECODE": "1"},
        )

        assert completed.returncode == 2
        assert "BLOCKED" in completed.stderr

    def test_blocked_emits_deny_json_alongside_exit_2(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """ブロック時は host に関わらず stdout の deny JSON + exit 2 を同時に出す。"""
        payload = json.dumps({"tool_input": {"command": "git commit --no-verify"}})
        monkeypatch.setattr(block_no_verify, "read_raw_stdin_with_truncation", lambda: (payload, False))
        assert block_no_verify.main() == 2
        deny = json.loads(capsys.readouterr().out)
        assert deny["permissionDecision"] == "deny"

    def test_truncated_input_is_fail_closed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """1 MiB 超で切り捨てられた入力は判定不能として deny する（F-01 回帰）。

        切り捨て後の JSON は不完全になりうるため、切り捨てフラグそのものを
        deny の根拠にする（切り捨て後の中身がたまたま parse できても信用しない）。
        """
        monkeypatch.setattr(
            block_no_verify, "read_raw_stdin_with_truncation", lambda: ("{not-even-json", True)
        )

        assert block_no_verify.main() == 2
        err = capsys.readouterr().err
        assert "BLOCKED" in err
        assert "exceeded" in err

    def test_unparseable_json_is_fail_closed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """非空だが JSON として読めない入力は判定不能として deny する（F-01 回帰）。"""
        monkeypatch.setattr(
            block_no_verify, "read_raw_stdin_with_truncation", lambda: ("{not-json", False)
        )

        assert block_no_verify.main() == 2
        assert "could not parse hook input" in capsys.readouterr().err

    def test_missing_tool_input_is_fail_closed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """JSON は妥当だが tool_input 系キーが無い場合も deny する（F-01 回帰）。"""
        monkeypatch.setattr(
            block_no_verify,
            "read_raw_stdin_with_truncation",
            lambda: (json.dumps({"unrelated": "payload"}), False),
        )

        assert block_no_verify.main() == 2
        assert "no recognizable tool_input" in capsys.readouterr().err


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
            "bluecore.hooks.hook_common.read_raw_stdin_with_truncation",
            lambda: (json.dumps({"tool_input": {"command": "git status"}}), False),
        )

        assert _run_entrypoint("bluecore.hooks.block_no_verify") == 0


@pytest.mark.parametrize(
    "module_name",
    [
        "block_no_verify",
        "config_protection",
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


class TestBlockNoVerifyScansAllContainerKeys:
    """コンテナキーを全て走査する（先勝ちで無害な側だけ見て素通りしない）。"""

    def _run(self, monkeypatch: pytest.MonkeyPatch, payload: dict) -> int:
        raw = json.dumps(payload)
        monkeypatch.setattr(block_no_verify, "read_raw_stdin_with_truncation", lambda: (raw, False))
        return block_no_verify.main()

    def test_bypass_in_later_container_key_is_blocked(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """先頭キーが無害でも後続キーのバイパスを検出する。

        先勝ちで 1 キーだけ見る実装では、無害な tool_input と悪意ある toolInput が
        同居する payload を素通りさせていた。ADR-0002 は本フックの検出境界を
        「誤検出を誤通過より選ぶ」と定めており、config_protection は既に全キー走査。
        """
        code = self._run(
            monkeypatch,
            {"tool_input": {"command": "ls"}, "toolInput": {"command": "git commit --no-verify"}},
        )

        assert code == 2
        assert "bypass" in capsys.readouterr().err

    def test_all_benign_container_keys_pass(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """全キーが無害なら通す。"""
        code = self._run(
            monkeypatch, {"tool_input": {"command": "ls"}, "toolInput": {"command": "git status"}}
        )

        assert code == 0
        assert capsys.readouterr().err == ""
