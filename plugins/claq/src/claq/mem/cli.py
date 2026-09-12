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
  ``LIST_CHAR_BUDGET`` / ``SEARCH_CHAR_BUDGET`` 文字で打ち切る。
* 0 件なら何も出力しない（「見つかりません」も出さない）。
* 既定出力は素のテキスト。``--json`` は機械処理用のオプトインに留める。

``search`` は転置インデックスも仮想テーブルも持たない。知識カードは数百件
オーダーに収まるため、全件を ``list_knowledge`` でロードして Python 側で
スコアリングするほうが安く、DB も肥大しない。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from claq.hooks.hook_common import (
    print_session_start_output,
    read_raw_stdin,
    recent_bg_failure_notice,
)
from claq.lib.core_utils import actor_identity, get_plugin_root
from claq.lib.env_pointer import ancestor_pointers_supported
from claq.lib.harness import normalize_user_message
from claq.mem import logger as mem_logger
from claq.mem.database import Database
from claq.mem.handoff import build_handoff
from claq.mem.knowledge_input import (
    KINDS,
    STATUSES,
    KnowledgeInputError,
    optional_str,
    parse_knowledge_payload,
    validate_choice,
)
from claq.mem.models import Knowledge, Repo, Session, utc_now_iso
from claq.mem.repo_identity import resolve_repo
from claq.mem.settings import (
    CONTEXT_GLOBAL_CHAR_BUDGET,
    CONTEXT_HANDOFF_CHAR_BUDGET,
    CONTEXT_ITEM_CHAR_LIMIT,
    CONTEXT_REPO_CHAR_BUDGET,
    Settings,
)
from claq.mem.tag_stripping import strip_tags

log = mem_logger.get("CLI")

# SessionStart フックで JSON 出力が必須なコマンドの集合。
# main() のフォールバック保証とエラー時の早期 return に使用する。
_SESSION_START_COMMANDS: frozenset[str] = frozenset({"context"})
# WAL モードの接続が残す sidecar ファイルの拡張子。
_DB_SIDECAR_SUFFIXES: tuple[str, ...] = ("-wal", "-shm", "-journal")

# 値を伴うオプションと、真偽のみのフラグ。
_VALUE_OPTIONS: frozenset[str] = frozenset({"--status", "--kind", "--limit", "--superseded-by"})
_BOOL_FLAGS: frozenset[str] = frozenset({"--global", "--repo", "--json"})

LIST_DEFAULT_LIMIT = 20
"""``list`` の既定表示件数。増やすには ``--limit`` の明示が要る。"""

LIST_CHAR_BUDGET = 1400
"""``list`` の出力予算（文字数）。400 トークン相当で打ち切る。"""

SEARCH_DEFAULT_LIMIT = 5
"""``search`` の既定表示件数。上位だけ返し、詳細は ``show`` に任せる。"""

SEARCH_CHAR_BUDGET = 700
"""``search`` の出力予算（文字数）。200 トークン相当で打ち切る。"""

_SEARCH_FIELD_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("title", 3.0),
    ("key", 1.5),
    ("domain", 1.0),
    ("body", 0.5),
)
"""検索語がどの項目に当たったかの重み。

``title`` は人が書いた 1 行要約で、語が出るならそれが主題そのものなので最重量。
``key`` は title 由来のスラッグで意味は同じだが機械生成で語が落ちるため半減。
``domain`` は分類語で、当たれば主題を示すが粒度が粗い。``body`` は長文ゆえに
無関係な語が偶然含まれる確率が高く、``title`` の 1/6 まで下げる。
"""

_SEARCH_RECENCY_SCALE_DAYS = 90.0
"""新しさ係数の減衰スケール（日）。この日数で係数が 1.0 → 0.75 に落ちる。"""

_SEARCH_MULTIPLIER_FLOOR = 0.5
"""confidence / 新しさ係数の下限。

どちらも下限 0.5・上限 1.0 に収めるため、両方が最悪でもスコアは 1/4 までしか
下がらない。``title`` ヒット（3.0）が ``body`` ヒット（0.5）を下回ることはなく、
確度と鮮度は僅差の順位を決める補正に留まる。confidence=0.0 のカードが
スコア 0 に潰れて「ヒットしなかった」扱いになるのも防ぐ。
"""


class CommandError(Exception):
    """利用者へ 1 行で提示する想定内のエラー。

    main() がこれを捕捉して stderr へメッセージだけを出し、終了コード 1 を返す。
    """


# 人間が promote 済みのカードへ再 learn したときの拒否理由（H-3 対応）。
# 「失敗」ではなく「その知識は既に有効なので何もしなくてよい」と読めるよう、
# 次に取るべき行動まで書く。ここを曖昧にすると、agent が別 key で作り直して
# 重複カードを量産する（この経路が防ごうとしていた事故そのもの）。
_ACTIVE_CARD_IS_IMMUTABLE_MESSAGE = (
    "learn: {key} は既に active（人間が promote 済み）です。"
    "agent からの更新は受け付けません —— 更新できてしまうと、人間が承認した内容と"
    "違うものが注入され続けるためです。この知識は既に SessionStart へ注入されている"
    "ので、記録としては完了しています。**別の key で作り直さないでください**"
    "（重複カードになります）。本文を変えたい場合は人間へ依頼してください。"
)


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

