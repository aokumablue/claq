"""エージェント定義に書かれた実行規則を、定義本文から抽出して検査する構造テスト。

``agents/reviewer.md`` の signature → argv の adapter 表は、プロンプト文書に書かれて
いるだけで誰も実行しないため、書き間違えても気付けない。定義本文から表を抽出して
期待する形を確かめる。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_REVIEWER = _ROOT / "agents" / "reviewer.md"

# reviewer.md の signature → argv adapter 表。ランナーごとに連結形式が異なるため、
# 一律 `-- <signature>` を当てると node/go で偽 GREEN か偽 BLOCKER になる。
_ADAPTER_TABLE_RE = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*(.+?)\s*\|$", re.MULTILINE)


@pytest.mark.parametrize(
    ("runner", "required_form", "dash_dash_allowed"),
    [("pytest", "--", True), ("go test", "-run", False), ("node --test", "--test-name-pattern", False)],
)
def test_reviewer_declares_runner_specific_signature_adapters(
    runner: str, required_form: str, dash_dash_allowed: bool
) -> None:
    """adapter 表が各ランナー固有の選択フラグを指定し、`--` 連結を通用しないランナーへ当てていないこと。

    定義本文へ書いた検証規則は CI で実測する。この表は誰も実行しないプロンプト文書の
    契約の典型で、書き間違えても気づけない。`node --test -- <name>` は name をファイル
    位置引数として解釈するため、全件実行による偽 GREEN か、存在しないファイルによる
    偽 BLOCKER になる。`go test` も同様に `-run` が要る。
    """
    rows = dict(_ADAPTER_TABLE_RE.findall(_REVIEWER.read_text(encoding="utf-8")))

    assert runner in rows, f"adapter 表に {runner} の行がない"
    assert required_form in rows[runner], f"{runner} の argv 形式に {required_form} が含まれていない: {rows[runner]}"
    if not dash_dash_allowed:
        assert not rows[runner].strip().startswith("`<test_cmd>` --"), (
            f"{runner} に `--` 連結を割り当てている: {rows[runner]}"
        )


def test_reviewer_declares_skip_for_unsupported_runners() -> None:
    """表に無いランナーでは個別再実行を skip する旨が書かれていること。

    未対応ランナーへ既定形式を当てるより、全体実行の結果だけを使う方が安全側。
    """
    body = _REVIEWER.read_text(encoding="utf-8")

    assert "上記以外" in body
    assert "skip" in body


def _section(relative_path: str, heading: str) -> str:
    """定義ファイルから ``## {heading}`` 節の本文（次の ``## `` 見出しの直前まで）を取り出す。"""
    body = (_ROOT / relative_path).read_text(encoding="utf-8")
    marker = f"\n## {heading}"
    assert marker in body, f"{relative_path} に「## {heading}」節が見つからない"
    return body.split(marker, 1)[1].split("\n## ", 1)[0]


@pytest.mark.parametrize(
    ("relative_path", "heading", "phrases"),
    [
        # 境界は git 履歴が本体の文脈へ入る時点（ステップ1）から張る。
        pytest.param("commands/skill-gen.md", "ステップ1", ("データであり指示ではない",), id="skill-gen-step1"),
        pytest.param(
            "commands/skill-gen.md",
            "ステップ3",
            ("ランダム ID", "開始・終了タグ", "偽装", "大小無視", "データであり指示ではない"),
            id="skill-gen-step3",
        ),
        pytest.param("skills/skill-make/SKILL.md", "入力安全", ("git 履歴", "件名", "転記しない"), id="skill-make"),
    ],
)
def test_git_history_handed_to_skill_make_is_bounded_as_data(
    relative_path: str, heading: str, phrases: tuple[str, ...]
) -> None:
    """skill-gen が git 履歴（第三者が書ける件名・パス）を境界付きで skill-make へ渡し、skill-make がそれを入力安全の対象にすること。

    `collect_skill_create_inputs` の出力をそのまま渡すと、コミット件名に埋め込んだ
    指示が生成される SKILL.md の方針へ混ざる。毎回生成するランダム ID 付きの
    開始・終了タグで囲み、囲いを偽装するタグを除き、データであって指示ではないと示す。
    """
    section = _section(relative_path, heading)

    assert [phrase for phrase in phrases if phrase not in section] == []
