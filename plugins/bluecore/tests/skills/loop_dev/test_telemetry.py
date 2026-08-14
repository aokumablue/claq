"""bluecore.skills.loop_dev.telemetry のテスト。

loop-dev の反復テレメトリが ``knowledge`` テーブルではなく
``~/.bluecore/repos/<repo-id>/loop-dev.jsonl`` に落ちること、loop-audit が
全件を決定論的に読み戻せること、ハードタイムアウトが効くことを検証する。

データディレクトリは ``settings._DEFAULT_DATA_DIR`` の差し替えで隔離する。
"""

from __future__ import annotations

import json
import signal
from pathlib import Path

import pytest

import bluecore.mem.settings as settings_mod
from bluecore.mem.database import Database
from bluecore.skills.learn import storage
from bluecore.skills.loop_dev import telemetry


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """データディレクトリを tmp 配下に差し替えて返す。"""
    root = tmp_path / "bluecore"
    root.mkdir()
    monkeypatch.setattr(settings_mod, "_DEFAULT_DATA_DIR", root)
    return root


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """テレメトリの対象リポジトリを作り、そこを cwd にする。"""
    root = tmp_path / "demo-repo"
    root.mkdir()
    monkeypatch.chdir(root)
    return root


def _log(data_dir: Path) -> storage.JsonlLog:
    """テスト対象リポジトリのテレメトリログを返す。"""
    return storage.JsonlLog(
        data_dir / "repos" / "demo-repo" / "loop-dev.jsonl",
        data_dir / "repos" / "demo-repo" / "loop-dev.archive",
    )


_MIN_RECORD = ["record", "--task", "T", "--result", "converged", "--iterations", "1"]


# --- record ------------------------------------------------------------------