_Ranker = Callable[[Knowledge], float]
"""知識カード 1 件に検索スコアを与える関数。0.0 は「ヒットなし」を意味する。"""


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

    hook_common.read_raw_stdin() を使う。tty 判定に加え、最初のバイト
    到着（2 秒）と読み取り全体（5 秒）の両方に上限を持つ。``context`` は
    launcher の in-process 実行で host の実 pipe を直接読むため、書き手が
    pipe を閉じないまま部分送信で止まった場合に旧実装（sys.stdin.read()）
    は EOF まで無期限ブロックしていた（F-03 対応）。

    Returns:
        読み取った dict。stdin が tty・空・上限到達・非 dict・不正 JSON
        なら空 dict。
    """
    raw = read_raw_stdin()
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("stdin 読み取り失敗: %s", e)
        return {}
    return parsed if isinstance(parsed, dict) else {}


# stdin の JSON を実際に使う subcommand。これ以外は stdin を読まない。
#
# 全 subcommand で無条件に読むと、書き手が pipe を閉じない呼ばれ方（他コマンドと
# 連結された bash ブロック等）で `list` / `search` / `show` のような stdin を
# 一切使わない操作まで STDIN_FIRST_BYTE_TIMEOUT ぶん待たされる（実測 2.10 秒）。
# さらに「stdin リダイレクト漏れの可能性」という警告は、渡すべき stdin が
# そもそも無いこれらの subcommand には誤りで、利用者を無い問題の調査へ誘導する。
_STDIN_COMMANDS: frozenset[str] = frozenset({"learn", "context", "handoff"})


def _parse_args_and_stdin(command: str, argv: list[str]) -> CommandArgs:
    """コマンド名より後ろの引数と、必要な subcommand だけ stdin JSON を読み取る。

    Args:
        command: subcommand 名。stdin を読むかどうかの判定に使う。
        argv: コマンド名を除いた引数リスト。

    Returns:
        組み立てた CommandArgs。``command`` が stdin を使わないなら
        ``stdin_data`` は空 dict。

    Raises:
        CommandError: オプションの解析に失敗した場合。
    """
    args = _parse_options(argv)
    return CommandArgs(
        positionals=args.positionals,
        flags=args.flags,
        values=args.values,
        stdin_data=_read_stdin_json() if command in _STDIN_COMMANDS else {},
    )


# 位置引数を一切取らない subcommand（L-01 対応）。
_ZERO_POSITIONAL_COMMANDS: frozenset[str] = frozenset({"init", "learn", "list", "context", "handoff"})

# 位置引数をちょうど 1 個（key）だけ取る subcommand（L-01 対応）。
_SINGLE_KEY_COMMANDS: frozenset[str] = frozenset({"show", "promote", "forget"})

# subcommand ごとに受理するオプション。`_parse_options` はグローバルな
# `_VALUE_OPTIONS` / `_BOOL_FLAGS` しか見ないため、ここで対応表を持たないと
# `promote <key> --global` のような未対応 flag が黙って通り、指定と異なる
# カード（repo 優先探索の結果）を操作してしまう（F-22）。`--global` と
# `--repo` の排他チェックも list/search からしか呼ばれていなかったため、
# `--global --repo` の同時指定すら素通りしていた。
_SUPPORTED_OPTIONS: dict[str, frozenset[str]] = {
    "init": frozenset(),
    "context": frozenset(),
    "handoff": frozenset(),
    "learn": frozenset(),
    "list": frozenset({"--global", "--repo", "--status", "--kind", "--limit", "--json"}),
    "search": frozenset({"--global", "--repo", "--status", "--kind", "--limit", "--json"}),
    "show": frozenset(),
    "promote": frozenset(),
    "forget": frozenset({"--superseded-by"}),
}

# 同時に指定できないオプションの組。
_MUTUALLY_EXCLUSIVE_OPTIONS: tuple[tuple[str, str], ...] = (("--global", "--repo"),)


def _check_supported_options(command: str, args: CommandArgs) -> None:
    """subcommand が受理しないオプションを、副作用より先に拒否する。

    黙って無視すると、利用者の指定と異なるカードを操作したことに気づけない。
    たとえば `promote <key> --global` は repo 側のカードを昇格させ、global 側は
    pending のまま残っていた。

    Args:
        command: 実行するコマンド名。
        args: コマンド引数と stdin JSON。

    Returns:
        なし。

    Raises:
        CommandError: 未対応のオプション、または排他オプションの同時指定。
    """
    supported = _SUPPORTED_OPTIONS.get(command)
    if supported is None:
        return

    used = set(args.flags) | set(args.values)
    unsupported = sorted(used - supported)
    if unsupported:
        raise CommandError(f"{command} は次のオプションを取りません: {' '.join(unsupported)}")

    for left, right in _MUTUALLY_EXCLUSIVE_OPTIONS:
        if left in used and right in used:
            raise CommandError(f"{left} と {right} は同時に指定できません")


def _check_positional_arity(command: str, args: CommandArgs) -> None:
    """side effect（DB オープン・再作成等）より先に位置引数の個数を検証する（L-01 対応）。

    ドキュメント化されていない位置引数を黙って無視すると、typo が成功扱いに
    なり呼出元が失敗を検知できない（例: 隔離 DB で ``init unexpected`` を
    実行すると、usage error ではなく exit 0 で DB が再作成されていた）。
    各 subcommand の契約: ``init``/``learn``/``list``/``context``/``handoff``
    は 0 個、``show``/``promote``/``forget`` はちょうど 1 個。``search`` は
    複数の positional を検索語として連結する既存契約のため対象外とする。

    Args:
        command: 実行するコマンド名。
        args: コマンド引数と stdin JSON。

    Returns:
        なし。

    Raises:
        CommandError: 契約に反する個数の位置引数が指定された場合。
    """
    if command in _ZERO_POSITIONAL_COMMANDS and args.positionals:
        raise CommandError(f"{command} は位置引数を取りません: {' '.join(args.positionals)!r}")
    if command in _SINGLE_KEY_COMMANDS:
        if not args.positionals:
            raise CommandError("key を指定してください")
        if len(args.positionals) > 1:
            raise CommandError(f"{command} は key を1つだけ指定してください: {' '.join(args.positionals)!r}")


def _check_learn_options(command: str, args: CommandArgs) -> None:
    """learn は ``--status`` を受け付けない（H-01 対応）。

    caller（agent・外部入力を処理した agent 含む）が ``--status active`` を
    指定するだけで永続 SessionStart context への注入を自己承認できていた。
    ``--status`` は ``list``/``search`` の絞り込みフラグとしては引き続き有効
    （`_VALUE_OPTIONS` に残したまま）だが、``learn`` に渡された場合だけ拒否する。
    黙って無視すると「指定したのに効いていない」という別の事故を招くため、
    usage error として明示的に拒否する。

    Args:
        command: 実行するコマンド名。
        args: コマンド引数。

    Returns:
        なし。

    Raises:
        CommandError: ``learn`` に ``--status`` が指定された場合。
    """
    if command == "learn" and "--status" in args.values:
        raise CommandError(
            "learn --status は指定できません（常に status=pending で登録されます。"
            "有効化は `promote <key>` を通す運用です。promote は技術的に人間へ"
            "限定されておらず、Bash を持つ agent からも到達できます）"
        )


def _load_settings_or_raise() -> Settings:
    """Settings と logger を初期化して返す。

    Returns:
        初期化済みの Settings。

    Raises:
        Exception: logger 初期化に失敗した場合。
    """
    settings = Settings()
    mem_logger.setup(settings.log_dir, settings.log_level)
    return settings


def _run_session_start_command(command: str, settings: Settings, args: CommandArgs) -> str | None:
    """SessionStart コマンドを実行して追加コンテキストを返す。

    Args:
        command: 実行するコマンド名。
        settings: mem 設定。
        args: コマンド引数と stdin JSON。

    Returns:
        追加コンテキスト文字列。無ければ None。
    """
    handler = _COMMAND_HANDLERS[command]
    return handler(settings, args)


def _run_normal_command(command: str, settings: Settings, args: CommandArgs) -> int:
    """SessionStart 以外のコマンドを実行し終了コードを返す。

    Args:
        command: 実行するコマンド名。
        settings: mem 設定。
        args: コマンド引数と stdin JSON。

    Returns:
        終了コード。未知のコマンドは 2。
    """
    handler = _COMMAND_HANDLERS.get(command)
    if handler is None:
        log.error("不明なコマンド: %s", command)
        return 2

    handler(settings, args)
    return 0


def main() -> int:
    """CLI エントリポイント。argv からコマンドを解決して実行し、終了コードを返す。

    Returns:
        終了コード。SessionStart コマンドは失敗してもフックエラーを避けるため 0 を維持する。
    """
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help"}:
        print(HELP_TEXT)
        return 0

    command = sys.argv[1]
    session_start = command in _SESSION_START_COMMANDS
    additional_context = ""
    # SessionStart はどの失敗経路でも exit 0 を維持する。フックが非 0 を返すと
    # セッション全体がエラー扱いになるため、`Returns:` に書いた契約はこの 1 本の
    # 値で表現し、失敗を捕まえる 4 箇所すべてがこれを使う。以前は
    # `if not session_start` を持つのが設定ロード失敗の 1 箇所だけで、
    # CommandError 経路と実行時例外経路は無条件に 1 を立てていた（＝契約が
    # 3 箇所で破れていた）。片方だけ直すと非対称が残るので 1 本に集約する。
    #
    # 失敗しても沈黙はさせない。stderr への出力はどの経路でも行う —— 黙って
    # 抜けると記憶注入が失われたこと自体に気づけないため。
    failure_exit_code = 0 if session_start else 1
    exit_code = 0
    try:
        args = _parse_args_and_stdin(command, sys.argv[2:])
        _check_positional_arity(command, args)
        # learn --status には専用の説明があるため、一般的な未対応オプション
        # チェックより先に評価する。
        _check_learn_options(command, args)
        _check_supported_options(command, args)
        settings = _load_settings_or_raise()
    except CommandError as e:
        print(str(e), file=sys.stderr)
        exit_code = failure_exit_code
    except Exception as e:
        print(f"設定/ログ初期化失敗: {e}", file=sys.stderr)
        exit_code = failure_exit_code
    else:
        try:
            if session_start:
                additional_context = _run_session_start_command(command, settings, args) or ""
            else:
                exit_code = _run_normal_command(command, settings, args)
        except CommandError as e:
            print(str(e), file=sys.stderr)
            exit_code = failure_exit_code
        except Exception as e:
            log.error("コマンド %s 失敗: %s", command, e)
            print(f"コマンド {command} 失敗: {e}", file=sys.stderr)
            exit_code = failure_exit_code
    finally:
        if session_start:
            print_session_start_output(additional_context)

    return exit_code


# --- DB の作り直し ---


def _handle_init(settings: Settings, _args: CommandArgs) -> None:
    """init コマンド: 既存 DB を削除して空の DB を作り直す。

    通常運用で DB を作るのは ``Database`` のコンストラクタ（親ディレクトリ作成 +
    ``_init_schema``）であり、``context`` が毎セッション走るため初期化専用の
    コマンドは要らない。本コマンドは中身を捨てて作り直す手動操作専用。

    Args:
        settings: mem 設定。
        _args: 未使用。他コマンドとハンドラ署名を揃える。
    """
    _remove_db_artifacts(settings.db_path)
    with Database(settings.db_path):
        pass
    # WAL モードの新規接続が残す -wal/-shm を除去し、再作成後の
    # データディレクトリを pristine に保つ。close 時の checkpoint で
    # データは mem.db へ反映済みのため安全。SQLite ビルドにより
    # close 時に自動削除されない環境があるため明示削除する。
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


# --- 知識 CRUD の補助 ---


def _reject_bad_input(error: KnowledgeInputError) -> CommandError:
    """入力検証エラーを CLI の想定内エラーへ包み直す。

    ``knowledge_input`` は CLI 非依存のため ``KnowledgeInputError`` を送出する。
    main() が捕捉するのは ``CommandError`` だけなので、境界でここを通す。

    Args:
        error: ``knowledge_input`` が送出した検証エラー。

    Returns:
        同じメッセージを持つ CommandError。
    """
    return CommandError(str(error))


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


def _require_key(args: CommandArgs) -> str:
    """位置引数から key を取り出す。

    呼び出し元（show/promote/forget）は dispatch 層の `_check_positional_arity`
    （L-01 対応）を経ているため、``args.positionals`` は必ずちょうど 1 件。

    Args:
        args: コマンド引数。

    Returns:
        指定された key。

    Raises:
        例外は発生しません。
    """
    return args.positionals[0]


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
    kept: list[str] = []
    used = 0
    for index, line in enumerate(lines):
        cost = len(line) + 1
        if used + cost > budget:
            return [*kept, f"… +{len(lines) - index} 件"]
        kept.append(line)
        used += cost
    return kept


def _summary_line(row: Knowledge) -> str:
    """list / search の 1 行（body なし）。"""
    return f"- [{row.kind}] {row.title} ({row.key})"


def _session_uid(payload: dict[str, Any]) -> str:
    """フック JSON から session_id を取り出す。無ければ空文字。"""
    return str(payload.get("session_id") or "").strip()


# --- 知識 CRUD ---


def _handle_learn(settings: Settings, args: CommandArgs) -> None:
    """learn コマンド: stdin の JSON から知識カードを 1 件登録する。

    既存の key と衝突した場合は ``upsert_knowledge`` が更新に落とす。ただし
    既存行が ``status='active'`` のときは更新せず usage error にする（H-3 対応。
    後述）。出力は ``learned: <key>`` の 1 行のみ。``source``/``status`` は常に
    ``agent``/``pending`` に固定される（H-01 対応。caller が JSON の
    ``source``/``status`` を書いても authority として扱わない）。有効化
    （``status='active'``）は ``promote <key>`` による人間承認のみ。

    **active カードは learn 経路から不変にする**（H-3）。以前は ``upsert`` の
    ``ON CONFLICT`` が ``status = excluded.status`` で無条件に上書きしていたため、
    人間が ``promote`` したカードと同じ key へ再 learn すると、learn が常に
    ``pending`` を書く（H-01）都合で **人間の承認が黙って取り消され**、以後
    SessionStart へ注入されなくなっていた。しかも本文まで差し替わるため、
    「人間が承認済み」かつ「再発するほど重要」という最も価値の高いカードだけが
    狙い撃ちで注入対象から外れる。

    拒否（案 a）を選び、既存 status を維持する更新（案 b）は採らない。案 b は
    「agent が active カードの本文を書き換えられる」＝人間が承認した内容と違う
    ものが注入され続ける、という H-01 と同じ穴を別の口で開けるため。案 a なら
    「active カードは agent の書込みに対して不変」という 1 行の不変条件になり、
    H-01 の設計思想（agent 由来の書込みは自動で有効化されない）と整合する。

    ``archived`` と ``pending`` は従来どおり更新できる。``archived`` への再 learn は
    ``pending`` へ戻すだけで注入はされず、人間が再度 ``promote`` を通す余地を
    残す方が「人間が決める」方針に合う。

    Args:
        settings: mem 設定。
        args: コマンド引数と stdin JSON。

    Raises:
        CommandError: title 欠落や列挙値・数値の不正がある場合、
            ``source``/``status`` を明示指定した場合、または同じ key の
            既存カードが既に ``active`` の場合。
    """
    try:
        draft = parse_knowledge_payload(args.stdin_data)
    except KnowledgeInputError as e:
        raise _reject_bad_input(e) from e

    with Database(settings.db_path) as db:
        repo_id = resolve_repo(None, db).id if draft.scope == "repo" else None
        existing = db.get_knowledge_by_key(draft.key, repo_id)
        if existing is not None and existing.status == "active":
            raise CommandError(_ACTIVE_CARD_IS_IMMUTABLE_MESSAGE.format(key=draft.key))
        db.upsert_knowledge(draft.to_knowledge(repo_id))
    print(f"learned: {draft.key}")


def _collect_list_rows(
    settings: Settings,
    args: CommandArgs,
    *,
    default_limit: int = LIST_DEFAULT_LIMIT,
    rank: _Ranker | None = None,
) -> list[Knowledge]:
    """``list`` / ``search`` の対象行を DB から集めて整列する。

    既定は cwd の repo スコープと global スコープの両方。``--global`` /
    ``--repo`` でどちらか一方に絞る。

    Args:
        settings: mem 設定。
        args: コマンド引数。
        default_limit: ``--limit`` 未指定時の件数。
        rank: 各行のスコアを返す関数。None なら更新の新しい順に並べる。
            指定した場合はスコアの高い順（同点は更新の新しい順）に並べ、
            スコアが 0 以下の行は落とす。

    Returns:
        整列して ``--limit`` 件で切った Knowledge のリスト。

    Raises:
        CommandError: 列挙値・件数が不正な場合。
    """
    # --global と --repo の排他は dispatch 層の `_check_supported_options` が
    # 全 subcommand に対して検証する。
    only_global = "--global" in args.flags
    only_repo = "--repo" in args.flags

    try:
        status = validate_choice("status", args.values.get("--status", "active"), STATUSES)
        kind = args.values.get("--kind")
        if kind is not None:
            validate_choice("kind", kind, KINDS)
    except KnowledgeInputError as e:
        raise _reject_bad_input(e) from e
    limit = _coerce_limit(args.values.get("--limit"), default_limit)

    rows = _load_scoped_knowledge(
        settings, include_repo=not only_global, include_global=not only_repo, status=status
    )
    if kind is not None:
        rows = [row for row in rows if row.kind == kind]
    return _take_rows(rows, rank=rank, limit=limit)


def _load_scoped_knowledge(
    settings: Settings,
    *,
    include_repo: bool,
    include_global: bool,
    status: str,
) -> list[Knowledge]:
    """指定スコープの知識カードを DB から集める。

    Args:
        settings: mem 設定。
        include_repo: cwd の repo スコープを含めるなら True。
        include_global: global スコープを含めるなら True。
        status: 絞り込む status。

    Returns:
        条件に一致する Knowledge のリスト。
    """
    rows: list[Knowledge] = []
    with Database(settings.db_path) as db:
        if include_repo:
            repo = resolve_repo(None, db)
            rows.extend(db.list_knowledge(scope="repo", repo_id=repo.id, status=status))
        if include_global:
            rows.extend(db.list_knowledge(scope="global", status=status))
    return rows


def _take_rows(rows: list[Knowledge], *, rank: _Ranker | None, limit: int) -> list[Knowledge]:
    """知識カードを更新順またはスコア順に並べて *limit* 件返す。

    Args:
        rows: 整列前の知識カード。
        rank: 各行のスコアを返す関数。None なら更新の新しい順。
        limit: 返す件数の上限。

    Returns:
        整列して *limit* 件で切った Knowledge のリスト。
    """
    if rank is None:
        rows.sort(key=lambda row: (row.updated_at, row.id or 0), reverse=True)
        return rows[:limit]

    hits = [(rank(row), row) for row in rows]
    hits = [hit for hit in hits if hit[0] > 0.0]
    hits.sort(key=lambda hit: (hit[0], hit[1].updated_at, hit[1].id or 0), reverse=True)
    return [row for _score, row in hits[:limit]]


def _emit_rows(rows: list[Knowledge], args: CommandArgs, budget: int) -> None:
    """知識カード群を 1 件 1 行で出力する。

    ``body`` は決して出さない。0 件なら 1 文字も出さない（「見つかりません」も
    出さず、エージェントの空振りコストを 0 トークンに落とす）。

    Args:
        rows: 出力する知識カード。
        args: コマンド引数。``--json`` の有無だけを見る。
        budget: 素テキスト出力に使ってよい文字数の上限。
    """
    if not rows:
        return

    if "--json" in args.flags:
        payload = [
            {"key": row.key, "scope": row.scope, "kind": row.kind, "title": row.title} for row in rows
        ]
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return

    for line in _fit_budget([_summary_line(row) for row in rows], budget):
        print(line)


def _handle_list(settings: Settings, args: CommandArgs) -> None:
    """list コマンド: 知識カードの一覧を 1 件 1 行で出す。

    ``body`` は出さず ``- [kind] title (key)`` のみ。0 件なら無出力。
    ``--json`` 指定時のみ機械処理向けの JSON 1 行を出す。

    Args:
        settings: mem 設定。
        args: コマンド引数。
    """
    _emit_rows(_collect_list_rows(settings, args), args, LIST_CHAR_BUDGET)


def _query_terms(query: str) -> tuple[str, ...]:
    """検索クエリを小文字の検索語へ分解する。

    空白区切りの語を返す。語が 2 つ以上に割れた場合は、空白を取り除いた
    クエリ全体も検索語として足す。日本語のように空白で語が割れない言語では
    split が 1 語（= クエリ全体）を返すため、そのままクエリ全体の部分一致に
    なり、``型 ヒント`` のように利用者が空白を入れたクエリでも本文の
    ``型ヒント`` を拾える。

    Args:
        query: 利用者が渡したクエリ文字列。

    Returns:
        重複を除いた検索語のタプル。

    Raises:
        CommandError: 空白のみで検索語が 1 つも取れない場合。
    """
    terms = [term.lower() for term in query.split()]
    if not terms:
        raise CommandError("search: 検索クエリを指定してください")
    if len(terms) > 1:
        terms.append("".join(terms))
    return tuple(dict.fromkeys(terms))


def _match_score(row: Knowledge, terms: tuple[str, ...]) -> float:
    """検索語が知識カードのどの項目に当たったかを重み付きで合算する。

    項目ごとの重みは ``_SEARCH_FIELD_WEIGHTS``。1 語が複数項目に当たれば
    その分だけ加算し、語ごとの合算で「何語をカバーしたか」も自然に効く。

    Args:
        row: 採点対象の知識カード。
        terms: 小文字化済みの検索語。

    Returns:
        一致の重み合計。1 語も当たらなければ 0.0。
    """
    haystacks = {
        "title": row.title.lower(),
        "key": row.key.lower(),
        "domain": (row.domain or "").lower(),
        "body": row.body.lower(),
    }
    score = 0.0
    for term in terms:
        for field_name, weight in _SEARCH_FIELD_WEIGHTS:
            if term in haystacks[field_name]:
                score += weight
    return score


def _scaled_factor(value: float) -> float:
    """0.0〜1.0 を ``_SEARCH_MULTIPLIER_FLOOR``〜1.0 へ線形写像する。"""
    return _SEARCH_MULTIPLIER_FLOOR + (1.0 - _SEARCH_MULTIPLIER_FLOOR) * value


def _recency_factor(updated_at: str, now: datetime) -> float:
    """更新時刻の新しさを ``_SEARCH_MULTIPLIER_FLOOR``〜1.0 の係数へ写す。

    Args:
        updated_at: 知識カードの ISO8601 更新時刻。
        now: 現在時刻（tz-aware）。

    Returns:
        新しいほど 1.0 に近く、古いほど下限へ漸近する係数。
    """
    age_days = max((now - datetime.fromisoformat(updated_at)).total_seconds(), 0.0) / 86400.0
    decay = 1.0 / (1.0 + age_days / _SEARCH_RECENCY_SCALE_DAYS)
    return _scaled_factor(decay)


def _score_knowledge(row: Knowledge, terms: tuple[str, ...], now: datetime) -> float:
    """知識カード 1 件の検索スコアを返す。

    一致の重み合計に、確度と新しさの係数（いずれも下限
    ``_SEARCH_MULTIPLIER_FLOOR``・上限 1.0）を掛ける。係数に下限があるので、
    一度でも一致した行のスコアは必ず正になり「ヒットなし」と混ざらない。

    Args:
        row: 採点対象の知識カード。
        terms: 小文字化済みの検索語。
        now: 現在時刻（tz-aware）。

    Returns:
        検索スコア。1 語も当たらなければ 0.0。
    """
    matched = _match_score(row, terms)
    if matched == 0.0:
        return 0.0
    return matched * _scaled_factor(row.confidence) * _recency_factor(row.updated_at, now)


def _handle_search(settings: Settings, args: CommandArgs) -> None:
    """search コマンド: クエリに一致する知識カードを 1 件 1 行で出す。

    全件を DB からロードして Python 側で採点する（インデックスは持たない）。
    ``body`` はスコア計算にだけ使い、出力には一切出さない。本文を読みたい
    場合は上位の key を 1 件だけ ``show`` に渡す二段構えにする。

    Args:
        settings: mem 設定。
        args: コマンド引数。位置引数の連結が検索クエリ。

    Raises:
        CommandError: 検索クエリが空、またはオプション値が不正な場合。
    """
    terms = _query_terms(" ".join(args.positionals))
    now = datetime.now(UTC)
    rows = _collect_list_rows(
        settings,
        args,
        default_limit=SEARCH_DEFAULT_LIMIT,
        rank=lambda row: _score_knowledge(row, terms, now),
    )
    _emit_rows(rows, args, SEARCH_CHAR_BUDGET)


def _handle_show(settings: Settings, args: CommandArgs) -> None:
    """show コマンド: 知識カード 1 件を全項目表示する。

    ``body`` を返す唯一の口。

    Args:
        settings: mem 設定。
        args: コマンド引数。

    Raises:
        CommandError: key の指定が無い、または該当が無い場合。
    """
    key = _require_key(args)
    with Database(settings.db_path) as db:
        found = _find_knowledge(db, key)

    for label, value in (
        ("key", found.key),
        ("scope", found.scope),
        ("repo", found.repo_id or "-"),
        ("kind", found.kind),
        ("title", found.title),
        ("body", found.body or "-"),
        ("domain", found.domain or "-"),
        ("confidence", f"{found.confidence:.2f}"),
        ("status", found.status),
        ("source", found.source),
        ("updated_at", found.updated_at),
    ):
        print(f"{label}: {value}")


def _handle_promote(settings: Settings, args: CommandArgs) -> None:
    """promote コマンド: 知識カードの status を active にする。

    昇格は以後の全セッションへ自動注入される状態への遷移なので、誰が・いつ・
    どのカードを昇格させたかをログへ残す。これは境界ではなく事後追跡である。
    docs/adr/untrusted-input-prompt-boundary.md のとおり、promote が人間によって実行されたことを技術的に強制する
    手段は無く（同一 UID から mem.db を直接更新できる以上、CLI をいくら固めても
    保証にならない）、Bash を持つ agent からも到達できる。

    Args:
        settings: mem 設定。
        args: コマンド引数。

    Raises:
        CommandError: key の指定が無い、または該当が無い場合。
    """
    key = _require_key(args)
    # actor は DB 更新の前に確定させる。更新後に解決していた頃は、識別子の
    # 取得に失敗すると「カードは active になったのに監査ログが残らず非 0 終了」
    # という部分成功になりえた（P1-005）。
    actor = actor_identity()
    with Database(settings.db_path) as db:
        found = _find_knowledge(db, key)
        db.set_knowledge_status(found.id, "active")
    log.info(
        "promote: key=%s scope=%s source=%s previous_status=%s actor=%s",
        found.key,
        found.scope,
        found.source,
        found.status,
        actor,
    )
    print(f"promoted: {key}")


def _handle_forget(settings: Settings, args: CommandArgs) -> None:
    """forget コマンド: 知識カードを archived にする。

    ``--superseded-by KEY`` を渡すと、その key の ``knowledge.id`` を
    ``superseded_by`` に記録して置き換え関係を残す。

    Args:
        settings: mem 設定。
        args: コマンド引数。

    Raises:
        CommandError: key の指定が無い、または該当が無い場合。
    """
    key = _require_key(args)
    successor_key = args.values.get("--superseded-by")
    with Database(settings.db_path) as db:
        found = _find_knowledge(db, key)
        found.status = "archived"
        found.updated_at = utc_now_iso()
        if successor_key is not None:
            found.superseded_by = _find_knowledge(db, successor_key).id
        db.upsert_knowledge(found)
    suffix = f" (superseded by {successor_key})" if successor_key is not None else ""
    print(f"forgot: {key}{suffix}")


# --- SessionStart への知識注入 ---


def _one_line(text: str) -> str:
    """連続する空白と改行を 1 個の空白へ畳み、1 行にする。

    注入枠の中で「1 行」として描かれる文字列は、改行が残ると 1 件が複数行へ割れて
    偽の項目を増やせる。描画側で畳むことで、DB に既に入っている行も守る。

    Args:
        text: 畳む対象の文字列

    Returns:
        改行を含まない 1 行
    """
    return " ".join(text.split())


#: 引き継ぎ本文の行頭で無害化する構造マーカー。注入枠は `## 見出し` と
#: `- [kind] タイトル` で組み立てられているため、引き継ぎ側がこの 2 つを行頭へ
#: 置けると、人間の承認（promote）を通った知識と見分けの付かない節や項目を
#: 捏造できる。引き継ぎは promote を通らないので、ここが唯一の防壁になる。
_HANDOFF_STRUCTURE_MARKERS = ("#", "- [")


def _neutralize_handoff_structure(text: str) -> str:
    """引き継ぎ本文が注入枠の構造を偽装するのを防ぐ。

    行頭の `#`（見出し）と `- [`（知識カード行）だけを対象にし、全角へ寄せずに
    空白 1 個で字下げする。本文は散文なので改行と通常の箇条書き `- ` は残す
    （正当な引き継ぎがそれらを使う）。

    Args:
        text: 引き継ぎ本文

    Returns:
        行頭の構造マーカーを無害化した本文
    """
    lines = []
    for line in text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith(_HANDOFF_STRUCTURE_MARKERS):
            lines.append(f" {stripped}")
        else:
            lines.append(line)
    return "\n".join(lines)


def _format_injected_item(row: Knowledge) -> str:
    """知識カード 1 件を注入用の 1 行へ整形する。

    既定は ``- [kind] title`` のみ。``body`` を足しても 1 行が
    ``CONTEXT_ITEM_CHAR_LIMIT`` 文字に収まる場合だけ ``—`` で続ける。
    長い body を抱えたカードが 1 件で節の予算を食い潰すのを防ぐ。
    title / body は ``<claq-memory>`` 等の信頼境界タグを偽装できる
    ため、注入前に ``strip_tags`` で無害化する。

    Args:
        row: 注入対象の知識カード。

    Returns:
        整形済みの 1 行（改行を含まない）。
    """
    # 入口で畳んでいても、既に DB にある行や将来の別経路を信用しない。返り値が
    # 「改行を含まない 1 行」であることは、この関数が自分で保証する。
    title = _one_line(strip_tags(row.title))
    head = f"- [{row.kind}] {title}"
    body = _one_line(strip_tags(row.body.strip()))
    if not body:
        return head
    combined = f"{head} — {body}"
    return combined if len(combined) <= CONTEXT_ITEM_CHAR_LIMIT else head


def _knowledge_section(heading: str, rows: list[Knowledge], budget: int) -> str:
    """知識カード群を見出し付きの 1 節へ組み立てる。

    並び順は ``confidence DESC, updated_at DESC``（同値は ``id`` の新しい順）。
    0 件なら見出しごと出さない。

    Args:
        heading: 節の見出し（``## `` は本関数が付ける）。
        rows: 節に載せる知識カード。
        budget: 明細行に使ってよい文字数の上限。

    Returns:
        見出しと明細からなる節の文字列。*rows* が空なら空文字列。
    """
    if not rows:
        return ""
    ordered = sorted(rows, key=lambda row: (row.confidence, row.updated_at, row.id or 0), reverse=True)
    lines = _fit_budget([_format_injected_item(row) for row in ordered], budget)
    return "\n".join([f"## {heading}", *lines])


def _handoff_section(db: Database, repo_id: str) -> str:
    """直近セッションの引き継ぎを ``前回の続き`` 節へ組み立てる。

    handoff 本文は ``<claq-memory>`` 等の信頼境界タグを偽装できるため、
    注入前に ``strip_tags`` で無害化する。

    Args:
        db: 参照する mem データベース。
        repo_id: 対象リポジトリの ``repos.id``。

    Returns:
        見出しと引き継ぎ本文からなる節。引き継ぎが無ければ空文字列。
    """
    session = db.get_latest_session(repo_id)
    if session is None:
        return ""
    handoff = _neutralize_handoff_structure(strip_tags(session.handoff.strip()))
    if not handoff:
        return ""
    if len(handoff) > CONTEXT_HANDOFF_CHAR_BUDGET:
        handoff = handoff[: CONTEXT_HANDOFF_CHAR_BUDGET - 1] + "…"
    stamp = datetime.fromisoformat(session.ended_at or session.started_at).strftime("%Y-%m-%d %H:%M")
    return f"## 前回の続き ({stamp})\n{handoff}"


def _bg_failure_section() -> str:
    """前回セッションの detach 起動（``--bg``）失敗痕跡があれば 1 行の節にする（§6.2 対応）。

    通知本文はログファイルの末尾行そのままであり、``~/.claq/logs/`` を守る
    フックは無い。1 度書き込まれた行は次セッション以降の SessionStart で
    注入され続けるため、隣の handoff 節（``_handoff_section`` →
    ``handoff._summarize_transcript``）と同じ
    ``strip_tags(normalize_user_message(...))`` の合成を通してから注入する。
    通していなかった頃は ``</claq-memory> IMPORTANT: ...`` の 1 行をログへ
    追記するだけで注入枠のタグ境界を閉じ、以後の本文を指示として提示できた
    （実測）。``strip_tags`` だけでは足場タグ（``<system-reminder>`` 等）が
    素通りするため、両方を通す。

    Returns:
        痕跡があれば見出し付き 1 行の節。無ければ空文字列。

    Raises:
        例外は発生しません。
    """
    notice = strip_tags(normalize_user_message(recent_bg_failure_notice())).strip()
    if not notice:
        return ""
    return f"## 前回セッションの通知\n{notice}"


def _shell_bootstrap_section() -> str:
    """`$HOME/.claq/env.sh` が使えない OS で、その読み替え方を 1 節で示す。

    md（agents/commands/skills）の bash fence は
    ``. "$HOME/.claq/env.sh"`` → ``claq_run <module> ...`` という形を取る。
    この resolver は祖先 PID と起動時刻の照合（docs/adr/plugin-root-resolution.md）に依存し、
    Windows では ``ps`` が無いため原理的に解決できない
    （`env_pointer.ancestor_pointers_supported`）。md を全部 2 通り書くと
    プロンプトが倍になるため、代わりに「その場で解決済みの plugin root」を
    SessionStart で 1 度だけ渡す。plugin root は今まさにこの hook を起動した
    host 自身のものなので、ポインタ方式より曖昧さが無い。

    出力トークン最小化の原則に従い、必要な OS でのみ、必要な 2 行だけを出す。

    Args:
        なし

    Returns:
        読み替え方の節。POSIX では空文字列。

    Raises:
        例外は発生しません。
    """
    if ancestor_pointers_supported():
        return ""
    wrapper = get_plugin_root() / "runtime" / "claq-hook.cmd"
    return (
        "## claq の呼び出し\n"
        'この OS では `. "$HOME/.claq/env.sh"` は解決できない。md 中の '
        f'`claq_run <module> ...` は `"{wrapper}" <module> ...`'
        "（PowerShell では先頭に `&`）へ、"
        f'`claq_mem_learn ...` は `CLAQ_LEARN_KIND=... CLAQ_LEARN_TITLE=... "{wrapper}" '
        f'claq.mem.learn_payload` の JSON を `"{wrapper}" claq.mem.cli learn` の '
        "stdin へ渡す形へ読み替える。"
    )


def _record_session(db: Database, repo: Repo, payload: dict[str, Any]) -> None:
    """ハーネスの session_id で ``sessions`` に開始行を作る。

    ここが ``sessions`` 行を作る唯一の場所。SessionEnd 側の handoff 記録は
    この行の更新になる。session_id を渡さないハーネスでは行を作らない
    （``session_uid`` は NOT NULL UNIQUE のため空文字で埋めると衝突する）。

    Args:
        db: 書き込み先の mem データベース。
        repo: 解決済みのリポジトリ。
        payload: フックが stdin へ渡した JSON。
    """
    session_uid = _session_uid(payload)
    if not session_uid:
        return
    db.start_session(Session(session_uid=session_uid, repo_id=repo.id))


def _build_context(settings: Settings, args: CommandArgs) -> str:
    """注入する ``<claq-memory>`` ブロックを組み立てる。

    共通知識（``scope='global'``）・リポジトリ知識（``scope='repo'``）・
    前回の引き継ぎの 3 節を、いずれも ``status='active'`` に限って集める
    （``pending`` は人間の昇格レビュー前の下書きでノイズになるため注入しない）。
    加えて、前回セッションの detach 起動（``--bg``）に失敗痕跡があれば
    4 節目として 1 行だけ追加する（§6.2 対応、`_bg_failure_section`）。

    Args:
        settings: mem 設定。
        args: コマンド引数とフックの stdin JSON。

    Returns:
        注入する文字列。3 節すべて空なら空文字列（注入をゼロにする）。
    """
    payload = args.stdin_data
    with Database(settings.db_path) as db:
        repo = resolve_repo(optional_str(payload.get("cwd")), db)
        _record_session(db, repo, payload)
        sections = [
            _knowledge_section(
                "共通知識",
                db.list_knowledge(scope="global", status="active"),
                CONTEXT_GLOBAL_CHAR_BUDGET,
            ),
            _knowledge_section(
                repo.id,
                db.list_knowledge(scope="repo", repo_id=repo.id, status="active"),
                CONTEXT_REPO_CHAR_BUDGET,
            ),
            _handoff_section(db, repo.id),
            _bg_failure_section(),
            _shell_bootstrap_section(),
        ]

    body = "\n\n".join(section for section in sections if section)
    if not body:
        return ""
    return f"<claq-memory>\n{body}\n</claq-memory>"


def _handle_context(settings: Settings, args: CommandArgs) -> str:
    """context コマンド: SessionStart へ蓄積済みの知識を注入する。

    毎セッション必ず走る唯一の注入口。フックを壊さないため失敗は握り潰し、
    注入なし（空文字列）に倒す。

    Args:
        settings: mem 設定。
        args: コマンド引数とフックの stdin JSON。

    Returns:
        注入する追加コンテキスト。組み立てに失敗した場合は空文字列。
    """
    try:
        return _build_context(settings, args)
    except Exception as e:
        log.warning("context 失敗: %s", e)
        return ""


# --- SessionEnd の引き継ぎ記録 ---


def _record_handoff(settings: Settings, payload: dict[str, Any]) -> None:
    """引き継ぎ本文を組み立てて ``sessions`` へ書き込む。

    既存行があれば更新し、無ければ作る（SessionStart が走らなかった
    ハーネスでも引き継ぎは残すべきため）。行を新設する場合、開始時刻は
    不明なので ``ended_at`` と同値にする。``session_id`` が無い場合は
    何も書かない（``session_uid`` は NOT NULL UNIQUE で、空文字で埋めると
    全セッションが 1 行に衝突する）。

    Args:
        settings: mem 設定。
        payload: SessionEnd フックが stdin へ渡した JSON。
    """
    session_uid = _session_uid(payload)
    if not session_uid:
        return

    handoff = build_handoff(payload)
    if not handoff:
        return

    now = utc_now_iso()
    with Database(settings.db_path) as db:
        if db.finish_session(session_uid, handoff, now):
            return
        repo = resolve_repo(optional_str(payload.get("cwd")), db)
        db.start_session(
            Session(
                session_uid=session_uid,
                repo_id=repo.id,
                handoff=handoff,
                started_at=now,
                ended_at=now,
            )
        )


def _handle_handoff(settings: Settings, args: CommandArgs) -> None:
    """handoff コマンド: SessionEnd の引き継ぎを ``sessions`` へ記録する。

    フック経路のため失敗は握り潰して終了コード 0 を保つ。出力は無い。
    外部 I/O はトランスクリプト走査（``TRANSCRIPT_TIMEOUT_SEC`` で打ち切り）と
    ローカル SQLite（``sqlite3`` の既定 busy timeout 5 秒で打ち切り）のみで、
    いずれも上限時間を持つ。

    Args:
        settings: mem 設定。
        args: コマンド引数とフックの stdin JSON。
    """
    try:
        _record_handoff(settings, args.stdin_data)
    except Exception as e:
        log.warning("handoff 失敗: %s", e)


_COMMAND_HANDLERS: dict[str, _CommandHandler] = {
    "init": _handle_init,
    "context": _handle_context,
    "handoff": _handle_handoff,
    "learn": _handle_learn,
    "list": _handle_list,
    "search": _handle_search,
    "show": _handle_show,
    "promote": _handle_promote,
    "forget": _handle_forget,
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
