"""bluecore.mem.cli のテスト"""

from __future__ import annotations

import io
import json
import runpy
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bluecore.mem import cli
from bluecore.mem.database import Database
from bluecore.mem.models import Knowledge, Repo, Session
from bluecore.mem.repo_identity import resolve_repo
from bluecore.mem.settings import (
    CONTEXT_GLOBAL_CHAR_BUDGET,
    CONTEXT_HANDOFF_CHAR_BUDGET,
    CONTEXT_ITEM_CHAR_LIMIT,
)


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
        monkeypatch.setattr(
            cli, "_build_context", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db broken"))
        )
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["context"])
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
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["context"])
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
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["context"])
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
        assert cli._fit_budget(["a", "b"], cli.LIST_CHAR_BUDGET) == ["a", "b"]

    def test_fit_budget_truncates_first_line(self) -> None:
        """1 行目から予算超過なら告知行だけを返す。"""
        assert cli._fit_budget(["x" * (cli.LIST_CHAR_BUDGET + 1), "y"], cli.LIST_CHAR_BUDGET) == ["… +2 件"]


class TestQueryTerms:
    """検索クエリの語分解。"""

    def test_splits_on_whitespace_and_lowercases(self) -> None:
        """空白で割り、小文字化する。2 語以上なら連結語も足す。"""
        assert cli._query_terms("Alpha BETA") == ("alpha", "beta", "alphabeta")

    def test_single_term_is_used_as_is(self) -> None:
        """空白の無い日本語クエリは、そのままクエリ全体の部分一致になる。"""
        assert cli._query_terms("型ヒント") == ("型ヒント",)

    def test_duplicate_terms_are_deduplicated(self) -> None:
        """同じ語の重複はスコアの二重加算を避けるため畳む。"""
        assert cli._query_terms("ab ab") == ("ab", "abab")

    def test_blank_query_raises(self) -> None:
        """空白のみのクエリは CommandError。"""
        with pytest.raises(cli.CommandError, match="検索クエリ"):
            cli._query_terms("   ")


class TestScoring:
    """検索スコアの構成要素。"""

    _NOW = datetime(2026, 8, 9, tzinfo=UTC)

    def _card(self, **overrides: object) -> Knowledge:
        """採点用の Knowledge を組み立てる。"""
        fields: dict = {
            "key": "k",
            "scope": "global",
            "kind": "fact",
            "title": "title",
            "source": "agent",
            "updated_at": self._NOW.isoformat(),
        }
        fields.update(overrides)
        return Knowledge(**fields)

    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            ({"title": "alpha"}, 3.0),
            ({"key": "alpha"}, 1.5),
            ({"domain": "alpha"}, 1.0),
            ({"body": "alpha"}, 0.5),
            ({"title": "alpha", "body": "alpha"}, 3.5),
            ({}, 0.0),
        ],
    )
    def test_match_score_weights_each_field(self, overrides: dict, expected: float) -> None:
        """項目ごとの重みを合算する。domain が None でも落ちない。"""
        assert cli._match_score(self._card(**overrides), ("alpha",)) == expected

    def test_match_score_is_case_insensitive(self) -> None:
        """大文字小文字を無視して部分一致する。"""
        assert cli._match_score(self._card(title="ALPHA guide"), ("alpha",)) == 3.0

    def test_match_score_accumulates_per_term(self) -> None:
        """語ごとに加算されるので、カバーした語数が多い行ほど高くなる。"""
        card = self._card(title="alpha beta")
        assert cli._match_score(card, ("alpha", "beta")) == 6.0
        assert cli._match_score(card, ("alpha",)) == 3.0

    @pytest.mark.parametrize(
        ("updated_at", "expected"),
        [
            ("2026-08-09T00:00:00+00:00", 1.0),
            ("2026-11-07T00:00:00+00:00", 1.0),
            ("2026-05-11T00:00:00+00:00", 0.75),
        ],
    )
    def test_recency_factor(self, updated_at: str, expected: float) -> None:
        """現在・未来は 1.0、90 日前は 0.75。"""
        assert cli._recency_factor(updated_at, self._NOW) == pytest.approx(expected)

    def test_recency_factor_never_reaches_floor(self) -> None:
        """どれだけ古くても下限 0.5 を下回らない。"""
        factor = cli._recency_factor("1990-01-01T00:00:00+00:00", self._NOW)
        assert cli._SEARCH_MULTIPLIER_FLOOR < factor < 0.51

    def test_score_is_zero_without_any_hit(self) -> None:
        """1 語も当たらなければ 0.0。"""
        assert cli._score_knowledge(self._card(), ("alpha",), self._NOW) == 0.0

    def test_score_multiplies_confidence(self) -> None:
        """confidence は 0.5〜1.0 の係数として掛かる。"""
        low = cli._score_knowledge(self._card(title="alpha", confidence=0.0), ("alpha",), self._NOW)
        high = cli._score_knowledge(self._card(title="alpha", confidence=1.0), ("alpha",), self._NOW)
        assert (low, high) == (pytest.approx(1.5), pytest.approx(3.0))

    def test_matched_row_always_scores_positive(self) -> None:
        """confidence=0.0 でも一致した行は正のスコアを持つ（除外されない）。"""
        card = self._card(body="alpha", confidence=0.0, updated_at="1990-01-01T00:00:00+00:00")
        assert cli._score_knowledge(card, ("alpha",), self._NOW) > 0.0


