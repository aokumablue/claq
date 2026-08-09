"""bluecore.mem.cli のテスト"""

from __future__ import annotations

import io
import json
import runpy
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from bluecore.mem import cli
from bluecore.mem.database import Database
from bluecore.mem.models import Knowledge, Repo
from bluecore.mem.repo_identity import resolve_repo


def _run_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    argv: list[str],
    stdin_payload: dict | None = None,
) -> tuple[str, str, int]:
    """データディレクトリと cwd を tmp_path に差し替えて cli.main() を実行する。"""
    import bluecore.mem.settings as settings_mod

    monkeypatch.setattr(settings_mod, "_DEFAULT_DATA_DIR", tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["python", *argv])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(stdin_payload or {})))

    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = cli.main()
    return stdout.getvalue(), stderr.getvalue(), exit_code


def _repo_id(tmp_path: Path) -> str:
    """tmp_path を cwd とみなしたときの repos.id を返す（repos 行も作る）。"""
    with Database(tmp_path / "mem.db") as db:
        return resolve_repo(tmp_path, db).id


def _seed(tmp_path: Path, **overrides: object) -> Knowledge:
    """knowledge 行を 1 件直接投入する。"""
    fields: dict = {
        "key": "seeded",
        "scope": "global",
        "kind": "fact",
        "title": "seeded title",
        "source": "agent",
    }
    fields.update(overrides)
    with Database(tmp_path / "mem.db") as db:
        return db.upsert_knowledge(Knowledge(**fields))


class TestSetup:
    """setup コマンド（SessionStart フック経路）。"""

    def test_creates_database_and_emits_session_start_json(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """DB を作成し、SessionStart 契約の JSON を stdout に出す。"""
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["setup"])
        assert exit_code == 0
        assert stderr == ""
        payload = json.loads(stdout)
        assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert (tmp_path / "mem.db").exists()

    def test_swallows_db_failure(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """DB 初期化が失敗してもフックを壊さず JSON を返す。"""
        monkeypatch.setattr(
            cli, "_initialize_db", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db broken"))
        )
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["setup"])
        assert exit_code == 0
        assert stderr == ""
        assert json.loads(stdout)["hookSpecificOutput"]["hookEventName"] == "SessionStart"

    def test_settings_failure_still_emits_session_start_json(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """設定ロード失敗でも exit_code=0 と JSON 出力を維持する。"""
        monkeypatch.setattr(
            cli, "_load_settings_or_raise", lambda: (_ for _ in ()).throw(RuntimeError("設定失敗"))
        )
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["setup"])
        assert exit_code == 0
        assert stderr == ""
        assert json.loads(stdout)["hookSpecificOutput"]["hookEventName"] == "SessionStart"

    def test_handler_exception_keeps_exit_code_1_but_emits_json(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """ハンドラ例外時も JSON を出しつつ exit_code=1 を返す。"""
        monkeypatch.setattr(
            cli, "_run_session_start_command", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["setup"])
        assert exit_code == 1
        assert "boom" in stderr
        assert json.loads(stdout)["hookSpecificOutput"]["hookEventName"] == "SessionStart"


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

    def test_tty_stdin_is_not_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """stdin が tty なら読み取らず空 dict を返す。"""
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        assert cli._parse_args_and_stdin([]).stdin_data == {}

    @pytest.mark.parametrize("raw", ["", "   ", "[1, 2]", "{ broken"])
    def test_unusable_stdin_yields_empty_dict(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        """空・非 dict・不正 JSON はすべて空 dict に落とす。"""
        monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
        assert cli._read_stdin_json() == {}

    def test_valid_stdin_is_parsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """dict の JSON はそのまま返る。"""
        monkeypatch.setattr(sys, "stdin", io.StringIO('{"cwd": "/tmp"}'))
        assert cli._read_stdin_json() == {"cwd": "/tmp"}

    def test_stdin_os_error_is_logged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """stdin の読み取りが OSError でも空 dict に落とす。"""

        class _BrokenStdin:
            def isatty(self) -> bool:
                return False

            def read(self) -> str:
                raise OSError("stdin gone")

        monkeypatch.setattr(sys, "stdin", _BrokenStdin())
        assert cli._read_stdin_json() == {}

    def test_positionals_flags_and_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """位置引数・真偽フラグ・値付きオプションを分離する。"""
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        args = cli._parse_args_and_stdin(["pipefail", "--global", "--limit", "5"])
        assert args.positionals == ("pipefail",)
        assert args.flags == frozenset({"--global"})
        assert args.values == {"--limit": "5"}

    def test_value_option_without_value_is_error(self) -> None:
        """値付きオプションに値が無ければ CommandError。"""
        with pytest.raises(cli.CommandError, match="--limit"):
            cli._parse_options(["--limit"])

    def test_unknown_option_is_error(self) -> None:
        """未知のオプションは CommandError。"""
        with pytest.raises(cli.CommandError, match="不明なオプション"):
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
        monkeypatch.setattr(
            cli, "_load_settings_or_raise", lambda: (_ for _ in ()).throw(RuntimeError("設定失敗"))
        )
        _stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["init"])
        assert exit_code == 1
        assert "設定/ログ初期化失敗" in stderr

    def test_handler_exception_returns_1(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """ハンドラ例外は exit_code=1 と stderr 出力。"""
        monkeypatch.setattr(
            cli, "_run_normal_command", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("init failure"))
        )
        _stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["init"])
        assert exit_code == 1
        assert "init failure" in stderr


