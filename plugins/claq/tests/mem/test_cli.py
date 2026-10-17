"""claq.mem.cli のテスト（main・引数解析・init・SessionStart 契約・subcommand のオプション検証）"""

from __future__ import annotations

import io
import json
import runpy
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from claq.mem import cli, cmd_context, command_support
from claq.mem.database import Database
from claq.mem.models import Repo

from ._support import _always_raise, _run_cli, _seed


class TestSessionStartContract:
    """SessionStart フック経路（context）の出力契約。"""

    def test_creates_database_and_emits_session_start_json(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """DB が無くても作成し、SessionStart 契約の JSON を stdout に出す。"""
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["context"])
        assert exit_code == 0
        assert stderr == ""
        payload = json.loads(stdout)
        assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert (tmp_path / "mem.db").exists()

    def test_swallows_db_failure(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """コンテキスト組み立てが失敗してもフックを壊さず JSON を返す。"""
        stub = _always_raise("db broken")
        monkeypatch.setattr(cmd_context, "_build_context", stub)
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["context"])
        assert len(stub.calls) == 1
        assert exit_code == 0
        assert stderr == ""
        assert json.loads(stdout)["hookSpecificOutput"]["hookEventName"] == "SessionStart"

    def test_settings_failure_still_emits_session_start_json(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """設定ロード失敗でも exit_code=0 と JSON 出力を維持しつつ、原因を stderr へ出す。

        SessionStart はセッション開始を止めないので exit 0 のままにするが、
        黙って抜けると記憶注入が失われたこと自体に気づけない。
        """
        monkeypatch.setattr(cli, "_load_settings_or_raise", _always_raise("設定失敗"))
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["context"])
        assert exit_code == 0
        assert "設定失敗" in stderr
        assert json.loads(stdout)["hookSpecificOutput"]["hookEventName"] == "SessionStart"

    def test_handler_exception_keeps_exit_code_0_but_emits_json(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """ハンドラ例外時も JSON を出しつつ exit_code=0 を維持する。

        この経路で exit_code=1 を返すと、`main()` の docstring が
        宣言する「SessionStart コマンドは失敗しても 0 を維持する」契約を
        破る。非 0 を返すとセッション全体がエラー扱いになるうえ、
        `finally` が正しい SessionStart JSON を出し切っており原因も stderr へ
        出ているため、非 0 にして得られるものが無い。
        """
        monkeypatch.setitem(cli._COMMAND_HANDLERS, "context", _always_raise("boom"))
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["context"])
        assert exit_code == 0
        assert "boom" in stderr
        assert json.loads(stdout)["hookSpecificOutput"]["hookEventName"] == "SessionStart"

    def test_command_error_in_handler_keeps_exit_code_0(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """ハンドラが CommandError を投げても exit_code=0 と JSON 出力を維持する。"""

        def _raise_command_error(*_args: object, **_kwargs: object) -> None:
            raise command_support.CommandError("bad handler input")

        monkeypatch.setitem(cli._COMMAND_HANDLERS, "context", _raise_command_error)
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["context"])
        assert exit_code == 0
        assert "bad handler input" in stderr
        assert json.loads(stdout)["hookSpecificOutput"]["hookEventName"] == "SessionStart"

    def test_argument_error_keeps_exit_code_0(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """引数解析段の CommandError でも exit_code=0 と JSON 出力を維持する。

        `hooks.json` の argv は固定なので実運用では到達しないが、契約は
        「SessionStart はどの失敗経路でも 0」であり経路ごとの例外を作らない。
        """
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["context", "--unknown-option"])
        assert exit_code == 0
        assert stderr != ""
        assert json.loads(stdout)["hookSpecificOutput"]["hookEventName"] == "SessionStart"

    def test_normal_command_still_fails_with_exit_code_1(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """SessionStart 以外は失敗を exit_code=1 で報告する。

        `failure_exit_code` の集約が「全部 0 にする」変更になっていないことの
        退行防止。
        """
        _stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["show", "no-such-key"])
        assert exit_code == 1
        assert stderr != ""


