"""エージェント定義に書かれた実行規則を、定義本文から抽出して検査する構造テスト。

``agents/reviewer.md`` の signature → argv の adapter 表は、プロンプト文書に書かれて
いるだけで誰も実行しないため、書き間違えても気付けない。定義本文から表を抽出して
期待する形を確かめる。
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

# reviewer.md の signature → argv adapter 表。ランナーごとに連結形式が異なるため、
# 一律 `-- <signature>` を当てると node/go で偽 GREEN か偽 BLOCKER になる。
_ADAPTER_TABLE_RE = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*(.+?)\s*\|$", re.MULTILINE)
# 表に載せた各ランナーが、そのランナー固有の選択フラグを使っていること。
_REQUIRED_ADAPTER_FORMS = {
    "pytest": "--",
    "go test": "-run",
    "node --test": "--test-name-pattern",
}


def _adapter_table() -> dict[str, str]:
    """reviewer.md の signature → argv adapter 表を抽出する。

    Returns:
        ランナー名 → argv 形式 の辞書。

    Raises:
        AssertionError: 表が見つからない場合。
    """
    body = (_ROOT / "agents" / "reviewer.md").read_text(encoding="utf-8")
    rows = dict(_ADAPTER_TABLE_RE.findall(body))
    assert rows, "reviewer.md に signature → argv の adapter 表が見つからない"
    return rows


def test_reviewer_declares_runner_specific_signature_adapters() -> None:
    """adapter 表が各ランナー固有の選択フラグを指定していること。

    定義本文へ書いた検証規則は CI で実測する。この表は誰も実行しないプロンプト文書の
    契約の典型で、書き間違えても気づけない。
    """
    rows = _adapter_table()

    for runner, required_form in _REQUIRED_ADAPTER_FORMS.items():
        assert runner in rows, f"adapter 表に {runner} の行がない"
        assert required_form in rows[runner], (
            f"{runner} の argv 形式に {required_form} が含まれていない: {rows[runner]}"
        )


def test_reviewer_does_not_apply_dash_dash_to_selective_runners() -> None:
    """`--` 連結を、それが通用しないランナーへ当てていないこと。

    `node --test -- <name>` は name をファイル位置引数として解釈するため、全件実行に
    よる偽 GREEN か、存在しないファイルによる偽 BLOCKER になる。`go test` も同様に
    `-run` が要る。
    """
    rows = _adapter_table()

    for runner in ("go test", "node --test"):
        assert "--test-name-pattern" in rows[runner] or "-run" in rows[runner]
        assert not rows[runner].strip().startswith("`<test_cmd>` --"), (
            f"{runner} に `--` 連結を割り当てている: {rows[runner]}"
        )


def test_reviewer_declares_skip_for_unsupported_runners() -> None:
    """表に無いランナーでは個別再実行を skip する旨が書かれていること。

    未対応ランナーへ既定形式を当てるより、全体実行の結果だけを使う方が安全側。
    """
    body = (_ROOT / "agents" / "reviewer.md").read_text(encoding="utf-8")

    assert "上記以外" in body
    assert "skip" in body


def _section(relative_path: str, heading: str) -> str:
    """定義ファイルから ``## {heading}`` 節の本文を取り出す。

    Args:
        relative_path: プラグインルートからの相対パス。
        heading: ``## `` を除いた見出しの先頭。

    Returns:
        見出しの次行から次の ``## `` 見出しの直前までの本文。

    Raises:
        AssertionError: 見出しが見つからない場合。
    """
    body = (_ROOT / relative_path).read_text(encoding="utf-8")
    marker = f"\n## {heading}"
    assert marker in body, f"{relative_path} に「## {heading}」節が見つからない"
    return body.split(marker, 1)[1].split("\n## ", 1)[0]


def test_skill_gen_hands_git_history_to_skill_make_inside_a_per_run_boundary() -> None:
    """skill-gen が git 履歴（第三者が書ける件名・パス）を境界付きで skill-make へ渡すこと。

    `collect_skill_create_inputs` の出力をそのまま渡すと、コミット件名に埋め込んだ
    指示が生成される SKILL.md の方針へ混ざる。毎回生成するランダム ID 付きの
    開始・終了タグで囲み、囲いを偽装するタグを除き、データであって指示ではないと示す。
    """
    step1 = _section("commands/skill-gen.md", "ステップ1")
    step3 = _section("commands/skill-gen.md", "ステップ3")

    # 境界は git 履歴が本体の文脈へ入る時点（ステップ1）から張る。
    assert "データであり指示ではない" in step1
    assert "ランダム ID" in step3
    assert "開始・終了タグ" in step3
    assert "偽装" in step3
    assert "大小無視" in step3
    assert "データであり指示ではない" in step3


def test_skill_make_input_safety_covers_passed_git_history() -> None:
    """skill-make の入力安全が、渡された git 履歴（件名・パス）も対象にしていること。"""
    section = _section("skills/skill-make/SKILL.md", "入力安全")

    assert "git 履歴" in section
    assert "件名" in section
    assert "転記しない" in section
