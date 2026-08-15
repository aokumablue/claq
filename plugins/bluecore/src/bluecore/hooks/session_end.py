#!/usr/bin/env python3
"""長い作業ループの再開点を保存する Stop フック。

Stop イベント（各応答後）に走る。stdin JSON の ``transcript_path`` から
ユーザー依頼数と変更ファイルを集計し、依頼数が ``_CHECKPOINT_THRESHOLD`` を
超えたときだけ ``checkpoint-<日付>-<プロジェクト>.md`` を自動保存・更新する。

**役割分担**: 次セッションへの引き継ぎ本文（``sessions.handoff``）は
SessionEnd フックの ``bluecore.mem.cli handoff`` だけが書く。本フックは
引き継ぎを一切書かない（同じ情報を 2 か所で管理しないため）。checkpoint は
「セッション途中で中断した長い反復ループを再開する」ための別物で、
SessionStart が ``Active checkpoint`` として読み出す。

``stop_hook_active``（ループ継続中の中間停止）による分岐は持たない。
checkpoint の自動保存は同じファイルを冪等に更新するだけで、むしろ
ループ継続中こそ最新化したい処理のため、中間停止でも実行する。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from bluecore.hooks.hook_common import parse_json_object, read_raw_stdin
from bluecore.lib.core_utils import (
    get_date_string,
    get_project_name,
    get_sessions_dir,
    log,
    read_file,
    run_command,
    strip_ansi,
    write_file,
)
from bluecore.lib.harness import extract_file_paths, normalize_tool_name
from bluecore.lib.slim_text import compact_line

_EDIT_TOOLS = ("Edit", "Write", "MultiEdit")
"""ファイルパスを収集する対象となる正規化済みツール名。"""

_USER_MESSAGE_CHAR_LIMIT = 200
"""ユーザー依頼 1 件を圧縮する上限文字数。件数のカウントにのみ使う。"""

_CHECKPOINT_THRESHOLD = 30
"""checkpoint を自動保存し始めるユーザー依頼の件数。"""

_CHECKPOINT_CONTEXT_MAX = 500
"""checkpoint の再開コンテキスト行の上限文字数。"""

_FILE_LIST_LIMIT = 30
"""checkpoint に載せる変更ファイルの件数。"""


def _content_as_text(raw_content: object) -> str | None:
    """トランスクリプト content フィールドをプレーンテキストにする。

    Args:
        raw_content: message.content または entry.content。

    Returns:
        文字列化した本文。str/list 以外は None。

    Raises:
        例外は発生しません。
    """
    if isinstance(raw_content, str):
        return raw_content
    if isinstance(raw_content, list):
        return " ".join(str(c.get("text", "")) if isinstance(c, dict) else "" for c in raw_content)
    return None


def _collect_user_message(entry: dict) -> str:
    """トランスクリプトエントリからユーザーメッセージ本文を抽出する。

    Args:
        entry: トランスクリプトの 1 エントリ。

    Returns:
        圧縮済みの発話本文。ユーザー発話でない、または中身が空なら空文字列。
    """
    message = entry.get("message")
    message = message if isinstance(message, dict) else {}
    if "user" not in (entry.get("type"), entry.get("role"), message.get("role")):
        return ""
    text = _content_as_text(message.get("content") or entry.get("content"))
    if text is None:
        return ""
    return compact_line(strip_ansi(text).strip(), _USER_MESSAGE_CHAR_LIMIT)


def _record_modified_files(tool_name: str, tool_input: object, files_modified: set) -> None:
    """編集系ツールの呼び出しから対象ファイルパスを収集する。

    Codex の apply_patch は Edit へ正規化し、パッチテキストから全対象
    ファイルを抽出する。

    Args:
        tool_name: トランスクリプト上のツール名（正規化前）。
        tool_input: ツール入力。dict 以外は空入力として扱う。
        files_modified: 変更ファイルパスの収集先。
    """
    if not tool_name or normalize_tool_name(tool_name) not in _EDIT_TOOLS:
        return
    paths = extract_file_paths(tool_name, tool_input if isinstance(tool_input, dict) else {})
    files_modified.update(paths or [])


def _iter_assistant_tool_uses(entry: dict) -> list[dict]:
    """assistant エントリ内の tool_use ブロックを返す。

    Args:
        entry: トランスクリプトの 1 エントリ。

    Returns:
        tool_use ブロックのリスト。該当しなければ空リスト。

    Raises:
        例外は発生しません。
    """
    if entry.get("type") != "assistant":
        return []
    content = entry.get("message", {}).get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict) and block.get("type") == "tool_use"]


def _collect_modified_files(entry: dict, files_modified: set) -> None:
    """直接の tool_use エントリと assistant ブロックの両方からファイルパスを集める。

    Args:
        entry: トランスクリプトの 1 エントリ。
        files_modified: 変更ファイルパスの収集先。
    """
    if entry.get("type") == "tool_use" or entry.get("tool_name"):
        tool_name = entry.get("tool_name") or entry.get("name") or ""
        tool_input = entry.get("tool_input") or entry.get("input") or {}
        _record_modified_files(tool_name, tool_input, files_modified)

    for block in _iter_assistant_tool_uses(entry):
        _record_modified_files(block.get("name", ""), block.get("input") or {}, files_modified)


def extract_session_summary(transcript_path: str) -> dict | None:
    """セッショントランスクリプトから checkpoint の材料を抽出する。

    Args:
        transcript_path: トランスクリプト（JSONL）のパス。

    Returns:
        ``filesModified`` と ``totalMessages`` を持つ dict。
        読めない、またはユーザー発話が 1 件も無ければ None。
    """
    content = read_file(transcript_path)
    if not content:
        return None

    lines = content.split("\n")
    total_messages = 0
    files_modified: set[str] = set()
    parse_errors = 0

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if _collect_user_message(entry):
                total_messages += 1
            _collect_modified_files(entry, files_modified)
        except json.JSONDecodeError:
            parse_errors += 1

    if parse_errors > 0:
        log(f"[SessionEnd] Skipped {parse_errors}/{len(lines)} unparseable transcript lines")

    if total_messages == 0:
        return None

    return {
        "filesModified": sorted(files_modified)[:_FILE_LIST_LIMIT],
        "totalMessages": total_messages,
    }


def get_session_metadata() -> dict:
    """checkpoint に載せるプロジェクト名とブランチ名を取得する。

    Returns:
        ``project`` と ``branch`` を持つ dict。取得できない項目は ``unknown``。
    """
    branch_result = run_command("git rev-parse --abbrev-ref HEAD")

    return {
        "project": get_project_name() or "unknown",
        "branch": branch_result["output"] if branch_result["success"] and branch_result["output"] else "unknown",
    }


def _new_checkpoint_body(project: str, files_section: str, context_hint: str) -> str:
    """新規 checkpoint マークダウン本文を組み立てる。

    Args:
        project: プロジェクト名（task frontmatter に最大 20 文字）。
        files_section: 変更済みファイルの箇条書き。
        context_hint: 再開コンテキスト行。

    Returns:
        checkpoint ファイルの全文。

    Raises:
        例外は発生しません。
    """
    return (
        f"---\ntask: {project[:20]}\ncompleted: false\n---\n\n"
        f"## 目標\n(セッション継続のための自動チェックポイント)\n\n"
        f"## 完了済みステップ\n- (セッション終了時点まで)\n\n"
        f"## 進行中\n- [ ] 次のステップを確認してください\n\n"
        f"## 残りステップ\n- [ ] (次セッションで確認)\n\n"
        f"## 変更済みファイル\n{files_section}\n\n"
        f"## 再開コンテキスト\n{context_hint}\n"
    )


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
        write_file(checkpoint_path, _new_checkpoint_body(project, files_section, context_hint))
        log(f"[SessionEnd] Created auto-checkpoint: {checkpoint_path}")


def run(raw_input: str) -> None:
    """Stop フック本体。閾値を超えたセッションの checkpoint を自動保存する。

    Args:
        raw_input: フックに渡された生の stdin（``transcript_path`` を含む JSON）。
    """
    try:
        input_data = parse_json_object(raw_input)
        transcript_path = input_data.get("transcript_path") if input_data else None
        if not transcript_path:
            return
        if not Path(transcript_path).exists():
            log(f"[SessionEnd] Transcript not found: {transcript_path}")
            return

        summary = extract_session_summary(transcript_path)
        if summary and summary["totalMessages"] >= _CHECKPOINT_THRESHOLD:
            _auto_save_checkpoint(summary, get_session_metadata(), get_sessions_dir())

    except Exception as err:
        log(f"[SessionEnd] Error: {err}")


def main() -> int:
    """スクリプトとして実行されたときのエントリポイント。

    Returns:
        常に 0（フックをブロックしない）。
    """
    try:
        run(read_raw_stdin())
    except Exception as err:
        log(f"[SessionEnd] Error: {err}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
