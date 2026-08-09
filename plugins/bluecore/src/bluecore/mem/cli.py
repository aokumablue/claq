"""フックおよび人間・エージェントから呼び出される CLI エントリポイント。

提供するのは DB の初期化（``init`` / ``setup``）、知識 CRUD
（``learn`` / ``list`` / ``show`` / ``promote`` / ``forget``）、および
SessionStart への知識注入（``context``）。handoff / search は後続タスクで追加する。

設計原則は **出力トークンの最小化**。重い絞り込みは Python プロセス内で
完結させ、LLM のコンテキストへ返すのは最小限のテキストだけにする:

* ``body`` を返すのは ``show`` のみ。``list`` は ``title`` だけを 1 件 1 行で出す。
* ``list`` の既定件数は 20 件、出力は ``LIST_CHAR_BUDGET`` 文字で打ち切る。
* 0 件なら何も出力しない（「見つかりません」も出さない）。
* 既定出力は素のテキスト。``--json`` は機械処理用のオプトインに留める。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from bluecore.hooks.hook_common import print_session_start_output
from bluecore.lib.harness import detect_harness
from bluecore.mem.database import Database
from bluecore.mem.logger import get as _get_logger
from bluecore.mem.models import Knowledge, Repo, Session, utc_now_iso
from bluecore.mem.repo_identity import resolve_repo, slugify
from bluecore.mem.settings import (
    CONTEXT_GLOBAL_CHAR_BUDGET,
    CONTEXT_HANDOFF_CHAR_BUDGET,
    CONTEXT_ITEM_CHAR_LIMIT,
    CONTEXT_REPO_CHAR_BUDGET,
    Settings,
)

log = _get_logger("CLI")

# SessionStart フックで JSON 出力が必須なコマンドの集合。
# main() のフォールバック保証とエラー時の早期 return に使用する。
_SESSION_START_COMMANDS: frozenset[str] = frozenset({"setup", "context"})
# WAL モードの接続が残す sidecar ファイルの拡張子。
_DB_SIDECAR_SUFFIXES: tuple[str, ...] = ("-wal", "-shm", "-journal")

# knowledge の列挙値。DB の CHECK 制約に到達する前に CLI 側で弾く。
_KINDS: frozenset[str] = frozenset({"convention", "decision", "pitfall", "howto", "fact", "preference"})
_SCOPES: frozenset[str] = frozenset({"global", "repo"})
_STATUSES: frozenset[str] = frozenset({"active", "pending", "archived"})
_SOURCES: frozenset[str] = frozenset({"agent", "observer", "human"})

# 値を伴うオプションと、真偽のみのフラグ。
_VALUE_OPTIONS: frozenset[str] = frozenset({"--status", "--kind", "--limit", "--superseded-by"})
_BOOL_FLAGS: frozenset[str] = frozenset({"--global", "--repo", "--json"})

LIST_DEFAULT_LIMIT = 20
"""``list`` の既定表示件数。増やすには ``--limit`` の明示が要る。"""

LIST_CHAR_BUDGET = 1400
"""``list`` の出力予算（文字数）。400 トークン相当で打ち切る。"""

_ASCII_ALNUM_RE = re.compile(r"[A-Za-z0-9]")
_KEY_HASH_LENGTH = 8


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

    index = 0
    while index < len(argv):
        token = argv[index]
        if token in _VALUE_OPTIONS:
            index += 1
            if index >= len(argv):
                raise CommandError(f"オプション {token} に値がありません")
            values[token] = argv[index]
        elif token in _BOOL_FLAGS:
            flags.add(token)
        elif token.startswith("-"):
            raise CommandError(f"不明なオプション: {token}")
        else:
            positionals.append(token)
        index += 1

    return CommandArgs(positionals=tuple(positionals), flags=frozenset(flags), values=values)


def _read_stdin_json() -> dict[str, Any]:
    """stdin から JSON オブジェクトを読み取る。

    Returns:
        読み取った dict。stdin が tty・空・非 dict・不正 JSON なら空 dict。
    """
    if sys.stdin.isatty():
        return {}
    try:
        raw = sys.stdin.read()
    except OSError as e:
        log.warning("stdin 読み取り失敗: %s", e)
        return {}
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("stdin 読み取り失敗: %s", e)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_args_and_stdin(argv: list[str]) -> CommandArgs:
    """コマンド名より後ろの引数と stdin JSON を読み取る。

    Args:
        argv: コマンド名を除いた引数リスト。

    Returns:
        組み立てた CommandArgs。

    Raises:
        CommandError: オプションの解析に失敗した場合。
    """
    args = _parse_options(argv)
    return CommandArgs(
        positionals=args.positionals,
        flags=args.flags,
        values=args.values,
        stdin_data=_read_stdin_json(),
    )


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

    additional_context = ""
    # SESSION_START コマンドは設定ロード失敗でも exit_code=0 を維持する。
    # フックが非 0 を返すとセッション全体がエラー扱いになるため。
    exit_code = 0
    command = sys.argv[1]
    _silent = command in _SESSION_START_COMMANDS
    try:
        args = _parse_args_and_stdin(sys.argv[2:])
        settings = _load_settings_or_raise()
    except CommandError as e:
        print(str(e), file=sys.stderr)
        exit_code = 1
    except Exception as e:
        if not _silent:
            print(f"設定/ログ初期化失敗: {e}", file=sys.stderr)
            exit_code = 1
    else:
        try:
            if _silent:
                additional_context = _run_session_start_command(command, settings, args) or ""
            else:
                exit_code = _run_normal_command(command, settings, args)
        except CommandError as e:
            print(str(e), file=sys.stderr)
            exit_code = 1
        except Exception as e:
            log.error("コマンド %s 失敗: %s", command, e)
            print(f"コマンド {command} 失敗: {e}", file=sys.stderr)
            exit_code = 1
    finally:
        if _silent:
            print_session_start_output(additional_context)

    return exit_code


# --- DB 初期化 ---


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


# --- 知識 CRUD の補助 ---


def _validate_choice(name: str, value: str, allowed: frozenset[str]) -> str:
    """列挙値を検証して返す。

    Args:
        name: エラーメッセージに出す項目名。
        value: 検証したい値。
        allowed: 許可される値の集合。

    Returns:
        検証を通った *value*。

    Raises:
        CommandError: *value* が *allowed* に含まれない場合。
    """
    if value not in allowed:
        raise CommandError(f"{name} は {'/'.join(sorted(allowed))} のいずれか: {value!r}")
    return value


def _optional_str(value: Any) -> str | None:
    """空でない値だけを文字列にして返す。

    Args:
        value: JSON から得た任意の値。

    Returns:
        文字列化した値。None・空文字・空コンテナなら None。
    """
    return str(value) if value else None


def _coerce_confidence(value: Any) -> float:
    """confidence を 0.0〜1.0 の float に変換する。

    Args:
        value: JSON から得た確信度。None なら既定値 0.5。

    Returns:
        変換済みの確信度。

    Raises:
        CommandError: 数値に変換できない、または範囲外の場合。
    """
    if value is None:
        return 0.5
    try:
        confidence = float(value)
    except (TypeError, ValueError) as e:
        raise CommandError(f"confidence は数値で指定してください: {value!r}") from e
    if not 0.0 <= confidence <= 1.0:
        raise CommandError(f"confidence は 0.0〜1.0 の範囲で指定してください: {confidence}")
    return confidence


def _coerce_limit(value: str | None) -> int:
    """``--limit`` の値を正の整数へ変換する。

    Args:
        value: コマンドラインで渡された文字列。None なら既定件数。

    Returns:
        表示件数の上限。

    Raises:
        CommandError: 整数に変換できない、または 1 未満の場合。
    """
    if value is None:
        return LIST_DEFAULT_LIMIT
    try:
        limit = int(value)
    except ValueError as e:
        raise CommandError(f"--limit は整数で指定してください: {value!r}") from e
    if limit < 1:
        raise CommandError(f"--limit は 1 以上で指定してください: {limit}")
    return limit


def generate_key(title: str, kind: str) -> str:
    """title から知識カードの key スラッグを生成する。

    ASCII 英数字を 1 文字でも含む title は ``slugify`` で kebab-case 化する。
    日本語まじりでも ``pytest をパイプする際は set -o pipefail が必須`` →
    ``pytest-set-o-pipefail`` のように識別子として読める形に落ちる。
    ASCII 英数字を全く含まない title はスラッグ化すると空になるため、
    ``kind`` と title の SHA-1 先頭 8 桁で決定的な key を作る
    （同じ title の再投入が同じ key に落ちて重複行を作らない）。

    Args:
        title: 知識カードの 1 行タイトル。
        kind: 知識の種別。ハッシュ由来 key の接頭辞に使う。

    Returns:
        ``[a-z0-9-]`` のみからなる key。
    """
    if _ASCII_ALNUM_RE.search(title):
        return slugify(title)
    digest = hashlib.sha1(title.encode("utf-8")).hexdigest()[:_KEY_HASH_LENGTH]
    return f"{kind}-{digest}"


def _require_key(args: CommandArgs) -> str:
    """位置引数から key を 1 つ取り出す。

    Args:
        args: コマンド引数。

    Returns:
        指定された key。

    Raises:
        CommandError: 位置引数が無い場合。
    """
    if not args.positionals:
        raise CommandError("key を指定してください")
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


# --- 知識 CRUD ---


def _handle_learn(settings: Settings, args: CommandArgs) -> None:
    """learn コマンド: stdin の JSON から知識カードを 1 件登録する。

    既存の key と衝突した場合は ``upsert_knowledge`` が更新に落とす。
    出力は ``learned: <key>`` の 1 行のみ。

    Args:
        settings: mem 設定。
        args: コマンド引数と stdin JSON。

    Raises:
        CommandError: title 欠落や列挙値・数値の不正がある場合。
    """
    payload = args.stdin_data
    title = str(payload.get("title") or "").strip()
    if not title:
        raise CommandError("learn: title は必須です")

    kind = _validate_choice("kind", str(payload.get("kind") or ""), _KINDS)
    scope = _validate_choice("scope", str(payload.get("scope") or "repo"), _SCOPES)
    source = _validate_choice("source", str(payload.get("source") or "agent"), _SOURCES)
    status = _validate_choice(
        "status", args.values.get("--status") or str(payload.get("status") or "active"), _STATUSES
    )
    confidence = _coerce_confidence(payload.get("confidence"))
    key = str(payload.get("key") or "").strip() or generate_key(title, kind)

    now = utc_now_iso()
    with Database(settings.db_path) as db:
        repo_id = resolve_repo(None, db).id if scope == "repo" else None
        db.upsert_knowledge(
            Knowledge(
                key=key,
                scope=scope,
                kind=kind,
                title=title,
                source=source,
                repo_id=repo_id,
                body=str(payload.get("body") or ""),
                domain=_optional_str(payload.get("domain")),
                confidence=confidence,
                status=status,
                source_ref=_optional_str(payload.get("source_ref")),
                created_at=now,
                updated_at=now,
            )
        )
    print(f"learned: {key}")


def _collect_list_rows(settings: Settings, args: CommandArgs) -> list[Knowledge]:
    """list コマンドの対象行を DB から集めて整列する。

    既定は cwd の repo スコープと global スコープの両方。``--global`` /
    ``--repo`` でどちらか一方に絞る。

    Args:
        settings: mem 設定。
        args: コマンド引数。

    Returns:
        更新の新しい順に並べ、``--limit`` 件で切った Knowledge のリスト。

    Raises:
        CommandError: スコープ指定が矛盾する、または列挙値・件数が不正な場合。
    """
    only_global = "--global" in args.flags
    only_repo = "--repo" in args.flags
    if only_global and only_repo:
        raise CommandError("--global と --repo は同時に指定できません")

    status = _validate_choice("status", args.values.get("--status", "active"), _STATUSES)
    kind = args.values.get("--kind")
    if kind is not None:
        _validate_choice("kind", kind, _KINDS)
    limit = _coerce_limit(args.values.get("--limit"))

    rows: list[Knowledge] = []
    with Database(settings.db_path) as db:
        if not only_global:
            repo = resolve_repo(None, db)
            rows.extend(db.list_knowledge(scope="repo", repo_id=repo.id, status=status))
        if not only_repo:
            rows.extend(db.list_knowledge(scope="global", status=status))

    if kind is not None:
        rows = [row for row in rows if row.kind == kind]
    rows.sort(key=lambda row: (row.updated_at, row.id or 0), reverse=True)
    return rows[:limit]


def _handle_list(settings: Settings, args: CommandArgs) -> None:
    """list コマンド: 知識カードの一覧を 1 件 1 行で出す。

    ``body`` は出さず ``- [kind] title (key)`` のみ。0 件なら無出力。
    ``--json`` 指定時のみ機械処理向けの JSON 1 行を出す。

    Args:
        settings: mem 設定。
        args: コマンド引数。
    """
    rows = _collect_list_rows(settings, args)
    if not rows:
        return

    if "--json" in args.flags:
        payload = [
            {"key": row.key, "scope": row.scope, "kind": row.kind, "title": row.title} for row in rows
        ]
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return

    for line in _fit_budget([f"- [{row.kind}] {row.title} ({row.key})" for row in rows], LIST_CHAR_BUDGET):
        print(line)


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

    Args:
        settings: mem 設定。
        args: コマンド引数。

    Raises:
        CommandError: key の指定が無い、または該当が無い場合。
    """
    key = _require_key(args)
    with Database(settings.db_path) as db:
        found = _find_knowledge(db, key)
        db.set_knowledge_status(found.id, "active")
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


