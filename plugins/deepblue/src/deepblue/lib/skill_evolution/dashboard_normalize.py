"""ダッシュボード向けにスキル集合と実行レコードを正規化する補助関数群。"""

from __future__ import annotations

from typing import Any

from .skill_evolution_compat import get_value


def _iter_skills(skills: Any) -> list[dict[str, Any]]:
    """スキル集合を辞書リストへ正規化する。

    Args:
        skills: スキル集合。辞書またはリストを想定する。

    Returns:
        スキル辞書のリスト。

    Raises:
        なし。
    """
    # 未指定なら空のスキル集合として扱う。
    if skills is None:
        return []
    # 辞書形式なら values() を返し、順不同な単純リストに揃える。
    if isinstance(skills, dict):
        return list(skills.values())
    return list(skills)


def _iter_skill_items(skills_by_id: Any) -> list[tuple[str, dict[str, Any]]]:
    """スキル集合を (skill_id, skill_data) のタプル列へ正規化する。

    Args:
        skills_by_id: スキル辞書またはリスト。

    Returns:
        (スキル ID, スキルデータ) のタプルリスト。

    Raises:
        なし。
    """
    # 未指定なら空の列として返す。
    if skills_by_id is None:
        return []
    # 辞書形式なら items() をそのまま使う。
    if isinstance(skills_by_id, dict):
        return list(skills_by_id.items())

    # リスト形式では各要素から skill_id を取り出してタプル化する。
    items: list[tuple[str, dict[str, Any]]] = []
    # 各要素を順にタプル化する。
    for skill in skills_by_id:
        skill_id = get_value(skill, "skill_id", "skillId")
        # ID が無い要素は表示対象にできないため除外する。
        if skill_id is None:
            continue
        items.append((str(skill_id), skill))
    return items


def _group_records_by_skill(records: list[Any]) -> dict[str, list[Any]]:
    """実行レコードを skill_id ごとにグループ化する。

    Args:
        records: スキル実行レコードのリスト。

    Returns:
        skill_id をキーとするレコードリストの辞書。

    Raises:
        なし。
    """
    grouped: dict[str, list[Any]] = {}
    # すべてのレコードを skill_id ごとに束ねる。
    for record in records:
        skill_id = get_value(record, "skill_id", "skillId")
        # skill_id が無いレコードは、どのスキルにも紐づけられない。
        if skill_id is None:
            continue
        # setdefault で対象スキルの配列を初期化し、そのまま追加する。
        grouped.setdefault(str(skill_id), []).append(record)
    return grouped


def _collect_skill_ids(records_by_skill: dict[str, list[Any]], skill_list: list[dict[str, Any]]) -> list[str]:
    """レコード側と定義側の skill_id を統合して返す。

    Args:
        records_by_skill: skill_id をキーにしたレコードリストの辞書。
        skill_list: スキル定義のリスト。

    Returns:
        重複排除してソート済みの skill_id リスト。

    Raises:
        なし。
    """
    defined_ids: set[str] = set()
    for skill in skill_list:
        skill_id = get_value(skill, "skill_id", "skillId")
        if skill_id is not None:
            defined_ids.add(str(skill_id))
    return sorted({*records_by_skill.keys(), *defined_ids})
