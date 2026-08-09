"""フックから呼び出される CLI エントリポイント。

スキーマ再設計に伴い、現時点で提供するのは DB の初期化・再作成のみ。
learn / list / context / handoff / search は後続タスクで追加する。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from bluecore.hooks.hook_common import print_session_start_output
from bluecore.mem.database import Database
from bluecore.mem.logger import get as _get_logger
from bluecore.mem.settings import Settings

log = _get_logger("CLI")

# SessionStart フックで JSON 出力が必須なコマンドの集合。
# main() のフォールバック保証とエラー時の早期 return に使用する。
_SESSION_START_COMMANDS: frozenset[str] = frozenset({"setup"})
# WAL モードの接続が残す sidecar ファイルの拡張子。
_DB_SIDECAR_SUFFIXES: tuple[str, ...] = ("-wal", "-shm", "-journal")
_CommandHandler = Callable[[Settings, dict[str, Any]], str | None]


def _parse_argv_and_stdin() -> tuple[str, dict[str, Any]]:
    """コマンド名と JSON stdin を読み取る。

    Returns:
        コマンド名と、stdin から読んだ dict のタプル。
        stdin が空・非 dict・不正 JSON の場合の dict は空。
    """
    command = sys.argv[1]

    stdin_data: dict[str, Any] = {}
    if not sys.stdin.isatty():
        try:
            raw = sys.stdin.read()
            if raw.strip():
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    stdin_data = parsed
        except (json.JSONDecodeError, OSError) as e:
            log.warning("stdin 読み取り失敗: %s", e)

    return command, stdin_data


def _load_settings_or_raise() -> Settings:
    """Settings と logger を初期化して返す。

    Returns:
        ロード済みの Settings。

    Raises:
        Exception: Settings のロードまたは logger 初期化に失敗した場合。
    """
    import bluecore.mem.logger as _logger_mod

    settings = Settings.load()
    _logger_mod.setup(settings.log_dir, settings.log_level)
    return settings


def _run_session_start_command(command: str, settings: Settings, stdin_data: dict[str, Any]) -> str | None:
    """SessionStart コマンドを実行して追加コンテキストを返す。

    Args:
        command: 実行するコマンド名。
        settings: mem 設定。
        stdin_data: stdin から読んだ JSON。

    Returns:
        追加コンテキスト文字列。無ければ None。
    """
    handler = _COMMAND_HANDLERS[command]
    return handler(settings, stdin_data)


def _run_normal_command(command: str, settings: Settings, stdin_data: dict[str, Any]) -> int:
    """SessionStart 以外のコマンドを実行し終了コードを返す。

    Args:
        command: 実行するコマンド名。
        settings: mem 設定。
        stdin_data: stdin から読んだ JSON。

    Returns:
        終了コード。未知のコマンドは 2。
    """
    handler = _COMMAND_HANDLERS.get(command)
    if handler is None:
        log.error("不明なコマンド: %s", command)
        return 2

    handler(settings, stdin_data)
    return 0


def main() -> int:
    """CLI エントリポイント。argv からコマンドを解決して実行し、終了コードを返す。

    Returns:
        終了コード。SessionStart コマンドは失敗してもフックエラーを避けるため 0 を維持する。
    """
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help"}:
        print(HELP_TEXT)
        return 0

    command, stdin_data = _parse_argv_and_stdin()
    additional_context = ""
    # SESSION_START コマンドは設定ロード失敗でも exit_code=0 を維持する。
    # フックが非 0 を返すとセッション全体がエラー扱いになるため。
    exit_code = 0
    _silent = command in _SESSION_START_COMMANDS
    try:
        settings = _load_settings_or_raise()
    except Exception as e:
        if not _silent:
            print(f"設定/ログ初期化失敗: {e}", file=sys.stderr)
            exit_code = 1
    else:
        try:
            if _silent:
                additional_context = _run_session_start_command(command, settings, stdin_data) or ""
            else:
                exit_code = _run_normal_command(command, settings, stdin_data)
        except Exception as e:
            log.error("コマンド %s 失敗: %s", command, e)
            print(f"コマンド {command} 失敗: {e}", file=sys.stderr)
            exit_code = 1
    finally:
        if _silent:
            print_session_start_output(additional_context)

    return exit_code


def _handle_setup(settings: Settings) -> str:
    """setup コマンド: データディレクトリと DB を初期化する。

    SessionStart フックから呼ばれるため、失敗しても例外を伝播させない。

    Args:
        settings: mem 設定。

    Returns:
        追加コンテキスト（本コマンドは常に空文字列）。
    """
    try:
        _initialize_db(settings)
        log.info("セットアップ完了: %s", settings.data_path)
    except Exception as e:
        log.warning("setup 失敗: %s", e)

    return ""


def _handle_init(settings: Settings) -> None:
    """init コマンド: 既存 DB を削除して再作成する。

    Args:
        settings: mem 設定。
    """
    _initialize_db(settings, recreate=True)
    log.info("DB再作成完了: %s", settings.db_path)


def _initialize_db(settings: Settings, *, recreate: bool = False) -> None:
    """データディレクトリと mem.db を初期化する。

    Args:
        settings: mem 設定。
        recreate: True なら既存 DB を破棄してから作り直す。
    """
    if recreate:
        _remove_db_artifacts(settings.db_path)

    settings.data_path.mkdir(parents=True, exist_ok=True)
    settings.save()
    with Database(settings.db_path):
        pass

    if recreate:
        # WAL モードの新規接続が残す -wal/-shm を除去し、再作成後の
        # データディレクトリを pristine に保つ。close 時の checkpoint で
        # データは mem.db へ反映済みのため安全。SQLite ビルドにより
        # close 時に自動削除されない環境があるため明示削除する。
        _remove_db_sidecars(settings.db_path)


def _remove_db_artifacts(db_path: Path) -> None:
    """mem.db 本体と WAL sidecar を削除する。

    Args:
        db_path: mem.db の絶対パス。
    """
    db_path.unlink(missing_ok=True)
    _remove_db_sidecars(db_path)


def _remove_db_sidecars(db_path: Path) -> None:
    """WAL/SHM/journal の sidecar ファイルを削除する。

    Args:
        db_path: mem.db の絶対パス。
    """
    for suffix in _DB_SIDECAR_SUFFIXES:
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)


_COMMAND_HANDLERS: dict[str, _CommandHandler] = {
    "init": lambda settings, stdin_data: (_handle_init(settings) or None),
    "setup": lambda settings, stdin_data: (_handle_setup(settings) or None),
}


HELP_TEXT = """\
CLI Commands for mem

Usage:
  python -m bluecore.mem <command>

Commands:
  init               Recreate the local mem database from scratch
  setup              Initialize the local mem database
"""


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
