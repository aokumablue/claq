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

from ._support import _run_cli, _seed


def _raising(error: Exception):
    """呼ばれると error を送出し、呼び出しを ``calls`` に記録するスタブを返す（CommandError も送出できる）。"""
    calls: list[tuple[object, ...]] = []

    def _stub(*args: object, **_kwargs: object) -> None:
        """呼び出しを記録して error を送出する。"""
        calls.append(args)
        raise error

    _stub.calls = calls  # type: ignore[attr-defined]
    return _stub


def _session_start_event(stdout: str) -> str:
    """stdout の SessionStart 契約 JSON から hookEventName を取り出す。"""
    return json.loads(stdout)["hookSpecificOutput"]["hookEventName"]


class TestSessionStartContract:
    """SessionStart フック経路（context）の出力契約。"""

    def test_creates_database_and_emits_session_start_json(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """DB が無くても作成し、SessionStart 契約の JSON を stdout に出す。"""
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["context"])
        assert (stderr, exit_code) == ("", 0)
        assert _session_start_event(stdout) == "SessionStart"
        assert (tmp_path / "mem.db").exists()

    def test_swallows_db_failure(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """コンテキスト組み立てが失敗してもフックを壊さず JSON を返す。"""
        stub = _raising(RuntimeError("db broken"))
        monkeypatch.setattr(cmd_context, "_build_context", stub)
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["context"])
        assert len(stub.calls) == 1
        assert (stderr, exit_code) == ("", 0)
        assert _session_start_event(stdout) == "SessionStart"


class TestFailurePaths:
    """失敗経路の終了コード: SessionStart（context）は 0 を維持し、それ以外（init）は 1 で報告する。"""

    @pytest.mark.parametrize(
        ("command", "exit_code_on_failure"), [("context", 0), ("init", 1)], ids=["session-start", "normal"]
    )
    @pytest.mark.parametrize(
        ("extra", "target", "error", "expected"),
        [
            ([], "settings", RuntimeError("設定失敗"), "設定/ログ初期化失敗: 設定失敗"),
            ([], "handler", RuntimeError("boom"), "コマンド {command} 失敗: boom"),
            ([], "handler", command_support.CommandError("bad handler input"), "bad handler input"),
            (["--unknown-option"], None, None, "不明なオプション: --unknown-option"),
            (["unexpected"], None, None, "{command} は位置引数を取りません: 'unexpected'"),
        ],
        ids=["settings-failure", "handler-exception", "handler-command-error", "unknown-option", "extra-positional"],
    )
    def test_failure_reports_to_stderr_with_contract_exit_code(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        command: str,
        exit_code_on_failure: int,
        extra: list[str],
        target: str | None,
        error: Exception | None,
        expected: str,
    ) -> None:
        """どの失敗経路でも原因を stderr へ 1 行出し、DB を作らない。

        SessionStart は非 0 を返すとセッション全体がエラー扱いになるため、`finally` で契約の
        JSON を出し切って 0 を維持する（`hooks.json` の argv は固定なので引数エラーは実運用で
        到達しないが、経路ごとの例外は作らない）。黙って抜けると記憶注入が失われたこと自体に
        気づけないので stderr には必ず出す。SessionStart 以外は 1 で報告する（`failure_exit_code`
        の集約が「全部 0 にする」変更になっていないことの退行防止）。typo の位置引数を無視すると
        `init unexpected` が exit 0 で DB を再作成し、呼出元が失敗を検知できない。
        """
        stub = _raising(error) if error is not None else None
        if target == "settings":
            monkeypatch.setattr(cli, "_load_settings_or_raise", stub)
        elif target == "handler":
            monkeypatch.setitem(cli._COMMAND_HANDLERS, command, stub)

        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, [command, *extra])

        assert exit_code == exit_code_on_failure
        assert stderr == expected.format(command=command) + "\n"
        if command == "context":
            assert _session_start_event(stdout) == "SessionStart"
        else:
            assert stdout == ""
        if stub is not None:
            assert len(stub.calls) == 1
        assert not (tmp_path / "mem.db").exists()


