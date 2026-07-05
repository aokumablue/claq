"""フックから呼び出される CLI エントリポイント"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bluecore.hooks.hook_common import print_session_start_output
from bluecore.lib.core_utils import get_git_user_name
from bluecore.mem import cli_dashboard_handlers as _dashboard_handlers
from bluecore.mem import cli_digest_handlers as _digest_handlers
from bluecore.mem import cli_record_handlers as _record_handlers
from bluecore.mem import cli_search_handlers as _search_handlers
from bluecore.mem import cli_session_handlers as _session_handlers
from bluecore.mem import cli_sync_handlers as _sync_handlers
from bluecore.mem import cli_team_handlers as _team_handlers
from bluecore.mem.cli_sync_handlers import SyncStatusDict
from bluecore.mem.logger import get as _get_logger
from bluecore.mem.settings import Settings

if TYPE_CHECKING:
    from bluecore.mem.database import Database, MemoryChunk
    from bluecore.mem.search import SearchResult

log = _get_logger("CLI")

# SessionStart フックで JSON 出力が必須なコマンドの集合。
# main() のフォールバック保証とエラー時の早期 return に使用する。
_SESSION_START_COMMANDS: frozenset[str] = frozenset(
    {"setup", "context", "record-project-profile", "team-context"}
)
# フックから呼ばれるが失敗しても exit_code=0 を維持すべきコマンド（非0 を返すとフックエラーになるため）。
_BENIGN_COMMANDS: frozenset[str] = frozenset(
    {"session-init", "team-session-init", "record-interaction", "sync-check"}
)
_CommandHandler = Callable[[Settings, dict[str, Any]], str | None]


@contextmanager
def _open_db(settings: Settings):
    """Database を開き、ブロック終了時に必ず close するコンテキストマネージャ。"""
    from bluecore.mem.database import Database

    db = Database(settings.db_path)
    try:
        yield db
    finally:
        db.close()


def _parse_argv_and_stdin() -> tuple[str, dict[str, Any]]:
    """コマンド名と JSON stdin を読み取る。"""
    command = sys.argv[1] if len(sys.argv) >= 2 else ""

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
    """Settings と logger を初期化して返す。"""
    import bluecore.mem.logger as _logger_mod

    settings = Settings.load()
    _logger_mod.setup(settings.log_dir, settings.log_level)
    return settings


def _run_session_start_command(command: str, settings: Settings, stdin_data: dict[str, Any]) -> str | None:
    """SessionStart コマンドを実行して追加コンテキストを返す。"""
    handler = _COMMAND_HANDLERS[command]
    return handler(settings, stdin_data)


def _run_normal_command(command: str, settings: Settings, stdin_data: dict[str, Any]) -> int:
    """SessionStart 以外のコマンドを実行し終了コードを返す。"""
    handler = _COMMAND_HANDLERS.get(command)
    if handler is None:
        log.error("不明なコマンド: %s", command)
        return 2

    handler(settings, stdin_data)
    return 0


def embed(texts: list[str]) -> list[list[float]]:
    """埋め込み生成を遅延ロードで実行する。"""
    from bluecore.mem.embedding import embed as _embed

    return _embed(texts)


def main() -> int:
    """CLI エントリポイント。argv からコマンドを解決して実行し、終了コードを返す。

    Returns:
        終了コード。SessionStart コマンドは失敗してもフックエラーを避けるため 0 を維持する。
    """
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help"}:
        command = sys.argv[1] if len(sys.argv) >= 2 else ""
        if command in _SESSION_START_COMMANDS:
            print_session_start_output()
            return 0
        print(HELP_TEXT)
        return 0

    command, stdin_data = _parse_argv_and_stdin()
    additional_context = ""
    # SESSION_START コマンドは設定ロード失敗でも exit_code=0 を維持する。
    # フックが非 0 を返すとセッション全体がエラー扱いになるため。
    exit_code = 0
    settings: Settings | None = None
    _silent = command in _SESSION_START_COMMANDS or command in _BENIGN_COMMANDS
    try:
        settings = _load_settings_or_raise()
    except Exception as e:
        if not _silent:
            print(f"設定/ログ初期化失敗: {e}", file=sys.stderr)
            exit_code = 1
    else:
        try:
            if command in _SESSION_START_COMMANDS:
                additional_context = _run_session_start_command(command, settings, stdin_data) or ""
            else:
                exit_code = _run_normal_command(command, settings, stdin_data)
        except Exception as e:
            log.error("コマンド %s 失敗: %s", command, e)
            if not _silent:
                print(f"コマンド {command} 失敗: {e}", file=sys.stderr)
                exit_code = 1
    finally:
        if command in _SESSION_START_COMMANDS:
            print_session_start_output(additional_context)

    return exit_code


def _handle_setup(settings: Settings) -> str:
    """Setup: データディレクトリとDB初期化 + sync 設定診断"""
    try:
        _initialize_db(settings)
        log.info("セットアップ完了: %s", settings.data_path)
    except Exception as e:
        log.warning("setup 失敗: %s", e)

    # lite=True で接続テストをスキップし、セッション開始の遅延を防ぐ
    try:
        sync_status = _sync_handlers._build_sync_status_dict(settings, lite=True)
        recommendations = _build_sync_recommendations(sync_status)
        log.info(
            "sync 設定: enabled=%s postgres_url_set=%s psycopg=%s connection=%s",
            sync_status["enabled"],
            sync_status["postgres_url_set"],
            sync_status["psycopg_installed"],
            sync_status["connection"],
        )
        for rec in recommendations:
            log.warning("setup 推奨: %s", rec)
    except Exception as e:
        log.debug("sync 診断スキップ: %s", e)

    return ""


def _build_sync_recommendations(status: SyncStatusDict) -> list[str]:
    """sync_status に基づいて推奨アクションの文字列リストを返す。"""
    recs: list[str] = []
    if not status["postgres_url_set"]:
        recs.append(
            "postgres_url 未設定。"
            "~/.bluecore/settings.json の mem.sync.postgres_url に接続 URL を設定してください"
        )
    if not status["psycopg_installed"]:
        recs.append("psycopg 未インストール。install.sh を再実行してください")
    if status["connection"] == "failed":
        recs.append(f"PG 接続失敗: {status['connection_error']}。PG が起動しているか確認してください")
    return recs


def _handle_init(settings: Settings) -> None:
    """Init: 既存DBを削除して再作成"""
    _initialize_db(settings, recreate=True)
    log.info("DB再作成完了: %s", settings.db_path)


def _initialize_db(settings: Settings, *, recreate: bool = False) -> None:
    """データディレクトリと mem.db を初期化する。"""
    if recreate:
        _remove_db_artifacts(settings.db_path)

    settings.data_path.mkdir(parents=True, exist_ok=True)
    settings.save()
    with _open_db(settings):
        pass

    if recreate:
        # WAL モードの新規接続が残す -wal/-shm を除去し、再作成後の
        # データディレクトリを pristine に保つ。close 時の checkpoint で
        # データは mem.db へ反映済みのため安全。SQLite ビルドにより
        # close 時に自動削除されない環境があるため明示削除する。
        for suffix in ("-wal", "-shm", "-journal"):
            Path(f"{settings.db_path}{suffix}").unlink(missing_ok=True)


def _remove_db_artifacts(db_path: Path) -> None:
    """SQLite DB と sidecar を削除する。"""
    for path in (
        db_path,
        Path(f"{db_path}-wal"),
        Path(f"{db_path}-shm"),
        Path(f"{db_path}-journal"),
    ):
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()


def _handle_context(settings: Settings, stdin_data: dict) -> str:
    """context コマンド: ローカル DB から ``<mem-context>`` を構築して返す。"""
    return _session_handlers.handle_context(
        settings,
        stdin_data,
        open_db=_open_db,
        get_project=_get_project,
        log=log,
    )


def _search_deps() -> _search_handlers.SearchDeps:
    """search系ハンドラへ渡す依存性をまとめて構築する。"""
    return _search_handlers.SearchDeps(
        open_db=_open_db,
        get_project=_get_project,
        coerce_int=_coerce_int,
        log=log,
    )


def _handle_search(settings: Settings, stdin_data: dict) -> None:
    """search コマンド: ローカル DB を検索する。"""
    _search_handlers.handle_search(settings, stdin_data, _search_deps())


def _handle_session_init(settings: Settings, stdin_data: dict) -> None:
    """session-init コマンド: セッションを初期化し適応的記憶を注入する。"""
    _session_handlers.handle_session_init(
        settings,
        stdin_data,
        open_db=_open_db,
        get_project=_get_project,
        log=log,
    )


def _handle_observe(settings: Settings, stdin_data: dict) -> None:
    """observe コマンド: ツール使用チャンクを保存する。"""
    _session_handlers.handle_observe(
        settings,
        stdin_data,
        open_db=_open_db,
        get_project=_get_project,
        log=log,
    )


def _handle_session_end(settings: Settings, stdin_data: dict) -> None:
    """session-end コマンド: 現在のセッションを埋め込み生成して圧縮する。"""
    deps = _session_handlers.SessionEndDeps(
        open_db=_open_db,
        embed_fn=embed,
        log=log,
        time_module=time,
    )
    _session_handlers.handle_session_end(settings, stdin_data, deps)


def _handle_compact(settings: Settings) -> None:
    """compact コマンド: メモリ圧縮を実行する。"""
    _session_handlers.handle_compact(settings, open_db=_open_db, log=log)


def _handle_reembed(settings: Settings) -> None:
    """reembed コマンド: vec テーブルを再作成し全チャンクの埋め込みを再生成する。"""
    deps = _session_handlers.SessionEndDeps(
        open_db=_open_db,
        embed_fn=embed,
        log=log,
        time_module=time,
    )
    _session_handlers.handle_reembed(settings, deps)


def _handle_search_structured(settings: Settings, stdin_data: dict) -> None:
    """search-structured コマンド: フィルタ付き構造化検索を実行する。"""
    _search_handlers.handle_search_structured(settings, stdin_data, _search_deps())


def _apply_structured_filters(
    db: Database,
    candidate_ids: list[int],
    filt: _search_handlers.StructuredFilter,
) -> list[int]:
    """候補 chunk_id を構造化フィルタで絞り込む。"""
    return _search_handlers.apply_structured_filters(db, candidate_ids, filt)


def _parse_date_to_epoch(value: int | str | None) -> int | None:
    """日付文字列または epoch を epoch 秒に変換する。"""
    return _search_handlers.parse_date_to_epoch(value)


def _record_deps() -> _record_handlers.RecordDeps:
    """record系ハンドラへ渡す依存性をまとめて構築する。"""
    return _record_handlers.RecordDeps(
        open_db=_open_db,
        get_project=_get_project,
        log=log,
        get_git_user_name=get_git_user_name,
    )


def _handle_record(settings: Settings, stdin_data: dict) -> None:
    """record コマンド: コマンド/スキル/エージェントからのイベントを明示記録する。"""
    _record_handlers.handle_record(settings, stdin_data, _record_deps())


def _get_project(stdin_data: dict) -> str:
    """cwd からプロジェクト名を導出する"""
    cwd = stdin_data.get("cwd", os.getcwd())
    return os.path.basename(cwd)


def _coerce_int(value: object, default: int) -> int:
    """値を非負 int に変換する。変換できなければ default を返す。"""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _merge_search_results_rrf(
    local_results: list[SearchResult],
    team_results: list[SearchResult],
    top_k: int = 3,
    k: int = 60,
) -> list[SearchResult]:
    """ローカルとチームの検索結果を RRF で統合する。"""
    return _search_handlers.merge_search_results_rrf(local_results, team_results, top_k=top_k, k=k)


def _render_adaptive_context(db: Database, results: list[SearchResult], max_tokens: int = 400) -> str:
    """検索結果を max_tokens 以内の適応的コンテキスト文字列に整形する。"""
    return _search_handlers.render_adaptive_context(db, results, max_tokens=max_tokens)


def _format_fields(
    user_prompt: str,
    tool_names: list[str],
    files_modified: list[str],
    content: str,
) -> str:
    """プロンプト・ツール・変更ファイル・内容を表示用フィールド文字列に整形する。"""
    return _search_handlers.format_fields(user_prompt, tool_names, files_modified, content)


def _format_chunk_from_result(result: SearchResult) -> str:
    """検索結果 1 件を表示用文字列に整形する。"""
    return _search_handlers.format_chunk_from_result(result)


def _format_chunk(chunk: MemoryChunk) -> str:
    """メモリチャンク 1 件を表示用文字列に整形する。"""
    return _search_handlers.format_chunk(chunk)


def _format_timestamp(epoch: int) -> str:
    """epoch 秒を表示用のタイムスタンプ文字列に整形する。"""
    return _search_handlers.format_timestamp(epoch)


def _truncate(text: str, max_len: int) -> str:
    """テキストを max_len 文字に切り詰める。"""
    return _search_handlers.truncate(text, max_len)


def _slim_prompt(text: str, max_len: int = 160) -> str:
    """プロンプト文字列を表示用に簡略化する。"""
    return _search_handlers.slim_prompt(text, max_len=max_len)


def _slim_context_content(text: str, *, max_prose_lines: int = 6, max_prose_line_length: int = 160) -> str:
    """コンテキスト本文を行数・行長の上限で簡略化する。"""
    return _search_handlers.slim_context_content(
        text,
        max_prose_lines=max_prose_lines,
        max_prose_line_length=max_prose_line_length,
    )


def _handle_migrate_settings(settings: Settings) -> None:  # noqa: ARG001
    """既存 settings.json を新セキュリティ仕様（パスワード分離・sslmode 強制）に自動移行する。"""
    import json as _json
    from datetime import datetime
    from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

    from bluecore.mem.cli_sync_handlers import _split_password, _write_pgpass

    settings_path = Path(os.environ.get("HOME", "~")).expanduser() / ".bluecore" / "settings.json"
    if not settings_path.exists():
        log.info("migrate-settings: settings.json が存在しません。スキップ")
        return

    try:
        data = _json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("migrate-settings: settings.json 読み込み失敗: %s", e)
        return

    url: str = data.get("mem", {}).get("sync", {}).get("postgres_url", "") or ""
    if not url:
        log.info("migrate-settings: postgres_url 未設定。スキップ")
        return

    changed = False

    # 1. パスワード分離
    stripped_url, password = _split_password(url)
    if password:
        parsed = urlparse(stripped_url)
        host = parsed.hostname or "localhost"
        port = parsed.port if parsed.port is not None else 5432
        db = parsed.path.lstrip("/") or "*"
        user = parsed.username or "*"
        _write_pgpass(host, port, db, user, password)
        url = stripped_url
        changed = True
        log.info("migrate-settings: PG パスワードを <data_dir>/.pgpass に移行しました")

    # 2. sslmode 正規化（明示指定は尊重。危険値は警告のみで書き換えない）
    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    existing_mode = (qs.get("sslmode", [None])[0] or "").lower()
    if existing_mode in {"disable", "allow", "prefer"}:
        log.warning(
            "migrate-settings: sslmode=%s は安全性が低いです。本番環境では sslmode=require 以上を推奨します",
            existing_mode,
        )
    elif not existing_mode:
        qs["sslmode"] = ["require"]
        url = urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))
        changed = True
        log.info("migrate-settings: sslmode=require を付与しました")

    if not changed:
        log.info("migrate-settings: 移行不要")
        return

    # バックアップ
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak_path = settings_path.with_name(f"settings.json.bak-{ts}")
    bak_path.write_bytes(settings_path.read_bytes())
    bak_path.chmod(0o600)

    # 書き戻し
    data.setdefault("mem", {}).setdefault("sync", {})["postgres_url"] = url
    settings_path.write_text(_json.dumps(data, indent=2) + "\n", encoding="utf-8")
    settings_path.chmod(0o600)
    log.info("migrate-settings: settings.json を更新しました（バックアップ: %s）", bak_path.name)


def _handle_sync(settings: Settings, stdin_data: dict) -> None:
    """sync コマンド: ローカル SQLite を PostgreSQL に同期する。"""
    _sync_handlers.handle_sync(settings, stdin_data)


def _handle_sync_check(settings: Settings) -> None:
    """sync-check コマンド: 同期間隔を確認し必要なら同期する。"""
    _sync_handlers.handle_sync_check(settings, log=log)


def _count_lines(path: Path) -> int:
    """ファイルの行数を数える。"""
    return _dashboard_handlers.count_lines(path)


def _collect_project_overview() -> dict:
    """ダッシュボード用にプロジェクト概況を収集する。"""
    return _dashboard_handlers.collect_project_overview(
        count_lines_fn=_count_lines,
        log=log,
    )


def _collect_skill_health_overview(options: dict[str, object]) -> dict[str, object]:
    """ダッシュボード用にスキル健全性の概況を収集する。"""
    return _dashboard_handlers.collect_skill_health_overview(options, log=log)


def _collect_skill_growth_overview(settings: Settings, days: int) -> dict[str, object]:
    """ダッシュボード用に直近 days 日のスキル成長概況を収集する。"""
    return _dashboard_handlers.collect_skill_growth_overview(settings, days, log=log)


def _handle_import(settings: Settings, stdin_data: dict) -> None:
    """import コマンド: 外部データ（instincts/adrs/events）を mem に取り込む。"""
    _dashboard_handlers.handle_import(
        settings,
        stdin_data,
        open_db=_open_db,
        get_git_user_name=get_git_user_name,
    )


def _handle_dashboard(settings: Settings, stdin_data: dict) -> None:
    """dashboard コマンド: PostgreSQL データから静的 HTML ダッシュボードを生成する。"""
    deps = _dashboard_handlers.DashboardDeps(
        open_db=_open_db,
        log=log,
        collect_project_overview_fn=_collect_project_overview,
        collect_skill_health_overview_fn=_collect_skill_health_overview,
        collect_skill_growth_overview_fn=_collect_skill_growth_overview,
    )
    _dashboard_handlers.handle_dashboard(settings, stdin_data, deps)


def _handle_record_interaction(settings: Settings, stdin_data: dict) -> None:
    """record-interaction コマンド: ユーザー/AI のやり取りを interaction_logs に記録する。"""
    _record_handlers.handle_record_interaction(settings, stdin_data, _record_deps())


def _handle_record_project_profile(settings: Settings, stdin_data: dict) -> str:
    """record-project-profile コマンド: プロジェクトの技術スタックを project_profiles に upsert する。"""
    return _record_handlers.handle_record_project_profile(settings, stdin_data, _record_deps())


def _handle_get_project_profile(settings: Settings, stdin_data: dict) -> None:
    """get-project-profile コマンド: project_profiles から技術スタックを取得する。"""
    _record_handlers.handle_get_project_profile(settings, stdin_data, _record_deps())


def _handle_record_item_run(settings: Settings, stdin_data: dict) -> None:
    """record-item-run コマンド: スキル/コマンド/エージェントの実行を mem_item_runs に記録する。"""
    _record_handlers.handle_record_item_run(settings, stdin_data, _record_deps())


def _handle_team_context(settings: Settings, stdin_data: dict) -> str:
    """team-context コマンド: PostgreSQL から ``<team-context>`` を FTS で注入する。"""
    return _team_handlers.handle_team_context(
        settings,
        stdin_data,
        get_project=_get_project,
        get_git_user_name=get_git_user_name,
        log=log,
    )


def _handle_digest_backfill(settings: Settings, stdin_data: dict) -> None:
    """digest-backfill コマンド: 既存セッションの session digest を遡及生成する。"""
    deps = _digest_handlers.DigestBackfillDeps(open_db=_open_db, log=log)
    _digest_handlers.handle_digest_backfill(settings, stdin_data, deps)


def _handle_team_session_init(settings: Settings, stdin_data: dict) -> None:
    """team-session-init コマンド: ハイブリッド検索で ``<team-context>`` を注入する。"""
    _team_handlers.handle_team_session_init(
        settings,
        stdin_data,
        get_project=_get_project,
        get_git_user_name=get_git_user_name,
        log=log,
    )


_COMMAND_HANDLERS: dict[str, _CommandHandler] = {
    "init": lambda settings, stdin_data: (_handle_init(settings) or None),
    "setup": lambda settings, stdin_data: (_handle_setup(settings) or None),
    "migrate-settings": lambda settings, stdin_data: (_handle_migrate_settings(settings) or None),
    "context": _handle_context,
    "search": _handle_search,
    "session-init": _handle_session_init,
    "observe": _handle_observe,
    "session-end": _handle_session_end,
    "compact": lambda settings, stdin_data: (_handle_compact(settings) or None),
    "reembed": lambda settings, stdin_data: (_handle_reembed(settings) or None),
    "search-structured": _handle_search_structured,
    "record": _handle_record,
    "sync": _handle_sync,
    "sync-check": lambda settings, stdin_data: (_handle_sync_check(settings) or None),
    "sync-status": lambda settings, stdin_data: (_sync_handlers.handle_sync_status(settings, stdin_data) or None),
    "import": _handle_import,
    "dashboard": _handle_dashboard,
    "record-interaction": _handle_record_interaction,
    "record-project-profile": _handle_record_project_profile,
    "get-project-profile": _handle_get_project_profile,
    "record-item-run": _handle_record_item_run,
    "team-context": _handle_team_context,
    "team-session-init": _handle_team_session_init,
    "digest-backfill": _handle_digest_backfill,
}


HELP_TEXT = """\
CLI Commands for mem