class TestGenerateKey:
    """key の自動生成規則。"""

    def test_ascii_tokens_survive_japanese_title(self) -> None:
        """日本語まじりでも ASCII トークンだけの読める key になる。"""
        key = cli.generate_key("pytest をパイプする際は set -o pipefail が必須", "pitfall")
        assert key == "pytest-set-o-pipefail"

    def test_pure_japanese_title_falls_back_to_hash(self) -> None:
        """ASCII 英数字を含まない title は kind + ハッシュの決定的 key になる。"""
        key = cli.generate_key("テーブル定義変更は移行不要", "decision")
        assert key.startswith("decision-")
        assert len(key) == len("decision-") + 8
        assert key == cli.generate_key("テーブル定義変更は移行不要", "decision")


class TestLearn:
    """learn コマンド。"""

    def test_stores_repo_scoped_card_with_generated_key(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """scope 省略時は repo スコープで登録し、1 行だけ出力する。"""
        payload = {"kind": "howto", "title": "run pytest with pipefail", "body": "why"}
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["learn"], payload)
        assert (stderr, exit_code) == ("", 0)
        assert stdout == "learned: run-pytest-with-pipefail\n"

        with Database(tmp_path / "mem.db") as db:
            rows = db.list_knowledge()
        assert len(rows) == 1
        assert (rows[0].scope, rows[0].status, rows[0].source) == ("repo", "active", "agent")
        assert rows[0].repo_id is not None
        assert rows[0].body == "why"

    def test_stores_global_card_with_all_fields(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """明示された全項目をそのまま保存する。"""
        payload = {
            "key": "pipefail",
            "scope": "global",
            "kind": "pitfall",
            "title": "set -o pipefail",
            "body": "パイプ先の失敗を拾う",
            "domain": "testing",
            "confidence": 0.9,
            "source": "human",
            "source_ref": "CLAUDE.md",
        }
        stdout, _stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["learn"], payload)
        assert (stdout, exit_code) == ("learned: pipefail\n", 0)

        with Database(tmp_path / "mem.db") as db:
            found = db.get_knowledge_by_key("pipefail")
        assert found is not None
        assert (found.scope, found.repo_id, found.domain) == ("global", None, "testing")
        assert (found.confidence, found.source, found.source_ref) == (0.9, "human", "CLAUDE.md")

    def test_observer_can_store_pending_via_flag(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """--status pending で観測由来の下書きを積める。"""
        payload = {"scope": "global", "kind": "fact", "title": "observed", "source": "observer"}
        _stdout, _stderr, exit_code = _run_cli(
            monkeypatch, tmp_path, ["learn", "--status", "pending"], payload
        )
        assert exit_code == 0
        with Database(tmp_path / "mem.db") as db:
            found = db.get_knowledge_by_key("observed")
        assert found is not None
        assert (found.status, found.source) == ("pending", "observer")

    def test_status_can_come_from_json(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """JSON の status も受け付ける。"""
        payload = {"scope": "global", "kind": "fact", "title": "draft", "status": "pending"}
        _stdout, _stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["learn"], payload)
        assert exit_code == 0
        with Database(tmp_path / "mem.db") as db:
            found = db.get_knowledge_by_key("draft")
        assert found is not None and found.status == "pending"

    def test_same_key_updates_instead_of_duplicating(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """同じ key の再投入は更新に落ちる。"""
        base = {"key": "dup", "scope": "global", "kind": "fact", "title": "first"}
        _run_cli(monkeypatch, tmp_path, ["learn"], base)
        _run_cli(monkeypatch, tmp_path, ["learn"], {**base, "title": "second"})

        with Database(tmp_path / "mem.db") as db:
            rows = db.list_knowledge()
        assert len(rows) == 1
        assert rows[0].title == "second"

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ({"kind": "fact"}, "title は必須です"),
            ({"title": "t"}, "kind は"),
            ({"kind": "fact", "title": "t", "scope": "team"}, "scope は"),
            ({"kind": "fact", "title": "t", "source": "robot"}, "source は"),
            ({"kind": "fact", "title": "t", "status": "draft"}, "status は"),
            ({"kind": "fact", "title": "t", "confidence": "high"}, "confidence は数値"),
            ({"kind": "fact", "title": "t", "confidence": 1.5}, "confidence は 0.0〜1.0"),
        ],
    )
    def test_invalid_payload_returns_1(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: dict, expected: str
    ) -> None:
        """不正な入力は stderr 1 行と exit_code=1。"""
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["learn"], payload)
        assert (stdout, exit_code) == ("", 1)
        assert len(stderr.splitlines()) == 1
        assert expected in stderr