class TestInit:
    """init コマンド（DB 再作成）。"""

    def test_recreates_database_dropping_old_rows(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """既存 DB と sidecar を破棄して空の DB を作り直す。"""
        db_path = tmp_path / "mem.db"
        with Database(db_path) as db:
            db.upsert_repo(Repo(id="old", identity_key="key-old", root_path="/tmp/old"))
        for suffix in ("-wal", "-shm", "-journal"):
            (tmp_path / f"mem.db{suffix}").write_text("stale", encoding="utf-8")

        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["init"])
        assert exit_code == 0
        assert (stdout, stderr) == ("", "")
        for suffix in ("-wal", "-shm", "-journal"):
            assert not (tmp_path / f"mem.db{suffix}").exists()

        with Database(db_path) as db:
            assert db.list_repos() == []

    def test_remove_db_artifacts_tolerates_missing_files(self, tmp_path: Path) -> None:
        """存在しないファイルがあっても例外を出さない。"""
        db_path = tmp_path / "mem.db"
        db_path.write_text("db", encoding="utf-8")
        (tmp_path / "mem.db-journal").write_text("stale", encoding="utf-8")

        cli._remove_db_artifacts(db_path)

        assert not db_path.exists()
        assert not (tmp_path / "mem.db-journal").exists()

    def test_unexpected_positional_arg_is_usage_error_and_db_untouched(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """回帰防止: 未定義の位置引数は usage error にし、DB 副作用を起こさない。

        typo を無視すると `init unexpected_extra_arg` が exit 0 で DB を
        再作成し、呼出元が失敗を検知できない。
        """
        db_path = tmp_path / "mem.db"
        assert not db_path.exists()

        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["init", "unexpected_extra_arg"])

        assert exit_code == 1
        assert stdout == ""
        assert "init は位置引数を取りません" in stderr
        assert not db_path.exists()


