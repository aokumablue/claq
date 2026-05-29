"""スキル棚卸しのコアロジック（純関数）。"""

import json
import re
from datetime import UTC, datetime
from pathlib import Path


def parse_frontmatter(path: Path) -> tuple[str, str]:
    """SKILL.md から name と description を抽出する。

    フロントマターの開始/終了 `---` ブロック内の単一行値のみ対応。
    クォートあり・なし両方を処理する。値が見つからない場合は空文字列を返す。
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    in_fm = False
    fm_count = 0
    name = ""
    desc = ""

    for line in lines:
        if line.strip() == "---":
            fm_count += 1
            in_fm = fm_count == 1
            if fm_count >= 2:
                break
            continue

        if not in_fm:
            continue

        for field, storage in (("name", "name"), ("description", "description")):
            prefix = f"{field}: "
            if line.startswith(prefix):
                val = line[len(prefix):]
                # クォート除去（"value" → value）
                val = val.strip()
                if val.startswith('"') and val.endswith('"'):
                    val = val[1:-1]
                if storage == "name":
                    name = val
                else:
                    desc = val

    return name, desc


def aggregate_observations(
    obs_file: Path,
    cutoff_7d: datetime,
    cutoff_30d: datetime,
) -> dict[str, tuple[int, int]]:
    """observations.jsonl を 1 パスで読み、path ごとの (use_7d, use_30d) を返す。

    tool == "Read" かつ timestamp >= cutoff の行を集計する。
    壊れ行（JSON パースエラー）はスキップする。
    cutoff_7d / cutoff_30d は UTC の datetime オブジェクトを渡すこと。
    """
    counts_7d: dict[str, int] = {}
    counts_30d: dict[str, int] = {}

    if not obs_file.is_file():
        return {}

    # ISO 8601 文字列比較は字句順 == 時間順なので有効
    c7 = cutoff_7d.strftime("%Y-%m-%dT%H:%M:%SZ")
    c30 = cutoff_30d.strftime("%Y-%m-%dT%H:%M:%SZ")

    with obs_file.open(encoding="utf-8", errors="replace") as fh:
        for raw_line in fh:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                entry = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            if entry.get("tool") != "Read":
                continue

            p = entry.get("path", "")
            ts = entry.get("timestamp", "")

            if not isinstance(p, str) or not isinstance(ts, str):
                continue

            if ts >= c30:
                counts_30d[p] = counts_30d.get(p, 0) + 1
                if ts >= c7:
                    counts_7d[p] = counts_7d.get(p, 0) + 1

    all_paths = set(counts_7d) | set(counts_30d)
    return {p: (counts_7d.get(p, 0), counts_30d.get(p, 0)) for p in all_paths}


_ISO8601_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def validate_evaluated_at(value: str) -> None:
    """evaluated_at の形式を検証する。不正な場合は ValueError を送出する。"""
    if not _ISO8601_RE.match(value):
        raise ValueError(f"invalid evaluated_at format: {value!r}")


def classify_changed(
    known_paths: set[str],
    evaluated_at: datetime,
    skill_files: list[Path],
    home: Path | None = None,
) -> list[dict]:
    """スキルファイルのリストを変更済み/新規に分類して返す。

    bash 版との一致: mtime を int 秒に切り詰めて ISO 8601 文字列比較する。
    evaluated_at は UTC の datetime を渡すこと。
    home は ~ 置換のベース（省略時は Path.home()）。
    """
    if home is None:
        home = Path.home()

    # evaluated_at を int 秒にして文字列変換（bash の ISO 8601 比較と等価）
    ea_str = evaluated_at.strftime("%Y-%m-%dT%H:%M:%SZ")

    result = []
    for f in skill_files:
        mtime_sec = int(f.stat().st_mtime)
        mtime_str = datetime.fromtimestamp(mtime_sec, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        # ~ 表現への変換
        try:
            dp = "~/" + str(f.relative_to(home))
        except ValueError:
            dp = str(f)

        if dp in known_paths:
            # 既知ファイル: mtime が evaluated_at より新しい場合のみ報告
            if mtime_str > ea_str:
                result.append({"path": dp, "mtime": mtime_str, "is_new": False})
        else:
            # 新規ファイル: 常に報告
            result.append({"path": dp, "mtime": mtime_str, "is_new": True})

    return result
