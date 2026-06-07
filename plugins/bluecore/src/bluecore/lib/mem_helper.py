"""mem サブシステムのヘルパー関数（コマンド/スキル/エージェント向け）"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bluecore.mem.database import Database
from bluecore.mem.settings import Settings


@dataclass(frozen=True)
class RecordEventParams:
    """record_event のパラメータ（イベント種別・内容・ファイル情報・プロジェクト）。"""

    event_type: str
    content: str
    user_prompt: str = ""
    files_read: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    project: str | None = None


def search_similar_context(
    query: str,
    project: str | None = None,
    limit: int = 5,
    tool_filter: str | None = None,
    file_pattern: str | None = None,
) -> list[dict[str, Any]]:
    """関連するメモリチャンクを検索する。

    Args:
        query: 検索クエリ（セマンティック検索）
        project: プロジェクト名（省略時は現在のディレクトリ名）
        limit: 取得件数
        tool_filter: ツール名でフィルタ（例: "Edit", "Bash"）
        file_pattern: ファイルパターンでフィルタ（例: "*.py", "src/**/*.ts"）

    Returns:
        検索結果のリスト。各要素は以下のキーを持つ:
        - chunk_id: チャンクID
        - content: チャンク内容
        - user_prompt: ユーザープロンプト
        - project: プロジェクト名
        - tool_names: 使用ツール
        - files_modified: 変更ファイル
    """
    input_data: dict[str, Any] = {"query": query, "limit": limit}
    if project:
        input_data["project"] = project
    if tool_filter:
        input_data["tool_name"] = tool_filter
    if file_pattern:
        input_data["file_pattern"] = file_pattern

    cmd = "search-structured" if (tool_filter or file_pattern) else "search"
    result = _run_mem_cli(cmd, input_data)
    return result.get("results", [])


def record_event(params: RecordEventParams) -> dict[str, Any]:
    """イベントを明示的に記録する。

    Args:
        params: イベント種別・内容・ファイル情報・プロジェクトを含む RecordEventParams。

    Returns:
        記録結果 {"success": bool, "chunk_id": int | None, "error": str | None}
    """
    input_data: dict[str, Any] = {
        "event_type": params.event_type,
        "content": params.content,
        "user_prompt": params.user_prompt,
        "metadata": {
            "files_read": params.files_read,
            "files_modified": params.files_modified,
        },
    }
    if params.project:
        input_data["project"] = params.project

    return _run_mem_cli("record", input_data)


def get_project_stats(project: str | None = None, days: int = 30) -> dict[str, Any]:
    """プロジェクトの統計情報を取得する。

    Args:
        project: プロジェクト名（省略時は現在のディレクトリ名）
        days: 集計期間（日数）

    Returns:
        統計情報:
        - total_chunks: 総チャンク数
        - recent_chunks: 指定期間内のチャンク数
        - top_tools: ツール使用頻度トップ10
        - top_files: ファイル変更頻度トップ10
        - search_hit_rate: 検索ヒット率
    """
    settings = Settings.load()
    db = Database(settings.db_path)
    try:
        project_name = project or Path.cwd().resolve().name
        since_epoch = int(time.time()) - (days * 86400)
        chunks = [chunk for chunk in db.get_all_chunks() if chunk.project == project_name]
        recent_chunks = [chunk for chunk in chunks if chunk.created_at_epoch >= since_epoch]

        tool_counts: Counter = Counter()
        file_counts: Counter = Counter()
        for chunk in recent_chunks:
            tool_counts.update(chunk.tool_names)
            file_counts.update(chunk.files_modified)

        accessed_chunks = sum(1 for chunk in chunks if chunk.access_count > 0)
        hit_rate = (accessed_chunks / len(chunks) * 100) if chunks else 0

        return {
            "project": project_name,
            "days": days,
            "total_chunks": len(chunks),
            "recent_chunks": len(recent_chunks),
            "top_tools": dict(tool_counts.most_common(10)),
            "top_files": dict(file_counts.most_common(10)),
            "search_hit_rate": round(hit_rate, 1),
        }
    finally:
        db.close()


def format_context_for_prompt(
    results: list[dict[str, Any]],
    max_results: int = 5,
    max_content_length: int = 500,
) -> str:
    """検索結果をプロンプト用のコンテキストにフォーマットする。

    Args:
        results: search_similar_context() の結果
        max_results: 最大結果数
        max_content_length: 各チャンクの最大文字数

    Returns:
        フォーマットされたコンテキスト文字列
    """
    if not results:
        return ""

    lines = ["## 関連する過去の作業", ""]

    for i, result in enumerate(results[:max_results], 1):
        prompt = result.get("user_prompt", "")
        content = result.get("content", "")
        tools = result.get("tool_names", [])
        files = result.get("files_modified", [])

        lines.append(f"### {i}. {_truncate(prompt, 100)}")
        if tools:
            lines.append(f"**ツール**: {', '.join(tools)}")
        if files:
            lines.append(f"**変更ファイル**: {', '.join(files[:5])}")
        if content:
            lines.append(f"```\n{_truncate(content, max_content_length)}\n```")
        lines.append("")

    return "\n".join(lines)


def _run_mem_cli(command: str, input_data: dict[str, Any]) -> dict[str, Any]:
    """mem CLI を実行してJSON結果を返す。"""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "bluecore.mem", command],
            input=json.dumps(input_data),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
        return {"error": result.stderr or "Unknown error"}
    except subprocess.TimeoutExpired:
        return {"error": "Timeout"}
    except json.JSONDecodeError:
        return {"error": "Invalid JSON response"}
    except Exception as e:
        return {"error": str(e)}


def _truncate(text: str, max_len: int) -> str:
    """テキストを最大長で切り詰める。"""
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."
