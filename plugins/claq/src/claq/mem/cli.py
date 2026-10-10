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
オーダーに収まるため、全件をロードして Python 側でスコアリングするほうが
安く、DB も肥大しない。
"""

from __future__ import annotations

import dataclasses
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
from claq.mem.models import Knowledge, Session, utc_now_iso
from claq.mem.redaction import redact
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

_FILTER_OPTIONS = frozenset({"--global", "--repo", "--status", "--kind", "--limit", "--json"})

# subcommand ごとの（位置引数の個数, 受理するオプション）。個数 None は可変長
# （``search`` は複数の位置引数を検索語として連結する）。オプションの対応表を
# 持たないと `promote <key> --global` のような未対応 flag が黙って通り、指定と
# 異なるカード（repo 優先探索の結果）を操作してしまう。
_COMMAND_SPECS: dict[str, tuple[int | None, frozenset[str]]] = {
    "init": (0, frozenset()),
    "context": (0, frozenset()),
    "handoff": (0, frozenset()),
    "learn": (0, frozenset()),
    "list": (0, _FILTER_OPTIONS),
    "search": (None, _FILTER_OPTIONS),
    "show": (1, frozenset()),
    "promote": (1, frozenset()),
    "forget": (1, frozenset({"--superseded-by"})),
}

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


# 人間が promote 済みのカードへ再 learn したときの拒否理由。
# 「失敗」ではなく「その知識は既に有効なので何もしなくてよい」と読めるよう、
# 次に取るべき行動まで書く。ここを曖昧にすると、agent が別 key で作り直して
# 重複カードを量産する（この経路が防ごうとしている事故そのもの）。
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

    ``context`` は launcher の in-process 実行で host の実 pipe を直接読むため、
    書き手が pipe を閉じないまま止まると ``sys.stdin.read()`` は EOF まで
    無期限ブロックする。そのため到着・読み取り全体の両方に上限を持つ
    ``read_raw_stdin()`` を使う。

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
    return dataclasses.replace(args, stdin_data=_read_stdin_json() if command in _STDIN_COMMANDS else {})


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
    arity = _COMMAND_SPECS[command][0] if command in _COMMAND_SPECS else None
    if arity == 0 and args.positionals:
        raise CommandError(f"{command} は位置引数を取りません: {' '.join(args.positionals)!r}")
    if arity == 1:
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
    if command not in _COMMAND_SPECS:
        return
    used = set(args.flags) | set(args.values)
    unsupported = sorted(used - _COMMAND_SPECS[command][1])
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


def main() -> int:
    """CLI エントリポイント。argv からコマンドを解決して実行し、終了コードを返す。

    Returns:
        終了コード。未知のコマンドは 2。SessionStart コマンドは失敗しても
        フックエラーを避けるため 0 を維持する。
    """
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help"}:
        print(HELP_TEXT)
        return 0

    command = sys.argv[1]
    session_start = command in _SESSION_START_COMMANDS
    additional_context = ""
    # SessionStart はどの失敗経路でも exit 0 を維持する（非 0 だとセッション全体が
    # エラー扱いになる）。失敗を捕まえる全経路がこの 1 本の値を使い、片方だけ
    # 直したときの非対称を防ぐ。stderr への出力はどの経路でも行う —— 黙って
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
        handler = _COMMAND_HANDLERS.get(command)
        try:
            if session_start:
                additional_context = handler(settings, args) or ""
            elif handler is None:
                log.error("不明なコマンド: %s", command)
                exit_code = 2
            else:
                handler(settings, args)
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


# --- 知識 CRUD ---


def _handle_learn(settings: Settings, args: CommandArgs) -> None:
    """learn コマンド: stdin の JSON から知識カードを 1 件登録する。

    出力は ``learned: <key>`` の 1 行のみ。``source``/``status`` は常に
    ``agent``/``pending`` に固定され、有効化は ``promote <key>`` のみ。

    **active カードは learn 経路から不変にする**。``upsert`` は status を無条件に
    上書きするため、そのまま通すと人間が ``promote`` したカードへの再 learn で
    承認が黙って取り消され、本文まで差し替わる。既存 status を維持して本文だけ
    更新する案は「agent が承認済みの内容を書き換えられる」穴になるため採らず、
    拒否する。``archived`` / ``pending`` は更新でき、``archived`` は ``pending``
    へ戻るだけで注入はされない。

    Args:
        settings: mem 設定。
        args: コマンド引数と stdin JSON。

    Raises:
        CommandError: payload が不正な場合、または同じ key の既存カードが
            既に ``active`` の場合。
    """
    try:
        draft = parse_knowledge_payload(args.stdin_data)
    except KnowledgeInputError as e:
        raise CommandError(str(e)) from e

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
    ``--repo`` でどちらか一方に絞る（両者の排他は dispatch 層で検証済み）。

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
    try:
        status = validate_choice("status", args.values.get("--status", "active"), STATUSES)
        kind = args.values.get("--kind")
        if kind is not None:
            validate_choice("kind", kind, KINDS)
    except KnowledgeInputError as e:
        raise CommandError(str(e)) from e
    limit = _coerce_limit(args.values.get("--limit"), default_limit)

    rows: list[Knowledge] = []
    with Database(settings.db_path) as db:
        if "--global" not in args.flags:
            rows += db.list_knowledge(scope="repo", repo_id=resolve_repo(None, db).id, status=status)
        if "--repo" not in args.flags:
            rows += db.list_knowledge(scope="global", status=status)
    if kind is not None:
        rows = [row for row in rows if row.kind == kind]

    if rank is None:
        rows.sort(key=lambda row: (row.updated_at, row.id or 0), reverse=True)
        return rows[:limit]
    hits = [(score, row) for row in rows if (score := rank(row)) > 0.0]
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
        payload = [{"key": row.key, "scope": row.scope, "kind": row.kind, "title": row.title} for row in rows]
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return
    for line in _fit_budget([f"- [{row.kind}] {row.title} ({row.key})" for row in rows], budget):
        print(line)


def _handle_list(settings: Settings, args: CommandArgs) -> None:
    """list コマンド: 知識カードの一覧を ``- [kind] title (key)`` で 1 件 1 行に出す。

    Args:
        settings: mem 設定。
        args: コマンド引数。
    """
    _emit_rows(_collect_list_rows(settings, args), args, LIST_CHAR_BUDGET)


def _query_terms(query: str) -> tuple[str, ...]:
    """検索クエリを小文字の検索語へ分解する。

    語が 2 つ以上に割れた場合は、空白を取り除いたクエリ全体も検索語に足す。
    ``型 ヒント`` のように空白を入れたクエリでも本文の ``型ヒント`` を拾うため。

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

    1 語が複数項目に当たればその分だけ加算し、語ごとの合算で「何語を
    カバーしたか」も効く。

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
    return sum(
        (weight for term in terms for name, weight in _SEARCH_FIELD_WEIGHTS if term in haystacks[name]),
        0.0,
    )


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
    return _scaled_factor(1.0 / (1.0 + age_days / _SEARCH_RECENCY_SCALE_DAYS))