class TestList:
    """list コマンド。"""

    def test_empty_result_prints_nothing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """0 件なら 1 文字も出力しない。"""
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["list"])
        assert (stdout, stderr, exit_code) == ("", "", 0)

    def test_lists_repo_and_global_titles_without_body(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """既定は repo + global を 1 件 1 行、body は出さない。"""
        repo_id = _repo_id(tmp_path)
        _seed(tmp_path, key="g", kind="pitfall", title="global card", body="GLOBAL BODY")
        _seed(tmp_path, key="r", scope="repo", repo_id=repo_id, kind="howto", title="repo card")

        stdout, _stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["list"])
        assert exit_code == 0
        assert sorted(stdout.splitlines()) == ["- [howto] repo card (r)", "- [pitfall] global card (g)"]
        assert "GLOBAL BODY" not in stdout

    def test_global_flag_limits_scope(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """--global は global スコープだけを出す。"""
        repo_id = _repo_id(tmp_path)
        _seed(tmp_path, key="g", title="global card")
        _seed(tmp_path, key="r", scope="repo", repo_id=repo_id, title="repo card")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["list", "--global"])
        assert stdout == "- [fact] global card (g)\n"

    def test_repo_flag_limits_scope(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """--repo は cwd の repo スコープだけを出す。"""
        repo_id = _repo_id(tmp_path)
        _seed(tmp_path, key="g", title="global card")
        _seed(tmp_path, key="r", scope="repo", repo_id=repo_id, title="repo card")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["list", "--repo"])
        assert stdout == "- [fact] repo card (r)\n"

    def test_both_scope_flags_conflict(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """--global と --repo の同時指定はエラー。"""
        _stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["list", "--global", "--repo"])
        assert exit_code == 1
        assert "同時に指定できません" in stderr

    def test_status_filter(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """--status で pending だけを出せる。"""
        _seed(tmp_path, key="a", title="active card")
        _seed(tmp_path, key="p", title="pending card", status="pending")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["list", "--status", "pending"])
        assert stdout == "- [fact] pending card (p)\n"

    def test_kind_filter(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """--kind で種別を絞れる。"""
        _seed(tmp_path, key="a", kind="fact", title="a card")
        _seed(tmp_path, key="b", kind="howto", title="b card")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["list", "--kind", "howto"])
        assert stdout == "- [howto] b card (b)\n"

    def test_limit_caps_rows(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """--limit は件数上限として効く。"""
        for index in range(3):
            _seed(tmp_path, key=f"k{index}", title=f"card {index}")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["list", "--limit", "1"])
        assert len(stdout.splitlines()) == 1

    def test_default_limit_is_20(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """既定は 20 件で打ち切る。"""
        for index in range(25):
            _seed(tmp_path, key=f"k{index}", title=f"c{index}")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["list"])
        assert len(stdout.splitlines()) == cli.LIST_DEFAULT_LIMIT

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            (["list", "--status", "gone"], "status は"),
            (["list", "--kind", "rumor"], "kind は"),
            (["list", "--limit", "many"], "--limit は整数"),
            (["list", "--limit", "0"], "--limit は 1 以上"),
        ],
    )
    def test_invalid_options_return_1(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str], expected: str
    ) -> None:
        """不正なオプション値は stderr 1 行と exit_code=1。"""
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, argv)
        assert (stdout, exit_code) == ("", 1)
        assert expected in stderr

    def test_json_flag_emits_single_line(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """--json は機械処理用に 1 行の JSON を出す（body は含めない）。"""
        _seed(tmp_path, key="g", title="global card", body="BODY")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["list", "--json"])
        assert json.loads(stdout) == [
            {"key": "g", "scope": "global", "kind": "fact", "title": "global card"}
        ]
        assert len(stdout.splitlines()) == 1

    def test_output_is_truncated_at_char_budget(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """予算超過分は捨てて ``… +N 件`` の 1 行だけ添える。"""
        for index in range(20):
            _seed(tmp_path, key=f"k{index}", title="あ" * 100)

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["list"])
        lines = stdout.splitlines()
        assert lines[-1].startswith("… +")
        assert len(stdout) <= cli.LIST_CHAR_BUDGET + len(lines[-1]) + 1

    def test_fit_budget_keeps_everything_within_budget(self) -> None:
        """予算内なら全行そのまま返す。"""
        assert cli._fit_budget(["a", "b"]) == ["a", "b"]

    def test_fit_budget_truncates_first_line(self) -> None:
        """1 行目から予算超過なら告知行だけを返す。"""
        assert cli._fit_budget(["x" * (cli.LIST_CHAR_BUDGET + 1), "y"]) == ["… +2 件"]


