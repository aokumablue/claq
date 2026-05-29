"""devgear.skills.stocktake.core のテスト。"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from devgear.skills.stocktake import core

# ─────────────────────────────────────────────
# parse_frontmatter
# ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "content, expected_name, expected_desc",
    [
        # クォートなし
        (
            "---\nname: my-skill\ndescription: A tool.\n---\n",
            "my-skill",
            "A tool.",
        ),
        # クォートあり
        (
            '---\nname: "my-skill"\ndescription: "A tool."\n---\n',
            "my-skill",
            "A tool.",
        ),
        # 順序が逆
        (
            "---\ndescription: Desc here.\nname: reversed\n---\n",
            "reversed",
            "Desc here.",
        ),
        # フロントマター欠落
        (
            "# No frontmatter\nSome content.\n",
            "",
            "",
        ),
        # フィールド欠落（name のみ）
        (
            "---\nname: only-name\n---\n",
            "only-name",
            "",
        ),
        # 日本語説明
        (
            "---\nname: ja-skill\ndescription: 日本語の説明。\n---\n",
            "ja-skill",
            "日本語の説明。",
        ),
    ],
)
def test_parse_frontmatter(tmp_path: Path, content: str, expected_name: str, expected_desc: str) -> None:
    f = tmp_path / "SKILL.md"
    f.write_text(content, encoding="utf-8")
    name, desc = core.parse_frontmatter(f)
    assert name == expected_name
    assert desc == expected_desc


# ─────────────────────────────────────────────
# aggregate_observations
# ─────────────────────────────────────────────

_NOW = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
_CUTOFF_7D = _NOW - timedelta(days=7)
_CUTOFF_30D = _NOW - timedelta(days=30)


def _make_obs(path: str, ts: str, tool: str = "Read") -> str:
    return json.dumps({"tool": tool, "path": path, "timestamp": ts})


def test_aggregate_observations_no_file(tmp_path: Path) -> None:
    result = core.aggregate_observations(tmp_path / "obs.jsonl", _CUTOFF_7D, _CUTOFF_30D)
    assert result == {}


def test_aggregate_observations_counts(tmp_path: Path) -> None:
    obs = tmp_path / "obs.jsonl"
    # 7d 以内: 2 件、30d 以内: 3 件（7d 以内も含む）、範囲外: 1 件
    lines = [
        _make_obs("/skills/foo/SKILL.md", "2026-04-25T10:00:00Z"),  # 7d 以内
        _make_obs("/skills/foo/SKILL.md", "2026-04-20T10:00:00Z"),  # 7d 以内
        _make_obs("/skills/foo/SKILL.md", "2026-04-10T10:00:00Z"),  # 30d 以内 (7d 外)
        _make_obs("/skills/foo/SKILL.md", "2026-03-01T10:00:00Z"),  # 範囲外
    ]
    obs.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = core.aggregate_observations(obs, _CUTOFF_7D, _CUTOFF_30D)
    u7, u30 = result["/skills/foo/SKILL.md"]
    assert u7 == 2
    assert u30 == 3


def test_aggregate_observations_ignores_non_read(tmp_path: Path) -> None:
    obs = tmp_path / "obs.jsonl"
    obs.write_text(
        _make_obs("/skills/foo/SKILL.md", "2026-04-25T10:00:00Z", tool="Write") + "\n",
        encoding="utf-8",
    )
    result = core.aggregate_observations(obs, _CUTOFF_7D, _CUTOFF_30D)
    assert "/skills/foo/SKILL.md" not in result


def test_aggregate_observations_skips_broken_lines(tmp_path: Path) -> None:
    obs = tmp_path / "obs.jsonl"
    obs.write_text(
        "NOT JSON\n"
        + _make_obs("/skills/foo/SKILL.md", "2026-04-25T10:00:00Z") + "\n",
        encoding="utf-8",
    )
    result = core.aggregate_observations(obs, _CUTOFF_7D, _CUTOFF_30D)
    assert "/skills/foo/SKILL.md" in result


def test_aggregate_observations_empty_lines(tmp_path: Path) -> None:
    obs = tmp_path / "obs.jsonl"
    obs.write_text("\n\n", encoding="utf-8")
    result = core.aggregate_observations(obs, _CUTOFF_7D, _CUTOFF_30D)
    assert result == {}


def test_aggregate_observations_non_string_path_or_ts(tmp_path: Path) -> None:
    obs = tmp_path / "obs.jsonl"
    # path/timestamp が str でない（int/null）→ スキップされる
    lines = [
        json.dumps({"tool": "Read", "path": 123, "timestamp": "2026-04-25T10:00:00Z"}),
        json.dumps({"tool": "Read", "path": "/valid/path.md", "timestamp": None}),
        json.dumps({"tool": "Read", "path": "/counted.md", "timestamp": "2026-04-25T10:00:00Z"}),
    ]
    obs.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = core.aggregate_observations(obs, _CUTOFF_7D, _CUTOFF_30D)
    assert "/counted.md" in result
    assert 123 not in result
    assert "/valid/path.md" not in result


# ─────────────────────────────────────────────
# validate_evaluated_at
# ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "value, ok",
    [
        ("2026-04-26T12:00:00Z", True),
        ("2026-04-26T00:00:00Z", True),
        ("", False),
        ("null", False),
        ("2026-04-26", False),
        ("2026-04-26T12:00:00+09:00", False),
    ],
)
def test_validate_evaluated_at(value: str, ok: bool) -> None:
    if ok:
        core.validate_evaluated_at(value)  # no exception
    else:
        with pytest.raises(ValueError):
            core.validate_evaluated_at(value)


# ─────────────────────────────────────────────
# classify_changed
# ─────────────────────────────────────────────


def _make_skill(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name / "SKILL.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
    return p


def test_classify_changed_new_file(tmp_path: Path) -> None:
    f = _make_skill(tmp_path, "new-skill")
    evaluated_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

    result = core.classify_changed(set(), evaluated_at, [f], home=tmp_path)
    assert len(result) == 1
    assert result[0]["is_new"] is True


def test_classify_changed_unchanged_known(tmp_path: Path) -> None:
    f = _make_skill(tmp_path, "old-skill")
    # mtime を過去に固定できないのでファイル作成後に evaluated_at を未来に設定
    evaluated_at = datetime(2099, 1, 1, 0, 0, 0, tzinfo=UTC)

    try:
        dp = "~/" + str(f.relative_to(tmp_path))
    except ValueError:
        dp = str(f)

    result = core.classify_changed({dp}, evaluated_at, [f], home=tmp_path)
    assert result == []


def test_classify_changed_modified_known(tmp_path: Path) -> None:
    f = _make_skill(tmp_path, "mod-skill")
    # evaluated_at を過去に設定 → 最近変更されたファイルとして検出される
    evaluated_at = datetime(2000, 1, 1, 0, 0, 0, tzinfo=UTC)

    try:
        dp = "~/" + str(f.relative_to(tmp_path))
    except ValueError:
        dp = str(f)

    result = core.classify_changed({dp}, evaluated_at, [f], home=tmp_path)
    assert len(result) == 1
    assert result[0]["is_new"] is False


def test_classify_changed_partial_match_not_confused(tmp_path: Path) -> None:
    """部分一致誤検知防止: 'python-patterns' が 'python-patterns-v2' に影響しない。"""
    f = _make_skill(tmp_path, "python-patterns-v2")
    evaluated_at = datetime(2099, 1, 1, 0, 0, 0, tzinfo=UTC)

    # 'python-patterns' を既知パスとして登録（v2 は登録しない）
    known = {"~/python-patterns/SKILL.md"}
    result = core.classify_changed(known, evaluated_at, [f], home=tmp_path)
    # v2 は新規として検出される
    assert len(result) == 1
    assert result[0]["is_new"] is True
