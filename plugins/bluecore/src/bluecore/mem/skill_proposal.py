"""スキル提案レポートの生成モジュール"""

from __future__ import annotations

from typing import Any

from bluecore.mem.logger import get as _get_logger

log = _get_logger("SKILL_PROPOSAL")


def generate_proposal(
    patterns: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
) -> dict[str, Any]:
    """パターン分析とギャップ分析からスキル提案レポートを生成する。

    Args:
      patterns: detect_repeated_patterns() の結果
      gaps: detect_skill_gaps() の結果

    Returns:
      構造化された提案レポート
    """
    skill_candidates = _build_skill_candidates(patterns)
    gap_candidates = _build_gap_candidates(gaps)

    return {
        "summary": {
            "total_patterns": len(patterns),
            "total_gaps": len(gaps),
            "skill_candidates": len(skill_candidates),
            "gap_candidates": len(gap_candidates),
        },
        "skill_candidates": skill_candidates,
        "gap_candidates": gap_candidates,
        "action_items": _build_action_items(skill_candidates, gap_candidates),
    }


def _build_skill_candidates(
    patterns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """繰り返しパターンからスキル候補を構築する。

    Args:
      patterns: ツール組み合わせパターンリスト

    Returns:
      スキル候補リスト（優先度付き）
    """
    candidates = []
    for p in patterns:
        tools = p.get("tools", [])
        count = p.get("count", 0)
        users = p.get("users", [])
        projects = p.get("projects", [])

        # 優先度スコア: 出現回数 × ユーザー数 × プロジェクト数
        priority_score = count * len(users) * max(len(projects), 1)

        skill_name = _infer_skill_name(tools)

        candidates.append(
            {
                "suggested_name": skill_name,
                "tools": tools,
                "evidence": {
                    "occurrence_count": count,
                    "user_count": len(users),
                    "project_count": len(projects),
                    "users": users[:5],
                    "projects": projects[:5],
                },
                "priority_score": priority_score,
                "priority": _classify_priority(priority_score),
                "skillmaster_prompt": _build_skillmaster_prompt(skill_name, tools, count),
            }
        )

    # 優先度スコア順でソート
    candidates.sort(key=lambda x: x["priority_score"], reverse=True)
    return candidates


def _build_gap_candidates(
    gaps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """プロンプトギャップからスキル候補を構築する。

    Args:
      gaps: プロンプトパターンギャップリスト

    Returns:
      ギャップ候補リスト（サンプルプロンプト付き）
    """
    candidates = []
    for g in gaps:
        count = g.get("count", 0)
        users = g.get("users", [])
        sample = g.get("sample_prompt", "")

        candidates.append(
            {
                "sample_prompt": sample,
                "occurrence_count": count,
                "user_count": len(users),
                "users": users[:5],
                "priority": _classify_priority(count * len(users)),
                "suggestion": f"「{sample[:40]}...」のような操作をスキル化することを検討してください",
            }
        )

    return candidates[:10]


def _build_action_items(
    skill_candidates: list[dict[str, Any]],
    gap_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """アクションアイテムリストを生成する。

    Args:
      skill_candidates: スキル候補リスト
      gap_candidates: ギャップ候補リスト

    Returns:
      優先度付きアクションアイテムリスト
    """
    actions = []

    for c in skill_candidates[:3]:  # 上位3件のみアクション化
        if c["priority"] in ("high", "medium"):
            actions.append(
                {
                    "action": "create_skill",
                    "target": c["suggested_name"],
                    "description": f"ツール組み合わせ {c['tools']} を自動化するスキルを作成する（{c['evidence']['occurrence_count']}回使用）",
                    "priority": c["priority"],
                    "command": f"/skill-make でスキル '{c['suggested_name']}' を作成してください",
                }
            )

    for g in gap_candidates[:2]:  # 上位2件
        if g["occurrence_count"] >= 5:
            actions.append(
                {
                    "action": "fill_gap",
                    "target": g["sample_prompt"][:30],
                    "description": g["suggestion"],
                    "priority": g["priority"],
                    "command": f"/skill-make で以下の操作パターンをスキル化してください: {g['sample_prompt'][:60]}",
                }
            )

    return actions


def _infer_skill_name(tools: list[str]) -> str:
    """ツールリストからスキル名を推論する。

    Args:
      tools: ツール名リスト

    Returns:
      推論されたスキル名
    """
    # よく知られたツール組み合わせのマッピング
    tool_set = {t.lower() for t in tools}

    if {"bash", "write", "edit"} & tool_set and "read" in tool_set:
        return "file-workflow"
    if "bash" in tool_set and len(tool_set) == 1:
        return "shell-automation"
    if {"read", "grep", "glob"} & tool_set and not ({"edit", "write", "bash"} & tool_set):
        return "code-search"
    if {"edit", "write"} & tool_set and "bash" not in tool_set:
        return "code-edit"
    if "bash" in tool_set and {"edit", "write"} & tool_set:
        return "build-run"

    # フォールバック: ツール名を結合
    primary = tools[0].lower().replace("_", "-") if tools else "workflow"
    return primary


def _classify_priority(score: int) -> str:
    """スコアから優先度を分類する。

    Args:
      score: 優先度スコア

    Returns:
      "high" | "medium" | "low"
    """
    if score >= 20:
        return "high"
    if score >= 5:
        return "medium"
    return "low"


def _build_skillmaster_prompt(
    skill_name: str,
    tools: list[str],
    count: int,
) -> str:
    """skill-make に渡すプロンプトを生成する。

    Args:
      skill_name: スキル名
      tools: ツールリスト
      count: 使用回数

    Returns:
      skillmaster 用プロンプト文字列
    """
    tools_str = "、".join(tools[:5])
    return (
        f"以下のパターンを自動化するスキル '{skill_name}' を作成してください。\n"
        f"- 使用ツール: {tools_str}\n"
        f"- 使用回数: {count}回\n"
        f"- スキル名: {skill_name}\n"
        "このパターンを標準的なワークフローとして SKILL.md に定義してください。"
    )