class TestShow:
    """show コマンド。"""

    def test_shows_all_fields_including_body(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """body を含む全項目を人間可読で出す。"""
        _seed(
            tmp_path,
            key="pipefail",
            kind="pitfall",
            title="set -o pipefail",
            body="パイプ先の失敗を拾う",
            domain="testing",
            confidence=0.9,
        )
        stdout, _stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["show", "pipefail"])
        assert exit_code == 0
        fields = dict(line.split(": ", 1) for line in stdout.splitlines())
        assert fields["key"] == "pipefail"
        assert fields["scope"] == "global"
        assert fields["repo"] == "-"
        assert fields["kind"] == "pitfall"
        assert fields["body"] == "パイプ先の失敗を拾う"
        assert fields["domain"] == "testing"
        assert fields["confidence"] == "0.90"
        assert fields["status"] == "active"
        assert fields["source"] == "agent"

    def test_repo_scope_wins_over_global(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """同じ key が両スコープにあれば repo スコープを返す。"""
        repo_id = _repo_id(tmp_path)
        _seed(tmp_path, key="dup", title="global one")
        _seed(tmp_path, key="dup", scope="repo", repo_id=repo_id, title="repo one")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["show", "dup"])
        assert "title: repo one" in stdout
        assert f"repo: {repo_id}" in stdout

    def test_empty_body_and_domain_render_as_dash(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """body / domain が空なら ``-`` を出す。"""
        _seed(tmp_path, key="bare", title="bare card")
        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["show", "bare"])
        assert "body: -" in stdout
        assert "domain: -" in stdout

    def test_missing_key_returns_1(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """該当が無ければ stderr 1 行と exit_code=1。"""
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["show", "nope"])
        assert (stdout, exit_code) == ("", 1)
        assert stderr.splitlines() == ["知識が見つかりません: nope"]

    def test_key_argument_is_required(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """key 未指定は exit_code=1。"""
        _stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["show"])
        assert exit_code == 1
        assert "key を指定してください" in stderr


class TestPromote:
    """promote コマンド。"""

    def test_pending_becomes_active(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """pending を active に上げて 1 行だけ出力する。"""
        _seed(tmp_path, key="draft", title="draft card", status="pending")

        stdout, _stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["promote", "draft"])
        assert (stdout, exit_code) == ("promoted: draft\n", 0)
        with Database(tmp_path / "mem.db") as db:
            found = db.get_knowledge_by_key("draft")
        assert found is not None and found.status == "active"


class TestForget:
    """forget コマンド。"""

    def test_archives_card(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """status を archived にして 1 行だけ出力する。"""
        _seed(tmp_path, key="old", title="old card")

        stdout, _stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["forget", "old"])
        assert (stdout, exit_code) == ("forgot: old\n", 0)
        with Database(tmp_path / "mem.db") as db:
            found = db.get_knowledge_by_key("old")
        assert found is not None
        assert (found.status, found.superseded_by) == ("archived", None)

    def test_records_successor(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """--superseded-by で置き換え先の id を残す。"""
        _seed(tmp_path, key="old", title="old card")
        successor = _seed(tmp_path, key="new", title="new card")

        stdout, _stderr, exit_code = _run_cli(
            monkeypatch, tmp_path, ["forget", "old", "--superseded-by", "new"]
        )
        assert (stdout, exit_code) == ("forgot: old (superseded by new)\n", 0)
        with Database(tmp_path / "mem.db") as db:
            found = db.get_knowledge_by_key("old")
        assert found is not None
        assert (found.status, found.superseded_by) == ("archived", successor.id)

    def test_unknown_successor_returns_1(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """置き換え先が無ければエラーにして元の行も変えない。"""
        _seed(tmp_path, key="old", title="old card")

        _stdout, stderr, exit_code = _run_cli(
            monkeypatch, tmp_path, ["forget", "old", "--superseded-by", "ghost"]
        )
        assert exit_code == 1
        assert "知識が見つかりません: ghost" in stderr
        with Database(tmp_path / "mem.db") as db:
            found = db.get_knowledge_by_key("old")
        assert found is not None and found.status == "active"


def test_mem_main_module_invokes_cli_main(monkeypatch: pytest.MonkeyPatch) -> None:
    """python -m bluecore.mem が cli.main() を呼ぶ。"""
    monkeypatch.setattr(sys, "argv", ["python", "--help"])
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("bluecore.mem.__main__", run_name="__main__")

    assert excinfo.value.code == 0
