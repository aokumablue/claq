#!/usr/bin/env python3
"""
新しいセッションで以前のコンテキストを読み込む SessionStart フック

新しい Claude セッション開始時に実行されます。未完了チェックポイントと
検出したプロジェクト種別を stdout 経由で Claude のコンテキストに読み込みます。

前回セッションの要約（直近の依頼・変更ファイル）は本フックでは扱いません。
同じ情報は ``bluecore.mem.cli context`` が DB の ``sessions.handoff`` から
``## 前回の続き`` として注入するため、二重管理になるからです。本フックが
注入するのは checkpoint（中断した反復ループの再開点）だけです。
"""

from __future__ import annotations

import json
from pathlib import Path

from bluecore.hooks.hook_common import emit_session_start_output, read_raw_stdin
from bluecore.lib.core_utils import (
    ensure_dir,
    find_files,
    get_learned_skills_dir,
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


def _log_sanitized_exception(prefix: str, exc: BaseException) -> None:
    """例外をサニタイズして単一行ログとして出力する。"""
    log(f"{prefix}: {sanitize_log_value(str(exc))}")


def _collect_session_context(sessions_dir: Path) -> list[str]:
    """未完了チェックポイントをコンテキストパーツとして収集する。

    Args:
        sessions_dir: checkpoint ファイルを探すディレクトリ。

    Returns:
        注入するコンテキストパーツ。未完了 checkpoint が無ければ空リスト。
    """
    parts: list[str] = []

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
    """セッション開始フックを実行し hookSpecificOutput の JSON を返す。

    Grok plugin-root symlink を fail-open で修復してからコンテキストを収集する。

    Args:
        _raw_input: フックの生 stdin。本フックでは内容を参照しない。

    Returns:
        additionalContext を含む hookSpecificOutput を格納した JSON 文字列。
    """
    try:
        from bluecore.lib.grok_plugin_root import ensure_grok_plugin_root_symlink

        linked = ensure_grok_plugin_root_symlink()
        if linked is not None:
            log(f"[SessionStart] Grok plugin root symlink -> {linked}")
    except Exception as exc:  # noqa: BLE001 — セッション開始を止めない
        _log_sanitized_exception("[SessionStart] Grok symlink 修復スキップ", exc)

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