def _score_knowledge(row: Knowledge, terms: tuple[str, ...], now: datetime) -> float:
    """知識カード 1 件の検索スコアを返す。

    一致の重み合計に確度と新しさの係数を掛ける。係数に下限があるので、一度でも
    一致した行のスコアは必ず正になり「ヒットなし」と混ざらない。

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

    ``body`` はスコア計算にだけ使い、出力しない。本文は上位の key を ``show``
    に渡して読む二段構えにする。

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
    """show コマンド: 知識カード 1 件を全項目表示する。``body`` を返す唯一の口。

    Args:
        settings: mem 設定。
        args: コマンド引数。

    Raises:
        CommandError: 該当が無い場合。
    """
    with Database(settings.db_path) as db:
        found = _find_knowledge(db, args.positionals[0])
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
    どのカードを昇格させたかをログへ残す。これは境界ではなく事後追跡である
    （同一 UID から mem.db を直接更新できる以上、人間の実行を技術的に強制
    できず、Bash を持つ agent からも到達できる）。

    Args:
        settings: mem 設定。
        args: コマンド引数。

    Raises:
        CommandError: 該当が無い場合。
    """
    key = args.positionals[0]
    # actor は DB 更新の前に確定させる。更新後だと、識別子の取得失敗が
    # 「active になったのに監査ログが残らず非 0 終了」という部分成功になる。
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
        CommandError: 該当が無い場合。
    """
    key = args.positionals[0]
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

    行頭の `#`（見出し）と `- [`（知識カード行）だけを対象にし、空白 1 個で
    字下げする。正当な引き継ぎが使う改行と通常の箇条書き `- ` は残す。

    Args:
        text: 引き継ぎ本文

    Returns:
        行頭の構造マーカーを無害化した本文
    """
    return "\n".join(
        f" {line.lstrip()}" if line.lstrip().startswith(_HANDOFF_STRUCTURE_MARKERS) else line
        for line in text.split("\n")
    )


