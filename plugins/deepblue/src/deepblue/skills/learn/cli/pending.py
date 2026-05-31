"""保留中 instinct（TTL 管理対象）の収集と日時解析。"""

import sys
from datetime import UTC, datetime
from pathlib import Path

import deepblue.skills.learn.cli as _pkg

from .paths import ALLOWED_INSTINCT_EXTENSIONS, _all_project_dirs


def _collect_pending_dirs() -> list[Path]:
    """保留中 instinct ディレクトリをすべて返す（グローバル + 各プロジェクト）。"""
    dirs = []
    global_pending = _pkg.GLOBAL_INSTINCTS_DIR / "pending"
    if global_pending.is_dir():
        dirs.append(global_pending)
    for project_dir in _all_project_dirs():
        pending = project_dir / "instincts" / "pending"
        if pending.is_dir():
            dirs.append(pending)
    return dirs


def _parse_created_date(file_path: Path) -> datetime | None:
    """instinct ファイルの YAML フロントマターから 'created' 日時を解析する。

    'created' フィールドがない場合は、ファイルの mtime を代わりに使う。
    """
    try:
        content = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    in_frontmatter = False
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped == "---":
            if in_frontmatter:
                break  # created を見つけずにフロントマター終了
            in_frontmatter = True
            continue
        if in_frontmatter and ":" in line:
            key, value = line.split(":", 1)
            if key.strip() == "created":
                date_str = value.strip().strip('"').strip("'")
                for fmt in (
                    "%Y-%m-%dT%H:%M:%S%z",
                    "%Y-%m-%dT%H:%M:%SZ",
                    "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%d",
                ):
                    try:
                        dt = datetime.strptime(date_str, fmt)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=UTC)
                        return dt
                    except ValueError:
                        continue

    # フォールバック: ファイル更新時刻
    try:
        mtime = file_path.stat().st_mtime
        return datetime.fromtimestamp(mtime, tz=UTC)
    except OSError:
        return None


def _collect_pending_instincts() -> list[dict]:
    """保留中ディレクトリをすべて走査し、各保留 instinct の情報を返す。

    各辞書には path, created, age_days, name, parent_dir を含む。
    """
    now = datetime.now(UTC)
    results = []
    for pending_dir in _pkg._collect_pending_dirs():
        files = [
            f for f in sorted(pending_dir.iterdir()) if f.is_file() and f.suffix.lower() in ALLOWED_INSTINCT_EXTENSIONS
        ]
        for file_path in files:
            created = _pkg._parse_created_date(file_path)
            if created is None:
                print(f"Warning: could not parse age for pending instinct: {file_path.name}", file=sys.stderr)
                continue
            age = now - created
            results.append(
                {
                    "path": file_path,
                    "created": created,
                    "age_days": age.days,
                    "name": file_path.stem,
                    "parent_dir": str(pending_dir),
                }
            )
    return results
