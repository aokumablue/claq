"""tests/mem のテストの複数のファイルが使う補助（CLI 実行・repos/knowledge の投入・必ず失敗するスタブ）。"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from claq.mem import cli, cmd_context
from claq.mem.database import Database
from claq.mem.models import Knowledge
from claq.mem.repo_identity import resolve_repo


def _run_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    argv: list[str],
    stdin_payload: dict | None = None,
    *,
    bg_failure_notice: str = "",
) -> tuple[str, str, int]:
    """データディレクトリと cwd を tmp_path に差し替えて cli.main() を実行する。"""
    import claq.mem.settings as settings_mod

    monkeypatch.setattr(settings_mod, "_default_data_dir", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["python", *argv])
    # _read_stdin_json は stdin_io.read_raw_stdin() 経由（tty 判定・
    # select による deadline を内包）。その機構自体は test_stdin_io.py が
    # 検証済みのため、ここでは境界を直接差し替えて stdin 内容だけを渡す。
    monkeypatch.setattr(cli, "read_raw_stdin", lambda: json.dumps(stdin_payload or {}))
    # 実環境の ~/.claq/logs/bg-*.log の内容にテスト結果が左右されない
    # よう、既定では detach 失敗痕跡なしに固定する（detach 失敗通知のテストは
    # bg_failure_notice 引数で明示的に上書きする）。
    monkeypatch.setattr(cmd_context, "recent_bg_failure_notice", lambda: bg_failure_notice)

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
        # status は必須引数（既定値なし）。list/search の既定絞り込みが 'active'
        # なので、ヘルパの既定も 'active' に置く。
        "status": "active",
    }
    fields.update(overrides)
    with Database(tmp_path / "mem.db") as db:
        return db.upsert_knowledge(Knowledge(**fields))


def _always_raise(message: str):
    """呼び出されると必ず RuntimeError を送出するスタブを返す。

    呼び出しごとの ``(args, kwargs)`` を ``stub.calls`` に記録する。パッチが対象へ届かず
    本物が成功した場合は結果が変わらないテストが多いので、呼ばれたことを ``calls`` で固定する。
    """
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def _stub(*args: object, **kwargs: object) -> None:
        """呼び出しを記録して RuntimeError を送出する。"""
        calls.append((args, kwargs))
        raise RuntimeError(message)

    _stub.calls = calls  # type: ignore[attr-defined]
    return _stub
