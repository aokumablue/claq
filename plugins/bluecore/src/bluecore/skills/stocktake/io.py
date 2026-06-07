"""スキル棚卸しの I/O 境界層。"""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path


def walk_skills(directory: Path) -> list[Path]:
    """ディレクトリ以下の *.md ファイルをソート順で返す。

    シンボリックリンクの追跡は行わない（ループ防止）。
    ディレクトリが存在しない場合は空リストを返す。
    """
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.rglob("*.md") if p.is_file() and not p.is_symlink()
    )


def read_results(results_path: Path) -> dict | None:
    """results.json を読んで辞書を返す。ファイルが存在しない場合は None を返す。"""
    if not results_path.is_file():
        return None
    with results_path.open(encoding="utf-8") as fh:
        return json.load(fh)


def atomic_write(results_path: Path, data: dict) -> None:
    """data を JSON として results_path に原子的に書き込む。

    同一ファイルシステム上に一時ファイルを作成し、os.replace() で置き換える。
    """
    results_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=results_path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp_path, results_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def merge_results(existing: dict | None, new_data: dict, now: datetime) -> dict:
    """new_data を existing にマージして返す。

    - evaluated_at は now で上書き
    - skills は existing + new_data（new_data が優先）
    - mode / batch_progress は new_data に存在する場合のみ上書き
    """
    ea = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    if existing is None:
        merged = dict(new_data)
        merged["evaluated_at"] = ea
        return merged

    merged = dict(existing)
    merged["evaluated_at"] = ea
    merged["skills"] = {**(existing.get("skills") or {}), **(new_data.get("skills") or {})}
    if "mode" in new_data:
        merged["mode"] = new_data["mode"]
    if "batch_progress" in new_data:
        merged["batch_progress"] = new_data["batch_progress"]
    return merged
