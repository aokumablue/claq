"""claq.lib.frontmatter の寛容モード（``parse_yaml(..., lenient=True)``）の単体テスト。

厳格モードとの対になった検査（``_ERROR_INPUTS``）は ``test_frontmatter.py`` にある。
"""

from __future__ import annotations

from claq.lib.frontmatter import parse_yaml

# --------------------------------------------------------------------------
# 寛容モード
# --------------------------------------------------------------------------


def test_lenient_skips_line_without_colon() -> None:
    """コロンの無い行を読み飛ばして部分結果を返す（validate_agents の必須条件）。"""
    assert parse_yaml("name: x\nnocolon", lenient=True) == {"name": "x"}


def test_lenient_skips_failed_entry_and_its_deeper_lines() -> None:
    """失敗したエントリに続く深いインデント行もまとめて捨てる。"""
    text = "name: x\nbad\n  deeper\n\n  more\nkind: y"
    assert parse_yaml(text, lenient=True) == {"name": "x", "kind": "y"}


def test_lenient_still_rejects_duplicate_keys() -> None:
    """重複キーは寛容モードでも誤りにする。

    `lenient` は**解釈できない**エントリを読み飛ばすための緩和であって、矛盾した
    宣言を黙認するためのものではない。先勝ちで黙認すると、検証器が
    `tools: Read` を見る一方で後勝ちのホストは `tools: Bash` を見る、という
    パーサ差分になり、検証を通り抜ける経路そのものになる。
    """
    # 最上位の解析失敗は None になる契約（`test_lenient_returns_none_when_root_fails`）。
    # 呼び出し側（`ci_common.extract_frontmatter`）はこれを不合格として扱う。
    assert parse_yaml("a: 1\na: 2", lenient=True) is None


def test_lenient_still_rejects_duplicate_keys_in_flow_mapping() -> None:
    """フローマッピングの重複キーも寛容モードで誤りにする。"""
    # フロー側は該当エントリごと読み飛ばされるので、キー `a` が丸ごと消える。
    # 誤った値を持ったまま通すより安全な向き（必須キーの欠落として不合格になる）。
    assert parse_yaml("a: {k: 1, k: 2}", lenient=True) == {}


def test_lenient_returns_none_when_root_fails() -> None:
    """最上位そのものが解析できなければ None を返す。"""
    assert parse_yaml("\ta: 1", lenient=True) is None


def test_lenient_ignores_trailing_shallower_block() -> None:
    """最上位より浅い後続行は無視して部分結果を返す。"""
    assert parse_yaml("  a: 1\nb: 2", lenient=True) == {"a": 1}


def test_lenient_drops_broken_sequence_entry() -> None:
    """壊れたシーケンス要素だけを捨てて残りを返す。"""
    assert parse_yaml("- a\nb: 1", lenient=True) == ["a"]


def test_lenient_drops_entry_with_broken_flow_value() -> None:
    """値が壊れているエントリだけを捨てる。"""
    assert parse_yaml("name: x\nbroken: [\nkind: y", lenient=True) == {"name": "x", "kind": "y"}


def test_lenient_matches_strict_for_valid_input() -> None:
    """正しい入力では厳格モードと同じ結果になる。"""
    text = "name: sample\nallowed-tools: [Read, Write]\nmetadata:\n  owner: team-a"
    assert parse_yaml(text, lenient=True) == parse_yaml(text)