class TestSearch:
    """search コマンド。"""

    def test_no_hit_prints_nothing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """ヒット 0 件なら 1 文字も出力しない。"""
        _seed(tmp_path, key="g", title="global card")

        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["search", "nomatch"])
        assert (stdout, stderr, exit_code) == ("", "", 0)

    def test_blank_query_returns_1(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """クエリ無しは stderr 1 行と exit_code=1。"""
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["search"])
        assert (stdout, exit_code) == ("", 1)
        assert "検索クエリ" in stderr

    def test_matches_body_but_never_prints_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """body はスコア計算にだけ使い、出力には出さない。"""
        _seed(tmp_path, key="g", kind="pitfall", title="short title", body="SECRET alpha detail")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["search", "alpha"])
        assert stdout == "- [pitfall] short title (g)\n"

    def test_long_body_does_not_grow_output(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """body の長さは出力バイト数に影響しない。"""
        _seed(tmp_path, key="g", title="short title", body="alpha " + "x" * 20000)
        long_body_output, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["search", "alpha"])

        with Database(tmp_path / "mem.db") as db:
            card = db.get_knowledge_by_key("g")
            card.body = "alpha"
            db.upsert_knowledge(card)
        short_body_output, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["search", "alpha"])

        assert long_body_output == short_body_output

    def test_title_outranks_body(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """title ヒットは body ヒットより上位。"""
        _seed(tmp_path, key="b", title="unrelated", body="alpha")
        _seed(tmp_path, key="t", title="alpha guide")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["search", "alpha"])
        assert stdout.splitlines() == ["- [fact] alpha guide (t)", "- [fact] unrelated (b)"]

    def test_excludes_rows_without_any_hit(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """1 語も当たらない知識は結果に出さない。"""
        _seed(tmp_path, key="hit", title="alpha guide")
        _seed(tmp_path, key="miss", title="beta guide")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["search", "alpha"])
        assert stdout == "- [fact] alpha guide (hit)\n"

    def test_japanese_query_without_spaces(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """空白の無い日本語クエリはクエリ全体の部分一致で拾う。"""
        _seed(tmp_path, key="j", kind="convention", title="カバレッジは 100% を維持する")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["search", "カバレッジ"])
        assert stdout == "- [convention] カバレッジは 100% を維持する (j)\n"

    def test_japanese_spaced_query_matches_unspaced_text(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """利用者が入れた空白は連結語のフォールバックで吸収する。"""
        _seed(tmp_path, key="j", kind="howto", title="型ヒントを付ける")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["search", "型", "ヒント"])
        assert stdout == "- [howto] 型ヒントを付ける (j)\n"

    def test_matches_domain_and_key(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """domain と key も検索対象。"""
        _seed(tmp_path, key="alpha-slug", title="one")
        _seed(tmp_path, key="two", title="two", domain="alpha")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["search", "alpha"])
        assert sorted(stdout.splitlines()) == ["- [fact] one (alpha-slug)", "- [fact] two (two)"]

    def test_default_limit_is_5(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """既定は 5 件で打ち切る。"""
        for index in range(8):
            _seed(tmp_path, key=f"k{index}", title=f"alpha {index}")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["search", "alpha"])
        assert len(stdout.splitlines()) == cli.SEARCH_DEFAULT_LIMIT

    def test_limit_option_overrides_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """--limit は既定件数を上書きする。"""
        for index in range(8):
            _seed(tmp_path, key=f"k{index}", title=f"alpha {index}")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["search", "alpha", "--limit", "2"])
        assert len(stdout.splitlines()) == 2

    def test_kind_and_status_filters(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """--kind と --status は list と同じ意味で効く。"""
        _seed(tmp_path, key="a", kind="fact", title="alpha fact")
        _seed(tmp_path, key="b", kind="howto", title="alpha howto")
        _seed(tmp_path, key="c", kind="fact", title="alpha pending", status="pending")

        by_kind, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["search", "alpha", "--kind", "howto"])
        by_status, _stderr, _exit = _run_cli(
            monkeypatch, tmp_path, ["search", "alpha", "--status", "pending"]
        )
        assert by_kind == "- [howto] alpha howto (b)\n"
        assert by_status == "- [fact] alpha pending (c)\n"

    def test_scope_flags(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """--global / --repo でスコープを片方に絞る。"""
        repo_id = _repo_id(tmp_path)
        _seed(tmp_path, key="g", title="alpha global")
        _seed(tmp_path, key="r", scope="repo", repo_id=repo_id, title="alpha repo")

        only_global, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["search", "alpha", "--global"])
        only_repo, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["search", "alpha", "--repo"])
        assert only_global == "- [fact] alpha global (g)\n"
        assert only_repo == "- [fact] alpha repo (r)\n"

    def test_json_flag_emits_single_line(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """--json は body を含まない 1 行 JSON を出す。"""
        _seed(tmp_path, key="g", title="alpha card", body="LONG BODY alpha")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["search", "alpha", "--json"])
        assert json.loads(stdout) == [
            {"key": "g", "scope": "global", "kind": "fact", "title": "alpha card"}
        ]

    def test_output_is_truncated_at_char_budget(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """予算超過分は捨てて ``… +N 件`` の 1 行だけ添える。"""
        for index in range(cli.SEARCH_DEFAULT_LIMIT):
            _seed(tmp_path, key=f"k{index}", title="共通" + "あ" * 200)

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["search", "共通"])
        lines = stdout.splitlines()
        assert lines[-1].startswith("… +")
        assert len(stdout) <= cli.SEARCH_CHAR_BUDGET + len(lines[-1]) + 1


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


