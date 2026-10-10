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