class TestPositionalArity:
    """dispatch 層の位置引数個数検証。"""

    @pytest.mark.parametrize("command", ["init", "learn", "list", "handoff"])
    def test_zero_positional_commands_reject_extra_arg(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str
    ) -> None:
        """0 個の位置引数を取る subcommand は余分な引数を usage error にする。"""
        _stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, [command, "unexpected"])

        assert exit_code == 1
        assert f"{command} は位置引数を取りません" in stderr

    def test_context_reports_extra_arg_without_failing_the_hook(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`context` も usage error を stderr へ出すが exit_code は 0 に留める。

        `context` は唯一の SessionStart コマンドで、非 0 を返すとセッション全体が
        エラー扱いになる。検出（stderr）と終了コードは別物として扱う。
        """
        _stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["context", "unexpected"])

        assert exit_code == 0
        assert "context は位置引数を取りません" in stderr

    @pytest.mark.parametrize("command", ["show", "promote", "forget"])
    def test_single_key_commands_reject_missing_key(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str
    ) -> None:
        """key 未指定は usage error にする。"""
        _stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, [command])

        assert exit_code == 1
        assert "key を指定してください" in stderr

    @pytest.mark.parametrize("command", ["show", "promote", "forget"])
    def test_single_key_commands_reject_extra_positional(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str
    ) -> None:
        """key に続く余分な位置引数は usage error にする。"""
        _stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, [command, "key", "extra"])

        assert exit_code == 1
        assert f"{command} は key を1つだけ指定してください" in stderr

    def test_search_still_accepts_multiple_positionals(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """search は複数 positional を検索語として連結する既存契約を維持する（回帰防止）。"""
        _seed(tmp_path, key="multi-word-hit", title="two words here")

        stdout, _stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["search", "two", "words"])

        assert exit_code == 0
        assert "multi-word-hit" in stdout


class TestArgvAndStdin:
    """引数と stdin の解釈。"""

    def test_no_command_prints_help(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """引数なしは HELP_TEXT を出力する。"""
        monkeypatch.setattr(sys, "argv", ["python"])
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            assert cli.main() == 0
        assert "CLI Commands for mem" in stdout.getvalue()

    def test_help_flag_prints_help(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--help も HELP_TEXT を出力する。"""
        monkeypatch.setattr(sys, "argv", ["python", "--help"])
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            assert cli.main() == 0
        assert "CLI Commands for mem" in stdout.getvalue()

    def test_unknown_command_returns_2(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """未知のコマンドは exit_code=2。"""
        _stdout, _stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["nonexistent"])
        assert exit_code == 2

    @pytest.mark.parametrize("raw", ["", "   ", "[1, 2]", "{ broken"])
    def test_unusable_stdin_yields_empty_dict(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        """read_raw_stdin() の戻りが空・非 dict・不正 JSON ならすべて空 dict に落とす。

        空文字列は tty・stdin 未接続・deadline 到達のいずれでも
        stdin_io.read_raw_stdin() が返す値。個々の原因ごとの分岐は
        stdin_io 側（test_stdin_io.py）が検証済みのため、ここでは
        _read_stdin_json がその戻り値をどう扱うかだけを見る。
        """
        monkeypatch.setattr(cli, "read_raw_stdin", lambda: raw)
        assert cli._read_stdin_json() == {}

    def test_valid_stdin_is_parsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """dict の JSON はそのまま返る。"""
        monkeypatch.setattr(cli, "read_raw_stdin", lambda: '{"cwd": "/tmp"}')
        assert cli._read_stdin_json() == {"cwd": "/tmp"}

    def test_read_stdin_json_delegates_to_bounded_reader(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_read_stdin_json は sys.stdin を直接読まず read_raw_stdin() に委譲する（回帰防止）。

        sys.stdin.read() は EOF まで無期限ブロックしうる
        （context は launcher の in-process 実行で host の実 pipe を直接
        読むため実害がある）。read_raw_stdin() は最初のバイト到着
        （2 秒）と読み取り全体（5 秒）の両方に上限を持つ bounded reader
        で、閉じない stdin でも有限時間で（空文字列を含む）値を返す。
        """
        calls: list[int] = []

        def _bounded_reader() -> str:
            calls.append(1)
            return '{"probe": true}'

        monkeypatch.setattr(cli, "read_raw_stdin", _bounded_reader)
        assert cli._read_stdin_json() == {"probe": True}
        assert calls == [1]

    def test_positionals_flags_and_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """位置引数・真偽フラグ・値付きオプションを分離する。"""
        monkeypatch.setattr(cli, "read_raw_stdin", lambda: "")
        args = cli._parse_args_and_stdin("search", ["pipefail", "--global", "--limit", "5"])
        assert args.positionals == ("pipefail",)
        assert args.flags == frozenset({"--global"})
        assert args.values == {"--limit": "5"}

    @pytest.mark.parametrize("command", ["list", "search", "show", "promote", "forget", "init"])
    def test_stdin_is_not_read_for_commands_that_do_not_use_it(
        self, command: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """stdin を使わない subcommand では stdin を読まない。

        全 subcommand で無条件に読むと、書き手が pipe を閉じない呼ばれ方で
        `list` / `search` / `show` まで STDIN_FIRST_BYTE_TIMEOUT ぶん待たされ
        （実測 2.10 秒）、さらに「stdin リダイレクト漏れの可能性」という
        渡すべき stdin が無い操作には誤った警告が出る。
        """
        calls: list[int] = []
        monkeypatch.setattr(cli, "read_raw_stdin", lambda: calls.append(1) or "")

        args = cli._parse_args_and_stdin(command, [])

        assert calls == []
        assert args.stdin_data == {}

    @pytest.mark.parametrize("command", ["learn", "context", "handoff"])
    def test_stdin_is_read_for_commands_that_use_it(
        self, command: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """stdin を使う subcommand では読む。"""
        monkeypatch.setattr(cli, "read_raw_stdin", lambda: '{"probe": true}')

        assert cli._parse_args_and_stdin(command, []).stdin_data == {"probe": True}

    def test_value_option_without_value_is_error(self) -> None:
        """値付きオプションに値が無ければ CommandError。"""
        with pytest.raises(command_support.CommandError, match="--limit"):
            cli._parse_options(["--limit"])

    def test_unknown_option_is_error(self) -> None:
        """未知のオプションは CommandError。"""
        with pytest.raises(command_support.CommandError, match="不明なオプション"):
            cli._parse_options(["--bogus"])

    def test_option_parse_error_returns_1(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """オプション解析エラーは stderr 1 行と exit_code=1。"""
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["list", "--bogus"])
        assert (stdout, exit_code) == ("", 1)
        assert stderr.splitlines() == ["不明なオプション: --bogus"]


class TestNormalCommandFailures:
    """SessionStart 以外のコマンドのエラー経路。"""

    def test_settings_failure_returns_1(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """設定ロード失敗は exit_code=1 と stderr 出力。"""
        monkeypatch.setattr(cli, "_load_settings_or_raise", _always_raise("設定失敗"))
        _stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["init"])
        assert exit_code == 1
        assert "設定/ログ初期化失敗" in stderr

    def test_handler_exception_returns_1(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """ハンドラ例外は exit_code=1 と stderr 出力。"""
        monkeypatch.setitem(cli._COMMAND_HANDLERS, "init", _always_raise("init failure"))
        _stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["init"])
        assert exit_code == 1
        assert "init failure" in stderr


def test_mem_main_module_invokes_cli_main(monkeypatch: pytest.MonkeyPatch) -> None:
    """python -m claq.mem が cli.main() を呼ぶ。"""
    monkeypatch.setattr(sys, "argv", ["python", "--help"])
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("claq.mem.__main__", run_name="__main__")

    assert excinfo.value.code == 0


def test_mem_main_module_does_not_run_on_plain_import() -> None:
    """import しただけでは CLI が走らないこと（``__name__`` ガード）。

    ガードが無いと ``pkgutil.walk_packages`` のようにパッケージを走査する
    道具がこのモジュールを import した瞬間に CLI が走り、``sys.exit`` の
    ``SystemExit`` が走査側へ飛ぶ（走査が途中で中断する）。
    ``python -m`` 経路は上のテストが押さえているので、ここは走らない側を固定する。
    """
    module = runpy.run_module("claq.mem.__main__", run_name="claq.mem.__main__")

    assert module["__name__"] == "claq.mem.__main__"


class TestSubcommandOptionContract:
    """subcommand が受理しないオプションを黙って無視しない。"""

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            (["promote", "some-key", "--global"], "--global"),
            (["show", "some-key", "--repo"], "--repo"),
            (["forget", "some-key", "--kind", "fact"], "--kind"),
            (["init", "--json"], "--json"),
        ],
    )
    def test_unsupported_option_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str], expected: str
    ) -> None:
        """未対応オプションは exit 1 で拒否し、対象の DB を触らないこと。

        黙って無視すると、`promote <key> --global` が repo 側のカードを
        昇格させ、利用者が指定した global 側は pending のまま残る。
        """
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, argv)

        assert (stdout, exit_code) == ("", 1)
        assert expected in stderr

    def test_mutually_exclusive_scope_flags_are_rejected_on_every_subcommand(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`--global --repo` の同時指定は list/search でも拒否されること。

        排他チェックが `_collect_list_rows` にしか無いと、それを呼ばない
        subcommand では素通りする。
        """
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["list", "--global", "--repo"])

        assert (stdout, exit_code) == ("", 1)
        assert "同時に指定できません" in stderr

    def test_supported_options_are_accepted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """対応済みオプションは通ること。"""
        _stdout, _stderr, exit_code = _run_cli(
            monkeypatch, tmp_path, ["list", "--global", "--status", "pending", "--limit", "3"]
        )

        assert exit_code == 0
