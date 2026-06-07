"""外部データ取り込みロジック（インスティンクト、ADR、イベントログ）"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

import yaml

from bluecore.lib.core_utils import get_bluecore_dir
from bluecore.mem.database import Adr, Database, EventLog, Instinct, generate_uuid
from bluecore.mem.logger import get as _get_logger

log = _get_logger("IMPORT")

# --- パス定義 ---

BLUECORE_DIR = get_bluecore_dir()
BLUECORE_STATE_DIR = get_bluecore_dir() / "state"


def _project_dirs() -> list[Path]:
    """project 保存先を返す。"""
    directory = BLUECORE_DIR / "projects"
    return [directory] if directory.exists() else []


# --- インスティンクト取り込み ---


def _import_global_instincts(db: Database, origin_user: str) -> int:
    """グローバルスコープのインスティンクト YAML を取り込む。

    Args:
        db: データベース接続
        origin_user: 同期元ユーザー識別子

    Returns:
        取り込んだインスティンクト数
    """
    count = 0
    for subdir in ("personal", "inherited"):
        d = BLUECORE_DIR / "instincts" / subdir
        if d.exists():
            count += _import_instincts_from_dir(db, d, "global", None, origin_user)
    return count


def _import_project_instincts(
    db: Database,
    origin_user: str,
    project_id: str | None,
    seen_projects: set[str],
) -> int:
    """プロジェクト単位のインスティンクト YAML を取り込む。

    Args:
        db: データベース接続
        origin_user: 同期元ユーザー識別子
        project_id: プロジェクト ID（指定時はそのプロジェクトのみ）
        seen_projects: 処理済みプロジェクト名の集合（重複スキップ用）

    Returns:
        取り込んだインスティンクト数
    """
    count = 0
    for projects_dir in _project_dirs():
        for proj_dir in projects_dir.iterdir():
            if not proj_dir.is_dir():
                continue
            if proj_dir.name in seen_projects:
                continue
            seen_projects.add(proj_dir.name)
            if project_id and proj_dir.name != project_id:
                continue
            for subdir in ("personal", "inherited"):
                instincts_dir = proj_dir / "instincts" / subdir
                if instincts_dir.exists():
                    count += _import_instincts_from_dir(db, instincts_dir, "project", proj_dir.name, origin_user)
    return count


def import_instincts(db: Database, origin_user: str, project_id: str | None = None) -> int:
    """インスティンクト YAML ファイルを mem に取り込む。

    Args:
        db: データベース接続
        origin_user: 同期元ユーザー識別子
        project_id: プロジェクト ID（指定時はそのプロジェクトのみ）

    Returns:
        取り込んだインスティンクト数
    """
    count = 0
    if project_id is None:
        count += _import_global_instincts(db, origin_user)
    seen_projects: set[str] = set()
    count += _import_project_instincts(db, origin_user, project_id, seen_projects)
    return count


def _import_instincts_from_dir(
    db: Database,
    directory: Path,
    scope: str,
    project_id: str | None,
    origin_user: str,
) -> int:
    """ディレクトリ内のインスティンクト YAML を取り込む。"""
    count = 0
    for pattern in ("*.yaml", "*.yml"):
        for f in directory.glob(pattern):
            try:
                instinct = _parse_instinct_yaml(f, scope, project_id, origin_user)
                if instinct:
                    db.upsert_instinct(instinct)
                    count += 1
            except Exception as e:
                log.warning("インスティンクト取り込み失敗 %s: %s", f, e)
    return count


def _parse_instinct_yaml(file_path: Path, scope: str, project_id: str | None, origin_user: str) -> Instinct | None:
    """インスティンクト YAML をパースする。"""
    content = file_path.read_text(encoding="utf-8")

    # フロントマター形式（---で囲まれた YAML）
    frontmatter_match = re.match(r"^---\s*\n(.+?)\n---\s*\n?(.*)$", content, re.DOTALL)
    if frontmatter_match:
        try:
            meta = yaml.safe_load(frontmatter_match.group(1))
        except yaml.YAMLError:
            meta = {}
    else:
        # 全体が YAML の場合
        try:
            meta = yaml.safe_load(content)
        except yaml.YAMLError:
            return None

    if not isinstance(meta, dict):
        return None

    instinct_id = meta.get("id") or file_path.stem
    if not instinct_id:
        return None

    stat = file_path.stat()
    return Instinct(
        id=generate_uuid(),
        origin_user=origin_user,
        instinct_id=instinct_id,
        scope=scope,
        project_id=project_id,
        trigger_text=meta.get("trigger"),
        confidence=float(meta.get("confidence", 0.5)),
        domain=meta.get("domain"),
        content=content,
        created_at_epoch=int(stat.st_ctime),
        updated_at_epoch=int(stat.st_mtime),
    )


# --- ADR 取り込み ---


def import_adrs(db: Database, origin_user: str, repo_root: str | Path | None = None) -> int:
    """ADR Markdown ファイルを mem に取り込む。

    Args:
        db: データベース接続
        origin_user: 同期元ユーザー識別子
        repo_root: リポジトリルート（None の場合はカレントディレクトリ）

    Returns:
        取り込んだ ADR 数
    """
    if repo_root is None:
        repo_root = Path.cwd()
    else:
        repo_root = Path(repo_root)

    adr_dir = repo_root / "docs" / "adr"
    if not adr_dir.exists():
        return 0

    project = _get_project_identifier(repo_root)
    count = 0

    for f in adr_dir.glob("*.md"):
        if f.name.lower() in ("readme.md", "template.md"):
            continue
        try:
            adr = _parse_adr_markdown(f, project, origin_user)
            if adr:
                db.upsert_adr(adr)
                count += 1
        except Exception as e:
            log.warning("ADR 取り込み失敗 %s: %s", f, e)

    return count


def _parse_adr_markdown(file_path: Path, project: str, origin_user: str) -> Adr | None:
    """ADR Markdown をパースする。"""
    content = file_path.read_text(encoding="utf-8")

    # ファイル名から番号を抽出（例: 0001-use-nextjs.md）
    name = file_path.stem
    number_match = re.match(r"^(\d+)", name)
    if number_match:
        adr_number = int(number_match.group(1))
    else:
        return None

    # タイトル抽出（最初の # 行）
    title_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    if title_match:
        title = title_match.group(1).strip()
        # ADR-NNNN: プレフィックスを除去
        title = re.sub(r"^ADR-?\d+:\s*", "", title, flags=re.IGNORECASE)
    else:
        title = name

    # ステータス抽出
    status_match = re.search(
        r"\*\*ステータス\*\*:\s*(\w+)|\*\*Status\*\*:\s*(\w+)",
        content,
        re.IGNORECASE,
    )
    if status_match:
        status = (status_match.group(1) or status_match.group(2)).lower()
    else:
        status = "accepted"

    stat = file_path.stat()
    return Adr(
        id=generate_uuid(),
        origin_user=origin_user,
        project=project,
        adr_number=adr_number,
        title=title,
        status=status,
        content=content,
        created_at_epoch=int(stat.st_ctime),
        updated_at_epoch=int(stat.st_mtime),
    )


def _get_project_identifier(repo_root: Path) -> str:
    """リポジトリのプロジェクト識別子を取得する。"""
    # git remote URL から生成
    git_dir = repo_root / ".git"
    if git_dir.exists():
        config_file = git_dir / "config"
        if config_file.exists():
            try:
                content = config_file.read_text(encoding="utf-8")
                url_match = re.search(r"url\s*=\s*(.+)", content)
                if url_match:
                    url = url_match.group(1).strip()
                    return hashlib.sha256(url.encode()).hexdigest()[:12]
            except Exception:
                pass

    # フォールバック: ディレクトリ名
    return repo_root.name


# --- イベントログ取り込み ---


def _import_global_event_logs(db: Database, origin_user: str) -> int:
    """グローバルスコープのイベントログ（observations, skill-runs, costs）を取り込む。

    Args:
        db: データベース接続
        origin_user: 同期元ユーザー識別子

    Returns:
        取り込んだイベント数
    """
    count = 0
    global_obs = BLUECORE_DIR / "observations.jsonl"
    if global_obs.exists():
        count += _import_jsonl_events(db, global_obs, "observation", None, origin_user)
    skill_runs = BLUECORE_STATE_DIR / "skill-runs.jsonl"
    if skill_runs.exists():
        count += _import_jsonl_events(db, skill_runs, "skill-run", None, origin_user)
    costs_file = BLUECORE_DIR / "logs" / "costs.jsonl"
    if costs_file.exists():
        count += _import_jsonl_events(db, costs_file, "cost", None, origin_user)
    return count


def _import_project_event_logs(
    db: Database,
    origin_user: str,
    project_id: str | None,
    seen_projects: set[str],
) -> int:
    """プロジェクト単位の observations を取り込む。

    Args:
        db: データベース接続
        origin_user: 同期元ユーザー識別子
        project_id: プロジェクト ID（指定時はそのプロジェクトのみ）
        seen_projects: 処理済みプロジェクト名の集合（重複スキップ用）

    Returns:
        取り込んだイベント数
    """
    count = 0
    for projects_dir in _project_dirs():
        for proj_dir in projects_dir.iterdir():
            if not proj_dir.is_dir():
                continue
            if proj_dir.name in seen_projects:
                continue
            seen_projects.add(proj_dir.name)
            if project_id and proj_dir.name != project_id:
                continue
            obs_file = proj_dir / "observations.jsonl"
            if obs_file.exists():
                count += _import_jsonl_events(db, obs_file, "observation", proj_dir.name, origin_user)
    return count


def import_event_logs(db: Database, origin_user: str, project_id: str | None = None) -> int:
    """イベントログ（observations, skill-runs, costs）を mem に取り込む。

    Args:
        db: データベース接続
        origin_user: 同期元ユーザー識別子
        project_id: プロジェクト ID（指定時はそのプロジェクトのみ）

    Returns:
        取り込んだイベント数
    """
    count = 0
    if project_id is None:
        count += _import_global_event_logs(db, origin_user)
    seen_projects: set[str] = set()
    count += _import_project_event_logs(db, origin_user, project_id, seen_projects)
    return count


def _parse_ts_to_epoch(ts: object) -> int:
    """タイムスタンプ値をエポック秒に変換する（ISO 文字列・数値に対応）。"""
    if isinstance(ts, str):
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return int(dt.timestamp())
        except Exception:
            return int(time.time())
    if isinstance(ts, (int, float)):
        return int(ts)
    return int(time.time())


def _build_event_log(
    data: dict,
    event_type: str,
    project_id: str | None,
    origin_user: str,
) -> EventLog:
    """JSONL 行データから EventLog オブジェクトを構築する。"""
    ts = data.get("timestamp") or data.get("ts") or data.get("created_at")
    epoch = _parse_ts_to_epoch(ts)
    content_str = json.dumps(data, sort_keys=True, ensure_ascii=False)
    content_hash = hashlib.sha256(content_str.encode()).hexdigest()[:16]
    return EventLog(
        id=f"{event_type}-{epoch}-{content_hash}",
        origin_user=origin_user,
        event_type=event_type,
        project_id=project_id,
        content=content_str,
        created_at_epoch=epoch,
    )


def _import_jsonl_events(
    db: Database,
    file_path: Path,
    event_type: str,
    project_id: str | None,
    origin_user: str,
) -> int:
    """JSONL ファイルからイベントを取り込む。"""
    count = 0
    try:
        with open(file_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    event = _build_event_log(data, event_type, project_id, origin_user)
                    db.store_event_log(event)
                    count += 1
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        log.warning("イベントログ取り込み失敗 %s: %s", file_path, e)
    return count


# --- 一括取り込み ---


def import_all(
    db: Database,
    origin_user: str,
    repo_root: str | Path | None = None,
    project_id: str | None = None,
) -> dict[str, int]:
    """全データを取り込む。

    Args:
        db: データベース接続
        origin_user: 同期元ユーザー識別子
        repo_root: リポジトリルート（ADR 用）
        project_id: プロジェクト ID（指定時はそのプロジェクトのみ）

    Returns:
        取り込み結果 {"instincts": N, "adrs": N, "events": N}
    """
    return {
        "instincts": import_instincts(db, origin_user, project_id),
        "adrs": import_adrs(db, origin_user, repo_root),
        "events": import_event_logs(db, origin_user, project_id),
    }
