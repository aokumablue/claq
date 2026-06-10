"""mem チャンク → learn observations.jsonl ブリッジ

セッション終了時に mem の新規チャンクを learn の observations.jsonl 形式に変換し、
インスティンクト分析の精度を高める。
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC
from pathlib import Path

from bluecore.lib.core_utils import get_bluecore_dir
from bluecore.mem.database import Database, MemoryChunk
from bluecore.mem.logger import get as _get_logger

log = _get_logger("BRIDGE")


def _project_base_dir() -> Path:
    """learn の観測 project 保存先を呼び出し時に解決して返す。"""
    return get_bluecore_dir() / "projects"


def _get_project_id(project_name: str, cwd: str | None = None) -> str:
    """プロジェクト名/cwdから決定論的なプロジェクトIDを取得する。

    learn の共有 project detection と同じ方式（git remote URL のハッシュ）を使用する。
    git が利用できない場合は project_name をそのまま返す。
    """
    check_dir = cwd or os.getcwd()
    try:
        result = subprocess.run(
            ["git", "-C", check_dir, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            import hashlib

            url = result.stdout.strip()
            return hashlib.sha256(url.encode()).hexdigest()[:12]
    except Exception:
        pass
    return project_name


def _get_project_observations_path(project_id: str, project_name: str) -> Path:
    """プロジェクトスコープの observations.jsonl パスを返す。"""
    project_dir = _project_base_dir() / project_id
    project_dir.mkdir(parents=True, exist_ok=True)

    # project.json がなければ作成
    project_json = project_dir / "project.json"
    if not project_json.exists():
        project_json.write_text(
            json.dumps({"project_id": project_id, "project_name": project_name}, indent=2),
            encoding="utf-8",
        )

    return project_dir / "observations.jsonl"


def chunk_to_observation(chunk: MemoryChunk) -> dict:
    """MemoryChunk を learn observations.jsonl エントリに変換する。"""
    # tool_complete イベントとして記録
    tool_name = chunk.tool_names[0] if chunk.tool_names else "unknown"

    # 変更ファイルがある場合はそれを output として記録
    output_parts: list[str] = []
    if chunk.files_modified:
        output_parts.append(f"files_modified: {', '.join(chunk.files_modified[:5])}")
    if chunk.files_read:
        output_parts.append(f"files_read: {', '.join(chunk.files_read[:5])}")

    return {
        "timestamp": _epoch_to_iso(chunk.created_at_epoch),
        "event": "tool_complete",
        "tool": tool_name,
        "session": chunk.session_id,
        "project_id": chunk.project,
        "project_name": chunk.project,
        # mem 由来であることを示すフラグ（learn 側で使える情報）
        "source": "mem",
        "input": (f"prompt: {chunk.user_prompt[:500]}" if chunk.user_prompt else None),
        "output": "; ".join(output_parts) if output_parts else chunk.content[:500] or None,
        # 構造化メタデータ（learn 拡張フィールド）
        "tool_names": chunk.tool_names,
        "files_modified": chunk.files_modified,
        "files_read": chunk.files_read,
    }


def sync_session_to_observations(
    db: Database,
    session_id: str,
    cwd: str | None = None,
) -> int:
    """セッション内の新規チャンクを learn observations.jsonl に書き出す。

    返り値:
      書き出したチャンク数
    """
    chunks = db.get_chunks_by_session(session_id)
    if not chunks:
        return 0

    # プロジェクト別にグループ化
    by_project: dict[str, list[MemoryChunk]] = {}
    for chunk in chunks:
        by_project.setdefault(chunk.project, []).append(chunk)

    total_written = 0
    for project_name, project_chunks in by_project.items():
        try:
            project_id = _get_project_id(project_name, cwd)
            obs_path = _get_project_observations_path(project_id, project_name)

            # 既存ファイルのサイズ制限（10MB超で処理スキップ）
            if obs_path.exists() and obs_path.stat().st_size > 10 * 1024 * 1024:
                log.warning("observations.jsonl が大きすぎるためスキップ: %s", obs_path)
                continue

            lines: list[str] = []
            for chunk in project_chunks:
                obs = chunk_to_observation(chunk)
                lines.append(json.dumps(obs, ensure_ascii=False))

            with obs_path.open("a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")

            total_written += len(lines)
            log.info("観測データ書き出し: project=%s count=%d", project_name, len(lines))

        except Exception as e:
            log.warning("観測データ書き出し失敗: project=%s error=%s", project_name, e)

    return total_written


def _epoch_to_iso(epoch: int) -> str:
    """Unix エポック秒を ISO 8601 文字列に変換する。"""
    from datetime import datetime

    dt = datetime.fromtimestamp(epoch, tz=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
