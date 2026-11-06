"""フックおよび人間・エージェントから呼び出される CLI エントリポイント。

提供するのは DB の作り直し（``init``）、知識 CRUD
（``learn`` / ``list`` / ``show`` / ``promote`` / ``forget``）、全文検索
（``search``）、SessionStart への知識注入（``context``）、および SessionEnd の
引き継ぎ記録（``handoff``）。

設計原則は **出力トークンの最小化**。重い絞り込みは Python プロセス内で
完結させ、LLM のコンテキストへ返すのは最小限のテキストだけにする:

* ``body`` を返すのは ``show`` のみ。``list`` / ``search`` は ``title`` だけを
  1 件 1 行で出す。``body`` は ``search`` のスコア計算にしか使わない。
* ``list`` の既定件数は 20 件、``search`` は 5 件。出力はそれぞれ
  ``cmd_knowledge.LIST_CHAR_BUDGET`` / ``cmd_knowledge.SEARCH_CHAR_BUDGET`` 文字で
  打ち切る。
* 0 件なら何も出力しない（「見つかりません」も出さない）。
* 既定出力は素のテキスト。``--json`` は機械処理用のオプトインに留める。

``search`` は転置インデックスも仮想テーブルも持たない。知識カードは数百件
オーダーに収まるため、全件をロードして Python 側でスコアリングするほうが
安く、DB も肥大しない。

このモジュールは引数解析・dispatch・``init`` を持つ。知識 CRUD と ``search`` は
``cmd_knowledge``、``context`` / ``handoff`` は ``cmd_context``、各ハンドラが共有する
型とヘルパは ``command_support`` にある（依存は ``cli`` → ``cmd_*`` → ``command_support``
の一方向）。
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from typing import Any, NamedTuple

from claq.hooks.hook_common import print_session_start_output
from claq.hooks.stdin_io import read_raw_stdin
from claq.mem import logger as mem_logger
from claq.mem.cmd_context import handle_context, handle_handoff
from claq.mem.cmd_knowledge import (
    handle_forget,
    handle_learn,
    handle_list,
    handle_promote,
    handle_search,
    handle_show,
)
from claq.mem.command_support import CommandArgs, CommandError, CommandHandler
from claq.mem.database import Database
from claq.mem.settings import Settings

log = mem_logger.get("CLI")

# SessionStart フックで JSON 出力が必須なコマンドの集合。
_SESSION_START_COMMANDS: frozenset[str] = frozenset({"context"})
# WAL モードの接続が残す sidecar ファイルの拡張子。
_DB_SIDECAR_SUFFIXES: tuple[str, ...] = ("-wal", "-shm", "-journal")

# 値を伴うオプションと、真偽のみのフラグ。
_VALUE_OPTIONS: frozenset[str] = frozenset({"--status", "--kind", "--limit", "--superseded-by"})
_BOOL_FLAGS: frozenset[str] = frozenset({"--global", "--repo", "--json"})

# stdin の JSON を実際に使う subcommand。これ以外は stdin を読まない。
# 無条件に読むと、書き手が pipe を閉じない呼ばれ方で `list` / `search` / `show`
# まで STDIN_FIRST_BYTE_TIMEOUT ぶん待たされ（実測 2.10 秒）、渡すべき stdin が
# 無いのに「stdin リダイレクト漏れの可能性」と誤って警告する。
_STDIN_COMMANDS: frozenset[str] = frozenset({"learn", "context", "handoff"})

_FILTER_OPTIONS: frozenset[str] = frozenset({"--global", "--repo", "--status", "--kind", "--limit", "--json"})


class _CommandSpec(NamedTuple):
    """subcommand が受理する引数の形。

    Attributes:
        arity: 位置引数の個数。None は可変長（``search`` は複数の位置引数を検索語として連結する）。
        options: 受理するオプション。
    """

    arity: int | None
    options: frozenset[str]


# オプションの対応表を持たないと `promote <key> --global` のような未対応 flag が
# 黙って通り、指定と異なるカード（repo 優先探索の結果）を操作してしまう。
_COMMAND_SPECS: dict[str, _CommandSpec] = {
    "init": _CommandSpec(0, frozenset()),
    "context": _CommandSpec(0, frozenset()),
    "handoff": _CommandSpec(0, frozenset()),
    "learn": _CommandSpec(0, frozenset()),
    "list": _CommandSpec(0, _FILTER_OPTIONS),
    "search": _CommandSpec(None, _FILTER_OPTIONS),
    "show": _CommandSpec(1, frozenset()),
    "promote": _CommandSpec(1, frozenset()),
    "forget": _CommandSpec(1, frozenset({"--superseded-by"})),
}


def _parse_options(argv: list[str]) -> CommandArgs:
    """コマンド以降の argv を位置引数・フラグ・値付きオプションへ分解する。

    Args:
        argv: コマンド名を除いた引数リスト。

    Returns:
        stdin を除いて組み立てた CommandArgs。

    Raises:
        CommandError: 未知のオプション、または値付きオプションに値が無い場合。
    """
    positionals: list[str] = []
    flags: set[str] = set()
    values: dict[str, str] = {}
    tokens = iter(argv)
    for token in tokens:
        if token in _VALUE_OPTIONS:
            value = next(tokens, None)
            if value is None:
                raise CommandError(f"オプション {token} に値がありません")
            values[token] = value
        elif token in _BOOL_FLAGS:
            flags.add(token)
        elif token.startswith("-"):
            raise CommandError(f"不明なオプション: {token}")
        else:
            positionals.append(token)
    return CommandArgs(positionals=tuple(positionals), flags=frozenset(flags), values=values)


def _read_stdin_json() -> dict[str, Any]:
    """stdin から JSON オブジェクトを読み取る。

    ``context`` は launcher の in-process 実行で host の実 pipe を直接読むため、
    書き手が pipe を閉じないまま止まると ``sys.stdin.read()`` は EOF まで
    無期限ブロックする。そのため到着・読み取り全体の両方に上限を持つ
    ``read_raw_stdin()`` を使う。

    Returns:
        読み取った dict。stdin が tty・空なら空 dict。

    Raises:
        CommandError: stdin が JSON object として読めない（不正 JSON・非 dict・上限での切り捨て）場合。
    """
    raw = read_raw_stdin()
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise CommandError(f"stdin を JSON として読めません: {e}") from e
    if not isinstance(parsed, dict):
        raise CommandError("stdin が JSON object ではありません")
    return parsed


def _parse_args_and_stdin(command: str, argv: list[str]) -> CommandArgs:
    """コマンド名より後ろの引数と、必要な subcommand だけ stdin JSON を読み取る。

    Args:
        command: subcommand 名。stdin を読むかどうかの判定に使う。
        argv: コマンド名を除いた引数リスト。

    Returns:
        組み立てた CommandArgs。``command`` が stdin を使わないなら
        ``stdin_data`` は空 dict。

    Raises:
        CommandError: オプションの解析に失敗した場合、または stdin が JSON object として読めない場合。
    """
    args = _parse_options(argv)
    if command not in _STDIN_COMMANDS:
        return args
    return dataclasses.replace(args, stdin_data=_read_stdin_json())


def _check_positional_arity(command: str, args: CommandArgs) -> None:
    """side effect（DB オープン・再作成等）より先に位置引数の個数を検証する。

    ドキュメント化されていない位置引数を黙って無視すると、typo が成功扱いに
    なり呼出元が失敗を検知できない（例: ``init unexpected`` が exit 0 で DB を
    再作成する）。

    Args:
        command: 実行するコマンド名。
        args: コマンド引数と stdin JSON。

    Raises:
        CommandError: 契約に反する個数の位置引数が指定された場合。
    """
    spec = _COMMAND_SPECS.get(command)
    if spec is None:
        return
    if spec.arity == 0 and args.positionals:
        raise CommandError(f"{command} は位置引数を取りません: {' '.join(args.positionals)!r}")
    if spec.arity == 1:
        if not args.positionals:
            raise CommandError("key を指定してください")
        if len(args.positionals) > 1:
            raise CommandError(f"{command} は key を1つだけ指定してください: {' '.join(args.positionals)!r}")


def _check_learn_options(command: str, args: CommandArgs) -> None:
    """learn は ``--status`` を受け付けない。

    受け付けると、caller（外部入力を処理した agent 含む）が ``--status active``
    を指定するだけで SessionStart への永続注入を自己承認できてしまう。黙って
    無視すると「指定したのに効いていない」事故になるため usage error にする。
    ``list``/``search`` の絞り込みとしては引き続き有効。

    Args:
        command: 実行するコマンド名。
        args: コマンド引数。

    Raises:
        CommandError: ``learn`` に ``--status`` が指定された場合。
    """
    if command == "learn" and "--status" in args.values:
        raise CommandError(
            "learn --status は指定できません（常に status=pending で登録されます。"
            "有効化は `promote <key>` を通す運用です。promote は技術的に人間へ"
            "限定されておらず、Bash を持つ agent からも到達できます）"
        )


def _check_supported_options(command: str, args: CommandArgs) -> None:
    """subcommand が受理しないオプションを、副作用より先に拒否する。

    黙って無視すると、利用者の指定と異なるカードを操作したことに気づけない
    （`promote <key> --global` が repo 側を昇格させ、global 側は pending のまま
    残る）。

    Args:
        command: 実行するコマンド名。
        args: コマンド引数と stdin JSON。

    Raises:
        CommandError: 未対応のオプション、または排他オプションの同時指定。
    """
    spec = _COMMAND_SPECS.get(command)
    if spec is None:
        return
    used = set(args.flags) | set(args.values)
    unsupported = sorted(used - spec.options)
    if unsupported:
        raise CommandError(f"{command} は次のオプションを取りません: {' '.join(unsupported)}")
    if {"--global", "--repo"} <= used:
        raise CommandError("--global と --repo は同時に指定できません")


def _load_settings_or_raise() -> Settings:
    """Settings と logger を初期化して返す。

    Returns:
        初期化済みの Settings。
    """
    settings = Settings()
    mem_logger.setup(settings.log_dir, settings.log_level)
    return settings


def _prepare_command(command: str, argv: list[str]) -> tuple[Settings, CommandArgs]:
    """引数と stdin を読んで検証し、通ったら設定と logger を初期化する。

    Args:
        command: 実行するコマンド名。
        argv: コマンド名を除いた引数リスト。

    Returns:
        初期化済みの Settings と、検証済みの CommandArgs。

    Raises:
        CommandError: 引数・stdin が契約に反する場合。
    """
    args = _parse_args_and_stdin(command, argv)
    _check_positional_arity(command, args)
    # learn --status には専用の説明があるため、一般的な未対応オプション
    # チェックより先に評価する。
    _check_learn_options(command, args)
    _check_supported_options(command, args)
    return _load_settings_or_raise(), args


def main() -> int:
    """CLI エントリポイント。argv からコマンドを解決して実行し、終了コードを返す。

    Returns:
        終了コード。成功は 0、失敗は 1、未知のコマンドは 2。SessionStart コマンドは成功したときだけ
        stdout へ注入の JSON を出す（失敗時に JSON を出すと Claude Code は終了コードを見ずに成功扱いにし、
        利用者に hook error の通知が出ない）。
    """
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help"}:
        print(HELP_TEXT)
        return 0

    command = sys.argv[1]
    try:
        settings, args = _prepare_command(command, sys.argv[2:])
    except CommandError as e:
        print(str(e), file=sys.stderr)
        return 1
    except Exception as e:
        print(f"設定/ログ初期化失敗: {e}", file=sys.stderr)
        return 1

    # 不明なコマンドの報告はファイルへも残すので、logger の初期化後に判定する。
    handler = _COMMAND_HANDLERS.get(command)
    if handler is None:
        log.error("不明なコマンド: %s", command)
        return 2

    try:
        additional_context = handler(settings, args)
    except CommandError as e:
        print(str(e), file=sys.stderr)
        return 1
    except Exception as e:
        log.error("コマンド %s 失敗: %s", command, e)
        print(f"コマンド {command} 失敗: {e}", file=sys.stderr)
        return 1

    if command in _SESSION_START_COMMANDS:
        print_session_start_output(additional_context)
    return 0


# --- DB の作り直し ---


def _handle_init(settings: Settings, _args: CommandArgs) -> None:
    """init コマンド: 既存 DB を削除して空の DB を作り直す。

    通常運用の DB 作成は ``Database`` のコンストラクタが担う。本コマンドは
    中身を捨てて作り直す手動操作専用。

    Args:
        settings: mem 設定。
        _args: 未使用。他コマンドとハンドラ署名を揃える。
    """
    _remove_db_artifacts(settings.db_path)
    with Database(settings.db_path):
        pass
    # SQLite ビルドにより close 時に -wal/-shm が自動削除されない環境があるため
    # 明示削除する。close 時の checkpoint でデータは mem.db へ反映済みで安全。
    _remove_db_sidecars(settings.db_path)
    log.info("DB再作成完了: %s", settings.db_path)


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


_COMMAND_HANDLERS: dict[str, CommandHandler] = {
    "init": _handle_init,
    "context": handle_context,
    "handoff": handle_handoff,
    "learn": handle_learn,
    "list": handle_list,
    "search": handle_search,
    "show": handle_show,
    "promote": handle_promote,
    "forget": handle_forget,
}


HELP_TEXT = """\
CLI Commands for mem

Usage:
  python -m claq.mem <command> [options]

Commands:
  init                                  Recreate the local mem database from scratch
  context                             Emit the SessionStart knowledge injection (hook JSON on stdin)
  handoff                               Record the SessionEnd handoff (hook JSON on stdin; "handoff" key wins)
  learn                                 Store one knowledge card read as JSON from stdin
  list [--global|--repo]                List knowledge titles (default: repo + global, active, 20)
       [--status S] [--kind K]
       [--limit N] [--json]
  search <query> [--global|--repo]      Rank knowledge titles by relevance (default: repo + global, active, 5)
         [--status S] [--kind K]        Scores title/key/domain/body but never prints body
         [--limit N] [--json]
  show <key>                            Show one knowledge card including its body
  promote <key>                         Flip a pending knowledge card to active
  forget <key> [--superseded-by KEY]    Archive a knowledge card
"""


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