def _format_injected_item(row: Knowledge) -> str:
    """知識カード 1 件を注入用の 1 行へ整形する。

    既定は ``- [kind] title`` のみ。``body`` を足しても 1 行が
    ``CONTEXT_ITEM_CHAR_LIMIT`` 文字に収まる場合だけ ``—`` で続ける。
    長い body を抱えたカードが 1 件で節の予算を食い潰すのを防ぐ。

    Args:
        row: 注入対象の知識カード。

    Returns:
        整形済みの 1 行（改行を含まない）。
    """
    head = f"- [{row.kind}] {row.title}"
    body = row.body.strip()
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

    Args:
        db: 参照する mem データベース。
        repo_id: 対象リポジトリの ``repos.id``。

    Returns:
        見出しと引き継ぎ本文からなる節。引き継ぎが無ければ空文字列。
    """
    session = db.get_latest_session(repo_id)
    if session is None:
        return ""
    handoff = session.handoff.strip()
    if not handoff:
        return ""
    if len(handoff) > CONTEXT_HANDOFF_CHAR_BUDGET:
        handoff = handoff[: CONTEXT_HANDOFF_CHAR_BUDGET - 1] + "…"
    stamp = datetime.fromisoformat(session.ended_at or session.started_at).strftime("%Y-%m-%d %H:%M")
    return f"## 前回の続き ({stamp})\n{handoff}"


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
    session_uid = str(payload.get("session_id") or "").strip()
    if not session_uid:
        return
    db.start_session(Session(session_uid=session_uid, repo_id=repo.id, harness=detect_harness()))


def _build_context(settings: Settings, args: CommandArgs) -> str:
    """注入する ``<bluecore-memory>`` ブロックを組み立てる。

    共通知識（``scope='global'``）・リポジトリ知識（``scope='repo'``）・
    前回の引き継ぎの 3 節を、いずれも ``status='active'`` に限って集める
    （``pending`` は observer の下書きでノイズになるため注入しない）。

    Args:
        settings: mem 設定。
        args: コマンド引数とフックの stdin JSON。

    Returns:
        注入する文字列。3 節すべて空なら空文字列（注入をゼロにする）。
    """
    payload = args.stdin_data
    with Database(settings.db_path) as db:
        repo = resolve_repo(_optional_str(payload.get("cwd")), db)
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
        ]

    body = "\n\n".join(section for section in sections if section)
    if not body:
        return ""
    return f"<bluecore-memory>\n{body}\n</bluecore-memory>"


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


_COMMAND_HANDLERS: dict[str, _CommandHandler] = {
    "init": lambda settings, args: _handle_init(settings),
    "setup": lambda settings, args: _handle_setup(settings),
    "context": _handle_context,
    "learn": _handle_learn,
    "list": _handle_list,
    "show": _handle_show,
    "promote": _handle_promote,
    "forget": _handle_forget,
}


HELP_TEXT = """\
CLI Commands for mem

Usage:
  python -m bluecore.mem <command> [options]

Commands:
  init                                  Recreate the local mem database from scratch
  setup                                 Initialize the local mem database
  context                               Emit the SessionStart knowledge injection (hook JSON on stdin)
  learn                                 Store one knowledge card read as JSON from stdin
  list [--global|--repo]                List knowledge titles (default: repo + global, active, 20)
       [--status S] [--kind K]
       [--limit N] [--json]
  show <key>                            Show one knowledge card including its body
  promote <key>                         Flip a pending knowledge card to active
  forget <key> [--superseded-by KEY]    Archive a knowledge card
"""


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