class TestContext:
    """context コマンド（SessionStart 注入経路）。"""

    @staticmethod
    def _inject(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: dict | None = None
    ) -> str:
        """context を実行し、注入される additionalContext を取り出す。"""
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["context"], payload or {})
        assert (stderr, exit_code) == ("", 0)
        return json.loads(stdout)["hookSpecificOutput"]["additionalContext"]

    @staticmethod
    def _seed_session(tmp_path: Path, repo_id: str, **overrides: object) -> None:
        """sessions 行を 1 件直接投入する。"""
        fields: dict = {
            "session_uid": "prev-session",
            "repo_id": repo_id,
            "handoff": "前回の作業内容",
            "started_at": "2026-08-09T09:00:00+00:00",
            "ended_at": "2026-08-09T14:03:22+00:00",
        }
        fields.update(overrides)
        with Database(tmp_path / "mem.db") as db:
            db.start_session(Session(**fields))

    def test_no_knowledge_injects_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """知識も引き継ぎも無ければ 1 文字も注入しない。"""
        assert self._inject(monkeypatch, tmp_path) == ""

    def test_injects_three_sections(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """共通知識・リポジトリ・前回の続きの 3 層を規定の書式で注入する。"""
        repo_id = _repo_id(tmp_path)
        _seed(tmp_path, key="g1", kind="convention", title="Python は python3 を使う", confidence=0.9)
        _seed(tmp_path, key="g2", kind="pitfall", title="set -o pipefail が必須", confidence=0.8)
        _seed(
            tmp_path,
            key="r1",
            scope="repo",
            repo_id=repo_id,
            kind="howto",
            title="PYTHONPATH=plugins/bluecore/src を付与",
            body="venv の bluecore はプラグインキャッシュ側を解決するため",
            confidence=0.7,
        )
        self._seed_session(tmp_path, repo_id, handoff="スキーマ確定まで完了、実装未着手。")

        assert self._inject(monkeypatch, tmp_path) == (
            "<bluecore-memory>\n"
            "## 共通知識\n"
            "- [convention] Python は python3 を使う\n"
            "- [pitfall] set -o pipefail が必須\n"
            "\n"
            f"## {repo_id}\n"
            "- [howto] PYTHONPATH=plugins/bluecore/src を付与 — "
            "venv の bluecore はプラグインキャッシュ側を解決するため\n"
            "\n"
            "## 前回の続き (2026-08-09 14:03)\n"
            "スキーマ確定まで完了、実装未着手。\n"
            "</bluecore-memory>"
        )

    def test_empty_sections_are_omitted_entirely(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """global だけある場合、リポジトリ節と前回の続き節は見出しごと出さない。"""
        _seed(tmp_path, key="g", kind="fact", title="only global")

        injected = self._inject(monkeypatch, tmp_path)
        assert injected == "<bluecore-memory>\n## 共通知識\n- [fact] only global\n</bluecore-memory>"

    def test_pending_and_archived_are_not_injected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """observer の下書き（pending）と archived は注入しない。"""
        repo_id = _repo_id(tmp_path)
        _seed(tmp_path, key="p", title="pending card", status="pending")
        _seed(tmp_path, key="a", title="archived card", status="archived")
        _seed(tmp_path, key="rp", scope="repo", repo_id=repo_id, title="repo draft", status="pending")

        assert self._inject(monkeypatch, tmp_path) == ""

    def test_orders_by_confidence_then_updated_at(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """confidence DESC → updated_at DESC の順に並べる。"""
        _seed(tmp_path, key="low", title="low", confidence=0.1, updated_at="2026-08-09T00:00:00+00:00")
        _seed(tmp_path, key="old", title="old", confidence=0.9, updated_at="2026-01-01T00:00:00+00:00")
        _seed(tmp_path, key="new", title="new", confidence=0.9, updated_at="2026-08-09T00:00:00+00:00")

        injected = self._inject(monkeypatch, tmp_path)
        assert injected.splitlines()[2:5] == ["- [fact] new", "- [fact] old", "- [fact] low"]

    def test_body_is_appended_when_item_fits(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """1 件が上限に収まるなら body を ``—`` で続ける。"""
        _seed(tmp_path, key="g", kind="fact", title="短い題", body="短い補足")

        assert "- [fact] 短い題 — 短い補足" in self._inject(monkeypatch, tmp_path)

    def test_oversized_body_is_dropped_keeping_title(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """1 件が上限を超えるなら body を落として title だけにする。"""
        _seed(tmp_path, key="g", kind="fact", title="題", body="あ" * CONTEXT_ITEM_CHAR_LIMIT)

        injected = self._inject(monkeypatch, tmp_path)
        assert "- [fact] 題\n" in injected
        assert "—" not in injected

    def test_blank_body_is_not_appended(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """空白だけの body は区切りごと出さない。"""
        _seed(tmp_path, key="g", kind="fact", title="題", body="   ")

        assert "- [fact] 題\n" in self._inject(monkeypatch, tmp_path)

    def test_section_is_truncated_at_char_budget(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """節の予算超過分は捨てて ``… +N 件`` を 1 行だけ添える。"""
        for index in range(40):
            _seed(tmp_path, key=f"k{index}", title="あ" * 100)

        lines = self._inject(monkeypatch, tmp_path).splitlines()
        assert lines[-2].startswith("… +")
        # 見出し・タグ・envelope を除いた明細だけで予算内に収まっている
        assert sum(len(line) + 1 for line in lines[2:-2]) <= CONTEXT_GLOBAL_CHAR_BUDGET

    def test_long_handoff_is_truncated(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """引き継ぎが予算を超えたら末尾を省略記号で切る。"""
        repo_id = _repo_id(tmp_path)
        self._seed_session(tmp_path, repo_id, handoff="あ" * (CONTEXT_HANDOFF_CHAR_BUDGET + 50))

        handoff_line = self._inject(monkeypatch, tmp_path).splitlines()[2]
        assert len(handoff_line) == CONTEXT_HANDOFF_CHAR_BUDGET
        assert handoff_line.endswith("…")

    def test_handoff_falls_back_to_started_at(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """未終了セッションの見出しは started_at を使う。"""
        repo_id = _repo_id(tmp_path)
        self._seed_session(tmp_path, repo_id, ended_at=None, started_at="2026-08-09T09:30:00+00:00")

        assert "## 前回の続き (2026-08-09 09:30)" in self._inject(monkeypatch, tmp_path)

    def test_whitespace_only_handoff_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """空白だけの引き継ぎは節ごと出さない。"""
        repo_id = _repo_id(tmp_path)
        self._seed_session(tmp_path, repo_id, handoff="   ")

        assert self._inject(monkeypatch, tmp_path) == ""

    def test_handoff_of_other_repo_is_not_injected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """別リポジトリの引き継ぎは混ざらない。"""
        _repo_id(tmp_path)
        with Database(tmp_path / "mem.db") as db:
            db.upsert_repo(Repo(id="other", identity_key="key-other", root_path="/tmp/other"))
        self._seed_session(tmp_path, "other", handoff="別リポジトリの続き")

        assert self._inject(monkeypatch, tmp_path) == ""

    def test_records_session_row(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """hook の session_id で sessions 行を作り harness を記録する。"""
        monkeypatch.setattr(cli, "detect_harness", lambda: "claude")
        self._inject(monkeypatch, tmp_path, {"session_id": "abc-123"})

        with Database(tmp_path / "mem.db") as db:
            row = db.conn.execute("SELECT * FROM sessions").fetchone()
        assert row is not None
        assert (row["session_uid"], row["harness"], row["handoff"]) == ("abc-123", "claude", "")
        assert row["repo_id"] == _repo_id(tmp_path)

    def test_missing_session_id_records_no_row(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """session_id を渡さないハーネスでは sessions 行を作らない。"""
        self._inject(monkeypatch, tmp_path, {"cwd": str(tmp_path)})

        with Database(tmp_path / "mem.db") as db:
            assert db.conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"] == 0

    def test_resolves_repo_from_hook_cwd(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """repos は stdin の cwd から解決し last_seen_at を更新する。"""
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        _seed(tmp_path, key="g", title="global card")

        injected = self._inject(monkeypatch, tmp_path, {"cwd": str(workdir)})
        assert injected != ""
        with Database(tmp_path / "mem.db") as db:
            assert [repo.root_path for repo in db.list_repos()] == [str(workdir.resolve())]

    def test_failure_is_swallowed_and_injects_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """組み立てに失敗してもフックを壊さず注入ゼロに倒す。"""
        monkeypatch.setattr(
            cli, "_build_context", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db broken"))
        )
        assert self._inject(monkeypatch, tmp_path) == ""


class TestHandoff:
    """handoff コマンド（SessionEnd 引き継ぎ記録経路）。"""

    @staticmethod
    def _run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: dict) -> None:
        """handoff を実行し、無出力・終了コード 0 であることを確かめる。"""
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["handoff"], payload)
        assert (stdout, stderr, exit_code) == ("", "", 0)

    @staticmethod
    def _sessions(tmp_path: Path) -> list[dict]:
        """sessions テーブルの全行を dict のリストで返す。"""
        with Database(tmp_path / "mem.db") as db:
            return [dict(row) for row in db.conn.execute("SELECT * FROM sessions ORDER BY id").fetchall()]

    def test_updates_the_row_created_by_context(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """SessionStart が作った行に引き継ぎと終了時刻を書き込む。"""
        _run_cli(monkeypatch, tmp_path, ["context"], {"session_id": "s1"})
        started_at = self._sessions(tmp_path)[0]["started_at"]

        self._run(monkeypatch, tmp_path, {"session_id": "s1", "handoff": "タスク5まで完了"})

        rows = self._sessions(tmp_path)
        assert len(rows) == 1
        assert rows[0]["handoff"] == "タスク5まで完了"
        assert rows[0]["started_at"] == started_at
        assert rows[0]["ended_at"] is not None

    def test_creates_the_row_when_session_start_never_ran(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """SessionStart 未実行でも行を新設し started_at は ended_at と同値にする。"""
        monkeypatch.setattr(cli, "detect_harness", lambda: "codex")

        self._run(monkeypatch, tmp_path, {"session_id": "orphan", "handoff": "引き継ぎ"})

        rows = self._sessions(tmp_path)
        assert len(rows) == 1
        assert (rows[0]["session_uid"], rows[0]["harness"]) == ("orphan", "codex")
        assert rows[0]["started_at"] == rows[0]["ended_at"]
        assert rows[0]["repo_id"] == _repo_id(tmp_path)

    def test_resolves_repo_from_hook_cwd(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """行を新設するとき repos は stdin の cwd から解決する。"""
        workdir = tmp_path / "workdir"
        workdir.mkdir()

        self._run(monkeypatch, tmp_path, {"session_id": "s1", "cwd": str(workdir), "handoff": "続き"})

        with Database(tmp_path / "mem.db") as db:
            assert [repo.root_path for repo in db.list_repos()] == [str(workdir.resolve())]

    def test_round_trip_feeds_the_next_context(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """context → handoff → context で ``前回の続き`` が次セッションへ渡る。"""
        _run_cli(monkeypatch, tmp_path, ["context"], {"session_id": "s1"})
        self._run(monkeypatch, tmp_path, {"session_id": "s1", "handoff": "タスク5まで完了"})

        stdout, _, _ = _run_cli(monkeypatch, tmp_path, ["context"], {"session_id": "s2"})
        injected = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]

        assert "## 前回の続き (" in injected
        assert injected.endswith("タスク5まで完了\n</bluecore-memory>")

    @pytest.mark.parametrize(
        "payload",
        [
            {"handoff": "引き継ぎ"},
            {"session_id": "   ", "handoff": "引き継ぎ"},
            {"session_id": "s1", "handoff": "   "},
            {"session_id": "s1"},
        ],
        ids=["missing-session-id", "blank-session-id", "blank-handoff", "no-material"],
    )
    def test_records_nothing_without_material(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: dict
    ) -> None:
        """session_id か引き継ぎ本文が欠けていれば何も記録せず正常終了する。"""
        self._run(monkeypatch, tmp_path, payload)

        assert self._sessions(tmp_path) == []

    def test_failure_is_swallowed_and_keeps_exit_code_0(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """記録に失敗してもフックを壊さず終了コード 0 を保つ。"""
        monkeypatch.setattr(
            cli, "_record_handoff", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db broken"))
        )

        self._run(monkeypatch, tmp_path, {"session_id": "s1", "handoff": "引き継ぎ"})


def test_mem_main_module_invokes_cli_main(monkeypatch: pytest.MonkeyPatch) -> None:
    """python -m bluecore.mem が cli.main() を呼ぶ。"""
    monkeypatch.setattr(sys, "argv", ["python", "--help"])
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("bluecore.mem.__main__", run_name="__main__")

    assert excinfo.value.code == 0
