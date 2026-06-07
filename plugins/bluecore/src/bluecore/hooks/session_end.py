#!/usr/bin/env python3
"""
アクティブセッション中の学習内容を永続化する SessionEnd フック

Stop イベント時（各応答後）に実行されます。セッショントランスクリプト
（stdin JSON の transcript_path 経由）から意味のあるサマリーを抽出し、
セッション間の継続性のためセッションファイルを更新します。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from bluecore.hooks.hook_common import parse_json_object, read_raw_stdin
from bluecore.lib.core_utils import (
    ensure_dir,
    get_date_string,
    get_project_name,
    get_session_id_short,
    get_sessions_dir,
    get_time_string,
    log,
    read_file,
    run_command,
    strip_ansi,
    write_file,
)
from bluecore.lib.slim_text import compact_line

SUMMARY_START_MARKER = "<!-- bluecore:SUMMARY:START -->"
SUMMARY_END_MARKER = "<!-- bluecore:SUMMARY:END -->"
SESSION_SEPARATOR = "\n---\n"


def _collect_user_message(entry: dict) -> str:
    """トランスクリプトエントリからユーザーメッセージテキストを抽出して返す。空の場合は空文字列。"""
    if not (
        entry.get("type") == "user"
        or entry.get("role") == "user"
        or entry.get("message", {}).get("role") == "user"
    ):
        return ""
    raw_content = entry.get("message", {}).get("content") or entry.get("content")
    if isinstance(raw_content, str):
        text = raw_content
    elif isinstance(raw_content, list):
        text = " ".join(str(c.get("text", "")) if isinstance(c, dict) else "" for c in raw_content)
    else:
        return ""
    return compact_line(strip_ansi(text).strip(), 200)


def _collect_tool_use(entry: dict, tools_used: set, files_modified: set) -> None:
    """直接の tool_use エントリおよび assistant ブロックからツール名とファイルパスを収集する。"""
    if entry.get("type") == "tool_use" or entry.get("tool_name"):
        tool_name = entry.get("tool_name") or entry.get("name") or ""
        if tool_name:
            tools_used.add(tool_name)
        tool_input = entry.get("tool_input") or entry.get("input") or {}
        file_path = tool_input.get("file_path") or tool_input.get("path") or ""
        if file_path and tool_name in ("Edit", "Write", "MultiEdit"):
            files_modified.add(file_path)

    if entry.get("type") == "assistant" and isinstance(entry.get("message", {}).get("content"), list):
        for block in entry["message"]["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_name = block.get("name", "")
                if tool_name:
                    tools_used.add(tool_name)
                block_input = block.get("input") or {}
                file_path = block_input.get("file_path") or block_input.get("path") or ""
                if file_path and tool_name in ("Edit", "Write", "MultiEdit"):
                    files_modified.add(file_path)


def extract_session_summary(transcript_path: str) -> dict | None:
    """セッショントランスクリプトから意味のあるサマリーを抽出

    userMessages、toolsUsed、filesModified、totalMessages を含む dict を返す
    """
    content = read_file(transcript_path)
    if not content:
        return None

    lines = content.split("\n")
    user_messages: list[str] = []
    tools_used: set[str] = set()
    files_modified: set[str] = set()
    parse_errors = 0

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            msg = _collect_user_message(entry)
            if msg:
                user_messages.append(msg)
            _collect_tool_use(entry, tools_used, files_modified)
        except json.JSONDecodeError:
            parse_errors += 1

    if parse_errors > 0:
        log(f"[SessionEnd] Skipped {parse_errors}/{len(lines)} unparseable transcript lines")

    if not user_messages:
        return None

    return {
        "userMessages": user_messages[-10:],
        "toolsUsed": sorted(tools_used)[:20],
        "filesModified": sorted(files_modified)[:30],
        "totalMessages": len(user_messages),
    }


def get_session_metadata() -> dict:
    """セッションメタデータ（プロジェクト、ブランチ、ワークツリー）を取得"""
    branch_result = run_command("git rev-parse --abbrev-ref HEAD")

    return {
        "project": get_project_name() or "unknown",
        "branch": branch_result["output"] if branch_result["success"] and branch_result["output"] else "unknown",
        "worktree": str(Path.cwd()),
    }


def extract_header_field(header: str, label: str) -> str | None:
    """マークダウンヘッダーからフィールド値を抽出"""
    pattern = rf"\*\*{re.escape(label)}:\*\*\s*(.+)$"
    match = re.search(pattern, header, re.MULTILINE)
    return match.group(1).strip() if match else None


def build_session_header(today: str, current_time: str, metadata: dict, existing_content: str = "") -> str:
    """メタデータを含むセッションヘッダーを構築"""
    heading_match = re.search(r"^#\s+.+$", existing_content, re.MULTILINE)
    heading = heading_match.group(0) if heading_match else f"# Session: {today}"
    date = extract_header_field(existing_content, "Date") or today
    started = extract_header_field(existing_content, "Started") or current_time

    return "\n".join(
        [
            heading,
            f"**Date:** {date}",
            f"**Started:** {started}",
            f"**Last Updated:** {current_time}",
            f"**Project:** {metadata['project']}",
            f"**Branch:** {metadata['branch']}",
            f"**Worktree:** {metadata['worktree']}",
            "",
        ]
    )


def merge_session_header(content: str, today: str, current_time: str, metadata: dict) -> str | None:
    """セッションヘッダーを新しいメタデータとマージ"""
    separator_index = content.find(SESSION_SEPARATOR)
    if separator_index == -1:
        return None

    existing_header = content[:separator_index]
    body = content[separator_index + len(SESSION_SEPARATOR) :]
    next_header = build_session_header(today, current_time, metadata, existing_header)
    return f"{next_header}{SESSION_SEPARATOR}{body}"


def build_summary_section(summary: dict) -> str:
    """抽出されたデータからサマリーセクションを構築"""
    section = ""

    # タスク（ユーザーメッセージから — 改行を折りたたみバッククォートをエスケープ）
    section += "### Tasks\n"
    for msg in summary["userMessages"]:
        escaped = compact_line(msg, 200).replace("\n", " ").replace("`", "\\`")
        if not escaped:
            continue
        section += f"- {escaped}\n"
    section += "\n"

    # 変更されたファイル
    if summary["filesModified"]:
        section += "### Files Modified\n"
        for f in summary["filesModified"]:
            section += f"- {f}\n"
        section += "\n"

    # 使用されたツール
    if summary["toolsUsed"]:
        section += f"### 使用したツール\n{', '.join(summary['toolsUsed'])}\n\n"

    section += f"### 統計\n- ユーザーメッセージ総数: {summary['totalMessages']}\n"

    return section


def build_summary_block(summary: dict) -> str:
    """マーカー付きの完全なサマリーブロックを構築"""
    return f"{SUMMARY_START_MARKER}\n{build_summary_section(summary).strip()}\n{SUMMARY_END_MARKER}"


def _record_stop_event(summary: dict | None, metadata: dict) -> None:
    """Stop 時にセッションイベントを event_logs に記録（AI コンテキスト注入なし）。"""
    try:
        import time

        from bluecore.lib.core_utils import get_git_user_name
        from bluecore.mem.database import Database, EventLog
        from bluecore.mem.settings import Settings

        settings = Settings.load()
        content = json.dumps(
            {
                "project": metadata.get("project"),
                "branch": metadata.get("branch"),
                "tools_used": summary.get("toolsUsed") if summary else [],
                "files_modified": summary.get("filesModified") if summary else [],
                "total_messages": summary.get("totalMessages") if summary else 0,
            },
            ensure_ascii=False,
        )
        event = EventLog(
            origin_user=get_git_user_name(),
            event_type="session_stop",
            content=content,
            created_at_epoch=int(time.time()),
            project_id=metadata.get("project"),
        )
        db = Database(settings.db_path)
        try:
            db.store_event_log(event)
        finally:
            db.close()
        log(f"[SessionEnd] event_log recorded: project={metadata.get('project')}")
    except Exception as e:
        log(f"[SessionEnd] event_log error: {e}")


_CHECKPOINT_THRESHOLD = 30
_CHECKPOINT_CONTEXT_MAX = 500


def _auto_save_checkpoint(summary: dict, metadata: dict, sessions_dir: Path) -> None:
    """メッセージ数が閾値を超えた場合にチェックポイントを自動保存する。

    既存のアクティブなチェックポイントがあれば Files Modified を更新し、
    なければ新規作成する。

    Args:
        summary: extract_session_summary() の戻り値。
        metadata: get_session_metadata() の戻り値。
        sessions_dir: セッションデータ保存ディレクトリ。
    """
    today = get_date_string()
    project = metadata.get("project", "unknown")
    slug = re.sub(r"[^a-z0-9]+", "-", project.lower()).strip("-")
    checkpoint_path = sessions_dir / f"checkpoint-{today}-{slug}.md"

    files_modified = summary.get("filesModified", [])
    files_section = "\n".join(f"- {f}" for f in files_modified) if files_modified else "- (なし)"
    context_hint = compact_line(
        f"project={project} branch={metadata.get('branch', '?')} messages={summary.get('totalMessages', 0)}",
        _CHECKPOINT_CONTEXT_MAX,
    )

    if checkpoint_path.exists():
        existing = read_file(checkpoint_path) or ""
        if "completed: true" in existing:
            log(f"[SessionEnd] Checkpoint already completed, skipping: {checkpoint_path}")
            return
        updated = re.sub(
            r"(?m)^## 変更済みファイル\n.*?(?=\n## |\Z)",
            f"## 変更済みファイル\n{files_section}",
            existing,
            flags=re.DOTALL,
        )
        write_file(checkpoint_path, updated)
        log(f"[SessionEnd] Updated auto-checkpoint: {checkpoint_path}")
    else:
        content = (
            f"---\ntask: {project[:20]}\ncompleted: false\n---\n\n"
            f"## 目標\n(セッション継続のための自動チェックポイント)\n\n"
            f"## 完了済みステップ\n- (セッション終了時点まで)\n\n"
            f"## 進行中\n- [ ] 次のステップを確認してください\n\n"
            f"## 残りステップ\n- [ ] (次セッションで確認)\n\n"
            f"## 変更済みファイル\n{files_section}\n\n"
            f"## 再開コンテキスト\n{context_hint}\n"
        )
        write_file(checkpoint_path, content)
        log(f"[SessionEnd] Created auto-checkpoint: {checkpoint_path}")


def _update_session_file(session_file: Path, summary: dict | None, today: str, current_time: str, session_metadata: dict) -> None:
    """既存のセッションファイルのヘッダーとサマリーブロックを更新する。"""
    existing = read_file(session_file)
    updated_content = existing

    if existing:
        merged = merge_session_header(existing, today, current_time, session_metadata)
        updated_content = merged if merged else existing
        if not merged:
            log(f"[SessionEnd] Failed to normalize header in {session_file}")

    if summary and updated_content:
        summary_block = build_summary_block(summary)
        if SUMMARY_START_MARKER in updated_content and SUMMARY_END_MARKER in updated_content:
            pattern = re.escape(SUMMARY_START_MARKER) + r"[\s\S]*?" + re.escape(SUMMARY_END_MARKER)
            updated_content = re.sub(pattern, summary_block, updated_content)
        else:
            updated_content = re.sub(
                r"## (?:Session Summary|Current State)[\s\S]*?$",
                f"{summary_block}\n\n### 次回セッションへの引継ぎ\n-\n\n### 読み込むコンテキスト\n```\n[relevant files]\n```\n",
                updated_content,
            )

    if updated_content:
        write_file(session_file, updated_content)
        log(f"[SessionEnd] Updated session file: {session_file}")


def _create_session_file(session_file: Path, summary: dict | None, today: str, current_time: str, session_metadata: dict) -> None:
    """新規セッションファイルをサマリーあり・なし両パターンで作成する。"""
    if summary:
        summary_section = f"{build_summary_block(summary)}\n\n### 次回セッションへの引継ぎ\n-\n\n### 読み込むコンテキスト\n```\n[relevant files]\n```"
    else:
        summary_section = "## 現在の状態\n\n[セッションコンテキストをここに記載]\n\n### 完了済み\n- [ ]\n\n### 進行中\n- [ ]\n\n### 次回セッションへの引継ぎ\n-\n\n### 読み込むコンテキスト\n```\n[relevant files]\n```"
    template = f"{build_session_header(today, current_time, session_metadata)}{SESSION_SEPARATOR}{summary_section}\n"
    write_file(session_file, template)
    log(f"[SessionEnd] Created session file: {session_file}")


def run(raw_input: str) -> str:
    """セッション終了フックを実行。入力をそのまま返す（パススルー）"""
    try:
        input_data = parse_json_object(raw_input)
        transcript_path = input_data.get("transcript_path") if input_data else None
        # stop_hook_active=True はループ継続中の中間停止。session_stop 記録は本停止時のみ行う。
        stop_hook_active = bool(input_data.get("stop_hook_active")) if input_data else False

        sessions_dir = get_sessions_dir()
        today = get_date_string()
        short_id = get_session_id_short()
        session_file = sessions_dir / f"{today}-{short_id}-session.tmp"
        session_metadata = get_session_metadata()
        ensure_dir(sessions_dir)
        current_time = get_time_string()

        if transcript_path and not Path(transcript_path).exists():
            log(f"[SessionEnd] Transcript not found: {transcript_path}")
        summary = extract_session_summary(transcript_path) if transcript_path and Path(transcript_path).exists() else None

        if session_file.exists():
            _update_session_file(session_file, summary, today, current_time, session_metadata)
        else:
            _create_session_file(session_file, summary, today, current_time, session_metadata)

        if not stop_hook_active:
            _record_stop_event(summary, session_metadata)
        else:
            log("[SessionEnd] stop_hook_active=True (loop continuation): skip session_stop event")

        if summary and summary.get("totalMessages", 0) >= _CHECKPOINT_THRESHOLD:
            _auto_save_checkpoint(summary, session_metadata, sessions_dir)

    except Exception as err:
        log(f"[SessionEnd] Error: {err}")

    return raw_input


def main() -> int:
    """スクリプトとして実行されたときのエントリポイント"""

    try:
        raw = read_raw_stdin()
        run(raw)
        return 0
    except Exception as err:
        log(f"[SessionEnd] Error: {err}")
        return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
