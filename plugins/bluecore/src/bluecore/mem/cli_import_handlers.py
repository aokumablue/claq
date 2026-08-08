"""mem CLI: import handler (外部データの取り込み)。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from bluecore.mem.database import Database
    from bluecore.mem.settings import Settings

    OpenDbFn = Callable[[Settings], AbstractContextManager[Database]]
    GitUserFn = Callable[[], str]


def handle_import(
    settings: Settings,
    stdin_data: dict[str, Any],
    *,
    open_db: OpenDbFn,
    get_git_user_name: GitUserFn,
) -> None:
    """外部データを mem に取り込む。"""
    from bluecore.mem.importers import import_adrs, import_event_logs, import_instincts

    origin_user = get_git_user_name()
    types = stdin_data.get("types", ["instincts", "adrs", "events"])
    repo_root = stdin_data.get("repo_root")

    result = {"instincts": 0, "adrs": 0, "events": 0}

    with open_db(settings) as db:
        if "instincts" in types:
            result["instincts"] = import_instincts(db, origin_user)

        if "adrs" in types and repo_root:
            result["adrs"] = import_adrs(db, origin_user, repo_root)

        if "events" in types:
            result["events"] = import_event_logs(db, origin_user)

    print(json.dumps({"success": True, "imported": result}, ensure_ascii=False))
