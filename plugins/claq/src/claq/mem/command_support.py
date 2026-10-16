"""``mem.cli`` の各サブコマンドハンドラが共有する型とヘルパ。

``CommandError``（利用者向けの想定内エラー）、``CommandArgs``（解析済み引数）、
ハンドラの型 ``_CommandHandler``、および ``--limit`` の変換・key の解決・出力予算・
session_id の取り出しを持つ。``cmd_knowledge`` / ``cmd_context`` / ``cli`` はこの
モジュールを import し、このモジュールはそれらを import しない（依存は一方向）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from claq.mem.database import Database
from claq.mem.models import Knowledge
from claq.mem.repo_identity import resolve_repo
from claq.mem.settings import Settings


class CommandError(Exception):
    """利用者へ 1 行で提示する想定内のエラー。

    main() がこれを捕捉して stderr へメッセージだけを出し、終了コード 1 を返す。
    """


@dataclass(frozen=True)
class CommandArgs:
    """コマンド名より後ろの引数と stdin JSON をまとめた入力。

    Attributes:
        positionals: オプション以外の位置引数。
        flags: 指定された真偽フラグ（``--global`` 等）。
        values: 値付きオプション（``--limit`` 等）の名前から値への写像。
        stdin_data: stdin から読んだ JSON。dict でなければ空。
    """

    positionals: tuple[str, ...] = ()
    flags: frozenset[str] = frozenset()
    values: dict[str, str] = field(default_factory=dict)
    stdin_data: dict[str, Any] = field(default_factory=dict)


_CommandHandler = Callable[[Settings, CommandArgs], str | None]


# --- 知識 CRUD の補助 ---


def _coerce_limit(value: str | None, default: int) -> int:
    """``--limit`` の値を正の整数へ変換する。

    Args:
        value: コマンドラインで渡された文字列。None なら *default* を返す。
        default: ``--limit`` 未指定時の件数。

    Returns:
        表示件数の上限。

    Raises:
        CommandError: 整数に変換できない、または 1 未満の場合。
    """
    if value is None:
        return default
    try:
        limit = int(value)
    except ValueError as e:
        raise CommandError(f"--limit は整数で指定してください: {value!r}") from e
    if limit < 1:
        raise CommandError(f"--limit は 1 以上で指定してください: {limit}")
    return limit


def _find_knowledge(db: Database, key: str) -> Knowledge:
    """cwd の repo スコープ → global スコープの順に key を解決する。

    Args:
        db: 参照する mem データベース。
        key: 探す key。

    Returns:
        見つかった Knowledge。

    Raises:
        CommandError: どちらのスコープにも存在しない場合。
    """
    repo = resolve_repo(None, db)
    found = db.get_knowledge_by_key(key, repo.id) or db.get_knowledge_by_key(key)
    if found is None:
        raise CommandError(f"知識が見つかりません: {key}")
    return found


def _fit_budget(lines: list[str], budget: int) -> list[str]:
    """出力行を文字数予算に収め、切り捨て分を 1 行で告知する。

    Args:
        lines: 出力予定の行（改行を含まない）。
        budget: 収めるべき文字数の上限（改行 1 文字分を各行に加算して数える）。

    Returns:
        予算内に収まる行のリスト。打ち切った場合は末尾に ``… +N 件`` を足す。
    """
    used = 0
    for index, line in enumerate(lines):
        used += len(line) + 1
        if used > budget:
            return [*lines[:index], f"… +{len(lines) - index} 件"]
    return list(lines)


def _session_uid(payload: dict[str, Any]) -> str:
    """フック JSON から session_id を取り出す。無ければ空文字。"""
    return str(payload.get("session_id") or "").strip()