def test_record_writes_jsonl_and_prints_path(
    data_dir: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """テレメトリを JSONL へ 1 行追記し、書き込んだパスを出力する。"""
    exit_code = telemetry.main(
        [
            "record",
            "--task",
            "  リファクタ   テレメトリ移送  ",
            "--result",
            "circuit-break",
            "--iterations",
            "2",
            "--max-iterations",
            "2",
            "--blockers",
            "3",
            "--flakes",
            "1",
            "--commit",
            "abc1234",
            "--commit",
            "def5678",
            "--note",
            "残 blocker は次セッション",
        ]
    )

    assert exit_code == 0
    log = _log(data_dir)
    assert capsys.readouterr().out.strip() == str(log.path)

    record = log.read_records()[0]
    assert record["repo"] == "demo-repo"
    assert record["task"] == "リファクタ テレメトリ移送"  # 空白畳み込み
    assert record["result"] == "circuit-break"
    assert record["iterations"] == 2
    assert record["max_iterations"] == 2
    assert record["blockers"] == 3
    assert record["flakes"] == 1
    assert record["commits"] == ["abc1234", "def5678"]
    assert record["note"] == "残 blocker は次セッション"
    assert record["timestamp"].endswith("Z")


def test_record_defaults(data_dir: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """任意項目は 0 / 2 / 空リストに既定され、note は付かない。"""
    assert telemetry.main(_MIN_RECORD) == 0
    capsys.readouterr()
    record = _log(data_dir).read_records()[0]
    assert record["max_iterations"] == 2
    assert record["blockers"] == 0
    assert record["flakes"] == 0
    assert record["commits"] == []
    assert "note" not in record


def test_record_leaves_knowledge_table_untouched(
    data_dir: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """テレメトリは生ログ。knowledge には 1 件も書かない（pending も含む）。"""
    assert telemetry.main(_MIN_RECORD) == 0
    capsys.readouterr()
    with Database(data_dir / "mem.db") as db:
        assert db.list_knowledge() == []


def test_record_redacts_secrets(data_dir: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """自由文に混じったトークンはマスクして記録する。"""
    assert telemetry.main([*_MIN_RECORD, "--note", "key=sk-ant-" + "a" * 24]) == 0
    capsys.readouterr()
    note = _log(data_dir).read_records()[0]["note"]
    assert "sk-ant-" not in note
    assert "[REDACTED]" in note


def test_record_truncates_long_text(data_dir: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """自由文は MAX_TEXT_CHARS で切り詰めて生ログを肥大させない。"""
    long_task = "テレメトリ移送 " * 200
    assert telemetry.main(["record", "--task", long_task, "--result", "stopped", "--iterations", "0"]) == 0
    capsys.readouterr()
    assert len(_log(data_dir).read_records()[0]["task"]) == telemetry.MAX_TEXT_CHARS


def test_record_rejects_unknown_result(data_dir: Path, repo: Path) -> None:
    """Result は反復履歴の 4 値のみ。それ以外は argparse が弾く。"""
    with pytest.raises(SystemExit) as excinfo:
        telemetry.main(["record", "--task", "T", "--result", "maybe", "--iterations", "1"])
    assert excinfo.value.code == 2


@pytest.mark.parametrize(
    ("option", "value"),
    [("--iterations", "-1"), ("--max-iterations", "-1"), ("--blockers", "-1"), ("--flakes", "-1")],
)
def test_record_rejects_negative_counts(
    data_dir: Path, repo: Path, capsys: pytest.CaptureFixture[str], option: str, value: str
) -> None:
    """反復数・件数に負値は許さず、JSONL も作らない。"""
    argv = ["record", "--task", "T", "--result", "converged", "--iterations", "1", option, value]
    assert telemetry.main(argv) == 1
    assert "0 以上" in capsys.readouterr().err
    assert not _log(data_dir).path.exists()


def test_record_maintains_log_before_appending(
    data_dir: Path, repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """追記前にローテーション・purge を回す。"""
    calls: list[str] = []
    monkeypatch.setattr(storage.JsonlLog, "maintain", lambda _self: calls.append("maintain"))
    assert telemetry.main(_MIN_RECORD) == 0
    capsys.readouterr()
    assert calls == ["maintain"]


# --- list --------------------------------------------------------------------


def _seed(data_dir: Path, *days: str) -> storage.JsonlLog:
    """指定日のレコードを 1 件ずつ書き込んだログを返す。"""
    log = _log(data_dir)
    log.ensure_dirs()
    for day in days:
        log.append({"timestamp": f"{day}T00:00:00Z", "task": day})
    return log


def _listed_tasks(capsys: pytest.CaptureFixture[str]) -> list[str]:
    """``list`` の stdout から各レコードの task を古い順で返す。"""
    return [json.loads(line)["task"] for line in capsys.readouterr().out.splitlines()]


def test_list_outputs_all_records_oldest_first(
    data_dir: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """関連度打ち切り無しで全件を古い順に 1 行 1 JSON で出す。"""
    _seed(data_dir, "2026-06-01", "2026-07-01", "2026-08-01")
    assert telemetry.main(["list"]) == 0
    assert _listed_tasks(capsys) == ["2026-06-01", "2026-07-01", "2026-08-01"]


def test_list_empty_prints_nothing(data_dir: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """0 件なら無出力。"""
    assert telemetry.main(["list"]) == 0
    assert capsys.readouterr().out == ""


def test_list_filters_by_period_inclusive(
    data_dir: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--since / --until はどちらもその日を含む。"""
    _seed(data_dir, "2026-05-31", "2026-06-01", "2026-06-30", "2026-07-01")
    assert telemetry.main(["list", "--since", "2026-06-01", "--until", "2026-06-30"]) == 0
    assert _listed_tasks(capsys) == ["2026-06-01", "2026-06-30"]


def test_list_since_only(data_dir: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--until 省略時は上限なし。"""
    _seed(data_dir, "2026-05-31", "2026-06-01")
    assert telemetry.main(["list", "--since", "2026-06-01"]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 1


def test_list_drops_records_without_usable_timestamp(
    data_dir: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """期間指定時、日付を読めないレコードは母数から外す。"""
    log = _log(data_dir)
    log.ensure_dirs()
    log.append({"task": "no timestamp"})
    log.append({"timestamp": "2026-06-15T00:00:00Z", "task": "ok"})
    assert telemetry.main(["list", "--since", "2026-01-01"]) == 0
    assert _listed_tasks(capsys) == ["ok"]


def test_list_keeps_records_without_timestamp_when_no_period(
    data_dir: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """期間指定が無ければ timestamp 欠落レコードも出す。"""
    log = _log(data_dir)
    log.ensure_dirs()
    log.append({"task": "no timestamp"})
    assert telemetry.main(["list"]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 1


def test_list_limit_keeps_newest(data_dir: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--limit は末尾 N 件（新しい方）を残す。"""
    _seed(data_dir, "2026-06-01", "2026-07-01", "2026-08-01")
    assert telemetry.main(["list", "--limit", "2"]) == 0
    assert _listed_tasks(capsys) == ["2026-07-01", "2026-08-01"]


@pytest.mark.parametrize("option", ["--since", "--until"])
def test_list_rejects_malformed_date(
    data_dir: Path, repo: Path, capsys: pytest.CaptureFixture[str], option: str
) -> None:
    """日付は YYYY-MM-DD のみ受け付ける。"""
    assert telemetry.main(["list", option, "6月"]) == 1
    assert "YYYY-MM-DD" in capsys.readouterr().err


# --- path --------------------------------------------------------------------


def test_path_prints_jsonl_location(data_dir: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """テレメトリ JSONL の絶対パスを出力する（ファイルは作らない）。"""
    assert telemetry.main(["path"]) == 0
    assert capsys.readouterr().out.strip() == str(_log(data_dir).path)
    assert not _log(data_dir).path.exists()


# --- ハードタイムアウト ------------------------------------------------------


def test_timeout_disabled_when_non_positive(
    data_dir: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--timeout 0 ならアラームを張らない。"""
    assert telemetry.main(["--timeout", "0", "path"]) == 0
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_timeout_fires_and_reports(
    data_dir: Path, repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ハンドラが時間内に終わらなければ打ち切って 1 を返す。"""

    def _hang(_args: object) -> int:
        """SIGALRM を即時送出してタイムアウト経路を踏むハンドラ。"""
        signal.raise_signal(signal.SIGALRM)
        raise AssertionError("SIGALRM で中断されるはず")  # pragma: no cover

    monkeypatch.setattr(telemetry, "_handle_path", _hang)
    assert telemetry.main(["--timeout", "10", "path"]) == 1
    assert "ハードタイムアウト" in capsys.readouterr().err
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_missing_subcommand_exits_2(data_dir: Path, repo: Path) -> None:
    """サブコマンド未指定は argparse が終了コード 2 で弾く。"""
    with pytest.raises(SystemExit) as excinfo:
        telemetry.main([])
    assert excinfo.value.code == 2


def test_main_reads_sys_argv_by_default(
    data_dir: Path, repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """argv 省略時は sys.argv[1:] を使う（launcher からの起動経路）。"""
    monkeypatch.setattr(telemetry.sys, "argv", ["telemetry", "path"])
    assert telemetry.main() == 0
    assert capsys.readouterr().out.strip() == str(_log(data_dir).path)