Usage:
  python -m bluecore.mem <command>

Commands:
  init               Recreate the local mem database from scratch
  setup              Initialize the local mem database
  context            Build <mem-context> from the local database (reads JSON from stdin)
  search             Search the local database (reads JSON from stdin)
  search-structured  Structured search with filters (tool_name, file_pattern, date_range)
  record             Explicitly record an event from commands/skills/agents
  session-init       Initialize a session and inject adaptive memory (reads JSON from stdin)
  observe            Store a tool-use chunk (reads JSON from stdin)
  session-end        Embed and compact the current session (reads JSON from stdin)
  compact            Execute memory compaction
  reembed            Recreate the vector table and re-embed all chunks (after model change)
  sync               Sync local SQLite data to PostgreSQL (reads JSON from stdin)
  sync-check         Check sync interval and sync if needed
  import             Import external data (instincts, adrs, events) to mem
  dashboard          Generate a static HTML dashboard from PostgreSQL data
  record-interaction     Record a user/AI interaction pair to interaction_logs
  record-project-profile Upsert project tech stack to project_profiles
  get-project-profile    Get project tech stack from project_profiles
  record-item-run        Record a skill/command/agent execution to mem_item_runs
  team-context           Inject <team-context> from PostgreSQL (FTS-only, SessionStart)
  team-session-init      Inject <team-context> with hybrid search (UserPromptSubmit)
  migrate-settings       Migrate existing ~/.bluecore/settings.json to hardened format (PG password → <data_dir>/.pgpass, sslmode=require if unset)
  digest-backfill        Backfill session digests for existing sessions (reads JSON from stdin)

search-structured Input (JSON):
  {"query": "...", "project": "...", "tool_name": "Edit", "file_pattern": "*.py", "date_from": "2024-01-01", "date_to": "2024-12-31"}

record Input (JSON):
  {"event_type": "review|plan|audit|...", "content": "...", "user_prompt": "...", "metadata": {"files_read": [], "files_modified": []}}

sync Input (JSON):
  {"dry_run": false}

import Input (JSON):
  {"types": ["instincts", "adrs", "events"], "repo_root": "/path/to/repo"}

digest-backfill Input (JSON):
  {"force": false, "project": null, "limit": null}
"""


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
