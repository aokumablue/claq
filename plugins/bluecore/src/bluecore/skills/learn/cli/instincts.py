"""instinct ファイルの解析と読み込み。

``_load_instincts_from_dir`` と ``load_all_instincts`` は ``cli`` 名前空間で
``monkeypatch`` 差し替えされるため、相互参照は ``_pkg`` 経由で行う。
"""

import re
import sys
from collections import defaultdict
from pathlib import Path

import bluecore.skills.learn.cli as _pkg

from .paths import ALLOWED_INSTINCT_EXTENSIONS


def _unescape_yaml_value(value: str) -> str:
    """YAML フロントマター中のクォートされた文字列をアンエスケープする。"""
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    return value


def _parse_frontmatter_line(line: str, current: dict) -> None:
    """フロントマター行を解析して current 辞書に書き込む。"""
    if ":" not in line:
        return
    key, value = line.split(":", 1)
    key = key.strip()
    value = _unescape_yaml_value(value.strip())
    if key == "confidence":
        try:
            current[key] = float(value)
        except ValueError:
            current[key] = 0.5
    else:
        current[key] = value


def parse_instinct_file(content: str) -> list[dict]:
    """YAML 風の instinct ファイル形式を解析する。

    各 instinct は ``---``（YAML フロントマター）2 つで区切られる。
    注意: ``---`` は常にフロントマター境界として扱うため、本文中の区切り線は
    曖昧さ回避のため ``***`` または ``___`` を使うこと。
    """
    instincts = []
    current: dict = {}
    in_frontmatter = False
    content_lines: list[str] = []

    for line in content.split("\n"):
        if line.strip() == "---":
            if in_frontmatter:
                in_frontmatter = False
            else:
                in_frontmatter = True
                if current:
                    current["content"] = "\n".join(content_lines).strip()
                    instincts.append(current)
                current = {}
                content_lines = []
        elif in_frontmatter:
            _parse_frontmatter_line(line, current)
        else:
            content_lines.append(line)

    if current:
        current["content"] = "\n".join(content_lines).strip()
        instincts.append(current)

    return [i for i in instincts if i.get("id")]


def _load_instincts_from_dir(directory: Path, source_type: str, scope_label: str) -> list[dict]:
    """単一ディレクトリから instinct を読み込む。"""
    instincts = []
    if not directory.exists():
        return instincts
    files = [
        file
        for file in sorted(directory.iterdir())
        if file.is_file() and file.suffix.lower() in ALLOWED_INSTINCT_EXTENSIONS
    ]
    for file in files:
        try:
            content = file.read_text(encoding="utf-8")
            parsed = parse_instinct_file(content)
            for inst in parsed:
                inst["_source_file"] = str(file)
                inst["_source_type"] = source_type
                inst["_scope_label"] = scope_label
                # フロントマターで scope 未指定なら既定値を設定
                if "scope" not in inst:
                    inst["scope"] = scope_label
            instincts.extend(parsed)
        except Exception as e:
            print(f"Warning: Failed to parse {file}: {e}", file=sys.stderr)
    return instincts


def load_all_instincts(project: dict, include_global: bool = True) -> list[dict]:
    """すべての instinct を読み込む（プロジェクトスコープ + グローバル）。

    ID が衝突した場合は、プロジェクトスコープの instinct を優先する。
    """
    instincts = []

    # 1. プロジェクトスコープ instinct を読み込む（すでに global でなければ）
    if project["id"] != "global":
        instincts.extend(_pkg._load_instincts_from_dir(project["instincts_personal"], "personal", "project"))
        instincts.extend(_pkg._load_instincts_from_dir(project["instincts_inherited"], "inherited", "project"))

    # 2. グローバル instinct を読み込む
    if include_global:
        global_instincts = []
        global_instincts.extend(_pkg._load_instincts_from_dir(_pkg.GLOBAL_PERSONAL_DIR, "personal", "global"))
        global_instincts.extend(_pkg._load_instincts_from_dir(_pkg.GLOBAL_INHERITED_DIR, "inherited", "global"))

        # 重複排除: 同一 ID の場合はプロジェクトスコープを優先
        project_ids = {i.get("id") for i in instincts}
        for gi in global_instincts:
            if gi.get("id") not in project_ids:
                instincts.append(gi)

    return instincts


def load_project_only_instincts(project: dict) -> list[dict]:
    """プロジェクトスコープの instinct のみを読み込む（グローバルは含めない）。

    グローバルフォールバックモード（git プロジェクトなし）では、
    グローバル instinct を返す。
    """
    if project.get("id") == "global":
        instincts = _pkg._load_instincts_from_dir(_pkg.GLOBAL_PERSONAL_DIR, "personal", "global")
        instincts += _pkg._load_instincts_from_dir(_pkg.GLOBAL_INHERITED_DIR, "inherited", "global")
        return instincts
    return _pkg.load_all_instincts(project, include_global=False)


def _print_instincts_by_domain(instincts: list[dict]) -> None:
    """ドメインごとにグループ化した instinct を表示する補助関数。"""
    by_domain = defaultdict(list)
    for inst in instincts:
        domain = inst.get("domain", "general")
        by_domain[domain].append(inst)

    for domain in sorted(by_domain.keys()):
        domain_instincts = by_domain[domain]
        print(f"  ### {domain.upper()} ({len(domain_instincts)})")
        print()

        for inst in sorted(domain_instincts, key=lambda x: -x.get("confidence", 0.5)):
            conf = inst.get("confidence", 0.5)
            conf_bar = "█" * int(conf * 10) + "░" * (10 - int(conf * 10))
            trigger = inst.get("trigger", "unknown trigger")
            scope_tag = f"[{inst.get('scope', '?')}]"

            print(f"    {conf_bar} {int(conf * 100):3d}%  {inst.get('id', 'unnamed')} {scope_tag}")
            print(f"              trigger: {trigger}")

            # 本文から action を抽出
            content = inst.get("content", "")
            action_match = re.search(r"## Action\s*\n\s*(.+?)(?:\n\n|\n##|$)", content, re.DOTALL)
            if action_match:
                action = action_match.group(1).strip().split("\n")[0]
                print(f"              action: {action[:60]}{'...' if len(action) > 60 else ''}")

            print()
