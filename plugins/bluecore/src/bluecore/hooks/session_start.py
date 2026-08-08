#!/usr/bin/env python3
"""
新しいセッションで以前のコンテキストを読み込む SessionStart フック

新しい Claude セッション開始時に実行されます。最新のセッションサマリーを
stdout 経由で Claude のコンテキストに読み込み、利用可能なセッションと
学習したスキルを報告します。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from bluecore.hooks.hook_common import emit_session_start_output, read_raw_stdin
from bluecore.lib.core_utils import (
    ensure_dir,
    find_files,
    get_learned_skills_dir,
    get_session_search_dirs,
    get_sessions_dir,
    log,
    read_file,
    strip_ansi,
)
from bluecore.lib.package_manager import get_package_manager, get_selection_prompt
from bluecore.lib.project_detect import ProjectInfo, detect_project
from bluecore.lib.sanitize import sanitize_log_value
from bluecore.lib.settings import extract_coverage_hint_lines
from bluecore.lib.slim_text import compact_line

_SUMMARY_START = "<!-- bluecore:SUMMARY:START -->"
_SUMMARY_END = "<!-- bluecore:SUMMARY:END -->"
_SUMMARY_PATTERN = re.compile(
    re.escape(_SUMMARY_START) + r"\n(.*?)\n" + re.escape(_SUMMARY_END),
    re.DOTALL,
)
_SECTION_PATTERN = re.compile(r"(### .+?\n.*?)(?=\n### |\Z)", re.DOTALL)
# Files Modified は次セッションでプロジェクト探索から再取得できるため注入しない（トークン削減）
_KEEP_SECTIONS = {"### Tasks"}


def _log_sanitized_exception(prefix: str, exc: BaseException) -> None:
    """例外をサニタイズして単一行ログとして出力する。"""
    log(f"{prefix}: {sanitize_log_value(str(exc))}")


def _filter_session_summary(content: str, max_length: int = 2000) -> str:
    """Tasks と Files Modified のみを抽出し、上限文字数に収める。

    SUMMARY マーカーが見つからない場合は compact_line でフォールバックする。

    Args:
        content: session.tmp の全文字列。
        max_length: 出力の最大文字数。

    Returns:
        フィルタ済みの文字列を返します。

    Raises:
        例外は発生しません。
    """
    if not content:
        return content

    m = _SUMMARY_PATTERN.search(content)
    if not m:
        return compact_line(content, max_length)

    block = m.group(1)
    parts: list[str] = []
    for sec in _SECTION_PATTERN.finditer(block):
        header = sec.group(1).split("\n", 1)[0]
        if header in _KEEP_SECTIONS:
            parts.append(sec.group(1).strip())

    result = "\n\n".join(parts)
    if len(result) > max_length:
        result = compact_line(result, max_length - 3)  # -3 to account for "..." suffix
    return result


def _import_adrs_and_instincts() -> None:
    """SessionStart 時に ADR・instincts を mem DB に取り込む（トークン増加なし）。"""
    try:
        from bluecore.lib.core_utils import get_git_user_name
        from bluecore.mem.database import Database
        from bluecore.mem.importers import import_adrs, import_instincts
        from bluecore.mem.settings import Settings

        settings = Settings.load()
        origin_user = get_git_user_name()
        db = Database(settings.db_path)
        try:
            n_instincts = import_instincts(db, origin_user)
            n_adrs = import_adrs(db, origin_user, repo_root=Path.cwd())
        finally:
            db.close()
        log(f"[SessionStart] mem import: instincts={n_instincts} adrs={n_adrs}")
    except Exception as e:
        log(f"[SessionStart] mem import error: {sanitize_log_value(str(e))}")


def dedupe_recent_sessions(search_dirs: list[Path]) -> list[dict]:
    """basename で最近のセッションを重複排除し、名前ごとに最新のものを保持

    mtime でソートされたリストを返す（新しいものが先）

    Args:
        search_dirs: 処理に渡す search_dirs の値です。

    Returns:
        処理結果を返します。

    Raises:
        例外は発生しません。
    """
    recent_sessions_by_name = {}

    for dir_index, dir_path in enumerate(search_dirs):
        matches = find_files(dir_path, "*-session.tmp", max_age=7)

        for match in matches:
            basename = Path(match["path"]).name
            current = {
                **match,
                "basename": basename,
                "dir_index": dir_index,
            }
            existing = recent_sessions_by_name.get(basename)

            if (
                not existing
                or current["mtime"] > existing["mtime"]
                or (current["mtime"] == existing["mtime"] and current["dir_index"] < existing["dir_index"])
            ):
                recent_sessions_by_name[basename] = current

    results = list(recent_sessions_by_name.values())
    results.sort(key=lambda x: (-x["mtime"], x["dir_index"]))
    return results


def _collect_session_context(sessions_dir: Path) -> list[str]:
    """最近のセッション要約と未完了チェックポイントをコンテキストパーツとして収集する。"""
    parts: list[str] = []

    recent_sessions = dedupe_recent_sessions(get_session_search_dirs())
    if recent_sessions:
        latest = recent_sessions[0]
        log(f"[SessionStart] Found {len(recent_sessions)} recent session(s)")
        log(f"[SessionStart] Latest: {latest['path']}")
        content = strip_ansi(read_file(latest["path"]) or "")
        if content and "[Session context goes here]" not in content:
            filtered = _filter_session_summary(content)
            parts.append(f"Previous session summary:\n{filtered}")

    checkpoint_files = find_files(sessions_dir, "checkpoint-*.md", max_age=7)
    active_checkpoints = [
        c for c in checkpoint_files if "completed: false" in (read_file(c["path"]) or "")
    ]
    if active_checkpoints:
        latest_checkpoint = active_checkpoints[0]
        raw_content = strip_ansi(read_file(latest_checkpoint["path"]) or "")
        if raw_content:  # pragma: no branch  # active 判定と同一ファイル読込のため空にはならない
            parts.append(f"Active checkpoint:\n{compact_line(raw_content, 500)}")
            log(f"[SessionStart] Injected active checkpoint: {latest_checkpoint['path']}")

    return parts


def _collect_project_context(project_info: ProjectInfo) -> list[str]:
    """プロジェクト検出結果からコンテキストパーツを生成し、パッケージマネージャーをログ出力する。"""
    parts: list[str] = []

    pm = get_package_manager()
    if pm.name is not None:
        log(f"[SessionStart] Package manager: {pm.name} ({pm.source})")
    elif (Path.cwd() / "package.json").exists():
        log(get_selection_prompt())
    elif "ruby" in project_info.languages:
        fw = ", ".join(project_info.frameworks) or "none"
        log(f"[SessionStart] Ruby project detected (bundler) — frameworks: {fw}")

    if project_info.languages or project_info.frameworks:
        log_parts = []
        if project_info.languages:
            log_parts.append(f"languages: {', '.join(project_info.languages)}")
        if project_info.frameworks:
            log_parts.append(f"frameworks: {', '.join(project_info.frameworks)}")
        log(f"[SessionStart] Project detected — {'; '.join(log_parts)}")
        project_dict: dict = {
            "languages": project_info.languages,
            "frameworks": project_info.frameworks,
            "primary_language": project_info.primary_language,
        }
        coverage_hint = extract_coverage_hint_lines(Path.cwd())
        if coverage_hint:
            project_dict["coverage_hint"] = coverage_hint
        parts.append(f"Project type: {json.dumps(project_dict)}")
    else:
        log("[SessionStart] No specific project type detected")

    return parts


def run(_raw_input: str) -> str:
    """セッション開始フックを実行し hookSpecificOutput の JSON を返す

    Args:
        _raw_input: フックの生 stdin。本フックでは内容を参照しない。

    Returns:
        additionalContext を含む hookSpecificOutput を格納した JSON 文字列。
    """
    learned_dir = get_learned_skills_dir()
    sessions_dir = get_sessions_dir()
    ensure_dir(sessions_dir)
    ensure_dir(learned_dir)

    additional_context_parts: list[str] = []
    additional_context_parts.extend(_collect_session_context(sessions_dir))

    learned_skills = find_files(learned_dir, "*.md")
    if learned_skills:
        log(f"[SessionStart] {len(learned_skills)} learned skill(s) available in {learned_dir}")

    project_info = detect_project(Path.cwd())
    additional_context_parts.extend(_collect_project_context(project_info))

    _import_adrs_and_instincts()

    additional_context = "\n\n".join(additional_context_parts)
    return emit_session_start_output(additional_context)


def main() -> int:
    """スクリプトとして実行されたときのエントリポイント

    Args:
        引数はありません。

    Returns:
        処理結果を返します。

    Raises:
        例外は発生しません。
    """
    try:
        raw = read_raw_stdin()
        output = run(raw)
        print(output, end="")
        return 0
    except Exception as err:
        _log_sanitized_exception("[SessionStart] Error", err)
        print(emit_session_start_output(), end="")
        return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