def _format_injected_item(row: Knowledge) -> str:
    """知識カード 1 件を注入用の 1 行へ整形する。

    既定は ``- [kind] title`` のみ。``body`` を足しても ``CONTEXT_ITEM_CHAR_LIMIT``
    文字に収まる場合だけ ``—`` で続け、長い body が節の予算を食い潰すのを防ぐ。
    title / body は信頼境界タグを偽装できるため ``strip_tags`` で無害化する。

    Args:
        row: 注入対象の知識カード。

    Returns:
        整形済みの 1 行（改行を含まない）。
    """
    # 入口で畳んでいても、既に DB にある行や将来の別経路を信用しない。返り値が
    # 「改行を含まない 1 行」であることは、この関数が自分で保証する。
    head = f"- [{row.kind}] {_one_line(strip_tags(row.title))}"
    body = _one_line(strip_tags(row.body.strip()))
    if not body:
        return head
    combined = f"{head} — {body}"
    return combined if len(combined) <= CONTEXT_ITEM_CHAR_LIMIT else head


def _knowledge_section(heading: str, rows: list[Knowledge], budget: int) -> str:
    """知識カード群を見出し付きの 1 節へ組み立てる。

    並び順は ``confidence DESC, updated_at DESC``（同値は ``id`` の新しい順）。

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
    return "\n".join([f"## {heading}", *_fit_budget([_format_injected_item(row) for row in ordered], budget)])


def _handoff_section(db: Database, repo_id: str) -> str:
    """直近セッションの引き継ぎを ``前回の続き`` 節へ組み立てる。

    handoff 本文は信頼境界タグを偽装できるため ``strip_tags`` で無害化する。

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
    """前回セッションの detach 起動（``--bg``）失敗痕跡があれば 1 行の節にする。

    通知本文はログファイルの末尾行そのままで、ログへ書かれた行は次セッション
    以降へ注入され続ける。``</claq-memory> IMPORTANT: ...`` を追記するだけで
    注入枠の境界を閉じられるため、handoff 節の ``_sanitize_freeform`` と同じ
    順序（``normalize_user_message`` → ``strip_tags`` → ``redact``）で無害化する。
    ``strip_tags`` だけでは足場タグが素通りし、``redact`` はログ中のシークレット
    を伏せる。タグで分断された秘密は ``strip_tags`` で再結合して初めて
    ``redact`` に一致するため、``redact`` を後に置く。

    Returns:
        痕跡があれば見出し付き 1 行の節。無ければ空文字列。
    """
    notice = redact(strip_tags(normalize_user_message(recent_bg_failure_notice()))).strip()
    if not notice:
        return ""
    return f"## 前回セッションの通知\n{notice}"


def _shell_bootstrap_section() -> str:
    """`$HOME/.claq/env.sh` が使えない OS で、その読み替え方を 1 節で示す。

    md の bash fence は ``. "$HOME/.claq/env.sh"`` → ``claq_run <module> ...``
    の形を取るが、この resolver は祖先 PID の照合に依存し、``ps`` の無い
    Windows では解決できない。md を全部 2 通り書くとプロンプトが倍になるため、
    この hook を起動した host 自身の plugin root を SessionStart で 1 度だけ渡す。

    Returns:
        読み替え方の節。POSIX では空文字列。
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


def _build_context(settings: Settings, args: CommandArgs) -> str:
    """注入する ``<claq-memory>`` ブロックを組み立てる。

    共通知識・リポジトリ知識・前回の引き継ぎを、いずれも ``status='active'`` に
    限って集める（``pending`` は昇格レビュー前の下書きでノイズになる）。加えて
    detach 起動の失敗通知と、Windows 向けの呼び出し方を節として足す。

    Args:
        settings: mem 設定。
        args: コマンド引数とフックの stdin JSON。

    Returns:
        注入する文字列。全節が空なら空文字列（注入をゼロにする）。
    """
    payload = args.stdin_data
    with Database(settings.db_path) as db:
        repo = resolve_repo(optional_str(payload.get("cwd")), db)
        # ここが sessions 行を作る唯一の場所で、SessionEnd の handoff はこの行を
        # 更新する。session_uid は NOT NULL UNIQUE なので空文字では作らない。
        if session_uid := _session_uid(payload):
            db.start_session(Session(session_uid=session_uid, repo_id=repo.id))
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

    既存行があれば更新し、無ければ作る（SessionStart が走らなかったハーネスでも
    引き継ぎは残すべきため）。新設時の開始時刻は不明なので ``ended_at`` と同値に
    する。``session_id`` が無ければ何も書かない（``session_uid`` は NOT NULL
    UNIQUE で、空文字で埋めると全セッションが 1 行に衝突する）。

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
            Session(session_uid=session_uid, repo_id=repo.id, handoff=handoff, started_at=now, ended_at=now)
        )


def _handle_handoff(settings: Settings, args: CommandArgs) -> None:
    """handoff コマンド: SessionEnd の引き継ぎを ``sessions`` へ記録する。

    フック経路のため失敗は握り潰して終了コード 0 を保つ。外部 I/O は
    トランスクリプト走査（``TRANSCRIPT_TIMEOUT_SEC``）とローカル SQLite
    （既定 busy timeout 5 秒）のみで、いずれも上限時間を持つ。

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