class TestUsageErrors:
    """副作用より先に検出する usage error（位置引数の個数・未対応/排他/不明なオプション）。"""

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            (["learn", "unexpected"], "learn は位置引数を取りません: 'unexpected'"),
            (["list", "unexpected"], "list は位置引数を取りません: 'unexpected'"),
            (["handoff", "unexpected"], "handoff は位置引数を取りません: 'unexpected'"),
            (["show"], "key を指定してください"),
            (["promote"], "key を指定してください"),
            (["forget"], "key を指定してください"),
            (["show", "key", "extra"], "show は key を1つだけ指定してください: 'key extra'"),
            (["promote", "key", "extra"], "promote は key を1つだけ指定してください: 'key extra'"),
            (["forget", "key", "extra"], "forget は key を1つだけ指定してください: 'key extra'"),
            (["promote", "some-key", "--global"], "promote は次のオプションを取りません: --global"),
            (["show", "some-key", "--repo"], "show は次のオプションを取りません: --repo"),
            (["forget", "some-key", "--kind", "fact"], "forget は次のオプションを取りません: --kind"),
            (["init", "--json"], "init は次のオプションを取りません: --json"),
            (["list", "--global", "--repo"], "--global と --repo は同時に指定できません"),
            (["search", "alpha", "--global", "--repo"], "--global と --repo は同時に指定できません"),
            (["list", "--bogus"], "不明なオプション: --bogus"),
        ],
    )
    def test_usage_error_returns_1_without_touching_db(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str], expected: str
    ) -> None:
        """usage error は stderr 1 行と exit_code=1 で返し、DB を作らない。

        未対応オプションを黙って無視すると、`promote <key> --global` が repo 側のカードを
        昇格させ、利用者が指定した global 側は pending のまま残る。排他チェックが
        `_collect_list_rows` にしか無いと、それを呼ばない経路では素通りする。
        """
        assert _run_cli(monkeypatch, tmp_path, argv) == ("", expected + "\n", 1)
        assert not (tmp_path / "mem.db").exists()

    def test_supported_options_are_accepted(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """対応済みオプションは通ること。"""
        argv = ["list", "--global", "--status", "pending", "--limit", "3"]
        _stdout, _stderr, exit_code = _run_cli(monkeypatch, tmp_path, argv)
        assert exit_code == 0

    def test_search_still_accepts_multiple_positionals(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """search は複数 positional を検索語として連結する既存契約を維持する（回帰防止）。"""
        _seed(tmp_path, key="multi-word-hit", title="two words here")

        stdout, _stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["search", "two", "words"])

        assert exit_code == 0
        assert "multi-word-hit" in stdout


class TestInit:
    """init コマンド（DB 再作成）。"""

    def test_recreates_database_dropping_old_rows(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """既存 DB と sidecar を破棄して空の DB を作り直す。"""
        db_path = tmp_path / "mem.db"
        with Database(db_path) as db:
            db.upsert_repo(Repo(id="old", identity_key="key-old", root_path="/tmp/old"))
        for suffix in ("-wal", "-shm", "-journal"):
            (tmp_path / f"mem.db{suffix}").write_text("stale", encoding="utf-8")

        assert _run_cli(monkeypatch, tmp_path, ["init"]) == ("", "", 0)
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


class TestArgvAndStdin:
    """引数と stdin の解釈。"""

    @pytest.mark.parametrize("argv", [[], ["--help"], ["-h"]], ids=["no-command", "--help", "-h"])
    def test_help_is_printed(self, monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
        """引数なし・--help・-h は HELP_TEXT を出力する。"""
        monkeypatch.setattr(sys, "argv", ["python", *argv])
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            assert cli.main() == 0
        assert "CLI Commands for mem" in stdout.getvalue()

    def test_unknown_command_returns_2(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """未知のコマンドは exit_code=2。"""
        _stdout, _stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["nonexistent"])
        assert exit_code == 2

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("", {}),
            ("   ", {}),
            ("[1, 2]", {}),
            ("{ broken", {}),
            ('{"cwd": "/tmp"}', {"cwd": "/tmp"}),
        ],
        ids=["empty", "blank", "non-dict", "broken-json", "dict"],
    )
    def test_read_stdin_json_parses_bounded_reader_output(
        self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: dict
    ) -> None:
        """read_raw_stdin() を 1 回だけ呼び、dict の JSON だけを返し、空・非 dict・不正 JSON は空 dict に落とす。

        sys.stdin.read() は EOF まで無期限ブロックしうる（context は launcher の in-process
        実行で host の実 pipe を直接読む）ため、到着と読み取り全体に上限を持つ
        read_raw_stdin() に委譲する。空文字列は tty・stdin 未接続・deadline 到達のいずれでも
        返る値で、原因ごとの分岐は test_stdin_io.py が検証済み。
        """
        calls: list[int] = []
        monkeypatch.setattr(cli, "read_raw_stdin", lambda: calls.append(1) or raw)
        assert cli._read_stdin_json() == expected
        assert calls == [1]

    def test_positionals_flags_and_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """位置引数・真偽フラグ・値付きオプションを分離する。"""
        monkeypatch.setattr(cli, "read_raw_stdin", lambda: "")
        args = cli._parse_args_and_stdin("search", ["pipefail", "--global", "--limit", "5"])
        assert args.positionals == ("pipefail",)
        assert args.flags == frozenset({"--global"})
        assert args.values == {"--limit": "5"}

    @pytest.mark.parametrize(
        ("command", "reads_stdin"),
        [
            ("list", False),
            ("search", False),
            ("show", False),
            ("promote", False),
            ("forget", False),
            ("init", False),
            ("learn", True),
            ("context", True),
            ("handoff", True),
        ],
    )
    def test_stdin_is_read_only_for_commands_that_use_it(
        self, monkeypatch: pytest.MonkeyPatch, command: str, reads_stdin: bool
    ) -> None:
        """stdin を使う subcommand だけが stdin を読む。

        全 subcommand で無条件に読むと、書き手が pipe を閉じない呼ばれ方で
        `list` / `search` / `show` まで STDIN_FIRST_BYTE_TIMEOUT ぶん待たされ
        （実測 2.10 秒）、さらに「stdin リダイレクト漏れの可能性」という
        渡すべき stdin が無い操作には誤った警告が出る。
        """
        calls: list[int] = []
        monkeypatch.setattr(cli, "read_raw_stdin", lambda: calls.append(1) or '{"probe": true}')

        args = cli._parse_args_and_stdin(command, [])

        assert calls == ([1] if reads_stdin else [])
        assert args.stdin_data == ({"probe": True} if reads_stdin else {})

    @pytest.mark.parametrize(
        ("argv", "match"),
        [(["--limit"], "--limit"), (["--bogus"], "不明なオプション")],
        ids=["value-option-without-value", "unknown-option"],
    )
    def test_parse_options_errors(self, argv: list[str], match: str) -> None:
        """値付きオプションに値が無い・未知のオプションは CommandError。"""
        with pytest.raises(command_support.CommandError, match=match):
            cli._parse_options(argv)


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
