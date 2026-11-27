"""tests/mem の複数のファイルが使う補助（CLI 実行・repos/knowledge の組み立てと投入・失敗スタブ・空 DB の fixture）。"""

from __future__ import annotations

import io
import json
import logging
import sys
from collections.abc import Callable, Iterator
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from claq.mem import cli, cmd_context
from claq.mem import settings as settings_mod
from claq.mem.database import Database
from claq.mem.models import Knowledge, Repo, utc_now_iso
from claq.mem.repo_identity import resolve_repo


def reset_mem_logger() -> None:
    """claq.mem のロガーに付いているハンドラを外す（`setup` 前の状態へ戻す）。"""
    logging.getLogger("claq.mem").handlers.clear()


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    """テスト用の空 DB を開いて返す。"""
    database = Database(tmp_path / "mem.db")
    yield database
    database.close()


def _raising(error: BaseException) -> Callable[..., None]:
    """呼ばれると error を送出し、呼び出しを ``calls`` に記録するスタブを返す。"""
    calls: list[tuple[object, ...]] = []

    def _stub(*args: object, **_kwargs: object) -> None:
        """呼び出しを記録して error を送出する。"""
        calls.append(args)
        raise error

    _stub.calls = calls  # type: ignore[attr-defined]
    return _stub


def _run_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    argv: list[str],
    stdin_payload: dict | None = None,
    *,
    bg_failure_notice: str = "",
) -> tuple[str, str, int]:
    """データディレクトリと cwd を tmp_path に差し替えて cli.main() を実行する。"""
    monkeypatch.setattr(settings_mod, "_default_data_dir", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["python", *argv])
    # _read_stdin_json は stdin_io.read_raw_stdin() 経由（tty 判定と、daemon スレッドの
    # 読み取りを待つ期限を内包）。その機構自体は test_stdin_io.py が
    # 検証済みのため、ここでは境界を直接差し替えて stdin 内容だけを渡す。
    monkeypatch.setattr(cli, "read_raw_stdin", lambda: json.dumps(stdin_payload or {}))
    # 実環境の ~/.claq/logs/bg-*.log の内容にテスト結果が左右されない
    # よう、既定では detach 失敗痕跡なしに固定する（detach 失敗通知のテストは
    # bg_failure_notice 引数で明示的に上書きする）。
    monkeypatch.setattr(cmd_context, "recent_bg_failure_notice", lambda: bg_failure_notice)

    stdout = io.StringIO()
    stderr = io.StringIO()
    # logger は初回の setup で stderr を掴んだまま使い回すため、前のテストの初期化が残ると
    # logger の stderr 出力がこの捕捉に入らない。前後で捨て、捕捉した stderr へ掴ませる。
    reset_mem_logger()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = cli.main()
    finally:
        reset_mem_logger()
    return stdout.getvalue(), stderr.getvalue(), exit_code


def _repo_id(tmp_path: Path) -> str:
    """tmp_path を cwd とみなしたときの repos.id を返す（repos 行も作る）。"""
    with Database(tmp_path / "mem.db") as db:
        return resolve_repo(tmp_path, db).id


def _repo_row(repo_id: str, identity_key: str, root_path: str) -> Repo:
    """remote 無しで今観測した repos 行を組み立てる。"""
    now = utc_now_iso()
    return Repo(
        id=repo_id, identity_key=identity_key, root_path=root_path, remote_url=None, first_seen_at=now, last_seen_at=now
    )


def _make_knowledge(**overrides: object) -> Knowledge:
    """既定値に overrides を重ねた Knowledge を組み立てる（DB へは入れない）。"""
    fields: dict = {
        "key": "seeded",
        "scope": "global",
        "kind": "fact",
        "title": "seeded title",
        "source": "agent",
        # status は必須引数（既定値なし）。list/search の既定絞り込みが 'active'
        # なので、ヘルパの既定も 'active' に置く。
        "status": "active",
        "repo_id": None,
        "body": "",
        "domain": None,
        "confidence": 0.5,
        "source_ref": None,
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
    }
    fields.update(overrides)
    return Knowledge(**fields)


def _seed(tmp_path: Path, **overrides: object) -> Knowledge:
    """knowledge 行を 1 件直接投入する。"""
    knowledge = _make_knowledge(**overrides)
    with Database(tmp_path / "mem.db") as db:
        db.upsert_knowledge(knowledge)
        return db.get_knowledge_by_key(knowledge.key, knowledge.repo_id)


def _all_knowledge(db: Database) -> list[Knowledge]:
    """knowledge の全行を更新の新しい順に返す（本番の list_knowledge はスコープと status で必ず絞る）。"""
    rows = db.conn.execute("SELECT * FROM knowledge ORDER BY updated_at DESC, id DESC").fetchall()
    return [Knowledge.from_row(row) for row in rows]
